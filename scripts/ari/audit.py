"""Skills and MCP capability audit: installed, exposed, invoked, and what that can support.

Distinctions kept apart:
* installed   a skill directory with SKILL.md under a skills root, or an MCP server name
              in a config file (names only; values, commands and env are never read out)
* exposed     the capability appeared in a session's skill listing, MCP instructions or
              deferred tool list (attachment records in the transcript)
* invoked     an explicit Skill tool call, a slash command, or an mcp__<server>__ tool call
* indirect    a Read of a file inside the skill's directory (a skill can be followed
              without the Skill tool, so absence of a Skill call is not absence of use)

The absence of an invocation is never enough for a removal recommendation. Consequential
recommendations must carry the expected benefit, evidence, capability loss risk and a
validation method, and must meet the evidence thresholds; otherwise validate_recommendation
rejects them and they are downgraded to INVESTIGATE or INSUFFICIENT_EVIDENCE.

Nothing here changes a setting, an allowlist, a skill directory or an MCP registration.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import interpret
from .evidence import (
    EvidenceSet,
    exposed_by_session,
    loads_bounded,
    mcp_server_of,
    normalize_mcp_name,
)

ACTIONS = ("KEEP", "INVESTIGATE", "RESTRICT_EXPOSURE", "CONSOLIDATE", "COMPARE_CONFIGURATIONS",
           "CONSIDER_REMOVAL", "INSUFFICIENT_EVIDENCE", "UNKNOWN")
CONSEQUENTIAL = ("RESTRICT_EXPOSURE", "CONSOLIDATE", "CONSIDER_REMOVAL")
REQUIRED_FIELDS = ("expected_benefit", "evidence", "capability_loss_risk", "validation_method")
_NAME = re.compile(r"^name:\s*['\"]?([^'\"\n]+)['\"]?\s*$", re.MULTILINE)


@dataclass
class Capability:
    name: str
    category: str  # "skill" or "mcp_server"
    installed: bool = False
    install_roots: list[int] = field(default_factory=list)
    exposed_sessions: int = 0
    invocations: int = 0
    indirect_uses: int = 0
    invoked_sessions: int = 0
    listing_chars: int = 0
    tool_names: int = 0
    status: str = "UNKNOWN"
    task_requirement: str = "UNKNOWN: task needs are not inferable from transcripts"


@dataclass
class Recommendation:
    target: str
    category: str
    action: str
    reason: str
    expected_benefit: str = ""
    evidence: list[str] = field(default_factory=list)
    capability_loss_risk: str = ""
    validation_method: str = ""
    rejected: str | None = None


def skill_inventory(roots: Iterable[str | os.PathLike[str]]) -> dict[str, list[int]]:
    """Skill name -> indexes of the roots that hold it (SKILL.md frontmatter name, else dir)."""
    out: dict[str, list[int]] = defaultdict(list)
    for i, root in enumerate(roots):
        base = Path(root).expanduser().resolve()
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            real = skill_md.resolve()
            if not real.is_file() or base not in real.parents:
                continue  # a symlink out of the skills root is never read
            name = skill_md.parent.name
            try:
                head = skill_md.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                head = ""
            m = _NAME.search(head)
            if m:
                name = m.group(1).strip()
            out[name].append(i)
    return dict(out)


def mcp_inventory(configs: Iterable[str | os.PathLike[str]]) -> dict[str, list[int]]:
    """MCP server name -> indexes of config files that register it. Reads key names only."""
    out: dict[str, list[int]] = defaultdict(list)
    for i, cfg in enumerate(configs):
        path = Path(cfg).expanduser()
        try:
            data = loads_bounded(path.read_bytes())
        except OSError:
            continue
        if not isinstance(data, dict):
            continue
        blocks: list[Any] = [data.get("mcpServers")]
        projects = data.get("projects")
        if isinstance(projects, dict):
            blocks += [p.get("mcpServers") for p in projects.values() if isinstance(p, dict)]
        for block in blocks:
            if isinstance(block, dict):
                for raw in block:
                    if not isinstance(raw, str):
                        continue
                    name = normalize_mcp_name(raw)
                    if i not in out[name]:
                        out[name].append(i)
    return dict(out)


def observed_window_days(ev: EvidenceSet, now: datetime, requested: float) -> float:
    """The window actually covered: never longer than the oldest record on disk allows."""
    stamps = [e.ts for e in ev.exposures if e.ts is not None] + [u.ts for u in ev.usage]
    if not stamps:
        return 0.0
    return min(requested, (now - min(stamps)).total_seconds() / 86400)


DUPLICATE_REASON = "servers expose identical tool name sets"


def validate_recommendation(rec: Recommendation, cap: Capability | None, window_days: float,
                            min_sessions: int, min_days: float,
                            coverage_gaps: int = 0) -> str | None:
    """Return a rejection reason for an unsupported consequential recommendation, else None."""
    if rec.action not in ACTIONS:
        return f"unknown action {rec.action}"
    if rec.action not in CONSEQUENTIAL:
        return None
    missing = [f for f in REQUIRED_FIELDS if not getattr(rec, f)]
    if missing:
        return "missing " + ", ".join(missing)
    if rec.action in ("CONSIDER_REMOVAL", "RESTRICT_EXPOSURE"):
        if cap is None:
            return "no capability record behind the recommendation"
        if cap.invocations or cap.indirect_uses:
            return "capability was used in the window"
        if cap.exposed_sessions < min_sessions:
            return f"exposed in {cap.exposed_sessions} sessions, at least {min_sessions} needed"
        if not window_days >= min_days:  # also refuses a NaN threshold or window
            return f"window {window_days:.1f} days, at least {min_days:g} needed"
        if coverage_gaps:
            return (f"{coverage_gaps} unread files or lines could hold a use (see the coverage "
                    "warnings); review them, then rerun with --accept-coverage-gaps")
        if rec.action == "CONSIDER_REMOVAL" and not cap.installed:
            return "not installed under an audited root, so not removable from here"
    if rec.action == "CONSOLIDATE" and coverage_gaps:  # removal class too (ARI2-R9)
        return (f"{coverage_gaps} unread files or lines could hold a use that tells the "
                "servers apart; review them, then rerun with --accept-coverage-gaps")
    return None


def audit(
    ev: EvidenceSet,
    skills_roots: list[str],
    mcp_configs: list[str],
    window_days: float,
    min_sessions: int = 20,
    min_days: float = 14,
    accept_coverage_gaps: bool = False,
) -> dict[str, Any]:
    caps: dict[tuple[str, str], Capability] = {}
    gaps = 0 if accept_coverage_gaps else ev.coverage.unread_gaps()

    def cap(cat: str, name: str) -> Capability:
        key = (cat, name)
        if key not in caps:
            caps[key] = Capability(name=name, category=cat)
        return caps[key]

    for name, idx in skill_inventory(skills_roots).items():
        c = cap("skill", name)
        c.installed, c.install_roots = True, idx
    for name, idx in mcp_inventory(mcp_configs).items():
        c = cap("mcp_server", name)
        c.installed, c.install_roots = True, idx

    exposed = exposed_by_session(ev.exposures)
    sizes: dict[str, list[int]] = defaultdict(list)
    for e in ev.exposures:
        for k, v in e.sizes.items():
            sizes[k].append(v)
    server_tools: dict[str, set[str]] = defaultdict(set)
    for e in ev.exposures:
        if e.category == "deferred_tool":
            for t in e.added:
                s = mcp_server_of(t)
                if s:
                    server_tools[s].add(t.split("__", 2)[2])
    for cats in exposed.values():
        for s in cats.get("skill", set()):
            cap("skill", s).exposed_sessions += 1
        for s in cats.get("mcp_server", set()):
            cap("mcp_server", s).exposed_sessions += 1
    used_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for tev in ev.tools:
        if tev.skill and tev.via in ("tool_use", "slash_command"):
            c = cap("skill", tev.skill)
            c.invocations += 1
            used_sessions[("skill", tev.skill)].add(tev.session_id)
        elif tev.skill and tev.via == "skill_file_read":
            cap("skill", tev.skill).indirect_uses += 1
        if tev.mcp_server:
            c = cap("mcp_server", tev.mcp_server)
            c.invocations += 1
            used_sessions[("mcp_server", tev.mcp_server)].add(tev.session_id)
    for (cat, name), c in caps.items():
        c.invoked_sessions = len(used_sessions.get((cat, name), set()))
        if cat == "skill" and sizes.get(name):
            c.listing_chars = round(sum(sizes[name]) / len(sizes[name]))
        if cat == "mcp_server":
            c.tool_names = len(server_tools.get(name, set()))
        if c.invocations:
            c.status = "INVOKED"
        elif c.indirect_uses:
            c.status = "INDIRECT_ONLY"
        elif c.exposed_sessions:
            c.status = "EXPOSED_UNOBSERVED"
        elif c.installed:
            c.status = "INSTALLED_NOT_SEEN_EXPOSED"
    recs = _recommend(caps, server_tools, exposed, window_days, min_sessions, min_days, gaps)
    findings = []
    for r in recs:
        rc = caps.get((r.category, r.target))
        if r.reason == DUPLICATE_REASON:
            servers = r.target.split(",")
            f = interpret.consolidate_finding(servers, len(server_tools.get(servers[0], set())),
                                              r.action, r.rejected or r.reason, ev,
                                              accept_coverage_gaps)
        elif rc is None or rc.status not in ("EXPOSED_UNOBSERVED",
                                             "INSTALLED_NOT_SEEN_EXPOSED"):
            continue
        else:
            f = interpret.no_use_finding(asdict(rc), r.action, r.rejected or r.reason, ev,
                                         min_sessions, accept_coverage_gaps)
        errs = interpret.validate_finding(f)
        if errs and r.action in interpret.REMOVAL_CLASS:
            # second, independent gate (ARI2-R9): the readings must allow the decision
            r.action, r.rejected = "INVESTIGATE", "finding contract: " + "; ".join(errs)
            f.decision, f.decision_reason = r.action, r.rejected
        findings.append(f.to_dict())
    sessions = len(exposed)
    return {
        "schema": "ari-capability-audit/1",
        "window_days": round(window_days, 2),
        "sessions_with_exposure_records": sessions,
        "thresholds": {"min_exposed_sessions": min_sessions, "min_window_days": min_days,
                       "unread_gaps_blocking_removal": gaps},
        "capabilities": [asdict(c) for c in sorted(caps.values(),
                                                   key=lambda c: (c.category, c.name))],
        "recommendations": [asdict(r) for r in recs],
        "findings": [interpret.coverage_finding(ev).to_dict()] + findings,
        "coverage": {"unread_gaps": ev.coverage.unread_gaps(),
                     "excluded": ev.coverage.excluded(),
                     "warnings": ev.coverage.warnings()},
        "drift": _drift(ev),
        "notes": [
            "Exposure comes from internal attachment records whose format can change between "
            "Claude Code versions; a capability not seen exposed may still be exposed.",
            "Token benefits are estimates (listing characters / 4), not measurements.",
            "No setting was changed. Every action here is a recommendation.",
        ],
    }


def _recommend(caps: dict[tuple[str, str], Capability], server_tools: dict[str, set[str]],
               exposed: dict[str, dict[str, set[str]]], window_days: float,
               min_sessions: int, min_days: float, gaps: int = 0) -> list[Recommendation]:
    out: list[Recommendation] = []
    for (cat, name), c in sorted(caps.items()):
        if c.status in ("INVOKED", "INDIRECT_ONLY"):
            out.append(Recommendation(name, cat, "KEEP",
                                      f"{c.invocations} invocations, {c.indirect_uses} indirect "
                                      f"uses in {c.invoked_sessions} sessions"))
            continue
        if c.status == "INSTALLED_NOT_SEEN_EXPOSED":
            out.append(Recommendation(name, cat, "UNKNOWN",
                                      "installed but never seen exposed: disabled, filtered, or "
                                      "the listing format was not recognised"))
            continue
        if c.status != "EXPOSED_UNOBSERVED":
            continue
        if cat == "skill":
            tokens = round(c.listing_chars / 4)
            rec = Recommendation(
                name, cat, "CONSIDER_REMOVAL",
                f"exposed in {c.exposed_sessions} sessions, no Skill call, slash command or "
                f"file read in {window_days:.1f} days",
                expected_benefit=f"about {tokens} tokens of skill listing per session start "
                                 "(estimate, characters / 4)",
                evidence=[f"exposed_sessions={c.exposed_sessions}", "invocations=0",
                          "indirect_uses=0"],
                capability_loss_risk="a future task that needs this skill would not find it; "
                                     "work done by hand may lose its procedure",
                validation_method="move the skill folder aside for one matched week (reversible), "
                                  "then compare first request context and check no task asked "
                                  "for it",
            )
        else:
            rec = Recommendation(
                name, cat, "RESTRICT_EXPOSURE",
                f"exposed in {c.exposed_sessions} sessions, no mcp__{name}__ call in "
                f"{window_days:.1f} days",
                expected_benefit=f"{c.tool_names} deferred tool names plus server instructions "
                                 "removed from sessions and general-purpose subagents",
                evidence=[f"exposed_sessions={c.exposed_sessions}", "invocations=0"],
                capability_loss_risk="tasks that need this server would have to enable it again",
                validation_method="disable the server for one project or keep it out of subagent "
                                  "tool lists, and compare first request context on matched runs",
            )
        reason = validate_recommendation(rec, c, window_days, min_sessions, min_days, gaps)
        if reason:
            enough = c.exposed_sessions >= min_sessions and window_days >= min_days
            rec = Recommendation(name, cat, "INVESTIGATE" if enough else "INSUFFICIENT_EVIDENCE",
                                 rec.reason, evidence=rec.evidence, rejected=reason)
        out.append(rec)
    # MCP servers whose deferred tool name sets are identical are consolidation candidates.
    by_tools: dict[frozenset[str], list[str]] = defaultdict(list)
    for s, tools in server_tools.items():
        if len(tools) >= 3:
            by_tools[frozenset(tools)].append(s)
    for group in by_tools.values():
        if len(group) > 1:
            names = sorted(group)
            rec = Recommendation(
                ",".join(names), "mcp_server", "CONSOLIDATE",
                DUPLICATE_REASON,
                expected_benefit=f"{len(server_tools[names[0]])} duplicate tool names per "
                                 "session",
                evidence=[f"identical tool sets: {', '.join(names)}"],
                capability_loss_risk="the servers may point at different accounts or "
                                     "workspaces; merging could lose one",
                validation_method="confirm both servers' targets, then compare one week with a "
                                  "single server enabled",
            )
            reason = validate_recommendation(rec, None, window_days, min_sessions, min_days,
                                             gaps)
            if reason:
                rec.action, rec.rejected = "INVESTIGATE", reason
            out.append(rec)
    for (cat, name), c in sorted(caps.items()):
        if cat == "skill" and len(c.install_roots) > 1:
            out.append(Recommendation(name, cat, "COMPARE_CONFIGURATIONS",
                                      f"installed under {len(c.install_roots)} roots; one copy "
                                      "may shadow the other"))
    return out


def _drift(ev: EvidenceSet) -> dict[str, Any]:
    """Exposed sets of the earliest and latest session with exposure records, per category."""
    exposed = exposed_by_session(ev.exposures)
    first_ts: dict[str, datetime] = {}
    for e in ev.exposures:
        if e.ts is not None and (e.session_id not in first_ts or e.ts < first_ts[e.session_id]):
            first_ts[e.session_id] = e.ts
    ordered = sorted(first_ts, key=lambda s: first_ts[s])
    if len(ordered) < 2:
        return {"status": "INSUFFICIENT_EVIDENCE", "sessions": len(ordered)}
    a, b = exposed.get(ordered[0], {}), exposed.get(ordered[-1], {})
    cats = sorted(set(a) | set(b))
    return {
        "status": "OK",
        "from": first_ts[ordered[0]].isoformat(),
        "to": first_ts[ordered[-1]].isoformat(),
        "changes": {c: {"added": sorted(b.get(c, set()) - a.get(c, set())),
                        "removed": sorted(a.get(c, set()) - b.get(c, set()))} for c in cats
                    if c != "deferred_tool"},
        "deferred_tool_count": {"from": len(a.get("deferred_tool", set())),
                                "to": len(b.get("deferred_tool", set()))},
    }


def render_text(r: dict[str, Any]) -> str:
    lines = [f"Capability audit over {r['window_days']} days, "
             f"{r['sessions_with_exposure_records']} sessions with exposure records"]
    counts: dict[str, int] = defaultdict(int)
    for c in r["capabilities"]:
        counts[f"{c['category']}:{c['status']}"] += 1
    for k, v in sorted(counts.items()):
        lines.append(f"  {k:<44} {v:>5}")
    acts: dict[str, int] = defaultdict(int)
    for rec in r["recommendations"]:
        acts[rec["action"]] += 1
    lines.append("Recommendations (none applied): " + ", ".join(
        f"{a} {n}" for a, n in sorted(acts.items())))
    for rec in r["recommendations"]:
        if rec["action"] in CONSEQUENTIAL:
            lines.append(f"  {rec['action']:<18} {rec['category']:<10} {rec['target'][:40]:<40} "
                         f"{rec['expected_benefit']}")
    d = r["drift"]
    if d.get("status") == "OK":
        for c, ch in d["changes"].items():
            if ch["added"] or ch["removed"]:
                lines.append(f"Drift {c}: +{len(ch['added'])} -{len(ch['removed'])} "
                             f"({d['from'][:10]} to {d['to'][:10]})")
    for n in r["notes"]:
        lines.append(f"note: {n}")
    return "\n".join(lines)

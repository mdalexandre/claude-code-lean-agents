#!/usr/bin/env python3
"""Bounded abductive discovery over the known ARI anomalies, on your own transcripts.

Runs deterministic discriminating tests for anomalies A (unread files), C (nightly
starvation), D (duplicate copies), F (report and share disclosure) and G (format scope),
and reads optional audit and burn JSON for B (no observed use) and E (high token use).
Each test writes an evidence file into --out; the ledger binds it by sha256. Statuses are
evidence states, not probabilities. Reads only; writes only into --out.

Usage:
    python3 ari_discover.py --out DIR [--days 14] [--audit audit.json] [--burn burn.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ari import learn, report
from ari.cli_common import add_common, resolve
from ari.discovery import Ledger
from ari.evidence import Coverage, EvidenceSet, UsageEvent, ingest


def _evidence(out: Path, name: str, data: Any) -> Path:
    p = out / f"{name}.json"
    p.write_text(json.dumps(data, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return p


def anomaly_a(led: Ledger, ev: EvidenceSet, out: Path) -> None:
    cov = ev.coverage
    q = led.ask("Transcript files found under the root were not read by the parser")
    h1 = led.propose(q.id, "the unread files are not transcripts (indexes or project data)",
                     "v0.1.5 audit: 60 unrecognised .jsonl files",
                     ["no unread file carries a transcript marker"],
                     "any unread file with a usage, tool_use, command or attachment marker")
    h2 = led.propose(q.id, "the unread files are transcripts written in a layout the parser "
                     "does not know", "same", ["unread files carry transcript markers"],
                     "a full marker scan finds none")
    # a compatible fact, not a rival of h1 or h2: its own question with its own rival
    qj = led.ask("Workflow journals name agent runs; are all their transcripts present", q.id)
    h3 = led.propose(qj.id, "workflow journals point at agent runs whose transcripts are "
                     "missing", "journals list started agents",
                     ["started agent ids without agent-<id>.jsonl"],
                     "every started agent has a transcript")
    h3b = led.propose(qj.id, "every agent a workflow started wrote a transcript the parser "
                      "reads", "rival of the above", ["no started id without a transcript"],
                      "a started id without agent-<id>.jsonl")
    rows = [r for r in ev.manifest if r.get("class") not in ("main", "subagent")]
    f = _evidence(out, "A_classification", {
        "classes": {c: sum(1 for r in rows if r["class"] == c)
                    for c in sorted({r["class"] for r in rows})},
        "unknown_with_markers": cov.files_unrecognized,
        "journal_agents_missing": cov.journal_agents_missing})
    led.record(h1.id, "refutes" if cov.files_unrecognized else "supports", f,
               f"{cov.files_unrecognized} unread files carry markers")
    led.record(h2.id, "supports" if cov.files_unrecognized else "refutes", f,
               f"{cov.files_unrecognized} unread files carry markers")
    miss = cov.journal_agents_missing
    led.record(h3.id, "supports" if miss else "refutes", f,
               f"{miss} started agents without a transcript")
    led.record(h3b.id, "refutes" if miss else "supports", f,
               f"{miss} started agents without a transcript")


def anomaly_c(led: Ledger, ev: EvidenceSet, out: Path, now: datetime, days: int) -> None:
    q = led.ask("Nightly runs drew no lesson while sessions were open")
    h1 = led.propose(q.id, "the global rule deferring lessons whenever any file is active "
                     "blocks them", "v0.1.5 rule: defer = files_active > 0",
                     ["active files exist and detectable changes exist"],
                     "no detectable change on any complete day")
    h2 = led.propose(q.id, "no complete day had a detectable starting context change",
                     "detector thresholds 25 percent and 5k tokens",
                     ["the detector is quiet on every day"], "the detector fires on some day")
    today = now.date()
    span = [today - timedelta(days=i) for i in range(days, 0, -1)]
    snaps = {k: v for k, v in learn.daily_snapshots(ev, span).items() if v["requests"]}
    fired = [a for d in sorted(snaps) for a in learn.detect(snaps, d)]
    f = _evidence(out, "C_starvation", {"files_active": ev.coverage.files_active,
                                        "days": len(snaps), "detector_hits": fired})
    if fired:
        led.record(h2.id, "refutes", f, f"{len(fired)} detector hits")
        led.record(h1.id, "supports" if ev.coverage.files_active else "refutes", f,
                   f"{ev.coverage.files_active} active files")
    else:
        led.record(h2.id, "supports", f, "detector quiet on every day")
        led.record(h1.id, "refutes", f, "nothing to defer")


def anomaly_d(led: Ledger, ev: EvidenceSet, out: Path) -> None:
    q = led.ask("Some requests have copies with different usage numbers")
    h1 = led.propose(q.id, "every differing copy is streaming growth or a zeroed resume copy",
                     "observed on 2026-11-01 in round 4", ["no AMBIGUOUS class"],
                     "any copy pair that differs outside output and is not all zero")
    h2 = led.propose(q.id, "some copies differ in cache or input fields for an unknown reason",
                     "v0.1.5 limit: mixed cache shapes", ["AMBIGUOUS class present"],
                     "no AMBIGUOUS class")
    h3 = led.propose(q.id, "two different requests share one request id", "competitor",
                     ["same id, different content"], "transcripts do not keep request content")
    unc = ev.total_uncertainty()
    f = _evidence(out, "D_duplicates", {"classes": ev.coverage.duplicate_classes,
                                        "total_uncertainty": unc})
    amb = unc["ambiguous_requests"]
    led.record(h1.id, "refutes" if amb else "supports", f, f"{amb} ambiguous requests")
    led.record(h2.id, "supports" if amb else "refutes", f, f"{amb} ambiguous requests")
    led.unresolved(h3.id, "message content is not compared, so a shared id cannot be told "
                          "from two copies; counted as one request with a band")


def anomaly_f(led: Ledger, ev: EvidenceSet, out: Path) -> None:
    q = led.ask("The report and share file could disclose or execute injected text")
    h1 = led.propose(q.id, "transcript derived text reaches the HTML unescaped",
                     "agent type and skill names come from local files",
                     ["an injected tag survives in the page"],
                     "rendering a hostile fixture yields no live tag")
    h2 = led.propose(q.id, "the share screen passes identifying or structured injection",
                     "share is built from local data", ["a hostile share passes the screen"],
                     "the screen refuses every hostile fixture")
    evil = "<script>alert(1)</script><img src=x onerror=1>"
    fx = EvidenceSet([], [UsageEvent("r", 0, 1, "s", "a", evil, "subagent", "m", "2.1.1",
                            datetime(2026, 11, 1, tzinfo=timezone.utc), 1, 1, 0, 0, 1)],
            [], [], Coverage(requests=1), (None, None))
    finding = {"id": "F-x", "subject": evil, "observed": evil, "coverage": "COMPLETE",
               "readings": [{"id": evil, "text": evil, "status": "SUPPORTED",
                             "supports": [evil], "weakens": []}] * 2,
               "decision": evil, "decision_reason": evil, "would_change": [evil],
               "provenance": {"parser": evil, "window": [evil], "files_read": 1,
                              "unread_gaps": 0}}
    page = report.render_html(fx, None, None, [], [], datetime(2026, 11, 1,
                                                               tzinfo=timezone.utc), [finding])
    # only a real tag is live; "onerror=" also appears inside correctly escaped text
    live = [t for t in ("<script", "<img") if t in page]
    base = report.build_share(ev)
    hostile = [{**base, "x": "/home/someone"},
               {**base, "agent_types": [{"agent_type": "<script>", "runs_bucket": "5-9"}]},
               {**base, "claude_code_minor_versions": ["2.1</script>"]},
               {**base, "suppressed_groups": "someone@example.com"}]
    passed = [h for h in hostile if not report.screen_share(h)]
    f = _evidence(out, "F_disclosure", {"live_tags": live, "hostile_shares_passed":
                                        len(passed), "hostile_shares": len(hostile)})
    led.record(h1.id, "supports" if live else "refutes", f, f"live tags {live}")
    led.record(h2.id, "supports" if passed else "refutes", f,
               f"{len(passed)} of {len(hostile)} hostile shares passed")


def anomaly_g(led: Ledger, ev: EvidenceSet, out: Path) -> None:
    q = led.ask("Lessons were learned on one transcript version and may not generalise")
    h1 = led.propose(q.id, "the window holds more than one transcript format",
                     "Claude Code updates often", ["several major.minor values"],
                     "a single major.minor value")
    h2 = led.propose(q.id, "lessons hold across transcript versions", "untested",
                     ["replay passes on another version"], "replay fails on another version")
    fmts = ev.coverage.format_versions
    f = _evidence(out, "G_formats", {"formats": fmts})
    led.record(h1.id, "supports" if len(fmts) > 1 else "refutes", f, f"formats {fmts}")
    led.unresolved(h2.id, "no lesson has been replayed on a second transcript version; "
                          "replays report versions_not_covered when one appears")


def anomaly_b(led: Ledger, aud: dict[str, Any], out: Path) -> None:
    q = led.ask("Many skills and MCP servers show no observed use")
    nouse = [f for f in aud.get("findings", []) if f["id"].startswith("F-nouse-")]

    def count(rid: str, status: str) -> int:
        return sum(1 for f in nouse for r in f["readings"]
                   if r["id"] == rid and r["status"] == status)
    f = _evidence(out, "B_no_use", {"findings": len(nouse), "by_reading": {
        rid: {s: count(rid, s) for s in ("SUPPORTED", "WEAKENED", "UNRESOLVED")}
        for rid in ("unused", "coverage_gap", "not_exposed")}})
    h1 = led.propose(q.id, "the capabilities are exposed but genuinely unused",
                     "audit findings", ["unused readings SUPPORTED"],
                     "coverage gap readings SUPPORTED for them")
    h2 = led.propose(q.id, "the observer lacks coverage of their use", "audit findings",
                     ["coverage gap readings SUPPORTED"], "coverage gap readings WEAKENED")
    h3 = led.propose(q.id, "they are installed but never exposed to an agent",
                     "audit findings", ["not exposed readings SUPPORTED"],
                     "not exposed readings WEAKENED")
    for h, rid in ((h1, "unused"), (h2, "coverage_gap"), (h3, "not_exposed")):
        sup, weak = count(rid, "SUPPORTED"), count(rid, "WEAKENED")
        if sup and weak:
            led.unresolved(h.id, f"holds for {sup} capabilities and not for {weak}: a "
                                 "per capability reading, not one explanation")
        else:
            led.record(h.id, "supports" if sup else "refutes", f,
                       f"{sup} supported, {weak} weakened")


def anomaly_e(led: Ledger, burn: dict[str, Any], out: Path) -> None:
    q = led.ask("Weekly token use is high and its causes are uncertain")
    by = burn.get("by_agent_type", {})
    tot = sum(sum(r.get(k, 0) for k in ("input_tokens", "cache_creation_5m",
                                         "cache_creation_1h", "cache_read", "output"))
              for r in by.values()) or 1
    cache_read = sum(r.get("cache_read", 0) for r in by.values())
    top = max((r.get("metric", 0) for r in by.values()), default=0)
    unc = burn.get("total_uncertainty", {})
    f = _evidence(out, "E_burn_composition", {"cache_read_share": round(cache_read / tot, 4),
                                              "top_agent_type_share": round(top / tot, 4),
                                              "uncertainty": unc})
    h3 = led.propose(q.id, "a configuration change caused the increase", "untested",
                     ["a controlled comparison shows the change"],
                     "transcripts give no controlled comparison")
    h4 = led.propose(q.id, "the use follows the amount of work done, not a change in cost "
                     "per request", "untested", ["requests and tokens move together"],
                     "tokens per request moved while requests did not")
    for h in (h3, h4):
        led.unresolved(h.id, "observational transcripts cannot separate causes; needs a "
                             "matched experiment")
    # composition facts: each its own question with its negation as the rival
    qc = led.ask("Which token category makes up most of the counted total", q.id)
    share = cache_read / tot
    h1 = led.propose(qc.id, "cache reads make up most of the counted tokens",
                     "burn categories", ["cache read share above one half"],
                     "cache read share at or below one half")
    h1b = led.propose(qc.id, "input, cache writes and output make up most of the counted "
                      "tokens", "rival", ["cache read share at or below one half"],
                      "cache read share above one half")
    note = f"cache read share {share:.4f}; one half is a JUDGMENT threshold for 'most'"
    led.record(h1.id, "supports" if share > 0.5 else "refutes", f, note)
    led.record(h1b.id, "refutes" if share > 0.5 else "supports", f, note)
    qb = led.ask("Do ambiguous duplicate copies change the total materially", q.id)
    h2 = led.propose(qb.id, "ambiguous duplicate copies inflate the total materially",
                     "duplicate classes", ["band width above 1 percent of the total"],
                     "band width at or below 1 percent")
    h2b = led.propose(qb.id, "the ambiguity band is immaterial to the total",
                      "rival", ["band width at or below 1 percent"],
                      "band width above 1 percent")
    width = unc.get("upper", 0) - unc.get("lower", 0)
    big = width > 0.01 * max(unc.get("upper", 0), 1)
    note = f"band width {width}; 1 percent is a JUDGMENT threshold"
    led.record(h2.id, "supports" if big else "refutes", f, note)
    led.record(h2b.id, "refutes" if big else "supports", f, note)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    add_common(ap, days=14)
    ap.add_argument("--out", required=True, help="directory for evidence files and the ledger")
    ap.add_argument("--audit", default=None, help="skill_mcp_audit.py --json output (B)")
    ap.add_argument("--burn", default=None, help="weekly_burn.py --json output (E)")
    ap.add_argument("--quiet-seconds", type=float, default=1800)
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    roots, now, since = resolve(args)
    skills = [os.path.expanduser("~/.claude/skills")]
    try:
        ev = ingest(roots, since=since, until=now, want_exposure=True, skills_roots=skills,
                    quiet_seconds=args.quiet_seconds, now=now)
        led = Ledger()
        anomaly_a(led, ev, out)
        anomaly_c(led, ev, out, now, max(1, int(args.days)))
        anomaly_d(led, ev, out)
        anomaly_f(led, ev, out)
        anomaly_g(led, ev, out)
        if args.audit:
            anomaly_b(led, json.loads(Path(args.audit).read_text(encoding="utf-8")), out)
        if args.burn:
            anomaly_e(led, json.loads(Path(args.burn).read_text(encoding="utf-8")), out)
    except (OSError, ValueError, KeyError) as exc:
        print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    led.dump(out / "ledger.json")
    for h in led.hypotheses.values():
        print(f"{h.status:<12} {h.id} {h.mechanism}")
    viol = led.validate()
    for v in viol:
        print(f"violation: {v}", file=sys.stderr)
    return 1 if viol else 0


if __name__ == "__main__":
    sys.exit(main())

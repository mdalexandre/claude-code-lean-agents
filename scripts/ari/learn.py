"""Nightly, opt in procedural learning with a gated lesson lifecycle (ARI2-R11 to R15).

Lifecycle: DISCOVERED -> SPECIFIED -> CANDIDATE -> REPLAY_TESTED -> QUALIFIED ->
APPROVAL_PENDING -> PROMOTED, plus QUARANTINED (kept with its evidence, never deleted).
The v1 names OBSERVED, HYPOTHESIZED, TESTED, VALIDATED, RETAINED and REJECTED are accepted
as aliases. The nightly run creates SPECIFIED lessons at most. Every later step goes
through promote() or add_result(), each with a contract; a nightly summary can never
qualify a procedure, and PROMOTED here is a lesson state, never a vgtp registry write.

Per source eligibility: active files are read up to their last complete line and only
days before the run day are snapshotted, so an open session no longer blocks lessons. A
source whose sampled checkpoint shows truncation or replacement makes the days it
contributed unstable for that run: recomputed, and no lesson is drawn from them.

Idempotence: snapshot keys are calendar days and lesson ids hash (kind, scope, metric,
day), so a repeated run on the same data finds the same ids and adds nothing.
Interruption: the state file is replaced atomically (write, fsync, rename). A journal file
marks a run in progress; if a run finds a stale journal it records the recovery and
recomputes, which is safe because the computation is idempotent.

Deterministic, no model calls, no network. An optional model layer is not implemented.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ARI_VERSION
from .abduction import HypothesisSet, context_gap, from_dict, record_result
from .evidence import PARSER_VERSION, EvidenceSet, compare_checkpoint, exposed_by_session

STATE_SCHEMA = "ari-learn-state/2"
V1_SCHEMA = "ari-learn-state/1"
LIFECYCLE = ("DISCOVERED", "SPECIFIED", "CANDIDATE", "REPLAY_TESTED", "QUALIFIED",
             "APPROVAL_PENDING", "PROMOTED")
TERMINAL = ("QUARANTINED",)
ALIASES = {"OBSERVED": "DISCOVERED", "HYPOTHESIZED": "SPECIFIED", "TESTED": "REPLAY_TESTED",
           "VALIDATED": "QUALIFIED", "RETAINED": "PROMOTED", "REJECTED": "QUARANTINED"}
NIGHTLY_MAX_STATE = "SPECIFIED"
APPROVAL_SCHEMA = "ari-lesson-approval/1"
ROLLBACK_NOTE = ("A lesson changes no setting. Rolling it back means moving it to QUARANTINED; "
                 "an experiment it names is undone by that experiment's own validation method.")


def canonical(state: str) -> str:
    return ALIASES.get(state, state)


def code_sha256() -> str:
    """Hash of the ARI package sources: replay and approval bind to it."""
    h = hashlib.sha256()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


class PromotionError(Exception):
    pass


def _empty_state() -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "ari_version": ARI_VERSION, "snapshots": {},
            "lessons": {}, "hypotheses": {}, "runs": [], "manifest": {}}


def load_state(state_dir: Path) -> dict[str, Any]:
    """Read the state; a v1 file is migrated in memory (saved with a .v1.bak copy)."""
    path = state_dir / "state.json"
    if not path.exists():
        return _empty_state()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and data.get("schema") == V1_SCHEMA:
        data["schema"] = STATE_SCHEMA
        data.setdefault("manifest", {})
        for lesson in data.get("lessons", {}).values():
            lesson["state"] = canonical(lesson["state"])
        data["_migrated_from"] = V1_SCHEMA
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        raise ValueError(f"{path} is not an {STATE_SCHEMA} file")
    for key in ("snapshots", "lessons", "hypotheses", "manifest"):
        if not isinstance(data.get(key, {}), dict):
            raise ValueError(f"{path}: {key} is not an object")
    return data


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if state.pop("_migrated_from", None):
        bak = state_dir / "state.json.v1.bak"
        if not bak.exists():
            bak.write_bytes((state_dir / "state.json").read_bytes())
    tmp = state_dir / "state.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, state_dir / "state.json")


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def daily_snapshots(ev: EvidenceSet, days: list[date]) -> dict[str, dict[str, Any]]:
    """Per complete day: starting context per agent type, agent mix, exposure counts."""
    starts: dict[date, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    versions: dict[date, set[str]] = defaultdict(set)
    for u in ev.first_requests().values():
        starts[u.ts.date()][u.agent_type].append(u.context)
        versions[u.ts.date()].add(u.version)
    mix: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in ev.usage:
        mix[u.ts.date()][u.agent_type] += 1
    exposed = exposed_by_session(ev.exposures)
    first_seen: dict[str, date] = {}
    for x in ev.exposures:
        if x.ts is not None:
            d = x.ts.date()
            if x.session_id not in first_seen or d < first_seen[x.session_id]:
                first_seen[x.session_id] = d
    expo: dict[date, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for sid, d in first_seen.items():
        for cat, names in exposed.get(sid, {}).items():
            expo[d][cat].append(len(names))
    out: dict[str, dict[str, Any]] = {}
    for d in days:
        total = sum(mix[d].values())
        out[d.isoformat()] = {
            "start_context": {t: {"runs": len(v), "median": statistics.median(v)}
                              for t, v in sorted(starts[d].items())},
            "agent_mix": {t: round(n / total, 4) for t, n in sorted(mix[d].items())}
            if total else {},
            "requests": total,
            "exposure": {c: round(statistics.fmean(v), 1) for c, v in sorted(expo[d].items())},
            "versions": sorted(versions[d]),
        }
    return out


def _baseline(snaps: dict[str, dict[str, Any]], day: str, agent_type: str, min_runs: int
              ) -> tuple[float, list[str]] | None:
    # an unstable day (its source files still changing) never anchors a comparison
    prior = [k for k in sorted(snaps) if k < day and not snaps[k].get("unstable")]
    vals = [(k, snaps[k]["start_context"][agent_type]["median"]) for k in prior
            if snaps[k]["start_context"].get(agent_type, {}).get("runs", 0) >= min_runs]
    if len(vals) < 3:
        return None
    vals = vals[-7:]
    return statistics.median(v for _, v in vals), [k for k, _ in vals]


def detect(snaps: dict[str, dict[str, Any]], day: str, min_runs: int = 3,
           pct: float = 0.25, abs_tokens: int = 5000) -> list[dict[str, Any]]:
    """Starting context changes on `day` against the median of up to 7 earlier days."""
    found: list[dict[str, Any]] = []
    cur = snaps.get(day)
    if not cur:
        return found
    for t, row in cur["start_context"].items():
        if row["runs"] < min_runs:
            continue
        base = _baseline(snaps, day, t, min_runs)
        if base is None:
            continue
        b, keys = base
        delta = row["median"] - b
        if abs(delta) >= abs_tokens and b > 0 and abs(delta) / b >= pct:
            found.append({"kind": "start_context_change", "scope": t, "metric": "median",
                          "day": day, "value": row["median"], "baseline": b,
                          "baseline_days": keys, "delta": delta})
    return found


def _signals(snaps: dict[str, dict[str, Any]], a: dict[str, Any]) -> dict[str, list[str]]:
    cur = snaps[a["day"]]
    prev = [snaps[k] for k in a["baseline_days"]]
    sig: dict[str, list[str]] = defaultdict(list)
    for cat, key in (("skill", "skills"), ("mcp_server", "mcp"), ("deferred_tool", "mcp")):
        before = [p["exposure"].get(cat) for p in prev if p["exposure"].get(cat) is not None]
        now = cur["exposure"].get(cat)
        if now is not None and before and abs(now - statistics.median(before)) >= 1:
            sig[key].append(f"{cat} exposure {statistics.median(before):.0f} -> {now:.0f}")
    old_versions = set().union(*(set(p["versions"]) for p in prev)) if prev else set()
    if set(cur["versions"]) - old_versions:
        sig["version_model"].append("new Claude Code version: " +
                                    ", ".join(sorted(set(cur["versions"]) - old_versions)))
    return dict(sig)


def _source_days(ev: EvidenceSet) -> dict[str, set[str]]:
    days: dict[str, set[str]] = defaultdict(set)
    for u in ev.usage:
        if 0 <= u.source < len(ev.sources):
            days[ev.sources[u.source].relpath].add(u.ts.date().isoformat())
    return days


def update_manifest(state: dict[str, Any], ev: EvidenceSet) -> tuple[dict[str, int], set[str]]:
    """Compare this run's sources with the stored manifest (ARI2-R3, R11). Returns counts per
    change class and the days made unstable by a TRUNCATED or REPLACED source."""
    prev: dict[str, Any] = state.setdefault("manifest", {})
    days = _source_days(ev)
    counts: dict[str, int] = defaultdict(int)
    unstable: set[str] = set()
    for row in ev.manifest:
        if row.get("class") not in ("main", "subagent") or "path" not in row:
            continue
        key = row["path"]
        old = prev.get(key)
        if old is None:
            change = "NEW"
        else:
            change = compare_checkpoint(old, Path(key))
            if change == "UNCHANGED" and row.get("offset") != old.get("offset"):
                change = "GROWN"  # a partial last line was completed
        counts[change] += 1
        mine = days.get(row["relpath"], set())
        if change in ("TRUNCATED", "REPLACED"):
            unstable |= mine | set(old.get("days", []) if old else [])
        prev[key] = {k: row[k] for k in ("size", "offset", "head_sha256", "window_sha256",
                                          "parser", "class") if k in row}
        prev[key]["days"] = sorted(mine | (set(old.get("days", [])) if old and change in (
            "UNCHANGED", "GROWN") else set()))
        prev[key]["last_change"] = change
    return dict(sorted(counts.items())), unstable


def nightly(state_dir: Path, ev: EvidenceSet, now: datetime, days: int = 14,
            min_runs: int = 3) -> dict[str, Any]:
    """One idempotent nightly pass. Returns a run summary."""
    state_dir.mkdir(parents=True, exist_ok=True)
    journal = state_dir / "run.journal"
    recovered = None
    if journal.exists():
        recovered = journal.read_text(encoding="utf-8").strip() or "unknown"
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    journal.write_text(run_id, encoding="utf-8")
    state = load_state(state_dir)
    changes, unstable = update_manifest(state, ev)
    today = now.astimezone(timezone.utc).date()
    span = [today - timedelta(days=i) for i in range(days, 0, -1)]
    if ev.window[0]:  # a day the evidence window opens inside is partial: never snapshot it
        opened = datetime.fromisoformat(ev.window[0]).astimezone(timezone.utc)
        span = [d for d in span if datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                >= opened]
    snaps = {k: v for k, v in daily_snapshots(ev, span).items() if v["requests"] > 0}
    added_snaps = 0
    for k, v in snaps.items():
        v["parser"] = PARSER_VERSION
        v["content_hash"] = _snapshot_hash(v)
        old = state["snapshots"].get(k)
        if old is None:
            added_snaps += 1
        # keep the fuller record: files deleted by cleanupPeriodDays never shrink a day
        if old is None or v["requests"] >= old.get("requests", 0):
            if k in unstable:
                v["unstable"] = run_id
            state["snapshots"][k] = v
        elif k in unstable:
            old["unstable"] = run_id
    for k in snaps:
        if k not in unstable:
            state["snapshots"][k].pop("unstable", None)
    new_lessons = 0
    deferred = sorted(d for d in snaps if d in unstable)
    for d in sorted(d for d in snaps if d not in unstable):
        for a in detect(state["snapshots"], d, min_runs):
            lid = "L-" + _hash([a["kind"], a["scope"], a["metric"], a["day"]])[:12]
            if lid in state["lessons"]:
                continue
            obs = (f"{a['scope']} median starting context {a['value'] / 1000:.1f}k on "
                   f"{a['day']} vs baseline {a['baseline'] / 1000:.1f}k")
            hs = context_gap(obs, [f"snapshot:{k}" for k in a["baseline_days"] + [a["day"]]],
                             _signals(state["snapshots"], a))
            state["hypotheses"][hs.id] = hs.to_dict()
            versions = sorted(set().union(*(set(state["snapshots"][k]["versions"])
                                            for k in a["baseline_days"] + [a["day"]])))
            lesson = {
                "id": lid, "version": 1, "state": NIGHTLY_MAX_STATE,
                "claim": f"Starting context of {a['scope']} changed by "
                         f"{a['delta'] / 1000:+.1f}k tokens",
                "scope": {"agent_type": a["scope"], "day": a["day"]},
                "trigger": {"kind": a["kind"], "scope": a["scope"], "metric": a["metric"],
                            "day": a["day"], "sign": 1 if a["delta"] > 0 else -1,
                            "min_runs": min_runs, "baseline_days": a["baseline_days"]},
                "scope_versions": versions,
                "configurations": {"versions": state["snapshots"][a["day"]]["versions"],
                                   "exposure": state["snapshots"][a["day"]]["exposure"]},
                "evidence_refs": hs.evidence_refs,
                "evidence_hashes": {k: state["snapshots"][k]["content_hash"]
                                    for k in a["baseline_days"] + [a["day"]]},
                "hypothesis_set": hs.id,
                "alternatives": [x.id for x in hs.explanations if x.id != hs.primary],
                "proposed_experiment": next((x.test for x in hs.explanations
                                             if x.id == hs.primary), hs.explanations[0].test),
                "expected_benefit": "UNKNOWN until tested",
                "failure_modes": ["the change is a workload mix effect, not a configuration "
                                  "effect", "too few runs on the day"],
                "rollback": ROLLBACK_NOTE,
                "verification_state": "NOT_TESTED",
                "author": "ari-nightly",
                "history": [{"state": "DISCOVERED", "run": run_id},
                            {"state": NIGHTLY_MAX_STATE, "run": run_id}],
            }
            lesson["content_hash"] = _content_hash(lesson)
            state["lessons"][lid] = lesson
            new_lessons += 1
    summary: dict[str, Any] = {
        "run": run_id, "snapshots_added": added_snaps, "lessons_added": new_lessons,
        "lessons_total": len(state["lessons"]), "recovered_from": recovered,
        "requests_seen": ev.coverage.requests, "source_changes": changes,
        "active_files_read": ev.coverage.files_active, "unstable_days": deferred,
        "parser": PARSER_VERSION}
    if not ev.coverage.requests:
        summary["warning"] = "no usage records in the window: missing data, not zero usage"
    elif deferred:
        summary["warning"] = (f"{len(deferred)} days were touched by a truncated or replaced "
                              "file; they were recomputed and no lesson was drawn from them")
    state["runs"].append(summary)
    save_state(state_dir, state)
    journal.unlink()
    return summary


def _snapshot_hash(snap: dict[str, Any]) -> str:
    """Hash of a day snapshot's content, recomputed on every use, never trusted from the
    stored content_hash field."""
    return _hash({k: v for k, v in snap.items() if k not in ("unstable", "content_hash")})


def _content_hash(lesson: dict[str, Any]) -> str:
    return _hash({k: v for k, v in lesson.items()
                  if k not in ("history", "content_hash", "replay", "state", "version")})


def replay(state_dir: Path, lesson_id: str) -> dict[str, Any]:
    """Re-run the detector on the stored snapshots with the current code (ARI2-R14, R15).

    Passes when the motivating day reproduces the trigger (same scope, same sign) and every
    baseline day stays quiet for that scope (counterexamples), and the stored snapshots
    still hash to what the lesson was built on. A failed replay quarantines the lesson."""
    state = load_state(state_dir)
    lesson = _get(state, lesson_id)
    trig = lesson.get("trigger")
    if not isinstance(trig, dict):
        raise PromotionError("lesson has no reproducible trigger")
    snaps = state["snapshots"]
    problems: list[str] = []
    for day, h in lesson.get("evidence_hashes", {}).items():
        if day not in snaps:
            problems.append(f"snapshot {day} is missing")
        elif _snapshot_hash(snaps[day]) != h:
            problems.append(f"snapshot {day} changed since the lesson was built")
    hit = [a for a in detect(snaps, trig["day"], trig["min_runs"])
           if a["scope"] == trig["scope"] and (1 if a["delta"] > 0 else -1) == trig["sign"]]
    if not hit:
        problems.append(f"trigger did not reproduce on {trig['day']}")
    counter = {}
    for day in trig["baseline_days"]:
        quiet = not any(a["scope"] == trig["scope"]
                        for a in detect(snaps, day, trig["min_runs"]))
        counter[day] = "quiet" if quiet else "fired"
        if not quiet:
            problems.append(f"counterexample {day} also fired: the trigger is not specific")
    seen = sorted(set().union(*(set(snaps[d]["versions"]) for d in [trig["day"]]
                                + trig["baseline_days"] if d in snaps)))
    uncovered = sorted(set(seen) - set(lesson.get("scope_versions", seen)))
    result = {"passed": not problems, "problems": problems, "counterexamples": counter,
              "code_sha256": code_sha256(), "parser": PARSER_VERSION,
              "versions_not_covered": uncovered}
    lesson["replay"] = result
    if problems and lesson["state"] not in TERMINAL:
        lesson["state"] = "QUARANTINED"
        lesson["history"].append({"state": "QUARANTINED", "actor": "replay",
                                  "reason": "; ".join(problems)})
    lesson["version"] += 1
    save_state(state_dir, state)
    return result


def _get(state: dict[str, Any], lesson_id: str) -> dict[str, Any]:
    lesson = state["lessons"].get(lesson_id)
    if not isinstance(lesson, dict):
        raise PromotionError(f"no lesson {lesson_id}")
    return lesson


def _binding_current(lesson: dict[str, Any]) -> str | None:
    rp = lesson.get("replay")
    if not isinstance(rp, dict) or not rp.get("passed"):
        return "needs a passed replay (ari_nightly.py replay)"
    if rp.get("code_sha256") != code_sha256() or rp.get("parser") != PARSER_VERSION:
        return "the ARI code or parser changed since the replay: replay again"
    return None


def promote(state_dir: Path, lesson_id: str, to: str, actor: str,
            owner_approved: bool = False, approval_file: Path | None = None
            ) -> dict[str, Any]:
    """Advance one lesson by one state, only when its promotion contract holds."""
    to = canonical(to)
    if not str(actor or "").strip():
        raise PromotionError("an actor must be named")
    if to == "REPLAY_TESTED":
        state = load_state(state_dir)
        if _get(state, lesson_id)["state"] == "CANDIDATE":
            replay(state_dir, lesson_id)  # the transition runs its own test
    state = load_state(state_dir)
    lesson = _get(state, lesson_id)
    cur = lesson["state"]
    if cur in TERMINAL:
        raise PromotionError(f"{lesson_id} is {cur}"
                             + (f": {lesson['replay']['problems']}" if lesson.get("replay")
                                and lesson["replay"].get("problems") else ""))
    if to not in LIFECYCLE or LIFECYCLE.index(to) != LIFECYCLE.index(cur) + 1:
        raise PromotionError(f"{cur} can only advance to the next state, not {to}")
    hs = from_dict(state["hypotheses"][lesson["hypothesis_set"]])
    if to == "CANDIDATE":
        if not lesson.get("proposed_experiment") or not lesson.get("alternatives"):
            raise PromotionError("CANDIDATE needs a proposed experiment and an alternative")
        if not lesson.get("rollback"):
            raise PromotionError("CANDIDATE needs rollback information")
    elif to == "REPLAY_TESTED":
        why = _binding_current(lesson)
        if why:
            raise PromotionError(f"REPLAY_TESTED {why}")
    elif to == "QUALIFIED":
        why = _binding_current(lesson)
        if why:
            raise PromotionError(f"QUALIFIED {why}")
        after = [r for r in hs.results if r.get("lesson_state") in LIFECYCLE[2:]]
        if not after:
            raise PromotionError("QUALIFIED needs a recorded experiment result after CANDIDATE")
        last = after[-1]
        if last["outcome"] != "supports":
            raise PromotionError("QUALIFIED needs a supporting result")
        if not _who(last.get("checker")) or _who(last["checker"]) == _who(last.get("author")):
            raise PromotionError("QUALIFIED needs a checker other than the author")
        if not any(x.state == "REJECTED" for x in hs.explanations if x.id != last["explanation"]):
            raise PromotionError("QUALIFIED needs at least one alternative rejected by a test")
    elif to == "APPROVAL_PENDING":
        why = _binding_current(lesson)
        if why:
            raise PromotionError(f"APPROVAL_PENDING {why}")
    elif to == "PROMOTED":
        why = _binding_current(lesson)
        if why:
            raise PromotionError(f"PROMOTED {why}")
        _check_approval(lesson, approval_file, owner_approved)
    lesson["state"] = to
    lesson["version"] += 1
    lesson["history"].append({"state": to, "actor": actor})
    if to != "PROMOTED":
        lesson["content_hash"] = _content_hash(lesson)
    save_state(state_dir, state)
    return lesson


def _check_approval(lesson: dict[str, Any], approval_file: Path | None,
                    owner_approved: bool) -> None:
    """An approval file binds the exact lesson content. It does not authenticate a person:
    no local mechanism can, so whoever controls the file system controls approval."""
    if approval_file is None:
        raise PromotionError("PROMOTED needs an approval file naming the lesson id and its "
                             "content hash" + (" (--owner-approved alone is not enough)"
                                               if owner_approved else ""))
    try:
        data = json.loads(Path(approval_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromotionError(f"approval file unreadable: {type(exc).__name__}") from None
    if not isinstance(data, dict) or data.get("schema") != APPROVAL_SCHEMA:
        raise PromotionError(f"approval file is not {APPROVAL_SCHEMA}")
    if data.get("lesson_id") != lesson["id"]:
        raise PromotionError("approval names another lesson")
    if data.get("content_hash") != _content_hash(lesson):  # recomputed, not the stored field
        raise PromotionError("approval is for other content: the lesson changed after approval")
    if not _who(data.get("approved_by")):
        raise PromotionError("approval file names no approver")


def _who(name: object) -> str:
    """Actor identity for the author and checker comparison: compatibility normalised, case
    insensitive, with every space and invisible format character removed."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(ch for ch in text if not ch.isspace()
                   and unicodedata.category(ch) != "Cf").casefold()


def add_result(state_dir: Path, lesson_id: str, explanation: str, outcome: str,
               evidence_file: Path, author: str, checker: str) -> dict[str, Any]:
    """Record one experiment result. When every explanation of the set has been refuted the
    lesson becomes REJECTED; it is kept with its history, never deleted."""
    state = load_state(state_dir)
    lesson = _get(state, lesson_id)
    if lesson["state"] in TERMINAL:
        raise PromotionError(f"{lesson_id} is {lesson['state']}")
    if not evidence_file.is_file():
        raise PromotionError("evidence file does not exist")
    if not _who(author) or not _who(checker):
        raise PromotionError("author and checker must both be named")
    digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    hs: HypothesisSet = from_dict(state["hypotheses"][lesson["hypothesis_set"]])
    record_result(hs, explanation, outcome, f"sha256:{digest}")
    hs.results[-1].update({"author": author, "checker": checker,
                           "lesson_state": lesson["state"]})
    state["hypotheses"][hs.id] = hs.to_dict()
    lesson["verification_state"] = f"{outcome.upper()} ({explanation})"
    if hs.state == "REJECTED":
        lesson["state"] = "QUARANTINED"
        lesson["history"].append({"state": "QUARANTINED", "actor": author,
                                  "reason": "every explanation was refuted"})
    lesson["version"] += 1
    lesson["content_hash"] = _content_hash(lesson)
    save_state(state_dir, state)
    return lesson

"""The governor (H0): one deterministic tick of the autonomous operator.

    lock -> journal -> integrity -> observe -> investigate (code) -> candidates -> verify
    -> model rounds (bounded, regulated) -> self regulation -> brief -> run record

Authority stays here. The model proposes, the governor validates, probes and judges.
Nothing in a tick commits, pushes, schedules, uninstalls, publishes, edits a setting, a
hook or a skill, or applies a candidate: every effect is a file in the state directory.

Safety properties, each tested:
- SAFE state: a protected file differs from its pin -> no model call, no transition.
- Idempotence: probe results are keyed by evidence digest; candidate ids hash kind and
  target; a second tick on the same data changes no evidence state.
- One tick at a time: an exclusive lock; a concurrent tick returns BUSY.
- Recovery: a journal marks a tick in progress; a later tick that finds it records the
  interruption. State files are replaced atomically, stage by stage, so a failure keeps
  what earlier stages wrote.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import (
    brief,
    candidates,
    competence,
    investigator,
    observer,
    policy,
    probes,
    regulator,
)
from .candidates import atomic_write
from .competence import redact
from .discovery import DiscoveryError
from .investigate import Investigation

SCHEMA = "ari-operator/1"
GOVERNOR = "governor"


@dataclass
class Config:
    state_dir: Path
    roots: list[str]
    skills_roots: list[str]
    mcp_configs: list[str]
    reference_files: list[str] = field(default_factory=list)
    days: float = 14
    now: str | None = None
    model: bool = True
    accept_coverage_gaps: bool = False
    repo: Path | None = None
    runner: investigator.Runner | None = None
    observe_fn: Callable[..., dict[str, Any]] | None = None


class Busy(Exception):
    pass


def _now(cfg: Config) -> datetime:
    if cfg.now:
        t = cfg.now[:-1] + "+00:00" if cfg.now.endswith("Z") else cfg.now
        dt = datetime.fromisoformat(t)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def load_gov(state_dir: Path) -> dict[str, Any]:
    p = state_dir / "state.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("schema") == SCHEMA:
            return d
        raise ValueError("state.json is not " + SCHEMA)
    return {"schema": SCHEMA, "mode": "NORMAL", "safe_reasons": [], "runs": [],
            "offered": [], "model_suspended": None, "incidents": []}


def load_invs(state_dir: Path) -> dict[str, Investigation]:
    p = state_dir / "investigations.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: Investigation.from_dict(v) for k, v in data.get("investigations", {}).items()}


def save_invs(state_dir: Path, invs: dict[str, Investigation]) -> None:
    atomic_write(state_dir / "investigations.json",
                 {"schema": "ari-investigations/1",
                  "investigations": {k: v.to_dict() for k, v in sorted(invs.items())}})


# candidates from settled investigations --------------------------------------------------
def _verify(spec: dict[str, Any], days: int) -> dict[str, Any]:
    return {"spec": spec, "min_days": days}


def _capability_candidate(inv: Investigation, ctx: probes.ProbeContext, cap: dict[str, Any],
                          now: datetime, digest: str) -> dict[str, Any] | None:
    final = inv.conclusion()["final"]
    x = inv.target
    s = {"skill": x}
    weekly = round(int(cap.get("exposed", 0)) * int(cap.get("listing_chars", 0)) / 4)
    ev = {"investigation": inv.root, "chain": inv.conclusion()["settled"], "digest": digest,
          "exposed_sessions": cap.get("exposed", 0), "listing_chars": cap.get("listing_chars")}
    if final == "dormant":
        c = candidates.make(
            "restrict_exposure", x,
            f"stop listing {x} in every session (make it explicit only or move it out of the "
            f"auto-listed skills); estimated listing cost {weekly} tokens over the window",
            _verify(probes.spec("skill_eligible_prompts", s, ("==", 0), (">=", 3)), 14),
            ev, float(weekly), now)
        c["benefit_unit"] = "listing_tokens"
    elif final == "redundant":
        closest = probes.run(ctx, probes.spec("skill_description_overlap", s, ("==", 0),
                                              ("==", 0)))[1].get("closest_skill", "")
        if not closest:
            return None
        c = candidates.make(
            "consolidate", x, f"fold {x} into {closest} (their descriptions overlap); keep "
            f"one listing", _verify(probes.spec("skill_raw_invocations", {"skill": closest},
                                                (">", 0), ("==", 0)), 14),
            ev | {"closest_skill": closest}, float(weekly), now)
        c["benefit_unit"] = "listing_tokens"
    elif final == "unrouted_demand":
        words = ctx.distinctive(x)
        asked = probes.run(ctx, probes.spec("skill_eligible_prompts", s, ("==", 0),
                                            (">=", 3)))[0]
        c = candidates.make(
            "routing_correction", x, f"rewrite the description of {x} so it triggers on how "
            f"users ask ({asked:g} prompts used at least 3 of its words: {', '.join(words)})",
            _verify(probes.spec("skill_raw_invocations", s, (">", 0), ("==", 0)), 7),
            ev, float(probes.jsonable(asked) or 0), now)
        c["benefit_unit"] = "matching_prompts"
    elif final == "telemetry":
        c = candidates.make(
            "telemetry_defect", x, f"the ARI parser misses invocations of {x}; inspect the "
            "raw lines and fix the parser", _verify(probes.spec(
                "skill_raw_minus_parsed", s, ("<=", 0), (">", 0)), 7),
            ev, float(cap.get("exposed", 0)), now)
        c["benefit_unit"] = "exposed_sessions"
    else:
        return None
    c["origin_digest"] = digest
    return c


def _competence_candidate(p: dict[str, Any], now: datetime, digest: str) -> dict[str, Any]:
    g = p["gate"]
    words = p["signature"][:6]
    need = max(1, math.ceil(len(words) / 2))
    what = {"competence_routing": f"route this work to {g['skill']} (its description covers it)",
            "competence_revision": f"revise {g['skill']}: it is used yet the same correction "
                                   "recurs",
            "competence_new_skill": "write a new skill for this recurring correction"}[g["kind"]]
    c = candidates.make(
        g["kind"], p["id"], f"{what}; correction words {', '.join(words)} in "
        f"{p['sessions']} sessions",
        _verify(probes.spec("prompt_term_count", {"terms": words, "min_match": need},
                            ("<=", 1), (">=", 3)), 14),
        {"pattern": p, "digest": digest}, float(p["sessions"]), now)
    c["benefit_unit"] = "correction_sessions"
    c["origin_digest"] = digest
    return c


def _policy_candidate(kind: str, acc: dict[str, int], now: datetime,
                      digest: str) -> dict[str, Any]:
    c = candidates.make(
        "policy_revision", kind, f"stop proposing {kind} until its rule is reviewed: "
        f"{acc['ROLLED_BACK']} of {acc['ROLLED_BACK'] + acc['VERIFIED']} outcomes rolled back",
        _verify({"probe": "policy_outcomes", "args": {"kind": kind},
                 "supports_if": {"op": "==", "value": 0},
                 "refutes_if": {"op": ">", "value": 0}}, 30),
        {"accuracy": acc, "digest": digest}, float(acc["ROLLED_BACK"]), now)
    c["benefit_unit"] = "rolled_back_outcomes"
    c["origin_digest"] = digest
    return c


def suppressed(cands: dict[str, dict[str, Any]]) -> set[str]:
    """R4 with consent: kinds the owner agreed to stop, through an approved revision."""
    return {c["target"] for c in cands.values() if c["kind"] == "policy_revision"
            and c["state"] in ("APPROVED", "CANARY", "VERIFIED")}


def advance_candidates(cands: dict[str, dict[str, Any]], invs: dict[str, Investigation],
                       ctx: probes.ProbeContext, caps: dict[str, dict[str, Any]],
                       pats: dict[str, dict[str, Any]], gaps: int, accept_gaps: bool,
                       now: datetime, digest: str) -> int:
    moved = 0
    for c in sorted(cands.values(), key=lambda c: c["id"]):
        st, kind = c["state"], c["kind"]
        later = c.get("origin_digest") != digest
        try:
            if st == "CANDIDATE" and kind.startswith("competence_"):
                p = pats.get(c["target"])
                if p and p["holdout_session_pass"]:
                    candidates.transition(c, "REPLAYED", GOVERNOR, "the newest session, held "
                                          "out of the signature, shows the same correction", now)
                    moved += 1
            elif st == "CANDIDATE" and later and kind == "policy_revision":
                still = c["target"] in regulator.poor_kinds(cands)
                candidates.transition(c, "REPLAYED" if still else "REJECTED", GOVERNOR,
                                      "poor record persists" if still else "record recovered",
                                      now)
                moved += 1
            elif st == "CANDIDATE" and later:
                inv = invs.get(c["target"])
                ok = inv is not None and inv.recheck(ctx)
                candidates.transition(c, "REPLAYED" if ok else "REJECTED", GOVERNOR,
                                      "conclusion reproduced on newer evidence" if ok
                                      else "conclusion did not reproduce on newer evidence", now)
                moved += 1
            if c["state"] == "REPLAYED":
                why = _quality(c, caps, pats, gaps, accept_gaps, digest)
                if why is None:
                    candidates.transition(c, "QUALIFIED", GOVERNOR, "quality gates passed", now)
                    candidates.transition(c, "APPROVAL_PENDING", GOVERNOR, "awaiting the owner",
                                          now)
                    moved += 2
                else:
                    c["blocked"] = why
        except candidates.CandidateError as exc:
            c["blocked"] = str(exc)
    return moved


def _quality(c: dict[str, Any], caps: dict[str, dict[str, Any]],
             pats: dict[str, dict[str, Any]], gaps: int, accept_gaps: bool,
             digest: str) -> str | None:
    if c["kind"].startswith("competence_"):
        if c.get("origin_digest") == digest:
            return "waiting for the pattern to recur on newer evidence"
        if c["target"] not in pats:
            return "the pattern did not recur on newer evidence"
        return None
    if c["kind"] == "policy_revision":
        return None
    if gaps and not accept_gaps:
        return f"{gaps} unread transcript files could hold uses (coverage gap)"
    if int(caps.get(c["target"], {}).get("exposed", 0)) < 20:
        return "exposed in fewer than 20 sessions"
    return None


# verification of applied candidates (ARI-09) ---------------------------------------------
def verify_canaries(cands: dict[str, dict[str, Any]], cfg: Config, now: datetime,
                    audit: dict[str, Any], incidents: list[str]) -> int:
    """Judge each due canary by its predeclared rule. Probes that read the audit (parsed
    counts, exposure) get the current audit; a candidate that cannot move is an incident."""
    moved = 0
    ctxs: dict[str, probes.ProbeContext] = {}
    for c in cands.values():
        if c["state"] != "CANARY" or not c.get("canary"):
            continue
        if not candidates.intact(c):
            incidents.append(f"{c['id']} changed after approval; it is not verified until "
                             "you reject it or restore it")
            continue
        start = datetime.fromisoformat(c["canary"]["started"])
        if now < start + timedelta(days=int(c["verify"]["min_days"])):
            continue
        spec = c["verify"]["spec"]
        if spec["probe"] == "policy_outcomes":
            value = float(sum(1 for o in cands.values() if o["kind"] == spec["args"]["kind"]
                              and o["state"] == "ROLLED_BACK"
                              and any(h["to"] == "ROLLED_BACK" and h["at"] >= start.isoformat()
                                      for h in o["history"])))
        else:
            gaps = int((audit.get("coverage") or {}).get("unread_gaps") or 0)
            if gaps > 0:
                incidents.append(f"{c['id']} not judged: {gaps} transcript gaps are unread, "
                                 "so the evidence is incomplete")
                continue
            key = start.isoformat()  # each canary is judged on its own window
            if key not in ctxs:
                ctxs[key] = probes.ProbeContext.build(cfg.roots, start, now, cfg.skills_roots,
                                                      cfg.reference_files, audit)
            if ctxs[key].lines_read == 0:
                incidents.append(f"{c['id']} not judged: no transcript evidence was read "
                                 "for its window")
                continue
            value = probes.run(ctxs[key], spec)[0]
        out = probes.judge(value, spec)
        c.setdefault("checks", []).append({"at": now.isoformat(),
                                           "value": probes.jsonable(value),
                                           "outcome": out})
        if out != "inconclusive":
            candidates.transition(c, "VERIFIED" if out == "supports" else "ROLLED_BACK",
                                  GOVERNOR, f"{spec['probe']}={value} since {start.date()}", now)
            moved += 1
    return moved


# model rounds ------------------------------------------------------------------------------
def _question_view(inv: Investigation, qid: str) -> dict[str, Any]:
    q = inv.ledger.questions[qid]
    hyps = []
    for hid in q.hypotheses:
        h = inv.ledger.hypotheses[hid]
        hyps.append({"id": hid, "mechanism": h.mechanism, "status": h.status,
                     "probe": inv.specs.get(hid),
                     "results": [{"outcome": r["outcome"], "note": r["note"]}
                                 for r in h.results[-3:]],
                     "unresolved_reason": h.unresolved_reason})
    parent = inv.ledger.questions[q.parent].observation if q.parent else None
    return {"question": qid, "target": inv.target, "observation": q.observation,
            "depth": q.depth, "parent_observation": parent, "hypotheses": hyps}


def _q_statuses(inv: Investigation, qid: str) -> list[str]:
    q = inv.ledger.questions[qid]
    return [q.status] + [inv.ledger.hypotheses[h].status for h in q.hypotheses]


def model_rounds(cfg: Config, gov: dict[str, Any], invs: dict[str, Investigation],
                 ctx: probes.ProbeContext, digest: str, now: datetime,
                 evidence_dir: Path, order: list[str]) -> dict[str, Any]:
    rounds, cost, changes_total, note = 0, 0.0, 0, "no model call"
    offered_order = [str(x) for x in gov.get("offered", [])]  # oldest first
    offered = set(offered_order)
    while rounds < policy.BOUNDS["max_model_rounds"]:
        ok, reason = regulator.allow_model(cfg.state_dir, gov, now, cost)
        if not ok:
            note = reason
            break
        picks: list[tuple[Investigation, str]] = []
        for name in order:
            inv = invs.get(name)
            if inv is None:
                continue
            for qid in inv.open_questions():
                if f"{qid}@{digest}" not in offered:
                    picks.append((inv, qid))
            if len(picks) >= policy.BOUNDS["max_model_questions"]:
                break
        picks = picks[: int(policy.BOUNDS["max_model_questions"])]
        if not picks:
            note = "nothing new for the model at this evidence digest"
            break
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{rounds}-{secrets.token_hex(3)}"
        before = {qid: _q_statuses(inv, qid) for inv, qid in picks}
        rolling_left = policy.BOUNDS["rolling_budget_usd"] - regulator.spent(
            cfg.state_dir, now, policy.BOUNDS["rolling_days"])
        budget = math.floor(min(policy.BOUNDS["tick_budget_usd"] - cost, rolling_left)
                            * 100 + 1e-9) / 100  # floor to cents: never above the remainder
        if budget < 0.05:
            note = f"remaining budget {budget:.2f} USD is below one call"
            break
        for _inv, qid in picks:  # asked once per digest, whatever the outcome
            if f"{qid}@{digest}" not in offered:
                offered.add(f"{qid}@{digest}")
                offered_order.append(f"{qid}@{digest}")
        gov["offered"] = offered_order[-5000:]
        atomic_write(cfg.state_dir / "state.json", gov)  # a call killed midway is not repeated
        regulator.record(cfg.state_dir, {"at": now.isoformat(), "event": "call_started",
                                         "run": run_id, "ok": None, "cost_usd": budget})
        res = investigator.investigate(
            investigator.packet([_question_view(inv, qid) for inv, qid in picks]), budget,
            policy.BOUNDS["model_timeout_s"], cfg.state_dir / "model-cwd", cfg.runner)
        applied, refused, changes = 0, [], 0
        try:  # whatever happens below, the call and its cost are recorded
            by_q = {qid: inv for inv, qid in picks}
            for p in res.proposals[: int(policy.BOUNDS["max_proposals"])]:
                inv = by_q.get(str(p.get("question")))
                if inv is None:
                    refused.append("question was not offered")
                    continue
                try:
                    inv.apply(p, run_id)
                    applied += 1
                except DiscoveryError as exc:
                    refused.append(str(exc)[:120])
            invs_now = list({id(i): i for i, _q in picks}.values())
            probes.prefetch(ctx, (s for i in invs_now for s in i.specs.values()))
            for inv in invs_now:
                changes += inv.probe(ctx, digest, evidence_dir)
            for inv, qid in picks:  # a failed call taught nothing but also asked nothing
                if res.ok:
                    moved = _q_statuses(inv, qid) != before[qid]
                    regulator.note_round(inv.rounds, inv.nonprogress, qid, int(moved))
        finally:
            regulator.record(cfg.state_dir, {
                "at": now.isoformat(), "run": run_id, "ok": res.ok, "error": res.error,
                "cost_usd": res.cost_usd, "input_tokens": res.input_tokens,
                "output_tokens": res.output_tokens, "duration_s": round(res.duration_s, 2),
                "questions": [q for _i, q in picks], "proposals": len(res.proposals),
                "applied": applied, "refused": refused, "status_changes": changes})
            gov["offered"] = offered_order[-5000:]
        rounds += 1
        cost = round(cost + res.cost_usd, 6)
        changes_total += changes
        note = (f"last round: {applied} proposals applied, {len(refused)} refused"
                if res.ok else f"last round failed: {res.error[:100]}")
        if regulator.consecutive_failures(cfg.state_dir) >= policy.BOUNDS["max_model_failures"]:
            gov["model_suspended"] = (f"{policy.BOUNDS['max_model_failures']:g} consecutive "
                                      "failed model calls; resume with ari_operator.py resume")
            break
        if not res.ok:
            break
    gov["offered"] = offered_order[-5000:]
    return {"rounds": rounds, "cost_usd": cost, "note": note, "changes": changes_total}


# the tick ----------------------------------------------------------------------------------
def tick(cfg: Config) -> dict[str, Any]:
    sd = cfg.state_dir
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    try:
        lock = open(sd / ".lock", "w")
    except OSError:  # a folder or an unwritable file where the lock belongs: set it aside
        os.replace(sd / ".lock", sd / f".lock.unusable-{secrets.token_hex(4)}")
        lock = open(sd / ".lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "BUSY", "reason": "another tick holds the lock"}
        try:
            return _tick(cfg)
        except Exception as exc:  # noqa: BLE001  the last resort: a crash leaves a record
            err = redact(f"{type(exc).__name__}: {str(exc)[:600]}")[:300]
            try:
                with open(sd / CRASHES, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                         "error": err}) + "\n")
            except OSError:
                pass
            return {"status": "FAILED", "error": err, "incidents": [f"tick crashed: {err}"]}
    finally:
        lock.close()


CRASHES = "tick_crashes.jsonl"
FOLDERS = ("evidence", "model-cwd", "observe", "nightly")


def _tick(cfg: Config) -> dict[str, Any]:
    sd, now = cfg.state_dir, _now(cfg)
    t: dict[str, Any] = {"id": now.strftime("%Y%m%dT%H%M%SZ"), "at": now.isoformat(),
                         "status": "OK"}
    unreadable = ""
    try:
        gov = load_gov(sd)
    except (ValueError, OSError, RecursionError) as exc:  # kept aside, never silently lost
        aside = sd / f"state.json.unreadable-{t['id']}"
        os.replace(sd / "state.json", aside)
        unreadable = f"state.json was unreadable ({type(exc).__name__}); kept as {aside.name}"
        gov = load_gov(sd)
        gov["model_suspended"] = "the run history was lost with an unreadable state.json; " \
                                 "resume with ari_operator.py resume after a look"
    journal = sd / ".journal"
    gov["incidents"] = [unreadable] if unreadable else []
    for key in ("runs", "offered"):  # a valid schema with a wrongly typed field
        if not isinstance(gov.get(key), list):
            gov[key] = []
            gov["incidents"].append(f"run history field {key} was not a list; reset")
    if journal.exists() or journal.is_symlink():
        try:
            t["recovered_from"] = journal.read_text(encoding="utf-8")[:200]
            gov["incidents"].append(f"recovered from an interrupted tick "
                                    f"({t['recovered_from']})")
        except (OSError, ValueError):  # a folder, unreadable or not text: set it aside
            aside = sd / f".journal.unusable-{t['id']}"
            os.replace(journal, aside)
            gov["incidents"].append(f"the tick journal was unusable; kept as {aside.name}")
    journal.write_text(t["id"], encoding="utf-8")
    try:
        problems = policy.integrity(cfg.repo)
        if problems:
            gov["mode"], gov["safe_reasons"] = "SAFE", problems
            t["status"] = "SAFE"
            try:
                shown = candidates.load(sd)
            except Exception:  # noqa: BLE001  the SAFE brief never depends on the store
                shown = {}
            return _finish(sd, gov, t, shown, {}, journal, now)
        if gov.get("mode") == "SAFE":
            gov["incidents"].append("left the SAFE state: protected files match their pin")
        gov["mode"], gov["safe_reasons"] = "NORMAL", []
        _heal_stores(sd, t["id"], gov["incidents"])
        _work(cfg, gov, t, now)
    except Exception as exc:  # noqa: BLE001  a failed stage is recorded, never silent
        t["status"] = "FAILED"
        t["error"] = redact(f"{type(exc).__name__}: {str(exc)[:600]}")[:200]
        gov["incidents"].append("tick failed: " + t["error"])
    try:
        cands = candidates.load(sd)
    except (candidates.CandidateError, ValueError, RecursionError):
        cands = {}
    return _finish(sd, gov, t, cands, t.pop("_findings", {}), journal, now)


def _heal_stores(sd: Path, tick_id: str, incidents: list[str]) -> None:
    """An unreadable store is kept aside for the owner and the tick goes on without it;
    a malformed candidate is moved to candidates.quarantine.json. Nothing is deleted."""
    for name in FOLDERS:  # a plain file where a working folder belongs is set aside
        p = sd / name
        if (p.exists() or p.is_symlink()) and not p.is_dir():
            aside = sd / f"{name}.not-a-folder-{tick_id}"
            os.replace(p, aside)
            incidents.append(f"{name} was not a folder; kept as {aside.name}")
    for name, loader in (("candidates.json", candidates.load), ("investigations.json",
                                                                 _raw_invs)):
        try:
            loader(sd)
        except Exception as exc:  # noqa: BLE001  any unreadable store is set aside
            aside = sd / f"{name}.unreadable-{tick_id}"
            os.replace(sd / name, aside)
            incidents.append(f"{name} was unreadable ({type(exc).__name__}); kept as "
                             f"{aside.name}")
    _heal_invs(sd, tick_id, incidents)
    cands = candidates.load(sd)
    bad = {k: v for k, v in cands.items() if candidates.problem(v) or k != v.get("id")}
    if not bad:
        return
    qp = sd / "candidates.quarantine.json"
    held = _held(qp)
    for k, v in bad.items():
        try:  # a value the strict writer refuses (NaN) is kept as text
            json.dumps(v, allow_nan=False, default=str)
            kept: Any = v
        except ValueError:
            kept = repr(v)[:20000]
        held[f"{k}@{tick_id}"] = {"reason": candidates.problem(v) or "id does not match its key",
                                  "candidate": kept}
        incidents.append(f"candidate {k} is malformed ({held[f'{k}@{tick_id}']['reason']}); "
                         "moved to candidates.quarantine.json")
        del cands[k]
    atomic_write(qp, held)
    candidates.save(sd, cands)


def _held(qp: Path) -> dict[str, Any]:
    try:
        held = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else {}
    except (ValueError, RecursionError, OSError):
        held = {}
    return held if isinstance(held, dict) else {}


def _raw_invs(sd: Path) -> dict[str, Any]:
    """The stored investigations as plain data; a document that is not even that raises."""
    p = sd / "investigations.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and isinstance(data.get("investigations"), dict)):
        raise ValueError("investigations.json has no investigations object")
    return dict(data["investigations"])


def _heal_invs(sd: Path, tick_id: str, incidents: list[str]) -> None:
    """An investigation that does not load is moved to investigations.quarantine.json."""
    raw = _raw_invs(sd)
    bad: dict[str, str] = {}
    for k, v in raw.items():
        try:
            Investigation.from_dict(v)
        except Exception as exc:  # noqa: BLE001  any unusable investigation is set aside
            bad[k] = redact(str(exc))[:160] or type(exc).__name__
    if not bad:
        return
    qp = sd / "investigations.quarantine.json"
    held = _held(qp)
    for k, why in bad.items():
        held[f"{k}@{tick_id}"] = {"reason": why, "investigation": raw.pop(k)}
        incidents.append(f"investigation {k} is malformed ({why}); moved to "
                         "investigations.quarantine.json")
    atomic_write(qp, held)
    atomic_write(sd / "investigations.json", {"schema": "ari-investigations/1",
                                              "investigations": raw})


def _work(cfg: Config, gov: dict[str, Any], t: dict[str, Any], now: datetime) -> None:
    sd = cfg.state_dir
    since = now - timedelta(days=cfg.days)
    obs_fn = cfg.observe_fn or observer.observe
    obs = obs_fn(sd / "observe" / t["id"], cfg.roots, cfg.days, now.isoformat(),
                 cfg.skills_roots, cfg.mcp_configs, sd / "nightly")
    for name, r in obs["tools"].items():
        if not r["ok"]:
            gov["incidents"].append(f"{name} tool failed: {r['error'][:160]}")
    digest = obs["digest"]
    t["changed"] = digest != gov.get("last_digest")
    t["digest"] = digest[:16]
    burn = obs["signals"].get("burn")
    if burn and burn.get("observed") is not None:
        t["burn_line"] = (f"tokens this period {burn['observed']:,}; forecast "
                          f"{burn.get('forecast_status')} {burn.get('point') or ''}")
    audit = obs.get("audit")
    if not (obs["tools"].get("audit", {}).get("ok") and isinstance(audit, dict)):
        # parsed counts and exposure are unknown: judging now would read them as zeros
        gov["incidents"].append("no usable audit this tick: probes, candidate moves, canary "
                                "checks and model rounds wait for the next tick")
        t.update(opened=0, probed=0, status_changes=0, model_rounds=0, model_cost_usd=0.0,
                 model_note="skipped: no audit")
        t["_findings"] = _findings(load_invs(sd))
        return
    ctx = probes.ProbeContext.build(cfg.roots, since, now, cfg.skills_roots,
                                    cfg.reference_files, audit)
    invs = load_invs(sd)
    tg = observer.targets(audit)
    caps = {x["name"]: x for x in tg}
    t["targets_total"] = len(tg)
    new = [x for x in tg if x["name"] not in invs][: int(policy.BOUNDS["max_targets_per_tick"])]
    for x in new:
        invs[x["name"]] = Investigation.open_skill(x["name"], x["exposed"])
    t["opened"] = len(new)
    ev_dir = sd / "evidence"
    changes = probed = 0
    for inv in invs.values():
        if inv.open_questions():
            changes += inv.probe(ctx, digest, ev_dir)
            probed += 1
    t["probed"] = probed
    save_invs(sd, invs)

    cands = candidates.load(sd)
    stop = suppressed(cands)
    for inv in invs.values():
        cid_kinds = {c["target"] for c in cands.values()}
        if inv.target in cid_kinds or inv.target not in caps:
            continue
        c = _capability_candidate(inv, ctx, caps[inv.target], now, digest)
        if c and c["kind"] not in stop and c["id"] not in cands:
            cands[c["id"]] = c
    pats = {p["id"]: p for p in competence.patterns(ctx)}
    t["correction_patterns"] = len(pats)
    for p in pats.values():
        c = _competence_candidate(p, now, digest)
        if c["kind"] not in stop and c["id"] not in cands:
            cands[c["id"]] = c
    for kind in regulator.poor_kinds(cands):
        acc = regulator.accuracy(cands)[kind]
        c = _policy_candidate(kind, acc, now, digest)
        if c["id"] not in cands:
            cands[c["id"]] = c
    gaps = int((audit.get("coverage") or {}).get("unread_gaps") or 0)
    t["candidate_moves"] = advance_candidates(cands, invs, ctx, caps, pats, gaps,
                                              cfg.accept_coverage_gaps, now, digest)
    t["verified"] = verify_canaries(cands, cfg, now, audit, gov["incidents"])
    candidates.save(sd, cands)

    order = [x["name"] for x in tg] + sorted(n for n in invs if n not in caps)
    if cfg.model:
        m = model_rounds(cfg, gov, invs, ctx, digest, now, ev_dir, order)
        changes += m["changes"]
        t.update(model_rounds=m["rounds"], model_cost_usd=m["cost_usd"], model_note=m["note"])
        save_invs(sd, invs)
    else:
        t.update(model_rounds=0, model_cost_usd=0.0, model_note="model layer off")
    t["status_changes"] = changes
    gov["last_digest"] = digest
    t["_findings"] = _findings(invs)
    t["_interpretations"] = [i["text"] for inv in invs.values() for i in inv.interpretations]


def _findings(invs: dict[str, Investigation]) -> dict[str, int]:
    out: dict[str, int] = {}
    for inv in invs.values():
        f = inv.conclusion()["final"] or "open"
        out[f] = out.get(f, 0) + 1
    return out


def _finish(sd: Path, gov: dict[str, Any], t: dict[str, Any],
            cands: dict[str, dict[str, Any]], findings: dict[str, int], journal: Path,
            now: datetime) -> dict[str, Any]:
    interps = t.pop("_interpretations", [])
    cands = {k: v for k, v in cands.items() if not candidates.problem(v)}  # SAFE path too
    report = regulator.self_report(sd, now)
    if report.get("ledger_error"):
        gov["incidents"].append(str(report["ledger_error"]))
    text, doc = brief.render(gov, t, cands, findings, interps, report)
    try:
        (sd / "brief.md").write_text(text, encoding="utf-8")
        atomic_write(sd / "brief.json", doc)
    except (OSError, ValueError) as exc:  # the run record below is still written
        gov["incidents"].append(f"brief not written ({type(exc).__name__})")
    gov["runs"] = (gov.get("runs", []) + [{k: v for k, v in t.items()
                                           if not k.startswith("_")}])[-200:]
    atomic_write(sd / "state.json", gov)
    journal.unlink(missing_ok=True)
    t["incidents"] = gov["incidents"]
    return t

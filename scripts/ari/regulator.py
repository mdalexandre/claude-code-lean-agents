"""Self observation and self regulation (H4, R5): ARI measures and limits ARI.

Every model call is a row in model_runs.jsonl: cost, tokens, duration, the questions it
worked on and the information it produced (hypothesis and question status changes). The
regulator decides from those rows alone whether the model may run:

    refused when the operator is in the SAFE state or the model layer is suspended,
    when the last N calls failed, when the rolling 7 day spend reached its cap, or when
    this tick's spend reached the tick cap.

A question that received model rounds without any status change is marked nonprogress
and is not offered again: an investigation that costs more than it teaches stops.
Outcome accuracy per candidate kind (VERIFIED against ROLLED_BACK) is ARI's record of
its own advice; a kind with a poor record yields a policy revision proposal, never a
silent change of behaviour.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .policy import BOUNDS

LEDGER = "model_runs.jsonl"
MAX_COUNT = 10 ** 9


def record(state_dir: Path, row: dict[str, Any]) -> None:
    with open(state_dir / LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _money(v: Any) -> float:
    """A cost that is not a finite, non negative number counts as a whole tick budget:
    an unreadable cost must never read as free."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return float(BOUNDS["tick_budget_usd"])
    return f if math.isfinite(f) and f >= 0 else float(BOUNDS["tick_budget_usd"])


def _count(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(n, 0), MAX_COUNT)


def rows(state_dir: Path) -> list[dict[str, Any]]:
    """Ledger rows with their numbers normalised; unreadable lines are skipped."""
    p = state_dir / LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(r, dict):
            continue
        r["cost_usd"] = 0.0 if r.get("event") and "cost_usd" not in r else _money(
            r.get("cost_usd", 0))
        for k in ("status_changes", "input_tokens", "output_tokens"):
            r[k] = _count(r.get(k, 0))
        out.append(r)
    return out


def _when(r: dict[str, Any]) -> datetime | None:
    """The row's time, or None when it is missing, unreadable or has no time zone."""
    try:
        t = datetime.fromisoformat(str(r.get("at")))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


STARTED = "call_started"


def _completed(rs: list[dict[str, Any]]) -> set[str]:
    return {str(r["run"]) for r in rs if not r.get("event") and r.get("run") is not None}


def spent(state_dir: Path, now: datetime, days: float) -> float:
    """Spend in the window; a row without a readable time counts as inside it. A call
    that started and never wrote its result (the process was killed) counts at its cap."""
    since = now - timedelta(days=days)
    total = 0.0
    rs = rows(state_dir)
    done = _completed(rs)
    for r in rs:
        if r.get("event") == STARTED and str(r.get("run")) in done:
            continue  # its result row carries the real cost
        t = _when(r)
        if t is None or t >= since:
            total += r["cost_usd"]
    return round(total, 6)


def consecutive_failures(state_dir: Path) -> int:
    n = 0
    rs = rows(state_dir)
    done = _completed(rs)
    for r in reversed(rs):
        if r.get("event") == STARTED and str(r.get("run")) in done:
            continue  # counted once, by its result row; an unfinished call is a failure
        if r.get("ok") or r.get("event") == "resume":
            break  # a success or an owner resume starts the count again
        n += 1
    return n


def resume(state_dir: Path, gov: dict[str, Any], now: datetime) -> None:
    """Owner resume: lift the suspension and restart the failure count (a ledger row)."""
    gov["model_suspended"] = None
    record(state_dir, {"at": now.isoformat(), "event": "resume", "ok": None, "cost_usd": 0.0})


def allow_model(state_dir: Path, gov: dict[str, Any], now: datetime,
                tick_spent: float) -> tuple[bool, str]:
    if gov.get("mode") == "SAFE":
        return False, "operator is in the SAFE state"
    if gov.get("model_suspended"):
        return False, "model layer suspended: " + str(gov["model_suspended"])
    try:
        fails = consecutive_failures(state_dir)
        rolling = spent(state_dir, now, BOUNDS["rolling_days"])
    except OSError as exc:  # spend unknown: no call
        return False, f"model ledger unreadable ({type(exc).__name__})"
    if fails >= BOUNDS["max_model_failures"]:
        return False, f"{fails} consecutive model failures"
    if rolling >= BOUNDS["rolling_budget_usd"]:
        return False, f"rolling {BOUNDS['rolling_days']:g} day spend {rolling:.2f} USD " \
                      f"reached the cap {BOUNDS['rolling_budget_usd']:.2f}"
    if tick_spent >= BOUNDS["tick_budget_usd"]:
        return False, f"tick spend reached {BOUNDS['tick_budget_usd']:.2f} USD"
    return True, "allowed"


def note_round(rounds: dict[str, int], nonprogress: list[str], qid: str,
               changed: int) -> bool:
    """Count a model round on a question; True when it is now closed as nonprogress."""
    if changed:
        rounds[qid] = 0
        return False
    rounds[qid] = rounds.get(qid, 0) + 1
    if rounds[qid] >= BOUNDS["nonprogress_rounds"] and qid not in nonprogress:
        nonprogress.append(qid)
        return True
    return False


def accuracy(candidates: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for c in candidates.values():
        if c.get("state") in ("VERIFIED", "ROLLED_BACK"):
            k = out.setdefault(c["kind"], {"VERIFIED": 0, "ROLLED_BACK": 0})
            k[c["state"]] += 1
    return out


def poor_kinds(candidates: dict[str, dict[str, Any]], min_outcomes: int = 3) -> list[str]:
    return sorted(k for k, v in accuracy(candidates).items()
                  if v["VERIFIED"] + v["ROLLED_BACK"] >= min_outcomes
                  and v["VERIFIED"] < v["ROLLED_BACK"])


def self_report(state_dir: Path, now: datetime) -> dict[str, Any]:
    try:
        rs = rows(state_dir)
    except OSError as exc:
        return {"model_runs_7d": 0, "model_cost_usd_7d": 0.0, "status_changes_7d": 0,
                "ledger_error": f"model ledger unreadable ({type(exc).__name__})"}
    week = now - timedelta(days=BOUNDS["rolling_days"])
    recent = [r for r in rs if (_when(r) or now) >= week and not r.get("event")]
    gain = sum(r["status_changes"] for r in recent)
    cost = sum(r["cost_usd"] for r in recent)
    return {"model_runs_7d": len(recent), "model_cost_usd_7d": round(cost, 4),
            "model_failures_7d": sum(1 for r in recent if not r.get("ok")),
            "status_changes_7d": gain,
            "status_changes_per_usd": round(gain / cost, 2) if cost else None,
            "input_tokens_7d": sum(r["input_tokens"] for r in recent),
            "output_tokens_7d": sum(r["output_tokens"] for r in recent)}

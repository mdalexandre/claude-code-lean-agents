#!/usr/bin/env python3
"""ARI 3.0 operator: the autonomous, recursive layer over the ARI 2.0 tools.

    tick       one governed run: observe, investigate, regulate, write the brief
    brief      print the latest brief
    status     counts of investigations, candidates and model spend
    approve    owner: approve an APPROVAL_PENDING candidate (needs a terminal)
    applied    owner: record that you applied an approved candidate (needs a terminal)
    reject     reject an open candidate with a reason
    resume     owner: resume a suspended model layer (needs a terminal)
    pin        owner: re-pin the protected files after a reviewed change (needs a terminal)
    hook       SessionStart hook body: show today's brief once

Nothing here is scheduled by itself; deploy/install_ari_operator.sh installs the timer and
the hook only when you run it. State lives in --state-dir (default ~/.ari/operator).
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ari import candidates, policy, regulator
from ari.cli_common import DEFAULT_ROOT, bounded_float, time_arg
from ari.governor import Config, load_gov, load_invs, tick

DEFAULT_STATE = os.path.join("~", ".ari", "operator")


def _refs() -> list[str]:
    home = os.path.expanduser("~")
    files = [os.path.join(home, ".claude", "CLAUDE.md")]
    files += sorted(glob.glob(os.path.join(home, ".claude", "agents", "*.md")))
    return [f for f in files if os.path.isfile(f)]


def _cands(sd: Path, cid: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cands = candidates.load(sd)
    if cid not in cands:
        raise candidates.CandidateError(f"no candidate {cid}")
    return cands, cands[cid]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--state-dir", default=DEFAULT_STATE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    tk = sub.add_parser("tick")
    tk.add_argument("--root", action="append", default=None)
    tk.add_argument("--skills-root", action="append", default=None)
    tk.add_argument("--mcp-config", action="append", default=None)
    tk.add_argument("--days", type=bounded_float(1, 90), default=14)
    tk.add_argument("--now", type=time_arg, default=None)
    tk.add_argument("--no-model", action="store_true", help="code only; no model call")
    tk.add_argument("--accept-coverage-gaps", action="store_true")
    sub.add_parser("brief")
    sub.add_parser("status")
    for name in ("approve", "applied", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        if name == "reject":
            p.add_argument("--reason", required=True)
    sub.add_parser("resume")
    sub.add_parser("pin")
    sub.add_parser("hook")
    args = ap.parse_args(argv)
    sd = Path(args.state_dir).expanduser()
    now = datetime.now(timezone.utc)
    if args.cmd in ("approve", "applied", "reject", "resume"):
        sd.mkdir(parents=True, exist_ok=True)
        with open(sd / ".lock", "w") as lock:
            try:  # never interleave with a running tick: one of the two would lose its write
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("refused: a tick is running; try again when it ends", file=sys.stderr)
                return 75
            return _run(args, sd, now)
    return _run(args, sd, now)


def _run(args: argparse.Namespace, sd: Path, now: datetime) -> int:
    try:
        if args.cmd in ("approve", "applied", "resume"):
            problems = policy.integrity()
            if problems:  # an owner decision is never taken on code that differs from its pin
                raise candidates.CandidateError("protected files differ from their pin: "
                                                + "; ".join(problems[:3]))
        if args.cmd == "tick":
            cfg = Config(state_dir=sd,
                         roots=args.root or [os.path.expanduser(DEFAULT_ROOT)],
                         skills_roots=args.skills_root or [os.path.expanduser("~/.claude/skills")],
                         mcp_configs=args.mcp_config or [os.path.expanduser("~/.claude.json")],
                         reference_files=_refs(), days=args.days, now=args.now,
                         model=not args.no_model,
                         accept_coverage_gaps=args.accept_coverage_gaps)
            out = tick(cfg)
            print(json.dumps(out, indent=1, default=str))
            return {"OK": 0, "BUSY": 75, "SAFE": 3}.get(out.get("status", ""), 1)
        if args.cmd == "brief":
            bp = sd / "brief.md"
            print(bp.read_text(encoding="utf-8") if bp.exists() else "no brief yet")
            return 0
        if args.cmd == "status":
            gov = load_gov(sd)
            cands = candidates.load(sd)
            states: dict[str, int] = {}
            for c in cands.values():
                states[c["state"]] = states.get(c["state"], 0) + 1
            print(json.dumps({"mode": gov.get("mode"), "safe_reasons": gov.get("safe_reasons"),
                              "model_suspended": gov.get("model_suspended"),
                              "ticks": len(gov.get("runs", [])),
                              "investigations": len(load_invs(sd)), "candidates": states,
                              "pending": [{k: c.get(k) for k in ("id", "kind", "target",
                                                                 "benefit", "benefit_unit")}
                                          for c in sorted(cands.values(),
                                                          key=lambda c: str(c.get("id")))
                                          if c.get("state") == "APPROVAL_PENDING"],
                              "approved_waiting": sorted(str(c.get("id")) for c in
                                                         cands.values()
                                                         if c.get("state") == "APPROVED"),
                              "self": regulator.self_report(sd, now),
                              "integrity": policy.integrity() or "ok"}, indent=1))
            return 0
        if args.cmd in ("approve", "applied"):
            cands, c = _cands(sd, args.id)
            to = "APPROVED" if args.cmd == "approve" else "CANARY"
            candidates.owner_approve(c, to, now, candidates.real_terminal)
            candidates.save(sd, cands)
            print(f"{c['id']} is {c['state']}")
            return 0
        if args.cmd == "reject":
            cands, c = _cands(sd, args.id)
            candidates.transition(c, "REJECTED", "owner", args.reason[:300], now)
            candidates.save(sd, cands)
            print(f"{c['id']} is REJECTED")
            return 0
        if args.cmd in ("resume", "pin"):
            what = ("resume the model layer" if args.cmd == "resume"
                    else "re-pin the protected files as they are now")
            import secrets
            if not candidates.real_terminal(f"Confirm: {what}", secrets.token_hex(3)):
                raise candidates.CandidateError("not confirmed at a terminal")
            if args.cmd == "resume":
                gov = load_gov(sd)
                regulator.resume(sd, gov, now)
                candidates.atomic_write(sd / "state.json", gov)
            else:
                print(policy.write_pin())
            print("done")
            return 0
        return hook(sd, now)
    except (candidates.CandidateError, ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


def hook(sd: Path, now: datetime) -> int:
    """SessionStart: show the brief once per day, as a message to the person."""
    p, mark = sd / "brief.md", sd / ".brief_shown"
    if not p.exists():
        return 0
    today = now.date().isoformat()
    try:
        if mark.read_text(encoding="utf-8").strip() == today:
            return 0
    except OSError:
        pass
    text = p.read_text(encoding="utf-8")[:4000]
    mark.write_text(today, encoding="utf-8")
    print(json.dumps({"systemMessage": text,
                      "hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "The ARI brief for today "
                                             "was shown to the owner; read "
                                             f"{p} if they ask about it."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

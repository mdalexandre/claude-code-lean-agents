#!/usr/bin/env python3
"""Opt in nightly analysis: snapshots, drift detection and candidate lessons.

The nightly run only creates DISCOVERED or SPECIFIED lessons. Advancing a lesson needs
promote (with its contract) and recorded experiment results. Nothing is scheduled by this
script; see docs/ari/scheduling.md for examples you can install yourself.

Usage:
    python3 ari_nightly.py run --state-dir DIR [--days 14]
    python3 ari_nightly.py status --state-dir DIR
    python3 ari_nightly.py record-result --state-dir DIR --id L-... --explanation mcp
        --outcome supports|refutes|inconclusive --evidence FILE --author A --checker C
    python3 ari_nightly.py replay --state-dir DIR --id L-...
    python3 ari_nightly.py promote --state-dir DIR --id L-... --to CANDIDATE --actor A
        [--approval FILE]   (PROMOTED needs an ari-lesson-approval/1 file)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from ari.cli_common import add_common, resolve
from ari.evidence import ingest
from ari.learn import (
    ALIASES,
    LIFECYCLE,
    PromotionError,
    add_result,
    load_state,
    nightly,
    promote,
    replay,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    add_common(run, days=14)
    run.add_argument("--state-dir", required=True)
    run.add_argument("--quiet-seconds", type=float, default=1800,
                     help="skip files written in the last N seconds (still active)")
    st = sub.add_parser("status")
    st.add_argument("--state-dir", required=True)
    rr = sub.add_parser("record-result")
    rr.add_argument("--state-dir", required=True)
    rr.add_argument("--id", required=True)
    rr.add_argument("--explanation", required=True)
    rr.add_argument("--outcome", choices=("supports", "refutes", "inconclusive"), required=True)
    rr.add_argument("--evidence", required=True)
    rr.add_argument("--author", required=True)
    rr.add_argument("--checker", required=True)
    pr = sub.add_parser("promote")
    pr.add_argument("--state-dir", required=True)
    pr.add_argument("--id", required=True)
    pr.add_argument("--to", choices=list(LIFECYCLE[1:]) + sorted(ALIASES), required=True)
    pr.add_argument("--approval", default=None,
                    help="approval file (ari-lesson-approval/1) naming the lesson id and its "
                         "content hash; needed for PROMOTED")
    rp = sub.add_parser("replay", help="re-run a lesson's trigger on the stored snapshots")
    rp.add_argument("--state-dir", required=True)
    rp.add_argument("--id", required=True)
    pr.add_argument("--actor", required=True)
    pr.add_argument("--owner-approved", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "run" and not (math.isfinite(args.quiet_seconds)
                                  and args.quiet_seconds >= 0):
        ap.error("--quiet-seconds must be a finite number of at least 0")
    state_dir = Path(args.state_dir).expanduser()
    try:
        if args.cmd == "run":
            roots, now, since = resolve(args)
            # ARI2-R11: active files are read to their last complete line, not skipped
            ev = ingest(roots, since=since, until=now, want_exposure=True,
                        quiet_seconds=args.quiet_seconds, now=now, skip_active=False)
            print(json.dumps(nightly(state_dir, ev, now, max(1, int(args.days))), indent=1))
            for w in ev.coverage.warnings():
                print(f"warning: {w}", file=sys.stderr)
        elif args.cmd == "status":
            s = load_state(state_dir)
            counts: dict[str, int] = {}
            for lesson in s["lessons"].values():
                counts[lesson["state"]] = counts.get(lesson["state"], 0) + 1
            print(json.dumps({"snapshots": len(s["snapshots"]), "lessons": counts,
                              "runs": len(s["runs"])}, indent=1))
        elif args.cmd == "record-result":
            out = add_result(state_dir, args.id, args.explanation, args.outcome,
                             Path(args.evidence), args.author, args.checker)
            print(json.dumps({"id": out["id"], "state": out["state"],
                              "verification_state": out["verification_state"]}))
        elif args.cmd == "replay":
            res = replay(state_dir, args.id)
            print(json.dumps(res, indent=1))
            return 0 if res["passed"] else 1
        else:
            out = promote(state_dir, args.id, args.to, args.actor, args.owner_approved,
                          Path(args.approval) if args.approval else None)
            print(json.dumps({"id": out["id"], "state": out["state"]}))
    except (PromotionError, KeyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001  a hand edited state must not end in a traceback
        print(f"refused: state could not be used ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

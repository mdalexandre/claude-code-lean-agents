#!/usr/bin/env python3
"""Write a self contained offline HTML report of your Claude Code agent runtime.

Optional --share writes a privacy screened JSON summary to a local file and prints it for
you to read. Nothing is uploaded: publishing it is a separate step you take yourself.

Usage:
    python3 ari_report.py --out report.html [--share share.json] [--state-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ari import audit, forecast, interpret, report
from ari.cli_common import add_common, resolve
from ari.evidence import ingest
from ari.learn import load_state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    add_common(ap, days=28)
    ap.add_argument("--out", required=True, help="HTML file to write")
    ap.add_argument("--share", default=None, help="also write the share summary to this file")
    ap.add_argument("--share-min-runs", type=int, default=5)
    ap.add_argument("--skills-root", action="append", default=None)
    ap.add_argument("--mcp-config", action="append", default=None)
    ap.add_argument("--state-dir", default=None, help="nightly state to show lessons from")
    ap.add_argument("--accept-coverage-gaps", action="store_true",
                    help="allow removal advice although some files or lines were not read")
    args = ap.parse_args(argv)
    if args.share_min_runs < 5:
        ap.error("--share-min-runs below 5 is not allowed: small groups can identify a setup")
    roots, now, since = resolve(args)
    skills = args.skills_root or [os.path.expanduser("~/.claude/skills")]
    configs = args.mcp_config or [os.path.expanduser("~/.claude.json")]
    ev = ingest(roots, since=since, until=now, want_tools=True, want_exposure=True,
                skills_roots=skills, now=now)
    burn = forecast.burn(ev, now, data_since=since)
    window = audit.observed_window_days(ev, now, args.days)
    aud = audit.audit(ev, skills, configs, window,
                      accept_coverage_gaps=args.accept_coverage_gaps)
    lessons: list[dict[str, object]] = []
    if args.state_dir:
        st = load_state(Path(args.state_dir).expanduser())
        lessons = list(st["lessons"].values())
    findings = ([interpret.burn_finding(ev, burn).to_dict()] + list(aud.get("findings", [])))
    page = report.render_html(ev, burn, aud, report.gap_hypotheses(ev), lessons, now, findings)
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({len(page):,} bytes, {ev.coverage.requests:,} requests)")
    for w in ev.coverage.warnings():
        print(f"warning: {w}")
    if args.share:
        share = report.build_share(ev, args.share_min_runs)
        problems = report.screen_share(share)
        if problems:
            print("share summary refused: " + "; ".join(problems), file=sys.stderr)
            return 1
        Path(args.share).write_text(json.dumps(share, indent=1), encoding="utf-8")
        print(f"wrote {args.share} locally; nothing was uploaded. Read it before sharing:")
        print(json.dumps(share, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

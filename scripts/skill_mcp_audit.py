#!/usr/bin/env python3
"""Audit installed, exposed and invoked skills and MCP servers from your transcripts.

Recommendations only: nothing is uninstalled, disabled or edited. MCP config files are
read for server names only; commands, arguments and env values are never printed.

Usage:
    python3 skill_mcp_audit.py [--skills-root DIR] [--mcp-config FILE] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ari import audit
from ari.cli_common import MAX_DAYS, add_common, bounded_float, bounded_int, resolve
from ari.evidence import ingest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    add_common(ap, days=28)
    ap.add_argument("--skills-root", action="append", default=None,
                    help="skills directory, repeatable (default ~/.claude/skills)")
    ap.add_argument("--mcp-config", action="append", default=None,
                    help="JSON config with mcpServers, repeatable (default ~/.claude.json)")
    ap.add_argument("--min-sessions", type=bounded_int(1, 10**6), default=20)
    ap.add_argument("--min-days", type=bounded_float(1, MAX_DAYS), default=14)
    ap.add_argument("--accept-coverage-gaps", action="store_true",
                    help="allow removal advice although some files or lines were not read "
                         "(read the coverage warnings first)")
    args = ap.parse_args(argv)
    roots, now, since = resolve(args)
    skills = args.skills_root or [os.path.expanduser("~/.claude/skills")]
    configs = args.mcp_config or [os.path.expanduser("~/.claude.json")]
    ev = ingest(roots, since=since, until=now, include_subagents=True, want_tools=True,
                want_exposure=True, skills_roots=skills, now=now)
    window = audit.observed_window_days(ev, now, args.days)
    r = audit.audit(ev, skills, configs, window, args.min_sessions, args.min_days,
                    args.accept_coverage_gaps)
    r["coverage_warnings"] = ev.coverage.warnings()
    if args.json:
        print(json.dumps(r, indent=1))
    else:
        print(audit.render_text(r))
        for w in r["coverage_warnings"]:
            print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

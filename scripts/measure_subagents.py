#!/usr/bin/env python3
"""Report how many tokens each subagent type starts with, from your own transcripts.

Claude Code saves every subagent run under ~/.claude/projects/<project>/<session>/subagents/
as a .jsonl transcript plus a .meta.json that names the agent type. The first API request
of a run shows the context the agent carried before it did any work: input tokens plus
cache writes plus cache reads. This script prints the median of that number per agent
type, so you can compare general-purpose with lean-worker and lean-checker on your setup.

Reads only. Standard library only. Usage:
    python3 measure_subagents.py [--days 30] [--root ~/.claude/projects]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import time
from collections import defaultdict


def first_request_tokens(path: str) -> int | None:
    """Context size of the first assistant API request in a transcript, or None."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if rec.get("type") != "assistant" or not usage:
                    continue
                return int(
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
    except OSError:
        return None
    return None


def collect(root: str, days: float) -> dict[str, list[int]]:
    cutoff = time.time() - days * 86400
    sizes: dict[str, list[int]] = defaultdict(list)
    for meta in glob.glob(os.path.join(root, "*", "*", "subagents", "*.meta.json")):
        transcript = meta[: -len(".meta.json")] + ".jsonl"
        if not os.path.exists(transcript) or os.path.getmtime(transcript) < cutoff:
            continue
        try:
            with open(meta, encoding="utf-8") as fh:
                agent_type = str(json.load(fh).get("agentType") or "general-purpose")
        except (OSError, json.JSONDecodeError):
            continue
        tokens = first_request_tokens(transcript)
        if tokens:
            sizes[agent_type].append(tokens)
    return sizes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--min-runs", type=int, default=3)
    args = ap.parse_args()
    sizes = collect(args.root, args.days)
    rows = sorted(
        ((t, v) for t, v in sizes.items() if len(v) >= args.min_runs),
        key=lambda tv: -statistics.median(tv[1]),
    )
    if not rows:
        print(f"No agent type with {args.min_runs}+ runs in the last {args.days:g} days.")
        return
    print(f"{'agent type':<32} {'runs':>6} {'median start':>13} {'p90 start':>10}")
    for agent_type, vals in rows:
        vals.sort()
        p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
        med = statistics.median(vals)
        print(f"{agent_type[:32]:<32} {len(vals):>6} {med / 1000:>12.1f}k {p90 / 1000:>9.1f}k")


if __name__ == "__main__":
    main()

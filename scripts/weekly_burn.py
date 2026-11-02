#!/usr/bin/env python3
"""Weekly measured token burn from your own Claude Code transcripts, with a forecast.

Units are transcript tokens, not subscription quota units, so the provider quota ETA is
always reported as unavailable. --budget forecasts against a budget you choose, labelled
as user defined. Reads only; standard library only; sends nothing.

Usage:
    python3 weekly_burn.py [--metric total] [--period-start ISO] [--budget N] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys

from ari import forecast
from ari.cli_common import add_common, bounded_float, bounded_int, parse_time, resolve, time_arg
from ari.evidence import ingest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    add_common(ap, days=28)
    ap.add_argument("--metric", choices=forecast.METRICS, default="total",
                    help="token category to forecast; total is the unweighted sum of all "
                         "categories (default total)")
    ap.add_argument("--period-start", type=time_arg, default=None,
                    help="start of the period to forecast (default: Monday 00:00 UTC)")
    ap.add_argument("--period-days", type=bounded_float(forecast.MIN_PERIOD_DAYS,
                                                        forecast.MAX_PERIOD_DAYS), default=7,
                    help="period length in days, from 1 hour to 366 days (default 7)")
    ap.add_argument("--budget", type=bounded_int(1, 10**18), default=None,
                    help="user-defined token budget for the period (not an official cap)")
    ap.add_argument("--tz-offset-minutes", type=bounded_int(-1440, 1440), default=0)
    args = ap.parse_args(argv)
    roots, now, since = resolve(args)
    start = parse_time(args.period_start) if args.period_start else None
    ev = ingest(roots, since=since, until=now, quiet_seconds=0, now=now)
    r = forecast.burn(ev, now, args.metric, start, args.period_days, args.budget,
                      args.tz_offset_minutes, data_since=since)
    print(json.dumps(r, indent=1) if args.json else forecast.render_text(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Shared command line options and time parsing for the ARI scripts."""
from __future__ import annotations

import argparse
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Callable

DEFAULT_ROOT = os.path.join("~", ".claude", "projects")


def add_common(ap: argparse.ArgumentParser, days: float = 28) -> None:
    ap.add_argument("--root", action="append", default=None,
                    help=f"transcript root, repeatable (default {DEFAULT_ROOT})")
    ap.add_argument("--days", type=bounded_float(1 / 24, MAX_DAYS), default=days,
                    help=f"how many days of transcripts to read (default {days:g})")
    ap.add_argument("--now", type=time_arg, default=None,
                    help="ISO time to treat as now, for reproducible runs (default: clock)")
    ap.add_argument("--json", action="store_true", help="print machine readable JSON")


MAX_DAYS = 3650.0


def bounded_float(lo: float, hi: float) -> Callable[[str], float]:
    """An argparse type: a finite number from lo to hi, inclusive."""
    def parse(text: str) -> float:
        try:
            v = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not math.isfinite(v) or not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be a finite number from {lo:g} to {hi:g}")
        return v
    return parse


def bounded_int(lo: int, hi: int) -> Callable[[str], int]:
    """An argparse type: an integer from lo to hi, inclusive."""
    def parse(text: str) -> int:
        try:
            v = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be an integer from {lo} to {hi}")
        return v
    return parse


def time_arg(text: str) -> str:
    """An argparse type: an ISO time between the years 2000 and 2999, kept as text."""
    try:
        dt = parse_time(text)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError(f"not an ISO time: {text!r}") from None
    if not 2000 <= dt.year <= 2999:
        raise argparse.ArgumentTypeError("time must be between the years 2000 and 2999")
    return text


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve(args: argparse.Namespace) -> tuple[list[str], datetime, datetime]:
    roots = args.root or [os.path.expanduser(DEFAULT_ROOT)]
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    return roots, now, since

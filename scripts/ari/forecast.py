"""Weekly measured token burn: observation, a simple forecast, a naive baseline, a backtest.

Units are transcript tokens as Claude Code logged them. They are NOT subscription quota
units: Anthropic does not publish how weekly limits map to tokens, so this module never
derives a provider quota ETA. It can forecast against a budget the user types in, and it
labels that budget as user defined.

Forecast model (explainable baseline): rate = mean of the last k complete days (k <= 7;
idle days count as 0, and at least 3 days with usage are required); projection = observed
so far + rate x remaining days. The envelope is the observed total plus the 10th and 90th
percentile day of the same history multiplied by the remaining days. It
assumes every remaining day is as quiet or as busy as those days, so it is wide on
purpose; it is not a statistical confidence interval.

Naive baseline (persistence): the last complete day x remaining days.
Backtest: for every cut day with enough history, both models predict the next H days from
data strictly before the cut, and their mean absolute errors are compared. No future data
reaches a prediction.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .evidence import EvidenceSet, UsageEvent, percentile

PROVIDER_QUOTA = "Provider quota ETA: unavailable"
METRICS = ("total", "context", "input_tokens", "cache_creation", "cache_read", "output")
MIN_HISTORY_DAYS = 3
MIN_BACKTEST_POINTS = 3
MIN_PERIOD_DAYS = 1 / 24
MAX_PERIOD_DAYS = 366.0


@dataclass
class Window:
    start: datetime
    end: datetime
    now: datetime

    @property
    def remaining_days(self) -> float:
        return max(0.0, (self.end - max(self.now, self.start)).total_seconds() / 86400)


def period_window(now: datetime, period_start: datetime | None, period_days: float) -> Window:
    """Default period: the ISO week (Monday 00:00 UTC) that contains `now`."""
    if not MIN_PERIOD_DAYS <= period_days <= MAX_PERIOD_DAYS:
        raise ValueError("period_days must be from 1 hour to 366 days")
    if period_start is None:
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = midnight - timedelta(days=midnight.weekday())
    length = timedelta(days=period_days)
    if period_start + length <= now:  # roll a stale anchor forward to the period with now
        period_start += length * ((now - period_start) // length)
        if period_start + length <= now:
            period_start += length
    return Window(period_start, period_start + length, now)


def daily_totals(events: list[UsageEvent], metric: str, tz_offset_minutes: int = 0
                 ) -> dict[date, int]:
    shift = timedelta(minutes=tz_offset_minutes)
    out: dict[date, int] = defaultdict(int)
    for ev in events:
        out[(ev.ts + shift).date()] += ev.value(metric)
    return dict(out)


def complete_days(totals: dict[date, int], first: date, before: date) -> list[tuple[date, int]]:
    """Every calendar day from `first` up to (not including) `before`; idle days are 0."""
    days: list[tuple[date, int]] = []
    d = first
    while d < before:
        days.append((d, totals.get(d, 0)))
        d += timedelta(days=1)
    return days


def trailing_rate(values: list[int], k: int = 7) -> float:
    tail = values[-k:]
    return float(statistics.fmean(tail))


def backtest(values: list[int], horizon: int = 3, k: int = 7, min_prior: int = MIN_HISTORY_DAYS
             ) -> dict[str, Any]:
    """Mean absolute error of the trailing mean and of persistence over all valid cuts."""
    errs_model: list[float] = []
    errs_naive: list[float] = []
    for i in range(min_prior, len(values) - horizon + 1):
        prior = values[:i]  # strictly before the cut: no leakage
        actual = sum(values[i:i + horizon])
        errs_model.append(abs(trailing_rate(prior, k) * horizon - actual))
        errs_naive.append(abs(prior[-1] * horizon - actual))
    n = len(errs_model)
    if n < MIN_BACKTEST_POINTS:
        return {"status": "INSUFFICIENT_HISTORY", "points": n, "horizon_days": horizon}
    mae_m = statistics.fmean(errs_model)
    mae_n = statistics.fmean(errs_naive)
    return {
        "status": "OK",
        "points": n,
        "horizon_days": horizon,
        "mae_trailing_mean": round(mae_m),
        "mae_persistence": round(mae_n),
        "better": "trailing_mean" if mae_m < mae_n else ("persistence" if mae_n < mae_m else "tie"),
    }


def burn(
    ev: EvidenceSet,
    now: datetime,
    metric: str = "total",
    period_start: datetime | None = None,
    period_days: float = 7,
    budget: int | None = None,
    tz_offset_minutes: int = 0,
    k: int = 7,
    horizon: int = 3,
    data_since: datetime | None = None,
) -> dict[str, Any]:
    """Build the weekly burn report as a JSON-able dict."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    win = period_window(now, period_start, period_days)
    shift = timedelta(minutes=tz_offset_minutes)
    in_period = [e for e in ev.usage if win.start <= e.ts < min(win.end, now)]
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in in_period:
        row = by_type[e.agent_type]
        row["requests"] += 1
        row["input_tokens"] += e.input_tokens
        row["cache_creation_5m"] += e.cache_creation_5m
        row["cache_creation_1h"] += e.cache_creation_1h
        row["cache_read"] += e.cache_read
        row["output"] += e.output
        row["metric"] += e.value(metric)
    observed = sum(r["metric"] for r in by_type.values())
    shares = sorted(
        ((t, (r["metric"] / observed) if observed else 0.0) for t, r in by_type.items()),
        key=lambda x: -x[1],
    )
    today = (now + shift).date()
    totals = daily_totals(ev.usage, metric, tz_offset_minutes)
    warnings = ev.coverage.warnings()
    result: dict[str, Any] = {
        "schema": "ari-weekly-burn/1",
        "units": f"transcript tokens, metric={metric} (not subscription quota units)",
        "metric": metric,
        "period": {"start": win.start.isoformat(), "end": win.end.isoformat(),
                   "now": now.isoformat(), "remaining_days": round(win.remaining_days, 3)},
        "observed": {"metric_total": observed, "requests": sum(r["requests"] for r in
                                                                by_type.values())},
        "by_agent_type": {t: dict(r) for t, r in sorted(by_type.items())},
        "concentration": [{"agent_type": t, "share": round(s, 4)} for t, s in shares],
        "provider_quota": PROVIDER_QUOTA,
        "warnings": warnings,
        # ARI2-R5 and R6: copies that disagree for no known reason widen a band, never a
        # silent maximum; the counted totals above are the upper bound
        "duplicate_classes": _dup_classes(in_period),
        "total_uncertainty": ev.total_uncertainty(in_period),
        "assumptions": [
            "transcript tokens only; provider quota units and weights are not observable",
            "a request is one requestId (fallback message.id); copies merge by per field "
            "maximum",
            f"days are UTC shifted by {tz_offset_minutes} minutes; today is partial",
            "forecast = observed + trailing mean x remaining days; the envelope uses the "
            "p10 and p90 history day, it is not a confidence interval",
            "files deleted by cleanupPeriodDays are missing data, read as zero days",
        ],
    }
    if data_since is not None and data_since > win.start:
        warnings.append(f"the read window opens at {data_since.isoformat()}, after the period "
                        f"start {win.start.isoformat()}: the observed total misses that part "
                        "of the period (read more --days)")
    if not totals:
        result["forecast"] = {"status": "INSUFFICIENT_DATA", "reason": "no usage records"}
        return result
    first = min(totals)
    if data_since is not None:  # the day the window opens mid-day is not a complete day
        opened = data_since + shift
        full = opened.date() + timedelta(days=0 if opened.time() == time(0) else 1)
        first = max(first, full)
    history = complete_days(totals, first, today)
    values = [v for _, v in history]
    result["daily"] = [{"day": d.isoformat(), "value": v} for d, v in history[-14:]]
    result["daily_today_partial"] = {"day": today.isoformat(), "value": totals.get(today, 0)}
    weekly: dict[str, int] = defaultdict(int)
    for d, v in history + [(today, totals.get(today, 0))]:
        y, w, _ = d.isocalendar()
        weekly[f"{y}-W{w:02d}"] += v
    result["weekly"] = [{"week": w, "value": v} for w, v in sorted(weekly.items())[-4:]]
    zero_days = sum(1 for v in values if v == 0)
    if zero_days:
        warnings.append(f"{zero_days} history days had no usage records (idle, or files "
                        "deleted by cleanupPeriodDays); they count as 0")
    active = sum(1 for v in values if v > 0)
    if len(values) < MIN_HISTORY_DAYS or active < MIN_HISTORY_DAYS:
        result["forecast"] = {
            "status": "INSUFFICIENT_DATA",
            "reason": f"{len(values)} complete history days, {active} with usage; at least "
                      f"{MIN_HISTORY_DAYS} with usage needed",
        }
        result["backtest"] = {"status": "INSUFFICIENT_HISTORY", "points": 0}
        return result
    rate = trailing_rate(values, k)
    rem = win.remaining_days
    q10 = percentile(values, 0.10)
    q90 = percentile(values, 0.90)
    projected = observed + rate * rem
    fc: dict[str, Any] = {
        "status": "OK",
        "model": f"trailing mean of the last {min(k, len(values))} complete days",
        "rate_per_day": round(rate),
        "projected_period_total": round(projected),
        "envelope": {"low": round(observed + q10 * rem), "high": round(observed + q90 * rem),
                     "method": "observed + p10/p90 history day x remaining days "
                               "(conservative envelope, not a confidence interval)"},
        "naive_persistence_total": round(observed + values[-1] * rem),
        "history_days": len(values),
    }
    if budget is not None:
        fc["budget"] = _budget(budget, observed, rate, win)
    result["forecast"] = fc
    result["backtest"] = backtest(values, horizon=horizon, k=k)
    return result


def _dup_classes(events: list[UsageEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        if e.dup_class:
            out[e.dup_class] = out.get(e.dup_class, 0) + 1
    return dict(sorted(out.items()))


def _budget(budget: int, observed: int, rate: float, win: Window) -> dict[str, Any]:
    out: dict[str, Any] = {"label": "user-defined budget (not an official subscription cap)",
                           "tokens": budget, "used_share": round(observed / budget, 4)
                           if budget > 0 else None}
    if observed >= budget:
        out["eta"] = "already reached"
    elif rate <= 0:
        out["eta"] = "not reached at the current rate"
    else:
        eta = max(win.now, win.start) + timedelta(days=(budget - observed) / rate)
        out["eta"] = eta.isoformat() if eta < win.end else "not within this period"
    return out


def render_text(r: dict[str, Any]) -> str:
    lines = [f"Weekly burn ({r['units']})",
             f"Period {r['period']['start']} to {r['period']['end']}, "
             f"{r['period']['remaining_days']} days left",
             f"Observed so far: {r['observed']['metric_total']:,} over "
             f"{r['observed']['requests']:,} requests"]
    unc = r.get("total_uncertainty")
    if unc and unc["ambiguous_requests"]:
        label = ("Total tokens" if r.get("metric", "total") == "total" else
                 f"Total tokens of all types (not metric={r['metric']})")
        lines.append(f"{label} between {unc['lower']:,} and {unc['upper']:,} "
                     f"({unc['ambiguous_requests']:,} requests with ambiguous copies)")
    if r["by_agent_type"]:
        lines.append(f"{'agent type':<28} {'requests':>9} {'metric':>14} {'cache read':>14} "
                     f"{'cache write':>13} {'share':>6}")
        share = {c["agent_type"]: c["share"] for c in r["concentration"]}
        for t, row in sorted(r["by_agent_type"].items(), key=lambda x: -x[1]["metric"]):
            cw = row["cache_creation_5m"] + row["cache_creation_1h"]
            lines.append(f"{t[:28]:<28} {row['requests']:>9,} {row['metric']:>14,} "
                         f"{row['cache_read']:>14,} {cw:>13,} {share[t]:>6.1%}")
    fc = r.get("forecast", {})
    if fc.get("status") == "OK":
        env = fc["envelope"]
        lines.append(f"Forecast period total: {fc['projected_period_total']:,} "
                     f"(envelope {env['low']:,} to {env['high']:,}; {fc['model']})")
        lines.append(f"Naive persistence: {fc['naive_persistence_total']:,}")
        if "budget" in fc:
            b = fc["budget"]
            lines.append(f"{b['label']}: {b['tokens']:,}, ETA {b['eta']}")
    else:
        lines.append(f"Forecast: {fc.get('status')} ({fc.get('reason', '')})")
    bt = r.get("backtest")
    if bt:
        if bt.get("status") == "OK":
            lines.append(f"Backtest {bt['points']} cuts, {bt['horizon_days']} day horizon: "
                         f"MAE trailing mean {bt['mae_trailing_mean']:,} vs persistence "
                         f"{bt['mae_persistence']:,} (better: {bt['better']})")
        else:
            lines.append(f"Backtest: {bt['status']}")
    lines.append(r["provider_quota"])
    for w in r["warnings"]:
        lines.append(f"warning: {w}")
    return "\n".join(lines)

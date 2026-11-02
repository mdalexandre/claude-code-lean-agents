from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from ari import forecast
from ari.evidence import Coverage, EvidenceSet, UsageEvent

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)  # Wednesday noon


def _ev(per_day: dict[int, int], agent: str = "main") -> EvidenceSet:
    """per_day maps days before NOW's date to an input token count (one request per day)."""
    usage = []
    for back, n in per_day.items():
        ts = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc) - timedelta(days=back)
        usage.append(UsageEvent(f"r{back}", 0, 1, "s", None, agent, "main", "m", "2.1.316", ts,
                                n, 0, 0, 0, 0))
    cov = Coverage(requests=len(usage))
    return EvidenceSet([], usage, [], [], cov, (None, None))


def test_known_rate_is_reproducible() -> None:
    ev = _ev({b: 1000 for b in range(0, 15)})
    r1 = forecast.burn(ev, NOW, metric="input_tokens")
    r2 = forecast.burn(ev, NOW, metric="input_tokens")
    assert r1 == r2
    fc = r1["forecast"]
    assert fc["status"] == "OK" and fc["rate_per_day"] == 1000
    # Monday 00:00 to Wednesday noon: Mon, Tue and today's 06:00 record = 3000 observed
    assert r1["observed"]["metric_total"] == 3000
    rem = r1["period"]["remaining_days"]
    assert rem == pytest.approx(4.5)
    assert fc["projected_period_total"] == round(3000 + 1000 * 4.5)
    assert fc["envelope"]["low"] == fc["envelope"]["high"] == fc["projected_period_total"]


def test_insufficient_history_refuses() -> None:
    r = forecast.burn(_ev({0: 500, 1: 500}), NOW)
    assert r["forecast"]["status"] == "INSUFFICIENT_DATA"
    assert "projected_period_total" not in r["forecast"]
    assert r["provider_quota"] == "Provider quota ETA: unavailable"


def test_no_data_refuses() -> None:
    ev = EvidenceSet([], [], [], [], Coverage(), (None, None))
    r = forecast.burn(ev, NOW)
    assert r["forecast"]["status"] == "INSUFFICIENT_DATA"
    assert any("missing data" in w for w in r["warnings"])


def test_units_and_quota_disclosed() -> None:
    r = forecast.burn(_ev({b: 10 for b in range(10)}), NOW, budget=10**6)
    assert "not subscription quota units" in r["units"]
    assert r["provider_quota"] == forecast.PROVIDER_QUOTA
    b = r["forecast"]["budget"]
    assert b["label"].startswith("user-defined budget")
    assert b["eta"] == "not within this period"
    text = forecast.render_text(r)
    assert "Provider quota ETA: unavailable" in text and "user-defined budget" in text


def test_budget_eta_inside_period() -> None:
    ev = _ev({b: 1000 for b in range(10)})
    r = forecast.burn(ev, NOW, metric="input_tokens", budget=5000)
    eta = datetime.fromisoformat(r["forecast"]["budget"]["eta"])
    assert eta == NOW + timedelta(days=2)


def test_zero_days_warned_and_counted() -> None:
    r = forecast.burn(_ev({1: 100, 5: 100, 9: 100}), NOW)
    assert any("had no usage records" in w for w in r["warnings"])


def test_backtest_uses_only_past_data(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = forecast.trailing_rate

    def spy(values: list[int], k: int = 7) -> float:
        seen.append(len(values))
        return real(values, k)

    monkeypatch.setattr(forecast, "trailing_rate", spy)
    vals = [10] * 10 + [10**6]
    bt = forecast.backtest(vals, horizon=1)
    assert seen == list(range(3, len(vals)))  # cut i sees exactly values[:i]
    assert bt["status"] == "OK" and bt["points"] == 8
    # a later spike cannot change the errors of earlier cuts
    early = forecast.backtest(vals[:10], horizon=1)
    assert early["mae_trailing_mean"] == 0


def test_backtest_compares_naive_baseline() -> None:
    alternating = [100, 0] * 8
    bt = forecast.backtest(alternating, horizon=1, k=2)
    assert bt["mae_persistence"] == 100 and bt["mae_trailing_mean"] == 50
    assert bt["better"] == "trailing_mean"
    assert forecast.backtest([1, 2, 3, 4], horizon=1)["status"] == "INSUFFICIENT_HISTORY"


def test_timezone_offset_moves_day_boundary() -> None:
    ts = datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc)
    ev = [UsageEvent("a", 0, 1, "s", None, "main", "main", "m", "v", ts, 5, 0, 0, 0, 0)]
    assert list(forecast.daily_totals(ev, "input_tokens", 0)) == [ts.date()]
    assert list(forecast.daily_totals(ev, "input_tokens", 60)) == [ts.date() + timedelta(days=1)]


def test_concentration_and_cache_kept_separate() -> None:
    ev = _ev({0: 100, 1: 100, 2: 100, 3: 100}, agent="general-purpose")
    ev.usage[0].cache_read = 900
    r: dict[str, Any] = forecast.burn(ev, NOW, metric="total")
    row = r["by_agent_type"]["general-purpose"]
    assert row["cache_read"] in (0, 900) and "cache_creation_5m" in row
    assert r["concentration"][0]["agent_type"] == "general-purpose"
    assert r["concentration"][0]["share"] == 1.0


def test_period_anchor_rolls_forward() -> None:
    win = forecast.period_window(NOW, datetime(2026, 10, 1, tzinfo=timezone.utc), 7)
    assert win.start <= NOW < win.end

from datetime import UTC, date, datetime, timedelta

from app.strategy_economics import promotion_assessment, strategy_economics


def _row(i: int, value: float, *, symbol: str | None = None):
    d = date(2026, 7, 1) + timedelta(days=i // 10)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return {
        "symbol": symbol or f"S{i%8}",
        "bar_ts": datetime(d.year, d.month, d.day, 14, i % 10, tzinfo=UTC),
        "trade_date": d,
        "gross_return_pct": value,
        "mae_pct": min(-0.05, value - 0.3),
        "mfe_pct": max(0.05, value + 0.3),
    }


def test_lower_hit_rate_can_have_positive_whole_strategy_expectancy():
    rows = [_row(i, 1.20 if i % 5 in {0, 1} else -0.35) for i in range(100)]
    metrics = strategy_economics(rows, cost_bps=20, holding_horizon_minutes=30)
    dist = metrics["return_distribution"]
    assert dist["trade_win_rate_pct"] == 40.0
    assert dist["net_expected_value_per_trade_pct"] > 0
    assert dist["profit_factor"] > 1


def test_extreme_winner_dependence_is_visible():
    rows = [_row(i, 0.05) for i in range(99)] + [_row(99, 20.0)]
    metrics = strategy_economics(rows, cost_bps=0, holding_horizon_minutes=30)
    dist = metrics["return_distribution"]
    assert dist["top_1pct_return_share_pct"] > 50
    assert "net_expectancy_excluding_top_1pct_pct" in dist


def test_no_trade_market_days_are_included():
    rows = [
        _row(0, 0.5),
        {
            **_row(0, 0.5),
            "bar_ts": datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
            "trade_date": date(2026, 7, 3),
        },
    ]
    metrics = strategy_economics(rows, cost_bps=0, holding_horizon_minutes=30)
    cal = metrics["calendar_consistency"]
    assert cal["market_days"] >= cal["active_days"]
    assert cal["average_return_per_market_day_pct"] <= cal["average_return_per_active_day_pct"]


def test_promotion_does_not_use_hit_rate():
    rows = [_row(i, 1.5 if i % 5 in {0, 1} else -0.3) for i in range(300)]
    economics = strategy_economics(rows, cost_bps=20, holding_horizon_minutes=30)
    positive = {"observations": 300, "dates": 30, "net_avg_pct": 0.1, "date_clustered_t_stat": 2.0}
    summary = {
        "base": positive,
        "cost_sensitivity": {"30": positive},
        "entry_delay_sensitivity": {"2": positive},
        "neighbourhood": {"relaxed": positive, "tightened": positive},
    }
    assessment = promotion_assessment(economics, summary, mode="development")
    assert assessment["hit_rate_used_as_primary_optimisation_target"] is False

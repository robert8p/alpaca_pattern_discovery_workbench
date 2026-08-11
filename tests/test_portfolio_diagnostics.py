from datetime import UTC, date, datetime, timedelta

from app.portfolio_diagnostics import liquidity_metrics, portfolio_diagnostics


def _row(i: int, gross: float = 1.0, liquidity: float = 100_000.0):
    ts = datetime(2026, 7, 6, 14, 0, tzinfo=UTC) + timedelta(minutes=i * 20)
    return {
        "symbol": f"S{i}",
        "bar_ts": ts,
        "trade_date": ts.date(),
        "gross_return_pct": gross,
        "bar_dollar_volume": liquidity * 1.1,
        "trailing_20bar_dollar_volume_proxy": liquidity,
    }


def test_liquidity_capacity_is_participation_bounded():
    metrics = liquidity_metrics([_row(0, liquidity=100_000), _row(1, liquidity=200_000)])
    one_pct = metrics["capacity_proxies"]["1pct_participation"]
    assert one_pct["minimum_trade_capacity_usd"] == 1_000
    assert one_pct["median_trade_capacity_usd"] == 1_500


def test_portfolio_scenarios_measure_capital_utilisation_and_return():
    diagnostics = portfolio_diagnostics(
        [_row(i, gross=1.0, liquidity=1_000_000) for i in range(8)],
        cost_bps=20,
        horizon_minutes=15,
        direction="long",
    )
    assert diagnostics["available"] is True
    scenarios = diagnostics["capital_scenario_grid"]["scenarios"]
    scenario = next(
        s for s in scenarios
        if s["capital_usd"] == 10_000 and s["position_slots"] == 10 and s["participation_rate_pct"] == 1.0
    )
    assert scenario["return_on_total_available_capital_pct"] > 0
    assert scenario["average_capital_utilisation_during_market_time_pct"] > 0
    assert scenario["peak_gross_exposure_pct_of_starting_capital"] <= 100.000001


def test_capacity_constraint_is_recorded_without_optimising_around_it():
    diagnostics = portfolio_diagnostics(
        [_row(i, gross=1.0, liquidity=1_000) for i in range(3)],
        cost_bps=20,
        horizon_minutes=15,
        direction="short",
    )
    scenario = next(
        s for s in diagnostics["capital_scenario_grid"]["scenarios"]
        if s["capital_usd"] == 1_000_000 and s["position_slots"] == 10 and s["participation_rate_pct"] == 1.0
    )
    assert scenario["capacity_constrained_allocations"] == 3
    assert "No scenario is labelled best" in diagnostics["capital_scenario_grid"]["selection_policy"]

from datetime import UTC, date, datetime, timedelta

from app.regime_diagnostics import regime_diagnostics


THRESHOLDS = {
    "dispersion_30m_pct": {"p25": 0.5, "p75": 1.5, "observations": 1000},
    "pct_abnormal_volatility": {"p25": 10.0, "p75": 30.0, "observations": 1000},
    "median_relative_volume": {"p25": 0.8, "p75": 1.4, "observations": 1000},
}


def _row(i: int, gross: float = 0.5, **overrides):
    ts = datetime(2026, 7, 6, 14, 30, tzinfo=UTC) + timedelta(minutes=i)
    row = {
        "symbol": f"S{i % 5}",
        "bar_ts": ts,
        "trade_date": date(2026, 7, 6),
        "minute_of_day": 570 + i,
        "gross_return_pct": gross,
        "market_state_run_id": "state-run",
        "spy_return_30m_pct": 0.50,
        "pct_positive_30m": 70.0,
        "dispersion_30m_pct": 2.0,
        "pct_abnormal_volatility": 35.0,
        "prior_pct_abnormal_volatility": 25.0,
        "median_relative_volume": 1.6,
    }
    row.update(overrides)
    return row


def test_regime_diagnostics_are_unavailable_without_full_timeline_calibration():
    result = regime_diagnostics(
        [_row(0)],
        cost_bps=20,
        calibration_thresholds=None,
        calibration_market_state_timestamps=0,
        market_state_run_id=None,
    )
    assert result["available"] is False
    assert result["sealed_data_accessed"] is False


def test_objective_regime_labels_use_fixed_market_state_rules():
    rows = [_row(i) for i in range(40)]
    result = regime_diagnostics(
        rows,
        cost_bps=20,
        calibration_thresholds=THRESHOLDS,
        calibration_market_state_timestamps=5000,
        market_state_run_id="state-run",
    )
    assert result["available"] is True
    assert result["calibration_market_state_timestamps"] == 5000
    assert result["regimes"]["market_direction"]["rising_market"]["observations"] == 40
    assert result["regimes"]["broad_market_breadth"]["broad_market_strength"]["observations"] == 40
    assert result["regimes"]["volatility_level"]["high_volatility"]["observations"] == 40
    assert result["regimes"]["volatility_change"]["volatility_expansion"]["observations"] == 40
    assert result["regimes"]["cross_sectional_dispersion"]["high_dispersion"]["observations"] == 40
    assert result["regimes"]["market_liquidity_activity"]["high_liquidity_activity"]["observations"] == 40


def test_negative_adequately_sampled_regime_is_flagged_not_filtered():
    rows = [_row(i, gross=0.0) for i in range(40)]
    result = regime_diagnostics(
        rows,
        cost_bps=20,
        calibration_thresholds=THRESHOLDS,
        calibration_market_state_timestamps=5000,
        market_state_run_id="state-run",
    )
    assert result["negative_supported_regime_count"] > 0
    assert all(item["mean_net_return_pct"] < 0 for item in result["negative_supported_regimes"])
    assert "must not be converted into rescue filters" in result["promotion_note"]

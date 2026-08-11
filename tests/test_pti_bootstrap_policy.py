from pathlib import Path


def test_pti_bootstrap_requires_prior_trading_days_and_adds_first_usable_date():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert 'PTI_UNIVERSE_VERSION = "1.0.1"' in source
    assert 'cadence = "single_date" if config.scope == "one_day_test" else "monthly_with_bootstrap"' in source
    assert 'minimum_trading_days = int(selection.get("minimum_trading_days") or 15)' in source
    assert 'candidate - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) <= prior < candidate' in source
    assert 'snapshots = [eligible_dates[0]]' in source
    assert "not SPY's older standalone history" in source
    assert "The first eligible date is an explicit bootstrap snapshot" in source

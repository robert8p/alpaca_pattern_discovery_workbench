from pathlib import Path


def test_pti_bootstrap_requires_prior_trading_days_and_adds_first_usable_date():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert 'PTI_UNIVERSE_VERSION = "1.0.2"' in source
    assert 'cadence = "single_date" if config.scope == "one_day_test" else "monthly"' in source
    assert 'minimum_trading_days = int(selection.get("minimum_trading_days") or 15)' in source
    assert 'candidate - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) <= prior < candidate' in source
    assert 'snapshots = [eligible_dates[0]]' in source
    assert "not SPY's older standalone history" in source
    assert "The first eligible date is an explicit bootstrap snapshot" in source


def test_completed_pti_run_can_extend_without_recomputing_snapshot_membership():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert "requested_start=%s,requested_end=%s,methodology_version=%s,completed_at=NULL" in source
    assert "ON CONFLICT (point_in_time_universe_run_id,snapshot_date) DO UPDATE SET" in source
    assert "effective_start=excluded.effective_start" in source
    assert "effective_end=excluded.effective_end" in source
    assert "lookback_start=excluded.lookback_start" in source
    assert "lookback_end=excluded.lookback_end" in source
    assert 'if snapshot["status"] == "completed" and snapshot.get("snapshot_universe_run_id"):' in source
    assert "Existing snapshots can safely extend their effective end date" in source

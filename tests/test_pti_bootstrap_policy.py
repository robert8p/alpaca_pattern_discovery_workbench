from pathlib import Path


def test_pti_bootstrap_requires_real_prior_history_and_first_research_trade_date():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert 'PTI_UNIVERSE_VERSION = "1.1.0"' in source
    assert 'cadence = "single_date" if config.scope == "one_day_test" else "monthly"' in source
    assert 'minimum_trading_days = int(selection.get("minimum_trading_days") or 15)' in source
    assert 'candidate - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) <= prior < candidate' in source
    assert 'snapshots = [eligible_dates[0]]' in source
    assert 'required_warmup_start' in source
    assert 'all_known_warmup_ready' in source
    assert 'inactive_survivorship_ready' in source
    assert 'research_dates = [d for d in trading_dates if config.start_date <= d <= config.end_date]' in source


def test_completed_pti_run_can_extend_without_recomputing_snapshot_membership():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert "requested_start=%s,requested_end=%s,methodology_version=%s,source_config=%s,completed_at=NULL" in source
    assert "ON CONFLICT (point_in_time_universe_run_id,snapshot_date) DO UPDATE SET" in source
    assert "effective_start=excluded.effective_start" in source
    assert "effective_end=excluded.effective_end" in source
    assert "lookback_start=excluded.lookback_start" in source
    assert "lookback_end=excluded.lookback_end" in source
    assert 'if snapshot["status"] == "completed" and snapshot.get("snapshot_universe_run_id"):' in source


def test_full_history_is_blocked_until_all_source_components_are_complete():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert 'if config.scope == "full_history":' in source
    assert 'point_in_time_source_readiness(reference_universe_run_id, config.start_date, config.end_date)' in source
    assert 'raise RuntimeError("Point-in-time historical source is not ready:' in source

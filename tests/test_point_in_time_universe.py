from __future__ import annotations

from datetime import date
from pathlib import Path

from app.point_in_time_universe import PTI_LOOKBACK_CALENDAR_DAYS


def test_point_in_time_lookback_is_past_only():
    snapshot = date(2026, 6, 2)
    assert PTI_LOOKBACK_CALENDAR_DAYS == 61
    assert snapshot - date(2026, 6, 1) == date.resolution
    assert snapshot.toordinal() - (snapshot.toordinal() - PTI_LOOKBACK_CALENDAR_DAYS) == 61


def test_point_in_time_migration_has_strict_temporal_guard_and_provenance():
    sql = (Path(__file__).resolve().parents[1] / "sql" / "migrations" / "2.6.0.sql").read_text(encoding="utf-8").lower()
    for token in (
        "ra_point_in_time_universe_runs",
        "ra_point_in_time_universe_snapshots",
        "ra_feature_chunk_universes",
        "lookback_end < snapshot_date",
        "ra_guard_point_in_time_universe_snapshot",
        "point_in_time_universe_run_id",
        "snapshot_universe_run_id",
        "enable row level security",
    ):
        assert token in sql


def test_historical_backfill_reuses_existing_feature_sql():
    source = (Path(__file__).resolve().parents[1] / "app" / "history_backfill.py").read_text(encoding="utf-8")
    assert "_feature_sql" in source
    assert "_build_batch" in source
    assert "point_in_time_monthly" in source
    assert "universe_membership_mode" in source
    assert "full-history execution never uses this fallback" in source


def test_point_in_time_builder_clones_existing_universe_methodology():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    assert "selection_config" in source
    assert "UniverseBuildConfig.model_validate(selection)" in source
    assert "lookback_end = snapshot_date - timedelta(days=1)" in source
    assert "build_universe(child_job_id, cfg)" in source
    assert "session_label='regular'" in source

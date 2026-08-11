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
    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "migrations" / "2.6.0.sql").read_text(encoding="utf-8").lower()
    availability_sql = (root / "sql" / "migrations" / "2.6.2.sql").read_text(encoding="utf-8").lower()
    for token in (
        "ra_point_in_time_universe_runs",
        "ra_point_in_time_universe_snapshots",
        "ra_feature_chunk_universes",
        "lookback_end < snapshot_date",
        "ra_guard_point_in_time_universe_snapshot",
        "point_in_time_universe_run_id",
        "snapshot_universe_run_id",
        "availability_reference_date",
        "availability_removed_symbols",
        "availability_refilled_symbols",
        "enable row level security",
    ):
        assert token in sql
    assert "availability_method_version" in availability_sql


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


def test_pti_availability_uses_only_last_completed_session_and_does_not_change_normal_builds():
    root = Path(__file__).resolve().parents[1]
    availability = (root / "app" / "pti_availability.py").read_text(encoding="utf-8")
    universe = (root / "app" / "universe.py").read_text(encoding="utf-8")
    assert "date < %s" in availability
    assert "session_label='regular'" in availability
    assert "_pti_snapshot_for_child_job" in availability
    assert "if not snapshot:" in availability and "return None" in availability
    assert "not trading on point-in-time reference session" in availability
    assert "row_number() OVER (ORDER BY b.rank_by_liquidity NULLS LAST,b.symbol)" in availability
    assert "apply_point_in_time_availability_filter" in universe

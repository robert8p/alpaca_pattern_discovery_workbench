from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.features import _feature_sql, _utc_bounds
from app.full_history import (
    HISTORY_START_DATE, PRESEALED_END_DATE, SEALED_START_DATE,
    _candidate_wave_query, _market_state_insert_sql, _month_partitions,
    feature_definition_hash,
)
from app.models import (
    CandidateWaveBuildConfig, DiscoveryConfig, HistoricalFeatureBackfillConfig,
    MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig,
)


def test_phase1_research_period_boundaries_are_frozen():
    assert HISTORY_START_DATE == date(2025, 5, 4)
    assert PRESEALED_END_DATE == date(2026, 8, 3)
    assert SEALED_START_DATE == date(2026, 8, 4)
    parts = _month_partitions(HISTORY_START_DATE, PRESEALED_END_DATE)
    assert parts[0] == (date(2025, 5, 4), date(2025, 5, 31))
    assert parts[-1] == (date(2026, 8, 1), date(2026, 8, 3))
    assert len(parts) == 16


def test_discovery_and_robustness_cannot_cross_sealed_boundary():
    fid = uuid4()
    with pytest.raises(ValueError, match="sealed holdout"):
        DiscoveryConfig(
            name="bad sealed discovery", feature_set_id=fid,
            discovery_start="2026-07-01", discovery_end="2026-08-04",
            directions=["long"], holding_horizons_minutes=[5], families=["time_of_day"],
        )
    with pytest.raises(ValueError, match="sealed holdout"):
        RobustnessAnalysisConfig(candidate_id=uuid4(), mode="historical_holdout", start_date="2026-08-01", end_date="2026-08-04")
    SealedEvaluationConfig(candidate_id=uuid4(), sealed_start="2026-08-04", sealed_end="2026-08-05")
    with pytest.raises(ValueError, match="True sealed"):
        SealedEvaluationConfig(candidate_id=uuid4(), sealed_start="2026-08-03", sealed_end="2026-08-05")


def test_backfill_scope_is_fixed_and_one_day_test_is_one_day():
    rid = uuid4()
    cfg = HistoricalFeatureBackfillConfig(reference_feature_set_id=rid, start_date="2025-05-05", end_date="2025-05-05", scope="one_day_test")
    assert cfg.start_date == cfg.end_date
    with pytest.raises(ValueError, match="one-day"):
        HistoricalFeatureBackfillConfig(reference_feature_set_id=rid, start_date="2025-05-05", end_date="2025-05-06", scope="one_day_test")
    with pytest.raises(ValueError, match="sealed holdout"):
        HistoricalFeatureBackfillConfig(reference_feature_set_id=rid, start_date="2026-08-04", end_date="2026-08-04", scope="one_day_test")


def test_market_state_and_wave_configs_are_presealed_only():
    with pytest.raises(ValueError, match="sealed holdout"):
        MarketStateBuildConfig(feature_set_id=uuid4(), start_date="2026-08-04", end_date="2026-08-04")
    with pytest.raises(ValueError, match="sealed holdout"):
        CandidateWaveBuildConfig(candidate_id=uuid4(), start_date="2026-08-04", end_date="2026-08-04")


def test_market_state_sql_is_predictor_only_and_point_in_time():
    sql = _market_state_insert_sql().lower()
    assert "lead(" not in sql
    assert "fwd_return_" not in sql
    assert " following" not in sql
    assert "bar_ts-interval '5 minutes'" in sql
    assert "rows between 20 preceding and 1 preceding" in sql
    assert "america/new_york" in sql
    assert "spy" in sql and "qqq" in sql
    assert "on conflict (market_state_run_id,bar_ts) do update" in sql


def test_candidate_wave_sql_has_no_forward_outcome_or_future_window():
    sql, params = _candidate_wave_query([{"column":"ret_5m_pct","operator":"gte","value":1.0}], "ret_5m_pct", 1.0)
    lower = sql.lower()
    assert "fwd_return_" not in lower
    assert "lead(" not in lower
    assert " following" not in lower
    assert "lag(qualifying_stock_count)" in lower
    assert len(params) == 1


def test_feature_definition_source_is_not_replaced_by_phase1():
    assert len(feature_definition_hash()) == 64
    source = Path(__file__).resolve().parents[1] / "app" / "features.py"
    text = source.read_text(encoding="utf-8")
    assert "ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING" in text
    assert "ROWS BETWEEN %s PRECEDING AND 1 PRECEDING" not in text  # baseline window is rendered safely, not future-looking
    assert "America/New_York" in text
    assert "lag(" in text
    assert "lead(" in text  # outcomes remain separate labels; predictor checks are exact timestamp gated


def test_et_utc_bounds_follow_dst_without_fixed_offset_assumption():
    winter_start, _ = _utc_bounds(date(2026, 2, 2), date(2026, 2, 2))
    summer_start, _ = _utc_bounds(date(2026, 6, 2), date(2026, 6, 2))
    assert winter_start.hour == 5
    assert summer_start.hour == 4


def test_phase1_migration_defines_unique_keys_and_database_guards():
    migration = (Path(__file__).resolve().parents[1] / "sql" / "migrations" / "2.5.0.sql").read_text(encoding="utf-8").lower()
    assert "primarykey(market_state_run_id,bar_ts)" in migration.replace(" ","")
    assert "primarykey(candidate_wave_run_id,bar_ts)" in migration.replace(" ","")
    assert "ra_jobs_research_period_guard" in migration
    assert "candidate must be frozen in the research ledger" in migration
    assert "full historical backfill execution is locked in phase 1" in migration
    assert "frozen research ledger candidates are immutable" in migration
    assert "on conflict (version) do update" in migration
    assert "alter table ra_research_ledger enable row level security" in migration
    assert "alter function ra_guard_research_job_periods() set search_path = public, pg_temp" in migration
    assert "alter table %i enable row level security" in migration


def test_phase1_does_not_modify_feature_sql_function_contract():
    # Import/signature smoke check: Phase 1 orchestration reuses this exact generator.
    assert callable(_feature_sql)


def test_phase1_generated_sql_bindings_are_exact():
    from app.sql_validation import validate_sql_bindings
    q = _market_state_insert_sql()
    params = (uuid4(), date(2025,5,5), date(2025,5,5), 1, date(2025,5,5), date(2025,5,5), uuid4(), uuid4(), 1)
    validate_sql_bindings(q, params, name="phase1 market state")
    q2, cp = _candidate_wave_query([{"column":"ret_5m_pct","operator":"gte","value":1.0}], "ret_5m_pct", 1.0)
    validate_sql_bindings(q2, (uuid4(), date(2025,5,5), date(2025,5,5), *cp), name="phase1 candidate wave")

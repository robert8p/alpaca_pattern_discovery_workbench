from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import StrategyEconomicsConfig
from app.strategy_economics import (
    _compound_pct,
    _effective_event_count,
    _expected_shortfall,
    _icc,
    _profit_factor,
    _quantile,
    _signal_query,
    _top_return_share,
    strategy_config_hash,
)


def _base_config(**overrides):
    payload = dict(
        candidate_id=uuid4(),
        target_feature_set_id=uuid4(),
        mode="research",
        research_stage="custom_presealed",
        start_date=date(2026, 6, 8),
        end_date=date(2026, 6, 30),
        round_trip_costs_bps=[20, 25, 30, 40],
        entry_delays_minutes=[0, 1, 2, 5],
    )
    payload.update(overrides)
    return StrategyEconomicsConfig(**payload)


def _candidate():
    return {
        "id": uuid4(),
        "family": "price_efficiency",
        "direction": "short",
        "holding_horizon_minutes": 30,
        "conditions": [
            {"column": "ret_5m_pct", "operator": "range", "low": -2.0, "high": -1.0},
            {"column": "activity_impact_change_ratio", "operator": "gte", "value": 2.0},
            {"column": "relative_volume_20bar", "operator": "lt", "value": 1.0},
            {"column": "volatility_vs_previous_day_ratio", "operator": "gte", "value": 1.0},
        ],
        "entry_sampling_mode": "non_overlapping",
        "entry_stride_minutes": 30,
        "entry_anchor_minute": 570,
        "rule_definition_version": "2026-08-coverage-pack1-v1",
    }


def test_strategy_model_preserves_cost_and_delay_stress_standards():
    cfg = _base_config()
    assert {20, 25, 30, 40}.issubset(set(cfg.round_trip_costs_bps))
    assert {0, 1, 2, 5}.issubset(set(cfg.entry_delays_minutes))
    with pytest.raises(ValueError, match="20, 25, 30 and 40"):
        _base_config(round_trip_costs_bps=[20, 30, 40])
    with pytest.raises(ValueError, match="0, 1, 2 and 5"):
        _base_config(entry_delays_minutes=[0, 1, 2])


def test_strategy_model_enforces_research_and_sealed_boundaries():
    with pytest.raises(ValueError, match="sealed holdout"):
        _base_config(end_date=date(2026, 8, 4))
    with pytest.raises(ValueError, match="strategy_config_hash"):
        _base_config(mode="sealed", research_stage="sealed_holdout", start_date=date(2026, 8, 4), end_date=date(2026, 8, 5))
    sealed = _base_config(
        mode="sealed", research_stage="sealed_holdout",
        start_date=date(2026, 8, 4), end_date=date(2026, 8, 5), strategy_config_hash="a"*64,
    )
    assert sealed.mode == "sealed"


def test_strategy_stage_calendar_is_explicit():
    _base_config(research_stage="research_confirmation", start_date=date(2026, 6, 1), end_date=date(2026, 8, 3))
    with pytest.raises(ValueError, match="Research-confirmation"):
        _base_config(research_stage="research_confirmation", start_date=date(2026, 5, 31), end_date=date(2026, 8, 3))


def test_sector_limit_cannot_use_current_metadata_as_historical_filter():
    with pytest.raises(ValueError, match="point-in-time sector metadata"):
        _base_config(max_sector_exposure_pct=25)


def test_strategy_hash_is_methodology_hash_not_period_hash():
    c = _candidate()
    a = _base_config(start_date=date(2026,6,8), end_date=date(2026,6,19))
    b = _base_config(candidate_id=a.candidate_id, target_feature_set_id=a.target_feature_set_id, start_date=date(2026,6,22), end_date=date(2026,6,30))
    assert strategy_config_hash(c, a) == strategy_config_hash(c, b)
    changed = b.model_copy(update={"position_size_pct_of_capital": 7.5})
    assert strategy_config_hash(c, b) != strategy_config_hash(c, changed)


def test_distribution_helpers_cover_tail_and_compounding():
    xs = [-4, -2, -1, 1, 2, 10]
    assert _quantile(xs, 0.5) == 0
    assert _expected_shortfall(xs, 0.05) == -4
    assert _profit_factor(xs) == pytest.approx(13/7)
    assert _compound_pct([10, -10]) == pytest.approx(-1.0)
    assert _top_return_share(xs, 10) is not None


def test_effective_event_count_penalises_clustered_signals():
    assert _effective_event_count([1,1,1,1]) == pytest.approx(4)
    assert _effective_event_count([4]) == pytest.approx(1)
    grouped = {"a": [1.0, 1.2, .8], "b": [-1.0, -.9, -1.1], "c": [.1, -.1, 0]}
    assert _icc(grouped) is not None


def test_h07_style_conditions_are_reconstructed_from_target_feature_set():
    c = _candidate()
    cfg = _base_config(candidate_id=c["id"])
    sql, params = _signal_query(c, cfg, 0, True)
    lower = sql.lower()
    assert "activity_impact_change_ratio" in lower
    assert "volatility_vs_previous_day_ratio" in lower
    assert "left join ra_intraday_features p" in lower
    assert "p.bar_ts=f.bar_ts-interval '5 minutes'" in lower
    assert "lead(" not in lower
    assert "ra_intraday_features en" in lower and "ra_intraday_features ex" in lower
    assert "rd_daily_features" not in lower
    assert "ra_feature_chunk_universes" in lower
    assert "pts.lookback_end < s.trade_date" in lower
    assert len(params) > 8


def test_whole_strategy_migration_has_immutable_sealed_methodology_guards():
    migration = (Path(__file__).resolve().parents[1] / "sql" / "migrations" / "2.7.0.sql").read_text().lower()
    for token in (
        "ra_strategy_economics_runs", "ra_strategy_trades", "ra_strategy_equity_points",
        "ra_strategy_daily_metrics", "ra_strategy_metric_sets", "ra_strategy_stress_results",
        "ra_strategy_regime_results", "strategy_configuration_hash", "strategy_freeze_timestamp",
        "frozen executable strategy methodology is immutable", "sealed whole-strategy evaluation requires the exact frozen strategy methodology",
    ):
        assert token in migration


def test_schema_startup_wires_point_in_time_and_strategy_migrations():
    source = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text()
    assert 'SCHEMA_VERSION = "2.7.0"' in source
    assert '_apply_v260_point_in_time_migration(cur)' in source
    assert '_apply_v261_point_in_time_hotfix(cur)' in source
    assert '_apply_v270_strategy_economics_migration(cur)' in source

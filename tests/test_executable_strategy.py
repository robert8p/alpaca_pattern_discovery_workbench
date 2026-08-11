from datetime import date
from pathlib import Path
from uuid import uuid4
import pytest
from app.models import StrategyEconomicsConfig
from app.executable_strategy import _compound_pct, _expected_shortfall, _effective_event_count, strategy_config_hash


def cfg(**kw):
    d=dict(candidate_id=uuid4(),target_feature_set_id=uuid4(),mode="research",research_stage="custom_presealed",start_date=date(2026,6,8),end_date=date(2026,6,30),round_trip_costs_bps=[20,25,30,40],entry_delays_minutes=[0,1,2,5])
    d.update(kw); return StrategyEconomicsConfig(**d)


def candidate():
    return {"id":uuid4(),"family":"price_efficiency","direction":"long","holding_horizon_minutes":30,"conditions":[{"column":"ret_5m_pct","operator":"gte","value":1}],"entry_sampling_mode":"non_overlapping","entry_stride_minutes":30,"entry_anchor_minute":570,"rule_definition_version":"test"}


def test_required_cost_and_delay_grids_are_hard_requirements():
    with pytest.raises(ValueError,match="20, 25, 30 and 40"): cfg(round_trip_costs_bps=[20,30,40])
    with pytest.raises(ValueError,match="0, 1, 2 and 5"): cfg(entry_delays_minutes=[0,1,2])


def test_sealed_period_requires_exact_frozen_hash():
    with pytest.raises(ValueError,match="strategy_config_hash"): cfg(mode="sealed",research_stage="sealed_holdout",start_date=date(2026,8,4),end_date=date(2026,8,5))


def test_methodology_hash_does_not_change_with_evaluation_period():
    c=candidate(); a=cfg(); b=cfg(candidate_id=a.candidate_id,target_feature_set_id=a.target_feature_set_id,start_date=date(2026,7,1),end_date=date(2026,7,20))
    assert strategy_config_hash(c,a)==strategy_config_hash(c,b)
    assert strategy_config_hash(c,b)!=strategy_config_hash(c,b.model_copy(update={"position_size_pct_of_capital":7.5}))


def test_tail_and_compounding_helpers():
    assert _expected_shortfall([-4,-2,-1,1,2,10],.05)==-4
    assert _compound_pct([10,-10])==pytest.approx(-1.0)
    assert _effective_event_count([4])==pytest.approx(1.0)
    assert _effective_event_count([1,1,1,1])==pytest.approx(4.0)


def test_migration_preserves_complete_strategy_and_sealed_guards():
    m=(Path(__file__).resolve().parents[1]/"sql/migrations/2.7.0.sql").read_text().lower()
    for token in ("ra_strategy_economics_runs","ra_strategy_trades","ra_strategy_equity_points","strategy_configuration_hash","strategy_freeze_timestamp","frozen executable strategy methodology is immutable","sealed whole-strategy evaluation requires the exact frozen strategy methodology"):
        assert token in m

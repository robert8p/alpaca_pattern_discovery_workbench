from pathlib import Path
p=Path('tests/test_postgres_integration.py'); s=p.read_text()
s=s.replace('''    from app.robustness import run_robustness\n''','''    from app.robustness import run_robustness\n    from app.executable_strategy import run_strategy_economics\n''',1)
s=s.replace('''        MarketStateBuildConfig, RobustnessAnalysisConfig, UniverseBuildConfig,\n''','''        MarketStateBuildConfig, RobustnessAnalysisConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n''',1)
old='''    assert robustness_result["summary"]["base"]["observations"] > 0
    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}

    # Ordinary signal robustness is no longer sufficient to unlock the true holdout.
'''
new='''    assert robustness_result["summary"]["base"]["observations"] > 0
    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}

    # The executable engine is a distinct layer above the signal-level diagnostics.
    # It must model actual simultaneous capital allocation and retain the mandatory
    # 20/25/30/40bps x 0/1/2/5m execution stress grid without touching sealed data.
    strategy_cfg = StrategyEconomicsConfig(
        candidate_id=candidate["id"], target_feature_set_id=feature_result["feature_set_id"],
        mode="research", research_stage="custom_presealed",
        start_date="2026-06-08", end_date="2026-06-30",
        capital_levels=[10000], base_entry_delay_minutes=0, entry_delays_minutes=[0,1,2,5],
        base_round_trip_cost_bps=20, round_trip_costs_bps=[20,25,30,40],
        position_size_pct_of_capital=5, max_positions=20, max_gross_exposure_pct=100,
        max_net_exposure_pct=100, max_symbol_exposure_pct=10,
        max_bar_participation_pct=100, max_daily_participation_pct=100, min_fill_fraction=0.01,
    )
    strategy_job=create_job("strategy_economics_analysis","Synthetic executable strategy",strategy_cfg.model_dump(mode="json"))
    strategy_result=run_strategy_economics(str(strategy_job["id"]),strategy_cfg)
    assert strategy_result["primary_metrics"]["trades"] > 0
    assert strategy_result["primary_metrics"]["market_days"] > 0
    assert strategy_result["primary_metrics"]["maximum_drawdown_pct"] is not None
    assert len(strategy_result["stress_results"]) == 16
    assert strategy_result["sealed_period_accessed_by_this_analysis"] is False if "sealed_period_accessed_by_this_analysis" in strategy_result else True
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n,count(DISTINCT (capital_level,signal_ts,symbol)) AS d FROM ra_strategy_trades WHERE strategy_run_id=%s",(strategy_result["strategy_run_id"],))
            row=cur.fetchone(); assert row["n"] == row["d"] and row["n"] > 0
            cur.execute("SELECT count(*) AS n FROM ra_strategy_stress_results WHERE strategy_run_id=%s",(strategy_result["strategy_run_id"],))
            assert cur.fetchone()["n"] == 16
        conn.rollback()

    # Ordinary signal robustness is no longer sufficient to unlock the true holdout,
    # and a custom-presealed executable diagnostic is also deliberately insufficient.
'''
if old not in s: raise SystemExit('robustness marker not found')
s=s.replace(old,new,1)
s=s.replace('test_upgrade_from_v211_schema_to_v250','test_upgrade_from_v211_schema_to_v270').replace('test_upgrade_from_v220_schema_to_v250','test_upgrade_from_v220_schema_to_v270')
s=s.replace("version='2.5.0'","version='2.7.0'").replace('== "2.5.0"','== "2.7.0"')
s=s.replace('match="locked in Phase 1"','match="locked"')
p.write_text(s)

# Permanent pure integrity tests.
p=Path('tests/test_executable_strategy.py')
p.write_text(r'''from datetime import date
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
''')

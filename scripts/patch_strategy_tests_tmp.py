from pathlib import Path

# Version expectations.
for rel in ('tests/test_candidate_export.py','tests/test_schema_startup.py'):
    p=Path(rel); s=p.read_text(); s=s.replace('2.5.0','2.7.0'); p.write_text(s)

# Strengthen startup migration assertions.
p=Path('tests/test_schema_startup.py'); s=p.read_text()
marker='''    assert 'sql" / "migrations" / "2.7.0.sql' in source\n'''
if marker not in s:
    old='''    assert 'sql" / "migrations" / "2.5.0.sql' in source\n'''
    if old in s:
        s=s.replace(old, old + '''    assert 'sql" / "migrations" / "2.6.0.sql' in source\n    assert 'sql" / "migrations" / "2.6.1.sql' in source\n    assert 'sql" / "migrations" / "2.7.0.sql' in source\n''',1)
p.write_text(s)

p=Path('tests/test_postgres_integration.py'); s=p.read_text()
s=s.replace('test_upgrade_from_v211_schema_to_v250','test_upgrade_from_v211_schema_to_v270')
s=s.replace('test_upgrade_from_v220_schema_to_v250','test_upgrade_from_v220_schema_to_v270')
s=s.replace("version='2.5.0'", "version='2.7.0'")
s=s.replace('== "2.5.0"','== "2.7.0"')
s=s.replace('match="locked in Phase 1"','match="locked"')
# Imports.
s=s.replace('''    from app.robustness import run_robustness\n''','''    from app.robustness import run_robustness\n    from app.strategy_economics import run_strategy_economics\n''',1)
s=s.replace('''        MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n''','''        MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n''',1)
# Insert whole-strategy integration after robustness.
old='''    assert robustness_result["summary"]["base"]["observations"] > 0\n    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}\n\n    freeze_candidate(candidate["id"], "Synthetic integration freeze")\n    sealed_config = SealedEvaluationConfig(\n        candidate_id=candidate["id"],\n        sealed_start="2026-08-04", sealed_end="2026-08-14",\n    )\n    sealed_job = create_job("sealed_evaluation", "Synthetic sealed", sealed_config.model_dump(mode="json"))\n    sealed_result = run_sealed_evaluation(str(sealed_job["id"]), sealed_config)\n    assert sealed_result["observations"] > 0\n    assert sealed_result["net_avg_pct"] > 0\n    close_pool()\n'''
new='''    assert robustness_result["summary"]["base"]["observations"] > 0\n    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}\n\n    strategy_cfg = StrategyEconomicsConfig(\n        name="Synthetic whole-strategy economics", candidate_id=candidate["id"],\n        target_feature_set_id=feature_result["feature_set_id"], mode="research", research_stage="custom_presealed",\n        start_date="2026-06-08", end_date="2026-06-30", capital_levels=[10000],\n        base_entry_delay_minutes=0, entry_delays_minutes=[0,1,2,5],\n        base_round_trip_cost_bps=20, round_trip_costs_bps=[20,25,30,40],\n        position_size_pct_of_capital=5, max_positions=20, max_gross_exposure_pct=100,\n        max_net_exposure_pct=100, max_symbol_exposure_pct=10, max_bar_participation_pct=5,\n        max_daily_participation_pct=1,\n    )\n    strategy_job=create_job("strategy_economics_analysis", strategy_cfg.name, strategy_cfg.model_dump(mode="json"))\n    strategy_result=run_strategy_economics(str(strategy_job["id"]), strategy_cfg)\n    assert strategy_result["primary_metrics"]["trades"] > 0\n    assert strategy_result["primary_metrics"]["market_days"] > 0\n    assert strategy_result["primary_metrics"]["maximum_drawdown_pct"] is not None\n    assert len(strategy_result["stress_results"]) == 16\n    with connection() as conn:\n        with conn.cursor() as cur:\n            cur.execute("SELECT count(*) n FROM ra_strategy_metric_sets WHERE strategy_run_id=%s",(strategy_result["strategy_run_id"],))\n            assert cur.fetchone()["n"] == 1\n            cur.execute("SELECT count(*) n,count(DISTINCT (capital_level,signal_ts,symbol)) d FROM ra_strategy_trades WHERE strategy_run_id=%s",(strategy_result["strategy_run_id"],))\n            row=cur.fetchone(); assert row["n"] == row["d"]\n        conn.rollback()\n\n    freeze_candidate(candidate["id"], "Synthetic integration freeze")\n    sealed_config = SealedEvaluationConfig(\n        candidate_id=candidate["id"], sealed_start="2026-08-04", sealed_end="2026-08-14",\n    )\n    with pytest.raises(Exception, match="strategy|executable|frozen"):\n        create_job("sealed_evaluation", "Synthetic sealed must remain blocked", sealed_config.model_dump(mode="json"))\n    close_pool()\n'''
if old not in s:
    raise SystemExit('postgres strategy insertion marker not found')
s=s.replace(old,new,1)
p.write_text(s)

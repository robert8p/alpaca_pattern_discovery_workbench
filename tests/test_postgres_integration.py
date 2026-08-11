from __future__ import annotations

import os
from datetime import date

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 with TEST_DATABASE_URL to run PostgreSQL integration tests",
)


def _reset_and_seed() -> None:
    import psycopg

    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute(
                """
                CREATE TABLE rd_assets (
                    symbol text PRIMARY KEY,
                    exchange text,
                    name text,
                    attributes jsonb,
                    raw jsonb
                );
                CREATE TABLE rd_bars (
                    symbol text NOT NULL,
                    bar_ts timestamptz NOT NULL,
                    timeframe text NOT NULL,
                    feed text NOT NULL,
                    adjustment text NOT NULL,
                    session_label text NOT NULL,
                    open double precision NOT NULL,
                    high double precision NOT NULL,
                    low double precision NOT NULL,
                    close double precision NOT NULL,
                    volume bigint NOT NULL,
                    trade_count bigint,
                    vwap double precision,
                    loaded_by_job_id uuid,
                    PRIMARY KEY(symbol,timeframe,feed,adjustment,bar_ts)
                );
                CREATE INDEX rd_bars_scan_idx
                    ON rd_bars(timeframe,feed,adjustment,session_label,bar_ts,symbol);
                INSERT INTO rd_assets(symbol,exchange,name) VALUES
                    ('AAA','NASDAQ','Alpha Operating Company'),
                    ('BBB','NYSE','Beta Operating Company'),
                    ('CCC','NASDAQ','Gamma Operating Company');
                """
            )
            cur.execute(
                """
                WITH trading_days AS (
                    SELECT d::date AS trade_date,
                           dense_rank() OVER (ORDER BY d)::integer AS day_number
                    FROM generate_series('2026-06-01'::date,'2026-08-14'::date,interval '1 day') d
                    WHERE extract(isodow FROM d) BETWEEN 1 AND 5
                ), symbols(symbol,base_price) AS (
                    VALUES ('AAA'::text,100.0::double precision),
                           ('BBB'::text,60.0::double precision),
                           ('CCC'::text,30.0::double precision),
                           ('SPY'::text,500.0::double precision),
                           ('QQQ'::text,450.0::double precision)
                ), bars AS (
                    SELECT s.symbol,t.trade_date,t.day_number,g.minute_number,
                        ((t.trade_date + time '09:30' + g.minute_number*interval '1 minute')
                            AT TIME ZONE 'America/New_York') AS bar_ts,
                        s.base_price + t.day_number*0.10 + g.minute_number*0.01 AS px
                    FROM trading_days t CROSS JOIN symbols s
                    CROSS JOIN generate_series(0,389) AS g(minute_number)
                )
                INSERT INTO rd_bars(
                    symbol,bar_ts,timeframe,feed,adjustment,session_label,
                    open,high,low,close,volume,trade_count,vwap
                )
                SELECT symbol,bar_ts,'1Min','sip','raw','regular',
                    px-0.005,px+0.015,px-0.015,px,
                    10000 + minute_number*10,100 + minute_number,px
                FROM bars
                """
            )


def test_complete_postgres_workflow():
    from app.config import get_settings
    from app.db import close_pool, connection, execute_schema
    from app.discovery import RULE_DEFINITION_VERSION, run_discovery, run_sealed_evaluation
    from app.full_history import (
        freeze_candidate, run_historical_feature_backfill, run_market_state_build, run_candidate_wave_build
    )
    from app.robustness import run_robustness
    from app.executable_strategy import run_strategy_economics
    from app.features import build_feature_set
    from app.jobs import create_job
    from app.models import (
        CandidateWaveBuildConfig, DiscoveryConfig, FeatureBuildConfig, HistoricalFeatureBackfillConfig,
        MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,
    )
    from app.preflight import database_sql_preflight
    from app.universe import build_universe

    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    close_pool()
    _reset_and_seed()
    execute_schema()

    universe_config = UniverseBuildConfig(
        name="Synthetic liquid universe",
        start_date="2026-06-01", end_date="2026-08-14",
        timeframe="1Min", feed="sip", adjustment="raw", session="regular",
        minimum_trading_days=10,
        minimum_average_bars_per_day=300,
        minimum_median_daily_dollar_volume=1_000_000,
        minimum_median_close=1,
        maximum_symbols=3,
        exclude_likely_funds=False,
    )
    universe_job = create_job("universe_build", universe_config.name, universe_config.model_dump(mode="json"))
    universe_result = build_universe(str(universe_job["id"]), universe_config)
    assert universe_result["included_symbols"] == 3

    feature_config = FeatureBuildConfig(
        name="Synthetic features",
        universe_run_id=universe_result["universe_run_id"],
        start_date="2026-06-01", end_date="2026-08-14",
        timeframe="1Min", feed="sip", adjustment="raw", session="regular",
        liquidity_tiers=["A", "B", "C", "D"],
        date_chunk_days=10,
        symbol_batch_size=10,
        time_of_day_baseline_days=5,
        predictor_horizons_minutes=[1,5,15,30,60],
        outcome_horizons_minutes=[5,15,30,60],
    )
    feature_job = create_job("feature_build", feature_config.name, feature_config.model_dump(mode="json"))
    feature_result = build_feature_set(str(feature_job["id"]), feature_config)
    assert feature_result["rows"] > 30_000
    assert feature_result["symbols"] == 3

    # Phase 1 one-day backfill plumbing uses the unchanged feature pipeline and is idempotent.
    one_day_cfg = HistoricalFeatureBackfillConfig(
        name="Synthetic one-day historical feature backfill",
        reference_feature_set_id=feature_result["feature_set_id"],
        start_date="2026-06-02", end_date="2026-06-02", scope="one_day_test",
    )
    one_day_job = create_job("historical_feature_backfill", one_day_cfg.name, one_day_cfg.model_dump(mode="json"))
    one_day_first = run_historical_feature_backfill(str(one_day_job["id"]), one_day_cfg)
    one_day_second = run_historical_feature_backfill(str(one_day_job["id"]), one_day_cfg)
    assert one_day_first["rows"] == one_day_second["rows"] > 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) rows, count(DISTINCT (symbol,bar_ts)) distinct_rows
                FROM ra_intraday_features WHERE feature_set_id=%s
                """,
                (one_day_first["feature_set_id"],),
            )
            dedupe = cur.fetchone()
            assert dedupe["rows"] == dedupe["distinct_rows"]
        conn.rollback()

    market_cfg = MarketStateBuildConfig(
        name="Synthetic market state", feature_set_id=feature_result["feature_set_id"],
        start_date="2026-06-08", end_date="2026-06-08", sample_stride_minutes=5,
    )
    market_job = create_job("market_state_build", market_cfg.name, market_cfg.model_dump(mode="json"))
    market_result = run_market_state_build(str(market_job["id"]), market_cfg)
    assert market_result["rows"] > 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) rows,count(DISTINCT bar_ts) unique_ts,
                       count(*) FILTER (WHERE spy_return_5m_pct IS NOT NULL) spy_ready,
                       count(*) FILTER (WHERE qqq_return_5m_pct IS NOT NULL) qqq_ready
                FROM ra_market_state_features WHERE market_state_run_id=%s
                """,
                (market_result["market_state_run_id"],),
            )
            ms = cur.fetchone()
            assert ms["rows"] == ms["unique_ts"]
            assert ms["spy_ready"] > 0 and ms["qqq_ready"] > 0
        conn.rollback()

    preflight = database_sql_preflight(force=True, exhaustive=True)
    assert preflight["ok"] is True
    assert preflight["database_plans"] == 772

    discovery_config = DiscoveryConfig(
        name="Synthetic audited discovery",
        feature_set_id=feature_result["feature_set_id"],
        discovery_start="2026-06-08", discovery_end="2026-06-19",
        validation_start="2026-06-22", validation_end="2026-06-30",
        directions=["long", "short"], holding_horizons_minutes=[5, 15, 30, 60],
        families=[
            "time_of_day", "oversold_reversal", "momentum_continuation",
            "vwap_reversion", "gap_behavior", "volume_shock",
            "dip_repair", "compression_expansion", "gap_state",
            "activity_absorption", "price_efficiency", "new_high_liquidity_divergence",
        ], round_trip_cost_bps=0,
        minimum_observations=20, minimum_symbols=2, minimum_dates=5,
        maximum_symbol_concentration_pct=100,
        maximum_date_concentration_pct=100,
        top_candidates_per_family=10,
        entry_sampling_mode="non_overlapping", date_chunk_days=14, symbol_shards=1,
    )
    discovery_job = create_job("discovery_scan", discovery_config.name, discovery_config.model_dump(mode="json"))
    discovery_result = run_discovery(str(discovery_job["id"]), discovery_config)
    assert discovery_result["candidates_tested"] > 0
    assert discovery_result["candidates_retained"] > 0

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,holding_horizon_minutes,entry_stride_minutes,entry_anchor_minute,rule_definition_version,
                       discovery_net_avg_pct,validation_net_avg_pct
                FROM ra_candidate_rules
                WHERE discovery_run_id=%s
                ORDER BY rank_score DESC LIMIT 1
                """,
                (discovery_result["discovery_run_id"],),
            )
            candidate = cur.fetchone()
        conn.rollback()
    assert candidate["entry_stride_minutes"] == candidate["holding_horizon_minutes"]
    assert candidate["entry_anchor_minute"] == 570
    assert candidate["rule_definition_version"] == RULE_DEFINITION_VERSION
    assert candidate["discovery_net_avg_pct"] > 0

    wave_cfg = CandidateWaveBuildConfig(
        name="Synthetic generic candidate wave", candidate_id=candidate["id"],
        start_date="2026-06-08", end_date="2026-06-08",
        signal_strength_field="ret_5m_pct", elevated_wave_threshold_pct=1.0,
    )
    wave_job = create_job("candidate_wave_build", wave_cfg.name, wave_cfg.model_dump(mode="json"))
    wave_result = run_candidate_wave_build(str(wave_job["id"]), wave_cfg)
    assert wave_result["rows"] > 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) rows,count(DISTINCT bar_ts) unique_ts FROM ra_candidate_wave_stats WHERE candidate_wave_run_id=%s",
                (wave_result["candidate_wave_run_id"],),
            )
            wave = cur.fetchone()
            assert wave["rows"] == wave["unique_ts"]
        conn.rollback()

    # Database-level guards remain effective even when application validation is bypassed.
    import psycopg
    from psycopg.types.json import Jsonb
    with connection() as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error, match="sealed holdout"):
                cur.execute(
                    "INSERT INTO ra_jobs(job_type,name,config) VALUES ('discovery_scan','illegal sealed discovery',%s)",
                    (Jsonb({"discovery_end":"2026-08-03","validation_end":"2026-08-04"}),),
                )
        conn.rollback()
    with pytest.raises(Exception, match="locked"):
        create_job(
            "historical_feature_backfill", "illegal full history",
            {**one_day_cfg.model_dump(mode="json"), "scope":"full_history"},
        )
    with pytest.raises(Exception, match="frozen"):
        create_job(
            "sealed_evaluation", "illegal unfrozen sealed",
            {"candidate_id":str(candidate["id"]),"sealed_start":"2026-08-04","sealed_end":"2026-08-05"},
        )

    robustness_config = RobustnessAnalysisConfig(
        candidate_id=candidate["id"], mode="development",
        start_date="2026-06-08", end_date="2026-06-30",
        round_trip_costs_bps=[0,10,20,30], entry_delays_minutes=[0,1],
    )
    robustness_job = create_job("robustness_analysis", "Synthetic robustness", robustness_config.model_dump(mode="json"))
    robustness_result = run_robustness(str(robustness_job["id"]), robustness_config)
    assert robustness_result["summary"]["base"]["observations"] > 0
    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}

    strategy_cfg=StrategyEconomicsConfig(
        candidate_id=candidate["id"],target_feature_set_id=feature_result["feature_set_id"],
        mode="research",research_stage="custom_presealed",start_date="2026-06-08",end_date="2026-06-30",
        capital_levels=[10000],entry_delays_minutes=[0,1,2,5],round_trip_costs_bps=[20,25,30,40],
        max_bar_participation_pct=100,max_daily_participation_pct=100,min_fill_fraction=0.01,
    )
    sj=create_job("strategy_economics_analysis","Synthetic executable strategy",strategy_cfg.model_dump(mode="json"))
    sr=run_strategy_economics(str(sj["id"]),strategy_cfg)
    assert sr["primary_metrics"]["trades"] > 0
    assert sr["primary_metrics"]["maximum_drawdown_pct"] is not None
    assert len(sr["stress_results"]) == 16

    freeze_candidate(candidate["id"], "Synthetic signal freeze")
    sealed_config = SealedEvaluationConfig(candidate_id=candidate["id"],sealed_start="2026-08-04",sealed_end="2026-08-14")
    with pytest.raises(Exception, match="strategy|executable|frozen"):
        create_job("sealed_evaluation", "Synthetic sealed remains locked", sealed_config.model_dump(mode="json"))
    close_pool()


def test_upgrade_from_v211_schema_to_v270():
    """Exercise the real production upgrade path from the last shipped schema."""
    import psycopg
    from pathlib import Path
    from app.config import get_settings
    from app.db import close_pool, execute_schema, connection

    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    close_pool()
    url = os.environ["TEST_DATABASE_URL"]
    legacy_schema = (Path(__file__).resolve().parent / "fixtures" / "schema_v2.1.1.sql").read_text(encoding="utf-8")
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute(legacy_schema)
    execute_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT app_version FROM ra_schema_versions WHERE version='2.7.0'")
            assert cur.fetchone()["app_version"] == "2.7.0"
            cur.execute("SELECT to_regclass('public.ra_robustness_runs') AS runs,to_regclass('public.ra_robustness_observations') AS observations,to_regclass('public.ra_robustness_results') AS results")
            row=cur.fetchone()
            assert row['runs'] and row['observations'] and row['results']
            cur.execute("SELECT defined_variant_count,campaign_definition_version FROM ra_discovery_runs LIMIT 0")
            cur.execute("SELECT variants_defined_campaign,multiple_testing_adjusted_p,sealed_feature_set_id FROM ra_candidate_rules LIMIT 0")
            cur.execute("SELECT relative_trade_count_20bar,activity_impact_change_ratio,opening_range_position,touched_session_high FROM ra_discovery_samples LIMIT 0")
        conn.rollback()
    close_pool()


def test_upgrade_from_v220_schema_to_v270():
    import psycopg
    from pathlib import Path
    from app.config import get_settings
    from app.db import close_pool, execute_schema, connection

    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    close_pool()
    url = os.environ["TEST_DATABASE_URL"]
    legacy_schema = (Path(__file__).resolve().parent / "fixtures" / "schema_v2.2.0.sql").read_text(encoding="utf-8")
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute(legacy_schema)
    execute_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT app_version FROM ra_schema_versions WHERE version='2.7.0'")
            assert cur.fetchone()["app_version"] == "2.7.0"
            cur.execute("SELECT to_regclass('public.ra_robustness_chunks') AS chunks,to_regclass('public.ra_robustness_samples') AS samples")
            row=cur.fetchone()
            assert row["chunks"] and row["samples"]
            cur.execute("SELECT engine_version FROM ra_robustness_runs LIMIT 0")
        conn.rollback()
    close_pool()

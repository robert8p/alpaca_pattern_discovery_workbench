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
                    name text
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
                    FROM generate_series('2026-06-01'::date,'2026-07-10'::date,interval '1 day') d
                    WHERE extract(isodow FROM d) BETWEEN 1 AND 5
                ), symbols(symbol,base_price) AS (
                    VALUES ('AAA'::text,100.0::double precision),
                           ('BBB'::text,60.0::double precision),
                           ('CCC'::text,30.0::double precision)
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
    from app.db import close_pool, execute_schema
    from app.discovery import RULE_DEFINITION_VERSION, run_discovery, run_sealed_evaluation
    from app.features import build_feature_set
    from app.jobs import create_job
    from app.models import DiscoveryConfig, FeatureBuildConfig, SealedEvaluationConfig, UniverseBuildConfig
    from app.preflight import database_sql_preflight
    from app.universe import build_universe

    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    close_pool()
    _reset_and_seed()
    execute_schema()

    universe_config = UniverseBuildConfig(
        name="Synthetic liquid universe",
        start_date="2026-06-01", end_date="2026-07-10",
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
        start_date="2026-06-01", end_date="2026-07-10",
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

    preflight = database_sql_preflight(force=True, exhaustive=True)
    assert preflight["ok"] is True
    assert preflight["database_plans"] == 194

    discovery_config = DiscoveryConfig(
        name="Synthetic audited discovery",
        feature_set_id=feature_result["feature_set_id"],
        discovery_start="2026-06-08", discovery_end="2026-06-19",
        validation_start="2026-06-22", validation_end="2026-06-30",
        directions=["long"], holding_horizons_minutes=[5],
        families=["time_of_day"], round_trip_cost_bps=0,
        minimum_observations=20, minimum_symbols=2, minimum_dates=5,
        maximum_symbol_concentration_pct=100,
        maximum_date_concentration_pct=100,
        top_candidates_per_family=10,
        entry_sampling_mode="non_overlapping",
    )
    discovery_job = create_job("discovery_scan", discovery_config.name, discovery_config.model_dump(mode="json"))
    discovery_result = run_discovery(str(discovery_job["id"]), discovery_config)
    assert discovery_result["candidates_tested"] > 0
    assert discovery_result["candidates_retained"] > 0

    from app.db import connection
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,entry_stride_minutes,entry_anchor_minute,rule_definition_version,
                       discovery_net_avg_pct,validation_net_avg_pct
                FROM ra_candidate_rules
                WHERE discovery_run_id=%s
                ORDER BY rank_score DESC LIMIT 1
                """,
                (discovery_result["discovery_run_id"],),
            )
            candidate = cur.fetchone()
        conn.rollback()
    assert candidate["entry_stride_minutes"] == 5
    assert candidate["entry_anchor_minute"] == 570
    assert candidate["rule_definition_version"] == RULE_DEFINITION_VERSION
    assert candidate["discovery_net_avg_pct"] > 0

    sealed_config = SealedEvaluationConfig(
        candidate_id=candidate["id"],
        sealed_start="2026-07-01", sealed_end="2026-07-10",
    )
    sealed_job = create_job("sealed_evaluation", "Synthetic sealed", sealed_config.model_dump(mode="json"))
    sealed_result = run_sealed_evaluation(str(sealed_job["id"]), sealed_config)
    assert sealed_result["observations"] > 0
    assert sealed_result["net_avg_pct"] > 0
    close_pool()

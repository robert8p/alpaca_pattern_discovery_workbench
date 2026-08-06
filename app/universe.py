from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.jobs import add_event, set_progress
from app.models import UniverseBuildConfig
from app.sql_validation import validate_sql_bindings
from app.utils import json_safe, market_date_bounds

FUND_NAME_REGEX = r"(ETF|ETN|FUND|TRUST|INDEX|PORTFOLIO|ISHARES|SPDR|VANGUARD|INVESCO|DIREXION|PROSHARES|WISDOMTREE|GLOBAL X|FIRST TRUST)"


def _universe_sql(config: UniverseBuildConfig, run_id: UUID | str) -> tuple[str, tuple[Any, ...]]:
    """Build and validate the complete liquidity-ranked universe statement.

    Keeping query generation separate from execution makes the exact production
    statement available to the local and PostgreSQL-backed release preflight.
    """
    max_symbols = config.maximum_symbols or 2_147_483_647
    start_ts, end_ts = market_date_bounds(config.start_date, config.end_date)
    sql = """
        WITH daily AS (
            SELECT b.symbol,
                (b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                count(*)::integer AS bar_count,
                sum(b.close*b.volume)::double precision AS daily_dollar_volume,
                (array_agg(b.close ORDER BY b.bar_ts DESC))[1]::double precision AS day_close
            FROM rd_bars b
            WHERE b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
              AND (%s='all' OR b.session_label=%s)
              AND b.bar_ts >= %s AND b.bar_ts < %s
            GROUP BY b.symbol,2
        ), stats AS (
            SELECT symbol,count(*)::integer AS trading_days,
                avg(bar_count)::double precision AS average_bars_per_day,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY daily_dollar_volume)::double precision AS median_daily_dollar_volume,
                avg(daily_dollar_volume)::double precision AS average_daily_dollar_volume,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY day_close)::double precision AS median_close
            FROM daily GROUP BY symbol
        ), enriched AS (
            SELECT s.*,a.exchange,a.name AS asset_name,
                CASE
                    WHEN s.average_bars_per_day >= %s AND s.median_daily_dollar_volume >= %s THEN 'A'
                    WHEN s.average_bars_per_day >= %s AND s.median_daily_dollar_volume >= %s THEN 'B'
                    WHEN s.average_bars_per_day >= %s AND s.median_daily_dollar_volume >= %s THEN 'C'
                    ELSE 'D' END AS liquidity_tier,
                CASE
                    WHEN s.trading_days < %s THEN 'insufficient trading days'
                    WHEN s.average_bars_per_day < %s THEN 'insufficient bars per day'
                    WHEN s.median_daily_dollar_volume < %s THEN 'insufficient dollar volume'
                    WHEN s.median_close < %s THEN 'price below minimum'
                    WHEN %s::text IS NOT NULL AND NOT (s.symbol ~ %s::text) THEN 'does not match include regex'
                    WHEN %s::text IS NOT NULL AND s.symbol ~ %s::text THEN 'matches exclude regex'
                    WHEN %s AND COALESCE(a.name,'') ~* %s THEN 'likely fund or exchange-traded product'
                    ELSE NULL END AS exclusion_reason
            FROM stats s
            LEFT JOIN rd_assets a ON a.symbol=s.symbol
        ), ranked AS (
            SELECT *,
                row_number() OVER (
                    ORDER BY CASE WHEN exclusion_reason IS NULL THEN 0 ELSE 1 END,
                             median_daily_dollar_volume DESC NULLS LAST,symbol
                )::integer AS overall_rank,
                CASE WHEN exclusion_reason IS NULL THEN
                    row_number() OVER (
                        PARTITION BY (exclusion_reason IS NULL)
                        ORDER BY median_daily_dollar_volume DESC NULLS LAST,symbol
                    )::integer
                END AS eligible_rank
            FROM enriched
        )
        INSERT INTO ra_analysis_universe(
            universe_run_id,symbol,exchange,asset_name,trading_days,average_bars_per_day,
            median_daily_dollar_volume,average_daily_dollar_volume,median_close,
            liquidity_tier,included,rank_by_liquidity,exclusion_reason
        )
        SELECT %s,symbol,exchange,asset_name,trading_days,average_bars_per_day,
            median_daily_dollar_volume,average_daily_dollar_volume,median_close,
            liquidity_tier,
            (exclusion_reason IS NULL AND eligible_rank <= %s) AS included,
            eligible_rank,
            CASE WHEN exclusion_reason IS NULL AND eligible_rank > %s THEN 'outside maximum-symbol limit'
                 ELSE exclusion_reason END
        FROM ranked
    """
    params: tuple[Any, ...] = (
        config.timeframe, config.feed, config.adjustment, config.session, config.session,
        start_ts, end_ts,
        config.tier_a_min_bars_per_day, config.tier_a_min_dollar_volume,
        config.tier_b_min_bars_per_day, config.tier_b_min_dollar_volume,
        config.tier_c_min_bars_per_day, config.tier_c_min_dollar_volume,
        config.minimum_trading_days, config.minimum_average_bars_per_day,
        config.minimum_median_daily_dollar_volume, config.minimum_median_close,
        config.include_symbol_regex, config.include_symbol_regex,
        config.exclude_symbol_regex, config.exclude_symbol_regex,
        config.exclude_likely_funds, FUND_NAME_REGEX,
        run_id, max_symbols, max_symbols,
    )
    validate_sql_bindings(sql, params, name="universe-build SQL")
    return sql, params


def build_universe(job_id: str, config: UniverseBuildConfig) -> dict[str, Any]:
    timeout = get_settings().database_statement_timeout_seconds
    set_progress(job_id, "creating universe run", 0, 4)
    with connection() as conn:
        with conn.cursor() as cur:
            source_config = Jsonb({
                "start_date": config.start_date.isoformat(), "end_date": config.end_date.isoformat(),
                "timeframe": config.timeframe, "feed": config.feed,
                "adjustment": config.adjustment, "session": config.session,
            })
            cur.execute("SELECT id FROM ra_universe_runs WHERE job_id=%s", (job_id,))
            existing = cur.fetchone()
            if existing:
                run_id = existing["id"]
                cur.execute("DELETE FROM ra_analysis_universe WHERE universe_run_id=%s", (run_id,))
                cur.execute(
                    "UPDATE ra_universe_runs SET name=%s,source_config=%s,selection_config=%s,completed_at=NULL WHERE id=%s",
                    (config.name, source_config, Jsonb(config.model_dump(mode="json")), run_id),
                )
            else:
                cur.execute(
                    "INSERT INTO ra_universe_runs(job_id,name,source_config,selection_config) VALUES (%s,%s,%s,%s) RETURNING id",
                    (job_id, config.name, source_config, Jsonb(config.model_dump(mode="json"))),
                )
                run_id = cur.fetchone()["id"]
        conn.commit()

    set_progress(job_id, "aggregating liquidity", 1, 4, result={"universe_run_id": run_id})
    universe_sql, universe_params = _universe_sql(config, run_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(universe_sql, universe_params)
            rows = cur.rowcount or 0
        conn.commit()

    set_progress(job_id, "summarising universe", 3, 4)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS total_symbols,
                    count(*) FILTER (WHERE included) AS included_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='A') AS tier_a_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='B') AS tier_b_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='C') AS tier_c_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='D') AS tier_d_symbols
                FROM ra_analysis_universe WHERE universe_run_id=%s
                """,
                (run_id,),
            )
            summary = cur.fetchone()
            cur.execute(
                """
                UPDATE ra_universe_runs SET total_symbols=%s,included_symbols=%s,
                    tier_a_symbols=%s,tier_b_symbols=%s,tier_c_symbols=%s,tier_d_symbols=%s,
                    completed_at=now() WHERE id=%s
                """,
                (
                    summary["total_symbols"],summary["included_symbols"],summary["tier_a_symbols"],
                    summary["tier_b_symbols"],summary["tier_c_symbols"],summary["tier_d_symbols"],run_id,
                ),
            )
        conn.commit()
    result = {"universe_run_id": run_id, "rows_assessed": rows, **dict(summary)}
    add_event(job_id, "universe_created", f"Universe built with {summary['included_symbols']:,} included symbols.", details=result)
    set_progress(job_id, "complete", 4, 4, result=result)
    return json_safe(result)

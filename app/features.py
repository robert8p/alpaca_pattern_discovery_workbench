from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.jobs import add_event, check_control, set_progress
from app.models import FeatureBuildConfig, timeframe_minutes
from app.utils import json_safe

FIXED_HORIZONS = (1, 5, 15, 30, 60)


def date_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def estimate_feature_build(config: FeatureBuildConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS symbols
                FROM ra_analysis_universe
                WHERE universe_run_id=%s AND included AND liquidity_tier=ANY(%s)
                """,
                (config.universe_run_id, config.liquidity_tiers),
            )
            symbols = int(cur.fetchone()["symbols"])
            cur.execute(
                """
                SELECT count(*) AS bars
                FROM rd_bars b
                JOIN ra_analysis_universe u ON u.symbol=b.symbol
                WHERE u.universe_run_id=%s AND u.included AND u.liquidity_tier=ANY(%s)
                  AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                  AND (%s='all' OR b.session_label=%s)
                  AND (b.bar_ts AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                """,
                (
                    config.universe_run_id, config.liquidity_tiers,
                    config.timeframe, config.feed, config.adjustment,
                    config.session, config.session, config.start_date, config.end_date,
                ),
            )
            bars = int(cur.fetchone()["bars"])
        conn.rollback()
    chunks = date_chunks(config.start_date, config.end_date, config.date_chunk_days)
    return {
        "symbols": symbols,
        "estimated_feature_rows": bars,
        "chunks": len(chunks),
        "estimated_table_bytes": bars * 420,
    }


def _ensure_feature_set(job_id: str, config: FeatureBuildConfig) -> tuple[str, list[dict[str, Any]]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_feature_sets WHERE job_id=%s", (job_id,))
            existing = cur.fetchone()
            if existing:
                feature_set_id = existing["id"]
                cur.execute("UPDATE ra_feature_sets SET status='building',completed_at=NULL WHERE id=%s", (feature_set_id,))
                cur.execute(
                    "UPDATE ra_feature_chunks SET status='pending',error=NULL WHERE feature_set_id=%s AND status IN ('running','failed','cancelled')",
                    (feature_set_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT count(*) AS symbols FROM ra_analysis_universe
                    WHERE universe_run_id=%s AND included AND liquidity_tier=ANY(%s)
                    """,
                    (config.universe_run_id, config.liquidity_tiers),
                )
                symbol_count = int(cur.fetchone()["symbols"])
                if not symbol_count:
                    raise RuntimeError("The selected universe and tiers contain no included symbols")
                cur.execute(
                    """
                    INSERT INTO ra_feature_sets(job_id,universe_run_id,name,config,symbol_count,min_trade_date,max_trade_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (
                        job_id, config.universe_run_id, config.name,
                        Jsonb(config.model_dump(mode="json")), symbol_count,
                        config.start_date, config.end_date,
                    ),
                )
                feature_set_id = cur.fetchone()["id"]
                cur.executemany(
                    "INSERT INTO ra_feature_chunks(feature_set_id,chunk_start,chunk_end) VALUES (%s,%s,%s)",
                    [(feature_set_id, start, end) for start, end in date_chunks(config.start_date, config.end_date, config.date_chunk_days)],
                )
            cur.execute(
                "SELECT * FROM ra_feature_chunks WHERE feature_set_id=%s ORDER BY chunk_start",
                (feature_set_id,),
            )
            chunks = cur.fetchall()
        conn.commit()
    return str(feature_set_id), [dict(row) for row in chunks]


def _lag_columns(base_minutes: int, predictors: set[int], outcomes: set[int]) -> tuple[str, str, str]:
    all_needed = sorted((predictors | outcomes | {30}) & set(FIXED_HORIZONS))
    window_parts: list[str] = []
    derived_parts: list[str] = []
    insert_select_parts: list[str] = []

    for horizon in FIXED_HORIZONS:
        supported = horizon % base_minutes == 0
        steps = horizon // base_minutes if supported else 0
        if supported and horizon in all_needed:
            window_parts.extend([
                f"lag(bar_ts,{steps}) OVER w_ord AS ts_lag_{horizon}",
                f"lag(close,{steps}) OVER w_ord AS close_lag_{horizon}",
                f"lead(bar_ts,{steps}) OVER w_ord AS ts_lead_{horizon}",
                f"lead(close,{steps}) OVER w_ord AS close_lead_{horizon}",
            ])
        else:
            window_parts.extend([
                f"NULL::timestamptz AS ts_lag_{horizon}",
                f"NULL::double precision AS close_lag_{horizon}",
                f"NULL::timestamptz AS ts_lead_{horizon}",
                f"NULL::double precision AS close_lead_{horizon}",
            ])

        if supported and horizon in predictors:
            derived_parts.append(
                f"CASE WHEN ts_lag_{horizon} IS NOT NULL AND bar_ts-ts_lag_{horizon}=interval '{horizon} minutes' "
                f"THEN (close/NULLIF(close_lag_{horizon},0)-1)*100 END AS ret_{horizon}m_pct"
            )
        else:
            derived_parts.append(f"NULL::double precision AS ret_{horizon}m_pct")

        if supported and horizon in outcomes:
            derived_parts.append(
                f"CASE WHEN ts_lead_{horizon} IS NOT NULL AND ts_lead_{horizon}-bar_ts=interval '{horizon} minutes' "
                f"THEN (close_lead_{horizon}/NULLIF(close,0)-1)*100 END AS fwd_return_{horizon}m_pct"
            )
        else:
            derived_parts.append(f"NULL::double precision AS fwd_return_{horizon}m_pct")

        insert_select_parts.extend([
            f"d.ret_{horizon}m_pct",
        ])

    return ",\n                    ".join(window_parts), ",\n                    ".join(derived_parts), ",".join(insert_select_parts)


def _feature_sql(config: FeatureBuildConfig, chunk_start: date, chunk_end: date) -> tuple[str, tuple[Any, ...]]:
    base_minutes = timeframe_minutes(config.timeframe)
    predictors = set(config.predictor_horizons_minutes)
    outcomes = set(config.outcome_horizons_minutes)
    window_lags, derived_returns, _ = _lag_columns(base_minutes, predictors, outcomes)
    step_30 = 30 // base_minutes if 30 % base_minutes == 0 else 0
    if step_30 and 30 in outcomes:
        future_ranges = (
            f"max(high) OVER (w_ord ROWS BETWEEN 1 FOLLOWING AND {step_30} FOLLOWING) AS future_high_30,"
            f"min(low) OVER (w_ord ROWS BETWEEN 1 FOLLOWING AND {step_30} FOLLOWING) AS future_low_30"
        )
        mfe = "CASE WHEN d.ts_lead_30 IS NOT NULL AND d.ts_lead_30-d.bar_ts=interval '30 minutes' THEN (d.future_high_30/NULLIF(d.close,0)-1)*100 END"
        mae = "CASE WHEN d.ts_lead_30 IS NOT NULL AND d.ts_lead_30-d.bar_ts=interval '30 minutes' THEN (d.future_low_30/NULLIF(d.close,0)-1)*100 END"
    else:
        future_ranges = "NULL::double precision AS future_high_30,NULL::double precision AS future_low_30"
        mfe = "NULL::double precision"
        mae = "NULL::double precision"

    lookback_start = chunk_start - timedelta(days=config.time_of_day_baseline_days * 2 + 10)
    conflict = "DO NOTHING" if config.conflict_policy == "skip_existing" else "DO UPDATE SET built_at=excluded.built_at"

    sql = f"""
        WITH source AS (
            SELECT b.*,u.liquidity_tier,
                (b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                (extract(hour FROM b.bar_ts AT TIME ZONE 'America/New_York')::integer*60
                 + extract(minute FROM b.bar_ts AT TIME ZONE 'America/New_York')::integer)::smallint AS minute_of_day,
                extract(isodow FROM b.bar_ts AT TIME ZONE 'America/New_York')::smallint AS weekday_iso
            FROM rd_bars b
            JOIN ra_analysis_universe u ON u.symbol=b.symbol
            WHERE u.universe_run_id=%s AND u.included AND u.liquidity_tier=ANY(%s)
              AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
              AND (%s='all' OR b.session_label=%s)
              AND (b.bar_ts AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        ), windowed AS (
            SELECT s.*,
                (row_number() OVER w_ord)::integer AS session_bar_number,
                lag(bar_ts,1) OVER w_ord AS previous_bar_ts,
                lag(close,1) OVER w_ord AS previous_bar_close,
                {window_lags},
                first_value(open) OVER (w_ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS session_open,
                max(high) OVER (w_ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_high,
                min(low) OVER (w_ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_low,
                sum(COALESCE(vwap,close)*volume) OVER (w_ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_vwap_numerator,
                sum(volume) OVER (w_ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_volume,
                avg(volume) OVER (w_ord ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS previous_20bar_avg_volume,
                avg(trade_count) OVER (w_ord ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS previous_20bar_avg_trade_count,
                max(high) OVER (w_ord ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS rolling_high_30,
                min(low) OVER (w_ord ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS rolling_low_30,
                {future_ranges}
            FROM source s
            WINDOW w_ord AS (
                PARTITION BY symbol,trade_date,session_label ORDER BY bar_ts
            )
        ), derived AS (
            SELECT w.*,
                CASE WHEN previous_bar_ts IS NOT NULL AND bar_ts-previous_bar_ts=interval '{base_minutes} minutes'
                     THEN ln(close/NULLIF(previous_bar_close,0)) END AS bar_log_return,
                CASE WHEN previous_bar_ts IS NOT NULL AND bar_ts-previous_bar_ts=interval '{base_minutes} minutes'
                     THEN (close/NULLIF(previous_bar_close,0)-1)*100 END AS bar_return_pct,
                CASE WHEN previous_bar_ts IS NOT NULL THEN extract(epoch FROM bar_ts-previous_bar_ts)::integer END AS prior_bar_gap_seconds,
                {derived_returns}
            FROM windowed w
        ), rolled AS (
            SELECT d.*,
                stddev_samp(bar_log_return) OVER (
                    PARTITION BY symbol,trade_date,session_label ORDER BY bar_ts
                    ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
                ) AS rolling_realised_volatility_30bar
            FROM derived d
        ), baselined AS (
            SELECT r.*,
                avg(volume) OVER (
                    PARTITION BY symbol,session_label,minute_of_day ORDER BY trade_date
                    ROWS BETWEEN {config.time_of_day_baseline_days} PRECEDING AND 1 PRECEDING
                ) AS same_minute_avg_volume_prior_days,
                avg(abs(bar_return_pct)) OVER (
                    PARTITION BY symbol,session_label,minute_of_day ORDER BY trade_date
                    ROWS BETWEEN {config.time_of_day_baseline_days} PRECEDING AND 1 PRECEDING
                ) AS same_minute_avg_abs_return_prior_days
            FROM rolled r
        ), daily_raw AS (
            SELECT b.symbol,(b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                (array_agg(b.open ORDER BY b.bar_ts))[1] AS day_open,
                max(b.high) AS day_high,min(b.low) AS day_low,
                (array_agg(b.close ORDER BY b.bar_ts DESC))[1] AS day_close,
                sum(b.volume)::bigint AS day_volume,
                stddev_samp(ln(b.close/NULLIF(lag_close,0))) AS realised_volatility
            FROM (
                SELECT x.*,lag(x.close) OVER (
                    PARTITION BY x.symbol,(x.bar_ts AT TIME ZONE 'America/New_York')::date ORDER BY x.bar_ts
                ) AS lag_close
                FROM rd_bars x
                JOIN ra_analysis_universe u ON u.symbol=x.symbol
                WHERE u.universe_run_id=%s AND u.included AND u.liquidity_tier=ANY(%s)
                  AND x.timeframe=%s AND x.feed=%s AND x.adjustment=%s AND x.session_label='regular'
                  AND (x.bar_ts AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
            ) b
            GROUP BY b.symbol,(b.bar_ts AT TIME ZONE 'America/New_York')::date
        ), daily_context AS (
            SELECT *,
                lag(day_close) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_regular_close,
                lag(CASE WHEN day_open<>0 THEN (day_close/day_open-1)*100 END) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_day_return_pct,
                lag(CASE WHEN day_open<>0 THEN (day_high-day_low)/day_open*100 END) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_day_range_pct,
                lag(day_volume) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_day_volume,
                lag(realised_volatility) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_day_realised_volatility
            FROM daily_raw
        )
        INSERT INTO ra_intraday_features(
            feature_set_id,symbol,bar_ts,trade_date,minute_of_day,weekday_iso,session_label,
            session_bar_number,liquidity_tier,open,high,low,close,volume,trade_count,bar_vwap,
            bar_dollar_volume,prior_bar_gap_seconds,
            ret_1m_pct,ret_5m_pct,ret_15m_pct,ret_30m_pct,ret_60m_pct,
            session_open,ret_from_session_open_pct,cumulative_high,cumulative_low,cumulative_vwap,
            distance_from_cumulative_vwap_pct,cumulative_range_position,
            previous_20bar_avg_volume,relative_volume_20bar,previous_20bar_avg_trade_count,
            relative_trade_count_20bar,rolling_realised_volatility_30bar,rolling_range_30bar_pct,
            same_minute_avg_volume_prior_days,same_minute_relative_volume,same_minute_avg_abs_return_prior_days,
            previous_regular_close,gap_from_previous_regular_close_pct,previous_day_return_pct,
            previous_day_range_pct,previous_day_volume,previous_day_realised_volatility,
            history_5m_complete,history_15m_complete,history_30m_complete,history_60m_complete,
            future_5m_complete,future_15m_complete,future_30m_complete,future_60m_complete,
            fwd_return_5m_pct,fwd_return_15m_pct,fwd_return_30m_pct,fwd_return_60m_pct,
            fwd_mfe_30m_pct,fwd_mae_30m_pct,built_at
        )
        SELECT
            %s,d.symbol,d.bar_ts,d.trade_date,d.minute_of_day,d.weekday_iso,d.session_label,
            d.session_bar_number,d.liquidity_tier,d.open,d.high,d.low,d.close,d.volume,d.trade_count,d.vwap,
            d.close*d.volume,d.prior_bar_gap_seconds,
            d.ret_1m_pct,d.ret_5m_pct,d.ret_15m_pct,d.ret_30m_pct,d.ret_60m_pct,
            d.session_open,(d.close/NULLIF(d.session_open,0)-1)*100,d.cumulative_high,d.cumulative_low,
            d.cumulative_vwap_numerator/NULLIF(d.cumulative_volume,0),
            (d.close/NULLIF(d.cumulative_vwap_numerator/NULLIF(d.cumulative_volume,0),0)-1)*100,
            (d.close-d.cumulative_low)/NULLIF(d.cumulative_high-d.cumulative_low,0),
            d.previous_20bar_avg_volume,d.volume/NULLIF(d.previous_20bar_avg_volume,0),
            d.previous_20bar_avg_trade_count,d.trade_count/NULLIF(d.previous_20bar_avg_trade_count,0),
            d.rolling_realised_volatility_30bar,(d.rolling_high_30-d.rolling_low_30)/NULLIF(d.close,0)*100,
            d.same_minute_avg_volume_prior_days,d.volume/NULLIF(d.same_minute_avg_volume_prior_days,0),
            d.same_minute_avg_abs_return_prior_days,
            c.previous_regular_close,(d.session_open/NULLIF(c.previous_regular_close,0)-1)*100,
            c.previous_day_return_pct,c.previous_day_range_pct,c.previous_day_volume,c.previous_day_realised_volatility,
            (d.ts_lag_5 IS NOT NULL AND d.bar_ts-d.ts_lag_5=interval '5 minutes'),
            (d.ts_lag_15 IS NOT NULL AND d.bar_ts-d.ts_lag_15=interval '15 minutes'),
            (d.ts_lag_30 IS NOT NULL AND d.bar_ts-d.ts_lag_30=interval '30 minutes'),
            (d.ts_lag_60 IS NOT NULL AND d.bar_ts-d.ts_lag_60=interval '60 minutes'),
            (d.ts_lead_5 IS NOT NULL AND d.ts_lead_5-d.bar_ts=interval '5 minutes'),
            (d.ts_lead_15 IS NOT NULL AND d.ts_lead_15-d.bar_ts=interval '15 minutes'),
            (d.ts_lead_30 IS NOT NULL AND d.ts_lead_30-d.bar_ts=interval '30 minutes'),
            (d.ts_lead_60 IS NOT NULL AND d.ts_lead_60-d.bar_ts=interval '60 minutes'),
            d.fwd_return_5m_pct,d.fwd_return_15m_pct,d.fwd_return_30m_pct,d.fwd_return_60m_pct,
            {mfe},{mae},now()
        FROM baselined d
        LEFT JOIN daily_context c ON c.symbol=d.symbol AND c.trade_date=d.trade_date
        WHERE d.trade_date BETWEEN %s AND %s
        ON CONFLICT(feature_set_id,symbol,bar_ts) {conflict}
    """
    params = (
        config.universe_run_id, config.liquidity_tiers,
        config.timeframe, config.feed, config.adjustment, config.session, config.session,
        lookback_start, chunk_end,
        config.universe_run_id, config.liquidity_tiers,
        config.timeframe, config.feed, config.adjustment,
        lookback_start - timedelta(days=7), chunk_end,
        None,  # replaced by feature_set_id in caller
        chunk_start, chunk_end,
    )
    return sql, params


def _build_chunk(feature_set_id: str, chunk: dict[str, Any], config: FeatureBuildConfig) -> int:
    timeout = get_settings().database_statement_timeout_seconds
    chunk_start, chunk_end = chunk["chunk_start"], chunk["chunk_end"]
    sql, params = _feature_sql(config, chunk_start, chunk_end)
    params = list(params)
    params[-3] = feature_set_id

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute("SELECT ra_ensure_feature_partitions(%s,%s)", (chunk_start - timedelta(days=1), chunk_end + timedelta(days=1)))
            if config.conflict_policy == "replace_slice":
                cur.execute(
                    "DELETE FROM ra_intraday_features WHERE feature_set_id=%s AND trade_date BETWEEN %s AND %s",
                    (feature_set_id, chunk_start, chunk_end),
                )
            cur.execute(sql, tuple(params))
            rows = max(cur.rowcount or 0, 0)
        conn.commit()
    return rows


def build_feature_set(job_id: str, config: FeatureBuildConfig) -> dict[str, Any]:
    feature_set_id, chunks = _ensure_feature_set(job_id, config)
    total = len(chunks)
    completed = sum(1 for chunk in chunks if chunk["status"] == "completed")
    set_progress(job_id, "building feature chunks", completed, total, result={"feature_set_id": feature_set_id})
    add_event(job_id, "feature_set_ready", f"Feature set planned in {total} date chunks.", details={"feature_set_id": feature_set_id})

    for chunk in chunks:
        if chunk["status"] == "completed":
            continue
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ra_feature_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",
                    (chunk["id"],),
                )
            conn.commit()
        try:
            rows = _build_chunk(feature_set_id, chunk, config)
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ra_feature_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s",
                        (rows, chunk["id"]),
                    )
                conn.commit()
            completed += 1
            set_progress(job_id, f"completed {chunk['chunk_start']} to {chunk['chunk_end']}", completed, total)
            add_event(job_id, "feature_chunk_completed", f"Built {rows:,} feature rows for {chunk['chunk_start']} to {chunk['chunk_end']}.")
        except Exception as exc:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_feature_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    cur.execute("UPDATE ra_feature_sets SET status='failed' WHERE id=%s", (feature_set_id,))
                conn.commit()
            raise

    check_control(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS rows,count(DISTINCT symbol) AS symbols,min(trade_date) AS first_date,max(trade_date) AS last_date FROM ra_intraday_features WHERE feature_set_id=%s", (feature_set_id,))
            summary = cur.fetchone()
            cur.execute(
                """
                UPDATE ra_feature_sets SET status='completed',row_count=%s,symbol_count=%s,
                    min_trade_date=%s,max_trade_date=%s,completed_at=now() WHERE id=%s
                """,
                (summary["rows"], summary["symbols"], summary["first_date"], summary["last_date"], feature_set_id),
            )
        conn.commit()
    result = {"feature_set_id": feature_set_id, **dict(summary)}
    add_event(job_id, "feature_set_completed", f"Feature set contains {summary['rows']:,} rows across {summary['symbols']:,} symbols.", details=result)
    return json_safe(result)

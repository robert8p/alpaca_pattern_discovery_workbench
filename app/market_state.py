from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import JobInterrupted, add_event, check_control, set_progress
from app.models import MarketStateBuildConfig
from app.research_policy import ABNORMAL_ACTIVITY_IMPACT_RATIO, ABNORMAL_VOLATILITY_RATIO, MARKET_STATE_VERSION

def _market_state_insert_sql() -> str:
    metrics: list[str] = []
    for h in (1, 5, 15, 30, 60):
        col = f"ret_{h}m_pct"
        metrics += [
            f"100.0*avg(CASE WHEN {col} IS NULL THEN NULL WHEN {col}>0 THEN 1.0 ELSE 0.0 END) AS pct_positive_{h}m",
            f"avg({col}) AS mean_return_{h}m_pct",
            f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL) AS median_return_{h}m_pct",
            f"(percentile_cont(0.75) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL)-percentile_cont(0.25) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL)) AS dispersion_{h}m_pct",
            f"stddev_samp({col}) AS stddev_return_{h}m_pct",
            f"percentile_cont(0.10) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL) AS p10_return_{h}m_pct",
            f"percentile_cont(0.25) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL) AS p25_return_{h}m_pct",
            f"percentile_cont(0.75) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL) AS p75_return_{h}m_pct",
            f"percentile_cont(0.90) WITHIN GROUP (ORDER BY {col}) FILTER (WHERE {col} IS NOT NULL) AS p90_return_{h}m_pct",
        ]
    metric_sql = ",\n                ".join(metrics)
    breadth_columns = []
    for prefix in ("pct_positive", "mean_return", "median_return", "dispersion", "stddev_return", "p10_return", "p25_return", "p75_return", "p90_return"):
        for h in (1, 5, 15, 30, 60):
            suffix = "" if prefix == "pct_positive" else "_pct"
            breadth_columns.append(f"{prefix}_{h}m{suffix}")
    breadth_select = ",".join(breadth_columns)
    return f"""
        INSERT INTO ra_market_state_features(
            market_state_run_id,feature_set_id,bar_ts,trade_date,minute_of_day,sample_stride_minutes,eligible_universe_count,
            {breadth_select},
            new_session_high_count,new_session_high_pct,new_session_low_count,new_session_low_pct,
            top_20pct_session_range_count,top_20pct_session_range_pct,bottom_20pct_session_range_count,bottom_20pct_session_range_pct,
            median_relative_volume,pct_relative_volume_gt_1,pct_relative_volume_gt_1_5,median_relative_trade_count,
            pct_abnormal_volatility,pct_abnormal_activity_adjusted_price_impact,
            spy_return_1m_pct,spy_return_5m_pct,spy_return_15m_pct,spy_return_30m_pct,spy_return_60m_pct,
            spy_distance_from_vwap_pct,spy_session_range_position,spy_relative_volume,spy_realised_volatility,
            qqq_return_1m_pct,qqq_return_5m_pct,qqq_return_15m_pct,qqq_return_30m_pct,qqq_return_60m_pct,
            qqq_distance_from_vwap_pct,qqq_session_range_position,qqq_relative_volume,qqq_realised_volatility
        )
        WITH u0 AS MATERIALIZED (
            SELECT f.*,p.ret_5m_pct AS prior_ret_5m_pct,p.relative_volume_20bar AS prior_rvol
            FROM ra_intraday_features f
            LEFT JOIN ra_intraday_features p ON p.feature_set_id=f.feature_set_id AND p.symbol=f.symbol AND p.bar_ts=f.bar_ts-interval '5 minutes'
            WHERE f.feature_set_id=%s AND f.trade_date BETWEEN %s AND %s
              AND f.session_label='regular' AND mod(f.minute_of_day-570,%s)=0
        ), u AS (
            SELECT u0.*,
                CASE WHEN ret_5m_pct IS NOT NULL AND relative_volume_20bar>0 THEN abs(ret_5m_pct)/relative_volume_20bar END AS activity_impact,
                CASE WHEN prior_ret_5m_pct IS NOT NULL AND prior_rvol>0 THEN abs(prior_ret_5m_pct)/prior_rvol END AS prior_activity_impact
            FROM u0
        ), breadth AS (
            SELECT bar_ts,min(trade_date) AS trade_date,min(minute_of_day) AS minute_of_day,count(*)::integer AS eligible_universe_count,
                {metric_sql},
                count(*) FILTER (WHERE high>=cumulative_high)::integer AS new_session_high_count,
                100.0*avg((high>=cumulative_high)::int) AS new_session_high_pct,
                count(*) FILTER (WHERE low<=cumulative_low)::integer AS new_session_low_count,
                100.0*avg((low<=cumulative_low)::int) AS new_session_low_pct,
                count(*) FILTER (WHERE cumulative_range_position>=0.8)::integer AS top_20pct_session_range_count,
                100.0*avg((cumulative_range_position>=0.8)::int) FILTER (WHERE cumulative_range_position IS NOT NULL) AS top_20pct_session_range_pct,
                count(*) FILTER (WHERE cumulative_range_position<=0.2)::integer AS bottom_20pct_session_range_count,
                100.0*avg((cumulative_range_position<=0.2)::int) FILTER (WHERE cumulative_range_position IS NOT NULL) AS bottom_20pct_session_range_pct,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY relative_volume_20bar) FILTER (WHERE relative_volume_20bar IS NOT NULL) AS median_relative_volume,
                100.0*avg((relative_volume_20bar>1)::int) FILTER (WHERE relative_volume_20bar IS NOT NULL) AS pct_relative_volume_gt_1,
                100.0*avg((relative_volume_20bar>1.5)::int) FILTER (WHERE relative_volume_20bar IS NOT NULL) AS pct_relative_volume_gt_1_5,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY relative_trade_count_20bar) FILTER (WHERE relative_trade_count_20bar IS NOT NULL) AS median_relative_trade_count,
                100.0*avg((rolling_realised_volatility_30bar>previous_day_realised_volatility*{ABNORMAL_VOLATILITY_RATIO})::int)
                    FILTER (WHERE rolling_realised_volatility_30bar IS NOT NULL AND previous_day_realised_volatility>0) AS pct_abnormal_volatility,
                100.0*avg((activity_impact>prior_activity_impact*{ABNORMAL_ACTIVITY_IMPACT_RATIO})::int)
                    FILTER (WHERE activity_impact IS NOT NULL AND prior_activity_impact>0) AS pct_abnormal_activity_adjusted_price_impact
            FROM u GROUP BY bar_ts
        ), idx0 AS MATERIALIZED (
            SELECT b.symbol,b.bar_ts,(b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                (extract(hour FROM b.bar_ts AT TIME ZONE 'America/New_York')::integer*60+extract(minute FROM b.bar_ts AT TIME ZONE 'America/New_York')::integer)::smallint AS minute_of_day,
                b.close,b.high,b.low,b.volume,COALESCE(b.vwap,b.close) AS px_vwap,
                lag(b.close,1) OVER w AS c1,lag(b.close,5) OVER w AS c5,lag(b.close,15) OVER w AS c15,lag(b.close,30) OVER w AS c30,lag(b.close,60) OVER w AS c60,
                lag(b.bar_ts,1) OVER w AS t1,lag(b.bar_ts,5) OVER w AS t5,lag(b.bar_ts,15) OVER w AS t15,lag(b.bar_ts,30) OVER w AS t30,lag(b.bar_ts,60) OVER w AS t60,
                max(b.high) OVER wcur AS session_high,min(b.low) OVER wcur AS session_low,
                sum(COALESCE(b.vwap,b.close)*b.volume) OVER wcur/NULLIF(sum(b.volume) OVER wcur,0) AS cumulative_vwap,
                avg(b.volume) OVER (PARTITION BY b.symbol,(b.bar_ts AT TIME ZONE 'America/New_York')::date ORDER BY b.bar_ts ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS prior20_volume
            FROM rd_bars b
            WHERE b.symbol=ANY(ARRAY['SPY','QQQ']) AND b.timeframe='1Min' AND b.feed='sip' AND b.adjustment='raw' AND b.session_label='regular'
              AND b.bar_ts >= ((%s::date)::timestamp AT TIME ZONE 'America/New_York')
              AND b.bar_ts < (((%s::date+1)::timestamp) AT TIME ZONE 'America/New_York')
            WINDOW w AS (PARTITION BY b.symbol,(b.bar_ts AT TIME ZONE 'America/New_York')::date ORDER BY b.bar_ts),
                   wcur AS (PARTITION BY b.symbol,(b.bar_ts AT TIME ZONE 'America/New_York')::date ORDER BY b.bar_ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ), idx1 AS (
            SELECT *,
                CASE WHEN t1=bar_ts-interval '1 minute' THEN (close/NULLIF(c1,0)-1)*100 END AS ret_1m_pct,
                CASE WHEN t5=bar_ts-interval '5 minutes' THEN (close/NULLIF(c5,0)-1)*100 END AS ret_5m_pct,
                CASE WHEN t15=bar_ts-interval '15 minutes' THEN (close/NULLIF(c15,0)-1)*100 END AS ret_15m_pct,
                CASE WHEN t30=bar_ts-interval '30 minutes' THEN (close/NULLIF(c30,0)-1)*100 END AS ret_30m_pct,
                CASE WHEN t60=bar_ts-interval '60 minutes' THEN (close/NULLIF(c60,0)-1)*100 END AS ret_60m_pct,
                (close/NULLIF(cumulative_vwap,0)-1)*100 AS distance_from_vwap_pct,
                (close-session_low)/NULLIF(session_high-session_low,0) AS session_range_position,
                volume/NULLIF(prior20_volume,0) AS relative_volume
            FROM idx0
        ), idx AS (
            SELECT idx1.*,
                stddev_samp(ret_1m_pct) OVER (PARTITION BY symbol,trade_date ORDER BY bar_ts ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS realised_volatility
            FROM idx1
        ), ip AS (
            SELECT bar_ts,
                max(ret_1m_pct) FILTER (WHERE symbol='SPY') AS spy_return_1m_pct,max(ret_5m_pct) FILTER (WHERE symbol='SPY') AS spy_return_5m_pct,
                max(ret_15m_pct) FILTER (WHERE symbol='SPY') AS spy_return_15m_pct,max(ret_30m_pct) FILTER (WHERE symbol='SPY') AS spy_return_30m_pct,
                max(ret_60m_pct) FILTER (WHERE symbol='SPY') AS spy_return_60m_pct,max(distance_from_vwap_pct) FILTER (WHERE symbol='SPY') AS spy_distance_from_vwap_pct,
                max(session_range_position) FILTER (WHERE symbol='SPY') AS spy_session_range_position,max(relative_volume) FILTER (WHERE symbol='SPY') AS spy_relative_volume,
                max(realised_volatility) FILTER (WHERE symbol='SPY') AS spy_realised_volatility,
                max(ret_1m_pct) FILTER (WHERE symbol='QQQ') AS qqq_return_1m_pct,max(ret_5m_pct) FILTER (WHERE symbol='QQQ') AS qqq_return_5m_pct,
                max(ret_15m_pct) FILTER (WHERE symbol='QQQ') AS qqq_return_15m_pct,max(ret_30m_pct) FILTER (WHERE symbol='QQQ') AS qqq_return_30m_pct,
                max(ret_60m_pct) FILTER (WHERE symbol='QQQ') AS qqq_return_60m_pct,max(distance_from_vwap_pct) FILTER (WHERE symbol='QQQ') AS qqq_distance_from_vwap_pct,
                max(session_range_position) FILTER (WHERE symbol='QQQ') AS qqq_session_range_position,max(relative_volume) FILTER (WHERE symbol='QQQ') AS qqq_relative_volume,
                max(realised_volatility) FILTER (WHERE symbol='QQQ') AS qqq_realised_volatility
            FROM idx GROUP BY bar_ts
        )
        SELECT %s,%s,b.bar_ts,b.trade_date,b.minute_of_day,%s,b.eligible_universe_count,
            {breadth_select},
            b.new_session_high_count,b.new_session_high_pct,b.new_session_low_count,b.new_session_low_pct,
            b.top_20pct_session_range_count,b.top_20pct_session_range_pct,b.bottom_20pct_session_range_count,b.bottom_20pct_session_range_pct,
            b.median_relative_volume,b.pct_relative_volume_gt_1,b.pct_relative_volume_gt_1_5,b.median_relative_trade_count,
            b.pct_abnormal_volatility,b.pct_abnormal_activity_adjusted_price_impact,
            ip.spy_return_1m_pct,ip.spy_return_5m_pct,ip.spy_return_15m_pct,ip.spy_return_30m_pct,ip.spy_return_60m_pct,
            ip.spy_distance_from_vwap_pct,ip.spy_session_range_position,ip.spy_relative_volume,ip.spy_realised_volatility,
            ip.qqq_return_1m_pct,ip.qqq_return_5m_pct,ip.qqq_return_15m_pct,ip.qqq_return_30m_pct,ip.qqq_return_60m_pct,
            ip.qqq_distance_from_vwap_pct,ip.qqq_session_range_position,ip.qqq_relative_volume,ip.qqq_realised_volatility
        FROM breadth b LEFT JOIN ip ON ip.bar_ts=b.bar_ts
        ON CONFLICT (market_state_run_id,bar_ts) DO UPDATE SET built_at=excluded.built_at
    """


def _ensure_market_state_run(job_id: str, config: MarketStateBuildConfig) -> str:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_market_state_runs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                run_id = row["id"]
                cur.execute("UPDATE ra_market_state_runs SET status='running',completed_at=NULL WHERE id=%s", (run_id,))
                cur.execute("UPDATE ra_market_state_chunks SET status='pending',error=NULL WHERE market_state_run_id=%s AND status IN ('running','failed','cancelled')", (run_id,))
            else:
                cur.execute("SELECT universe_run_id,status FROM ra_feature_sets WHERE id=%s", (config.feature_set_id,))
                fs = cur.fetchone()
                if not fs or fs["status"] != "completed":
                    raise ValueError("Market state requires a completed feature set")
                cur.execute(
                    """
                    INSERT INTO ra_market_state_runs(job_id,feature_set_id,universe_run_id,name,config,market_state_version,min_trade_date,max_trade_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (job_id, config.feature_set_id, fs["universe_run_id"], config.name, Jsonb(config.model_dump(mode="json")), MARKET_STATE_VERSION, config.start_date, config.end_date),
                )
                run_id = cur.fetchone()["id"]
                d = config.start_date
                while d <= config.end_date:
                    cur.execute("INSERT INTO ra_market_state_chunks(market_state_run_id,chunk_start,chunk_end) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (run_id, d, d))
                    d += timedelta(days=1)
            cur.execute("SELECT count(*) FROM ra_market_state_chunks WHERE market_state_run_id=%s", (run_id,))
        conn.commit()
    return str(run_id)


def run_market_state_build(job_id: str, config: MarketStateBuildConfig) -> dict[str, Any]:
    run_id = _ensure_market_state_run(job_id, config)
    query = _market_state_insert_sql()
    while True:
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ra_market_state_chunks WHERE market_state_run_id=%s AND status IN ('pending','failed') ORDER BY chunk_start LIMIT 1", (run_id,))
                chunk = cur.fetchone()
                if chunk:
                    cur.execute("UPDATE ra_market_state_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s", (chunk["id"],))
            conn.commit()
        if not chunk:
            break
        try:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT ra_ensure_market_state_partitions(%s,%s)", (chunk["chunk_start"], chunk["chunk_end"]))
                    params = (
                        config.feature_set_id, chunk["chunk_start"], chunk["chunk_end"], config.sample_stride_minutes,
                        chunk["chunk_start"], chunk["chunk_end"], run_id, config.feature_set_id, config.sample_stride_minutes,
                    )
                    cur.execute(query, params)
                    rows = cur.rowcount or 0
                    cur.execute("UPDATE ra_market_state_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s", (rows, chunk["id"]))
                conn.commit()
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_market_state_chunks SET status='pending' WHERE id=%s", (chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_market_state_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    cur.execute("UPDATE ra_market_state_runs SET status='failed' WHERE id=%s", (run_id,))
                conn.commit()
            raise
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FILTER (WHERE status='completed') done,count(*) total FROM ra_market_state_chunks WHERE market_state_run_id=%s", (run_id,))
                p = cur.fetchone()
            conn.rollback()
        set_progress(job_id, f"market state {chunk['chunk_start']}", p["done"], p["total"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(rows_written),0) rows FROM ra_market_state_chunks WHERE market_state_run_id=%s AND status='completed'", (run_id,))
            rows = int(cur.fetchone()["rows"])
            cur.execute("UPDATE ra_market_state_runs SET status='completed',row_count=%s,completed_at=now() WHERE id=%s", (rows, run_id))
        conn.commit()
    result = {"market_state_run_id": run_id, "rows": rows, "version": MARKET_STATE_VERSION}
    add_event(job_id, "market_state_completed", "Point-in-time market-state layer completed.", details=result)
    return result


_SAFE_STRENGTH_FIELDS = {
    "ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "ret_60m_pct",
    "relative_volume_20bar", "relative_trade_count_20bar", "activity_impact_change_ratio",
    "distance_from_cumulative_vwap_pct", "cumulative_range_position",
}



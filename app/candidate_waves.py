from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import JobInterrupted, add_event, check_control, set_progress
from app.models import CandidateWaveBuildConfig
from app.research_policy import CANDIDATE_WAVE_VERSION

_SAFE_STRENGTH_FIELDS = {
    "ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "ret_60m_pct",
    "relative_volume_20bar", "relative_trade_count_20bar", "activity_impact_change_ratio",
    "distance_from_cumulative_vwap_pct", "cumulative_range_position",
}

def _candidate_wave_query(conditions: list[dict[str, Any]], strength_field: str | None, elevated_pct: float) -> tuple[str, list[Any]]:
    from app.discovery import _condition_sql
    where, condition_params = _condition_sql(conditions, alias="e")
    strength = f"e.{strength_field}" if strength_field in _SAFE_STRENGTH_FIELDS else "NULL::double precision"
    sql = f"""
        WITH eligible AS MATERIALIZED (
            SELECT s.*,a.exchange,COALESCE(a.attributes->>'sector',a.raw->>'sector') AS sector
            FROM ra_discovery_samples s LEFT JOIN rd_assets a ON a.symbol=s.symbol
            WHERE s.discovery_run_id=%s AND s.trade_date BETWEEN %s AND %s
        ), q AS (
            SELECT e.*,{strength} AS signal_strength FROM eligible e WHERE {where}
        ), all_counts AS (
            SELECT bar_ts,min(trade_date) trade_date,count(*)::integer eligible_universe_count FROM eligible GROUP BY bar_ts
        ), q_counts AS (
            SELECT bar_ts,count(*)::integer qualifying_stock_count,
                count(*) FILTER (WHERE liquidity_tier='A')::integer tier_a_count,
                count(*) FILTER (WHERE liquidity_tier='B')::integer tier_b_count,
                count(*) FILTER (WHERE liquidity_tier='C')::integer tier_c_count,
                avg(signal_strength) average_signal_strength,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY signal_strength) FILTER (WHERE signal_strength IS NOT NULL) median_signal_strength,
                max(signal_strength) maximum_signal_strength,stddev_samp(signal_strength) signal_strength_dispersion
            FROM q GROUP BY bar_ts
        ), exch AS (
            SELECT bar_ts,jsonb_object_agg(exchange,n) FILTER (WHERE exchange IS NOT NULL) AS exchange_concentration,
                   100.0*max(n)/NULLIF(sum(n),0) AS largest_exchange_share_pct
            FROM (SELECT bar_ts,exchange,count(*)::double precision n FROM q WHERE exchange IS NOT NULL GROUP BY bar_ts,exchange) z GROUP BY bar_ts
        ), sect AS (
            SELECT bar_ts,jsonb_object_agg(sector,n) FILTER (WHERE sector IS NOT NULL) AS sector_concentration,
                   100.0*max(n)/NULLIF(sum(n),0) AS largest_sector_share_pct
            FROM (SELECT bar_ts,sector,count(*)::double precision n FROM q WHERE sector IS NOT NULL GROUP BY bar_ts,sector) z GROUP BY bar_ts
        ), waves0 AS (
            SELECT a.bar_ts,a.trade_date,a.eligible_universe_count,COALESCE(q.qualifying_stock_count,0) qualifying_stock_count,
                100.0*COALESCE(q.qualifying_stock_count,0)/NULLIF(a.eligible_universe_count,0) qualifying_stock_pct,
                COALESCE(q.tier_a_count,0) tier_a_count,COALESCE(q.tier_b_count,0) tier_b_count,COALESCE(q.tier_c_count,0) tier_c_count,
                q.average_signal_strength,q.median_signal_strength,q.maximum_signal_strength,q.signal_strength_dispersion,
                x.exchange_concentration,x.largest_exchange_share_pct,s.sector_concentration,s.largest_sector_share_pct
            FROM all_counts a LEFT JOIN q_counts q USING(bar_ts) LEFT JOIN exch x USING(bar_ts) LEFT JOIN sect s USING(bar_ts)
        ), waves1 AS (
            SELECT *,lag(qualifying_stock_count) OVER (ORDER BY bar_ts) previous_wave_qualifying_count,
                CASE WHEN qualifying_stock_pct >= {float(elevated_pct)} THEN 1 ELSE 0 END AS elevated
            FROM waves0
        ), waves2 AS (
            SELECT *,sum(CASE WHEN elevated=0 THEN 1 ELSE 0 END) OVER (ORDER BY bar_ts) AS reset_group FROM waves1
        )
        SELECT *,CASE WHEN elevated=1 THEN count(*) FILTER (WHERE elevated=1) OVER (PARTITION BY reset_group ORDER BY bar_ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) ELSE 0 END::integer AS consecutive_elevated
        FROM waves2 ORDER BY bar_ts
    """
    return sql, condition_params


def _ensure_candidate_wave_run(job_id: str, config: CandidateWaveBuildConfig, candidate: dict[str, Any]) -> str:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                run_id = row["id"]
                cur.execute("UPDATE ra_candidate_wave_runs SET status='running',completed_at=NULL WHERE id=%s", (run_id,))
                cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending',error=NULL WHERE candidate_wave_run_id=%s AND status IN ('running','failed','cancelled')", (run_id,))
            else:
                cur.execute(
                    """
                    INSERT INTO ra_candidate_wave_runs(job_id,candidate_id,feature_set_id,name,config,wave_version,min_trade_date,max_trade_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (job_id, config.candidate_id, candidate["feature_set_id"], config.name, Jsonb(config.model_dump(mode="json")), CANDIDATE_WAVE_VERSION, config.start_date, config.end_date),
                )
                run_id = cur.fetchone()["id"]
                d = config.start_date
                while d <= config.end_date:
                    cur.execute("INSERT INTO ra_candidate_wave_chunks(candidate_wave_run_id,chunk_start,chunk_end) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (run_id, d, d))
                    d += timedelta(days=1)
        conn.commit()
    return str(run_id)


def run_candidate_wave_build(job_id: str, config: CandidateWaveBuildConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_candidate_rules WHERE id=%s", (config.candidate_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate does not exist")
    candidate = dict(row)
    run_id = _ensure_candidate_wave_run(job_id, config, candidate)
    query, condition_params = _candidate_wave_query(candidate["conditions"], config.signal_strength_field, config.elevated_wave_threshold_pct)
    while True:
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ra_candidate_wave_chunks WHERE candidate_wave_run_id=%s AND status IN ('pending','failed') ORDER BY chunk_start LIMIT 1", (run_id,))
                chunk = cur.fetchone()
                if chunk:
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s", (chunk["id"],))
            conn.commit()
        if not chunk:
            break
        try:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT ra_ensure_candidate_wave_partitions(%s,%s)", (chunk["chunk_start"], chunk["chunk_end"]))
                    cur.execute(query, (candidate["discovery_run_id"], chunk["chunk_start"], chunk["chunk_end"], *condition_params))
                    rows = cur.fetchall()
                    for r in rows:
                        r = dict(r)
                        prev = r.get("previous_wave_qualifying_count")
                        change = None if prev is None else int(r["qualifying_stock_count"]) - int(prev)
                        pct_change = None if not prev else 100.0 * change / prev
                        cur.execute(
                            """
                            INSERT INTO ra_candidate_wave_stats(
                                candidate_wave_run_id,candidate_id,feature_set_id,bar_ts,trade_date,eligible_universe_count,
                                qualifying_stock_count,qualifying_stock_pct,tier_a_count,tier_b_count,tier_c_count,
                                average_signal_strength,median_signal_strength,maximum_signal_strength,signal_strength_dispersion,signal_strength_method,
                                exchange_concentration,largest_exchange_share_pct,sector_concentration,largest_sector_share_pct,
                                previous_wave_qualifying_count,change_in_qualifying_count,pct_change_in_qualifying_count,
                                consecutive_elevated_wave_count,elevated_wave_threshold_pct
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (candidate_wave_run_id,bar_ts) DO UPDATE SET built_at=excluded.built_at
                            """,
                            (
                                run_id, config.candidate_id, candidate["feature_set_id"], r["bar_ts"], r["trade_date"], r["eligible_universe_count"],
                                r["qualifying_stock_count"], r["qualifying_stock_pct"], r["tier_a_count"], r["tier_b_count"], r["tier_c_count"],
                                r.get("average_signal_strength"), r.get("median_signal_strength"), r.get("maximum_signal_strength"), r.get("signal_strength_dispersion"),
                                config.signal_strength_field or "not_defined", r.get("exchange_concentration"), r.get("largest_exchange_share_pct"),
                                r.get("sector_concentration"), r.get("largest_sector_share_pct"), prev, change, pct_change,
                                r.get("consecutive_elevated") or 0, config.elevated_wave_threshold_pct,
                            ),
                        )
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s", (len(rows), chunk["id"]))
                conn.commit()
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE id=%s", (chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    cur.execute("UPDATE ra_candidate_wave_runs SET status='failed' WHERE id=%s", (run_id,))
                conn.commit()
            raise
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FILTER(WHERE status='completed') done,count(*) total FROM ra_candidate_wave_chunks WHERE candidate_wave_run_id=%s", (run_id,))
                p = cur.fetchone()
            conn.rollback()
        set_progress(job_id, f"candidate wave {chunk['chunk_start']}", p["done"], p["total"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(rows_written),0) rows FROM ra_candidate_wave_chunks WHERE candidate_wave_run_id=%s AND status='completed'", (run_id,))
            rows = int(cur.fetchone()["rows"])
            cur.execute("UPDATE ra_candidate_wave_runs SET status='completed',row_count=%s,completed_at=now() WHERE id=%s", (rows, run_id))
        conn.commit()
    result = {"candidate_wave_run_id": run_id, "rows": rows, "version": CANDIDATE_WAVE_VERSION}
    add_event(job_id, "candidate_wave_completed", "Candidate wave statistics completed without evaluating forward outcomes.", details=result)
    return result



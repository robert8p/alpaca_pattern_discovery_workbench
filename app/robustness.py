from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.discovery import (
    SYMBOL_BUCKETS, DiscoveryQueryTimeout, _bucket_ranges, _condition_sql,
    _execute_guarded, _is_retryable_database_error, _is_timeout,
)
from app.jobs import JobInterrupted, add_event, check_control, set_progress
from app.models import RobustnessAnalysisConfig
from app.sql_validation import validate_sql_bindings
from app.utils import ensure_feature_set_compatibility, finite_or_none, json_safe

ROBUSTNESS_VERSION = "2.0.0"
ROBUSTNESS_ENGINE_VERSION = "2.3.0"
SUPPORTED_RULE_DEFINITIONS = {
    "2026-08-staged-v2",
    "2026-08-coverage-pack1-v1",
}


def _dates(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _compatible(source: dict[str, Any], target: dict[str, Any], horizon: int) -> None:
    ensure_feature_set_compatibility(
        source.get("universe_run_id"), target.get("universe_run_id"),
        dict(source.get("config") or {}), dict(target.get("config") or {}), horizon,
    )


def _perturb_conditions(conditions: list[dict[str, Any]], pct: float, mode: str) -> list[dict[str, Any]]:
    factor = pct / 100.0
    out: list[dict[str, Any]] = []
    for raw in conditions:
        c = dict(raw)
        op = c.get("operator")
        if op == "range":
            low_raw, high_raw = c.get("low"), c.get("high")
            if low_raw is not None and high_raw is not None:
                low, high = float(low_raw), float(high_raw)
                center = (low + high) / 2.0
                half = (high - low) / 2.0
                scale = 1.0 + factor if mode == "relaxed" else max(0.05, 1.0 - factor)
                c["low"], c["high"] = center - half * scale, center + half * scale
            elif low_raw is not None:
                low = float(low_raw)
                delta = factor * max(abs(low), 1e-9)
                c["low"] = low - delta if mode == "relaxed" else low + delta
            elif high_raw is not None:
                high = float(high_raw)
                delta = factor * max(abs(high), 1e-9)
                c["high"] = high + delta if mode == "relaxed" else high - delta
        elif op in {"gte", "gt", "lte", "lt"} and isinstance(c.get("value"), (int, float)):
            value = float(c["value"])
            if value != 0:
                strict_positive = (op in {"gte", "gt"} and value > 0) or (op in {"lte", "lt"} and value < 0)
                strict_negative = (op in {"lte", "lt"} and value > 0) or (op in {"gte", "gt"} and value < 0)
                if mode == "relaxed":
                    c["value"] = value * (1.0 - factor if strict_positive else 1.0 + factor if strict_negative else 1.0)
                else:
                    c["value"] = value * (1.0 + factor if strict_positive else 1.0 - factor if strict_negative else 1.0)
        elif op == "abs_gte" and isinstance(c.get("value"), (int, float)):
            c["value"] = float(c["value"]) * (1.0 - factor if mode == "relaxed" else 1.0 + factor)
        elif op == "abs_lt" and isinstance(c.get("value"), (int, float)):
            c["value"] = float(c["value"]) * (1.0 + factor if mode == "relaxed" else 1.0 - factor)
        elif op == "abs_range" and c.get("low") is not None and c.get("high") is not None:
            low, high = float(c["low"]), float(c["high"])
            center = (low + high) / 2.0
            half = (high - low) / 2.0
            scale = 1.0 + factor if mode == "relaxed" else max(0.05, 1.0 - factor)
            c["low"], c["high"] = max(0.0, center - half * scale), center + half * scale
        out.append(c)
    return out


def _sampling_sql(stride: int, anchor: int, alias: str = "s") -> str:
    if stride < 1:
        raise ValueError("Entry stride must be at least one minute")
    return "TRUE" if stride == 1 else f"mod({alias}.minute_of_day - {int(anchor)}, {int(stride)}) = 0"


def _return_sql(direction: str, horizon: int) -> tuple[str, str, str]:
    if direction not in {"long", "short"}:
        raise ValueError("Direction must be long or short")
    if horizon not in {5, 15, 30, 60}:
        raise ValueError("Unsupported holding horizon")
    outcome = f"e.fwd_return_{horizon}m_pct"
    if direction == "long":
        return outcome, "(exc.max_high/NULLIF(e.close,0)-1)*100", "(exc.min_low/NULLIF(e.close,0)-1)*100"
    return f"-{outcome}", "-(exc.min_low/NULLIF(e.close,0)-1)*100", "-(exc.max_high/NULLIF(e.close,0)-1)*100"


def _development_observation_query(
    conditions: list[dict[str, Any]], direction: str, horizon: int,
    stride: int, anchor: int, delay: int,
) -> tuple[str, tuple[Any, ...]]:
    where, condition_params = _condition_sql(conditions, alias="s")
    sampling = _sampling_sql(stride, anchor, "s")
    gross, mfe, mae = _return_sql(direction, horizon)
    query = f"""
        WITH signal AS MATERIALIZED (
            SELECT s.*
            FROM ra_discovery_samples s
            WHERE s.discovery_run_id=%s
              AND s.period_label=%s
              AND s.trade_date=%s
              AND s.symbol_bucket >= %s AND s.symbol_bucket < %s
              AND ({sampling})
              AND ({where})
        )
        SELECT s.symbol_bucket,s.symbol,s.bar_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,
            COALESCE(s.price_group,CASE WHEN e.close < 5 THEN 'lt_5' WHEN e.close < 10 THEN '5_10'
                 WHEN e.close < 25 THEN '10_25' WHEN e.close < 100 THEN '25_100' ELSE 'ge_100' END) AS price_group,
            ({gross})::double precision AS gross_return_pct,
            ({mfe})::double precision AS mfe_pct,
            ({mae})::double precision AS mae_pct
        FROM signal s
        LEFT JOIN ra_intraday_features e
          ON e.feature_set_id=%s AND e.symbol=s.symbol
         AND e.bar_ts=s.bar_ts+(%s::integer * interval '1 minute')
        LEFT JOIN LATERAL (
            SELECT max(x.high) AS max_high,min(x.low) AS min_low
            FROM ra_intraday_features x
            WHERE x.feature_set_id=e.feature_set_id AND x.symbol=e.symbol
              AND x.bar_ts>e.bar_ts
              AND x.bar_ts<=e.bar_ts+(%s::integer * interval '1 minute')
        ) exc ON TRUE
        ORDER BY s.symbol,s.bar_ts
    """
    sample_params = ("run", "discovery", date.today(), 0, 256, *condition_params, "feature", delay, horizon)
    validate_sql_bindings(query, sample_params, name="robustness development observation query")
    return query, tuple(condition_params)


def _observation_query(
    conditions: list[dict[str, Any]], direction: str, horizon: int,
    stride: int, anchor: int, delay: int,
) -> tuple[str, tuple[Any, ...]]:
    """Build the bounded feature-table query used for cross-feature-set holdouts.

    Unlike the withdrawn robustness query, this statement is always constrained
    to one trade date and one deterministic symbol bucket range.
    """
    where, condition_params = _condition_sql(conditions, alias="s")
    sampling = _sampling_sql(stride, anchor, "s")
    gross, mfe, mae = _return_sql(direction, horizon)
    query = f"""
        WITH source AS MATERIALIZED (
            SELECT f.*,
                mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS})::smallint AS symbol_bucket,
                CASE WHEN f.close < 5 THEN 'lt_5' WHEN f.close < 10 THEN '5_10'
                     WHEN f.close < 25 THEN '10_25' WHEN f.close < 100 THEN '25_100' ELSE 'ge_100' END AS price_group,
                CASE WHEN f.ret_5m_pct IS NOT NULL AND f.relative_volume_20bar > 0
                     THEN abs(f.ret_5m_pct)/f.relative_volume_20bar END AS activity_adjusted_return_5m,
                CASE WHEN p.ret_5m_pct IS NOT NULL AND p.relative_volume_20bar > 0
                     THEN abs(p.ret_5m_pct)/p.relative_volume_20bar END AS prior_activity_adjusted_return_5m,
                p.relative_volume_20bar AS prior_relative_volume_20bar,
                p.relative_trade_count_20bar AS prior_relative_trade_count_20bar,
                max(f.high) FILTER (WHERE f.minute_of_day < 600) OVER (PARTITION BY f.symbol,f.trade_date) AS opening_range_high,
                min(f.low) FILTER (WHERE f.minute_of_day < 600) OVER (PARTITION BY f.symbol,f.trade_date) AS opening_range_low
            FROM ra_intraday_features f
            LEFT JOIN ra_intraday_features p
              ON p.feature_set_id=f.feature_set_id AND p.symbol=f.symbol
             AND p.bar_ts=f.bar_ts-interval '5 minutes'
            WHERE f.feature_set_id=%s
              AND f.bar_ts >= (%s::date::timestamp AT TIME ZONE 'America/New_York')
              AND f.bar_ts < (((%s::date + 1)::timestamp) AT TIME ZONE 'America/New_York')
              AND f.trade_date=%s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) >= %s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) < %s
        ), enriched AS (
            SELECT source.*,
                CASE WHEN activity_adjusted_return_5m IS NOT NULL AND prior_activity_adjusted_return_5m > 0
                     THEN activity_adjusted_return_5m/prior_activity_adjusted_return_5m END AS activity_impact_change_ratio,
                CASE WHEN relative_volume_20bar IS NOT NULL AND prior_relative_volume_20bar > 0
                     THEN relative_volume_20bar/prior_relative_volume_20bar END AS relative_volume_change_ratio,
                CASE WHEN relative_trade_count_20bar IS NOT NULL AND prior_relative_trade_count_20bar > 0
                     THEN relative_trade_count_20bar/prior_relative_trade_count_20bar END AS relative_trade_count_change_ratio,
                CASE WHEN rolling_range_30bar_pct IS NOT NULL AND previous_day_range_pct > 0
                     THEN rolling_range_30bar_pct/previous_day_range_pct END AS range_vs_previous_day_ratio,
                CASE WHEN rolling_realised_volatility_30bar IS NOT NULL AND previous_day_realised_volatility > 0
                     THEN rolling_realised_volatility_30bar/previous_day_realised_volatility END AS volatility_vs_previous_day_ratio,
                CASE WHEN minute_of_day < 600 OR opening_range_high IS NULL OR opening_range_low IS NULL THEN NULL
                     WHEN close > opening_range_high THEN 'above' WHEN close < opening_range_low THEN 'below' ELSE 'inside' END AS opening_range_position,
                (high>=cumulative_high) AS touched_session_high,
                (low<=cumulative_low) AS touched_session_low
            FROM source
        ), signal AS MATERIALIZED (
            SELECT s.* FROM enriched s
            WHERE ({sampling}) AND ({where})
        )
        SELECT s.symbol_bucket,s.symbol,s.bar_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,s.price_group,
            ({gross})::double precision AS gross_return_pct,
            ({mfe})::double precision AS mfe_pct,
            ({mae})::double precision AS mae_pct
        FROM signal s
        LEFT JOIN ra_intraday_features e
          ON e.feature_set_id=s.feature_set_id AND e.symbol=s.symbol
         AND e.bar_ts=s.bar_ts+(%s::integer * interval '1 minute')
        LEFT JOIN LATERAL (
            SELECT max(x.high) AS max_high,min(x.low) AS min_low
            FROM ra_intraday_features x
            WHERE x.feature_set_id=e.feature_set_id AND x.symbol=e.symbol
              AND x.bar_ts>e.bar_ts
              AND x.bar_ts<=e.bar_ts+(%s::integer * interval '1 minute')
        ) exc ON TRUE
        ORDER BY s.symbol,s.bar_ts
    """
    sample_params = ("feature", date.today(), date.today(), date.today(), 0, 256, *condition_params, delay, horizon)
    validate_sql_bindings(query, sample_params, name="robustness bounded observation query")
    return query, tuple(condition_params)

def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _metrics(rows: list[dict[str, Any]], cost_bps: float) -> dict[str, Any]:
    signal_count = len(rows)
    valid_rows = [r for r in rows if r.get("gross_return_pct") is not None]
    gross = [float(r["gross_return_pct"]) for r in valid_rows]
    cost_pct = float(cost_bps) / 100.0
    net = [x - cost_pct for x in gross]
    n = len(net)
    missing = signal_count - n
    if not n:
        return {
            "candidate_signals": signal_count, "observations": 0, "missing_outcomes": missing,
            "missing_data_rate_pct": 100.0 if signal_count else 0.0, "cost_bps": cost_bps,
        }
    mean = sum(net) / n
    stdev = statistics.stdev(net) if n > 1 else None
    by_date: dict[str, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(valid_rows, net):
        by_date[str(row["trade_date"])].append(value)
        by_symbol[str(row["symbol"])].append(value)
    daily_means = [sum(v)/len(v) for v in by_date.values()]
    daily_stdev = statistics.stdev(daily_means) if len(daily_means) > 1 else None
    clustered_t = (statistics.mean(daily_means)/daily_stdev*math.sqrt(len(daily_means))) if daily_stdev and daily_stdev > 0 else None
    loo = []
    total = sum(net)
    for values in by_date.values():
        remaining_n = n - len(values)
        if remaining_n:
            loo.append((total - sum(values)) / remaining_n)
    positive = sum(v for v in net if v > 0)
    negative = abs(sum(v for v in net if v < 0))
    ordered_desc = sorted(net, reverse=True)
    contribution = {}
    for pct in (1, 5, 10):
        k = max(1, math.ceil(n * pct / 100))
        contribution[f"top_{pct}pct_return_share_pct"] = finite_or_none(100 * sum(ordered_desc[:k]) / total) if total != 0 else None
    mfe_values = [float(r["mfe_pct"]) for r in valid_rows if r.get("mfe_pct") is not None]
    mae_values = [float(r["mae_pct"]) for r in valid_rows if r.get("mae_pct") is not None]
    return {
        "candidate_signals": signal_count, "observations": n, "missing_outcomes": missing,
        "missing_data_rate_pct": finite_or_none(100 * missing / signal_count) if signal_count else 0.0,
        "symbols": len(by_symbol), "dates": len(by_date), "cost_bps": cost_bps,
        "net_avg_pct": finite_or_none(mean), "gross_avg_pct": finite_or_none(sum(gross)/n),
        "median_pct": finite_or_none(_quantile(net, .5)), "p05_pct": finite_or_none(_quantile(net, .05)),
        "p25_pct": finite_or_none(_quantile(net, .25)), "p75_pct": finite_or_none(_quantile(net, .75)),
        "p95_pct": finite_or_none(_quantile(net, .95)), "worst_pct": finite_or_none(min(net)),
        "best_pct": finite_or_none(max(net)), "win_rate_pct": finite_or_none(100*sum(v>0 for v in net)/n),
        "stdev_pct": finite_or_none(stdev),
        "t_stat": finite_or_none(mean/stdev*math.sqrt(n)) if stdev and stdev > 0 else None,
        "date_clustered_t_stat": finite_or_none(clustered_t),
        "leave_one_date_out_min_net_avg_pct": finite_or_none(min(loo)) if loo else None,
        "profit_factor": finite_or_none(positive/negative) if negative > 0 else None,
        "max_symbol_share_pct": finite_or_none(100*max(len(v) for v in by_symbol.values())/n),
        "max_date_share_pct": finite_or_none(100*max(len(v) for v in by_date.values())/n),
        "mfe_avg_pct": finite_or_none(sum(mfe_values)/len(mfe_values)) if mfe_values else None,
        "mfe_median_pct": finite_or_none(_quantile(mfe_values,.5)) if mfe_values else None,
        "mae_avg_pct": finite_or_none(sum(mae_values)/len(mae_values)) if mae_values else None,
        "mae_median_pct": finite_or_none(_quantile(mae_values,.5)) if mae_values else None,
        **contribution,
    }


def _breakdown(rows: list[dict[str, Any]], cost_bps: float, field: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "missing")].append(row)
    return {key: _metrics(value, cost_bps) for key, value in sorted(groups.items())}


def _month_breakdown(rows: list[dict[str, Any]], cost_bps: float) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get("trade_date")
        key = str(value)[:7] if value else "missing"
        groups[key].append(row)
    return {key: _metrics(value, cost_bps) for key, value in sorted(groups.items())}


def _year_breakdown(rows: list[dict[str, Any]], cost_bps: float) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get("trade_date")
        key = str(value)[:4] if value else "missing"
        groups[key].append(row)
    return {key: _metrics(value, cost_bps) for key, value in sorted(groups.items())}


def _verdict(summary: dict[str, Any], mode: str) -> str:
    base = summary.get("base") or {}
    cost30 = (summary.get("cost_sensitivity") or {}).get("30") or (summary.get("cost_sensitivity") or {}).get(30) or {}
    neighborhood = summary.get("neighbourhood") or {}
    relaxed = neighborhood.get("relaxed") or {}
    tightened = neighborhood.get("tightened") or {}
    if (base.get("net_avg_pct") or 0) <= 0 or (base.get("date_clustered_t_stat") or -99) <= 0:
        return "REJECT"
    robust = (
        (cost30.get("net_avg_pct") or 0) > 0
        and (base.get("profit_factor") or 0) > 1.05
        and (base.get("date_clustered_t_stat") or 0) >= 1.0
        and (relaxed.get("net_avg_pct") or 0) > 0
        and (tightened.get("net_avg_pct") or 0) > 0
    )
    if not robust:
        return "WEAK"
    if mode == "historical_holdout" and (base.get("date_clustered_t_stat") or 0) >= 1.5:
        return "HISTORICAL_HOLDOUT"
    return "PROMISING"



def _variant_specs(config: RobustnessAnalysisConfig, conditions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for delay in config.entry_delays_minutes:
        specs[f"delay:{int(delay)}"] = {"delay": int(delay), "conditions": [dict(x) for x in conditions]}
    specs["neighbour:relaxed"] = {"delay": 0, "conditions": _perturb_conditions(conditions, config.neighbourhood_pct, "relaxed")}
    specs["neighbour:tightened"] = {"delay": 0, "conditions": _perturb_conditions(conditions, config.neighbourhood_pct, "tightened")}
    return specs


def _ensure_run(job_id: str, candidate: dict[str, Any], target_feature_set_id: Any,
                config: RobustnessAnalysisConfig, start: date, end: date) -> str:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_robustness_runs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                run_id = str(row["id"])
                cur.execute("DELETE FROM ra_robustness_results WHERE robustness_run_id=%s", (run_id,))
                cur.execute("""UPDATE ra_robustness_runs SET status='running',engine_version=%s,summary=NULL,verdict=NULL,completed_at=NULL,
                    config=%s,start_date=%s,end_date=%s,target_feature_set_id=%s WHERE id=%s""",
                    (ROBUSTNESS_ENGINE_VERSION,Jsonb(config.model_dump(mode="json")),start,end,target_feature_set_id,run_id))
                cur.execute("UPDATE ra_robustness_chunks SET status='pending',error=NULL WHERE robustness_run_id=%s AND status IN ('running','failed','cancelled')", (run_id,))
            else:
                cur.execute("""INSERT INTO ra_robustness_runs(job_id,candidate_id,source_feature_set_id,target_feature_set_id,mode,config,start_date,end_date,engine_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (job_id,candidate["id"],candidate["feature_set_id"],target_feature_set_id,config.mode,Jsonb(config.model_dump(mode="json")),start,end,ROBUSTNESS_ENGINE_VERSION))
                run_id = str(cur.fetchone()["id"])
        conn.commit()
    return run_id


def _initialise_chunks(run_id: str, dates: list[date], variant_keys: list[str], shards: int) -> None:
    shards = max(1, min(SYMBOL_BUCKETS, int(shards)))
    ranges = _bucket_ranges(shards)
    rows = [(run_id,key,d,b0,b1) for key in variant_keys for d in dates for b0,b1 in ranges]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO ra_robustness_chunks(robustness_run_id,variant_key,trade_date,bucket_start,bucket_end)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", rows)
        conn.commit()


def _claim_chunk(run_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM ra_robustness_chunks WHERE robustness_run_id=%s AND status='pending'
                ORDER BY trade_date,variant_key,bucket_start FOR UPDATE SKIP LOCKED LIMIT 1""", (run_id,))
            row=cur.fetchone()
            if row:
                cur.execute("UPDATE ra_robustness_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s", (row["id"],))
        conn.commit()
    return dict(row) if row else None


def _split_chunk(job_id: str, chunk: dict[str, Any], error: Exception) -> bool:
    b0,b1=int(chunk["bucket_start"]),int(chunk["bucket_end"])
    if b1-b0 <= 1:
        return False
    mid=(b0+b1)//2
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_robustness_chunks SET status='split',error=%s WHERE id=%s", (f"Automatically split after timeout: {error}",chunk["id"]))
            for x0,x1 in ((b0,mid),(mid,b1)):
                cur.execute("""INSERT INTO ra_robustness_chunks(robustness_run_id,variant_key,trade_date,bucket_start,bucket_end)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (chunk["robustness_run_id"],chunk["variant_key"],chunk["trade_date"],x0,x1))
        conn.commit()
    add_event(job_id,"robustness_chunk_split","A robustness slice exceeded the timeout and was split automatically.",level="warning",
              details={"chunk_id":chunk["id"],"variant":chunk["variant_key"],"date":str(chunk["trade_date"]),"bucket_start":b0,"bucket_end":b1})
    return True


def _retry_chunk(job_id: str, chunk: dict[str, Any], error: Exception) -> bool:
    attempts=int(chunk.get("attempts") or 0)+1
    maximum=max(1,int(get_settings().robustness_query_retries))
    if not _is_retryable_database_error(error) or attempts >= maximum:
        return False
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_robustness_chunks SET status='pending',error=%s WHERE id=%s", (f"Transient database error; retrying: {error}",chunk["id"]))
        conn.commit()
    add_event(job_id,"robustness_chunk_retry","Transient database error; robustness slice will retry.",level="warning",details={"chunk_id":chunk["id"],"attempt":attempts})
    return True


def _period_label(trade_date: date, dcfg: dict[str, Any]) -> str:
    discovery_end=date.fromisoformat(str(dcfg["discovery_end"]))
    return "discovery" if trade_date <= discovery_end else "validation"


def _store_chunk_rows(run_id: str, variant_key: str, chunk: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM ra_robustness_samples WHERE robustness_run_id=%s AND variant_key=%s AND trade_date=%s
                AND symbol_bucket >= %s AND symbol_bucket < %s""",
                (run_id,variant_key,chunk["trade_date"],chunk["bucket_start"],chunk["bucket_end"]))
            if rows:
                cur.executemany("""INSERT INTO ra_robustness_samples(
                    robustness_run_id,variant_key,symbol_bucket,symbol,bar_ts,trade_date,minute_of_day,liquidity_tier,price_group,gross_return_pct,mfe_pct,mae_pct
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    [(run_id,variant_key,r["symbol_bucket"],r["symbol"],r["bar_ts"],r["trade_date"],r["minute_of_day"],r.get("liquidity_tier"),r.get("price_group"),r.get("gross_return_pct"),r.get("mfe_pct"),r.get("mae_pct")) for r in rows])
            cur.execute("UPDATE ra_robustness_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s", (len(rows),chunk["id"]))
        conn.commit()


def _update_chunk_progress(job_id: str, run_id: str, chunk: dict[str, Any] | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE status='completed') AS done,
                count(*) FILTER (WHERE status<>'split') AS total FROM ra_robustness_chunks WHERE robustness_run_id=%s""", (run_id,))
            stats=cur.fetchone()
        conn.rollback()
    phase="robustness"
    if chunk:
        phase=f"robustness · {chunk['variant_key']} · {chunk['trade_date']} · buckets {chunk['bucket_start']}-{chunk['bucket_end']-1}"
    set_progress(job_id,phase,int(stats["done"] or 0),int(stats["total"] or 0))


def _run_chunks(job_id: str, run_id: str, candidate: dict[str, Any], config: RobustnessAnalysisConfig,
                target_id: str, dcfg: dict[str, Any], variants: dict[str, dict[str, Any]]) -> None:
    direction=str(candidate["direction"]); horizon=int(candidate["holding_horizon_minutes"])
    stride=int(candidate.get("entry_stride_minutes") or 1); anchor=int(candidate.get("entry_anchor_minute") or 570)
    use_discovery_samples = str(target_id) == str(candidate["feature_set_id"]) and config.mode == "development"
    while True:
        check_control(job_id)
        chunk=_claim_chunk(run_id)
        if not chunk:
            break
        spec=variants[chunk["variant_key"]]
        try:
            if use_discovery_samples:
                query,condition_params=_development_observation_query(spec["conditions"],direction,horizon,stride,anchor,int(spec["delay"]))
                params=(candidate["discovery_run_id"],_period_label(chunk["trade_date"],dcfg),chunk["trade_date"],chunk["bucket_start"],chunk["bucket_end"],*condition_params,target_id,int(spec["delay"]),horizon)
            else:
                query,condition_params=_observation_query(spec["conditions"],direction,horizon,stride,anchor,int(spec["delay"]))
                params=(target_id,chunk["trade_date"],chunk["trade_date"],chunk["trade_date"],chunk["bucket_start"],chunk["bucket_end"],*condition_params,int(spec["delay"]),horizon)
            rows=[dict(r) for r in (_execute_guarded(job_id,query,params,fetch="all",name=f"robust-v2-{chunk['id']}") or [])]
            _store_chunk_rows(run_id,chunk["variant_key"],chunk,rows)
            _update_chunk_progress(job_id,run_id,chunk)
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE ra_robustness_chunks SET status='pending' WHERE id=%s",(chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            if _is_timeout(exc) and _split_chunk(job_id,chunk,exc):
                _update_chunk_progress(job_id,run_id,chunk)
                continue
            if _retry_chunk(job_id,chunk,exc):
                continue
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE ra_robustness_chunks SET status='failed',error=%s WHERE id=%s",(str(exc),chunk["id"]))
                conn.commit()
            raise


def _load_variant_rows(run_id: str, variant_key: str) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol,bar_ts,trade_date,minute_of_day,liquidity_tier,price_group,gross_return_pct,mfe_pct,mae_pct
                FROM ra_robustness_samples WHERE robustness_run_id=%s AND variant_key=%s ORDER BY trade_date,symbol,bar_ts""", (run_id,variant_key))
            rows=cur.fetchall()
        conn.rollback()
    return [dict(r) for r in rows]


def run_robustness(job_id: str, config: RobustnessAnalysisConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.*,d.config discovery_config,sf.config source_feature_config,sf.status source_status
                FROM ra_candidate_rules c JOIN ra_discovery_runs d ON d.id=c.discovery_run_id
                JOIN ra_feature_sets sf ON sf.id=c.feature_set_id WHERE c.id=%s""", (config.candidate_id,))
            row=cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate does not exist")
    candidate=dict(row)
    if candidate.get("rule_definition_version") not in SUPPORTED_RULE_DEFINITIONS:
        raise ValueError("Candidate rule definition is too old for audited robustness analysis")
    dcfg=dict(candidate.get("discovery_config") or {})
    target_id=str(config.target_feature_set_id or candidate["feature_set_id"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=%s",(target_id,)); target=cur.fetchone()
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=%s",(candidate["feature_set_id"],)); source=cur.fetchone()
        conn.rollback()
    if not target or target["status"] != "completed":
        raise ValueError("Target feature set must be completed")
    _compatible(dict(source),dict(target),int(candidate["holding_horizon_minutes"]))

    development_start=date.fromisoformat(str(dcfg["discovery_start"]))
    development_end=date.fromisoformat(str(dcfg.get("validation_end") or dcfg["discovery_end"]))
    start=config.start_date or (development_start if config.mode=="development" else target["min_trade_date"])
    end=config.end_date or (development_end if config.mode=="development" else target["max_trade_date"])
    if start < target["min_trade_date"] or end > target["max_trade_date"]:
        raise ValueError("Robustness dates must remain inside the target feature set")
    if config.mode=="development":
        if start < development_start or end > development_end:
            raise ValueError("Development robustness must stay inside the original discovery/validation development period")
    elif not (end < development_start or start > development_end):
        raise ValueError("Historical holdout must not overlap the original discovery/validation development period")

    run_id=_ensure_run(job_id,candidate,target_id,config,start,end)
    conditions=[dict(x) for x in (candidate.get("conditions") or [])]
    variants=_variant_specs(config,conditions)
    dates=_dates(start,end)
    use_staged_development = str(target_id) == str(candidate["feature_set_id"]) and config.mode == "development"
    initial_shards = 1 if use_staged_development else int(get_settings().robustness_initial_symbol_shards)
    _initialise_chunks(run_id,dates,list(variants),initial_shards)
    _update_chunk_progress(job_id,run_id)
    _run_chunks(job_id,run_id,candidate,config,target_id,dcfg,variants)

    all_by_delay={int(d):_load_variant_rows(run_id,f"delay:{int(d)}") for d in config.entry_delays_minutes}
    neighbourhood_rows={
        "relaxed":_load_variant_rows(run_id,"neighbour:relaxed"),
        "tightened":_load_variant_rows(run_id,"neighbour:tightened"),
    }
    base_rows=all_by_delay.get(0,[])
    base_cost=float(dcfg.get("round_trip_cost_bps") or 20)
    summary={
        "robustness_version":ROBUSTNESS_VERSION,"engine_version":ROBUSTNESS_ENGINE_VERSION,
        "candidate_id":str(config.candidate_id),"source_feature_set_id":str(candidate["feature_set_id"]),
        "target_feature_set_id":str(target_id),"mode":config.mode,"start_date":start.isoformat(),"end_date":end.isoformat(),
        "base":_metrics(base_rows,base_cost),
        "cost_sensitivity":{str(int(c) if float(c).is_integer() else c):_metrics(base_rows,c) for c in config.round_trip_costs_bps},
        "entry_delay_sensitivity":{str(d):_metrics(rows,base_cost) for d,rows in all_by_delay.items()},
        "liquidity_tiers":_breakdown(base_rows,base_cost,"liquidity_tier"),
        "price_groups":_breakdown(base_rows,base_cost,"price_group"),
        "neighbourhood":{
            "relaxed":_metrics(neighbourhood_rows["relaxed"],base_cost),"exact":_metrics(base_rows,base_cost),
            "tightened":_metrics(neighbourhood_rows["tightened"],base_cost),"threshold_change_pct":config.neighbourhood_pct,
        },
    }
    verdict=_verdict(summary,config.mode); summary["verdict"]=verdict
    results={
        "cost_sensitivity":summary["cost_sensitivity"],"entry_delay":summary["entry_delay_sensitivity"],
        "liquidity_tier":summary["liquidity_tiers"],"price_group":summary["price_groups"],
        "date":_breakdown(base_rows,base_cost,"trade_date"),"month":_month_breakdown(base_rows,base_cost),
        "year":_year_breakdown(base_rows,base_cost),"symbol":_breakdown(base_rows,base_cost,"symbol"),
        "neighbourhood":summary["neighbourhood"],
    }
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_robustness_runs SET status='completed',observations=%s,verdict=%s,summary=%s,completed_at=now() WHERE id=%s",
                        (len(base_rows),verdict,Jsonb(json_safe(summary)),run_id))
            for result_type,values in results.items():
                for key,metrics in values.items():
                    cur.execute("""INSERT INTO ra_robustness_results(robustness_run_id,result_type,result_key,metrics) VALUES (%s,%s,%s,%s)
                        ON CONFLICT(robustness_run_id,result_type,result_key) DO UPDATE SET metrics=excluded.metrics""",
                        (run_id,result_type,str(key),Jsonb(json_safe(metrics))))
        conn.commit()
    add_event(job_id,"robustness_completed",f"Robustness analysis completed with verdict {verdict}.",details=summary)
    return {"robustness_run_id":run_id,"verdict":verdict,"observations":len(base_rows),"summary":summary}

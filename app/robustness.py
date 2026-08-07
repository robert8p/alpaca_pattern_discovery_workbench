from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.discovery import (
    SYMBOL_BUCKETS, _condition_sql, _execute_guarded,
)
from app.jobs import add_event, check_control, set_progress
from app.models import RobustnessAnalysisConfig
from app.sql_validation import validate_sql_bindings
from app.utils import ensure_feature_set_compatibility, finite_or_none, json_safe

ROBUSTNESS_VERSION = "1.0.0"
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
                strict_positive = op in {"gte", "gt"} and value > 0 or op in {"lte", "lt"} and value < 0
                strict_negative = op in {"lte", "lt"} and value > 0 or op in {"gte", "gt"} and value < 0
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


def _observation_query(
    conditions: list[dict[str, Any]], direction: str, horizon: int,
    stride: int, anchor: int, delay: int,
) -> tuple[str, tuple[Any, ...]]:
    where, condition_params = _condition_sql(conditions, alias="s")
    if direction not in {"long", "short"}:
        raise ValueError("Direction must be long or short")
    if horizon not in {5, 15, 30, 60}:
        raise ValueError("Unsupported holding horizon")
    sampling = "TRUE" if stride == 1 else f"mod(s.minute_of_day - {int(anchor)}, {int(stride)}) = 0"
    outcome = f"e.fwd_return_{horizon}m_pct"
    gross = outcome if direction == "long" else f"-{outcome}"
    if direction == "long":
        mfe = "(exc.max_high/NULLIF(e.close,0)-1)*100"
        mae = "(exc.min_low/NULLIF(e.close,0)-1)*100"
    else:
        mfe = "-(exc.min_low/NULLIF(e.close,0)-1)*100"
        mae = "-(exc.max_high/NULLIF(e.close,0)-1)*100"
    query = f"""
        WITH source AS MATERIALIZED (
            SELECT f.*,
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
              AND f.trade_date=%s
        ), signal AS MATERIALIZED (
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
        )
        SELECT s.symbol,s.bar_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,
            CASE WHEN COALESCE(e.close,s.close) < 5 THEN 'lt_5' WHEN COALESCE(e.close,s.close) < 10 THEN '5_10'
                 WHEN COALESCE(e.close,s.close) < 25 THEN '10_25' WHEN COALESCE(e.close,s.close) < 100 THEN '25_100' ELSE 'ge_100' END AS price_group,
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
        WHERE ({sampling})
          AND ({where})
        ORDER BY s.trade_date,s.symbol,s.bar_ts
    """
    params = ("feature", date.today(), delay, horizon, *condition_params)
    validate_sql_bindings(query, params, name="robustness observation query")
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


def _ensure_run(job_id: str, candidate: dict[str, Any], target_feature_set_id: Any,
                config: RobustnessAnalysisConfig, start: date, end: date) -> str:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_robustness_runs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                run_id = str(row["id"])
                cur.execute("DELETE FROM ra_robustness_observations WHERE robustness_run_id=%s", (run_id,))
                cur.execute("DELETE FROM ra_robustness_results WHERE robustness_run_id=%s", (run_id,))
                cur.execute("UPDATE ra_robustness_runs SET status='running',summary=NULL,verdict=NULL,completed_at=NULL,config=%s,start_date=%s,end_date=%s,target_feature_set_id=%s WHERE id=%s",
                            (Jsonb(config.model_dump(mode="json")), start, end, target_feature_set_id, run_id))
            else:
                cur.execute("""INSERT INTO ra_robustness_runs(job_id,candidate_id,source_feature_set_id,target_feature_set_id,mode,config,start_date,end_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (job_id,candidate["id"],candidate["feature_set_id"],target_feature_set_id,config.mode,Jsonb(config.model_dump(mode="json")),start,end))
                run_id = str(cur.fetchone()["id"])
        conn.commit()
    return run_id


def run_robustness(job_id: str, config: RobustnessAnalysisConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.*,d.config discovery_config,sf.config source_feature_config,sf.status source_status
                FROM ra_candidate_rules c JOIN ra_discovery_runs d ON d.id=c.discovery_run_id
                JOIN ra_feature_sets sf ON sf.id=c.feature_set_id WHERE c.id=%s""", (config.candidate_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate does not exist")
    candidate = dict(row)
    if candidate.get("rule_definition_version") not in SUPPORTED_RULE_DEFINITIONS:
        raise ValueError("Candidate rule definition is too old for audited robustness analysis")
    dcfg = dict(candidate.get("discovery_config") or {})
    target_id = config.target_feature_set_id or candidate["feature_set_id"]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=%s", (target_id,))
            target = cur.fetchone()
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=%s", (candidate["feature_set_id"],))
            source = cur.fetchone()
        conn.rollback()
    if not target or target["status"] != "completed":
        raise ValueError("Target feature set must be completed")
    _compatible(dict(source), dict(target), int(candidate["holding_horizon_minutes"]))

    development_start = date.fromisoformat(str(dcfg["discovery_start"]))
    development_end = date.fromisoformat(str(dcfg.get("validation_end") or dcfg["discovery_end"]))
    start = config.start_date or (development_start if config.mode == "development" else target["min_trade_date"])
    end = config.end_date or (development_end if config.mode == "development" else target["max_trade_date"])
    if start < target["min_trade_date"] or end > target["max_trade_date"]:
        raise ValueError("Robustness dates must remain inside the target feature set")
    if config.mode == "development":
        if start < development_start or end > development_end:
            raise ValueError("Development robustness must stay inside the original discovery/validation development period")
    else:
        if not (end < development_start or start > development_end):
            raise ValueError("Historical holdout must not overlap the original discovery/validation development period")

    run_id = _ensure_run(job_id, candidate, target_id, config, start, end)
    direction = str(candidate["direction"]); horizon = int(candidate["holding_horizon_minutes"])
    stride = int(candidate.get("entry_stride_minutes") or 1); anchor = int(candidate.get("entry_anchor_minute") or 570)
    conditions = [dict(x) for x in (candidate.get("conditions") or [])]
    base_cost = float(dcfg.get("round_trip_cost_bps") or 20)
    dates = _dates(start, end)
    total = len(dates) * len(config.entry_delays_minutes) + len(dates) * 2
    progress = 0
    all_by_delay: dict[int, list[dict[str, Any]]] = {d: [] for d in config.entry_delays_minutes}

    for delay in config.entry_delays_minutes:
        query, condition_params = _observation_query(conditions, direction, horizon, stride, anchor, delay)
        for trade_date in dates:
            check_control(job_id)
            params = (target_id, trade_date, delay, horizon, *condition_params)
            rows = [dict(r) for r in (_execute_guarded(job_id, query, params, fetch="all", name=f"robust-{delay}-{trade_date}") or [])]
            all_by_delay[delay].extend(rows)
            if delay == 0 and rows:
                valid_observations = [r for r in rows if r.get("gross_return_pct") is not None]
                if valid_observations:
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.executemany("""INSERT INTO ra_robustness_observations(
                                robustness_run_id,delay_minutes,symbol,bar_ts,trade_date,minute_of_day,liquidity_tier,price_group,gross_return_pct
                            ) VALUES (%s,0,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                                [(run_id,r["symbol"],r["bar_ts"],r["trade_date"],r["minute_of_day"],r.get("liquidity_tier"),r.get("price_group"),r["gross_return_pct"]) for r in valid_observations])
                        conn.commit()
            progress += 1
            set_progress(job_id, f"robustness · delay {delay}m · {trade_date}", progress, total)

    neighbourhood_rows: dict[str, list[dict[str, Any]]] = {}
    for mode in ("relaxed", "tightened"):
        altered = _perturb_conditions(conditions, config.neighbourhood_pct, mode)
        query, condition_params = _observation_query(altered, direction, horizon, stride, anchor, 0)
        collected: list[dict[str, Any]] = []
        for trade_date in dates:
            check_control(job_id)
            params = (target_id, trade_date, 0, horizon, *condition_params)
            collected.extend(dict(r) for r in (_execute_guarded(job_id, query, params, fetch="all", name=f"neighbour-{mode}-{trade_date}") or []))
            progress += 1
            set_progress(job_id, f"robustness · {mode} thresholds · {trade_date}", progress, total)
        neighbourhood_rows[mode] = collected

    base_rows = all_by_delay.get(0, [])
    summary = {
        "robustness_version": ROBUSTNESS_VERSION,
        "candidate_id": str(config.candidate_id),
        "source_feature_set_id": str(candidate["feature_set_id"]),
        "target_feature_set_id": str(target_id),
        "mode": config.mode,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "base": _metrics(base_rows, base_cost),
        "cost_sensitivity": {str(int(c) if float(c).is_integer() else c): _metrics(base_rows, c) for c in config.round_trip_costs_bps},
        "entry_delay_sensitivity": {str(d): _metrics(rows, base_cost) for d, rows in all_by_delay.items()},
        "liquidity_tiers": _breakdown(base_rows, base_cost, "liquidity_tier"),
        "price_groups": _breakdown(base_rows, base_cost, "price_group"),
        "neighbourhood": {
            "relaxed": _metrics(neighbourhood_rows["relaxed"], base_cost),
            "exact": _metrics(base_rows, base_cost),
            "tightened": _metrics(neighbourhood_rows["tightened"], base_cost),
            "threshold_change_pct": config.neighbourhood_pct,
        },
    }
    verdict = _verdict(summary, config.mode)
    summary["verdict"] = verdict

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_robustness_runs SET status='completed',observations=%s,verdict=%s,summary=%s,completed_at=now() WHERE id=%s",
                        (len(base_rows), verdict, Jsonb(json_safe(summary)), run_id))
            results = {
                "cost_sensitivity": summary["cost_sensitivity"], "entry_delay": summary["entry_delay_sensitivity"],
                "liquidity_tier": summary["liquidity_tiers"], "price_group": summary["price_groups"],
                "date": _breakdown(base_rows, base_cost, "trade_date"),
                "month": _month_breakdown(base_rows, base_cost),
                "year": _year_breakdown(base_rows, base_cost),
                "symbol": _breakdown(base_rows, base_cost, "symbol"),
                "neighbourhood": summary["neighbourhood"], "summary": summary["base"],
            }
            for result_type, payload in results.items():
                for key, value in (payload.items() if isinstance(payload, dict) and result_type not in {"summary"} else [("all", payload)]):
                    cur.execute("INSERT INTO ra_robustness_results(robustness_run_id,result_type,result_key,metrics) VALUES (%s,%s,%s,%s) ON CONFLICT(robustness_run_id,result_type,result_key) DO UPDATE SET metrics=excluded.metrics",
                                (run_id, result_type, str(key), Jsonb(json_safe(value))))
        conn.commit()
    add_event(job_id, "robustness_completed", f"Robustness analysis completed with verdict {verdict}.", details=summary)
    result = {"robustness_run_id": run_id, "verdict": verdict, "observations": len(base_rows), "summary": summary}
    set_progress(job_id, "complete", 1, 1, result=result)
    return json_safe(result)

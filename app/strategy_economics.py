from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, median
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import connection
from app.discovery import _condition_sql
from app.jobs import add_event, check_control, set_progress
from app.models import StrategyEconomicsConfig
from app.research_ledger import freeze_candidate, sync_candidate_ledger
from app.research_policy import SEALED_START_DATE
from app.utils import finite_or_none, json_safe

STRATEGY_ECONOMICS_VERSION = "1.0.0"
_SAFE_STRENGTH_FIELDS = {
    "ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "ret_60m_pct",
    "relative_volume_20bar", "relative_trade_count_20bar", "activity_impact_change_ratio",
    "distance_from_cumulative_vwap_pct", "cumulative_range_position",
}


def _quantile(values: list[float], q: float) -> float | None:
    vals = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    weight = pos - lo
    return vals[lo] * (1 - weight) + vals[hi] * weight


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    return wins / losses if losses > 0 else (None if wins <= 0 else float("inf"))


def _skew(values: list[float]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    m = mean(values)
    m2 = sum((x - m) ** 2 for x in values) / n
    if m2 <= 0:
        return 0.0
    m3 = sum((x - m) ** 3 for x in values) / n
    return m3 / (m2 ** 1.5)


def _kurtosis(values: list[float]) -> float | None:
    n = len(values)
    if n < 4:
        return None
    m = mean(values)
    m2 = sum((x - m) ** 2 for x in values) / n
    if m2 <= 0:
        return 0.0
    m4 = sum((x - m) ** 4 for x in values) / n
    return m4 / (m2 ** 2) - 3.0


def _top_return_share(values: list[float], pct: float) -> float | None:
    total = sum(values)
    if total <= 0 or not values:
        return None
    n = max(1, math.ceil(len(values) * pct / 100.0))
    return 100.0 * sum(sorted(values, reverse=True)[:n]) / total


def _expected_shortfall(values: list[float], tail_q: float) -> float | None:
    threshold = _quantile(values, tail_q)
    if threshold is None:
        return None
    tail = [v for v in values if v <= threshold]
    return mean(tail) if tail else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    ax = mean(x for x, _ in pairs)
    ay = mean(y for _, y in pairs)
    sx = sum((x-ax)**2 for x, _ in pairs)
    sy = sum((y-ay)**2 for _, y in pairs)
    if sx <= 0 or sy <= 0:
        return None
    return finite_or_none(sum((x-ax)*(y-ay) for x, y in pairs) / math.sqrt(sx*sy))


def _compound_pct(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    for value in values:
        wealth *= max(0.0, 1.0 + float(value)/100.0)
    return (wealth-1.0)*100.0


def _losing_streak(values: list[float]) -> int:
    best = current = 0
    for value in values:
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _icc(values_by_group: dict[Any, list[float]]) -> float | None:
    groups = [vals for vals in values_by_group.values() if vals]
    n = sum(len(v) for v in groups)
    k = len(groups)
    if k < 2 or n <= k:
        return None
    all_values = [x for group in groups for x in group]
    overall = mean(all_values)
    ss_between = sum(len(group) * (mean(group) - overall) ** 2 for group in groups)
    ss_within = sum(sum((x - mean(group)) ** 2 for x in group) for group in groups)
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n - k) if n > k else 0.0
    avg_n = n / k
    denom = ms_between + (avg_n - 1) * ms_within
    if denom == 0:
        return 0.0
    return finite_or_none((ms_between - ms_within) / denom)


def _effective_event_count(group_sizes: list[int]) -> float | None:
    if not group_sizes:
        return None
    total = sum(group_sizes)
    denom = sum(x * x for x in group_sizes)
    return total * total / denom if denom else None


def _strategy_payload(candidate: dict[str, Any], config: StrategyEconomicsConfig) -> dict[str, Any]:
    return {
        "candidate": {
            "candidate_id": str(candidate["id"]),
            "family": candidate["family"],
            "direction": candidate["direction"],
            "holding_horizon_minutes": int(candidate["holding_horizon_minutes"]),
            "conditions": candidate["conditions"],
            "entry_sampling_mode": candidate.get("entry_sampling_mode"),
            "entry_stride_minutes": int(candidate.get("entry_stride_minutes") or 1),
            "entry_anchor_minute": int(candidate.get("entry_anchor_minute") or 570),
            "rule_definition_version": candidate.get("rule_definition_version"),
        },
        "execution": {
            "base_entry_delay_minutes": config.base_entry_delay_minutes,
            "base_round_trip_cost_bps": config.base_round_trip_cost_bps,
            "commission_bps": config.commission_bps,
            "spread_bps": config.spread_bps,
            "slippage_bps": config.slippage_bps,
            "market_impact_bps": config.market_impact_bps,
            "borrow_bps": config.borrow_bps,
            "funding_bps": config.funding_bps,
            "position_sizing_method": config.position_sizing_method,
            "position_size_pct_of_capital": config.position_size_pct_of_capital,
            "max_positions": config.max_positions,
            "max_gross_exposure_pct": config.max_gross_exposure_pct,
            "max_net_exposure_pct": config.max_net_exposure_pct,
            "max_symbol_exposure_pct": config.max_symbol_exposure_pct,
            "max_sector_exposure_pct": config.max_sector_exposure_pct,
            "signal_priority": config.signal_priority,
            "one_position_per_symbol": config.one_position_per_symbol,
            "allow_partial_fills": config.allow_partial_fills,
            "min_fill_fraction": config.min_fill_fraction,
            "max_bar_participation_pct": config.max_bar_participation_pct,
            "max_daily_participation_pct": config.max_daily_participation_pct,
            "signal_strength_field": config.signal_strength_field,
            "breakeven_tolerance_bps": config.breakeven_tolerance_bps,
            "large_loss_threshold_pct": config.large_loss_threshold_pct,
            "max_acceptable_drawdown_pct": config.max_acceptable_drawdown_pct,
        },
    }


def strategy_config_hash(candidate: dict[str, Any], config: StrategyEconomicsConfig) -> str:
    payload = _strategy_payload(candidate, config)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_candidate(candidate_id: UUID | str) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*,f.config AS feature_config,f.universe_run_id,f.status AS feature_status
                FROM ra_candidate_rules c JOIN ra_feature_sets f ON f.id=c.feature_set_id
                WHERE c.id=%s
                """,
                (candidate_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate does not exist")
    return dict(row)


def _load_feature_set(feature_set_id: UUID | str) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=%s", (feature_set_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row or row["status"] != "completed":
        raise ValueError("Strategy economics requires a completed feature set")
    return dict(row)


def _signal_query(candidate: dict[str, Any], config: StrategyEconomicsConfig, delay_minutes: int, include_path: bool) -> tuple[str, tuple[Any, ...]]:
    where, condition_params = _condition_sql(candidate["conditions"], alias="s")
    horizon = int(candidate["holding_horizon_minutes"])
    stride = max(1, int(candidate.get("entry_stride_minutes") or 1))
    anchor = int(candidate.get("entry_anchor_minute") or 570)
    strength = f"s.{config.signal_strength_field}" if config.signal_strength_field in _SAFE_STRENGTH_FIELDS else "NULL::double precision"
    path_cols = "path.max_high,path.min_low" if include_path else "NULL::double precision AS max_high,NULL::double precision AS min_low"
    path_join = """
        LEFT JOIN LATERAL (
            SELECT max(b.high)::double precision AS max_high,min(b.low)::double precision AS min_low
            FROM rd_bars b
            WHERE b.symbol=s.symbol AND b.timeframe='1Min' AND b.feed='sip' AND b.adjustment='raw'
              AND b.session_label='regular' AND en.bar_ts IS NOT NULL AND ex.bar_ts IS NOT NULL
              AND b.bar_ts BETWEEN en.bar_ts AND ex.bar_ts
        ) path ON true
    """ if include_path else ""
    sql = f"""
        WITH source AS MATERIALIZED (
            SELECT f.*,
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
            WHERE f.feature_set_id=%s AND f.trade_date BETWEEN %s AND %s
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
            WHERE mod((s.minute_of_day-%s)::integer,%s)=0 AND ({where})
        )
        SELECT s.symbol,s.bar_ts AS signal_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,
               {strength} AS signal_strength,
               en.bar_ts AS entry_ts,en.close AS entry_price,en.bar_dollar_volume AS entry_bar_dollar_volume,
               ex.bar_ts AS exit_ts,ex.close AS exit_price,
               a.exchange,COALESCE(a.attributes->>'sector',a.raw->>'sector') AS sector,
               CASE WHEN a.raw ? 'shortable' THEN NULLIF(a.raw->>'shortable','')::boolean END AS current_reference_shortable,
               CASE WHEN pts.status='completed' AND pts.lookback_end < s.trade_date THEN au.median_daily_dollar_volume END::double precision AS daily_dollar_volume,
               CASE WHEN pts.status='completed' AND pts.lookback_end < s.trade_date THEN 'point_in_time_universe_t_minus_1' ELSE 'entry_bar_only_no_point_in_time_daily_capacity' END AS liquidity_metadata_temporal_status,
               {path_cols}
        FROM signal s
        LEFT JOIN ra_intraday_features en
          ON en.feature_set_id=s.feature_set_id AND en.symbol=s.symbol
         AND en.bar_ts=s.bar_ts+(%s*interval '1 minute') AND en.trade_date=s.trade_date
        LEFT JOIN ra_intraday_features ex
          ON ex.feature_set_id=s.feature_set_id AND ex.symbol=s.symbol
         AND ex.bar_ts=en.bar_ts+(%s*interval '1 minute') AND ex.trade_date=s.trade_date
        LEFT JOIN rd_assets a ON a.symbol=s.symbol
        LEFT JOIN ra_feature_chunks fc
          ON fc.feature_set_id=s.feature_set_id AND s.trade_date BETWEEN fc.chunk_start AND fc.chunk_end
        LEFT JOIN ra_feature_chunk_universes fcu ON fcu.feature_chunk_id=fc.id
        LEFT JOIN ra_point_in_time_universe_snapshots pts
          ON pts.id=fcu.point_in_time_snapshot_id AND s.trade_date BETWEEN pts.effective_start AND pts.effective_end
        LEFT JOIN ra_analysis_universe au
          ON au.universe_run_id=fcu.universe_run_id AND au.symbol=s.symbol AND au.included
        {path_join}
        ORDER BY s.bar_ts,s.symbol
    """
    params: tuple[Any, ...] = (
        config.target_feature_set_id, config.start_date, config.end_date,
        anchor, stride, *condition_params, delay_minutes, horizon,
    )
    return sql, params

def _fetch_signals(candidate: dict[str, Any], config: StrategyEconomicsConfig, delay_minutes: int, include_path: bool) -> list[dict[str, Any]]:
    sql, params = _signal_query(candidate, config, delay_minutes, include_path)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='120s'")
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
    direction_sign = 1.0 if candidate["direction"] == "long" else -1.0
    for row in rows:
        entry = finite_or_none(row.get("entry_price"))
        exit_price = finite_or_none(row.get("exit_price"))
        if entry and exit_price:
            row["gross_return_pct"] = direction_sign * (exit_price / entry - 1.0) * 100.0
        else:
            row["gross_return_pct"] = None
        if include_path and entry:
            max_high = finite_or_none(row.get("max_high"))
            min_low = finite_or_none(row.get("min_low"))
            if candidate["direction"] == "long":
                row["mfe_pct"] = (max_high / entry - 1.0) * 100.0 if max_high else None
                row["mae_pct"] = (min_low / entry - 1.0) * 100.0 if min_low else None
            else:
                row["mfe_pct"] = (1.0 - min_low / entry) * 100.0 if min_low else None
                row["mae_pct"] = (1.0 - max_high / entry) * 100.0 if max_high else None
        else:
            row["mfe_pct"] = row["mae_pct"] = None
    return rows


def _simulate(signals: list[dict[str, Any]], candidate: dict[str, Any], config: StrategyEconomicsConfig, capital: float, cost_bps: float) -> list[dict[str, Any]]:
    open_positions: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    direction_sign = 1 if candidate["direction"] == "long" else -1
    def priority(row: dict[str, Any]) -> tuple[Any, ...]:
        if config.signal_priority == "signal_strength_desc":
            return (row["signal_ts"], -(abs(float(row.get("signal_strength") or 0))), -(float(row.get("daily_dollar_volume") or 0)), row["symbol"])
        if config.signal_priority == "symbol":
            return (row["signal_ts"], row["symbol"])
        return (row["signal_ts"], -(float(row.get("daily_dollar_volume") or 0)), row["symbol"])

    for signal in sorted(signals, key=priority):
        entry_ts = signal.get("entry_ts")
        exit_ts = signal.get("exit_ts")
        row = dict(signal)
        row["direction"] = candidate["direction"]
        row.update({"accepted": False, "rejection_reason": None, "metadata_temporal_status": signal.get("liquidity_metadata_temporal_status") or "entry_bar_only_no_point_in_time_daily_capacity"})
        if entry_ts is None or row.get("entry_price") is None:
            row["rejection_reason"] = "unavailable_entry_fill"
            accepted.append(row)
            continue
        if exit_ts is None or row.get("exit_price") is None or row.get("gross_return_pct") is None:
            row["rejection_reason"] = "unavailable_exit_fill"
            accepted.append(row)
            continue
        open_positions = [p for p in open_positions if p["exit_ts"] > entry_ts]
        if config.one_position_per_symbol and any(p["symbol"] == row["symbol"] for p in open_positions):
            row["rejection_reason"] = "existing_symbol_position"
            accepted.append(row)
            continue
        if len(open_positions) >= config.max_positions:
            row["rejection_reason"] = "max_positions"
            accepted.append(row)
            continue
        gross_open = sum(float(p["filled_notional"]) for p in open_positions)
        net_open = sum((1 if p["direction"] == "long" else -1) * float(p["filled_notional"]) for p in open_positions)
        gross_available = max(0.0, capital * config.max_gross_exposure_pct / 100.0 - gross_open)
        if direction_sign > 0:
            net_available = max(0.0, capital * config.max_net_exposure_pct / 100.0 - net_open)
        else:
            net_available = max(0.0, capital * config.max_net_exposure_pct / 100.0 + net_open)
        symbol_limit = capital * config.max_symbol_exposure_pct / 100.0
        desired = capital * config.position_size_pct_of_capital / 100.0
        bar_cap = float(row.get("entry_bar_dollar_volume") or 0) * config.max_bar_participation_pct / 100.0
        daily_cap = float(row.get("daily_dollar_volume") or 0) * config.max_daily_participation_pct / 100.0
        positive_caps = [x for x in (bar_cap, daily_cap) if x > 0]
        capacity = min(positive_caps) if positive_caps else 0.0
        filled = min(desired, gross_available, net_available, symbol_limit, capacity)
        row["desired_notional"] = desired
        row["capacity_notional"] = capacity
        row["filled_notional"] = max(filled, 0.0)
        row["fill_fraction"] = filled / desired if desired > 0 else 0.0
        if capacity <= 0:
            row["rejection_reason"] = "liquidity_capacity_unavailable"
            accepted.append(row)
            continue
        if not config.allow_partial_fills and filled + 1e-9 < desired:
            row["rejection_reason"] = "partial_fill_not_allowed"
            accepted.append(row)
            continue
        if filled <= 0 or row["fill_fraction"] < config.min_fill_fraction:
            row["rejection_reason"] = "minimum_fill_fraction"
            accepted.append(row)
            continue
        row["round_trip_cost_bps"] = cost_bps
        row["estimated_cost_value"] = filled * cost_bps / 10000.0
        row["net_return_pct"] = float(row["gross_return_pct"]) - cost_bps / 100.0
        row["accepted"] = True
        row["rejection_reason"] = None
        open_positions.append(row)
        accepted.append(row)
    return accepted


def _trade_metrics(rows: list[dict[str, Any]], capital: float, config: StrategyEconomicsConfig) -> dict[str, Any]:
    trades = [r for r in rows if r.get("accepted")]
    net = [float(r["net_return_pct"]) for r in trades if r.get("net_return_pct") is not None]
    gross = [float(r["gross_return_pct"]) for r in trades if r.get("gross_return_pct") is not None]
    winners = [v for v in net if v > config.breakeven_tolerance_bps / 100.0]
    losers = [v for v in net if v < -config.breakeven_tolerance_bps / 100.0]
    breakeven = len(net) - len(winners) - len(losers)
    maes = [float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None]
    mfes = [float(r["mfe_pct"]) for r in trades if r.get("mfe_pct") is not None]
    pnl = [float(r["filled_notional"]) * float(r["net_return_pct"]) / 100.0 for r in trades]
    gross_pnl = [float(r["filled_notional"]) * float(r["gross_return_pct"]) / 100.0 for r in trades]
    costs = [float(r.get("estimated_cost_value") or 0) for r in trades]
    by_event: dict[Any, list[float]] = defaultdict(list)
    by_day: dict[Any, list[float]] = defaultdict(list)
    by_sector_day: dict[Any, list[float]] = defaultdict(list)
    for r in trades:
        value = float(r["net_return_pct"])
        by_event[r["signal_ts"]].append(value)
        by_day[r["trade_date"]].append(value)
        if r.get("sector"):
            by_sector_day[(r["trade_date"], r["sector"])].append(value)
    event_sizes = [len(v) for v in by_event.values()]
    daily_dv = [float(r["daily_dollar_volume"]) for r in trades if r.get("daily_dollar_volume")]
    capacities = [float(r["capacity_notional"]) for r in trades if r.get("capacity_notional")]
    filled = [float(r["filled_notional"]) for r in trades if r.get("filled_notional")]
    rejected = len(rows) - len(trades)
    return {
        "signals": len(rows), "trades": len(trades), "rejected_signals": rejected,
        "fill_rate_pct": 100.0 * len(trades) / len(rows) if rows else None,
        "gross_expected_value_pct": mean(gross) if gross else None,
        "net_expected_value_pct": mean(net) if net else None,
        "mean_net_trade_return_pct": mean(net) if net else None,
        "median_net_trade_return_pct": median(net) if net else None,
        "trade_win_rate_pct": 100.0 * len(winners) / len(net) if net else None,
        "loss_rate_pct": 100.0 * len(losers) / len(net) if net else None,
        "breakeven_rate_pct": 100.0 * breakeven / len(net) if net else None,
        "average_winner_pct": mean(winners) if winners else None,
        "median_winner_pct": median(winners) if winners else None,
        "average_loser_pct": mean(losers) if losers else None,
        "median_loser_pct": median(losers) if losers else None,
        "win_loss_magnitude_ratio": abs(mean(winners) / mean(losers)) if winners and losers and mean(losers) != 0 else None,
        "profit_factor": _profit_factor(net),
        "p05_pct": _quantile(net, .05), "p10_pct": _quantile(net, .10), "p25_pct": _quantile(net, .25),
        "p50_pct": _quantile(net, .50), "p75_pct": _quantile(net, .75), "p90_pct": _quantile(net, .90), "p95_pct": _quantile(net, .95),
        "p01_pct": _quantile(net, .01), "p99_pct": _quantile(net, .99),
        "skewness": _skew(net), "excess_kurtosis": _kurtosis(net),
        "worst_trade_pct": min(net) if net else None, "best_trade_pct": max(net) if net else None,
        "adverse_var_95_pct": _quantile(net,.05), "adverse_var_99_pct": _quantile(net,.01),
        "expected_shortfall_95_pct": _expected_shortfall(net,.05),
        "expected_shortfall_99_pct": _expected_shortfall(net,.01),
        "large_loss_frequency_pct": 100.0 * sum(v <= config.large_loss_threshold_pct for v in net) / len(net) if net else None,
        "mae_mean_pct": mean(maes) if maes else None, "mae_median_pct": median(maes) if maes else None,
        "mae_p05_pct": _quantile(maes,.05), "mae_p01_pct": _quantile(maes,.01),
        "mfe_mean_pct": mean(mfes) if mfes else None, "mfe_median_pct": median(mfes) if mfes else None,
        "mae_mean_winners_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])>0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])>0 for r in trades) else None,
        "mae_mean_losers_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])<0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])<0 for r in trades) else None,
        "mae_final_outcome_correlation": _pearson(
            [float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and r.get("net_return_pct") is not None],
            [float(r["net_return_pct"]) for r in trades if r.get("mae_pct") is not None and r.get("net_return_pct") is not None],
        ),
        "top_1pct_return_share_pct": _top_return_share(net,1), "top_5pct_return_share_pct": _top_return_share(net,5), "top_10pct_return_share_pct": _top_return_share(net,10),
        "gross_pnl": sum(gross_pnl), "net_pnl": sum(pnl), "estimated_costs": sum(costs),
        "fraction_gross_alpha_consumed_by_costs_pct": 100.0*sum(costs)/sum(gross_pnl) if sum(gross_pnl)>0 else None,
        "median_daily_dollar_volume": median(daily_dv) if daily_dv else None,
        "minimum_daily_dollar_volume": min(daily_dv) if daily_dv else None,
        "median_trade_capacity": median(capacities) if capacities else None,
        "minimum_trade_capacity": min(capacities) if capacities else None,
        "median_filled_notional": median(filled) if filled else None,
        "same_timestamp_outcome_icc": _icc(by_event), "same_day_outcome_icc": _icc(by_day),
        "same_sector_day_outcome_icc": _icc(by_sector_day) if by_sector_day else None,
        "independent_event_count": len(by_event), "effective_independent_event_count": _effective_event_count(event_sizes),
        "average_trades_per_event": mean(event_sizes) if event_sizes else None, "max_trades_per_event": max(event_sizes) if event_sizes else None,
        "rejection_reason_counts": dict(__import__("collections").Counter(str(r.get("rejection_reason")) for r in rows if not r.get("accepted"))),
        "capital_level": capital,
    }


def _insert_trades(run_id: str, capital: float, rows: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ra_strategy_trades WHERE strategy_run_id=%s AND capital_level=%s", (run_id, capital))
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO ra_strategy_trades(
                        strategy_run_id,capital_level,signal_ts,entry_ts,exit_ts,trade_date,symbol,direction,liquidity_tier,
                        exchange,sector,signal_strength,entry_price,exit_price,gross_return_pct,net_return_pct,mae_pct,mfe_pct,
                        desired_notional,filled_notional,fill_fraction,capacity_notional,entry_bar_dollar_volume,daily_dollar_volume,
                        round_trip_cost_bps,estimated_cost_value,accepted,rejection_reason,metadata_temporal_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (strategy_run_id,capital_level,signal_ts,symbol) DO UPDATE SET
                        entry_ts=excluded.entry_ts,exit_ts=excluded.exit_ts,entry_price=excluded.entry_price,exit_price=excluded.exit_price,
                        gross_return_pct=excluded.gross_return_pct,net_return_pct=excluded.net_return_pct,mae_pct=excluded.mae_pct,mfe_pct=excluded.mfe_pct,
                        desired_notional=excluded.desired_notional,filled_notional=excluded.filled_notional,fill_fraction=excluded.fill_fraction,
                        capacity_notional=excluded.capacity_notional,round_trip_cost_bps=excluded.round_trip_cost_bps,
                        estimated_cost_value=excluded.estimated_cost_value,accepted=excluded.accepted,rejection_reason=excluded.rejection_reason
                    """,
                    (
                        run_id,capital,r["signal_ts"],r.get("entry_ts"),r.get("exit_ts"),r["trade_date"],r["symbol"],candidate["direction"],r.get("liquidity_tier"),
                        r.get("exchange"),r.get("sector"),r.get("signal_strength"),r.get("entry_price"),r.get("exit_price"),r.get("gross_return_pct"),r.get("net_return_pct"),r.get("mae_pct"),r.get("mfe_pct"),
                        r.get("desired_notional"),r.get("filled_notional"),r.get("fill_fraction"),r.get("capacity_notional"),r.get("entry_bar_dollar_volume"),r.get("daily_dollar_volume"),
                        r.get("round_trip_cost_bps"),r.get("estimated_cost_value"),bool(r.get("accepted")),r.get("rejection_reason"),r.get("metadata_temporal_status"),
                    ),
                )
        conn.commit()


def _build_equity(run_id: str, feature_set_id: UUID | str, capital: float, start_date: date, end_date: date) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ra_strategy_equity_points WHERE strategy_run_id=%s AND capital_level=%s", (run_id,capital))
            cur.execute(
                """
                INSERT INTO ra_strategy_equity_points(
                    strategy_run_id,capital_level,bar_ts,trade_date,equity,realised_pnl,open_pnl,gross_exposure,net_exposure,open_positions
                )
                WITH grid AS MATERIALIZED (
                    SELECT bar_ts,min(trade_date) trade_date
                    FROM ra_intraday_features
                    WHERE feature_set_id=%s AND trade_date BETWEEN %s AND %s
                    GROUP BY bar_ts
                ), exits AS (
                    SELECT exit_ts AS bar_ts,sum(filled_notional::numeric*net_return_pct/100.0) pnl
                    FROM ra_strategy_trades
                    WHERE strategy_run_id=%s AND capital_level=%s AND accepted AND exit_ts IS NOT NULL
                    GROUP BY exit_ts
                ), realised AS (
                    SELECT g.bar_ts,g.trade_date,
                           sum(COALESCE(e.pnl,0)) OVER (ORDER BY g.bar_ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) realised_pnl
                    FROM grid g LEFT JOIN exits e USING(bar_ts)
                ), open_state AS (
                    SELECT g.bar_ts,
                           COALESCE(sum(CASE WHEN t.direction='long' THEN t.filled_notional::numeric*(b.close/t.entry_price-1.0)
                                             ELSE t.filled_notional::numeric*(1.0-b.close/t.entry_price) END
                                        - t.estimated_cost_value),0) AS open_pnl,
                           COALESCE(sum(t.filled_notional),0) AS gross_exposure,
                           COALESCE(sum(CASE WHEN t.direction='long' THEN t.filled_notional ELSE -t.filled_notional END),0) AS net_exposure,
                           count(t.symbol)::integer AS open_positions
                    FROM grid g
                    LEFT JOIN ra_strategy_trades t
                      ON t.strategy_run_id=%s AND t.capital_level=%s AND t.accepted
                     AND g.bar_ts>=t.entry_ts AND g.bar_ts<t.exit_ts
                    LEFT JOIN rd_bars b
                      ON b.symbol=t.symbol AND b.bar_ts=g.bar_ts AND b.timeframe='1Min' AND b.feed='sip' AND b.adjustment='raw' AND b.session_label='regular'
                    GROUP BY g.bar_ts
                )
                SELECT %s,%s,r.bar_ts,r.trade_date,%s+r.realised_pnl+o.open_pnl,r.realised_pnl,o.open_pnl,
                       o.gross_exposure,o.net_exposure,o.open_positions
                FROM realised r JOIN open_state o USING(bar_ts)
                ORDER BY r.bar_ts
                """,
                (feature_set_id,start_date,end_date,run_id,capital,run_id,capital,run_id,capital,capital),
            )
            cur.execute(
                """
                WITH peaks AS (
                    SELECT bar_ts,equity,max(equity) OVER (ORDER BY bar_ts) peak
                    FROM ra_strategy_equity_points WHERE strategy_run_id=%s AND capital_level=%s
                )
                UPDATE ra_strategy_equity_points e SET drawdown_pct=(p.equity/NULLIF(p.peak,0)-1)*100
                FROM peaks p WHERE e.strategy_run_id=%s AND e.capital_level=%s AND e.bar_ts=p.bar_ts
                """,
                (run_id,capital,run_id,capital),
            )
            cur.execute("SELECT * FROM ra_strategy_equity_points WHERE strategy_run_id=%s AND capital_level=%s ORDER BY bar_ts", (run_id,capital))
            points = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return points


def _daily_and_portfolio_metrics(run_id: str, capital: float, points: list[dict[str, Any]], trades: list[dict[str, Any]], config: StrategyEconomicsConfig) -> dict[str, Any]:
    accepted = [r for r in trades if r.get("accepted")]
    by_date_points: dict[date,list[dict[str,Any]]] = defaultdict(list)
    for p in points:
        by_date_points[p["trade_date"]].append(p)
    entries_by_day: dict[date,list[dict[str,Any]]] = defaultdict(list)
    for trade in accepted:
        entries_by_day[trade["trade_date"]].append(trade)
    daily_rows: list[dict[str,Any]] = []
    previous_equity = capital
    peak_equity = capital
    drawdown_values: list[float] = []
    underwater_run = max_underwater = 0
    drawdown_durations: list[int] = []
    for trade_date in sorted(by_date_points):
        day_points = by_date_points[trade_date]
        end_equity = float(day_points[-1]["equity"])
        net_return = (end_equity/previous_equity-1)*100 if previous_equity else 0.0
        active = bool(entries_by_day.get(trade_date))
        day_trades = entries_by_day.get(trade_date,[])
        gross_turn = sum(float(t.get("filled_notional") or 0) for t in day_trades)
        round_trip_turn = 2*gross_turn
        costs = sum(float(t.get("estimated_cost_value") or 0) for t in day_trades)
        max_dd = min(float(p.get("drawdown_pct") or 0) for p in day_points)
        daily_rows.append({
            "trade_date":trade_date,"active_day":active,"trades":len(day_trades),"net_return_pct":net_return,
            "end_equity":end_equity,"gross_turnover":gross_turn,"round_trip_turnover":round_trip_turn,"estimated_costs":costs,
            "peak_gross_exposure":max(float(p.get("gross_exposure") or 0) for p in day_points),
            "peak_net_exposure":max(abs(float(p.get("net_exposure") or 0)) for p in day_points),"max_intraday_drawdown_pct":max_dd,
        })
        previous_equity = end_equity
        peak_equity = max(peak_equity,end_equity)
        dd=(end_equity/peak_equity-1)*100 if peak_equity else 0
        drawdown_values.append(dd)
        if dd<0:
            underwater_run+=1; max_underwater=max(max_underwater,underwater_run)
        elif underwater_run:
            drawdown_durations.append(underwater_run); underwater_run=0
    if underwater_run:
        drawdown_durations.append(underwater_run)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ra_strategy_daily_metrics WHERE strategy_run_id=%s AND capital_level=%s",(run_id,capital))
            for d in daily_rows:
                cur.execute(
                    """INSERT INTO ra_strategy_daily_metrics(strategy_run_id,capital_level,trade_date,market_day,active_day,trades,net_return_pct,end_equity,gross_turnover,round_trip_turnover,estimated_costs,peak_gross_exposure,peak_net_exposure,max_intraday_drawdown_pct)
                       VALUES (%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id,capital,d["trade_date"],d["active_day"],d["trades"],d["net_return_pct"],d["end_equity"],d["gross_turnover"],d["round_trip_turnover"],d["estimated_costs"],d["peak_gross_exposure"],d["peak_net_exposure"],d["max_intraday_drawdown_pct"]),
                )
        conn.commit()
    market_returns=[d["net_return_pct"] for d in daily_rows]
    active_returns=[d["net_return_pct"] for d in daily_rows if d["active_day"]]
    weeks: dict[tuple[int,int],list[float]]=defaultdict(list)
    months: dict[tuple[int,int],list[float]]=defaultdict(list)
    daily_pnl: list[float] = []
    prior = capital
    for d in daily_rows:
        iso=d["trade_date"].isocalendar(); weeks[(iso.year,iso.week)].append(d["net_return_pct"])
        months[(d["trade_date"].year,d["trade_date"].month)].append(d["net_return_pct"])
        daily_pnl.append(float(d["end_equity"])-prior)
        prior=float(d["end_equity"])
    week_returns={k:_compound_pct(v) or 0.0 for k,v in weeks.items()}
    month_returns={k:_compound_pct(v) or 0.0 for k,v in months.items()}
    total_market_days=len(daily_rows)
    rolling_20=[]
    accepted_by_date: dict[date,list[float]]=defaultdict(list)
    for t in accepted:
        accepted_by_date[t["trade_date"]].append(float(t["net_return_pct"]))
    for i in range(19,len(daily_rows)):
        window=daily_rows[i-19:i+1]
        returns=[float(x["net_return_pct"]) for x in window]
        mu=mean(returns); sd=(sum((x-mu)**2 for x in returns)/(len(returns)-1))**0.5 if len(returns)>1 else 0.0
        downside=[x for x in returns if x<0]
        dsd=(sum(x*x for x in downside)/len(downside))**0.5 if downside else 0.0
        window_trade_returns=[]
        for x in window:
            window_trade_returns.extend(accepted_by_date.get(x["trade_date"],[]))
        local_peak=1.0; local_wealth=1.0; local_dd=0.0
        for x in returns:
            local_wealth*=max(0.0,1+x/100.0); local_peak=max(local_peak,local_wealth); local_dd=min(local_dd,(local_wealth/local_peak-1)*100)
        rolling_20.append({
            "end_date":window[-1]["trade_date"],"compounded_return_pct":_compound_pct(returns),
            "mean_market_day_return_pct":mu,"sharpe":mu/sd*(252**0.5) if sd>0 else None,
            "sortino":mu/dsd*(252**0.5) if dsd>0 else None,"profit_factor":_profit_factor(window_trade_returns),
            "maximum_drawdown_pct":local_dd,
        })
    geometric=(float(daily_rows[-1]["end_equity"])/capital-1)*100 if daily_rows else None
    annualised=None
    if total_market_days>=60 and daily_rows and float(daily_rows[-1]["end_equity"])>0:
        annualised=((float(daily_rows[-1]["end_equity"])/capital)**(252/total_market_days)-1)*100
    gross_exposures=[float(p.get("gross_exposure") or 0) for p in points]
    net_exposures=[abs(float(p.get("net_exposure") or 0)) for p in points]
    active_minutes=sum(x>0 for x in gross_exposures)
    total_net_pnl=(float(daily_rows[-1]["end_equity"])-capital) if daily_rows else 0
    deployed_capital_minutes=sum(gross_exposures)
    return {
        "geometric_compounded_return_pct":geometric,"annualised_return_pct":annualised,
        "average_return_per_active_day_pct":mean(active_returns) if active_returns else None,
        "median_return_per_active_day_pct":median(active_returns) if active_returns else None,
        "average_return_per_market_day_pct":mean(market_returns) if market_returns else None,
        "median_return_per_market_day_pct":median(market_returns) if market_returns else None,
        "active_day_win_rate_pct":100*sum(x>0 for x in active_returns)/len(active_returns) if active_returns else None,
        "market_day_win_rate_pct":100*sum(x>0 for x in market_returns)/len(market_returns) if market_returns else None,
        "profitable_week_pct":100*sum(x>0 for x in week_returns.values())/len(week_returns) if week_returns else None,
        "profitable_month_pct":100*sum(x>0 for x in month_returns.values())/len(month_returns) if month_returns else None,
        "weekly_returns_pct":{f"{y}-W{w:02d}":v for (y,w),v in week_returns.items()},
        "monthly_returns_pct":{f"{y}-{m:02d}":v for (y,m),v in month_returns.items()},
        "monthly_return_dispersion_pct":(sum((x-mean(month_returns.values()))**2 for x in month_returns.values())/(len(month_returns)-1))**0.5 if len(month_returns)>1 else None,
        "worst_active_day_pct":min(active_returns) if active_returns else None,"worst_market_day_pct":min(market_returns) if market_returns else None,
        "maximum_drawdown_pct":min(float(p.get("drawdown_pct") or 0) for p in points) if points else None,
        "average_drawdown_pct":mean(drawdown_values) if drawdown_values else None,
        "maximum_underwater_market_days":max_underwater,"average_drawdown_duration_market_days":mean(drawdown_durations) if drawdown_durations else 0,
        "longest_losing_active_day_sequence":_losing_streak(active_returns),
        "average_gross_exposure":mean(gross_exposures) if gross_exposures else 0,"peak_gross_exposure":max(gross_exposures) if gross_exposures else 0,
        "average_net_exposure":mean(net_exposures) if net_exposures else 0,"peak_net_exposure":max(net_exposures) if net_exposures else 0,
        "average_capital_utilisation_pct":100*mean(gross_exposures)/capital if gross_exposures else 0,
        "maximum_capital_utilisation_pct":100*max(gross_exposures)/capital if gross_exposures else 0,
        "pct_trading_minutes_capital_deployed":100*active_minutes/len(points) if points else 0,
        "pct_market_days_capital_deployed":100*sum(d["active_day"] for d in daily_rows)/len(daily_rows) if daily_rows else 0,
        "return_on_total_capital_pct":100*total_net_pnl/capital if capital else None,
        "return_on_deployed_capital_pct":100*total_net_pnl/(deployed_capital_minutes/len(points)) if points and deployed_capital_minutes>0 else None,
        "market_days":len(daily_rows),"active_days":sum(d["active_day"] for d in daily_rows),
        "gross_notional_turnover":sum(d["gross_turnover"] for d in daily_rows),
        "round_trip_notional_turnover":sum(d["round_trip_turnover"] for d in daily_rows),
        "annualised_turnover_multiple":sum(d["round_trip_turnover"] for d in daily_rows)/capital*252/max(1,len(daily_rows)) if capital and daily_rows else None,
        "estimated_total_trading_friction":sum(d["estimated_costs"] for d in daily_rows),
        "best_market_day_pnl_share_pct":100*max(daily_pnl)/sum(daily_pnl) if daily_pnl and sum(daily_pnl)>0 else None,
        "best_week_return_share_pct":100*max(week_returns.values())/sum(week_returns.values()) if week_returns and sum(week_returns.values())>0 else None,
        "best_month_return_share_pct":100*max(month_returns.values())/sum(month_returns.values()) if month_returns and sum(month_returns.values())>0 else None,
        "rolling_20_market_day":rolling_20,
        "rolling_20_min_compounded_return_pct":min((x["compounded_return_pct"] for x in rolling_20 if x["compounded_return_pct"] is not None),default=None),
        "rolling_20_min_profit_factor":min((x["profit_factor"] for x in rolling_20 if x["profit_factor"] is not None and math.isfinite(x["profit_factor"])),default=None),
        "rolling_20_min_sharpe":min((x["sharpe"] for x in rolling_20 if x["sharpe"] is not None),default=None),
        "rolling_20_min_sortino":min((x["sortino"] for x in rolling_20 if x["sortino"] is not None),default=None),
    }


def _stress_metrics(rows: list[dict[str,Any]], capital: float, config: StrategyEconomicsConfig) -> dict[str,Any]:
    trade = _trade_metrics(rows,capital,config)
    accepted=[r for r in rows if r.get("accepted")]
    by_day: dict[date,float]=defaultdict(float)
    for r in accepted:
        by_day[r["trade_date"]]+=float(r["filled_notional"])*float(r["net_return_pct"])/100.0
    market_days=(config.end_date-config.start_date).days+1
    realized_day_returns=[100*v/capital for v in by_day.values()]
    return {
        "trades":trade["trades"],"fill_rate_pct":trade["fill_rate_pct"],"net_expected_value_pct":trade["net_expected_value_pct"],
        "median_net_trade_return_pct":trade["median_net_trade_return_pct"],"profit_factor":trade["profit_factor"],
        "net_pnl":trade["net_pnl"],"active_day_average_return_pct":mean(realized_day_returns) if realized_day_returns else None,
        "active_day_win_rate_pct":100*sum(v>0 for v in realized_day_returns)/len(realized_day_returns) if realized_day_returns else None,
        "calendar_span_days":market_days,
    }


def _regime_results(run_id: str, capital: float) -> tuple[list[dict[str,Any]],float]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.net_return_pct,t.filled_notional,t.signal_ts,m.pct_positive_30m,m.spy_return_30m_pct,
                       m.stddev_return_30m_pct,m.median_relative_volume,m.minute_of_day
                FROM ra_strategy_trades t
                LEFT JOIN LATERAL (
                    SELECT f.* FROM ra_market_state_features f JOIN ra_market_state_runs r ON r.id=f.market_state_run_id
                    WHERE r.status='completed' AND f.feature_set_id=(SELECT feature_set_id FROM ra_strategy_economics_runs WHERE id=%s)
                      AND f.bar_ts=t.signal_ts ORDER BY r.completed_at DESC NULLS LAST LIMIT 1
                ) m ON true
                WHERE t.strategy_run_id=%s AND t.capital_level=%s AND t.accepted
                """,
                (run_id,run_id,capital),
            )
            rows=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    covered=[r for r in rows if r.get("pct_positive_30m") is not None]
    coverage=100*len(covered)/len(rows) if rows else 0.0
    buckets: dict[tuple[str,str],list[dict[str,Any]]]=defaultdict(list)
    for r in covered:
        breadth=float(r["pct_positive_30m"])
        buckets[("breadth","weak" if breadth<40 else "strong" if breadth>60 else "neutral")].append(r)
        spy=float(r.get("spy_return_30m_pct") or 0)
        buckets[("spy_30m","falling" if spy<-0.25 else "rising" if spy>0.25 else "sideways")].append(r)
        disp=float(r.get("stddev_return_30m_pct") or 0)
        buckets[("dispersion","low" if disp<1 else "high" if disp>=2 else "medium")].append(r)
        rvol=float(r.get("median_relative_volume") or 0)
        buckets[("market_activity","low" if rvol<0.8 else "high" if rvol>=1.5 else "normal")].append(r)
        minute=int(r.get("minute_of_day") or 0)
        buckets[("session","opening" if minute<660 else "closing" if minute>=900 else "midday")].append(r)
    output=[]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ra_strategy_regime_results WHERE strategy_run_id=%s AND capital_level=%s",(run_id,capital))
            for (kind,value),items in buckets.items():
                returns=[float(x["net_return_pct"]) for x in items]
                total_pnl=sum(float(x["filled_notional"])*float(x["net_return_pct"])/100 for x in items)
                result={"regime_type":kind,"regime_value":value,"observations":len(items),"independent_events":len({x["signal_ts"] for x in items}),
                        "net_avg_pct":mean(returns),"median_pct":median(returns),"profit_factor":_profit_factor(returns),
                        "win_rate_pct":100*sum(x>0 for x in returns)/len(returns),"total_pnl":total_pnl}
                output.append(result)
                cur.execute(
                    """INSERT INTO ra_strategy_regime_results(strategy_run_id,capital_level,regime_type,regime_value,observations,independent_events,net_avg_pct,median_pct,profit_factor,win_rate_pct,total_pnl)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id,capital,kind,value,result["observations"],result["independent_events"],result["net_avg_pct"],result["median_pct"],result["profit_factor"],result["win_rate_pct"],result["total_pnl"]),
                )
        conn.commit()
    return output,coverage


def _chronology_pass(candidate_id: UUID | str, config_hash: str, stage: str) -> bool:
    required = {
        "discovery": [],
        "validation": ["discovery"],
        "research_confirmation": ["discovery", "validation"],
        "custom_presealed": [],
        "sealed_holdout": ["discovery", "validation", "research_confirmation"],
    }.get(stage, [])
    if not required:
        return True
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT research_stage,classification,summary,scorecard FROM ra_strategy_economics_runs
                   WHERE candidate_id=%s AND strategy_config_hash=%s AND mode='research' AND status='completed'""",
                (candidate_id, config_hash),
            )
            rows=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    by_stage={r["research_stage"]:r for r in rows}
    for needed in required:
        row=by_stage.get(needed)
        if not row:
            return False
        metrics=dict((row.get("summary") or {}).get("primary_metrics") or {})
        score=dict(row.get("scorecard") or {})
        if (metrics.get("net_expected_value_pct") or 0)<=0 or (metrics.get("average_return_per_market_day_pct") or 0)<=0:
            return False
        if not score.get("economic_quality_pass"):
            return False
    return True


def _frozen_presealed_evidence(candidate_id: UUID | str, config_hash: str) -> tuple[dict[str,Any],list[dict[str,Any]]]:
    ledger=assert_strategy_frozen(candidate_id,config_hash)
    run_id=ledger.get("strategy_economics_run_id")
    if not run_id:
        raise ValueError("Frozen strategy is missing its pre-sealed economics run")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scorecard,research_stage,classification FROM ra_strategy_economics_runs WHERE id=%s AND status='completed' AND mode='research'",(run_id,))
            run=cur.fetchone()
            if not run or run["research_stage"]!='research_confirmation':
                raise ValueError("Sealed strategy requires a frozen research-confirmation economics run")
            cur.execute("SELECT capital_level,entry_delay_minutes,round_trip_cost_bps,metrics FROM ra_strategy_stress_results WHERE strategy_run_id=%s",(run_id,))
            stress=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    return dict(run.get("scorecard") or {}),stress


def _scorecard(metrics: dict[str,Any], stress: list[dict[str,Any]], config: StrategyEconomicsConfig, stage: str, mode: str, chronology_pass: bool=True, inherited_scorecard: dict[str,Any] | None=None) -> tuple[dict[str,Any],str]:
    def stress_net(cost: float, delay: int) -> float | None:
        for row in stress:
            if float(row["round_trip_cost_bps"])==float(cost) and int(row["entry_delay_minutes"])==delay:
                return finite_or_none(row["metrics"].get("net_expected_value_pct"))
        return None
    economic = bool((metrics.get("net_expected_value_pct") or 0)>0 and (metrics.get("profit_factor") or 0)>1 and (metrics.get("average_return_per_market_day_pct") or 0)>0)
    if mode=="sealed" and inherited_scorecard:
        execution=bool(inherited_scorecard.get("execution_quality_pass"))
        delay5=bool(inherited_scorecard.get("five_minute_delay_positive"))
        risk=bool(inherited_scorecard.get("risk_quality_pass"))
        tail=bool(inherited_scorecard.get("return_concentration_pass"))
        credibility=bool(inherited_scorecard.get("statistical_credibility_pass"))
    else:
        execution = bool((stress_net(30,0) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,1) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,2) or -1)>0)
        delay5 = (stress_net(config.base_round_trip_cost_bps,5) or -1)>0
        maxdd=metrics.get("maximum_drawdown_pct")
        risk = bool(maxdd is not None and maxdd >= -abs(config.max_acceptable_drawdown_pct))
        concentration=metrics.get("top_10pct_return_share_pct")
        tail = bool(concentration is None or concentration<=150)
        credibility=bool((metrics.get("independent_event_count") or 0)>=30 and (metrics.get("trades") or 0)>=100)
    concentration=metrics.get("top_10pct_return_share_pct")
    scorecard={
        "economic_quality_pass":economic,"execution_quality_pass":execution,"five_minute_delay_positive":delay5,
        "chronology_pass":chronology_pass,
        "risk_quality_pass":risk,"return_concentration_pass":tail,"statistical_credibility_pass":credibility,
        "net_expectancy_positive":(metrics.get("net_expected_value_pct") or 0)>0,
        "profit_factor_gt_one":(metrics.get("profit_factor") or 0)>1,
        "market_day_mean_positive":(metrics.get("average_return_per_market_day_pct") or 0)>0,
        "cost_30bps_positive":(stress_net(30,0) or -1)>0,
        "delay_1m_positive":(stress_net(config.base_round_trip_cost_bps,1) or -1)>0,
        "delay_2m_positive":(stress_net(config.base_round_trip_cost_bps,2) or -1)>0,
        "delay_5m_positive":delay5,
        "max_drawdown_within_declared_limit":risk,
        "top_10pct_return_share_pct":concentration,
    }
    if not economic:
        classification="exploratory"
    elif economic and not (execution and risk and credibility and chronology_pass):
        classification="promising"
    elif mode=="sealed" and execution and risk and credibility and chronology_pass:
        classification="deployment_candidate"
    elif stage in {"validation","research_confirmation"} and execution and risk and credibility and chronology_pass:
        classification="out_of_sample_validated"
    else:
        classification="statistically_credible"
    return scorecard,classification


def _ensure_run(job_id: str, candidate: dict[str,Any], config: StrategyEconomicsConfig, config_hash: str) -> str:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_strategy_economics_runs WHERE job_id=%s",(job_id,))
            row=cur.fetchone()
            if row:
                run_id=str(row["id"])
                cur.execute("UPDATE ra_strategy_economics_runs SET status='running',completed_at=NULL,config=%s,strategy_config_hash=%s WHERE id=%s",(Jsonb(config.model_dump(mode="json")),config_hash,run_id))
            else:
                cur.execute(
                    """INSERT INTO ra_strategy_economics_runs(job_id,candidate_id,feature_set_id,mode,research_stage,start_date,end_date,config,strategy_config_hash,engine_version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (job_id,candidate["id"],config.target_feature_set_id,config.mode,config.research_stage,config.start_date,config.end_date,Jsonb(config.model_dump(mode="json")),config_hash,STRATEGY_ECONOMICS_VERSION),
                )
                run_id=str(cur.fetchone()["id"])
        conn.commit()
    return run_id


def _record_research_result(candidate: dict[str,Any], run_id: str, config_hash: str, config_payload: dict[str,Any], summary: dict[str,Any], classification: str, mode: str) -> None:
    sync_candidate_ledger(candidate["discovery_run_id"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_research_ledger WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1",(candidate["id"],))
            row=cur.fetchone()
            if not row:
                raise RuntimeError("Research Ledger entry missing for strategy candidate")
            if mode=="sealed":
                cur.execute("UPDATE ra_research_ledger SET sealed_strategy_result=%s,classification=%s WHERE id=%s",(Jsonb(summary),classification,row["id"]))
            else:
                cur.execute("""UPDATE ra_research_ledger SET strategy_economics_run_id=%s,strategy_configuration=%s,
                               strategy_configuration_hash=%s,strategy_economics_result=%s,classification=%s WHERE id=%s""",
                            (run_id,Jsonb(config_payload),config_hash,Jsonb(summary),classification,row["id"]))
        conn.commit()


def run_strategy_economics(job_id: str, config: StrategyEconomicsConfig) -> dict[str,Any]:
    candidate=_load_candidate(config.candidate_id)
    target=_load_feature_set(config.target_feature_set_id)
    if config.mode=="research" and config.end_date>=SEALED_START_DATE:
        raise ValueError("Research strategy economics may not include sealed dates")
    if config.mode=="sealed" and config.start_date<SEALED_START_DATE:
        raise ValueError("Sealed strategy economics must begin on or after the sealed start")
    config_hash=strategy_config_hash(candidate,config)
    if config.strategy_config_hash and config.strategy_config_hash!=config_hash:
        raise ValueError("Provided strategy_config_hash does not match the executable methodology")
    inherited_scorecard=None
    inherited_stress=[]
    if config.mode=="sealed":
        inherited_scorecard,inherited_stress=_frozen_presealed_evidence(candidate["id"],config_hash)
    chronology_pass=_chronology_pass(candidate["id"],config_hash,config.research_stage)
    if not chronology_pass and config.research_stage in {"validation","research_confirmation","sealed_holdout"}:
        raise ValueError(f"Identical executable strategy methodology has not passed all prerequisite chronological stages for {config.research_stage}")
    run_id=_ensure_run(job_id,candidate,config,config_hash)
    set_progress(job_id,"loading executable signals",0,5,result={"strategy_run_id":run_id,"strategy_config_hash":config_hash})
    signals=_fetch_signals(candidate,config,config.base_entry_delay_minutes,True)
    check_control(job_id)
    set_progress(job_id,"simulating capital allocation",1,5)
    all_metrics={}
    stress_output=[]
    primary_summary=None
    primary_scorecard=None
    primary_classification="exploratory"
    for capital in config.capital_levels:
        trades=_simulate(signals,candidate,config,float(capital),config.base_round_trip_cost_bps)
        _insert_trades(run_id,float(capital),trades,candidate)
        trade_metrics=_trade_metrics(trades,float(capital),config)
        points=_build_equity(run_id,config.target_feature_set_id,float(capital),config.start_date,config.end_date)
        portfolio_metrics=_daily_and_portfolio_metrics(run_id,float(capital),points,trades,config)
        metrics={**trade_metrics,**portfolio_metrics}
        all_metrics[str(capital)]=metrics
    check_control(job_id)
    set_progress(job_id,"cost and latency stress",2,5)
    if config.mode=="research":
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ra_strategy_stress_results WHERE strategy_run_id=%s",(run_id,))
            conn.commit()
        for delay in config.entry_delays_minutes:
            delay_signals=_fetch_signals(candidate,config,delay,False)
            for cost in config.round_trip_costs_bps:
                for capital in config.capital_levels:
                    stressed=_simulate(delay_signals,candidate,config,float(capital),float(cost))
                    metrics=_stress_metrics(stressed,float(capital),config)
                    record={"capital_level":float(capital),"entry_delay_minutes":delay,"round_trip_cost_bps":float(cost),"metrics":metrics}
                    stress_output.append(record)
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""INSERT INTO ra_strategy_stress_results(strategy_run_id,capital_level,entry_delay_minutes,round_trip_cost_bps,metrics)
                                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (strategy_run_id,capital_level,entry_delay_minutes,round_trip_cost_bps)
                                           DO UPDATE SET metrics=excluded.metrics,created_at=now()""",
                                        (run_id,float(capital),delay,float(cost),Jsonb(metrics)))
                        conn.commit()
    check_control(job_id)
    set_progress(job_id,"regime and dependence analysis",3,5)
    regime_by_capital={}
    regime_coverages=[]
    for capital in config.capital_levels:
        regimes,coverage=_regime_results(run_id,float(capital))
        regime_by_capital[str(capital)]=regimes; regime_coverages.append(coverage)
    set_progress(job_id,"whole-strategy scorecard",4,5)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ra_strategy_metric_sets WHERE strategy_run_id=%s",(run_id,))
        conn.commit()
    for capital in config.capital_levels:
        capital_stress=[x for x in (stress_output if config.mode=="research" else inherited_stress) if float(x["capital_level"])==float(capital)]
        scorecard,classification=_scorecard(all_metrics[str(capital)],capital_stress,config,config.research_stage,config.mode,chronology_pass,inherited_scorecard)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO ra_strategy_metric_sets(strategy_run_id,capital_level,metric_scope,metrics,scorecard,classification)
                               VALUES (%s,%s,'base',%s,%s,%s)""",
                            (run_id,float(capital),Jsonb(all_metrics[str(capital)]),Jsonb(scorecard),classification))
            conn.commit()
        if float(capital)==float(config.capital_levels[0]):
            primary_summary=all_metrics[str(capital)]; primary_scorecard=scorecard; primary_classification=classification
    summary={
        "strategy_run_id":run_id,"strategy_config_hash":config_hash,"engine_version":STRATEGY_ECONOMICS_VERSION,
        "candidate_id":str(candidate["id"]),"mode":config.mode,"research_stage":config.research_stage,
        "start_date":config.start_date,"end_date":config.end_date,"capital_levels":config.capital_levels,
        "primary_capital_level":config.capital_levels[0],"primary_metrics":primary_summary,
        "primary_scorecard":primary_scorecard,"chronology_pass":chronology_pass,"all_capital_metrics":all_metrics,
        "stress_results":stress_output if config.mode=="research" else [],
        "regime_results":regime_by_capital,"regime_coverage_pct":mean(regime_coverages) if regime_coverages else 0,
        "execution_limitations":{
            "historical_short_availability":"Point-in-time borrow/short-availability history is not present; current asset metadata is diagnostic only.",
            "historical_bid_ask":"Minute SIP bars do not contain quote-level spread/depth; configured spread/slippage/impact assumptions are explicit proxies.",
            "capacity_information_timing":"Entry-bar dollar volume is observable at entry. Daily-capacity limits use only point-in-time universe median daily dollar volume whose lookback ended before the trade date; when unavailable the engine falls back to entry-bar capacity only and does not use same-day future volume.",
            "sector_metadata":"Sector labels are used only for diagnostics unless point-in-time sector metadata is supplied; no retroactive sector filter is applied.",
            "stop_execution":"No stop is introduced unless it is part of the frozen strategy methodology.",
        },
        "classification":primary_classification,
    }
    payload=_strategy_payload(candidate,config)
    _record_research_result(candidate,run_id,config_hash,payload,summary,primary_classification,config.mode)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_strategy_economics_runs SET status='completed',classification=%s,summary=%s,scorecard=%s,regime_coverage_pct=%s,completed_at=now() WHERE id=%s",
                        (primary_classification,Jsonb(summary),Jsonb(primary_scorecard or {}),summary["regime_coverage_pct"],run_id))
        conn.commit()
    set_progress(job_id,"complete",5,5,result=summary)
    add_event(job_id,"strategy_economics_completed","Whole-strategy economics completed; hit rate is retained only as a diagnostic.",details={"strategy_run_id":run_id,"classification":primary_classification,"strategy_config_hash":config_hash})
    return json_safe(summary)


def freeze_strategy(candidate_id: UUID | str, strategy_run_id: UUID | str, notes: str | None=None) -> dict[str,Any]:
    candidate=_load_candidate(candidate_id)
    freeze_candidate(candidate_id,notes or "Candidate rule frozen with executable strategy methodology.")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE id=%s AND candidate_id=%s AND status='completed' AND mode='research'",(strategy_run_id,candidate_id))
            run=cur.fetchone()
            if not run:
                raise ValueError("Strategy freeze requires a completed pre-sealed strategy-economics run")
            if run["research_stage"]!="research_confirmation" or run["classification"]!="out_of_sample_validated":
                raise ValueError("Strategy freeze requires the identical methodology to pass Discovery, Validation and Research Confirmation whole-strategy economics")
            if not _chronology_pass(candidate_id,str(run["strategy_config_hash"]),"research_confirmation"):
                raise ValueError("Strategy chronology is incomplete; freeze is not permitted")
            config=StrategyEconomicsConfig.model_validate(run["config"])
            payload=_strategy_payload(candidate,config)
            fingerprint=str(run["strategy_config_hash"])
            cur.execute("SELECT id,strategy_freeze_timestamp,strategy_configuration_hash FROM ra_research_ledger WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",(candidate_id,))
            ledger=cur.fetchone()
            if not ledger:
                raise RuntimeError("Research Ledger entry missing")
            if ledger["strategy_freeze_timestamp"]:
                if ledger["strategy_configuration_hash"]!=fingerprint:
                    raise ValueError("A different executable strategy methodology is already frozen for this candidate")
            else:
                cur.execute("""UPDATE ra_research_ledger SET strategy_economics_run_id=%s,strategy_configuration=%s,
                               strategy_configuration_hash=%s,strategy_freeze_timestamp=now(),candidate_retention_status='strategy_frozen',
                               notes=COALESCE(%s,notes) WHERE id=%s RETURNING *""",
                            (strategy_run_id,Jsonb(payload),fingerprint,notes,ledger["id"]))
                ledger=cur.fetchone()
        conn.commit()
    return json_safe({"candidate_id":candidate_id,"strategy_run_id":strategy_run_id,"strategy_config_hash":fingerprint,"ledger":dict(ledger)})


def assert_strategy_frozen(candidate_id: UUID | str, strategy_hash: str | None=None) -> dict[str,Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM ra_research_ledger WHERE candidate_id=%s AND strategy_freeze_timestamp IS NOT NULL
                           ORDER BY strategy_freeze_timestamp DESC LIMIT 1""",(candidate_id,))
            row=cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Executable strategy must be frozen in the Research Ledger before sealed evaluation")
    if strategy_hash and row["strategy_configuration_hash"]!=strategy_hash:
        raise ValueError("Sealed evaluation methodology differs from the frozen executable strategy")
    return dict(row)

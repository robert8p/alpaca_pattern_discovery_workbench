from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

OBJECTIVE_VERSION = "whole-strategy-economics-v1"


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _mean(values: Iterable[float]) -> float | None:
    xs = list(values)
    return sum(xs) / len(xs) if xs else None


def _compound_pct(values: Iterable[float]) -> float | None:
    xs = list(values)
    if not xs:
        return None
    wealth = 1.0
    for value in xs:
        wealth *= max(0.0, 1.0 + float(value) / 100.0)
    return (wealth - 1.0) * 100.0


def _shape(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 3:
        return None, None
    mu = statistics.mean(values)
    sigma = statistics.pstdev(values)
    if sigma <= 0:
        return 0.0, 0.0
    n = len(values)
    skew = sum(((x - mu) / sigma) ** 3 for x in values) / n
    kurtosis_excess = sum(((x - mu) / sigma) ** 4 for x in values) / n - 3.0
    return _finite(skew), _finite(kurtosis_excess)


def _expected_shortfall(values: list[float], q: float) -> float | None:
    if not values:
        return None
    cutoff = _quantile(values, q)
    if cutoff is None:
        return None
    tail = [x for x in values if x <= cutoff]
    return _finite(_mean(tail))


def _weekday_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _group_returns(rows: list[dict[str, Any]], net: list[float], field: str) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(rows, net):
        groups[str(row[field])].append(value)
    return groups


def _drawdown_metrics(daily_pct: list[float]) -> dict[str, Any]:
    if not daily_pct:
        return {}
    equity = 1.0
    peak = 1.0
    drawdowns: list[float] = []
    underwater = 0
    max_underwater = 0
    current_drawdown_days = 0
    drawdown_durations: list[int] = []
    for ret in daily_pct:
        equity *= max(0.0, 1.0 + ret / 100.0)
        if equity >= peak:
            if current_drawdown_days:
                drawdown_durations.append(current_drawdown_days)
            peak = equity
            current_drawdown_days = 0
        else:
            current_drawdown_days += 1
        dd = (equity / peak - 1.0) * 100.0 if peak else -100.0
        drawdowns.append(dd)
        if dd < 0:
            underwater += 1
            max_underwater = max(max_underwater, current_drawdown_days)
    if current_drawdown_days:
        drawdown_durations.append(current_drawdown_days)
    negatives = [x for x in drawdowns if x < 0]
    return {
        "maximum_drawdown_pct": _finite(min(drawdowns)),
        "average_drawdown_pct": _finite(_mean(negatives)),
        "underwater_market_days": underwater,
        "maximum_underwater_duration_market_days": max_underwater,
        "average_drawdown_duration_market_days": _finite(_mean(drawdown_durations)),
        "ending_equity_multiple": _finite(equity),
    }


def _rolling_stats(values: list[float], window: int) -> dict[str, Any]:
    if len(values) < window:
        return {"window": window, "observations": len(values), "available": False}
    means: list[float] = []
    pfs: list[float] = []
    for idx in range(window - 1, len(values)):
        chunk = values[idx - window + 1 : idx + 1]
        means.append(statistics.mean(chunk))
        pos = sum(x for x in chunk if x > 0)
        neg = abs(sum(x for x in chunk if x < 0))
        pfs.append(pos / neg if neg else math.inf)
    finite_pfs = [x for x in pfs if math.isfinite(x)]
    return {
        "window": window,
        "observations": len(values),
        "available": True,
        "minimum_rolling_expectancy_pct": _finite(min(means)),
        "median_rolling_expectancy_pct": _finite(_quantile(means, 0.5)),
        "minimum_rolling_profit_factor": _finite(min(finite_pfs)) if finite_pfs else None,
    }


def _return_concentration(values: list[float]) -> dict[str, Any]:
    if not values:
        return {}
    total = sum(values)
    ordered = sorted(values, reverse=True)
    out: dict[str, Any] = {}
    for pct in (1, 5, 10):
        k = max(1, math.ceil(len(values) * pct / 100.0))
        top = sum(ordered[:k])
        out[f"top_{pct}pct_return_share_pct"] = _finite(100.0 * top / total) if total else None
        out[f"net_expectancy_excluding_top_{pct}pct_pct"] = _finite(_mean(ordered[k:])) if len(ordered) > k else None
    return out


def _interval_metrics(rows: list[dict[str, Any]], horizon_minutes: int, market_days: int) -> dict[str, Any]:
    intervals: list[tuple[datetime, datetime]] = []
    points: list[tuple[datetime, int]] = []
    for row in rows:
        ts = row.get("bar_ts")
        if not isinstance(ts, datetime):
            continue
        end = ts + timedelta(minutes=int(horizon_minutes))
        intervals.append((ts, end))
        points.append((ts, 1))
        points.append((end, -1))
    if not intervals:
        return {"available": False}
    intervals.sort(key=lambda x: x[0])
    merged: list[list[datetime]] = []
    for start, end in intervals:
        if not merged or start >= merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    occupied_minutes = sum((end - start).total_seconds() / 60.0 for start, end in merged)
    points.sort(key=lambda x: (x[0], x[1]))
    concurrency = 0
    max_concurrency = 0
    weighted_concurrency_minutes = 0.0
    prior: datetime | None = None
    for ts, delta in points:
        if prior is not None and ts > prior:
            weighted_concurrency_minutes += concurrency * (ts - prior).total_seconds() / 60.0
        concurrency += delta
        max_concurrency = max(max_concurrency, concurrency)
        prior = ts
    available_market_minutes = max(1, market_days) * 390.0
    return {
        "available": True,
        "holding_horizon_minutes": int(horizon_minutes),
        "maximum_concurrent_positions": int(max_concurrency),
        "position_minutes": _finite(weighted_concurrency_minutes),
        "minutes_with_any_position": _finite(occupied_minutes),
        "position_time_utilisation_proxy_pct": _finite(100.0 * occupied_minutes / available_market_minutes),
        "average_concurrent_positions_during_market_time_proxy": _finite(weighted_concurrency_minutes / available_market_minutes),
        "note": "Opportunity/exposure proxy only; actual capital utilisation requires a frozen position-sizing and gross-exposure policy.",
    }


def _daily_and_calendar_metrics(rows: list[dict[str, Any]], net: list[float]) -> dict[str, Any]:
    if not rows:
        return {}
    by_date = _group_returns(rows, net, "trade_date")
    daily = {key: statistics.mean(values) for key, values in by_date.items()}
    start = min(row["trade_date"] for row in rows)
    end = max(row["trade_date"] for row in rows)
    market_dates = _weekday_dates(start, end)
    active_daily = [daily[key] for key in sorted(daily)]
    market_daily = [daily.get(str(d), 0.0) for d in market_dates]
    week_groups: dict[str, list[float]] = defaultdict(list)
    month_groups: dict[str, list[float]] = defaultdict(list)
    for d in market_dates:
        value = daily.get(str(d), 0.0)
        iso = d.isocalendar()
        week_groups[f"{iso.year}-W{iso.week:02d}"].append(value)
        month_groups[d.strftime("%Y-%m")].append(value)
    weekly = [_compound_pct(v) or 0.0 for v in week_groups.values()]
    monthly = [_compound_pct(v) or 0.0 for v in month_groups.values()]
    market_stdev = statistics.stdev(market_daily) if len(market_daily) > 1 else None
    sharpe = statistics.mean(market_daily) / market_stdev * math.sqrt(252) if market_stdev and market_stdev > 0 else None
    downside = [min(0.0, x) for x in market_daily]
    downside_dev = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else None
    sortino = statistics.mean(market_daily) / downside_dev * math.sqrt(252) if downside_dev and downside_dev > 0 else None
    best_day = max(market_daily) if market_daily else None
    total_compounded = _compound_pct(market_daily)
    return {
        "active_days": len(active_daily),
        "market_days": len(market_dates),
        "average_return_per_active_day_pct": _finite(_mean(active_daily)),
        "median_return_per_active_day_pct": _finite(_quantile(active_daily, 0.5)),
        "profitable_active_day_pct": _finite(100.0 * sum(x > 0 for x in active_daily) / len(active_daily)) if active_daily else None,
        "average_return_per_market_day_pct": _finite(_mean(market_daily)),
        "median_return_per_market_day_pct": _finite(_quantile(market_daily, 0.5)),
        "profitable_market_day_pct": _finite(100.0 * sum(x > 0 for x in market_daily) / len(market_daily)) if market_daily else None,
        "profitable_week_pct": _finite(100.0 * sum(x > 0 for x in weekly) / len(weekly)) if weekly else None,
        "profitable_month_pct": _finite(100.0 * sum(x > 0 for x in monthly) / len(monthly)) if monthly else None,
        "compounded_normalised_market_day_return_pct": _finite(total_compounded),
        "annualised_normalised_return_pct": _finite((1.0 + total_compounded / 100.0) ** (252 / len(market_daily)) * 100.0 - 100.0) if total_compounded is not None and len(market_daily) >= 20 and total_compounded > -100 else None,
        "annualised_sharpe_diagnostic": _finite(sharpe),
        "annualised_sortino_diagnostic": _finite(sortino),
        "best_market_day_pct": _finite(best_day),
        "best_day_share_of_compounded_return_pct": _finite(100.0 * best_day / total_compounded) if best_day is not None and total_compounded not in (None, 0) else None,
        "weekly_return_dispersion_pct": _finite(statistics.stdev(weekly)) if len(weekly) > 1 else None,
        "monthly_return_dispersion_pct": _finite(statistics.stdev(monthly)) if len(monthly) > 1 else None,
        "drawdown": _drawdown_metrics(market_daily),
        "rolling_20_market_day": _rolling_stats(market_daily, 20),
        "normalisation_note": "Each active day is the equal-weight mean of that day's qualifying trades. This is a diagnostic normalisation, not a final capital-allocation backtest.",
    }


def strategy_economics(rows: list[dict[str, Any]], *, cost_bps: float, holding_horizon_minutes: int) -> dict[str, Any]:
    valid_rows = [row for row in rows if row.get("gross_return_pct") is not None]
    gross = [float(row["gross_return_pct"]) for row in valid_rows]
    cost_pct = float(cost_bps) / 100.0
    net = [value - cost_pct for value in gross]
    if not net:
        return {"objective_version": OBJECTIVE_VERSION, "observations": 0, "cost_bps": float(cost_bps), "complete": False, "missing": ["trade outcomes"]}
    winners = [x for x in net if x > 0]
    losers = [x for x in net if x < 0]
    breakeven = [x for x in net if x == 0]
    pos = sum(winners)
    neg = abs(sum(losers))
    skew, kurtosis = _shape(net)
    by_event = _group_returns(valid_rows, net, "bar_ts")
    event_returns = [statistics.mean(values) for values in by_event.values()]
    by_date = _group_returns(valid_rows, net, "trade_date")
    market_days = len(_weekday_dates(min(row["trade_date"] for row in valid_rows), max(row["trade_date"] for row in valid_rows)))
    mae = [float(row["mae_pct"]) for row in valid_rows if row.get("mae_pct") is not None]
    mfe = [float(row["mfe_pct"]) for row in valid_rows if row.get("mfe_pct") is not None]
    winning_mae = [float(row["mae_pct"]) for row, value in zip(valid_rows, net) if value > 0 and row.get("mae_pct") is not None]
    losing_mae = [float(row["mae_pct"]) for row, value in zip(valid_rows, net) if value < 0 and row.get("mae_pct") is not None]
    gross_mean = statistics.mean(gross)
    net_mean = statistics.mean(net)
    distribution = {
        "gross_expected_value_per_trade_pct": _finite(gross_mean),
        "net_expected_value_per_trade_pct": _finite(net_mean),
        "mean_net_trade_return_pct": _finite(net_mean),
        "median_net_trade_return_pct": _finite(_quantile(net, 0.5)),
        "trade_win_rate_pct": _finite(100.0 * len(winners) / len(net)),
        "trade_loss_rate_pct": _finite(100.0 * len(losers) / len(net)),
        "trade_breakeven_rate_pct": _finite(100.0 * len(breakeven) / len(net)),
        "average_winner_pct": _finite(_mean(winners)),
        "median_winner_pct": _finite(_quantile(winners, 0.5)),
        "average_loser_pct": _finite(_mean(losers)),
        "median_loser_pct": _finite(_quantile(losers, 0.5)),
        "win_loss_magnitude_ratio": _finite(abs((_mean(winners) or 0.0) / (_mean(losers) or 1.0))) if winners and losers else None,
        "profit_factor": _finite(pos / neg) if neg else None,
        "expectancy_pct": _finite(net_mean),
        "p05_pct": _finite(_quantile(net, 0.05)), "p10_pct": _finite(_quantile(net, 0.10)), "p25_pct": _finite(_quantile(net, 0.25)),
        "p50_pct": _finite(_quantile(net, 0.50)), "p75_pct": _finite(_quantile(net, 0.75)), "p90_pct": _finite(_quantile(net, 0.90)), "p95_pct": _finite(_quantile(net, 0.95)),
        "skewness": skew, "excess_kurtosis": kurtosis, "worst_trade_pct": _finite(min(net)), "best_trade_pct": _finite(max(net)),
        "adverse_trade_95pct_threshold_pct": _finite(_quantile(net, 0.05)),
        "adverse_trade_99pct_threshold_pct": _finite(_quantile(net, 0.01)),
        "expected_shortfall_95_pct": _finite(_expected_shortfall(net, 0.05)),
        "expected_shortfall_99_pct": _finite(_expected_shortfall(net, 0.01)),
        **_return_concentration(net),
    }
    mae_mfe = {
        "mae_observations": len(mae), "mfe_observations": len(mfe),
        "mae_mean_pct": _finite(_mean(mae)), "mae_median_pct": _finite(_quantile(mae, 0.5)),
        "mae_p05_pct": _finite(_quantile(mae, 0.05)), "mae_p01_pct": _finite(_quantile(mae, 0.01)),
        "mfe_mean_pct": _finite(_mean(mfe)), "mfe_median_pct": _finite(_quantile(mfe, 0.5)),
        "winning_trade_mae_mean_pct": _finite(_mean(winning_mae)), "losing_trade_mae_mean_pct": _finite(_mean(losing_mae)),
        "stop_loss_note": "MAE/MFE are diagnostics only; no stop is optimised from final returns here.",
    }
    dependence = {
        "independent_timestamp_events": len(by_event),
        "effective_event_ratio_pct": _finite(100.0 * len(by_event) / len(net)),
        "average_trades_per_timestamp_event": _finite(_mean([len(v) for v in by_event.values()])),
        "maximum_trades_in_one_timestamp_event": max(len(v) for v in by_event.values()),
        "p95_trades_per_timestamp_event": _finite(_quantile([float(len(v)) for v in by_event.values()], 0.95)),
        "event_level_net_expectancy_pct": _finite(_mean(event_returns)),
        "event_level_median_net_return_pct": _finite(_quantile(event_returns, 0.5)),
        "event_level_win_rate_pct": _finite(100.0 * sum(v > 0 for v in event_returns) / len(event_returns)),
        "largest_single_day_trade_share_pct": _finite(100.0 * max(len(v) for v in by_date.values()) / len(net)),
        "same_sector_correlation": None, "sector_data_available": False,
        "note": "Timestamp-event aggregation prevents a broad same-minute shock from being mistaken for many independent observations. Sector/outcome correlation requires richer stored samples.",
    }
    turnover = {
        "trades": len(net), "trades_per_active_day": _finite(len(net) / len(by_date)) if by_date else None,
        "trades_per_market_day": _finite(len(net) / market_days) if market_days else None,
        "annualised_trade_count_diagnostic": _finite(len(net) / market_days * 252) if market_days else None,
        "normalised_one_way_turnover_units": len(net), "normalised_round_trip_turnover_units": 2 * len(net),
        "gross_notional_turnover": None, "actual_annualised_turnover": None,
        "note": "Actual notional turnover requires a frozen position-sizing/capital-allocation methodology.",
    }
    return {
        "objective_version": OBJECTIVE_VERSION, "complete": True, "observations": len(net),
        "symbols": len({str(row["symbol"]) for row in valid_rows}), "cost_bps": float(cost_bps),
        "return_distribution": distribution, "mae_mfe": mae_mfe,
        "calendar_consistency": _daily_and_calendar_metrics(valid_rows, net),
        "turnover_and_costs": {**turnover, "round_trip_cost_bps": float(cost_bps), "gross_mean_trade_return_pct": _finite(gross_mean), "net_mean_trade_return_pct": _finite(net_mean), "fraction_of_positive_gross_edge_consumed_by_base_cost_pct": _finite(100.0 * cost_pct / gross_mean) if gross_mean > 0 else None},
        "trade_dependence": dependence,
        "exposure_overlap": _interval_metrics(valid_rows, holding_horizon_minutes, market_days),
        "capital_and_capacity": {
            "average_gross_exposure": None, "average_net_exposure": None, "peak_gross_exposure": None, "peak_net_exposure": None,
            "average_capital_utilisation": None, "maximum_capital_utilisation": None, "return_on_deployed_capital": None,
            "return_on_total_available_capital": None, "signal_capacity": None, "portfolio_capacity": None,
            "liquidity_capacity_status": "not_yet_measured_in_robustness_samples",
            "note": "Do not infer deployability from these missing fields. Promotion is capped until sizing, liquidity and capacity are modelled explicitly.",
        },
    }


def promotion_assessment(economics: dict[str, Any], legacy_summary: dict[str, Any], *, mode: str, minimum_observations: int = 250, minimum_dates: int = 10) -> dict[str, Any]:
    dist = economics.get("return_distribution") or {}; dep = economics.get("trade_dependence") or {}; base = legacy_summary.get("base") or {}
    cost = legacy_summary.get("cost_sensitivity") or {}; delay = legacy_summary.get("entry_delay_sensitivity") or {}; neigh = legacy_summary.get("neighbourhood") or {}
    def metric(mapping: dict[str, Any], key: str) -> float | None:
        value = mapping.get(key)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None
    def cost_metric(bps: int) -> float | None:
        return metric(cost.get(str(bps)) or cost.get(bps) or {}, "net_avg_pct")
    def delay_metric(minutes: int) -> float | None:
        return metric(delay.get(str(minutes)) or delay.get(minutes) or {}, "net_avg_pct")
    checks = {
        "positive_net_expectancy": (metric(dist, "net_expected_value_per_trade_pct") or -math.inf) > 0,
        "positive_event_expectancy": (metric(dep, "event_level_net_expectancy_pct") or -math.inf) > 0,
        "profit_factor_above_one": (metric(dist, "profit_factor") or -math.inf) > 1.0,
        "positive_after_30bps": (cost_metric(30) or -math.inf) > 0,
        "positive_after_2m_delay": (delay_metric(2) or -math.inf) > 0,
        "positive_relaxed_neighbour": (metric(neigh.get("relaxed") or {}, "net_avg_pct") or -math.inf) > 0,
        "positive_tightened_neighbour": (metric(neigh.get("tightened") or {}, "net_avg_pct") or -math.inf) > 0,
        "not_dependent_on_top_1pct": (metric(dist, "net_expectancy_excluding_top_1pct_pct") or -math.inf) > 0,
        "minimum_observations": int(base.get("observations") or 0) >= int(minimum_observations),
        "minimum_dates": int(base.get("dates") or 0) >= int(minimum_dates),
    }
    economic_core = all(checks[key] for key in ("positive_net_expectancy", "positive_event_expectancy", "profit_factor_above_one", "positive_after_30bps", "not_dependent_on_top_1pct"))
    robustness = all(checks[key] for key in ("positive_after_2m_delay", "positive_relaxed_neighbour", "positive_tightened_neighbour"))
    statistical = checks["minimum_observations"] and checks["minimum_dates"] and (metric(base, "date_clustered_t_stat") or -math.inf) > 0
    if not economic_core:
        decision, classification, compatible_verdict = "reject", "exploratory", "REJECT"
    elif not robustness:
        decision, classification, compatible_verdict = "retain_for_research", "exploratory", "WEAK"
    elif statistical:
        decision, classification = "promote_for_further_validation", "statistically_credible"
        compatible_verdict = "HISTORICAL_HOLDOUT" if mode == "historical_holdout" else "PROMISING"
    else:
        decision, classification, compatible_verdict = "promote_for_further_validation", "promising", "PROMISING"
    return {
        "objective": "complete executable strategy economics after costs, execution constraints, dependence and capital allocation",
        "hit_rate_used_as_primary_optimisation_target": False,
        "decision": decision, "classification": classification, "legacy_compatible_verdict": compatible_verdict, "checks": checks,
        "deployment_candidate": False,
        "deployment_blockers": ["frozen position sizing", "portfolio gross/net exposure limits", "actual capital utilisation", "instrument-level liquidity/capacity", "sector-level dependence", "realistic partial-fill/borrow model where relevant"],
        "holdout_policy": "No result here may alter the frozen methodology after sealed data is viewed. Sealed data begins 2026-08-04.",
        "note": "A positive signal is not a deployment candidate. Missing portfolio, liquidity and sizing evidence caps classification until measured.",
    }

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Callable

REGIME_DIAGNOSTICS_VERSION = "1.1.0"


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def _expected_shortfall(values: list[float], q: float = 0.05) -> float | None:
    if not values:
        return None
    cutoff = _quantile(values, q)
    if cutoff is None:
        return None
    tail = [x for x in values if x <= cutoff]
    return sum(tail) / len(tail) if tail else None


def _summary(rows: list[dict[str, Any]], returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"observations": 0}
    winners = [x for x in returns if x > 0]
    losers = [x for x in returns if x < 0]
    positive = sum(winners)
    negative = abs(sum(losers))
    by_event: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(rows, returns):
        by_event[str(row.get("bar_ts"))].append(value)
    event_returns = [statistics.mean(values) for values in by_event.values()]
    return {
        "observations": len(returns),
        "independent_timestamp_events": len(event_returns),
        "dates": len({str(row.get("trade_date")) for row in rows}),
        "mean_net_return_pct": sum(returns) / len(returns),
        "median_net_return_pct": _quantile(returns, 0.50),
        "profit_factor": positive / negative if negative else None,
        "trade_win_rate_pct": 100.0 * len(winners) / len(returns),
        "p05_pct": _quantile(returns, 0.05),
        "expected_shortfall_95_pct": _expected_shortfall(returns, 0.05),
        "worst_trade_pct": min(returns),
        "event_mean_net_return_pct": sum(event_returns) / len(event_returns) if event_returns else None,
        "event_median_net_return_pct": _quantile(event_returns, 0.50),
    }


def _group(
    rows: list[dict[str, Any]],
    returns: list[float],
    labeler: Callable[[dict[str, Any]], str | None],
) -> dict[str, Any]:
    buckets: dict[str, tuple[list[dict[str, Any]], list[float]]] = {}
    for row, value in zip(rows, returns):
        label = labeler(row)
        if label is None:
            continue
        if label not in buckets:
            buckets[label] = ([], [])
        buckets[label][0].append(row)
        buckets[label][1].append(value)
    return {
        label: _summary(group_rows, group_returns)
        for label, (group_rows, group_returns) in sorted(buckets.items())
    }


def regime_diagnostics(
    rows: list[dict[str, Any]],
    *,
    cost_bps: float,
    calibration_thresholds: dict[str, Any] | None,
    calibration_market_state_timestamps: int = 0,
    market_state_run_id: str | None = None,
) -> dict[str, Any]:
    usable = [
        row for row in rows
        if row.get("gross_return_pct") is not None and row.get("market_state_run_id") is not None
    ]
    if not usable or not calibration_thresholds or not market_state_run_id:
        return {
            "version": REGIME_DIAGNOSTICS_VERSION,
            "available": False,
            "reason": "No completed full-timeline point-in-time market-state calibration was available for the target feature set.",
            "sealed_data_accessed": False,
        }

    thresholds = calibration_thresholds
    cost_pct = float(cost_bps) / 100.0
    returns = [float(row["gross_return_pct"]) - cost_pct for row in usable]

    def direction(row: dict[str, Any]) -> str | None:
        value = _finite(row.get("spy_return_30m_pct"))
        if value is None:
            return None
        if value > 0.25:
            return "rising_market"
        if value < -0.25:
            return "falling_market"
        return "sideways_market"

    def breadth(row: dict[str, Any]) -> str | None:
        value = _finite(row.get("pct_positive_30m"))
        if value is None:
            return None
        if value >= 60:
            return "broad_market_strength"
        if value <= 40:
            return "broad_market_weakness"
        return "mixed_breadth"

    def session(row: dict[str, Any]) -> str | None:
        minute = row.get("minute_of_day")
        if minute is None:
            return None
        minute = int(minute)
        if 570 <= minute < 630:
            return "opening_session"
        if 630 <= minute < 900:
            return "midday_session"
        if 900 <= minute < 960:
            return "closing_session"
        return None

    def quantile_label(field: str, threshold_key: str, low: str, normal: str, high: str):
        def label(row: dict[str, Any]) -> str | None:
            value = _finite(row.get(field))
            p25 = _finite((thresholds.get(threshold_key) or {}).get("p25"))
            p75 = _finite((thresholds.get(threshold_key) or {}).get("p75"))
            if value is None or p25 is None or p75 is None:
                return None
            if p25 == p75:
                return normal
            if value <= p25:
                return low
            if value >= p75:
                return high
            return normal
        return label

    def volatility_change(row: dict[str, Any]) -> str | None:
        current = _finite(row.get("pct_abnormal_volatility"))
        prior = _finite(row.get("prior_pct_abnormal_volatility"))
        if current is None or prior is None:
            return None
        delta = current - prior
        if delta >= 5.0:
            return "volatility_expansion"
        if delta <= -5.0:
            return "volatility_contraction"
        return "volatility_stable"

    dimensions = {
        "market_direction": direction,
        "broad_market_breadth": breadth,
        "session": session,
        "volatility_level": quantile_label(
            "pct_abnormal_volatility", "pct_abnormal_volatility",
            "low_volatility", "normal_volatility", "high_volatility",
        ),
        "volatility_change": volatility_change,
        "cross_sectional_dispersion": quantile_label(
            "dispersion_30m_pct", "dispersion_30m_pct",
            "low_dispersion", "normal_dispersion", "high_dispersion",
        ),
        "market_liquidity_activity": quantile_label(
            "median_relative_volume", "median_relative_volume",
            "low_liquidity_activity", "normal_liquidity_activity", "high_liquidity_activity",
        ),
    }

    grouped = {name: _group(usable, returns, labeler) for name, labeler in dimensions.items()}
    supported_regimes: list[dict[str, Any]] = []
    negative_supported_regimes: list[dict[str, Any]] = []
    for dimension_name, buckets in grouped.items():
        for label, stats in buckets.items():
            if int(stats.get("observations") or 0) >= 30 and int(stats.get("independent_timestamp_events") or 0) >= 5:
                item = {
                    "dimension": dimension_name,
                    "regime": label,
                    "observations": stats["observations"],
                    "events": stats["independent_timestamp_events"],
                    "mean_net_return_pct": stats.get("mean_net_return_pct"),
                    "event_mean_net_return_pct": stats.get("event_mean_net_return_pct"),
                }
                supported_regimes.append(item)
                if (stats.get("mean_net_return_pct") or 0.0) < 0 or (stats.get("event_mean_net_return_pct") or 0.0) < 0:
                    negative_supported_regimes.append(item)

    return {
        "version": REGIME_DIAGNOSTICS_VERSION,
        "available": True,
        "sealed_data_accessed": False,
        "market_state_run_id": market_state_run_id,
        "signal_rows_with_market_state": len(usable),
        "calibration_market_state_timestamps": int(calibration_market_state_timestamps),
        "threshold_methodology": {
            "calibration_population": "All timestamps in the latest completed pre-sealed point-in-time market-state run for the target feature set; thresholds are not calibrated from signal timestamps.",
            "direction": "SPY 30-minute return > +0.25% rising, < -0.25% falling, otherwise sideways.",
            "breadth": ">=60% positive over 30m = broad strength; <=40% = broad weakness; otherwise mixed.",
            "volatility_dispersion_liquidity": "Full pre-sealed market-state 25th/75th percentiles define low/high; middle 50% is normal. The percentile method is fixed before sealed holdout access.",
            "volatility_change": "Change in abnormal-volatility share versus 5 minutes earlier: >=+5 percentage points expansion, <=-5 contraction, otherwise stable.",
            "session": "09:30-10:30 ET opening; 10:30-15:00 ET midday; 15:00-16:00 ET closing.",
        },
        "calibration_thresholds": thresholds,
        "regimes": grouped,
        "supported_regime_count": len(supported_regimes),
        "negative_supported_regimes": negative_supported_regimes,
        "negative_supported_regime_count": len(negative_supported_regimes),
        "promotion_note": "Regime diagnostics describe unconditional stability. They must not be converted into rescue filters after seeing a weak regime; any conditional strategy requires a newly frozen candidate and future untouched holdout.",
    }

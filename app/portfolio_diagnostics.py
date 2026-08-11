from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

CAPITAL_SCENARIOS_USD = (1_000.0, 10_000.0, 100_000.0, 1_000_000.0)
POSITION_SLOT_SCENARIOS = (10, 25, 50)
PARTICIPATION_RATE_SCENARIOS = (0.01, 0.02, 0.05)


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


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


def _weekday_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def liquidity_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = [float(r["bar_dollar_volume"]) for r in rows if _finite(r.get("bar_dollar_volume")) is not None and float(r["bar_dollar_volume"]) > 0]
    trailing = [float(r["trailing_20bar_dollar_volume_proxy"]) for r in rows if _finite(r.get("trailing_20bar_dollar_volume_proxy")) is not None and float(r["trailing_20bar_dollar_volume_proxy"]) > 0]
    result: dict[str, Any] = {
        "observations": len(rows),
        "current_bar_dollar_volume_observations": len(current),
        "trailing_20bar_dollar_volume_observations": len(trailing),
        "missing_trailing_liquidity_pct": _finite(100.0 * (len(rows) - len(trailing)) / len(rows)) if rows else None,
        "current_bar_dollar_volume_usd": {
            "minimum": _finite(min(current)) if current else None,
            "p05": _finite(_quantile(current, 0.05)),
            "median": _finite(_quantile(current, 0.50)),
            "p95": _finite(_quantile(current, 0.95)),
        },
        "trailing_20bar_dollar_volume_proxy_usd": {
            "minimum": _finite(min(trailing)) if trailing else None,
            "p05": _finite(_quantile(trailing, 0.05)),
            "median": _finite(_quantile(trailing, 0.50)),
            "p95": _finite(_quantile(trailing, 0.95)),
        },
        "capacity_proxies": {},
        "methodology": "Capacity is bounded as a fixed percentage of point-in-time trailing 20-bar average minute volume multiplied by decision-time close. This is a participation proxy, not a calibrated market-impact model.",
    }
    for participation in PARTICIPATION_RATE_SCENARIOS:
        capacities = [v * participation for v in trailing]
        key = f"{int(participation * 100)}pct_participation"
        result["capacity_proxies"][key] = {
            "participation_rate_pct": participation * 100.0,
            "minimum_trade_capacity_usd": _finite(min(capacities)) if capacities else None,
            "p05_trade_capacity_usd": _finite(_quantile(capacities, 0.05)),
            "median_trade_capacity_usd": _finite(_quantile(capacities, 0.50)),
            "p95_trade_capacity_usd": _finite(_quantile(capacities, 0.95)),
        }
    return result


def _simulate(
    rows: list[dict[str, Any]], *, cost_bps: float, horizon_minutes: int, direction: str,
    capital_usd: float, position_slots: int, participation_rate: float,
) -> dict[str, Any]:
    usable: list[dict[str, Any]] = []
    missing_liquidity = 0
    for row in rows:
        gross = _finite(row.get("gross_return_pct"))
        liquidity = _finite(row.get("trailing_20bar_dollar_volume_proxy"))
        ts = row.get("bar_ts")
        if gross is None or not isinstance(ts, datetime):
            continue
        if liquidity is None or liquidity <= 0:
            missing_liquidity += 1
            continue
        item = dict(row)
        item["net_return_pct"] = gross - float(cost_bps) / 100.0
        item["capacity_usd"] = liquidity * participation_rate
        usable.append(item)
    usable.sort(key=lambda r: (r["bar_ts"], str(r.get("symbol") or "")))
    if not usable:
        return {
            "capital_usd": capital_usd,
            "position_slots": position_slots,
            "participation_rate_pct": participation_rate * 100.0,
            "available": False,
            "reason": "No signals with point-in-time liquidity data.",
        }

    sign = 1.0 if direction == "long" else -1.0
    start_date = min(r["trade_date"] for r in usable)
    end_date = max(r["trade_date"] for r in usable)
    market_dates = _weekday_dates(start_date, end_date)
    market_minutes = max(1, len(market_dates)) * 390.0
    slot_notional = capital_usd / max(1, int(position_slots))

    cash = float(capital_usd)
    active: list[dict[str, Any]] = []
    last_clock: datetime | None = None
    notional_minutes = 0.0
    signed_notional_minutes = 0.0
    peak_gross_exposure = 0.0
    peak_abs_net_exposure = 0.0
    total_entry_notional = 0.0
    capacity_constrained_allocations = 0
    signals_scaled_for_cash = 0
    signals_skipped_no_cash = 0
    signals_skipped_same_symbol = 0
    allocations = 0
    daily_pnl: dict[date, float] = defaultdict(float)
    equity_marks: list[float] = [float(capital_usd)]
    participation_realised: list[float] = []

    def active_notional() -> float:
        return sum(float(p["notional"]) for p in active)

    def mark_exposure() -> None:
        nonlocal peak_gross_exposure, peak_abs_net_exposure
        gross_pct = 100.0 * active_notional() / capital_usd
        net_pct = sign * gross_pct
        peak_gross_exposure = max(peak_gross_exposure, gross_pct)
        peak_abs_net_exposure = max(peak_abs_net_exposure, abs(net_pct))

    def integrate(to_ts: datetime) -> None:
        nonlocal last_clock, notional_minutes, signed_notional_minutes
        if last_clock is not None and to_ts > last_clock and active:
            minutes = (to_ts - last_clock).total_seconds() / 60.0
            deployed = active_notional()
            notional_minutes += deployed * minutes
            signed_notional_minutes += sign * deployed * minutes
        last_clock = to_ts

    def release(at_ts: datetime) -> None:
        nonlocal cash, active
        while True:
            exits = [p for p in active if p["exit_ts"] <= at_ts]
            if not exits:
                break
            next_exit = min(p["exit_ts"] for p in exits)
            integrate(next_exit)
            remaining: list[dict[str, Any]] = []
            for position in active:
                if position["exit_ts"] == next_exit:
                    pnl = position["notional"] * position["net_return_pct"] / 100.0
                    cash += position["notional"] + pnl
                    daily_pnl[position["exit_ts"].date()] += pnl
                else:
                    remaining.append(position)
            active = remaining
            equity_marks.append(cash + active_notional())
            mark_exposure()

    idx = 0
    while idx < len(usable):
        ts = usable[idx]["bar_ts"]
        batch: list[dict[str, Any]] = []
        while idx < len(usable) and usable[idx]["bar_ts"] == ts:
            batch.append(usable[idx])
            idx += 1
        release(ts)
        integrate(ts)
        active_symbols = {str(p["symbol"]) for p in active}
        eligible: list[dict[str, Any]] = []
        for row in batch:
            symbol = str(row.get("symbol") or "")
            if symbol in active_symbols:
                signals_skipped_same_symbol += 1
                continue
            eligible.append(row)
        if not eligible:
            continue
        desired = [min(slot_notional, float(r["capacity_usd"])) for r in eligible]
        capacity_constrained_allocations += sum(float(r["capacity_usd"]) < slot_notional for r in eligible)
        total_desired = sum(desired)
        if cash <= 0 or total_desired <= 0:
            signals_skipped_no_cash += len(eligible)
            continue
        scale = min(1.0, cash / total_desired)
        if scale < 1.0:
            signals_scaled_for_cash += len(eligible)
        for row, wanted in zip(eligible, desired):
            notional = wanted * scale
            if notional <= 1e-9:
                signals_skipped_no_cash += 1
                continue
            cash -= notional
            total_entry_notional += notional
            allocations += 1
            participation_realised.append(100.0 * notional / float(row["trailing_20bar_dollar_volume_proxy"]))
            active.append({
                "symbol": str(row.get("symbol") or ""),
                "notional": notional,
                "net_return_pct": float(row["net_return_pct"]),
                "exit_ts": ts + timedelta(minutes=int(horizon_minutes)),
            })
        mark_exposure()
        equity_marks.append(cash + active_notional())

    while active:
        next_exit = min(p["exit_ts"] for p in active)
        release(next_exit)

    ending_capital = cash
    total_pnl = ending_capital - capital_usd
    avg_util = 100.0 * notional_minutes / (capital_usd * market_minutes)
    avg_net = 100.0 * signed_notional_minutes / (capital_usd * market_minutes)
    peak = equity_marks[0]
    max_dd = 0.0
    for equity in equity_marks:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, 100.0 * (equity / peak - 1.0))
    market_day_returns = [100.0 * daily_pnl.get(d, 0.0) / capital_usd for d in market_dates]
    active_days = sum(abs(daily_pnl.get(d, 0.0)) > 0 for d in market_dates)
    return {
        "capital_usd": float(capital_usd),
        "position_slots": int(position_slots),
        "target_notional_per_signal_usd": _finite(slot_notional),
        "participation_rate_pct": participation_rate * 100.0,
        "available": True,
        "signals_with_liquidity": len(usable),
        "signals_missing_liquidity": missing_liquidity,
        "allocations": allocations,
        "capacity_constrained_allocations": capacity_constrained_allocations,
        "signals_scaled_for_cash": signals_scaled_for_cash,
        "signals_skipped_no_cash": signals_skipped_no_cash,
        "signals_skipped_same_symbol_no_pyramiding": signals_skipped_same_symbol,
        "ending_capital_usd": _finite(ending_capital),
        "net_profit_usd": _finite(total_pnl),
        "return_on_total_available_capital_pct": _finite(100.0 * total_pnl / capital_usd),
        "pnl_over_total_entry_notional_pct": _finite(100.0 * total_pnl / total_entry_notional) if total_entry_notional else None,
        "gross_entry_notional_turnover_usd": _finite(total_entry_notional),
        "round_trip_notional_turnover_usd": _finite(2.0 * total_entry_notional),
        "turnover_multiple_of_starting_capital": _finite(total_entry_notional / capital_usd),
        "average_capital_utilisation_during_market_time_pct": _finite(avg_util),
        "average_net_exposure_during_market_time_pct": _finite(avg_net),
        "peak_gross_exposure_pct_of_starting_capital": _finite(peak_gross_exposure),
        "peak_abs_net_exposure_pct_of_starting_capital": _finite(peak_abs_net_exposure),
        "market_days": len(market_dates),
        "active_market_days": active_days,
        "market_days_with_realised_pnl_pct": _finite(100.0 * active_days / len(market_dates)) if market_dates else None,
        "profitable_market_day_pct": _finite(100.0 * sum(v > 0 for v in market_day_returns) / len(market_day_returns)) if market_day_returns else None,
        "worst_market_day_pct_of_starting_capital": _finite(min(market_day_returns)) if market_day_returns else None,
        "maximum_realised_equity_drawdown_pct": _finite(max_dd),
        "median_realised_participation_pct": _finite(_quantile(participation_realised, 0.5)),
        "p95_realised_participation_pct": _finite(_quantile(participation_realised, 0.95)),
        "allocation_policy": "Fully collateralised, no leverage; fixed slot notional = starting capital / position_slots; no pyramiding in the same symbol; simultaneous signals share available cash proportionally; each trade is capped by the participation-rate liquidity proxy. Scenario grid is diagnostic and is not optimised or selected for promotion.",
        "drawdown_note": "Portfolio drawdown uses realised equity marks. Intratrade risk remains separately measured by MAE because pathwise unrealised P&L is not reconstructed here.",
    }


def portfolio_diagnostics(
    rows: list[dict[str, Any]], *, cost_bps: float, horizon_minutes: int, direction: str,
) -> dict[str, Any]:
    liquidity = liquidity_metrics(rows)
    scenarios: list[dict[str, Any]] = []
    for capital in CAPITAL_SCENARIOS_USD:
        for slots in POSITION_SLOT_SCENARIOS:
            for participation in PARTICIPATION_RATE_SCENARIOS:
                scenarios.append(
                    _simulate(
                        rows,
                        cost_bps=cost_bps,
                        horizon_minutes=horizon_minutes,
                        direction=direction,
                        capital_usd=capital,
                        position_slots=slots,
                        participation_rate=participation,
                    )
                )
    return {
        "available": bool(liquidity.get("trailing_20bar_dollar_volume_observations")),
        "liquidity": liquidity,
        "capital_scenario_grid": {
            "capital_levels_usd": list(CAPITAL_SCENARIOS_USD),
            "position_slot_counts": list(POSITION_SLOT_SCENARIOS),
            "participation_rates_pct": [x * 100.0 for x in PARTICIPATION_RATE_SCENARIOS],
            "scenario_count": len(scenarios),
            "selection_policy": "No scenario is labelled best and no scenario is used to tune the signal. A production sizing/allocation policy must be fixed before any sealed holdout evaluation.",
            "scenarios": scenarios,
        },
        "remaining_capacity_limitations": [
            "Participation caps are a liquidity proxy, not a market-impact model.",
            "Bid/ask depth and partial fills are not reconstructed from minute bars.",
            "Short borrow availability/fees are not available in this feature sample.",
            "Sector metadata is not available for sector concentration diagnostics.",
        ],
    }

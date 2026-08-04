from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.jobs import add_event, check_control, set_progress
from app.models import DiscoveryConfig, SealedEvaluationConfig
from app.utils import finite_or_none, json_safe


@dataclass(frozen=True)
class Dimension:
    name: str
    expression: str
    labels: dict[str, tuple[dict[str, Any], str]]


def _range(column: str, low: float | int | None, high: float | int | None, label: str) -> tuple[dict[str, Any], str]:
    return ({"column": column, "operator": "range", "low": low, "high": high}, label)


def _eq(column: str, value: Any, label: str) -> tuple[dict[str, Any], str]:
    return ({"column": column, "operator": "eq", "value": value}, label)


TIME = Dimension(
    "time_bin",
    """CASE
        WHEN minute_of_day>=570 AND minute_of_day<600 THEN '09:30–10:00 ET'
        WHEN minute_of_day>=600 AND minute_of_day<660 THEN '10:00–11:00 ET'
        WHEN minute_of_day>=660 AND minute_of_day<780 THEN '11:00–13:00 ET'
        WHEN minute_of_day>=780 AND minute_of_day<870 THEN '13:00–14:30 ET'
        WHEN minute_of_day>=870 AND minute_of_day<930 THEN '14:30–15:30 ET'
        WHEN minute_of_day>=930 AND minute_of_day<960 THEN '15:30–16:00 ET'
        ELSE 'Other time' END""",
    {
        "09:30–10:00 ET": _range("minute_of_day", 570, 600, "between 09:30 and 10:00 ET"),
        "10:00–11:00 ET": _range("minute_of_day", 600, 660, "between 10:00 and 11:00 ET"),
        "11:00–13:00 ET": _range("minute_of_day", 660, 780, "between 11:00 and 13:00 ET"),
        "13:00–14:30 ET": _range("minute_of_day", 780, 870, "between 13:00 and 14:30 ET"),
        "14:30–15:30 ET": _range("minute_of_day", 870, 930, "between 14:30 and 15:30 ET"),
        "15:30–16:00 ET": _range("minute_of_day", 930, 960, "between 15:30 and 16:00 ET"),
        "Other time": _range("minute_of_day", None, None, "outside the standard regular-session bins"),
    },
)
WEEKDAY = Dimension(
    "weekday_bin",
    "CASE weekday_iso WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday' WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday' ELSE 'Other' END",
    {day: _eq("weekday_iso", index, f"on {day}s") for index, day in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], 1)},
)
RET30 = Dimension(
    "ret30_bin",
    """CASE
        WHEN ret_30m_pct<=-5 THEN '≤ -5%'
        WHEN ret_30m_pct<=-3 THEN '-5% to -3%'
        WHEN ret_30m_pct<=-2 THEN '-3% to -2%'
        WHEN ret_30m_pct<=-1 THEN '-2% to -1%'
        WHEN ret_30m_pct<0 THEN '-1% to 0%'
        WHEN ret_30m_pct<1 THEN '0% to +1%'
        WHEN ret_30m_pct<2 THEN '+1% to +2%'
        WHEN ret_30m_pct<3 THEN '+2% to +3%'
        WHEN ret_30m_pct<5 THEN '+3% to +5%'
        ELSE '≥ +5%' END""",
    {
        "≤ -5%": _range("ret_30m_pct", None, -5, "the prior 30-minute return is at most -5%"),
        "-5% to -3%": _range("ret_30m_pct", -5, -3, "the prior 30-minute return is -5% to -3%"),
        "-3% to -2%": _range("ret_30m_pct", -3, -2, "the prior 30-minute return is -3% to -2%"),
        "-2% to -1%": _range("ret_30m_pct", -2, -1, "the prior 30-minute return is -2% to -1%"),
        "-1% to 0%": _range("ret_30m_pct", -1, 0, "the prior 30-minute return is -1% to 0%"),
        "0% to +1%": _range("ret_30m_pct", 0, 1, "the prior 30-minute return is 0% to +1%"),
        "+1% to +2%": _range("ret_30m_pct", 1, 2, "the prior 30-minute return is +1% to +2%"),
        "+2% to +3%": _range("ret_30m_pct", 2, 3, "the prior 30-minute return is +2% to +3%"),
        "+3% to +5%": _range("ret_30m_pct", 3, 5, "the prior 30-minute return is +3% to +5%"),
        "≥ +5%": _range("ret_30m_pct", 5, None, "the prior 30-minute return is at least +5%"),
    },
)
RET5 = Dimension(
    "ret5_bin",
    """CASE
        WHEN ret_5m_pct<=-2 THEN '≤ -2%'
        WHEN ret_5m_pct<=-1 THEN '-2% to -1%'
        WHEN ret_5m_pct<0 THEN '-1% to 0%'
        WHEN ret_5m_pct<1 THEN '0% to +1%'
        WHEN ret_5m_pct<2 THEN '+1% to +2%'
        ELSE '≥ +2%' END""",
    {
        "≤ -2%": _range("ret_5m_pct", None, -2, "the prior 5-minute return is at most -2%"),
        "-2% to -1%": _range("ret_5m_pct", -2, -1, "the prior 5-minute return is -2% to -1%"),
        "-1% to 0%": _range("ret_5m_pct", -1, 0, "the prior 5-minute return is -1% to 0%"),
        "0% to +1%": _range("ret_5m_pct", 0, 1, "the prior 5-minute return is 0% to +1%"),
        "+1% to +2%": _range("ret_5m_pct", 1, 2, "the prior 5-minute return is +1% to +2%"),
        "≥ +2%": _range("ret_5m_pct", 2, None, "the prior 5-minute return is at least +2%"),
    },
)
RVOL = Dimension(
    "rvol_bin",
    """CASE
        WHEN relative_volume_20bar<1 THEN '< 1x'
        WHEN relative_volume_20bar<1.5 THEN '1x to 1.5x'
        WHEN relative_volume_20bar<2 THEN '1.5x to 2x'
        WHEN relative_volume_20bar<3 THEN '2x to 3x'
        ELSE '≥ 3x' END""",
    {
        "< 1x": _range("relative_volume_20bar", None, 1, "relative volume is below 1x"),
        "1x to 1.5x": _range("relative_volume_20bar", 1, 1.5, "relative volume is 1x to 1.5x"),
        "1.5x to 2x": _range("relative_volume_20bar", 1.5, 2, "relative volume is 1.5x to 2x"),
        "2x to 3x": _range("relative_volume_20bar", 2, 3, "relative volume is 2x to 3x"),
        "≥ 3x": _range("relative_volume_20bar", 3, None, "relative volume is at least 3x"),
    },
)
VWAP = Dimension(
    "vwap_bin",
    """CASE
        WHEN distance_from_cumulative_vwap_pct<=-3 THEN '≤ -3%'
        WHEN distance_from_cumulative_vwap_pct<=-2 THEN '-3% to -2%'
        WHEN distance_from_cumulative_vwap_pct<=-1 THEN '-2% to -1%'
        WHEN distance_from_cumulative_vwap_pct<0 THEN '-1% to 0%'
        WHEN distance_from_cumulative_vwap_pct<1 THEN '0% to +1%'
        WHEN distance_from_cumulative_vwap_pct<2 THEN '+1% to +2%'
        WHEN distance_from_cumulative_vwap_pct<3 THEN '+2% to +3%'
        ELSE '≥ +3%' END""",
    {
        "≤ -3%": _range("distance_from_cumulative_vwap_pct", None, -3, "price is at least 3% below cumulative VWAP"),
        "-3% to -2%": _range("distance_from_cumulative_vwap_pct", -3, -2, "price is 2% to 3% below cumulative VWAP"),
        "-2% to -1%": _range("distance_from_cumulative_vwap_pct", -2, -1, "price is 1% to 2% below cumulative VWAP"),
        "-1% to 0%": _range("distance_from_cumulative_vwap_pct", -1, 0, "price is below cumulative VWAP by less than 1%"),
        "0% to +1%": _range("distance_from_cumulative_vwap_pct", 0, 1, "price is above cumulative VWAP by less than 1%"),
        "+1% to +2%": _range("distance_from_cumulative_vwap_pct", 1, 2, "price is 1% to 2% above cumulative VWAP"),
        "+2% to +3%": _range("distance_from_cumulative_vwap_pct", 2, 3, "price is 2% to 3% above cumulative VWAP"),
        "≥ +3%": _range("distance_from_cumulative_vwap_pct", 3, None, "price is at least 3% above cumulative VWAP"),
    },
)
RANGE_POS = Dimension(
    "range_bin",
    """CASE
        WHEN cumulative_range_position<0.2 THEN 'Bottom 20%'
        WHEN cumulative_range_position<0.4 THEN '20%–40%'
        WHEN cumulative_range_position<0.6 THEN '40%–60%'
        WHEN cumulative_range_position<0.8 THEN '60%–80%'
        ELSE 'Top 20%' END""",
    {
        "Bottom 20%": _range("cumulative_range_position", None, 0.2, "price is in the bottom 20% of the session range"),
        "20%–40%": _range("cumulative_range_position", 0.2, 0.4, "price is in the 20%–40% portion of the session range"),
        "40%–60%": _range("cumulative_range_position", 0.4, 0.6, "price is in the middle 40%–60% of the session range"),
        "60%–80%": _range("cumulative_range_position", 0.6, 0.8, "price is in the 60%–80% portion of the session range"),
        "Top 20%": _range("cumulative_range_position", 0.8, None, "price is in the top 20% of the session range"),
    },
)
GAP = Dimension(
    "gap_bin",
    """CASE
        WHEN gap_from_previous_regular_close_pct<=-5 THEN '≤ -5%'
        WHEN gap_from_previous_regular_close_pct<=-3 THEN '-5% to -3%'
        WHEN gap_from_previous_regular_close_pct<=-1 THEN '-3% to -1%'
        WHEN gap_from_previous_regular_close_pct<0 THEN '-1% to 0%'
        WHEN gap_from_previous_regular_close_pct<1 THEN '0% to +1%'
        WHEN gap_from_previous_regular_close_pct<3 THEN '+1% to +3%'
        WHEN gap_from_previous_regular_close_pct<5 THEN '+3% to +5%'
        ELSE '≥ +5%' END""",
    {
        "≤ -5%": _range("gap_from_previous_regular_close_pct", None, -5, "the opening gap is at most -5%"),
        "-5% to -3%": _range("gap_from_previous_regular_close_pct", -5, -3, "the opening gap is -5% to -3%"),
        "-3% to -1%": _range("gap_from_previous_regular_close_pct", -3, -1, "the opening gap is -3% to -1%"),
        "-1% to 0%": _range("gap_from_previous_regular_close_pct", -1, 0, "the opening gap is -1% to 0%"),
        "0% to +1%": _range("gap_from_previous_regular_close_pct", 0, 1, "the opening gap is 0% to +1%"),
        "+1% to +3%": _range("gap_from_previous_regular_close_pct", 1, 3, "the opening gap is +1% to +3%"),
        "+3% to +5%": _range("gap_from_previous_regular_close_pct", 3, 5, "the opening gap is +3% to +5%"),
        "≥ +5%": _range("gap_from_previous_regular_close_pct", 5, None, "the opening gap is at least +5%"),
    },
)
PREV_DAY = Dimension(
    "prev_day_bin",
    """CASE
        WHEN previous_day_return_pct<=-3 THEN '≤ -3%'
        WHEN previous_day_return_pct<=-1 THEN '-3% to -1%'
        WHEN previous_day_return_pct<0 THEN '-1% to 0%'
        WHEN previous_day_return_pct<1 THEN '0% to +1%'
        WHEN previous_day_return_pct<3 THEN '+1% to +3%'
        ELSE '≥ +3%' END""",
    {
        "≤ -3%": _range("previous_day_return_pct", None, -3, "the previous day returned at most -3%"),
        "-3% to -1%": _range("previous_day_return_pct", -3, -1, "the previous day returned -3% to -1%"),
        "-1% to 0%": _range("previous_day_return_pct", -1, 0, "the previous day returned -1% to 0%"),
        "0% to +1%": _range("previous_day_return_pct", 0, 1, "the previous day returned 0% to +1%"),
        "+1% to +3%": _range("previous_day_return_pct", 1, 3, "the previous day returned +1% to +3%"),
        "≥ +3%": _range("previous_day_return_pct", 3, None, "the previous day returned at least +3%"),
    },
)

FAMILIES: dict[str, dict[str, Any]] = {
    "time_of_day": {"dimensions": [TIME, WEEKDAY], "filter": "minute_of_day BETWEEN 570 AND 959"},
    "oversold_reversal": {"dimensions": [TIME, RET30, VWAP, RVOL], "filter": "ret_30m_pct<0 AND distance_from_cumulative_vwap_pct<0 AND relative_volume_20bar IS NOT NULL"},
    "momentum_continuation": {"dimensions": [TIME, RET30, RANGE_POS, RVOL], "filter": "ret_30m_pct>0 AND cumulative_range_position>=0.5 AND relative_volume_20bar IS NOT NULL"},
    "vwap_reversion": {"dimensions": [TIME, VWAP, RANGE_POS, RVOL], "filter": "abs(distance_from_cumulative_vwap_pct)>=1 AND relative_volume_20bar IS NOT NULL"},
    "gap_behavior": {"dimensions": [TIME, GAP, PREV_DAY], "filter": "abs(gap_from_previous_regular_close_pct)>=1 AND previous_day_return_pct IS NOT NULL"},
    "volume_shock": {"dimensions": [TIME, RVOL, RET5], "filter": "relative_volume_20bar>=1.5 AND ret_5m_pct IS NOT NULL"},
}

ALLOWED_CONDITION_COLUMNS = {
    "minute_of_day", "weekday_iso", "ret_30m_pct", "ret_5m_pct",
    "relative_volume_20bar", "distance_from_cumulative_vwap_pct",
    "cumulative_range_position", "gap_from_previous_regular_close_pct",
    "previous_day_return_pct",
}


def _period_group_query(dimensions: list[Dimension], family_filter: str, horizon: int, direction: str) -> str:
    aliases = [d.name for d in dimensions]
    select_dims = ",\n                ".join(f"{d.expression} AS {d.name}" for d in dimensions)
    group_dims = ",".join(aliases)
    join_dims = " AND ".join(f"x.{name} IS NOT DISTINCT FROM g.{name}" for name in aliases)
    outcome = f"fwd_return_{horizon}m_pct" if direction == "long" else f"-fwd_return_{horizon}m_pct"
    return f"""
        WITH base AS (
            SELECT symbol,trade_date,{select_dims},({outcome})::double precision AS outcome
            FROM ra_intraday_features
            WHERE feature_set_id=%s AND trade_date BETWEEN %s AND %s
              AND fwd_return_{horizon}m_pct IS NOT NULL AND ({family_filter})
        ), grouped AS (
            SELECT {group_dims},count(*)::bigint AS observations,
                count(DISTINCT symbol)::integer AS symbols,
                count(DISTINCT trade_date)::integer AS dates,
                avg(outcome)::double precision AS gross_avg_pct,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY outcome)::double precision AS median_pct,
                avg((outcome>0)::integer)::double precision*100 AS win_rate_pct,
                CASE WHEN stddev_samp(outcome)>0 THEN avg(outcome)/stddev_samp(outcome)*sqrt(count(*)) END::double precision AS t_stat,
                CASE WHEN abs(sum(outcome) FILTER (WHERE outcome<0))>0
                     THEN sum(outcome) FILTER (WHERE outcome>0)/abs(sum(outcome) FILTER (WHERE outcome<0)) END::double precision AS profit_factor,
                percentile_cont(0.05) WITHIN GROUP (ORDER BY outcome)::double precision AS p05_pct,
                min(outcome)::double precision AS worst_pct
            FROM base GROUP BY {group_dims}
        ), symbol_counts AS (
            SELECT {group_dims},symbol,count(*)::bigint AS n FROM base GROUP BY {group_dims},symbol
        ), symbol_max AS (
            SELECT {group_dims},max(n)::bigint AS max_symbol_n FROM symbol_counts GROUP BY {group_dims}
        ), date_counts AS (
            SELECT {group_dims},trade_date,count(*)::bigint AS n FROM base GROUP BY {group_dims},trade_date
        ), date_max AS (
            SELECT {group_dims},max(n)::bigint AS max_date_n FROM date_counts GROUP BY {group_dims}
        )
        SELECT g.*,100.0*s.max_symbol_n/NULLIF(g.observations,0) AS max_symbol_share_pct,
            100.0*d.max_date_n/NULLIF(g.observations,0) AS max_date_share_pct
        FROM grouped g
        JOIN symbol_max s ON {join_dims.replace('x.', 's.')}
        JOIN date_max d ON {join_dims.replace('x.', 'd.')}
    """


def _conditions(dimensions: list[Dimension], row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    conditions: list[dict[str, Any]] = []
    descriptions: list[str] = []
    for dim in dimensions:
        label = row[dim.name]
        condition, description = dim.labels[label]
        if condition["column"] in ALLOWED_CONDITION_COLUMNS and not (condition["operator"] == "range" and condition.get("low") is None and condition.get("high") is None):
            conditions.append(condition)
            descriptions.append(description)
    return conditions, descriptions


def _plain_rule(direction: str, descriptions: list[str], horizon: int) -> str:
    verb = "Buy" if direction == "long" else "Short"
    joined = ", ".join(descriptions[:-1]) + (f" and {descriptions[-1]}" if len(descriptions) > 1 else descriptions[0])
    return f"{verb} when {joined}; exit after {horizon} minutes."


def _rank_score(discovery: dict[str, Any], validation: dict[str, Any] | None, cost_pct: float) -> float:
    d_net = float(discovery["gross_avg_pct"] or 0) - cost_pct
    d_t = max(float(discovery["t_stat"] or 0), 0)
    concentration = max(0.05, 1 - float(discovery["max_symbol_share_pct"] or 100) / 100) * max(0.05, 1 - float(discovery["max_date_share_pct"] or 100) / 100)
    score = d_net * math.log1p(float(discovery["observations"])) * max(0.25, d_t) * concentration
    if validation:
        v_net = float(validation["gross_avg_pct"] or 0) - cost_pct
        if v_net <= 0:
            score *= 0.1
        else:
            stability = 1 - min(abs(d_net - v_net) / max(abs(d_net), abs(v_net), 0.01), 1)
            score *= (0.5 + 0.5 * stability) * min(1.5, max(0.25, float(validation["t_stat"] or 0)))
    return finite_or_none(score) or 0.0


def _normalise_stats(row: dict[str, Any] | None, cost_pct: float) -> dict[str, Any]:
    if not row:
        return {}
    observations = int(row["observations"] or 0)
    return {
        "observations": observations, "symbols": int(row["symbols"] or 0), "dates": int(row["dates"] or 0),
        "gross_avg_pct": finite_or_none(row["gross_avg_pct"]),
        "net_avg_pct": finite_or_none(float(row["gross_avg_pct"]) - cost_pct) if observations and row["gross_avg_pct"] is not None else None,
        "median_pct": finite_or_none(row["median_pct"]), "win_rate_pct": finite_or_none(row["win_rate_pct"]),
        "t_stat": finite_or_none(row["t_stat"]), "profit_factor": finite_or_none(row["profit_factor"]),
        "p05_pct": finite_or_none(row["p05_pct"]), "worst_pct": finite_or_none(row["worst_pct"]),
        "max_symbol_share_pct": finite_or_none(row["max_symbol_share_pct"]),
        "max_date_share_pct": finite_or_none(row["max_date_share_pct"]),
    }


def _run_group_query(feature_set_id: str, start: date, end: date, family: str, horizon: int, direction: str) -> tuple[list[Dimension], list[dict[str, Any]]]:
    spec = FAMILIES[family]
    dimensions: list[Dimension] = spec["dimensions"]
    query = _period_group_query(dimensions, spec["filter"], horizon, direction)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(get_settings().database_statement_timeout_seconds)}s'")
            cur.execute(query, (feature_set_id, start, end))
            rows = cur.fetchall()
        conn.rollback()
    return dimensions, [dict(row) for row in rows]


def run_discovery(job_id: str, config: DiscoveryConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,min_trade_date,max_trade_date FROM ra_feature_sets WHERE id=%s", (config.feature_set_id,))
            feature_set = cur.fetchone()
        conn.rollback()
    if not feature_set or feature_set["status"] != "completed":
        raise RuntimeError("The selected feature set does not exist or is not completed")
    periods = [(config.discovery_start, config.discovery_end)]
    if config.validation_start and config.validation_end:
        periods.append((config.validation_start, config.validation_end))
    if any(start < feature_set["min_trade_date"] or end > feature_set["max_trade_date"] for start, end in periods):
        raise ValueError(
            f"Discovery and validation dates must remain within the feature set: "
            f"{feature_set['min_trade_date']} to {feature_set['max_trade_date']}"
        )

    combinations = [
        (family, direction, int(horizon))
        for family in config.families
        for direction in config.directions
        for horizon in config.holding_horizons_minutes
    ]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_discovery_runs WHERE job_id=%s", (job_id,))
            existing = cur.fetchone()
            if existing:
                run_id = existing["id"]
                cur.execute(
                    "UPDATE ra_discovery_runs SET status='running',completed_at=NULL WHERE id=%s",
                    (run_id,),
                )
                cur.execute(
                    "UPDATE ra_discovery_tasks SET status='pending',error=NULL WHERE discovery_run_id=%s AND status IN ('running','failed','cancelled')",
                    (run_id,),
                )
            else:
                cur.execute(
                    "INSERT INTO ra_discovery_runs(job_id,feature_set_id,name,config) VALUES (%s,%s,%s,%s) RETURNING id",
                    (job_id, config.feature_set_id, config.name, Jsonb(config.model_dump(mode="json"))),
                )
                run_id = cur.fetchone()["id"]
            cur.executemany(
                """
                INSERT INTO ra_discovery_tasks(discovery_run_id,family,direction,holding_horizon_minutes)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """,
                [(run_id, family, direction, horizon) for family, direction, horizon in combinations],
            )
            cur.execute(
                "SELECT * FROM ra_discovery_tasks WHERE discovery_run_id=%s ORDER BY id",
                (run_id,),
            )
            tasks = [dict(row) for row in cur.fetchall()]
        conn.commit()

    completed = sum(1 for task in tasks if task["status"] == "completed")
    set_progress(job_id, "scanning rule families", completed, len(tasks), result={"discovery_run_id": run_id})
    cost_pct = config.round_trip_cost_bps / 100.0

    for task in tasks:
        if task["status"] == "completed":
            continue
        check_control(job_id)
        family, direction, horizon = task["family"], task["direction"], int(task["holding_horizon_minutes"])
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ra_discovery_tasks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",
                    (task["id"],),
                )
                cur.execute(
                    "DELETE FROM ra_candidate_rules WHERE discovery_run_id=%s AND family=%s AND direction=%s AND holding_horizon_minutes=%s",
                    (run_id, family, direction, horizon),
                )
            conn.commit()
        try:
            dimensions, discovery_rows = _run_group_query(
                str(config.feature_set_id), config.discovery_start, config.discovery_end,
                family, horizon, direction,
            )
            validation_map: dict[tuple[Any, ...], dict[str, Any]] = {}
            if config.validation_start and config.validation_end:
                _, validation_rows = _run_group_query(
                    str(config.feature_set_id), config.validation_start, config.validation_end,
                    family, horizon, direction,
                )
                validation_map = {tuple(row[d.name] for d in dimensions): row for row in validation_rows}

            eligible: list[tuple[float, dict[str, Any], dict[str, Any] | None, list[dict[str, Any]], str]] = []
            for row in discovery_rows:
                net = float(row["gross_avg_pct"] or 0) - cost_pct
                if (
                    int(row["observations"]) < config.minimum_observations
                    or int(row["symbols"]) < config.minimum_symbols
                    or int(row["dates"]) < config.minimum_dates
                    or float(row["max_symbol_share_pct"] or 100) > config.maximum_symbol_concentration_pct
                    or float(row["max_date_share_pct"] or 100) > config.maximum_date_concentration_pct
                    or net <= 0
                ):
                    continue
                key = tuple(row[d.name] for d in dimensions)
                validation = validation_map.get(key)
                conditions, descriptions = _conditions(dimensions, row)
                if not conditions:
                    continue
                plain = _plain_rule(direction, descriptions, horizon)
                eligible.append((_rank_score(row, validation, cost_pct), row, validation, conditions, plain))

            eligible.sort(key=lambda item: item[0], reverse=True)
            selected = eligible[: config.top_candidates_per_family]
            with connection() as conn:
                with conn.cursor() as cur:
                    for score, discovery_row, validation_row, conditions, plain in selected:
                        ds = _normalise_stats(discovery_row, cost_pct)
                        vs = _normalise_stats(validation_row, cost_pct)
                        cur.execute(
                            """
                            INSERT INTO ra_candidate_rules(
                                discovery_run_id,feature_set_id,family,direction,holding_horizon_minutes,
                                conditions,plain_english_rule,rank_score,
                                discovery_observations,discovery_symbols,discovery_dates,
                                discovery_gross_avg_pct,discovery_net_avg_pct,discovery_median_pct,
                                discovery_win_rate_pct,discovery_t_stat,discovery_profit_factor,
                                discovery_p05_pct,discovery_worst_pct,discovery_max_symbol_share_pct,
                                discovery_max_date_share_pct,
                                validation_observations,validation_symbols,validation_dates,
                                validation_gross_avg_pct,validation_net_avg_pct,validation_median_pct,
                                validation_win_rate_pct,validation_t_stat,validation_profit_factor,
                                validation_p05_pct,validation_worst_pct,validation_max_symbol_share_pct,
                                validation_max_date_share_pct
                            ) VALUES (
                                %s,%s,%s,%s,%s,%s,%s,%s,
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                            )
                            """,
                            (
                                run_id, config.feature_set_id, family, direction, horizon,
                                Jsonb(conditions), plain, score,
                                ds["observations"], ds["symbols"], ds["dates"], ds["gross_avg_pct"], ds["net_avg_pct"],
                                ds["median_pct"], ds["win_rate_pct"], ds["t_stat"], ds["profit_factor"], ds["p05_pct"],
                                ds["worst_pct"], ds["max_symbol_share_pct"], ds["max_date_share_pct"],
                                vs.get("observations"), vs.get("symbols"), vs.get("dates"), vs.get("gross_avg_pct"),
                                vs.get("net_avg_pct"), vs.get("median_pct"), vs.get("win_rate_pct"), vs.get("t_stat"),
                                vs.get("profit_factor"), vs.get("p05_pct"), vs.get("worst_pct"),
                                vs.get("max_symbol_share_pct"), vs.get("max_date_share_pct"),
                            ),
                        )
                    cur.execute(
                        """
                        UPDATE ra_discovery_tasks SET status='completed',groups_tested=%s,
                            candidates_retained=%s,completed_at=now(),error=NULL WHERE id=%s
                        """,
                        (len(discovery_rows), len(selected), task["id"]),
                    )
                conn.commit()
            completed += 1
            set_progress(job_id, f"completed {family} · {direction} · {horizon}m", completed, len(tasks))
            add_event(
                job_id, "family_scan_completed",
                f"{family}: tested {len(discovery_rows):,} groups and retained {len(selected):,} candidates for {direction} {horizon}m.",
            )
        except Exception as exc:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ra_discovery_tasks SET status='failed',error=%s WHERE id=%s",
                        (str(exc), task["id"]),
                    )
                    cur.execute("UPDATE ra_discovery_runs SET status='failed' WHERE id=%s", (run_id,))
                conn.commit()
            raise

    check_control(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(sum(groups_tested),0)::bigint AS tested,
                    COALESCE(sum(candidates_retained),0)::integer AS retained
                FROM ra_discovery_tasks WHERE discovery_run_id=%s
                """,
                (run_id,),
            )
            totals = cur.fetchone()
            cur.execute(
                "UPDATE ra_discovery_runs SET status='completed',candidates_tested=%s,candidates_retained=%s,completed_at=now() WHERE id=%s",
                (totals["tested"], totals["retained"], run_id),
            )
        conn.commit()
    result = {
        "discovery_run_id": run_id,
        "candidates_tested": totals["tested"],
        "candidates_retained": totals["retained"],
    }
    add_event(
        job_id, "discovery_completed",
        f"Discovery retained {totals['retained']:,} candidates from {totals['tested']:,} grouped tests.",
        details=result,
    )
    return json_safe(result)

def _condition_sql(conditions: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        column = condition.get("column")
        if column not in ALLOWED_CONDITION_COLUMNS:
            raise ValueError(f"Unsupported condition column: {column}")
        operator = condition.get("operator")
        if operator == "eq":
            clauses.append(f"{column}=%s")
            params.append(condition.get("value"))
        elif operator == "range":
            low, high = condition.get("low"), condition.get("high")
            if low is not None:
                clauses.append(f"{column}>=%s")
                params.append(low)
            if high is not None:
                clauses.append(f"{column}<%s")
                params.append(high)
        else:
            raise ValueError(f"Unsupported condition operator: {operator}")
    return " AND ".join(clauses) if clauses else "TRUE", params


def _exact_stats(feature_set_id: str, conditions: list[dict[str, Any]], direction: str, horizon: int, start: date, end: date, cost_pct: float) -> dict[str, Any]:
    where, condition_params = _condition_sql(conditions)
    outcome = f"fwd_return_{horizon}m_pct" if direction == "long" else f"-fwd_return_{horizon}m_pct"
    query = f"""
        WITH base AS (
            SELECT symbol,trade_date,({outcome})::double precision AS outcome
            FROM ra_intraday_features
            WHERE feature_set_id=%s AND trade_date BETWEEN %s AND %s
              AND fwd_return_{horizon}m_pct IS NOT NULL AND ({where})
        ), totals AS (
            SELECT count(*)::bigint AS observations,count(DISTINCT symbol)::integer AS symbols,
                count(DISTINCT trade_date)::integer AS dates,avg(outcome)::double precision AS gross_avg_pct,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY outcome)::double precision AS median_pct,
                avg((outcome>0)::integer)::double precision*100 AS win_rate_pct,
                CASE WHEN stddev_samp(outcome)>0 THEN avg(outcome)/stddev_samp(outcome)*sqrt(count(*)) END::double precision AS t_stat,
                CASE WHEN abs(sum(outcome) FILTER (WHERE outcome<0))>0
                     THEN sum(outcome) FILTER (WHERE outcome>0)/abs(sum(outcome) FILTER (WHERE outcome<0)) END::double precision AS profit_factor,
                percentile_cont(0.05) WITHIN GROUP (ORDER BY outcome)::double precision AS p05_pct,min(outcome)::double precision AS worst_pct
            FROM base
        ), sym AS (SELECT max(n) AS max_n FROM (SELECT symbol,count(*) n FROM base GROUP BY symbol)x),
             dat AS (SELECT max(n) AS max_n FROM (SELECT trade_date,count(*) n FROM base GROUP BY trade_date)x)
        SELECT t.*,100.0*sym.max_n/NULLIF(t.observations,0) AS max_symbol_share_pct,
            100.0*dat.max_n/NULLIF(t.observations,0) AS max_date_share_pct
        FROM totals t CROSS JOIN sym CROSS JOIN dat
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(get_settings().database_statement_timeout_seconds)}s'")
            cur.execute(query, (feature_set_id, start, end, *condition_params))
            row = cur.fetchone()
        conn.rollback()
    return _normalise_stats(dict(row), cost_pct)


def run_sealed_evaluation(job_id: str, config: SealedEvaluationConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*,d.config AS discovery_config,
                    f.min_trade_date AS feature_min_date,f.max_trade_date AS feature_max_date
                FROM ra_candidate_rules c
                JOIN ra_discovery_runs d ON d.id=c.discovery_run_id
                JOIN ra_feature_sets f ON f.id=c.feature_set_id
                WHERE c.id=%s
                """,
                (config.candidate_id,),
            )
            candidate = cur.fetchone()
        conn.rollback()
    if not candidate:
        raise RuntimeError("Candidate does not exist")
    discovery_config = candidate["discovery_config"]
    boundary = discovery_config.get("validation_end") or discovery_config["discovery_end"]
    if config.sealed_start.isoformat() <= boundary:
        raise ValueError(f"Sealed period must begin after {boundary}")
    if config.sealed_start < candidate["feature_min_date"] or config.sealed_end > candidate["feature_max_date"]:
        raise ValueError(
            f"Sealed dates must remain within the feature set: "
            f"{candidate['feature_min_date']} to {candidate['feature_max_date']}"
        )
    cost_pct = float(discovery_config["round_trip_cost_bps"]) / 100.0
    set_progress(job_id, "evaluating sealed period", 0, 1)
    stats = _exact_stats(
        str(candidate["feature_set_id"]), candidate["conditions"], candidate["direction"],
        int(candidate["holding_horizon_minutes"]), config.sealed_start, config.sealed_end, cost_pct,
    )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_candidate_rules SET workflow_status='sealed_tested',sealed_start=%s,sealed_end=%s,
                    sealed_observations=%s,sealed_net_avg_pct=%s,sealed_median_pct=%s,sealed_win_rate_pct=%s,
                    sealed_t_stat=%s,sealed_profit_factor=%s,sealed_evaluated_at=now() WHERE id=%s
                """,
                (
                    config.sealed_start, config.sealed_end, stats.get("observations"), stats.get("net_avg_pct"),
                    stats.get("median_pct"), stats.get("win_rate_pct"), stats.get("t_stat"),
                    stats.get("profit_factor"), config.candidate_id,
                ),
            )
        conn.commit()
    result = {"candidate_id": config.candidate_id, "sealed_start": config.sealed_start, "sealed_end": config.sealed_end, **stats}
    add_event(job_id, "sealed_evaluation_completed", "Sealed candidate evaluation completed.", details=result)
    set_progress(job_id, "complete", 1, 1, result=result)
    return json_safe(result)

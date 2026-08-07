from __future__ import annotations

import math
import random
import threading
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.jobs import JobInterrupted, add_event, check_control, set_progress
from app.models import DiscoveryConfig, SealedEvaluationConfig, timeframe_minutes
from app.sql_validation import validate_sql_bindings
from app.utils import ensure_feature_set_compatibility, finite_or_none, json_safe


DISCOVERY_VERSION = "2.2.0"
RULE_DEFINITION_VERSION = "2026-08-coverage-pack1-v1"
CAMPAIGN_DEFINITION_VERSION = "2026-08-research-integrity-pack1"


class DiscoveryQueryTimeout(RuntimeError):
    """Raised when one grouped discovery query exceeds its wall-clock deadline."""


@dataclass(frozen=True)
class Dimension:
    name: str
    expression: str
    labels: dict[str, tuple[dict[str, Any], str]]


def _range(
    column: str,
    low: float | int | None,
    high: float | int | None,
    label: str,
    *,
    low_inclusive: bool = True,
    high_inclusive: bool = False,
) -> tuple[dict[str, Any], str]:
    return (
        {
            "column": column,
            "operator": "range",
            "low": low,
            "high": high,
            "low_inclusive": low_inclusive,
            "high_inclusive": high_inclusive,
        },
        label,
    )


def _eq(column: str, value: Any, label: str) -> tuple[dict[str, Any], str]:
    return ({"column": column, "operator": "eq", "value": value}, label)


# SQL category values are machine-safe codes. Human-facing labels containing
# percent signs never enter a parameterised SQL string. Every CASE handles NULL
# explicitly, preventing missing predictors from falling into the terminal bin.
TIME = Dimension(
    "time_bin",
    """CASE
        WHEN minute_of_day>=570 AND minute_of_day<600 THEN 't0930_1000'
        WHEN minute_of_day>=600 AND minute_of_day<660 THEN 't1000_1100'
        WHEN minute_of_day>=660 AND minute_of_day<780 THEN 't1100_1300'
        WHEN minute_of_day>=780 AND minute_of_day<870 THEN 't1300_1430'
        WHEN minute_of_day>=870 AND minute_of_day<930 THEN 't1430_1530'
        WHEN minute_of_day>=930 AND minute_of_day<960 THEN 't1530_1600'
        ELSE NULL END""",
    {
        "t0930_1000": _range("minute_of_day", 570, 600, "between 09:30 and 10:00 ET"),
        "t1000_1100": _range("minute_of_day", 600, 660, "between 10:00 and 11:00 ET"),
        "t1100_1300": _range("minute_of_day", 660, 780, "between 11:00 and 13:00 ET"),
        "t1300_1430": _range("minute_of_day", 780, 870, "between 13:00 and 14:30 ET"),
        "t1430_1530": _range("minute_of_day", 870, 930, "between 14:30 and 15:30 ET"),
        "t1530_1600": _range("minute_of_day", 930, 960, "between 15:30 and 16:00 ET"),
    },
)
WEEKDAY = Dimension(
    "weekday_bin",
    "CASE weekday_iso WHEN 1 THEN 'mon' WHEN 2 THEN 'tue' WHEN 3 THEN 'wed' WHEN 4 THEN 'thu' WHEN 5 THEN 'fri' ELSE NULL END",
    {
        "mon": _eq("weekday_iso", 1, "on Mondays"),
        "tue": _eq("weekday_iso", 2, "on Tuesdays"),
        "wed": _eq("weekday_iso", 3, "on Wednesdays"),
        "thu": _eq("weekday_iso", 4, "on Thursdays"),
        "fri": _eq("weekday_iso", 5, "on Fridays"),
    },
)
RET30 = Dimension(
    "ret30_bin",
    """CASE
        WHEN ret_30m_pct IS NULL THEN NULL
        WHEN ret_30m_pct < -5 THEN 'lt_n5'
        WHEN ret_30m_pct < -3 THEN 'n5_n3'
        WHEN ret_30m_pct < -2 THEN 'n3_n2'
        WHEN ret_30m_pct < -1 THEN 'n2_n1'
        WHEN ret_30m_pct < 0 THEN 'n1_0'
        WHEN ret_30m_pct < 1 THEN '0_p1'
        WHEN ret_30m_pct < 2 THEN 'p1_p2'
        WHEN ret_30m_pct < 3 THEN 'p2_p3'
        WHEN ret_30m_pct < 5 THEN 'p3_p5'
        ELSE 'ge_p5' END""",
    {
        "lt_n5": _range("ret_30m_pct", None, -5, "the prior 30-minute return is below -5%"),
        "n5_n3": _range("ret_30m_pct", -5, -3, "the prior 30-minute return is -5% to below -3%"),
        "n3_n2": _range("ret_30m_pct", -3, -2, "the prior 30-minute return is -3% to below -2%"),
        "n2_n1": _range("ret_30m_pct", -2, -1, "the prior 30-minute return is -2% to below -1%"),
        "n1_0": _range("ret_30m_pct", -1, 0, "the prior 30-minute return is -1% to below 0%"),
        "0_p1": _range("ret_30m_pct", 0, 1, "the prior 30-minute return is 0% to below +1%"),
        "p1_p2": _range("ret_30m_pct", 1, 2, "the prior 30-minute return is +1% to below +2%"),
        "p2_p3": _range("ret_30m_pct", 2, 3, "the prior 30-minute return is +2% to below +3%"),
        "p3_p5": _range("ret_30m_pct", 3, 5, "the prior 30-minute return is +3% to below +5%"),
        "ge_p5": _range("ret_30m_pct", 5, None, "the prior 30-minute return is at least +5%"),
    },
)
RET5 = Dimension(
    "ret5_bin",
    """CASE
        WHEN ret_5m_pct IS NULL THEN NULL
        WHEN ret_5m_pct < -2 THEN 'lt_n2'
        WHEN ret_5m_pct < -1 THEN 'n2_n1'
        WHEN ret_5m_pct < 0 THEN 'n1_0'
        WHEN ret_5m_pct < 1 THEN '0_p1'
        WHEN ret_5m_pct < 2 THEN 'p1_p2'
        ELSE 'ge_p2' END""",
    {
        "lt_n2": _range("ret_5m_pct", None, -2, "the prior 5-minute return is below -2%"),
        "n2_n1": _range("ret_5m_pct", -2, -1, "the prior 5-minute return is -2% to below -1%"),
        "n1_0": _range("ret_5m_pct", -1, 0, "the prior 5-minute return is -1% to below 0%"),
        "0_p1": _range("ret_5m_pct", 0, 1, "the prior 5-minute return is 0% to below +1%"),
        "p1_p2": _range("ret_5m_pct", 1, 2, "the prior 5-minute return is +1% to below +2%"),
        "ge_p2": _range("ret_5m_pct", 2, None, "the prior 5-minute return is at least +2%"),
    },
)
RVOL = Dimension(
    "rvol_bin",
    """CASE
        WHEN relative_volume_20bar IS NULL THEN NULL
        WHEN relative_volume_20bar < 1 THEN 'lt_1x'
        WHEN relative_volume_20bar < 1.5 THEN 'x1_x15'
        WHEN relative_volume_20bar < 2 THEN 'x15_x2'
        WHEN relative_volume_20bar < 3 THEN 'x2_x3'
        ELSE 'ge_3x' END""",
    {
        "lt_1x": _range("relative_volume_20bar", None, 1, "relative volume is below 1x"),
        "x1_x15": _range("relative_volume_20bar", 1, 1.5, "relative volume is 1x to below 1.5x"),
        "x15_x2": _range("relative_volume_20bar", 1.5, 2, "relative volume is 1.5x to below 2x"),
        "x2_x3": _range("relative_volume_20bar", 2, 3, "relative volume is 2x to below 3x"),
        "ge_3x": _range("relative_volume_20bar", 3, None, "relative volume is at least 3x"),
    },
)
VWAP = Dimension(
    "vwap_bin",
    """CASE
        WHEN distance_from_cumulative_vwap_pct IS NULL THEN NULL
        WHEN distance_from_cumulative_vwap_pct < -3 THEN 'lt_n3'
        WHEN distance_from_cumulative_vwap_pct < -2 THEN 'n3_n2'
        WHEN distance_from_cumulative_vwap_pct < -1 THEN 'n2_n1'
        WHEN distance_from_cumulative_vwap_pct < 0 THEN 'n1_0'
        WHEN distance_from_cumulative_vwap_pct < 1 THEN '0_p1'
        WHEN distance_from_cumulative_vwap_pct < 2 THEN 'p1_p2'
        WHEN distance_from_cumulative_vwap_pct < 3 THEN 'p2_p3'
        ELSE 'ge_p3' END""",
    {
        "lt_n3": _range("distance_from_cumulative_vwap_pct", None, -3, "price is more than 3% below cumulative VWAP"),
        "n3_n2": _range("distance_from_cumulative_vwap_pct", -3, -2, "price is 2% to 3% below cumulative VWAP"),
        "n2_n1": _range("distance_from_cumulative_vwap_pct", -2, -1, "price is 1% to 2% below cumulative VWAP"),
        "n1_0": _range("distance_from_cumulative_vwap_pct", -1, 0, "price is below cumulative VWAP by less than 1%"),
        "0_p1": _range("distance_from_cumulative_vwap_pct", 0, 1, "price is above cumulative VWAP by less than 1%"),
        "p1_p2": _range("distance_from_cumulative_vwap_pct", 1, 2, "price is 1% to below 2% above cumulative VWAP"),
        "p2_p3": _range("distance_from_cumulative_vwap_pct", 2, 3, "price is 2% to below 3% above cumulative VWAP"),
        "ge_p3": _range("distance_from_cumulative_vwap_pct", 3, None, "price is at least 3% above cumulative VWAP"),
    },
)
RANGE_POS = Dimension(
    "range_bin",
    """CASE
        WHEN cumulative_range_position IS NULL THEN NULL
        WHEN cumulative_range_position < 0.2 THEN 'bottom20'
        WHEN cumulative_range_position < 0.4 THEN 'p20_p40'
        WHEN cumulative_range_position < 0.6 THEN 'p40_p60'
        WHEN cumulative_range_position < 0.8 THEN 'p60_p80'
        ELSE 'top20' END""",
    {
        "bottom20": _range("cumulative_range_position", None, 0.2, "price is in the bottom 20% of the session range"),
        "p20_p40": _range("cumulative_range_position", 0.2, 0.4, "price is in the 20% to below 40% portion of the session range"),
        "p40_p60": _range("cumulative_range_position", 0.4, 0.6, "price is in the 40% to below 60% portion of the session range"),
        "p60_p80": _range("cumulative_range_position", 0.6, 0.8, "price is in the 60% to below 80% portion of the session range"),
        "top20": _range("cumulative_range_position", 0.8, None, "price is in the top 20% of the session range"),
    },
)
GAP = Dimension(
    "gap_bin",
    """CASE
        WHEN gap_from_previous_regular_close_pct IS NULL THEN NULL
        WHEN gap_from_previous_regular_close_pct < -5 THEN 'lt_n5'
        WHEN gap_from_previous_regular_close_pct < -3 THEN 'n5_n3'
        WHEN gap_from_previous_regular_close_pct < -1 THEN 'n3_n1'
        WHEN gap_from_previous_regular_close_pct < 0 THEN 'n1_0'
        WHEN gap_from_previous_regular_close_pct < 1 THEN '0_p1'
        WHEN gap_from_previous_regular_close_pct < 3 THEN 'p1_p3'
        WHEN gap_from_previous_regular_close_pct < 5 THEN 'p3_p5'
        ELSE 'ge_p5' END""",
    {
        "lt_n5": _range("gap_from_previous_regular_close_pct", None, -5, "the opening gap is below -5%"),
        "n5_n3": _range("gap_from_previous_regular_close_pct", -5, -3, "the opening gap is -5% to below -3%"),
        "n3_n1": _range("gap_from_previous_regular_close_pct", -3, -1, "the opening gap is -3% to below -1%"),
        "n1_0": _range("gap_from_previous_regular_close_pct", -1, 0, "the opening gap is -1% to below 0%"),
        "0_p1": _range("gap_from_previous_regular_close_pct", 0, 1, "the opening gap is 0% to below +1%"),
        "p1_p3": _range("gap_from_previous_regular_close_pct", 1, 3, "the opening gap is +1% to below +3%"),
        "p3_p5": _range("gap_from_previous_regular_close_pct", 3, 5, "the opening gap is +3% to below +5%"),
        "ge_p5": _range("gap_from_previous_regular_close_pct", 5, None, "the opening gap is at least +5%"),
    },
)
PREV_DAY = Dimension(
    "prev_day_bin",
    """CASE
        WHEN previous_day_return_pct IS NULL THEN NULL
        WHEN previous_day_return_pct < -3 THEN 'lt_n3'
        WHEN previous_day_return_pct < -1 THEN 'n3_n1'
        WHEN previous_day_return_pct < 0 THEN 'n1_0'
        WHEN previous_day_return_pct < 1 THEN '0_p1'
        WHEN previous_day_return_pct < 3 THEN 'p1_p3'
        ELSE 'ge_p3' END""",
    {
        "lt_n3": _range("previous_day_return_pct", None, -3, "the previous day returned below -3%"),
        "n3_n1": _range("previous_day_return_pct", -3, -1, "the previous day returned -3% to below -1%"),
        "n1_0": _range("previous_day_return_pct", -1, 0, "the previous day returned -1% to below 0%"),
        "0_p1": _range("previous_day_return_pct", 0, 1, "the previous day returned 0% to below +1%"),
        "p1_p3": _range("previous_day_return_pct", 1, 3, "the previous day returned +1% to below +3%"),
        "ge_p3": _range("previous_day_return_pct", 3, None, "the previous day returned at least +3%"),
    },
)


RTCOUNT = Dimension(
    "rtcount_bin",
    """CASE
        WHEN relative_trade_count_20bar IS NULL THEN NULL
        WHEN relative_trade_count_20bar < 1 THEN 'lt_1x'
        WHEN relative_trade_count_20bar < 1.5 THEN 'x1_x15'
        WHEN relative_trade_count_20bar < 2 THEN 'x15_x2'
        WHEN relative_trade_count_20bar < 3 THEN 'x2_x3'
        ELSE 'ge_3x' END""",
    {
        "lt_1x": _range("relative_trade_count_20bar", None, 1, "relative trade count is below 1x"),
        "x1_x15": _range("relative_trade_count_20bar", 1, 1.5, "relative trade count is 1x to below 1.5x"),
        "x15_x2": _range("relative_trade_count_20bar", 1.5, 2, "relative trade count is 1.5x to below 2x"),
        "x2_x3": _range("relative_trade_count_20bar", 2, 3, "relative trade count is 2x to below 3x"),
        "ge_3x": _range("relative_trade_count_20bar", 3, None, "relative trade count is at least 3x"),
    },
)
ABS_RET5 = Dimension(
    "abs_ret5_bin",
    """CASE
        WHEN ret_5m_pct IS NULL THEN NULL
        WHEN abs(ret_5m_pct) < 0.25 THEN 'lt_p25'
        WHEN abs(ret_5m_pct) < 0.5 THEN 'p25_p5'
        WHEN abs(ret_5m_pct) < 1 THEN 'p5_p1'
        WHEN abs(ret_5m_pct) < 2 THEN 'p1_p2'
        ELSE 'ge_p2' END""",
    {
        "lt_p25": ({"column":"ret_5m_pct","operator":"abs_lt","value":0.25}, "the absolute prior 5-minute return is below 0.25%"),
        "p25_p5": ({"column":"ret_5m_pct","operator":"abs_range","low":0.25,"high":0.5}, "the absolute prior 5-minute return is 0.25% to below 0.5%"),
        "p5_p1": ({"column":"ret_5m_pct","operator":"abs_range","low":0.5,"high":1.0}, "the absolute prior 5-minute return is 0.5% to below 1%"),
        "p1_p2": ({"column":"ret_5m_pct","operator":"abs_range","low":1.0,"high":2.0}, "the absolute prior 5-minute return is 1% to below 2%"),
        "ge_p2": ({"column":"ret_5m_pct","operator":"abs_gte","value":2.0}, "the absolute prior 5-minute return is at least 2%"),
    },
)
VWAP_SIDE = Dimension(
    "vwap_side",
    """CASE
        WHEN distance_from_cumulative_vwap_pct IS NULL THEN NULL
        WHEN distance_from_cumulative_vwap_pct < -0.25 THEN 'below'
        WHEN distance_from_cumulative_vwap_pct <= 0.25 THEN 'near'
        ELSE 'above' END""",
    {
        "below": _range("distance_from_cumulative_vwap_pct", None, -0.25, "price is below cumulative VWAP by more than 0.25%"),
        "near": _range("distance_from_cumulative_vwap_pct", -0.25, 0.25, "price is within 0.25% of cumulative VWAP", high_inclusive=True),
        "above": _range("distance_from_cumulative_vwap_pct", 0.25, None, "price is above cumulative VWAP by more than 0.25%", low_inclusive=False),
    },
)
RVOL_CHANGE = Dimension(
    "rvol_change_bin",
    """CASE
        WHEN relative_volume_change_ratio IS NULL THEN NULL
        WHEN relative_volume_change_ratio < 0.7 THEN 'falling_fast'
        WHEN relative_volume_change_ratio < 0.95 THEN 'falling'
        WHEN relative_volume_change_ratio <= 1.05 THEN 'stable'
        WHEN relative_volume_change_ratio <= 1.4 THEN 'rising'
        ELSE 'rising_fast' END""",
    {
        "falling_fast": _range("relative_volume_change_ratio", None, 0.7, "relative volume has contracted by more than 30% versus five minutes earlier"),
        "falling": _range("relative_volume_change_ratio", 0.7, 0.95, "relative volume is falling versus five minutes earlier"),
        "stable": _range("relative_volume_change_ratio", 0.95, 1.05, "relative volume is broadly stable", high_inclusive=True),
        "rising": _range("relative_volume_change_ratio", 1.05, 1.4, "relative volume is rising versus five minutes earlier", low_inclusive=False, high_inclusive=True),
        "rising_fast": _range("relative_volume_change_ratio", 1.4, None, "relative volume has risen by more than 40% versus five minutes earlier", low_inclusive=False),
    },
)
RTCOUNT_CHANGE = Dimension(
    "rtcount_change_bin",
    """CASE
        WHEN relative_trade_count_change_ratio IS NULL THEN NULL
        WHEN relative_trade_count_change_ratio < 0.7 THEN 'falling_fast'
        WHEN relative_trade_count_change_ratio < 0.95 THEN 'falling'
        WHEN relative_trade_count_change_ratio <= 1.05 THEN 'stable'
        WHEN relative_trade_count_change_ratio <= 1.4 THEN 'rising'
        ELSE 'rising_fast' END""",
    {
        "falling_fast": _range("relative_trade_count_change_ratio", None, 0.7, "relative trade count has contracted by more than 30% versus five minutes earlier"),
        "falling": _range("relative_trade_count_change_ratio", 0.7, 0.95, "relative trade count is falling versus five minutes earlier"),
        "stable": _range("relative_trade_count_change_ratio", 0.95, 1.05, "relative trade count is broadly stable", high_inclusive=True),
        "rising": _range("relative_trade_count_change_ratio", 1.05, 1.4, "relative trade count is rising versus five minutes earlier", low_inclusive=False, high_inclusive=True),
        "rising_fast": _range("relative_trade_count_change_ratio", 1.4, None, "relative trade count has risen by more than 40% versus five minutes earlier", low_inclusive=False),
    },
)
RANGE_RATIO = Dimension(
    "range_ratio_bin",
    """CASE
        WHEN range_vs_previous_day_ratio IS NULL THEN NULL
        WHEN range_vs_previous_day_ratio < 0.15 THEN 'lt_p15'
        WHEN range_vs_previous_day_ratio < 0.25 THEN 'p15_p25'
        WHEN range_vs_previous_day_ratio < 0.4 THEN 'p25_p40'
        WHEN range_vs_previous_day_ratio < 0.6 THEN 'p40_p60'
        ELSE 'ge_p60' END""",
    {
        "lt_p15": _range("range_vs_previous_day_ratio", None, 0.15, "the rolling range is below 15% of the previous day's range"),
        "p15_p25": _range("range_vs_previous_day_ratio", 0.15, 0.25, "the rolling range is 15% to below 25% of the previous day's range"),
        "p25_p40": _range("range_vs_previous_day_ratio", 0.25, 0.4, "the rolling range is 25% to below 40% of the previous day's range"),
        "p40_p60": _range("range_vs_previous_day_ratio", 0.4, 0.6, "the rolling range is 40% to below 60% of the previous day's range"),
        "ge_p60": _range("range_vs_previous_day_ratio", 0.6, None, "the rolling range is at least 60% of the previous day's range"),
    },
)
VOL_RATIO = Dimension(
    "vol_ratio_bin",
    """CASE
        WHEN volatility_vs_previous_day_ratio IS NULL THEN NULL
        WHEN volatility_vs_previous_day_ratio < 0.5 THEN 'lt_p5'
        WHEN volatility_vs_previous_day_ratio < 0.8 THEN 'p5_p8'
        WHEN volatility_vs_previous_day_ratio < 1.2 THEN 'p8_p12'
        WHEN volatility_vs_previous_day_ratio < 2 THEN 'p12_p2'
        ELSE 'ge_p2' END""",
    {
        "lt_p5": _range("volatility_vs_previous_day_ratio", None, 0.5, "rolling realised volatility is below half the previous day's level"),
        "p5_p8": _range("volatility_vs_previous_day_ratio", 0.5, 0.8, "rolling realised volatility is 0.5x to below 0.8x the previous day's level"),
        "p8_p12": _range("volatility_vs_previous_day_ratio", 0.8, 1.2, "rolling realised volatility is near the previous day's level"),
        "p12_p2": _range("volatility_vs_previous_day_ratio", 1.2, 2.0, "rolling realised volatility is 1.2x to below 2x the previous day's level"),
        "ge_p2": _range("volatility_vs_previous_day_ratio", 2.0, None, "rolling realised volatility is at least 2x the previous day's level"),
    },
)
IMPACT_CHANGE = Dimension(
    "impact_change_bin",
    """CASE
        WHEN activity_impact_change_ratio IS NULL THEN NULL
        WHEN activity_impact_change_ratio < 0.5 THEN 'lt_p5'
        WHEN activity_impact_change_ratio < 0.8 THEN 'p5_p8'
        WHEN activity_impact_change_ratio <= 1.2 THEN 'p8_p12'
        WHEN activity_impact_change_ratio <= 2 THEN 'p12_p2'
        ELSE 'gt_p2' END""",
    {
        "lt_p5": _range("activity_impact_change_ratio", None, 0.5, "activity-adjusted price impact has fallen by more than half"),
        "p5_p8": _range("activity_impact_change_ratio", 0.5, 0.8, "activity-adjusted price impact is materially falling"),
        "p8_p12": _range("activity_impact_change_ratio", 0.8, 1.2, "activity-adjusted price impact is broadly stable", high_inclusive=True),
        "p12_p2": _range("activity_impact_change_ratio", 1.2, 2.0, "activity-adjusted price impact is rising", low_inclusive=False, high_inclusive=True),
        "gt_p2": _range("activity_impact_change_ratio", 2.0, None, "activity-adjusted price impact has more than doubled", low_inclusive=False),
    },
)
OPENING_STATE = Dimension(
    "opening_state_bin",
    "CASE WHEN opening_range_position IS NULL THEN NULL ELSE opening_range_position END",
    {
        "above": _eq("opening_range_position", "above", "price is above the completed first-30-minute opening range"),
        "inside": _eq("opening_range_position", "inside", "price is inside the completed first-30-minute opening range"),
        "below": _eq("opening_range_position", "below", "price is below the completed first-30-minute opening range"),
    },
)
MIDDAY_TIME = Dimension(
    "midday_time_bin",
    """CASE
        WHEN minute_of_day>=690 AND minute_of_day<750 THEN 't1130_1230'
        WHEN minute_of_day>=750 AND minute_of_day<810 THEN 't1230_1330'
        WHEN minute_of_day>=810 AND minute_of_day<840 THEN 't1330_1400'
        ELSE NULL END""",
    {
        "t1130_1230": _range("minute_of_day", 690, 750, "between 11:30 and 12:30 ET"),
        "t1230_1330": _range("minute_of_day", 750, 810, "between 12:30 and 13:30 ET"),
        "t1330_1400": _range("minute_of_day", 810, 840, "between 13:30 and 14:00 ET"),
    },
)
TOUCHED_HIGH = Dimension(
    "touched_high_bin",
    "CASE WHEN touched_session_high IS NULL THEN NULL WHEN touched_session_high THEN 'yes' ELSE 'no' END",
    {"yes": _eq("touched_session_high", True, "the current bar makes or touches a fresh session high")},
)

FAMILIES: dict[str, dict[str, Any]] = {
    "time_of_day": {
        "hypothesis_ids": ["BASE-TIME"], "hypothesis_version": "legacy-v1", "coverage": "BASELINE",
        "dimensions": [TIME, WEEKDAY],
        "filter": "minute_of_day>=570 AND minute_of_day<960 AND weekday_iso BETWEEN 1 AND 5",
        "constraints": [],
        "constraint_descriptions": [],
    },
    "oversold_reversal": {
        "hypothesis_ids": ["H01"], "hypothesis_version": "legacy-v1", "coverage": "PARTIAL",
        "dimensions": [TIME, RET30, VWAP, RVOL],
        "filter": "ret_30m_pct<0 AND distance_from_cumulative_vwap_pct<0 AND relative_volume_20bar IS NOT NULL",
        "constraints": [
            {"column": "ret_30m_pct", "operator": "lt", "value": 0},
            {"column": "distance_from_cumulative_vwap_pct", "operator": "lt", "value": 0},
            {"column": "relative_volume_20bar", "operator": "not_null"},
        ],
        "constraint_descriptions": [
            "the prior 30-minute return is negative",
            "price is below cumulative VWAP",
        ],
    },
    "momentum_continuation": {
        "hypothesis_ids": ["BASE-MOMENTUM"], "hypothesis_version": "legacy-v1", "coverage": "BASELINE",
        "dimensions": [TIME, RET30, RANGE_POS, RVOL],
        "filter": "ret_30m_pct>0 AND cumulative_range_position>=0.5 AND relative_volume_20bar IS NOT NULL",
        "constraints": [
            {"column": "ret_30m_pct", "operator": "gt", "value": 0},
            {"column": "cumulative_range_position", "operator": "gte", "value": 0.5},
            {"column": "relative_volume_20bar", "operator": "not_null"},
        ],
        "constraint_descriptions": [
            "the prior 30-minute return is positive",
            "price is in the upper half of the session range",
        ],
    },
    "vwap_reversion": {
        "hypothesis_ids": ["BASE-VWAP"], "hypothesis_version": "legacy-v1", "coverage": "BASELINE",
        "dimensions": [TIME, VWAP, RANGE_POS, RVOL],
        "filter": "abs(distance_from_cumulative_vwap_pct)>=1 AND cumulative_range_position IS NOT NULL AND relative_volume_20bar IS NOT NULL",
        "constraints": [
            {"column": "distance_from_cumulative_vwap_pct", "operator": "abs_gte", "value": 1},
            {"column": "cumulative_range_position", "operator": "not_null"},
            {"column": "relative_volume_20bar", "operator": "not_null"},
        ],
        "constraint_descriptions": [
            "price is at least 1% away from cumulative VWAP",
        ],
    },
    "gap_behavior": {
        "hypothesis_ids": ["H04", "H05"], "hypothesis_version": "legacy-v1", "coverage": "PARTIAL",
        "dimensions": [TIME, GAP, PREV_DAY],
        "filter": "abs(gap_from_previous_regular_close_pct)>=1 AND previous_day_return_pct IS NOT NULL",
        "constraints": [
            {"column": "gap_from_previous_regular_close_pct", "operator": "abs_gte", "value": 1},
            {"column": "previous_day_return_pct", "operator": "not_null"},
        ],
        "constraint_descriptions": [
            "the absolute opening gap is at least 1%",
        ],
    },
    "volume_shock": {
        "hypothesis_ids": ["H06"], "hypothesis_version": "legacy-v1", "coverage": "PARTIAL",
        "dimensions": [TIME, RVOL, RET5],
        "filter": "relative_volume_20bar>=1.5 AND ret_5m_pct IS NOT NULL",
        "constraints": [
            {"column": "relative_volume_20bar", "operator": "gte", "value": 1.5},
            {"column": "ret_5m_pct", "operator": "not_null"},
        ],
        "constraint_descriptions": ["relative volume is at least 1.5x"],
    },
    "dip_repair": {
        "hypothesis_ids": ["H01"], "hypothesis_version": "H01-v1-marketdata-only", "coverage": "PARTIAL",
        "dimensions": [TIME, RET30, VWAP_SIDE, RANGE_POS, RVOL_CHANGE, RTCOUNT_CHANGE],
        "filter": "ret_30m_pct<=-2 AND cumulative_range_position IS NOT NULL AND relative_volume_change_ratio IS NOT NULL AND relative_trade_count_change_ratio IS NOT NULL",
        "constraints": [
            {"column":"ret_30m_pct","operator":"lte","value":-2},
            {"column":"cumulative_range_position","operator":"not_null"},
            {"column":"relative_volume_change_ratio","operator":"not_null"},
            {"column":"relative_trade_count_change_ratio","operator":"not_null"},
        ],
        "constraint_descriptions": ["the stock has fallen at least 2% over the prior 30 minutes"],
    },
    "compression_expansion": {
        "hypothesis_ids": ["H03"], "hypothesis_version": "H03-v1-marketdata-only", "coverage": "PARTIAL",
        "dimensions": [MIDDAY_TIME, RANGE_RATIO, VOL_RATIO, RVOL, RTCOUNT],
        "filter": "minute_of_day>=690 AND minute_of_day<840 AND range_vs_previous_day_ratio<0.6 AND volatility_vs_previous_day_ratio IS NOT NULL AND relative_volume_20bar IS NOT NULL AND relative_trade_count_20bar IS NOT NULL",
        "constraints": [
            {"column":"minute_of_day","operator":"gte","value":690},
            {"column":"minute_of_day","operator":"lt","value":840},
            {"column":"range_vs_previous_day_ratio","operator":"lt","value":0.6},
            {"column":"volatility_vs_previous_day_ratio","operator":"not_null"},
        ],
        "constraint_descriptions": ["the stock is in a midday range-compression state"],
    },
    "gap_state": {
        "hypothesis_ids": ["H04", "H05"], "hypothesis_version": "H04-H05-v1-marketdata-only", "coverage": "PARTIAL",
        "dimensions": [TIME, GAP, VWAP_SIDE, OPENING_STATE, RVOL, RTCOUNT],
        "filter": "minute_of_day>=600 AND abs(gap_from_previous_regular_close_pct)>=1 AND opening_range_position IS NOT NULL AND relative_volume_20bar IS NOT NULL AND relative_trade_count_20bar IS NOT NULL",
        "constraints": [
            {"column":"minute_of_day","operator":"gte","value":600},
            {"column":"gap_from_previous_regular_close_pct","operator":"abs_gte","value":1},
            {"column":"opening_range_position","operator":"not_null"},
        ],
        "constraint_descriptions": ["the absolute opening gap is at least 1%", "the first 30-minute opening range is complete"],
    },
    "activity_absorption": {
        "hypothesis_ids": ["H06"], "hypothesis_version": "H06-v1-marketdata-only", "coverage": "PARTIAL",
        "dimensions": [TIME, RVOL, RTCOUNT, ABS_RET5, VWAP_SIDE],
        "filter": "(relative_volume_20bar>=1.5 OR relative_trade_count_20bar>=1.5) AND ret_5m_pct IS NOT NULL",
        "constraints": [
            {"column":"ret_5m_pct","operator":"not_null"},
        ],
        "constraint_descriptions": ["trading activity is abnormal relative to the recent baseline"],
    },
    "price_efficiency": {
        "hypothesis_ids": ["H07"], "hypothesis_version": "H07-v1-marketdata-only", "coverage": "PARTIAL",
        "dimensions": [TIME, RET5, IMPACT_CHANGE, RVOL],
        "filter": "abs(ret_5m_pct)>=1 AND activity_impact_change_ratio IS NOT NULL AND relative_volume_20bar IS NOT NULL",
        "constraints": [
            {"column":"ret_5m_pct","operator":"abs_gte","value":1},
            {"column":"activity_impact_change_ratio","operator":"not_null"},
        ],
        "constraint_descriptions": ["the stock has experienced at least a 1% five-minute volatility shock"],
    },
    "new_high_liquidity_divergence": {
        "hypothesis_ids": ["H12"], "hypothesis_version": "H12-v1-activity-proxy", "coverage": "PARTIAL",
        "dimensions": [TIME, TOUCHED_HIGH, RVOL_CHANGE, RTCOUNT_CHANGE, RET5],
        "filter": "touched_session_high AND relative_volume_change_ratio IS NOT NULL AND relative_trade_count_change_ratio IS NOT NULL",
        "constraints": [
            {"column":"touched_session_high","operator":"eq","value":True},
            {"column":"relative_volume_change_ratio","operator":"not_null"},
            {"column":"relative_trade_count_change_ratio","operator":"not_null"},
        ],
        "constraint_descriptions": ["the current bar makes or touches a fresh session high"],
    },
}

ALLOWED_CONDITION_COLUMNS = {
    "minute_of_day", "weekday_iso", "ret_30m_pct", "ret_5m_pct",
    "relative_volume_20bar", "distance_from_cumulative_vwap_pct",
    "cumulative_range_position", "gap_from_previous_regular_close_pct",
    "previous_day_return_pct", "relative_trade_count_20bar",
    "relative_volume_change_ratio", "relative_trade_count_change_ratio",
    "range_vs_previous_day_ratio", "volatility_vs_previous_day_ratio",
    "activity_impact_change_ratio", "opening_range_position", "touched_session_high",
}


STATISTICS_METHOD = "mergeable_histogram_0.1pct_v1"
HISTOGRAM_MIN_BIN = -200
HISTOGRAM_MAX_BIN = 200
SYMBOL_BUCKETS = 1024


def _date_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    if days < 1:
        raise ValueError("Chunk size must be at least one day")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _bucket_ranges(shards: int) -> list[tuple[int, int]]:
    if shards < 1 or shards > SYMBOL_BUCKETS:
        raise ValueError(f"Symbol shards must be between 1 and {SYMBOL_BUCKETS}")
    ranges: list[tuple[int, int]] = []
    for index in range(shards):
        start = math.floor(index * SYMBOL_BUCKETS / shards)
        end = math.floor((index + 1) * SYMBOL_BUCKETS / shards)
        if start < end:
            ranges.append((start, end))
    return ranges


def _sample_insert_query(
    selected_horizons: list[int] | tuple[int, ...],
    sample_stride_minutes: int,
    entry_anchor_minute: int,
) -> str:
    horizons = sorted({int(value) for value in selected_horizons})
    if not horizons or any(value not in {5, 15, 30, 60} for value in horizons):
        raise ValueError(f"Unsupported holding horizons: {horizons}")
    if sample_stride_minutes < 1:
        raise ValueError("Sample stride must be at least one minute")
    sampling = "TRUE" if sample_stride_minutes == 1 else (
        f"mod(minute_of_day - {int(entry_anchor_minute)}, {int(sample_stride_minutes)}) = 0"
    )
    outcome_filter = " OR ".join(f"fwd_return_{horizon}m_pct IS NOT NULL" for horizon in horizons)
    query = f"""
        INSERT INTO ra_discovery_samples(
            discovery_run_id,period_label,symbol_bucket,
            symbol,bar_ts,trade_date,minute_of_day,weekday_iso,liquidity_tier,
            close,price_group,ret_1m_pct,ret_5m_pct,ret_15m_pct,ret_30m_pct,ret_60m_pct,
            ret_from_session_open_pct,relative_volume_20bar,relative_trade_count_20bar,
            rolling_realised_volatility_30bar,rolling_range_30bar_pct,same_minute_relative_volume,
            distance_from_cumulative_vwap_pct,cumulative_range_position,
            gap_from_previous_regular_close_pct,previous_day_return_pct,previous_day_range_pct,
            previous_day_realised_volatility,activity_adjusted_return_5m,prior_activity_adjusted_return_5m,
            activity_impact_change_ratio,prior_relative_volume_20bar,prior_relative_trade_count_20bar,
            relative_volume_change_ratio,relative_trade_count_change_ratio,
            range_vs_previous_day_ratio,volatility_vs_previous_day_ratio,
            opening_range_high,opening_range_low,opening_range_position,touched_session_high,touched_session_low,
            fwd_return_5m_pct,fwd_return_15m_pct,fwd_return_30m_pct,fwd_return_60m_pct
        )
        WITH source AS MATERIALIZED (
            SELECT
                f.*,
                CASE WHEN f.close < 5 THEN 'lt_5' WHEN f.close < 10 THEN '5_10'
                     WHEN f.close < 25 THEN '10_25' WHEN f.close < 100 THEN '25_100' ELSE 'ge_100' END AS price_group_calc,
                CASE WHEN f.ret_5m_pct IS NOT NULL AND f.relative_volume_20bar > 0
                     THEN abs(f.ret_5m_pct) / f.relative_volume_20bar END AS activity_adjusted_return_calc,
                CASE WHEN p.ret_5m_pct IS NOT NULL AND p.relative_volume_20bar > 0
                     THEN abs(p.ret_5m_pct) / p.relative_volume_20bar END AS prior_activity_adjusted_return_calc,
                p.relative_volume_20bar AS prior_rvol_calc,
                p.relative_trade_count_20bar AS prior_rtcount_calc,
                max(f.high) FILTER (WHERE f.minute_of_day < 600)
                    OVER (PARTITION BY f.symbol,f.trade_date) AS opening_high_calc,
                min(f.low) FILTER (WHERE f.minute_of_day < 600)
                    OVER (PARTITION BY f.symbol,f.trade_date) AS opening_low_calc
            FROM ra_intraday_features f
            LEFT JOIN ra_intraday_features p
              ON p.feature_set_id=f.feature_set_id
             AND p.symbol=f.symbol
             AND p.bar_ts=f.bar_ts-interval '5 minutes'
            WHERE f.feature_set_id=%s
              AND f.bar_ts >= (%s::date::timestamp AT TIME ZONE 'America/New_York')
              AND f.bar_ts < (((%s::date + 1)::timestamp) AT TIME ZONE 'America/New_York')
              AND f.trade_date BETWEEN %s AND %s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) >= %s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) < %s
        ), enriched AS (
            SELECT source.*,
                CASE WHEN activity_adjusted_return_calc IS NOT NULL AND prior_activity_adjusted_return_calc > 0
                     THEN activity_adjusted_return_calc/prior_activity_adjusted_return_calc END AS impact_change_calc,
                CASE WHEN relative_volume_20bar IS NOT NULL AND prior_rvol_calc > 0
                     THEN relative_volume_20bar/prior_rvol_calc END AS rvol_change_calc,
                CASE WHEN relative_trade_count_20bar IS NOT NULL AND prior_rtcount_calc > 0
                     THEN relative_trade_count_20bar/prior_rtcount_calc END AS rtcount_change_calc,
                CASE WHEN rolling_range_30bar_pct IS NOT NULL AND previous_day_range_pct > 0
                     THEN rolling_range_30bar_pct/previous_day_range_pct END AS range_ratio_calc,
                CASE WHEN rolling_realised_volatility_30bar IS NOT NULL AND previous_day_realised_volatility > 0
                     THEN rolling_realised_volatility_30bar/previous_day_realised_volatility END AS vol_ratio_calc,
                CASE WHEN minute_of_day < 600 OR opening_high_calc IS NULL OR opening_low_calc IS NULL THEN NULL
                     WHEN close > opening_high_calc THEN 'above'
                     WHEN close < opening_low_calc THEN 'below' ELSE 'inside' END AS opening_position_calc
            FROM source
        )
        SELECT
            %s,%s,
            mod(abs(hashtext(symbol)::bigint), {SYMBOL_BUCKETS})::smallint,
            symbol,bar_ts,trade_date,minute_of_day,weekday_iso,liquidity_tier,
            close,price_group_calc,ret_1m_pct,ret_5m_pct,ret_15m_pct,ret_30m_pct,ret_60m_pct,
            ret_from_session_open_pct,relative_volume_20bar,relative_trade_count_20bar,
            rolling_realised_volatility_30bar,rolling_range_30bar_pct,same_minute_relative_volume,
            distance_from_cumulative_vwap_pct,cumulative_range_position,
            gap_from_previous_regular_close_pct,previous_day_return_pct,previous_day_range_pct,
            previous_day_realised_volatility,activity_adjusted_return_calc,prior_activity_adjusted_return_calc,
            impact_change_calc,prior_rvol_calc,prior_rtcount_calc,rvol_change_calc,rtcount_change_calc,
            range_ratio_calc,vol_ratio_calc,opening_high_calc,opening_low_calc,opening_position_calc,
            (high>=cumulative_high),(low<=cumulative_low),
            fwd_return_5m_pct,fwd_return_15m_pct,fwd_return_30m_pct,fwd_return_60m_pct
        FROM enriched
        WHERE ({outcome_filter})
          AND ({sampling})
        ON CONFLICT DO NOTHING
    """
    params = ("feature", date.today(), date.today(), date.today(), date.today(), 0, 256, "run", "discovery")
    validate_sql_bindings(query, params, name="sample insert query")
    return query


def _partial_insert_query(
    dimensions: list[Dimension], family_filter: str, direction: str,
    horizon: int, entry_stride_minutes: int, entry_anchor_minute: int,
) -> str:
    if direction not in {"long", "short"}:
        raise ValueError(f"Unsupported direction: {direction}")
    if horizon not in {5, 15, 30, 60}:
        raise ValueError(f"Unsupported holding horizon: {horizon}")
    if entry_stride_minutes < 1:
        raise ValueError("Entry stride must be at least one minute")
    aliases = [dimension.name for dimension in dimensions]
    select_dims = ",\n                ".join(
        f"{dimension.expression} AS {dimension.name}" for dimension in dimensions
    )
    group_dims = ",".join(aliases)
    non_null = " AND ".join(f"{alias} IS NOT NULL" for alias in aliases)
    joins = lambda left, right: " AND ".join(f"{left}.{a}={right}.{a}" for a in aliases)
    key_expr = "concat_ws('|', " + ",".join(f"m.{alias}" for alias in aliases) + ")"
    json_args = ",".join(f"'{a}',m.{a}" for a in aliases)
    outcome_column = f"fwd_return_{horizon}m_pct"
    gross = outcome_column if direction == "long" else f"-{outcome_column}"
    sampling = "TRUE" if entry_stride_minutes == 1 else (
        f"mod(minute_of_day - {int(entry_anchor_minute)}, {int(entry_stride_minutes)}) = 0"
    )
    query = f"""
        INSERT INTO ra_discovery_partials(
            discovery_task_chunk_id,group_key,group_values,
            observations,gross_sum,net_sum,net_sum_squares,wins,
            positive_sum,negative_sum_abs,worst_pct,best_pct,histogram,symbol_counts,date_counts
        )
        WITH categorised AS MATERIALIZED (
            SELECT symbol,trade_date,{select_dims},
                ({gross})::double precision AS gross_outcome,
                (({gross}) - %s::double precision)::double precision AS net_outcome
            FROM ra_discovery_samples
            WHERE discovery_run_id=%s
              AND period_label=%s
              AND trade_date BETWEEN %s AND %s
              AND symbol_bucket >= %s AND symbol_bucket < %s
              AND {outcome_column} IS NOT NULL
              AND ({sampling})
              AND ({family_filter})
        ), base AS MATERIALIZED (
            SELECT *,
                greatest({HISTOGRAM_MIN_BIN},least({HISTOGRAM_MAX_BIN},floor(net_outcome*10)::integer)) AS hist_bin
            FROM categorised
            WHERE {non_null}
        ), metrics AS (
            SELECT {group_dims},count(*)::bigint AS observations,
                sum(gross_outcome)::double precision AS gross_sum,
                sum(net_outcome)::double precision AS net_sum,
                sum(net_outcome*net_outcome)::double precision AS net_sum_squares,
                count(*) FILTER (WHERE net_outcome>0)::bigint AS wins,
                COALESCE(sum(net_outcome) FILTER (WHERE net_outcome>0),0)::double precision AS positive_sum,
                COALESCE(abs(sum(net_outcome) FILTER (WHERE net_outcome<0)),0)::double precision AS negative_sum_abs,
                min(net_outcome)::double precision AS worst_pct,
                max(net_outcome)::double precision AS best_pct
            FROM base GROUP BY {group_dims}
        ), symbol_counts AS (
            SELECT {group_dims},symbol,count(*)::bigint AS n
            FROM base GROUP BY {group_dims},symbol
        ), symbols AS (
            SELECT {group_dims},jsonb_object_agg(symbol,n) AS symbol_counts
            FROM symbol_counts GROUP BY {group_dims}
        ), date_counts AS (
            SELECT {group_dims},trade_date,count(*)::bigint AS n
            FROM base GROUP BY {group_dims},trade_date
        ), dates AS (
            SELECT {group_dims},jsonb_object_agg(trade_date::text,n) AS date_counts
            FROM date_counts GROUP BY {group_dims}
        ), hist_counts AS (
            SELECT {group_dims},hist_bin,count(*)::bigint AS n
            FROM base GROUP BY {group_dims},hist_bin
        ), histograms AS (
            SELECT {group_dims},jsonb_object_agg(hist_bin::text,n) AS histogram
            FROM hist_counts GROUP BY {group_dims}
        )
        SELECT %s,{key_expr},jsonb_build_object({json_args}),
            m.observations,m.gross_sum,m.net_sum,m.net_sum_squares,m.wins,
            m.positive_sum,m.negative_sum_abs,m.worst_pct,m.best_pct,
            h.histogram,s.symbol_counts,d.date_counts
        FROM metrics m
        JOIN symbols s ON {joins('s','m')}
        JOIN dates d ON {joins('d','m')}
        JOIN histograms h ON {joins('h','m')}
        WHERE TRUE
        ON CONFLICT(discovery_task_chunk_id,group_key) DO UPDATE SET
            group_values=excluded.group_values,observations=excluded.observations,
            gross_sum=excluded.gross_sum,net_sum=excluded.net_sum,
            net_sum_squares=excluded.net_sum_squares,wins=excluded.wins,
            positive_sum=excluded.positive_sum,negative_sum_abs=excluded.negative_sum_abs,
            worst_pct=excluded.worst_pct,best_pct=excluded.best_pct,histogram=excluded.histogram,
            symbol_counts=excluded.symbol_counts,date_counts=excluded.date_counts
    """
    params = (0.2, "run", "discovery", date.today(), date.today(), 0, 256, 1)
    validate_sql_bindings(query, params, name="partial insert query")
    return query


def _condition_sql(conditions: list[dict[str, Any]], alias: str = "") -> tuple[str, list[Any]]:
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        column = condition.get("column")
        if column not in ALLOWED_CONDITION_COLUMNS:
            raise ValueError(f"Unsupported condition column: {column}")
        operator = condition.get("operator")
        if operator == "eq":
            clauses.append(f"{prefix}{column}=%s")
            params.append(condition.get("value"))
        elif operator in {"gt", "gte", "lt", "lte"}:
            sql_operator = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
            clauses.append(f"{prefix}{column}{sql_operator}%s")
            params.append(condition.get("value"))
        elif operator == "abs_gte":
            clauses.append(f"abs({prefix}{column})>=%s")
            params.append(condition.get("value"))
        elif operator == "abs_lt":
            clauses.append(f"abs({prefix}{column})<%s")
            params.append(condition.get("value"))
        elif operator == "abs_range":
            low, high = condition.get("low"), condition.get("high")
            if low is None or high is None:
                raise ValueError(f"Absolute range requires both bounds for {column}")
            clauses.append(f"abs({prefix}{column})>=%s")
            params.append(low)
            clauses.append(f"abs({prefix}{column})<%s")
            params.append(high)
        elif operator == "not_null":
            clauses.append(f"{prefix}{column} IS NOT NULL")
        elif operator == "range":
            low, high = condition.get("low"), condition.get("high")
            if low is None and high is None:
                raise ValueError(f"Unbounded condition is not permitted for {column}")
            if low is not None:
                clauses.append(f"{prefix}{column}{'>=' if condition.get('low_inclusive', True) else '>'}%s")
                params.append(low)
            if high is not None:
                clauses.append(f"{prefix}{column}{'<=' if condition.get('high_inclusive', False) else '<'}%s")
                params.append(high)
        else:
            raise ValueError(f"Unsupported condition operator: {operator}")
    query = " AND ".join(clauses) if clauses else "TRUE"
    validate_sql_bindings(query, params, name="candidate condition SQL")
    return query, params


def _sealed_partial_query(
    conditions: list[dict[str, Any]], direction: str, horizon: int,
    entry_stride_minutes: int, entry_anchor_minute: int,
) -> tuple[str, list[Any]]:
    if direction not in {"long", "short"}:
        raise ValueError(f"Unsupported direction: {direction}")
    if horizon not in {5, 15, 30, 60}:
        raise ValueError(f"Unsupported holding horizon: {horizon}")
    where, condition_params = _condition_sql(conditions)
    outcome_col = f"fwd_return_{horizon}m_pct"
    gross = outcome_col if direction == "long" else f"-{outcome_col}"
    sampling = "TRUE" if entry_stride_minutes == 1 else (
        f"mod(minute_of_day - {int(entry_anchor_minute)}, {int(entry_stride_minutes)}) = 0"
    )
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
              AND f.bar_ts >= (%s::date::timestamp AT TIME ZONE 'America/New_York')
              AND f.bar_ts < (((%s::date + 1)::timestamp) AT TIME ZONE 'America/New_York')
              AND f.trade_date BETWEEN %s AND %s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) >= %s
              AND mod(abs(hashtext(f.symbol)::bigint), {SYMBOL_BUCKETS}) < %s
        ), enriched AS MATERIALIZED (
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
        ), base AS MATERIALIZED (
            SELECT symbol,trade_date,({gross})::double precision AS gross_outcome,
                (({gross})-%s::double precision)::double precision AS net_outcome
            FROM enriched
            WHERE {outcome_col} IS NOT NULL
              AND ({sampling})
              AND ({where})
        ), metrics AS (
            SELECT count(*)::bigint AS observations,
                COALESCE(sum(gross_outcome),0)::double precision AS gross_sum,
                COALESCE(sum(net_outcome),0)::double precision AS net_sum,
                COALESCE(sum(net_outcome*net_outcome),0)::double precision AS net_sum_squares,
                count(*) FILTER (WHERE net_outcome>0)::bigint AS wins,
                COALESCE(sum(net_outcome) FILTER (WHERE net_outcome>0),0)::double precision AS positive_sum,
                COALESCE(abs(sum(net_outcome) FILTER (WHERE net_outcome<0)),0)::double precision AS negative_sum_abs,
                min(net_outcome)::double precision AS worst_pct,
                max(net_outcome)::double precision AS best_pct
            FROM base
        ), sym AS (
            SELECT COALESCE(jsonb_object_agg(symbol,n),'{{}}'::jsonb) AS symbol_counts
            FROM (SELECT symbol,count(*)::bigint n FROM base GROUP BY symbol) x
        ), dat AS (
            SELECT COALESCE(jsonb_object_agg(trade_date::text,n),'{{}}'::jsonb) AS date_counts
            FROM (SELECT trade_date,count(*)::bigint n FROM base GROUP BY trade_date) x
        ), hist AS (
            SELECT COALESCE(jsonb_object_agg(hist_bin::text,n),'{{}}'::jsonb) AS histogram
            FROM (
                SELECT greatest({HISTOGRAM_MIN_BIN},least({HISTOGRAM_MAX_BIN},floor(net_outcome*10)::integer)) AS hist_bin,
                    count(*)::bigint AS n
                FROM base GROUP BY 1
            ) x
        )
        SELECT m.*,sym.symbol_counts,dat.date_counts,hist.histogram
        FROM metrics m CROSS JOIN sym CROSS JOIN dat CROSS JOIN hist
    """
    sample_params = ("feature", date.today(), date.today(), date.today(), date.today(), 0, 256, 0.2, *condition_params)
    validate_sql_bindings(query, sample_params, name="sealed partial query")
    return query, condition_params


def _is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, DiscoveryQueryTimeout) or "statement timeout" in text or "wall-clock" in text


def _is_retryable_database_error(exc: Exception) -> bool:
    """Return True for transient database failures that are safe to replay.

    Every discovery write is idempotent at chunk level: the exact partial/sample
    slice is deleted before replay and the chunk owns a unique key. That makes
    deadlocks, serialization failures, short lock timeouts and dropped pooled
    connections retryable without mixing partial statistics.
    """
    text = str(exc).lower()
    markers = (
        "deadlock detected",
        "could not serialize access",
        "serialization failure",
        "lock timeout",
        "canceling statement due to lock timeout",
        "connection is closed",
        "connection reset",
        "server closed the connection",
        "terminating connection",
        "ssl connection has been closed",
    )
    return any(marker in text for marker in markers)


def _retry_chunk(table: str, chunk_id: int, error: Exception, job_id: str, event_type: str, attempts: int) -> bool:
    maximum = max(1, int(get_settings().discovery_query_retries))
    if not _is_retryable_database_error(error) or attempts >= maximum:
        return False
    delay = min(10.0, 0.75 * (2 ** max(0, attempts - 1)) + random.random())
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET status='pending',error=%s WHERE id=%s",
                (f"Transient database error; retrying after {delay:.1f}s: {error}", chunk_id),
            )
        conn.commit()
    add_event(
        job_id, event_type, f"Transient database error; chunk will retry after {delay:.1f}s.",
        level="warning", details={"chunk_id": chunk_id, "attempt": attempts, "error": str(error)},
    )
    clock.sleep(delay)
    return True


def _execute_guarded(
    job_id: str, query: str, params: tuple[Any, ...], *, fetch: str = "none", name: str
) -> Any:
    validate_sql_bindings(query, params, name=name)
    settings = get_settings()
    statement_timeout = max(30, int(settings.discovery_statement_timeout_seconds))
    wall_timeout = max(statement_timeout + 5, int(settings.discovery_wall_timeout_seconds))
    cancel_grace = max(3, int(settings.discovery_cancel_grace_seconds))
    with connection() as conn:
        with conn.cursor() as pid_cur:
            pid_cur.execute("SELECT pg_backend_pid() AS pid")
            backend_pid = int(pid_cur.fetchone()["pid"])
        stop = threading.Event()
        interrupted: dict[str, str | None] = {"action": None}
        started = clock.monotonic()

        def interrupt(action: str) -> None:
            if interrupted["action"] is not None:
                return
            interrupted["action"] = action
            try:
                conn.cancel()
            except Exception:
                pass
            if not stop.wait(cancel_grace):
                try:
                    with connection() as killer:
                        with killer.cursor() as cur:
                            cur.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
                        killer.commit()
                except Exception:
                    pass

        def monitor() -> None:
            while not stop.wait(2.0):
                try:
                    with connection() as control:
                        with control.cursor() as cur:
                            cur.execute("UPDATE ra_jobs SET heartbeat_at=now() WHERE id=%s RETURNING status", (job_id,))
                            row = cur.fetchone()
                        control.commit()
                    status = row["status"] if row else "cancel_requested"
                    if status in {"pause_requested", "cancel_requested"}:
                        interrupt("pause" if status == "pause_requested" else "cancel")
                        return
                except Exception:
                    pass
                if clock.monotonic() - started >= wall_timeout:
                    interrupt("timeout")
                    return

        thread = threading.Thread(target=monitor, name=f"discovery-v2-{name}", daemon=True)
        thread.start()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{statement_timeout}s'")
                cur.execute("SET LOCAL lock_timeout = '30s'")
                cur.execute("SET LOCAL jit = off")
                cur.execute(query, params)
                if fetch == "one":
                    result = cur.fetchone()
                elif fetch == "all":
                    result = cur.fetchall()
                elif fetch == "rowcount":
                    result = cur.rowcount or 0
                else:
                    result = None
            conn.commit()
            return result
        except Exception as exc:
            conn.rollback()
            if interrupted["action"] in {"pause", "cancel"}:
                raise JobInterrupted(interrupted["action"]) from exc
            if interrupted["action"] == "timeout":
                raise DiscoveryQueryTimeout(f"{name} exceeded the {wall_timeout}-second wall-clock limit") from exc
            raise
        finally:
            stop.set()
            thread.join(timeout=3)


def _merge_json_counts(target: dict[str, int], value: Any) -> None:
    if not value:
        return
    for key, count in dict(value).items():
        target[str(key)] = target.get(str(key), 0) + int(count or 0)


def _hist_quantile(histogram: dict[str, int], q: float, observations: int) -> float | None:
    if observations <= 0 or not histogram:
        return None
    threshold = max(1, math.ceil(q * observations))
    cumulative = 0
    for bin_id in sorted((int(key) for key in histogram)):
        cumulative += int(histogram.get(str(bin_id), histogram.get(bin_id, 0)) or 0)
        if cumulative >= threshold:
            if bin_id <= HISTOGRAM_MIN_BIN:
                return HISTOGRAM_MIN_BIN / 10.0
            if bin_id >= HISTOGRAM_MAX_BIN:
                return HISTOGRAM_MAX_BIN / 10.0
            return (bin_id + 0.5) / 10.0
    return None


def _merge_partial_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["group_key"])
        merged = groups.setdefault(key, {
            "group_key": key, "group_values": dict(row["group_values"] or {}),
            "observations": 0, "gross_sum": 0.0, "net_sum": 0.0,
            "net_sum_squares": 0.0, "wins": 0, "positive_sum": 0.0,
            "negative_sum_abs": 0.0, "worst_pct": None, "best_pct": None,
            "histogram": {}, "symbol_counts": {}, "date_counts": {},
        })
        merged["observations"] += int(row["observations"] or 0)
        for field in ("gross_sum", "net_sum", "net_sum_squares", "positive_sum", "negative_sum_abs"):
            merged[field] += float(row[field] or 0)
        merged["wins"] += int(row["wins"] or 0)
        worst = finite_or_none(row.get("worst_pct"))
        if worst is not None:
            merged["worst_pct"] = worst if merged["worst_pct"] is None else min(merged["worst_pct"], worst)
        best = finite_or_none(row.get("best_pct"))
        if best is not None:
            merged["best_pct"] = best if merged["best_pct"] is None else max(merged["best_pct"], best)
        _merge_json_counts(merged["histogram"], row.get("histogram"))
        _merge_json_counts(merged["symbol_counts"], row.get("symbol_counts"))
        _merge_json_counts(merged["date_counts"], row.get("date_counts"))
    return groups


def _finalise_stats(merged: dict[str, Any]) -> dict[str, Any]:
    n = int(merged.get("observations") or 0)
    if n <= 0:
        return {}
    net_sum = float(merged["net_sum"])
    net_mean = net_sum / n
    gross_mean = float(merged["gross_sum"]) / n
    variance = None
    if n > 1:
        variance = max(0.0, (float(merged["net_sum_squares"]) - net_sum * net_sum / n) / (n - 1))
    stdev = math.sqrt(variance) if variance is not None else None
    t_stat = net_mean / stdev * math.sqrt(n) if stdev and stdev > 0 else None
    symbols = dict(merged.get("symbol_counts") or {})
    dates = dict(merged.get("date_counts") or {})
    return {
        "observations": n,
        "symbols": len(symbols),
        "dates": len(dates),
        "gross_avg_pct": finite_or_none(gross_mean),
        "net_avg_pct": finite_or_none(net_mean),
        "median_pct": finite_or_none(_hist_quantile(merged["histogram"], 0.5, n)),
        "p25_pct": finite_or_none(_hist_quantile(merged["histogram"], 0.25, n)),
        "p75_pct": finite_or_none(_hist_quantile(merged["histogram"], 0.75, n)),
        "p95_pct": finite_or_none(_hist_quantile(merged["histogram"], 0.95, n)),
        "win_rate_pct": finite_or_none(100.0 * int(merged["wins"]) / n),
        "t_stat": finite_or_none(t_stat),
        "profit_factor": finite_or_none(float(merged["positive_sum"]) / float(merged["negative_sum_abs"])) if float(merged["negative_sum_abs"]) > 0 else None,
        "p05_pct": finite_or_none(_hist_quantile(merged["histogram"], 0.05, n)),
        "worst_pct": finite_or_none(merged.get("worst_pct")),
        "best_pct": finite_or_none(merged.get("best_pct")),
        "max_symbol_share_pct": finite_or_none(100.0 * max(symbols.values()) / n) if symbols else None,
        "max_date_share_pct": finite_or_none(100.0 * max(dates.values()) / n) if dates else None,
        "statistics_method": STATISTICS_METHOD,
    }


def _rank_score(discovery: dict[str, Any], validation: dict[str, Any] | None) -> float:
    d_net = float(discovery.get("net_avg_pct") or 0)
    d_t = max(float(discovery.get("t_stat") or 0), 0)
    concentration = max(0.05, 1 - float(discovery.get("max_symbol_share_pct") or 100) / 100)
    concentration *= max(0.05, 1 - float(discovery.get("max_date_share_pct") or 100) / 100)
    score = d_net * math.log1p(float(discovery.get("observations") or 0)) * max(0.25, d_t) * concentration
    if validation:
        v_net = float(validation.get("net_avg_pct") or 0)
        if v_net <= 0:
            score *= 0.1
        else:
            stability = 1 - min(abs(d_net - v_net) / max(abs(d_net), abs(v_net), 0.01), 1)
            score *= (0.5 + 0.5 * stability) * min(1.5, max(0.25, float(validation.get("t_stat") or 0)))
    return finite_or_none(score) or 0.0


def _conditions(dimensions: list[Dimension], values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    conditions: list[dict[str, Any]] = []
    descriptions: list[str] = []
    for dimension in dimensions:
        label = values.get(dimension.name)
        if label is None or label not in dimension.labels:
            return [], []
        condition, description = dimension.labels[label]
        conditions.append(dict(condition))
        descriptions.append(description)
    return conditions, descriptions


def _plain_rule(direction: str, descriptions: list[str], horizon: int) -> str:
    if not descriptions:
        raise ValueError("A rule requires at least one condition")
    verb = "Buy" if direction == "long" else "Short"
    joined = ", ".join(descriptions[:-1]) + (f" and {descriptions[-1]}" if len(descriptions) > 1 else descriptions[0])
    return f"{verb} when {joined}; exit after {horizon} minutes."


def _ensure_discovery_run(job_id: str, config: DiscoveryConfig) -> tuple[str, bool]:
    stored_config = {**config.model_dump(mode="json"), "engine_version": DISCOVERY_VERSION,
                     "rule_definition_version": RULE_DEFINITION_VERSION,
                     "campaign_definition_version": CAMPAIGN_DEFINITION_VERSION,
                     "statistics_method": STATISTICS_METHOD}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,config FROM ra_discovery_runs WHERE job_id=%s", (job_id,))
            existing = cur.fetchone()
            reset = False
            if existing:
                run_id = str(existing["id"])
                prior = dict(existing.get("config") or {})
                reset = prior.get("engine_version") != DISCOVERY_VERSION
                cur.execute("UPDATE ra_discovery_runs SET status='running',completed_at=NULL,config=%s,campaign_name=%s,campaign_definition_version=%s WHERE id=%s", (Jsonb(stored_config), config.campaign_name, CAMPAIGN_DEFINITION_VERSION, run_id))
                if reset:
                    cur.execute("DELETE FROM ra_candidate_rules WHERE discovery_run_id=%s", (run_id,))
                    cur.execute("DELETE FROM ra_discovery_samples WHERE discovery_run_id=%s", (run_id,))
                    cur.execute("DELETE FROM ra_discovery_sample_chunks WHERE discovery_run_id=%s", (run_id,))
                    cur.execute("DELETE FROM ra_discovery_tasks WHERE discovery_run_id=%s", (run_id,))
                else:
                    cur.execute("UPDATE ra_discovery_sample_chunks SET status='pending',error=NULL WHERE discovery_run_id=%s AND status IN ('running','failed','cancelled')", (run_id,))
                    cur.execute("UPDATE ra_discovery_task_chunks SET status='pending',error=NULL WHERE discovery_task_id IN (SELECT id FROM ra_discovery_tasks WHERE discovery_run_id=%s) AND status IN ('running','failed','cancelled')", (run_id,))
            else:
                cur.execute("INSERT INTO ra_discovery_runs(job_id,feature_set_id,name,config,campaign_name,campaign_definition_version) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (job_id, config.feature_set_id, config.name, Jsonb(stored_config), config.campaign_name, CAMPAIGN_DEFINITION_VERSION))
                run_id = str(cur.fetchone()["id"])
                reset = True
        conn.commit()
    return run_id, reset


def _prepare_tasks_and_chunks(run_id: str, config: DiscoveryConfig, base_minutes: int) -> None:
    periods = [("discovery", config.discovery_start, config.discovery_end)]
    if config.validation_start and config.validation_end:
        periods.append(("validation", config.validation_start, config.validation_end))
    ranges = _bucket_ranges(config.symbol_shards)
    with connection() as conn:
        with conn.cursor() as cur:
            for family in config.families:
                for direction in config.directions:
                    for horizon in config.holding_horizons_minutes:
                        cur.execute("""
                            INSERT INTO ra_discovery_tasks(discovery_run_id,family,direction,holding_horizon_minutes,engine_version,stage)
                            VALUES (%s,%s,%s,%s,%s,'partial_scan')
                            ON CONFLICT(discovery_run_id,family,direction,holding_horizon_minutes)
                            DO UPDATE SET engine_version=excluded.engine_version
                            RETURNING id
                        """, (run_id, family, direction, horizon, DISCOVERY_VERSION))
                        task_id = int(cur.fetchone()["id"])
                        for period_label, start, end in periods:
                            for chunk_start, chunk_end in _date_chunks(start, end, config.date_chunk_days):
                                for bucket_start, bucket_end in ranges:
                                    cur.execute("""
                                        INSERT INTO ra_discovery_task_chunks(
                                            discovery_task_id,period_label,chunk_start,chunk_end,bucket_start,bucket_end
                                        ) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                                    """, (task_id, period_label, chunk_start, chunk_end, bucket_start, bucket_end))
            sample_stride = base_minutes if config.entry_sampling_mode == "all_bars" else min(config.holding_horizons_minutes)
            for period_label, start, end in periods:
                for chunk_start, chunk_end in _date_chunks(start, end, config.date_chunk_days):
                    for bucket_start, bucket_end in ranges:
                        cur.execute("""
                            INSERT INTO ra_discovery_sample_chunks(
                                discovery_run_id,period_label,sample_stride_minutes,
                                chunk_start,chunk_end,bucket_start,bucket_end
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                        """, (run_id, period_label, sample_stride, chunk_start, chunk_end, bucket_start, bucket_end))
        conn.commit()


def _progress(job_id: str, run_id: str, phase: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT count(*) FROM ra_discovery_sample_chunks WHERE discovery_run_id=%s AND status='completed') +
                    (SELECT count(*) FROM ra_discovery_task_chunks c JOIN ra_discovery_tasks t ON t.id=c.discovery_task_id WHERE t.discovery_run_id=%s AND c.status='completed') AS done,
                    (SELECT count(*) FROM ra_discovery_sample_chunks WHERE discovery_run_id=%s AND status<>'split') +
                    (SELECT count(*) FROM ra_discovery_task_chunks c JOIN ra_discovery_tasks t ON t.id=c.discovery_task_id WHERE t.discovery_run_id=%s AND c.status<>'split') AS total
            """, (run_id, run_id, run_id, run_id))
            row = cur.fetchone()
        conn.rollback()
    set_progress(job_id, phase, int(row["done"] or 0), int(row["total"] or 0), result={"discovery_run_id": run_id})


def _split_sample_chunk(chunk: dict[str, Any]) -> None:
    start, end = chunk["chunk_start"], chunk["chunk_end"]
    b0, b1 = int(chunk["bucket_start"]), int(chunk["bucket_end"])
    if start < end:
        middle = start + timedelta(days=(end-start).days//2)
        children = [(start,middle,b0,b1),(middle+timedelta(days=1),end,b0,b1)]
    elif b1-b0 > 1:
        middle=(b0+b1)//2
        children=[(start,end,b0,middle),(start,end,middle,b1)]
    else:
        message = "A one-day, one-bucket sample chunk still exceeded the query timeout"
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_discovery_sample_chunks SET status='failed',error=%s WHERE id=%s", (message, chunk["id"]))
            conn.commit()
        raise RuntimeError(message)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_discovery_sample_chunks SET status='split',error='Automatically split after timeout' WHERE id=%s", (chunk["id"],))
            for cstart,cend,cb0,cb1 in children:
                cur.execute("""INSERT INTO ra_discovery_sample_chunks(discovery_run_id,period_label,sample_stride_minutes,chunk_start,chunk_end,bucket_start,bucket_end)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (chunk["discovery_run_id"],chunk["period_label"],chunk["sample_stride_minutes"],cstart,cend,cb0,cb1))
        conn.commit()


def _split_task_chunk(chunk: dict[str, Any]) -> None:
    start, end = chunk["chunk_start"], chunk["chunk_end"]
    b0, b1 = int(chunk["bucket_start"]), int(chunk["bucket_end"])
    if start < end:
        middle = start + timedelta(days=(end-start).days//2)
        children = [(start,middle,b0,b1),(middle+timedelta(days=1),end,b0,b1)]
    elif b1-b0 > 1:
        middle=(b0+b1)//2
        children=[(start,end,b0,middle),(start,end,middle,b1)]
    else:
        message = "A one-day, one-bucket discovery chunk still exceeded the query timeout"
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_discovery_task_chunks SET status='failed',error=%s WHERE id=%s", (message, chunk["id"]))
            conn.commit()
        raise RuntimeError(message)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_discovery_task_chunks SET status='split',error='Automatically split after timeout' WHERE id=%s", (chunk["id"],))
            for cstart,cend,cb0,cb1 in children:
                cur.execute("""INSERT INTO ra_discovery_task_chunks(discovery_task_id,period_label,chunk_start,chunk_end,bucket_start,bucket_end)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (chunk["discovery_task_id"],chunk["period_label"],cstart,cend,cb0,cb1))
        conn.commit()


def _build_samples(job_id: str, run_id: str, config: DiscoveryConfig, feature_set_id: str, base_minutes: int) -> None:
    while True:
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT * FROM ra_discovery_sample_chunks WHERE discovery_run_id=%s AND status IN ('pending','failed') ORDER BY period_label,chunk_start,bucket_start LIMIT 1""", (run_id,))
                row=cur.fetchone()
                if row:
                    cur.execute("UPDATE ra_discovery_sample_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s", (row["id"],))
            conn.commit()
        if not row:
            return
        chunk=dict(row)
        stride = int(chunk["sample_stride_minutes"])
        query=_sample_insert_query(config.holding_horizons_minutes,stride,570)
        try:
            # A retry may have committed only before its status update. Delete the exact slice first.
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""DELETE FROM ra_discovery_samples WHERE discovery_run_id=%s AND period_label=%s AND trade_date BETWEEN %s AND %s AND symbol_bucket >= %s AND symbol_bucket < %s""",
                        (run_id,chunk["period_label"],chunk["chunk_start"],chunk["chunk_end"],chunk["bucket_start"],chunk["bucket_end"]))
                conn.commit()
            params=(feature_set_id,chunk["chunk_start"],chunk["chunk_end"],chunk["chunk_start"],chunk["chunk_end"],chunk["bucket_start"],chunk["bucket_end"],run_id,chunk["period_label"])
            rows=_execute_guarded(job_id,query,params,fetch="rowcount",name=f"sample-{chunk['id']}")
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_discovery_sample_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s", (rows,chunk["id"]))
                conn.commit()
            _progress(job_id,run_id,f"sampling {chunk['period_label']} · every {stride}m · {chunk['chunk_start']} to {chunk['chunk_end']}")
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE ra_discovery_sample_chunks SET status='pending' WHERE id=%s",(chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            if _is_timeout(exc):
                _split_sample_chunk(chunk)
                add_event(job_id,"sample_chunk_split",str(exc),level="warning",details={"chunk_id":chunk["id"]})
            elif _retry_chunk(
                "ra_discovery_sample_chunks", int(chunk["id"]), exc, job_id,
                "sample_chunk_retry", int(chunk.get("attempts") or 0) + 1,
            ):
                continue
            else:
                with connection() as conn:
                    with conn.cursor() as cur: cur.execute("UPDATE ra_discovery_sample_chunks SET status='failed',error=%s WHERE id=%s",(str(exc),chunk["id"]))
                    conn.commit()
                raise


def _analyze_discovery_samples(job_id: str, run_id: str) -> None:
    """Refresh planner statistics after bulk sample materialisation.

    Without an immediate ANALYZE, PostgreSQL can treat a newly populated table
    as tiny and repeatedly choose whole-table scans for bounded chunk queries.
    Failure to analyse is non-fatal because every scan remains bounded and can
    split, but it is surfaced as a warning.
    """
    try:
        _execute_guarded(
            job_id,
            "ANALYZE ra_discovery_samples",
            (),
            fetch="none",
            name=f"analyze-samples-{run_id}",
        )
        add_event(job_id, "discovery_samples_analyzed", "PostgreSQL planner statistics refreshed for staged samples.")
    except JobInterrupted:
        raise
    except Exception as exc:
        add_event(
            job_id, "discovery_samples_analyze_warning",
            "Planner statistics refresh did not complete; bounded scans will continue.",
            level="warning", details={"error": str(exc)},
        )


def _scan_partials(job_id: str, run_id: str, config: DiscoveryConfig) -> None:
    cost_pct=config.round_trip_cost_bps/100.0
    while True:
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.*,t.family,t.direction,t.holding_horizon_minutes
                    FROM ra_discovery_task_chunks c JOIN ra_discovery_tasks t ON t.id=c.discovery_task_id
                    WHERE t.discovery_run_id=%s AND c.status IN ('pending','failed')
                    ORDER BY t.id,c.period_label,c.chunk_start,c.bucket_start LIMIT 1
                """,(run_id,))
                row=cur.fetchone()
                if row:
                    cur.execute("UPDATE ra_discovery_task_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",(row["id"],))
                    cur.execute("UPDATE ra_discovery_tasks SET status='running',stage='partial_scan',started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",(row["discovery_task_id"],))
            conn.commit()
        if not row:
            return
        chunk=dict(row); spec=FAMILIES[chunk["family"]]
        horizon = int(chunk["holding_horizon_minutes"])
        # The sample table is built at the minimum required cadence. Each task
        # applies its own horizon cadence; all-bars mode deliberately keeps every
        # materialised entry.
        stride = 1 if config.entry_sampling_mode == "all_bars" else horizon
        query=_partial_insert_query(spec["dimensions"],spec["filter"],chunk["direction"],horizon,stride,570)
        params=(cost_pct,run_id,chunk["period_label"],chunk["chunk_start"],chunk["chunk_end"],chunk["bucket_start"],chunk["bucket_end"],chunk["id"])
        try:
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("DELETE FROM ra_discovery_partials WHERE discovery_task_chunk_id=%s",(chunk["id"],))
                conn.commit()
            groups=_execute_guarded(job_id,query,params,fetch="rowcount",name=f"scan-{chunk['id']}")
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(sum(observations),0)::bigint AS n FROM ra_discovery_partials WHERE discovery_task_chunk_id=%s",(chunk["id"],))
                    obs=int(cur.fetchone()["n"] or 0)
                    cur.execute("UPDATE ra_discovery_task_chunks SET status='completed',groups_written=%s,observations_scanned=%s,completed_at=now(),error=NULL WHERE id=%s",(groups,obs,chunk["id"]))
                conn.commit()
            _progress(job_id,run_id,f"scanning {chunk['family']} · {chunk['direction']} · {chunk['holding_horizon_minutes']}m · {chunk['period_label']}")
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE ra_discovery_task_chunks SET status='pending' WHERE id=%s",(chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            if _is_timeout(exc):
                _split_task_chunk(chunk)
                add_event(job_id,"discovery_chunk_split",str(exc),level="warning",details={"chunk_id":chunk["id"]})
            elif _retry_chunk(
                "ra_discovery_task_chunks", int(chunk["id"]), exc, job_id,
                "discovery_chunk_retry", int(chunk.get("attempts") or 0) + 1,
            ):
                continue
            else:
                with connection() as conn:
                    with conn.cursor() as cur: cur.execute("UPDATE ra_discovery_task_chunks SET status='failed',error=%s WHERE id=%s",(str(exc),chunk["id"]))
                    conn.commit()
                raise


def _load_task_period(task_id: int, period_label: str) -> dict[str, dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.* FROM ra_discovery_partials p
                JOIN ra_discovery_task_chunks c ON c.id=p.discovery_task_chunk_id
                WHERE c.discovery_task_id=%s AND c.period_label=%s AND c.status='completed'
                ORDER BY p.group_key
            """,(task_id,period_label))
            rows=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    return _merge_partial_rows(rows)


def _planned_variant_count(config: DiscoveryConfig) -> int:
    """Count every defined parameter variant, including zero-observation combinations.

    This is intentionally different from groups_tested, which counts only grouped
    combinations that actually appeared in the Discovery sample and were therefore
    statistically examined. We persist both values: groups_tested is the actual test
    count used by the current Bonferroni adjustment, while this full defined grid is
    retained as a conservative campaign-complexity measure including zero-observation
    combinations.
    """
    total = 0
    for family in config.families:
        spec = FAMILIES[family]
        family_variants = 1
        for dimension in spec["dimensions"]:
            family_variants *= len(dimension.labels)
        total += family_variants * len(config.directions) * len(config.holding_horizons_minutes)
    return total


def _write_candidates(job_id: str, run_id: str, config: DiscoveryConfig, base_minutes: int) -> tuple[int,int]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_discovery_tasks WHERE discovery_run_id=%s ORDER BY id",(run_id,))
            tasks=[dict(r) for r in cur.fetchall()]
            cur.execute("DELETE FROM ra_candidate_rules WHERE discovery_run_id=%s",(run_id,))
        conn.commit()
    family_candidates: dict[str,list[dict[str,Any]]]={family:[] for family in config.families}
    groups_tested=0
    planned_variants=_planned_variant_count(config)
    for task in tasks:
        discovery_groups=_load_task_period(int(task["id"]),"discovery")
        validation_groups=_load_task_period(int(task["id"]),"validation") if config.validation_start else {}
        groups_tested += len(discovery_groups)
        spec=FAMILIES[task["family"]]
        retained_for_task=0
        for key,merged in discovery_groups.items():
            ds=_finalise_stats(merged)
            if not ds or ds["observations"]<config.minimum_observations or ds["symbols"]<config.minimum_symbols or ds["dates"]<config.minimum_dates or ds["max_symbol_share_pct"]>config.maximum_symbol_concentration_pct or ds["max_date_share_pct"]>config.maximum_date_concentration_pct or (ds["net_avg_pct"] or 0)<=0:
                continue
            if config.validation_start:
                vs = _finalise_stats(validation_groups[key]) if key in validation_groups else {
                    "observations": 0, "symbols": 0, "dates": 0,
                    "gross_avg_pct": None, "net_avg_pct": 0.0, "median_pct": None,
                    "win_rate_pct": None, "t_stat": None, "profit_factor": None,
                    "p05_pct": None, "worst_pct": None,
                    "max_symbol_share_pct": None, "max_date_share_pct": None,
                    "statistics_method": STATISTICS_METHOD,
                }
            else:
                vs = {}
            conditions,descriptions=_conditions(spec["dimensions"],merged["group_values"])
            if not conditions: continue
            # Persist the family-level eligibility filter as structured conditions
            # as well as the grouped bins. This guarantees sealed evaluation uses
            # the exact same intersection, including thresholds that cut through a
            # wider display bin (for example range position >= 0.5).
            conditions.extend(dict(item) for item in spec.get("constraints", []))
            descriptions.extend(str(item) for item in spec.get("constraint_descriptions", []))
            item={"task":task,"ds":ds,"vs":vs,"conditions":conditions,"plain":_plain_rule(task["direction"],descriptions,int(task["holding_horizon_minutes"]))}
            item["score"]=_rank_score(ds,vs or None)
            family_candidates[task["family"]].append(item);retained_for_task+=1
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_discovery_tasks SET status='completed',stage='merged',groups_tested=%s,candidates_retained=%s,completed_at=now(),error=NULL WHERE id=%s",(len(discovery_groups),retained_for_task,task["id"]))
            conn.commit()
    selected=[]
    for family,items in family_candidates.items():
        items.sort(key=lambda x:x["score"],reverse=True)
        selected.extend(items[:config.top_candidates_per_family])
    selected_per_task: dict[int, int] = {}
    for item in selected:
        task_id = int(item["task"]["id"])
        selected_per_task[task_id] = selected_per_task.get(task_id, 0) + 1
    with connection() as conn:
        with conn.cursor() as cur:
            for task in tasks:
                cur.execute("UPDATE ra_discovery_tasks SET candidates_retained=%s WHERE id=%s", (selected_per_task.get(int(task["id"]), 0), task["id"]))
        conn.commit()
    stride_by_horizon=lambda h: base_minutes if config.entry_sampling_mode=="all_bars" else h
    with connection() as conn:
        with conn.cursor() as cur:
            for item in selected:
                t=item["task"];ds=item["ds"];vs=item["vs"];h=int(t["holding_horizon_minutes"])
                raw_p = math.erfc(abs(float(ds.get("t_stat") or 0)) / math.sqrt(2)) if ds.get("t_stat") is not None else 1.0
                adjusted_p = min(1.0, raw_p * max(groups_tested, 1))
                if config.validation_start and (vs.get("net_avg_pct") or 0) > 0 and (vs.get("profit_factor") or 0) > 1 and adjusted_p <= 0.10:
                    discovery_status = "PROGRESS_TO_FORMAL_VALIDATION"
                elif adjusted_p <= 0.10:
                    discovery_status = "PROMISING"
                else:
                    discovery_status = "WEAK"
                cur.execute("""
                    INSERT INTO ra_candidate_rules(
                        discovery_run_id,feature_set_id,family,direction,holding_horizon_minutes,
                        entry_sampling_mode,entry_stride_minutes,entry_anchor_minute,rule_definition_version,
                        statistics_method,engine_version,conditions,plain_english_rule,rank_score,
                        discovery_observations,discovery_symbols,discovery_dates,discovery_gross_avg_pct,
                        discovery_net_avg_pct,discovery_median_pct,discovery_win_rate_pct,discovery_t_stat,
                        discovery_profit_factor,discovery_p05_pct,discovery_p25_pct,discovery_p75_pct,discovery_p95_pct,
                        discovery_worst_pct,discovery_best_pct,discovery_max_symbol_share_pct,discovery_max_date_share_pct,
                        validation_observations,validation_symbols,validation_dates,validation_gross_avg_pct,
                        validation_net_avg_pct,validation_median_pct,validation_win_rate_pct,validation_t_stat,
                        validation_profit_factor,validation_p05_pct,validation_p25_pct,validation_p75_pct,validation_p95_pct,
                        validation_worst_pct,validation_best_pct,validation_max_symbol_share_pct,validation_max_date_share_pct,
                        hypothesis_ids,hypothesis_version,variants_tested_campaign,variants_defined_campaign,multiple_testing_method,
                        multiple_testing_adjusted_p,discovery_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,570,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s)
                """,(
                    run_id,config.feature_set_id,t["family"],t["direction"],h,
                    config.entry_sampling_mode,stride_by_horizon(h),RULE_DEFINITION_VERSION,
                    STATISTICS_METHOD,DISCOVERY_VERSION,Jsonb(item["conditions"]),item["plain"],item["score"],
                    ds.get("observations"),ds.get("symbols"),ds.get("dates"),ds.get("gross_avg_pct"),ds.get("net_avg_pct"),ds.get("median_pct"),ds.get("win_rate_pct"),ds.get("t_stat"),ds.get("profit_factor"),ds.get("p05_pct"),ds.get("p25_pct"),ds.get("p75_pct"),ds.get("p95_pct"),ds.get("worst_pct"),ds.get("best_pct"),ds.get("max_symbol_share_pct"),ds.get("max_date_share_pct"),
                    vs.get("observations"),vs.get("symbols"),vs.get("dates"),vs.get("gross_avg_pct"),vs.get("net_avg_pct"),vs.get("median_pct"),vs.get("win_rate_pct"),vs.get("t_stat"),vs.get("profit_factor"),vs.get("p05_pct"),vs.get("p25_pct"),vs.get("p75_pct"),vs.get("p95_pct"),vs.get("worst_pct"),vs.get("best_pct"),vs.get("max_symbol_share_pct"),vs.get("max_date_share_pct"),
                    Jsonb(FAMILIES[t["family"]].get("hypothesis_ids",[])),FAMILIES[t["family"]].get("hypothesis_version","legacy"),
                    groups_tested,planned_variants,"bonferroni_normal_approx",adjusted_p,discovery_status,
                ))
            hypothesis_ids = sorted({hid for family in config.families for hid in FAMILIES[family].get("hypothesis_ids", [])})
            cur.execute("UPDATE ra_discovery_runs SET status='completed',candidates_tested=%s,candidates_retained=%s,variant_count=%s,defined_variant_count=%s,hypothesis_ids=%s,completed_at=now() WHERE id=%s",(groups_tested,len(selected),groups_tested,planned_variants,Jsonb(hypothesis_ids),run_id))
        conn.commit()
    add_event(job_id,"discovery_merge_completed",f"Merged {groups_tested:,} observed groups from {planned_variants:,} defined variants and retained {len(selected):,} candidates.",details={"statistics_method":STATISTICS_METHOD,"defined_variants":planned_variants,"observed_groups":groups_tested})
    return groups_tested,len(selected)


def run_discovery(job_id: str, config: DiscoveryConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,min_trade_date,max_trade_date,config FROM ra_feature_sets WHERE id=%s",(config.feature_set_id,))
            feature=cur.fetchone()
        conn.rollback()
    if not feature or feature["status"]!="completed": raise RuntimeError("The selected feature set does not exist or is not completed")
    periods=[(config.discovery_start,config.discovery_end)] + ([(config.validation_start,config.validation_end)] if config.validation_start else [])
    if any(s<feature["min_trade_date"] or e>feature["max_trade_date"] for s,e in periods): raise ValueError(f"Dates must remain within the feature set: {feature['min_trade_date']} to {feature['max_trade_date']}")
    fcfg=dict(feature.get("config") or {});base_minutes=timeframe_minutes(str(fcfg.get("timeframe") or "1Min"))
    if str(fcfg.get("session") or "regular")!="regular": raise ValueError("Discovery v2 currently requires a regular-session feature set")
    available={int(x) for x in fcfg.get("outcome_horizons_minutes",[5,15,30,60])}
    missing=set(config.holding_horizons_minutes)-available
    if missing: raise ValueError(f"Feature set is missing outcome horizons: {sorted(missing)}")
    run_id,reset=_ensure_discovery_run(job_id,config)
    _prepare_tasks_and_chunks(run_id,config,base_minutes)
    if reset: add_event(job_id,"discovery_engine_v2_reset","The withdrawn monolithic scan was reset. Existing feature rows were preserved.",level="warning",details={"engine_version":DISCOVERY_VERSION})
    _progress(job_id,run_id,"preparing staged discovery")
    _build_samples(job_id,run_id,config,str(config.feature_set_id),base_minutes)
    _analyze_discovery_samples(job_id,run_id)
    _scan_partials(job_id,run_id,config)
    check_control(job_id)
    _progress(job_id,run_id,"merging partial statistics")
    tested,retained=_write_candidates(job_id,run_id,config,base_minutes)
    result={"discovery_run_id":run_id,"candidates_tested":tested,"candidates_retained":retained,"engine_version":DISCOVERY_VERSION,"statistics_method":STATISTICS_METHOD}
    set_progress(job_id,"complete",1,1,result=result)
    return json_safe(result)


def _sealed_chunks(job_id: str, candidate: dict[str, Any], start: date, end: date, shards: int, days: int) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            for cstart,cend in _date_chunks(start,end,days):
                for b0,b1 in _bucket_ranges(shards):
                    cur.execute("""INSERT INTO ra_sealed_chunks(job_id,candidate_id,chunk_start,chunk_end,bucket_start,bucket_end)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",(job_id,candidate["id"],cstart,cend,b0,b1))
            cur.execute("UPDATE ra_sealed_chunks SET status='pending',error=NULL WHERE job_id=%s AND status IN ('running','failed','cancelled')",(job_id,))
        conn.commit()


def run_sealed_evaluation(job_id: str, config: SealedEvaluationConfig) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.*,d.config discovery_config,
                    sf.config source_feature_config,sf.universe_run_id source_universe_run_id,
                    sf.min_trade_date source_min_date,sf.max_trade_date source_max_date
                FROM ra_candidate_rules c
                JOIN ra_discovery_runs d ON d.id=c.discovery_run_id
                JOIN ra_feature_sets sf ON sf.id=c.feature_set_id
                WHERE c.id=%s""",(config.candidate_id,))
            row=cur.fetchone()
        conn.rollback()
    if not row: raise RuntimeError("Candidate does not exist")
    candidate=dict(row);dcfg=dict(candidate["discovery_config"] or {})
    boundary=dcfg.get("validation_end") or dcfg["discovery_end"]
    if config.sealed_start.isoformat()<=str(boundary): raise ValueError(f"Sealed period must begin after {boundary}")
    target_feature_set_id = config.target_feature_set_id or candidate["feature_set_id"]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,config,universe_run_id,min_trade_date,max_trade_date FROM ra_feature_sets WHERE id=%s", (target_feature_set_id,))
            target = cur.fetchone()
        conn.rollback()
    if not target or target["status"] != "completed": raise ValueError("Target sealed feature set must be completed")
    source_cfg=dict(candidate.get("source_feature_config") or {}); target_cfg=dict(target.get("config") or {})
    ensure_feature_set_compatibility(
        candidate.get("source_universe_run_id"), target.get("universe_run_id"),
        source_cfg, target_cfg, int(candidate["holding_horizon_minutes"]),
    )
    if config.sealed_start<target["min_trade_date"] or config.sealed_end>target["max_trade_date"]: raise ValueError("Sealed dates must remain within the target feature set")
    if candidate.get("rule_definition_version") not in {"2026-08-staged-v2", RULE_DEFINITION_VERSION}: raise ValueError("Candidate rule definition is too old for audited sealed replay")
    shards=int(dcfg.get("symbol_shards",4));days=int(dcfg.get("date_chunk_days",3))
    _sealed_chunks(job_id,candidate,config.sealed_start,config.sealed_end,shards,days)
    query,condition_params=_sealed_partial_query(candidate["conditions"],candidate["direction"],int(candidate["holding_horizon_minutes"]),int(candidate["entry_stride_minutes"]),int(candidate["entry_anchor_minute"]))
    cost=float(dcfg["round_trip_cost_bps"])/100.0
    while True:
        check_control(job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ra_sealed_chunks WHERE job_id=%s AND status IN ('pending','failed') ORDER BY chunk_start,bucket_start LIMIT 1",(job_id,));r=cur.fetchone()
                if r: cur.execute("UPDATE ra_sealed_chunks SET status='running',attempts=attempts+1,error=NULL WHERE id=%s",(r["id"],))
            conn.commit()
        if not r: break
        chunk=dict(r);params=(target_feature_set_id,chunk["chunk_start"],chunk["chunk_end"],chunk["chunk_start"],chunk["chunk_end"],chunk["bucket_start"],chunk["bucket_end"],cost,*condition_params)
        try:
            stats=dict(_execute_guarded(job_id,query,params,fetch="one",name=f"sealed-{chunk['id']}") or {})
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE ra_sealed_chunks SET status='completed',observations=%s,gross_sum=%s,net_sum=%s,net_sum_squares=%s,wins=%s,positive_sum=%s,negative_sum_abs=%s,worst_pct=%s,best_pct=%s,histogram=%s,symbol_counts=%s,date_counts=%s,completed_at=now(),error=NULL WHERE id=%s""",
                        (stats.get("observations",0),stats.get("gross_sum",0),stats.get("net_sum",0),stats.get("net_sum_squares",0),stats.get("wins",0),stats.get("positive_sum",0),stats.get("negative_sum_abs",0),stats.get("worst_pct"),stats.get("best_pct"),Jsonb(stats.get("histogram") or {}),Jsonb(stats.get("symbol_counts") or {}),Jsonb(stats.get("date_counts") or {}),chunk["id"]))
                conn.commit()
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE ra_sealed_chunks SET status='pending' WHERE id=%s",(chunk["id"],))
                conn.commit()
            raise
        except Exception as exc:
            if _is_timeout(exc) and (chunk["chunk_start"]<chunk["chunk_end"] or int(chunk["bucket_end"])-int(chunk["bucket_start"])>1):
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE ra_sealed_chunks SET status='split',error=%s WHERE id=%s",(str(exc),chunk["id"]))
                        if chunk["chunk_start"]<chunk["chunk_end"]:
                            mid=chunk["chunk_start"]+timedelta(days=(chunk["chunk_end"]-chunk["chunk_start"]).days//2);children=[(chunk["chunk_start"],mid,chunk["bucket_start"],chunk["bucket_end"]),(mid+timedelta(days=1),chunk["chunk_end"],chunk["bucket_start"],chunk["bucket_end"])]
                        else:
                            mid=(int(chunk["bucket_start"])+int(chunk["bucket_end"]))//2;children=[(chunk["chunk_start"],chunk["chunk_end"],chunk["bucket_start"],mid),(chunk["chunk_start"],chunk["chunk_end"],mid,chunk["bucket_end"])]
                        for a,b,c,d in children: cur.execute("INSERT INTO ra_sealed_chunks(job_id,candidate_id,chunk_start,chunk_end,bucket_start,bucket_end) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",(job_id,candidate["id"],a,b,c,d))
                    conn.commit()
            elif _is_timeout(exc):
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE ra_sealed_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    conn.commit()
                raise RuntimeError("A one-day, one-bucket sealed chunk still exceeded the query timeout") from exc
            elif _retry_chunk(
                "ra_sealed_chunks", int(chunk["id"]), exc, job_id,
                "sealed_chunk_retry", int(chunk.get("attempts") or 0) + 1,
            ):
                continue
            else:
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE ra_sealed_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    conn.commit()
                raise
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FILTER(WHERE status='completed') done,count(*) FILTER(WHERE status<>'split') total FROM ra_sealed_chunks WHERE job_id=%s",(job_id,));p=cur.fetchone()
            conn.rollback()
        set_progress(job_id,"evaluating sealed period",p["done"],p["total"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT observations,gross_sum,net_sum,net_sum_squares,wins,positive_sum,negative_sum_abs,worst_pct,best_pct,histogram,symbol_counts,date_counts FROM ra_sealed_chunks WHERE job_id=%s AND status='completed'",(job_id,));rows=[dict(x) for x in cur.fetchall()]
        conn.rollback()
    merged=_merge_partial_rows([{**x,"group_key":"sealed","group_values":{}} for x in rows]);stats=_finalise_stats(merged["sealed"] if "sealed" in merged else {})
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE ra_candidate_rules SET workflow_status='sealed_tested',sealed_feature_set_id=%s,sealed_start=%s,sealed_end=%s,sealed_observations=%s,sealed_net_avg_pct=%s,sealed_median_pct=%s,sealed_win_rate_pct=%s,sealed_t_stat=%s,sealed_profit_factor=%s,sealed_evaluated_at=now() WHERE id=%s""",
                (target_feature_set_id,config.sealed_start,config.sealed_end,stats.get("observations"),stats.get("net_avg_pct"),stats.get("median_pct"),stats.get("win_rate_pct"),stats.get("t_stat"),stats.get("profit_factor"),config.candidate_id))
        conn.commit()
    result={"candidate_id":config.candidate_id,"target_feature_set_id":target_feature_set_id,"sealed_start":config.sealed_start,"sealed_end":config.sealed_end,**stats}
    add_event(job_id,"sealed_evaluation_completed","Sealed candidate evaluation completed using staged partial aggregates.",details=result)
    set_progress(job_id,"complete",1,1,result=result)
    return json_safe(result)

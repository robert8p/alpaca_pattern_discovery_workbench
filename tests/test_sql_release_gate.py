from datetime import date

import pytest

from app.discovery import (
    FAMILIES,
    GAP,
    PREV_DAY,
    RANGE_POS,
    RET5,
    RET30,
    RVOL,
    VWAP,
    _condition_sql,
    _conditions,
    _exact_stats_query,
    _period_group_query,
)
from app.preflight import local_sql_preflight
from app.sql_validation import SqlBindingError, inspect_psycopg_placeholders, validate_sql_bindings


def test_psycopg_parser_reproduces_literal_percent_failure():
    with pytest.raises(SqlBindingError, match="literal '%' must be written"):
        validate_sql_bindings("SELECT '-5% to -3%' WHERE x=%s", (1,))
    report = validate_sql_bindings("SELECT '-5%% to -3%%' WHERE x=%s", (1,))
    assert report.placeholder_count == 1
    assert report.literal_percent_count == 2


def test_every_discovery_query_combination_has_valid_driver_bindings():
    d = date(2026, 7, 6)
    for family, spec in FAMILIES.items():
        for direction in ("long", "short"):
            for horizon in (5, 15, 30, 60):
                for stride in (1, horizon):
                    query = _period_group_query(
                        spec["dimensions"], spec["filter"], horizon, direction,
                        entry_stride_minutes=stride, entry_anchor_minute=570,
                    )
                    validate_sql_bindings(query, (0.2, "feature", d, d, d, d), name=family)
                    assert "%" not in query.replace("%s", "")


def test_every_exact_query_combination_has_valid_driver_bindings():
    d = date(2026, 7, 6)
    for family, spec in FAMILIES.items():
        row = {dimension.name: next(iter(dimension.labels)) for dimension in spec["dimensions"]}
        conditions, descriptions = _conditions(spec["dimensions"], row)
        assert conditions and descriptions
        for direction in ("long", "short"):
            for horizon in (5, 15, 30, 60):
                for stride in (1, horizon):
                    query, condition_params = _exact_stats_query(
                        conditions, direction, horizon,
                        entry_stride_minutes=stride, entry_anchor_minute=570,
                    )
                    validate_sql_bindings(query, (0.2, "feature", d, d, d, d, *condition_params))
                    if stride == 1:
                        assert "mod(minute_of_day" not in query
                    else:
                        assert f"mod(minute_of_day - 570, {horizon}) = 0" in query
                    assert "bar_ts >=" in query and "bar_ts <" in query


def test_numeric_bins_are_contiguous_and_boundary_consistent():
    for dimension in (RET30, RET5, RVOL, VWAP, RANGE_POS, GAP, PREV_DAY):
        ranges = [condition for condition, _ in dimension.labels.values()]
        assert ranges[0]["low"] is None
        assert ranges[-1]["high"] is None
        for left, right in zip(ranges, ranges[1:]):
            assert left["high"] == right["low"], dimension.name
            assert left["high_inclusive"] is False
            assert right["low_inclusive"] is True
        assert "IS NULL THEN NULL" in dimension.expression


def test_condition_sql_preserves_inclusive_and_exclusive_boundaries():
    sql, params = _condition_sql([
        {
            "column": "ret_30m_pct", "operator": "range", "low": -5, "high": -3,
            "low_inclusive": True, "high_inclusive": False,
        },
        {
            "column": "relative_volume_20bar", "operator": "range", "low": 3, "high": None,
            "low_inclusive": True, "high_inclusive": False,
        },
    ])
    assert sql == "ret_30m_pct>=%s AND ret_30m_pct<%s AND relative_volume_20bar>=%s"
    assert params == [-5, -3, 3]


def test_local_preflight_is_exhaustive_and_stable():
    result = local_sql_preflight()
    assert result["ok"] is True
    # 6 families * 2 directions * 4 horizons * 2 sampling modes *
    # (grouped + exact), plus feature and universe production queries.
    assert result["checks"] == 194
    assert len(result["definition_hash"]) == 64
    int(result["definition_hash"], 16)

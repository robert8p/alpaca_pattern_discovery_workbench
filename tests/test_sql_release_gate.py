from datetime import date

import pytest

from app.discovery import (
    FAMILIES, GAP, PREV_DAY, RANGE_POS, RET5, RET30, RVOL, VWAP, RTCOUNT, RVOL_CHANGE, IMPACT_CHANGE,
    _condition_sql, _conditions, _partial_insert_query, _sample_insert_query,
    _sealed_partial_query,
)
from app.preflight import generated_queries, local_sql_preflight
from app.sql_validation import SqlBindingError, validate_sql_bindings


def test_psycopg_parser_reproduces_literal_percent_failure():
    with pytest.raises(SqlBindingError, match="literal '%' must be written"):
        validate_sql_bindings("SELECT '-5% to -3%' WHERE x=%s", (1,))
    validate_sql_bindings("SELECT '-5%% to -3%%' WHERE x=%s", (1,))


def test_every_generated_v2_query_has_valid_bindings():
    for name, query, params in generated_queries():
        validate_sql_bindings(query, params, name=name)
        assert "%" not in query.replace("%s", "").replace("%%", "")


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


def test_condition_sql_preserves_boundaries():
    sql, params = _condition_sql([
        {"column": "ret_30m_pct", "operator": "range", "low": -5, "high": -3,
         "low_inclusive": True, "high_inclusive": False},
        {"column": "relative_volume_20bar", "operator": "range", "low": 3, "high": None,
         "low_inclusive": True, "high_inclusive": False},
    ])
    assert sql == "ret_30m_pct>=%s AND ret_30m_pct<%s AND relative_volume_20bar>=%s"
    assert params == [-5, -3, 3]


def test_local_preflight_is_exhaustive_and_stable():
    result = local_sql_preflight()
    # 2 sample queries + 192 bounded partial scans + 192 sealed combinations + feature + universe + two robustness query paths.
    assert result["checks"] == 772
    assert result["ok"] is True
    assert result["engine_version"] == "2.2.0"
    assert len(result["definition_hash"]) == 64

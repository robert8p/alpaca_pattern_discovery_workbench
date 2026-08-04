from app.discovery import FAMILIES, TIME, _condition_sql, _period_group_query, _plain_rule


def test_all_families_generate_group_queries():
    for name, spec in FAMILIES.items():
        query = _period_group_query(spec["dimensions"], spec["filter"], 30, "long")
        assert "FROM ra_intraday_features" in query
        assert "fwd_return_30m_pct" in query
        assert "max_symbol_share_pct" in query
        assert name


def test_condition_sql_uses_parameters():
    sql, params = _condition_sql([
        {"column": "minute_of_day", "operator": "range", "low": 870, "high": 930},
        {"column": "weekday_iso", "operator": "eq", "value": 3},
    ])
    assert sql == "minute_of_day>=%s AND minute_of_day<%s AND weekday_iso=%s"
    assert params == [870, 930, 3]


def test_plain_rule_is_readable():
    rule = _plain_rule("long", ["between 14:30 and 15:30 ET", "relative volume is at least 3x"], 30)
    assert rule.startswith("Buy when")
    assert rule.endswith("exit after 30 minutes.")


def test_time_dimension_has_condition_mapping():
    condition, description = TIME.labels["14:30–15:30 ET"]
    assert condition["column"] == "minute_of_day"
    assert "14:30" in description

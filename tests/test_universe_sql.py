from datetime import date
from uuid import UUID

from app.models import UniverseBuildConfig
from app.sql_validation import validate_sql_bindings
from app.universe import _universe_sql


def test_nullable_regex_parameters_are_explicitly_typed_and_bindable():
    config = UniverseBuildConfig(
        name="Liquid universe",
        start_date=date(2026, 6, 4),
        end_date=date(2026, 8, 3),
        timeframe="1Min",
        feed="sip",
        adjustment="raw",
        session="regular",
        include_symbol_regex=None,
        exclude_symbol_regex="[/]",
    )
    query, params = _universe_sql(config, UUID("11111111-1111-1111-1111-111111111111"))

    report = validate_sql_bindings(query, params, name="universe regression SQL")
    assert report.placeholder_count == len(params)
    assert "WHEN %s::text IS NOT NULL AND NOT (s.symbol ~ %s::text)" in query
    assert "WHEN %s::text IS NOT NULL AND s.symbol ~ %s::text" in query
    assert params[17] is None
    assert params[18] is None
    assert params[19] == "[/]"
    assert params[20] == "[/]"
    # Date predicates use precomputed timestamptz bounds, enabling partition pruning.
    assert "b.bar_ts >= %s AND b.bar_ts < %s" in query

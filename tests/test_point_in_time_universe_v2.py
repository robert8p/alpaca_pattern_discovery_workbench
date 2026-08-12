from __future__ import annotations

from datetime import date

from app import point_in_time_universe as legacy
from app import point_in_time_universe_v2 as v2
from app import pti_availability as availability


def test_v2_routes_pti_helpers_without_changing_pti_methodology() -> None:
    assert legacy._spy_trading_dates is v2._spy_trading_dates
    assert availability._reference_trade_date is v2._reference_trade_date
    assert v2.ensure_point_in_time_universes is legacy.ensure_point_in_time_universes
    assert v2.PTI_QUERY_PRUNING_VERSION == "1.0.0"


def test_spy_date_lookup_uses_direct_partition_key_bounds(monkeypatch) -> None:
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchall(self):
            return [{"trade_date": date(2026, 6, 4)}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

    monkeypatch.setattr(v2, "connection", lambda: Connection())
    result = v2._spy_trading_dates(
        {"timeframe": "1Min", "feed": "sip", "adjustment": "raw"},
        date(2026, 6, 4),
        date(2026, 6, 4),
    )

    assert result == [date(2026, 6, 4)]
    sql, params = executed[0]
    assert "b.bar_ts >= %s AND b.bar_ts < %s" in sql
    assert "BETWEEN %s AND %s" not in sql
    assert params[:3] == ("1Min", "sip", "raw")
    assert params[3] < params[4]

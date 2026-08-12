from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app import point_in_time_universe as legacy
from app import pti_availability as availability
from app.db import connection
from app.models import UniverseBuildConfig
from app.utils import market_date_bounds

PTI_QUERY_PRUNING_VERSION = "1.0.0"


def _spy_trading_dates(
    source: dict[str, Any],
    start_date: date,
    end_date: date,
) -> list[date]:
    """Return SPY trading dates with explicit timestamp bounds for partition pruning.

    The legacy predicate derived a New York date from every candidate bar. That is
    logically correct but prevents PostgreSQL from pruning `rd_bars` partitions by
    their timestamp partition key. The explicit market timestamp bounds below are
    equivalent for the requested local-date interval and make the lookup sargable.
    """
    start_ts, end_ts = market_date_bounds(start_date, end_date)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT (b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date
                FROM rd_bars b
                WHERE b.symbol='SPY' AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                  AND b.session_label='regular'
                  AND b.bar_ts >= %s AND b.bar_ts < %s
                ORDER BY trade_date
                """,
                (
                    source.get("timeframe", "1Min"),
                    source.get("feed", "sip"),
                    source.get("adjustment", "raw"),
                    start_ts,
                    end_ts,
                ),
            )
            dates = [row["trade_date"] for row in cur.fetchall()]
        conn.rollback()
    return dates


def _reference_trade_date(snapshot_date: date, config: UniverseBuildConfig):
    """Resolve the last completed SPY session using a bounded PTI lookback.

    A valid PTI snapshot already requires sufficient prior trading history inside
    the 61-calendar-day lookback. Bounding this query to the same interval keeps
    the result unchanged for valid snapshots while allowing partition pruning.
    """
    lower_date = snapshot_date - timedelta(days=legacy.PTI_LOOKBACK_CALENDAR_DAYS)
    start_ts, end_ts = market_date_bounds(lower_date, snapshot_date - timedelta(days=1))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max((b.bar_ts AT TIME ZONE 'America/New_York')::date) AS trade_date
                FROM rd_bars b
                WHERE b.symbol='SPY' AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                  AND b.session_label='regular'
                  AND b.bar_ts >= %s AND b.bar_ts < %s
                """,
                (config.timeframe, config.feed, config.adjustment, start_ts, end_ts),
            )
            row = cur.fetchone()
        conn.rollback()
    return row["trade_date"] if row else None


# Patch only the two date-discovery helpers. All PTI selection, liquidity,
# survivorship, availability, checkpointing and sealed-data controls remain the
# existing production implementation.
legacy._spy_trading_dates = _spy_trading_dates
availability._reference_trade_date = _reference_trade_date

ensure_point_in_time_universes = legacy.ensure_point_in_time_universes
point_in_time_source_readiness = legacy.point_in_time_source_readiness
snapshot_universe_config = legacy.snapshot_universe_config

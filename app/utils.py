from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Not JSON serialisable: {type(value)!r}")


def finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


NY = ZoneInfo("America/New_York")

def market_date_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Return UTC timestamps covering inclusive New York calendar dates."""
    start = datetime.combine(start_date, time.min, tzinfo=NY).astimezone(UTC)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=NY).astimezone(UTC)
    return start, end

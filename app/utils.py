from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


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

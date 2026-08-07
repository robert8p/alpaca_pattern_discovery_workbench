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


def ensure_feature_set_compatibility(
    source_universe_id: Any,
    target_universe_id: Any,
    source_config: dict[str, Any],
    target_config: dict[str, Any],
    required_outcome_horizon: int,
) -> None:
    """Require a holdout/sealed feature set to preserve the frozen research definition.

    Operational chunk sizes and conflict policy may differ, but the stock population
    and predictor construction must not change between development and holdout.
    """
    if str(source_universe_id) != str(target_universe_id):
        raise ValueError("Target feature set must use the same frozen analysis universe")
    for key in ("timeframe", "feed", "adjustment", "session"):
        if source_config.get(key) != target_config.get(key):
            raise ValueError(
                f"Target feature set is incompatible on {key}: "
                f"{target_config.get(key)} vs {source_config.get(key)}"
            )
    source_tiers = sorted(source_config.get("liquidity_tiers") or [])
    target_tiers = sorted(target_config.get("liquidity_tiers") or [])
    if source_tiers != target_tiers:
        raise ValueError(
            f"Target feature set must use the same liquidity tiers: {target_tiers} vs {source_tiers}"
        )
    source_baseline = source_config.get("time_of_day_baseline_days")
    target_baseline = target_config.get("time_of_day_baseline_days")
    if source_baseline != target_baseline:
        raise ValueError(
            "Target feature set must use the same time-of-day baseline definition: "
            f"{target_baseline} vs {source_baseline} days"
        )
    source_predictors = sorted(int(x) for x in source_config.get("predictor_horizons_minutes", []))
    target_predictors = sorted(int(x) for x in target_config.get("predictor_horizons_minutes", []))
    if source_predictors != target_predictors:
        raise ValueError(
            f"Target feature set must use the same predictor horizons: {target_predictors} vs {source_predictors}"
        )
    outcomes = {int(x) for x in target_config.get("outcome_horizons_minutes", [5, 15, 30, 60])}
    if int(required_outcome_horizon) not in outcomes:
        raise ValueError(f"Target feature set does not contain the {required_outcome_horizon}-minute outcome")

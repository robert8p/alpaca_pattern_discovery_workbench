from __future__ import annotations

import threading
import time
from datetime import date
from hashlib import sha256
from typing import Any
from uuid import UUID

from app.db import connection
from app.discovery import (
    DISCOVERY_VERSION,
    FAMILIES,
    RULE_DEFINITION_VERSION,
    STATISTICS_METHOD,
    _conditions,
    _partial_insert_query,
    _sample_insert_query,
    _sealed_partial_query,
)
from app.features import _feature_sql
from app.models import FeatureBuildConfig, UniverseBuildConfig
from app.sql_validation import validate_sql_bindings
from app.universe import _universe_sql

_PREFLIGHT_LOCK = threading.Lock()
_PREFLIGHT_CACHE: dict[str, Any] = {"at": 0.0, "result": None}
_PREFLIGHT_TTL_SECONDS = 600
_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")
_SAMPLE_DATE = date(2026, 7, 6)


def _sample_conditions(family: str) -> list[dict[str, Any]]:
    dimensions = FAMILIES[family]["dimensions"]
    values = {dimension.name: next(iter(dimension.labels)) for dimension in dimensions}
    conditions, _ = _conditions(dimensions, values)
    if not conditions:
        raise RuntimeError(f"Could not generate preflight conditions for {family}")
    conditions.extend(dict(item) for item in FAMILIES[family].get("constraints", []))
    return conditions


def _sample_feature_query() -> tuple[str, tuple[Any, ...]]:
    config = FeatureBuildConfig(
        name="Preflight features", universe_run_id=_NIL_UUID,
        start_date=_SAMPLE_DATE, end_date=_SAMPLE_DATE,
        timeframe="1Min", feed="sip", adjustment="raw", session="regular",
        liquidity_tiers=["A", "B"], date_chunk_days=1, symbol_batch_size=10,
        time_of_day_baseline_days=10,
        predictor_horizons_minutes=[1, 5, 15, 30, 60],
        outcome_horizons_minutes=[5, 15, 30, 60],
    )
    return _feature_sql(config, _SAMPLE_DATE, _SAMPLE_DATE, ["PREFLIGHT"])


def _sample_universe_query() -> tuple[str, tuple[Any, ...]]:
    config = UniverseBuildConfig(
        name="Preflight universe", start_date=_SAMPLE_DATE, end_date=_SAMPLE_DATE,
        timeframe="1Min", feed="sip", adjustment="raw", session="regular",
        maximum_symbols=10, include_symbol_regex=None, exclude_symbol_regex=r"[/]",
    )
    return _universe_sql(config, _NIL_UUID)


def generated_queries() -> list[tuple[str, str, tuple[Any, ...]]]:
    queries: list[tuple[str, str, tuple[Any, ...]]] = []
    for stride in (1, 5):
        query = _sample_insert_query([5, 15, 30, 60], stride, 570)
        params = (_NIL_UUID, "discovery", _NIL_UUID,
                  _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE, 0, 256)
        queries.append((f"sample:{stride}", query, params))

    for family, spec in FAMILIES.items():
        conditions = _sample_conditions(family)
        for direction in ("long", "short"):
            for horizon in (5, 15, 30, 60):
                for stride in (1, horizon):
                    query = _partial_insert_query(
                        spec["dimensions"], spec["filter"], direction, horizon, stride, 570
                    )
                    params = (0.2, _NIL_UUID, "discovery", _SAMPLE_DATE, _SAMPLE_DATE, 0, 256, 1)
                    queries.append((f"partial:{family}:{direction}:{horizon}:{stride}", query, params))
                    sealed_query, condition_params = _sealed_partial_query(
                        conditions, direction, horizon, stride, 570
                    )
                    sealed_params = (0.2, _NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE,
                                     _SAMPLE_DATE, _SAMPLE_DATE, 0, 256, *condition_params)
                    queries.append((f"sealed:{family}:{direction}:{horizon}:{stride}", sealed_query, sealed_params))

    feature_query, feature_params = _sample_feature_query()
    queries.append(("feature-build", feature_query, feature_params))
    universe_query, universe_params = _sample_universe_query()
    queries.append(("universe-build", universe_query, universe_params))
    return queries


def local_sql_preflight() -> dict[str, Any]:
    checks = generated_queries()
    for name, query, params in checks:
        validate_sql_bindings(query, params, name=name)
    digest = sha256("\n".join(query for _, query, _ in checks).encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "engine_version": DISCOVERY_VERSION,
        "rule_definition_version": RULE_DEFINITION_VERSION,
        "statistics_method": STATISTICS_METHOD,
        "checks": len(checks),
        "definition_hash": digest,
    }


def _plan(cur: Any, query: str, params: tuple[Any, ...], *, name: str) -> None:
    validate_sql_bindings(query, params, name=name)
    cur.execute("EXPLAIN (FORMAT JSON, COSTS FALSE) " + query, params)
    cur.fetchone()


def database_sql_preflight(*, force: bool = False, exhaustive: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _PREFLIGHT_LOCK:
        if not force and not exhaustive and _PREFLIGHT_CACHE["result"] and now - _PREFLIGHT_CACHE["at"] < _PREFLIGHT_TTL_SECONDS:
            return dict(_PREFLIGHT_CACHE["result"])
        local = local_sql_preflight()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        to_regclass('public.rd_bars') IS NOT NULL AS rd_bars_ok,
                        to_regclass('public.ra_intraday_features') IS NOT NULL AS features_ok,
                        to_regclass('public.ra_discovery_samples') IS NOT NULL AS samples_ok,
                        to_regclass('public.ra_discovery_task_chunks') IS NOT NULL AS task_chunks_ok,
                        to_regclass('public.ra_discovery_partials') IS NOT NULL AS partials_ok,
                        to_regclass('public.ra_sealed_chunks') IS NOT NULL AS sealed_chunks_ok,
                        EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema='public' AND table_name='ra_candidate_rules'
                              AND column_name='statistics_method'
                        ) AS v2_columns_ok
                    """
                )
                objects = dict(cur.fetchone())
                missing = [key for key, value in objects.items() if not value]
                if missing:
                    raise RuntimeError("Missing v2 database objects: " + ", ".join(missing))
                checks = generated_queries()
                selected = checks if exhaustive else [
                    next(item for item in checks if item[0] == "sample:5"),
                    next(item for item in checks if item[0] == "partial:oversold_reversal:long:30:30"),
                    next(item for item in checks if item[0] == "sealed:oversold_reversal:long:30:30"),
                    next(item for item in checks if item[0] == "feature-build"),
                    next(item for item in checks if item[0] == "universe-build"),
                ]
                for name, query, params in selected:
                    _plan(cur, query, params, name=name)
            conn.rollback()
        result = {**local, "database_plans": len(selected), "objects": objects}
        if not exhaustive:
            _PREFLIGHT_CACHE.update({"at": now, "result": result})
        return result

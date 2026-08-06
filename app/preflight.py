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
    _conditions,
    _exact_stats_query,
    _period_group_query,
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
    row = {dimension.name: next(iter(dimension.labels)) for dimension in dimensions}
    conditions, _ = _conditions(dimensions, row)
    if not conditions:
        raise RuntimeError(f"Preflight could not generate conditions for {family}")
    return conditions


def _sample_feature_query() -> tuple[str, tuple[Any, ...]]:
    config = FeatureBuildConfig(
        name="Preflight features",
        universe_run_id=_NIL_UUID,
        start_date=_SAMPLE_DATE,
        end_date=_SAMPLE_DATE,
        timeframe="1Min",
        feed="sip",
        adjustment="raw",
        session="regular",
        liquidity_tiers=["A", "B"],
        date_chunk_days=1,
        symbol_batch_size=10,
        time_of_day_baseline_days=10,
        predictor_horizons_minutes=[1, 5, 15, 30, 60],
        outcome_horizons_minutes=[5, 15, 30, 60],
    )
    return _feature_sql(config, _SAMPLE_DATE, _SAMPLE_DATE, ["PREFLIGHT"])


def _sample_universe_query() -> tuple[str, tuple[Any, ...]]:
    config = UniverseBuildConfig(
        name="Preflight universe",
        start_date=_SAMPLE_DATE,
        end_date=_SAMPLE_DATE,
        timeframe="1Min",
        feed="sip",
        adjustment="raw",
        session="regular",
        maximum_symbols=10,
        include_symbol_regex=None,
        exclude_symbol_regex=r"[/]",
    )
    return _universe_sql(config, _NIL_UUID)


def local_sql_preflight() -> dict[str, Any]:
    """Validate every generated production query without touching the database.

    Both entry-sampling modes are covered for grouped discovery and exact
    validation/sealed statistics. Universe and feature builders are generated
    from real typed configurations and pass the same Psycopg binding validator.
    """
    checks: list[str] = []
    digest_parts: list[str] = []

    for family, spec in FAMILIES.items():
        conditions = _sample_conditions(family)
        for direction in ("long", "short"):
            for horizon in (5, 15, 30, 60):
                for stride in (1, horizon):
                    group_query = _period_group_query(
                        spec["dimensions"], spec["filter"], horizon, direction,
                        entry_stride_minutes=stride, entry_anchor_minute=570,
                    )
                    group_params = (0.2, _NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE)
                    validate_sql_bindings(
                        group_query, group_params,
                        name=f"group:{family}:{direction}:{horizon}:{stride}",
                    )
                    digest_parts.append(group_query)
                    checks.append(f"group:{family}:{direction}:{horizon}:{stride}")

                    exact_query, condition_params = _exact_stats_query(
                        conditions, direction, horizon,
                        entry_stride_minutes=stride, entry_anchor_minute=570,
                    )
                    exact_params = (
                        0.2, _NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE,
                        _SAMPLE_DATE, _SAMPLE_DATE, *condition_params,
                    )
                    validate_sql_bindings(
                        exact_query, exact_params,
                        name=f"exact:{family}:{direction}:{horizon}:{stride}",
                    )
                    digest_parts.append(exact_query)
                    checks.append(f"exact:{family}:{direction}:{horizon}:{stride}")

    feature_query, feature_params = _sample_feature_query()
    validate_sql_bindings(feature_query, feature_params, name="feature-build")
    digest_parts.append(feature_query)
    checks.append("feature-build")

    universe_query, universe_params = _sample_universe_query()
    validate_sql_bindings(universe_query, universe_params, name="universe-build")
    digest_parts.append(universe_query)
    checks.append("universe-build")

    definition_hash = sha256("\n".join(digest_parts).encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "engine_version": DISCOVERY_VERSION,
        "rule_definition_version": RULE_DEFINITION_VERSION,
        "checks": len(checks),
        "definition_hash": definition_hash,
    }


def _plan(cur: Any, query: str, params: tuple[Any, ...], *, name: str) -> None:
    validate_sql_bindings(query, params, name=name)
    cur.execute("EXPLAIN (FORMAT JSON, COSTS FALSE) " + query, params)
    cur.fetchone()


def database_sql_preflight(*, force: bool = False, exhaustive: bool = False) -> dict[str, Any]:
    """Ask PostgreSQL to plan representative or exhaustive production SQL.

    Normal dashboard checks use representative mode to stay quick. CI invokes
    exhaustive mode, planning every family, direction, horizon and sampling
    mode as well as the feature and universe INSERT statements.
    """
    now = time.monotonic()
    with _PREFLIGHT_LOCK:
        if (
            not force and not exhaustive and _PREFLIGHT_CACHE["result"]
            and now - _PREFLIGHT_CACHE["at"] < _PREFLIGHT_TTL_SECONDS
        ):
            return dict(_PREFLIGHT_CACHE["result"])

        local = local_sql_preflight()
        planned = 0
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        to_regclass('public.rd_bars') IS NOT NULL AS rd_bars_ok,
                        to_regclass('public.rd_assets') IS NOT NULL AS rd_assets_ok,
                        to_regclass('public.ra_analysis_universe') IS NOT NULL AS universe_ok,
                        to_regclass('public.ra_intraday_features') IS NOT NULL AS features_ok,
                        to_regclass('public.ra_candidate_rules') IS NOT NULL AS candidates_ok,
                        EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_schema='public' AND table_name='ra_candidate_rules'
                              AND column_name='entry_stride_minutes'
                        ) AS methodology_columns_ok
                    """
                )
                objects = dict(cur.fetchone())
                if not all(objects.values()):
                    missing = [name for name, value in objects.items() if not value]
                    raise RuntimeError(f"Database preflight failed; missing required objects: {missing}")

                horizons = (5, 15, 30, 60) if exhaustive else (30,)
                directions = ("long", "short") if exhaustive else ("long",)
                strides_for = lambda horizon: (1, horizon)
                for family, spec in FAMILIES.items():
                    conditions = _sample_conditions(family)
                    for direction in directions:
                        for horizon in horizons:
                            for stride in strides_for(horizon):
                                group_query = _period_group_query(
                                    spec["dimensions"], spec["filter"], horizon, direction,
                                    entry_stride_minutes=stride, entry_anchor_minute=570,
                                )
                                group_params = (
                                    0.2, _NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE,
                                    _SAMPLE_DATE, _SAMPLE_DATE,
                                )
                                _plan(
                                    cur, group_query, group_params,
                                    name=f"db-group:{family}:{direction}:{horizon}:{stride}",
                                )
                                planned += 1

                                exact_query, condition_params = _exact_stats_query(
                                    conditions, direction, horizon,
                                    entry_stride_minutes=stride, entry_anchor_minute=570,
                                )
                                exact_params = (
                                    0.2, _NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE,
                                    _SAMPLE_DATE, _SAMPLE_DATE, *condition_params,
                                )
                                _plan(
                                    cur, exact_query, exact_params,
                                    name=f"db-exact:{family}:{direction}:{horizon}:{stride}",
                                )
                                planned += 1

                feature_query, feature_params = _sample_feature_query()
                _plan(cur, feature_query, feature_params, name="db-feature-build")
                planned += 1

                universe_query, universe_params = _sample_universe_query()
                _plan(cur, universe_query, universe_params, name="db-universe-build")
                planned += 1
            conn.rollback()

        result = {
            **local,
            "database_plans": planned,
            "database_objects": objects,
            "exhaustive": exhaustive,
            "cached": False,
        }
        if not exhaustive:
            _PREFLIGHT_CACHE.update({"at": now, "result": result})
        return dict(result)

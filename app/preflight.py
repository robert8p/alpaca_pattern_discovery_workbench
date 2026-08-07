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
from app.robustness import _observation_query
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
        params = (_NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE, _SAMPLE_DATE, 0, 256, _NIL_UUID, "discovery")
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
                    sealed_params = (_NIL_UUID, _SAMPLE_DATE, _SAMPLE_DATE,
                                     _SAMPLE_DATE, _SAMPLE_DATE, 0, 256, 0.2, *condition_params)
                    queries.append((f"sealed:{family}:{direction}:{horizon}:{stride}", sealed_query, sealed_params))

    feature_query, feature_params = _sample_feature_query()
    queries.append(("feature-build", feature_query, feature_params))
    universe_query, universe_params = _sample_universe_query()
    queries.append(("universe-build", universe_query, universe_params))
    robustness_conditions = _sample_conditions("activity_absorption")
    robustness_query, robustness_condition_params = _observation_query(
        robustness_conditions, "long", 30, 30, 570, 0
    )
    queries.append((
        "robustness:activity_absorption:long:30", robustness_query,
        (_NIL_UUID, _SAMPLE_DATE, 0, 30, *robustness_condition_params),
    ))
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
                        to_regclass('public.ra_robustness_runs') IS NOT NULL AS robustness_runs_ok,
                        to_regclass('public.ra_robustness_results') IS NOT NULL AS robustness_results_ok,
                        (SELECT count(*) = 5 FROM information_schema.columns
                         WHERE table_schema='public' AND table_name='ra_discovery_runs'
                           AND column_name = ANY(ARRAY['campaign_name','hypothesis_ids','variant_count','defined_variant_count','campaign_definition_version']))
                        AND (SELECT count(*) = 16 FROM information_schema.columns
                         WHERE table_schema='public' AND table_name='ra_candidate_rules'
                           AND column_name = ANY(ARRAY[
                               'hypothesis_ids','hypothesis_version','variants_tested_campaign','variants_defined_campaign',
                               'multiple_testing_method','multiple_testing_adjusted_p','discovery_p25_pct','discovery_p75_pct',
                               'discovery_p95_pct','discovery_best_pct','validation_p25_pct','validation_p75_pct',
                               'validation_p95_pct','validation_best_pct','discovery_status','sealed_feature_set_id'
                           ]))
                        AND (SELECT count(*) = 26 FROM information_schema.columns
                         WHERE table_schema='public' AND table_name='ra_discovery_samples'
                           AND column_name = ANY(ARRAY[
                               'close','price_group','ret_1m_pct','ret_15m_pct','ret_60m_pct','ret_from_session_open_pct',
                               'relative_trade_count_20bar','rolling_realised_volatility_30bar','rolling_range_30bar_pct',
                               'same_minute_relative_volume','previous_day_range_pct','previous_day_realised_volatility',
                               'activity_adjusted_return_5m','prior_activity_adjusted_return_5m','activity_impact_change_ratio',
                               'prior_relative_volume_20bar','prior_relative_trade_count_20bar','relative_volume_change_ratio',
                               'relative_trade_count_change_ratio','range_vs_previous_day_ratio','volatility_vs_previous_day_ratio',
                               'opening_range_high','opening_range_low','opening_range_position','touched_session_high','touched_session_low'
                           ])) AS coverage_pack_columns_ok
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
                    next(item for item in checks if item[0] == "partial:activity_absorption:long:30:30"),
                    next(item for item in checks if item[0] == "robustness:activity_absorption:long:30"),
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

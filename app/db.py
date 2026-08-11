from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from psycopg import Connection
from psycopg.errors import ReadOnlySqlTransaction
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

logger = logging.getLogger(__name__)
_pool: ConnectionPool | None = None
SCHEMA_VERSION = "2.5.0"
APP_VERSION = "2.5.0"
SCHEMA_MIGRATION_LOCK = "alpaca_pattern_discovery_schema_migration"


class DatabaseConfigurationError(RuntimeError):
    """Raised when the workbench is connected to a non-writable database endpoint."""


def database_target() -> dict[str, Any]:
    """Return a credential-free summary of the configured database endpoint."""
    parsed = urlparse(get_settings().database_url)
    port = parsed.port or 5432
    if port == 6543:
        mode = "transaction_pooler"
    elif parsed.hostname and parsed.hostname.startswith("db."):
        mode = "direct"
    elif parsed.hostname and "pooler.supabase.com" in parsed.hostname:
        mode = "session_pooler"
    else:
        mode = "postgres"
    return {"host": parsed.hostname, "port": port, "mode": mode}


def validate_database_target() -> None:
    target = database_target()
    if target["port"] == 6543:
        raise DatabaseConfigurationError(
            "DATABASE_URL uses port 6543 (transaction pooling). This persistent worker and its "
            "schema migrations require the Supabase Primary Session pooler on port 5432. "
            "Copy the Primary > Session pooler connection string from Supabase and update both "
            "Render services."
        )


def _configure_connection(conn: Connection) -> None:
    """Reset leaked session state and confirm that the endpoint is a writable primary."""
    previous_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET default_transaction_read_only = off")
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
            cur.execute(
                """
                SELECT current_database() AS database_name,
                       current_user AS database_user,
                       pg_is_in_recovery() AS is_replica,
                       current_setting('default_transaction_read_only') AS default_read_only
                """
            )
            state = cur.fetchone()
    finally:
        conn.autocommit = previous_autocommit

    if state and state["is_replica"]:
        target = database_target()
        logger.error(
            "Configured database target %s:%s is a read replica; write transactions will be rejected",
            target["host"], target["port"],
        )
    if state and state["default_read_only"] == "on":
        logger.warning("Database session remained read-only after reset attempt")


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        validate_database_target()
        min_size = min(settings.database_pool_min_size, settings.database_pool_max_size)
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=min_size,
            max_size=settings.database_pool_max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=_configure_connection,
            open=True,
            timeout=30,
        )
    return _pool


@contextlib.contextmanager
def connection() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ WRITE")
        except ReadOnlySqlTransaction as exc:
            conn.rollback()
            target = database_target()
            raise DatabaseConfigurationError(
                "The database endpoint refused a read-write transaction. Confirm that DATABASE_URL "
                "uses Supabase Source=Primary, Session pooler port 5432, and a writable Postgres role. "
                f"Configured target: {target['host']}:{target['port']} ({target['mode']})."
            ) from exc
        yield conn


def database_diagnostics() -> dict[str, Any]:
    target = database_target()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_database() AS database_name,
                       current_user AS database_user,
                       pg_is_in_recovery() AS is_replica,
                       current_setting('default_transaction_read_only') AS default_read_only,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       current_setting('server_version') AS server_version
                """
            )
            state = dict(cur.fetchone())
        conn.rollback()
    return {**target, **state}


def _schema_state(cur: Any) -> dict[str, bool]:
    cur.execute(
        """
        SELECT
            to_regclass('public.ra_jobs') IS NOT NULL AS jobs_ok,
            to_regclass('public.ra_feature_batches') IS NOT NULL AS batches_ok,
            to_regclass('public.ra_intraday_features') IS NOT NULL AS features_ok,
            to_regprocedure('public.ra_ensure_feature_partitions(date,date)') IS NOT NULL AS partition_fn_ok,
            EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_candidate_rules' AND column_name='entry_sampling_mode'
            ) AS methodology_ok,
            to_regclass('public.ra_discovery_samples') IS NOT NULL AS discovery_samples_ok,
            to_regclass('public.ra_discovery_sample_chunks') IS NOT NULL AS sample_chunks_ok,
            EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_discovery_samples'
                  AND column_name='sample_bucket'
            ) AS v2_sample_layout_ok,
            EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_discovery_samples'
                  AND column_name=ANY(ARRAY[
                   'relative_trade_count_20bar','activity_impact_change_ratio',
                   'opening_range_high','opening_range_low','opening_range_position','touched_session_high',
                   'touched_session_low','fwd_return_60m_pct'
               ]))
            AND to_regclass('public.ra_robustness_runs') IS NOT NULL
            AND to_regclass('public.ra_robustness_observations') IS NOT NULL
            AND to_regclass('public.ra_robustness_results') IS NOT NULL
            AND to_regclass('public.ra_robustness_chunks') IS NOT NULL
            AND to_regclass('public.ra_robustness_samples') IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_robustness_runs' AND column_name='engine_version'
            )
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_discovery_partials' AND column_name='best_pct'
            )
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='ra_sealed_chunks' AND column_name='best_pct'
            )
            AND EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check'
                  AND pg_get_constraintdef(oid) LIKE '%%robustness_analysis%%'
            ) AS coverage_pack_ok,
            to_regclass('public.ra_full_history_backfills') IS NOT NULL
            AND to_regclass('public.ra_market_state_features') IS NOT NULL
            AND to_regclass('public.ra_candidate_wave_stats') IS NOT NULL
            AND to_regclass('public.ra_research_ledger') IS NOT NULL
            AND to_regclass('public.ra_research_periods') IS NOT NULL
            AND to_regprocedure('public.ra_ensure_market_state_partitions(date,date)') IS NOT NULL
            AND to_regprocedure('public.ra_ensure_candidate_wave_partitions(date,date)') IS NOT NULL
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ra_jobs_research_period_guard' AND NOT tgisinternal)
            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok
        """
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _schema_is_compatible(cur: Any) -> bool:
    state = _schema_state(cur)
    return bool(state and all(state.values()))


def _v1_core_is_compatible(state: dict[str, bool]) -> bool:
    required = {"jobs_ok", "batches_ok", "features_ok", "partition_fn_ok"}
    return bool(state and all(state.get(key) for key in required))


def _apply_v110_methodology_migration(cur: Any) -> None:
    cur.execute("ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS entry_sampling_mode text NOT NULL DEFAULT 'legacy'")
    cur.execute("ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS entry_stride_minutes integer NOT NULL DEFAULT 1")
    cur.execute("ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS entry_anchor_minute integer NOT NULL DEFAULT 570")
    cur.execute("ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS rule_definition_version text NOT NULL DEFAULT 'legacy'")


def _drop_incompatible_v2_discovery_tables(cur: Any) -> None:
    """Remove only withdrawn draft-v2 staging tables before recreating them."""
    cur.execute("DROP TABLE IF EXISTS ra_discovery_partials CASCADE")
    cur.execute("DROP TABLE IF EXISTS ra_discovery_task_chunks CASCADE")
    cur.execute("DROP TABLE IF EXISTS ra_discovery_samples CASCADE")
    cur.execute("DROP TABLE IF EXISTS ra_discovery_sample_chunks CASCADE")
    cur.execute("DROP TABLE IF EXISTS ra_sealed_chunks CASCADE")


def _apply_v200_discovery_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.0.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v220_coverage_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.2.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v230_robustness_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.3.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v250_full_history_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.5.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def execute_schema() -> None:
    schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA_MIGRATION_LOCK,))
                cur.execute("SET LOCAL lock_timeout = '30s'")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ra_schema_versions (
                        version text PRIMARY KEY,
                        app_version text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute("SELECT 1 FROM ra_schema_versions WHERE version=%s", (SCHEMA_VERSION,))
                already_applied = cur.fetchone() is not None
                if already_applied and _schema_is_compatible(cur):
                    cur.execute(
                        "UPDATE ra_schema_versions SET app_version=%s,applied_at=now() WHERE version=%s",
                        (APP_VERSION, SCHEMA_VERSION),
                    )
                    conn.commit()
                    logger.info("Pattern Discovery Workbench schema %s already installed; startup DDL skipped", SCHEMA_VERSION)
                    return

                state = _schema_state(cur)
                if _v1_core_is_compatible(state):
                    if not state.get("methodology_ok"):
                        _apply_v110_methodology_migration(cur)
                    if (state.get("discovery_samples_ok") or state.get("sample_chunks_ok")) and not state.get("v2_sample_layout_ok"):
                        _drop_incompatible_v2_discovery_tables(cur)
                    _apply_v200_discovery_migration(cur)
                    _apply_v220_coverage_migration(cur)
                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                else:
                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)

                if not _schema_is_compatible(cur):
                    raise RuntimeError("Schema migration completed but Phase 1 compatibility checks still failed")
                cur.execute(
                    "INSERT INTO ra_schema_versions(version,app_version) VALUES (%s,%s) ON CONFLICT (version) DO UPDATE SET app_version=excluded.app_version,applied_at=now()",
                    (SCHEMA_VERSION, APP_VERSION),
                )
            conn.commit()
    except ReadOnlySqlTransaction as exc:
        target = database_target()
        raise DatabaseConfigurationError(
            "Schema migration was blocked by a read-only database transaction. Use the Supabase "
            "Primary Session pooler on port 5432 for DATABASE_URL. "
            f"Configured target: {target['host']}:{target['port']} ({target['mode']})."
        ) from exc
    logger.info("Pattern Discovery Workbench schema %s is ready", SCHEMA_VERSION)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

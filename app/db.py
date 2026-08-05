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
    """Reset leaked session state and confirm that the endpoint is a writable primary.

    Supabase documents that pooled backend connections can retain a session-level
    default_transaction_read_only setting. Resetting it at checkout prevents a
    contaminated backend from making this application intermittently read-only.
    """
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
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=8,
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
            # Transaction-local and safe with pooled connections. This must be the
            # first command in the transaction.
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
                       current_setting('transaction_read_only') AS transaction_read_only,
                       current_setting('default_transaction_read_only') AS default_transaction_read_only,
                       to_regclass('public.rd_bars') AS rd_bars,
                       to_regclass('public.ra_jobs') AS ra_jobs,
                       now() AS checked_at
                """
            )
            row = dict(cur.fetchone())
        conn.rollback()
    return {**target, **row}


def execute_schema() -> None:
    schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_path.read_text(encoding="utf-8"))
            conn.commit()
    except ReadOnlySqlTransaction as exc:
        target = database_target()
        raise DatabaseConfigurationError(
            "Schema migration was blocked by a read-only database transaction. Use the Supabase "
            "Primary Session pooler on port 5432 for DATABASE_URL. "
            f"Configured target: {target['host']}:{target['port']} ({target['mode']})."
        ) from exc
    logger.info("Pattern Discovery Workbench schema is ready")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

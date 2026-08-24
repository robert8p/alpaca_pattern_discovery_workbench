"""Application package security bootstrap.

The workbench exposes an unauthenticated health endpoint for Render. Database
connectivity diagnostics are useful there, but infrastructure identifiers are
not. Install a narrow redaction wrapper before :mod:`app.main` imports the
helpers from :mod:`app.db`.
"""

from __future__ import annotations

from typing import Any

from app import db as _db

_original_database_target = _db.database_target
_original_database_diagnostics = _db.database_diagnostics


def _public_database_target() -> dict[str, Any]:
    target = _original_database_target()
    return {
        "host": "redacted",
        "port": target["port"],
        "mode": target["mode"],
    }


def _public_database_diagnostics() -> dict[str, Any]:
    row = _original_database_diagnostics()
    return {
        "host": "redacted",
        "port": row.get("port"),
        "mode": row.get("mode"),
        "is_replica": bool(row.get("is_replica")),
        "transaction_read_only": row.get("transaction_read_only"),
        "default_transaction_read_only": row.get("default_transaction_read_only"),
        "rd_bars": bool(row.get("rd_bars")),
        "ra_jobs": bool(row.get("ra_jobs")),
        "checked_at": row.get("checked_at"),
    }


_db.database_target = _public_database_target
_db.database_diagnostics = _public_database_diagnostics

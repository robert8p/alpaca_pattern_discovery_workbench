from __future__ import annotations

from pathlib import Path


def test_point_in_time_universe_job_type_is_allowed_by_database_constraint():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "migrations" / "2.6.2.sql").read_text(encoding="utf-8").lower()
    assert "ra_jobs_job_type_check" in sql
    assert "point_in_time_universe_backfill" in sql

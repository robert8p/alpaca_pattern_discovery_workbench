from __future__ import annotations

import logging
import os
import socket
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.utils import json_safe

logger = logging.getLogger(__name__)


class JobInterrupted(RuntimeError):
    def __init__(self, action: str):
        super().__init__(action)
        self.action = action


def make_worker_id() -> str:
    return os.getenv("WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def add_event(job_id: str, event_type: str, message: str, *, level: str = "info", details: dict | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ra_job_events(job_id,level,event_type,message,details) VALUES (%s,%s,%s,%s,%s)",
                (job_id, level, event_type, message, Jsonb(json_safe(details)) if details is not None else None),
            )
        conn.commit()


def create_job(job_type: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ra_jobs(job_type,name,config) VALUES (%s,%s,%s) RETURNING id,status,created_at",
                (job_type, name, Jsonb(json_safe(config))),
            )
            row = cur.fetchone()
            cur.execute(
                "INSERT INTO ra_job_events(job_id,event_type,message) VALUES (%s,'job_created','Job queued.')",
                (row["id"],),
            )
        conn.commit()
    return dict(row)


def recover_stale_jobs() -> int:
    """Recover jobs whose prior worker no longer owns an active process.

    A restart can occur after the web service has changed a running job to
    pause_requested/cancel_requested. Those control states must be reconciled
    as well as ordinary running jobs, otherwise they remain stranded forever.
    This function is called only when a worker process starts, when any query
    owned by the previous process has already been disconnected.
    """
    stale = get_settings().worker_stale_seconds
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_jobs SET status='queued',phase='recovered',claimed_by=NULL,
                    attempts=GREATEST(attempts-1,0),
                    error=COALESCE(error,'Recovered after a stale worker heartbeat')
                WHERE status='running'
                  AND (heartbeat_at IS NULL OR heartbeat_at < now() - (%s * interval '1 second'))
                """,
                (stale,),
            )
            running_count = cur.rowcount or 0

            cur.execute(
                """
                UPDATE ra_jobs SET status='paused',phase='paused',claimed_by=NULL,
                    heartbeat_at=now()
                WHERE status='pause_requested'
                  AND (heartbeat_at IS NULL OR heartbeat_at < now() - (%s * interval '1 second'))
                """,
                (stale,),
            )
            paused_count = cur.rowcount or 0

            cur.execute(
                """
                UPDATE ra_jobs SET status='cancelled',phase='cancelled',claimed_by=NULL,
                    heartbeat_at=now(),completed_at=COALESCE(completed_at,now())
                WHERE status='cancel_requested'
                  AND (heartbeat_at IS NULL OR heartbeat_at < now() - (%s * interval '1 second'))
                RETURNING id,job_type
                """,
                (stale,),
            )
            cancelled = cur.fetchall()
            for row in cancelled:
                if row['job_type'] == 'feature_build':
                    cur.execute("UPDATE ra_feature_sets SET status='cancelled' WHERE job_id=%s", (row['id'],))
                    cur.execute(
                        """
                        UPDATE ra_feature_chunks SET status='cancelled'
                        WHERE feature_set_id IN (SELECT id FROM ra_feature_sets WHERE job_id=%s)
                          AND status IN ('pending','running','failed')
                        """,
                        (row['id'],),
                    )
                    cur.execute(
                        """
                        UPDATE ra_feature_batches SET status='cancelled'
                        WHERE feature_chunk_id IN (
                            SELECT c.id FROM ra_feature_chunks c
                            JOIN ra_feature_sets f ON f.id=c.feature_set_id
                            WHERE f.job_id=%s
                        ) AND status IN ('pending','running','failed')
                        """,
                        (row['id'],),
                    )
                elif row['job_type'] == 'discovery_scan':
                    cur.execute("UPDATE ra_discovery_runs SET status='cancelled',completed_at=now() WHERE job_id=%s", (row['id'],))
                    cur.execute(
                        "UPDATE ra_discovery_tasks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','running','failed')",
                        (row['id'],),
                    )
        conn.commit()
    return running_count + paused_count + len(cancelled)


def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    max_attempts = get_settings().max_job_attempts
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM ra_jobs
                WHERE status='queued' AND attempts < %s
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (max_attempts,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE ra_jobs SET status='running',attempts=attempts+1,claimed_by=%s,
                        started_at=COALESCE(started_at,now()),heartbeat_at=now(),error=NULL
                    WHERE id=%s
                    """,
                    (worker_id, row["id"]),
                )
        conn.commit()
    return dict(row) if row else None


def worker_heartbeat(worker_id: str, version: str, status: str, job_id: str | None = None, details: dict | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ra_workers(worker_id,status,current_job_id,version,details,heartbeat_at)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status,
                    current_job_id=excluded.current_job_id,version=excluded.version,
                    details=excluded.details,heartbeat_at=now()
                """,
                (worker_id, status, job_id, version, Jsonb(json_safe(details or {}))),
            )
            if job_id:
                cur.execute("UPDATE ra_jobs SET heartbeat_at=now(),claimed_by=%s WHERE id=%s", (worker_id, job_id))
        conn.commit()


def set_progress(job_id: str, phase: str, current: int, total: int, *, result: dict | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_jobs SET phase=%s,progress_current=%s,progress_total=%s,
                    result=COALESCE(%s,result),heartbeat_at=now() WHERE id=%s
                """,
                (phase, current, total, Jsonb(json_safe(result)) if result is not None else None, job_id),
            )
        conn.commit()


def check_control(job_id: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM ra_jobs WHERE id=%s", (job_id,))
            row = cur.fetchone()
        conn.rollback()
    status = row["status"] if row else "cancel_requested"
    if status == "pause_requested":
        raise JobInterrupted("pause")
    if status == "cancel_requested":
        raise JobInterrupted("cancel")


def finish_job(job_id: str, result: dict[str, Any] | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_jobs SET status='completed',phase='completed',
                    progress_current=CASE WHEN progress_total>0 THEN progress_total ELSE progress_current END,
                    result=COALESCE(%s,result),completed_at=now(),heartbeat_at=now(),error=NULL
                WHERE id=%s
                """,
                (Jsonb(json_safe(result)) if result is not None else None, job_id),
            )
        conn.commit()
    add_event(job_id, "job_completed", "Job completed successfully.")


def interrupt_job(job_id: str, action: str) -> None:
    target = "paused" if action == "pause" else "cancelled"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_jobs SET status=%s,phase=%s,heartbeat_at=now(),completed_at=CASE WHEN %s='cancelled' THEN now() ELSE completed_at END WHERE id=%s", (target, target, target, job_id))
        conn.commit()
    add_event(job_id, f"job_{target}", f"Job {target}.", level="warning")


def fail_job(job_id: str, error: Exception | str) -> None:
    message = str(error)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_jobs SET status='failed',phase='failed',error=%s,completed_at=now(),heartbeat_at=now() WHERE id=%s", (message, job_id))
        conn.commit()
    add_event(job_id, "job_failed", message, level="error")

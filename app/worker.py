from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from app.config import get_settings
from app.db import close_pool, connection, execute_schema
from app.discovery import run_discovery, run_sealed_evaluation
from app.features import build_feature_set
from app.jobs import (
    JobInterrupted, claim_next_job, fail_job, finish_job, interrupt_job,
    make_worker_id, recover_stale_jobs, worker_heartbeat,
)
from app.models import DiscoveryConfig, FeatureBuildConfig, QualityScanConfig, SealedEvaluationConfig, UniverseBuildConfig
from app.quality import run_quality_scan
from app.preflight import local_sql_preflight
from app.universe import build_universe

VERSION = "2.0.0"
logger = logging.getLogger(__name__)
stop_event = asyncio.Event()


def _mark_related(job: dict[str, Any], status: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            if job["job_type"] == "feature_build":
                cur.execute("UPDATE ra_feature_sets SET status=%s WHERE job_id=%s", ("cancelled" if status == "cancelled" else "building", job["id"]))
                if status == "cancelled":
                    cur.execute("UPDATE ra_feature_chunks SET status='cancelled' WHERE feature_set_id IN (SELECT id FROM ra_feature_sets WHERE job_id=%s) AND status IN ('pending','failed')", (job["id"],))
                    cur.execute(
                        """
                        UPDATE ra_feature_batches SET status='cancelled'
                        WHERE feature_chunk_id IN (
                            SELECT c.id
                            FROM ra_feature_chunks c
                            JOIN ra_feature_sets f ON f.id=c.feature_set_id
                            WHERE f.job_id=%s
                        ) AND status IN ('pending','failed')
                        """,
                        (job["id"],),
                    )
            elif job["job_type"] == "discovery_scan":
                if status == "cancelled":
                    cur.execute("UPDATE ra_discovery_runs SET status='cancelled',completed_at=now() WHERE job_id=%s", (job["id"],))
                    cur.execute("UPDATE ra_discovery_tasks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','running','failed')", (job["id"],))
                    cur.execute("UPDATE ra_discovery_sample_chunks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','running','failed')", (job["id"],))
                    cur.execute("UPDATE ra_discovery_task_chunks SET status='cancelled' WHERE discovery_task_id IN (SELECT t.id FROM ra_discovery_tasks t JOIN ra_discovery_runs r ON r.id=t.discovery_run_id WHERE r.job_id=%s) AND status IN ('pending','running','failed')", (job["id"],))
                elif status == "paused":
                    cur.execute("UPDATE ra_discovery_sample_chunks SET status='pending' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                    cur.execute("UPDATE ra_discovery_task_chunks SET status='pending' WHERE discovery_task_id IN (SELECT t.id FROM ra_discovery_tasks t JOIN ra_discovery_runs r ON r.id=t.discovery_run_id WHERE r.job_id=%s) AND status='running'", (job["id"],))
        conn.commit()


def _dispatch(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job["id"])
    config = job["config"]
    if job["job_type"] == "quality_scan":
        return run_quality_scan(job_id, QualityScanConfig.model_validate(config))
    if job["job_type"] == "universe_build":
        return build_universe(job_id, UniverseBuildConfig.model_validate(config))
    if job["job_type"] == "feature_build":
        return build_feature_set(job_id, FeatureBuildConfig.model_validate(config))
    if job["job_type"] == "discovery_scan":
        return run_discovery(job_id, DiscoveryConfig.model_validate(config))
    if job["job_type"] == "sealed_evaluation":
        return run_sealed_evaluation(job_id, SealedEvaluationConfig.model_validate(config))
    raise ValueError(f"Unsupported job type: {job['job_type']}")


async def run_worker() -> None:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.auto_migrate:
        execute_schema()
    preflight = local_sql_preflight()
    logger.info(
        "Discovery SQL preflight passed: %s checks, definition %s",
        preflight["checks"], preflight["definition_hash"][:12],
    )
    worker_id = make_worker_id()
    recovered = recover_stale_jobs()
    worker_heartbeat(worker_id, VERSION, "idle", details={"recovered_jobs": recovered})
    logger.info("Pattern workbench worker %s started", worker_id)

    while not stop_event.is_set():
        job: dict[str, Any] | None = None
        try:
            job = claim_next_job(worker_id)
            if not job:
                worker_heartbeat(worker_id, VERSION, "idle")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass
                continue
            worker_heartbeat(worker_id, VERSION, "running", str(job["id"]), {"job_type": job["job_type"]})
            result = await asyncio.to_thread(_dispatch, job)
            finish_job(str(job["id"]), result)
        except JobInterrupted as exc:
            if job:
                _mark_related(job, "paused" if exc.action == "pause" else "cancelled")
                interrupt_job(str(job["id"]), exc.action)
        except Exception as exc:
            logger.exception("Analysis job failed")
            if job:
                _mark_related(job, "failed")
                fail_job(str(job["id"]), exc)
            await asyncio.sleep(1)

    worker_heartbeat(worker_id, VERSION, "stopped")
    close_pool()


def _signal_handler(*_: Any) -> None:
    stop_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    asyncio.run(run_worker())

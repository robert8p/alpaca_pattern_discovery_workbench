from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from app.config import get_settings
from app.db import close_pool, connection, execute_schema
from app.discovery_v3 import _ensure_discovery_run, run_discovery, run_sealed_evaluation
from app.robustness_v3 import run_robustness
from app.executable_strategy import assert_strategy_frozen, run_strategy_economics
from app.features import build_feature_set
from app.full_history import (
    assert_candidate_frozen, record_sealed_result, register_research_campaign, run_candidate_wave_build,
    run_historical_feature_backfill, run_market_state_build, sync_candidate_ledger,
)
from app.history_backfill import backfill_feature_config
from app.point_in_time_universe import ensure_point_in_time_universes
from app.jobs import (
    JobInterrupted, claim_next_job, fail_job, finish_job, interrupt_job,
    make_worker_id, recover_stale_jobs, worker_heartbeat,
)
from app.models import (
    CandidateWaveBuildConfig, DiscoveryConfig, FeatureBuildConfig, HistoricalFeatureBackfillConfig,
    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,
)
from app.quality import run_quality_scan
from app.preflight import local_sql_preflight
from app.universe import build_universe

VERSION = "2.7.0"
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
            elif job["job_type"] == "robustness_analysis":
                if status == "paused":
                    cur.execute("UPDATE ra_robustness_chunks SET status='pending' WHERE robustness_run_id IN (SELECT id FROM ra_robustness_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_robustness_runs SET status=%s,completed_at=CASE WHEN %s='cancelled' THEN now() ELSE completed_at END WHERE job_id=%s", (status, status, job["id"]))
                if status == "cancelled":
                    cur.execute("UPDATE ra_robustness_chunks SET status='cancelled' WHERE robustness_run_id IN (SELECT id FROM ra_robustness_runs WHERE job_id=%s) AND status IN ('pending','running','failed')", (job["id"],))
            elif job["job_type"] == "historical_feature_backfill":
                cur.execute("UPDATE ra_full_history_backfills SET status=%s,latest_error=CASE WHEN %s='failed' THEN latest_error ELSE NULL END WHERE job_id=%s", (status, status, job["id"]))
                if status == "paused":
                    cur.execute("UPDATE ra_feature_chunks SET status='pending' WHERE feature_set_id IN (SELECT id FROM ra_feature_sets WHERE job_id=%s) AND status='running'", (job["id"],))
                    cur.execute("UPDATE ra_feature_batches SET status='pending' WHERE feature_chunk_id IN (SELECT c.id FROM ra_feature_chunks c JOIN ra_feature_sets f ON f.id=c.feature_set_id WHERE f.job_id=%s) AND status='running'", (job["id"],))
            elif job["job_type"] == "point_in_time_universe_backfill":
                cur.execute(
                    "UPDATE ra_point_in_time_universe_runs SET status=%s,latest_error=CASE WHEN %s='failed' THEN latest_error ELSE NULL END WHERE parent_job_id=%s",
                    (status, status, job["id"]),
                )
            elif job["job_type"] == "market_state_build":
                if status == "paused":
                    cur.execute("UPDATE ra_market_state_chunks SET status='pending' WHERE market_state_run_id IN (SELECT id FROM ra_market_state_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_market_state_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "strategy_economics_analysis":
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_strategy_economics_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
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
        model = DiscoveryConfig.model_validate(config)
        run_id, _ = _ensure_discovery_run(job_id, model)
        register_research_campaign(run_id)
        result = run_discovery(job_id, model)
        sync_candidate_ledger(run_id)
        return result
    if job["job_type"] == "robustness_analysis":
        return run_robustness(job_id, RobustnessAnalysisConfig.model_validate(config))
    if job["job_type"] == "sealed_evaluation":
        model = SealedEvaluationConfig.model_validate(config)
        assert_candidate_frozen(model.candidate_id)
        assert_strategy_frozen(model.candidate_id)
        result = run_sealed_evaluation(job_id, model)
        record_sealed_result(model.candidate_id, model.sealed_start, model.sealed_end, result)
        return result
    if job["job_type"] == "historical_feature_backfill":
        return run_historical_feature_backfill(job_id, HistoricalFeatureBackfillConfig.model_validate(config))
    if job["job_type"] == "point_in_time_universe_backfill":
        model = HistoricalFeatureBackfillConfig.model_validate(config)
        reference_universe_run_id = backfill_feature_config(model).universe_run_id
        return ensure_point_in_time_universes(job_id, model, reference_universe_run_id)
    if job["job_type"] == "market_state_build":
        return run_market_state_build(job_id, MarketStateBuildConfig.model_validate(config))
    if job["job_type"] == "candidate_wave_build":
        return run_candidate_wave_build(job_id, CandidateWaveBuildConfig.model_validate(config))
    if job["job_type"] == "strategy_economics_analysis":
        return run_strategy_economics(job_id, StrategyEconomicsConfig.model_validate(config))
    if job["job_type"] == "strategy_combination_analysis":
        raise ValueError("Combination execution remains locked until at least two independently validated standalone executable strategies qualify and the combination methodology is frozen.")
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

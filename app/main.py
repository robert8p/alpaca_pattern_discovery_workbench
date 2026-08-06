from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import close_pool, connection, database_diagnostics, database_target, execute_schema
from app.features import estimate_feature_build
from app.jobs import create_job
from app.preflight import database_sql_preflight, local_sql_preflight
from app.models import (
    DiscoveryConfig, FeatureBuildConfig, FeatureEstimateRequest, JobCreateRequest,
    QualityScanConfig, SealedEvaluationConfig, UniverseBuildConfig,
)
from app.utils import json_safe

VERSION = "1.1.0"
logger = logging.getLogger(__name__)
settings = get_settings()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.auto_migrate:
        execute_schema()
    yield
    close_pool()


app = FastAPI(title="Alpaca Pattern Discovery Workbench", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    username_ok = secrets.compare_digest(credentials.username.encode(), settings.app_username.encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), settings.app_password.encode())
    if not (username_ok and password_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(400, "Invalid identifier") from exc


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        row = database_diagnostics()
        writable_primary = not row["is_replica"] and row["transaction_read_only"] == "off"
        return {
            "status": "ok" if row["rd_bars"] and row["ra_jobs"] and writable_primary else "degraded",
            "version": VERSION,
            **row,
        }
    except Exception as exc:
        return {"status": "degraded", "version": VERSION, "database_target": database_target(), "error": str(exc)}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request, "version": VERSION})


@app.get("/api/dashboard")
def dashboard(_: str = Depends(require_auth)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE status IN ('queued','running','pause_requested')) AS active_jobs,
                    count(*) FILTER (WHERE status='completed') AS completed_jobs,
                    count(*) FILTER (WHERE status='failed') AS failed_jobs
                FROM ra_jobs
                """
            )
            jobs_metrics = cur.fetchone()
            cur.execute("SELECT COALESCE(sum(row_count),0) AS feature_rows,count(*) FILTER (WHERE status='completed') AS feature_sets FROM ra_feature_sets")
            feature_metrics = cur.fetchone()
            cur.execute("SELECT count(*) AS candidates,count(*) FILTER (WHERE workflow_status='shortlisted') AS shortlisted,count(*) FILTER (WHERE workflow_status='sealed_tested') AS sealed FROM ra_candidate_rules")
            candidate_metrics = cur.fetchone()
            cur.execute(
                """
                SELECT id,job_type,name,status,phase,progress_current,progress_total,error,created_at,started_at,completed_at,
                    CASE WHEN progress_total>0 THEN round(progress_current::numeric/progress_total*100,1) ELSE 0 END AS progress_pct
                FROM ra_jobs ORDER BY created_at DESC LIMIT 12
                """
            )
            jobs = cur.fetchall()
            cur.execute("SELECT worker_id,status,current_job_id,version,heartbeat_at,EXTRACT(EPOCH FROM (now()-heartbeat_at))::integer AS heartbeat_age_seconds FROM ra_workers ORDER BY heartbeat_at DESC LIMIT 5")
            workers = cur.fetchall()
            cur.execute("SELECT id,name,total_symbols,included_symbols,tier_a_symbols,tier_b_symbols,tier_c_symbols,created_at,completed_at FROM ra_universe_runs ORDER BY created_at DESC LIMIT 5")
            universes = cur.fetchall()
            cur.execute("SELECT id,name,status,symbol_count,row_count,min_trade_date,max_trade_date,created_at,completed_at FROM ra_feature_sets ORDER BY created_at DESC LIMIT 5")
            feature_sets = cur.fetchall()
        conn.rollback()
    return json_safe({"jobs_metrics": jobs_metrics, "feature_metrics": feature_metrics, "candidate_metrics": candidate_metrics, "jobs": jobs, "workers": workers, "universes": universes, "feature_sets": feature_sets})


@app.get("/api/source-inventory")
def source_inventory(_: str = Depends(require_auth)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.rd_bars') AS bars,to_regclass('public.rd_inventory') AS inventory")
            exists = cur.fetchone()
            if not exists["bars"]:
                return {"available": False, "rows": []}
            if exists["inventory"]:
                cur.execute(
                    """
                    SELECT timeframe,feed,adjustment,'retained sessions'::text AS session_label,
                        rows_loaded AS bars,NULL::bigint AS symbols,
                        min_bar_ts AS first_bar,max_bar_ts AS last_bar
                    FROM rd_inventory ORDER BY timeframe,feed,adjustment
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT timeframe,feed,adjustment,'mixed'::text AS session_label,
                        count(*) AS bars,count(DISTINCT symbol) AS symbols,
                        min(bar_ts) AS first_bar,max(bar_ts) AS last_bar
                    FROM rd_bars GROUP BY timeframe,feed,adjustment
                    ORDER BY timeframe,feed,adjustment
                    """
                )
            rows = cur.fetchall()
        conn.rollback()
    return json_safe({"available": True, "rows": rows})


@app.post("/api/jobs", status_code=201)
def queue_job(payload: JobCreateRequest, _: str = Depends(require_auth)) -> dict[str, Any]:
    validators = {
        "quality_scan": QualityScanConfig,
        "universe_build": UniverseBuildConfig,
        "feature_build": FeatureBuildConfig,
        "discovery_scan": DiscoveryConfig,
        "sealed_evaluation": SealedEvaluationConfig,
    }
    model = validators[payload.job_type].model_validate(payload.config)
    if payload.job_type in {"discovery_scan", "sealed_evaluation"}:
        try:
            database_sql_preflight()
        except Exception as exc:
            raise HTTPException(503, f"Analysis SQL preflight failed: {exc}") from exc
    name = getattr(model, "name", f"Sealed evaluation {getattr(model, 'candidate_id', '')}")
    return json_safe(create_job(payload.job_type, name, model.model_dump(mode="json")))


@app.post("/api/feature-estimate")
def feature_estimate(payload: FeatureEstimateRequest, _: str = Depends(require_auth)) -> dict[str, Any]:
    return json_safe(estimate_feature_build(payload.config))


@app.get("/api/jobs")
def list_jobs(limit: int = 100, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,job_type,name,status,phase,progress_current,progress_total,result,error,
                    attempts,created_at,started_at,completed_at,heartbeat_at,
                    CASE WHEN progress_total>0 THEN round(progress_current::numeric/progress_total*100,1) ELSE 0 END AS progress_pct
                FROM ra_jobs ORDER BY created_at DESC LIMIT %s
                """,
                (min(max(limit, 1), 500),),
            )
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
    jid = parse_uuid(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_jobs WHERE id=%s", (jid,))
            job = cur.fetchone()
            if not job:
                raise HTTPException(404, "Job not found")
            cur.execute("SELECT level,event_type,message,details,created_at FROM ra_job_events WHERE job_id=%s ORDER BY created_at DESC LIMIT 150", (jid,))
            events = cur.fetchall()
            chunks = []
            if job["job_type"] == "feature_build":
                cur.execute(
                    """
                    SELECT c.chunk_start,c.chunk_end,c.status,c.rows_written,c.attempts,c.error,
                        c.started_at,c.completed_at,
                        count(b.id) AS symbol_batches,
                        count(b.id) FILTER (WHERE b.status='completed') AS completed_symbol_batches,
                        count(b.id) FILTER (WHERE b.status='failed') AS failed_symbol_batches
                    FROM ra_feature_chunks c
                    LEFT JOIN ra_feature_batches b ON b.feature_chunk_id=c.id
                    WHERE c.feature_set_id IN (SELECT id FROM ra_feature_sets WHERE job_id=%s)
                    GROUP BY c.id
                    ORDER BY c.chunk_start
                    """,
                    (jid,),
                )
                chunks = cur.fetchall()
            elif job["job_type"] == "discovery_scan":
                cur.execute(
                    """
                    SELECT family,direction,holding_horizon_minutes,status,groups_tested,
                        candidates_retained,attempts,error,started_at,completed_at
                    FROM ra_discovery_tasks
                    WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s)
                    ORDER BY id
                    """,
                    (jid,),
                )
                chunks = cur.fetchall()
        conn.rollback()
    return json_safe({"job": job, "events": events, "chunks": chunks})


@app.post("/api/jobs/{job_id}/actions/{action}")
def job_action(job_id: str, action: str, _: str = Depends(require_auth)) -> dict[str, Any]:
    jid = parse_uuid(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,job_type FROM ra_jobs WHERE id=%s FOR UPDATE", (jid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            current = row["status"]
            if action == "pause" and current == "running":
                target = "pause_requested"
            elif action == "resume" and current == "paused":
                target = "queued"
                cur.execute("UPDATE ra_jobs SET error=NULL,completed_at=NULL,attempts=0 WHERE id=%s", (jid,))
            elif action == "cancel" and current in {"queued", "running", "pause_requested", "paused", "failed"}:
                target = "cancel_requested" if current in {"running", "pause_requested"} else "cancelled"
            elif action == "retry" and current in {"failed", "cancelled"}:
                target = "queued"
                cur.execute("UPDATE ra_jobs SET error=NULL,completed_at=NULL,attempts=0 WHERE id=%s", (jid,))
            elif action == "delete" and current in {"completed", "failed", "cancelled"}:
                if row["job_type"] == "universe_build":
                    cur.execute(
                        "SELECT count(*) AS n FROM ra_feature_sets WHERE universe_run_id IN "
                        "(SELECT id FROM ra_universe_runs WHERE job_id=%s)",
                        (jid,),
                    )
                    if int(cur.fetchone()["n"]):
                        raise HTTPException(409, "Cannot delete this universe because feature sets depend on it")
                elif row["job_type"] == "feature_build":
                    cur.execute(
                        "SELECT count(*) AS n FROM ra_discovery_runs WHERE feature_set_id IN "
                        "(SELECT id FROM ra_feature_sets WHERE job_id=%s)",
                        (jid,),
                    )
                    if int(cur.fetchone()["n"]):
                        raise HTTPException(409, "Cannot delete this feature set because discovery runs depend on it")
                elif row["job_type"] == "discovery_scan":
                    cur.execute(
                        "SELECT count(*) AS n FROM ra_candidate_rules WHERE discovery_run_id IN "
                        "(SELECT id FROM ra_discovery_runs WHERE job_id=%s)",
                        (jid,),
                    )
                    if int(cur.fetchone()["n"]):
                        raise HTTPException(409, "Cannot delete this discovery run while it contains candidate rules")
                cur.execute("DELETE FROM ra_jobs WHERE id=%s", (jid,))
                conn.commit()
                return {"ok": True, "status": "deleted"}
            else:
                raise HTTPException(409, f"Cannot {action} a {current} job")
            cur.execute("UPDATE ra_jobs SET status=%s,completed_at=CASE WHEN %s='cancelled' THEN now() ELSE completed_at END WHERE id=%s", (target, target, jid))
            if target == "cancelled" and row["job_type"] == "feature_build":
                cur.execute("UPDATE ra_feature_sets SET status='cancelled' WHERE job_id=%s", (jid,))
                cur.execute("UPDATE ra_feature_chunks SET status='cancelled' WHERE feature_set_id IN (SELECT id FROM ra_feature_sets WHERE job_id=%s) AND status IN ('pending','failed')", (jid,))
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
                    (jid,),
                )
            elif target == "cancelled" and row["job_type"] == "discovery_scan":
                cur.execute("UPDATE ra_discovery_runs SET status='cancelled',completed_at=now() WHERE job_id=%s", (jid,))
                cur.execute("UPDATE ra_discovery_tasks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','failed')", (jid,))
            cur.execute("INSERT INTO ra_job_events(job_id,event_type,message) VALUES (%s,%s,%s)", (jid, f"action_{action}", f"Action requested: {action}."))
        conn.commit()
    return {"ok": True, "status": target}


@app.get("/api/quality-reports")
def quality_reports(limit: int = 20, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,job_id,name,source_config,summary,anomalies,created_at FROM ra_quality_reports ORDER BY created_at DESC LIMIT %s", (min(limit, 100),))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/quality-reports/{report_id}")
def quality_report(report_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_quality_reports WHERE id=%s", (parse_uuid(report_id),))
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise HTTPException(404, "Report not found")
    return json_safe(row)


@app.get("/api/universes")
def universes(limit: int = 50, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_universe_runs ORDER BY created_at DESC LIMIT %s", (min(limit, 200),))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/universes/{run_id}/symbols")
def universe_symbols(run_id: str, included: bool | None = True, limit: int = 500, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    clauses = ["universe_run_id=%s"]
    params: list[Any] = [parse_uuid(run_id)]
    if included is not None:
        clauses.append("included=%s")
        params.append(included)
    params.append(min(limit, 5000))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ra_analysis_universe WHERE {' AND '.join(clauses)} ORDER BY included DESC,rank_by_liquidity NULLS LAST,symbol LIMIT %s", tuple(params))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/feature-sets")
def feature_sets(limit: int = 100, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_feature_sets ORDER BY created_at DESC LIMIT %s", (min(limit, 200),))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/discovery-runs")
def discovery_runs(limit: int = 50, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_discovery_runs ORDER BY created_at DESC LIMIT %s", (min(limit, 200),))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/candidates")
def candidates(discovery_run_id: str | None = None, status_filter: str | None = None, limit: int = 250, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    clauses = ["TRUE"]
    params: list[Any] = []
    if discovery_run_id:
        clauses.append("discovery_run_id=%s")
        params.append(parse_uuid(discovery_run_id))
    if status_filter:
        clauses.append("workflow_status=%s")
        params.append(status_filter)
    params.append(min(limit, 1000))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ra_candidate_rules WHERE {' AND '.join(clauses)} ORDER BY rank_score DESC NULLS LAST,created_at DESC LIMIT %s", tuple(params))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.post("/api/candidates/{candidate_id}/actions/{action}")
def candidate_action(candidate_id: str, action: str, _: str = Depends(require_auth)) -> dict[str, Any]:
    statuses = {"shortlist": "shortlisted", "reject": "rejected", "restore": "new"}
    if action not in statuses:
        raise HTTPException(400, "Unsupported candidate action")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ra_candidate_rules SET workflow_status=%s WHERE id=%s RETURNING id", (statuses[action], parse_uuid(candidate_id)))
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "Candidate not found")
    return {"ok": True, "status": statuses[action]}


@app.post("/api/candidates/{candidate_id}/sealed", status_code=201)
def queue_sealed(candidate_id: str, payload: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
    config = SealedEvaluationConfig.model_validate({"candidate_id": parse_uuid(candidate_id), **payload})
    return json_safe(create_job("sealed_evaluation", f"Sealed evaluation · {str(config.candidate_id)[:8]}", config.model_dump(mode="json")))


@app.get("/api/dependencies")
def dependencies(_: str = Depends(require_auth)) -> dict[str, Any]:
    database = database_diagnostics()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id,status,current_job_id,version,heartbeat_at,EXTRACT(EPOCH FROM (now()-heartbeat_at))::integer AS heartbeat_age_seconds FROM ra_workers ORDER BY heartbeat_at DESC")
            workers = cur.fetchall()
        conn.rollback()
    preflight = database_sql_preflight(force=True)
    return json_safe({
        "database": database,
        "workers": workers,
        "preflight": preflight,
        "auth_warning": settings.app_password == "change-me",
        "raw_write_policy": "Application code only SELECTs from rd_ tables.",
    })

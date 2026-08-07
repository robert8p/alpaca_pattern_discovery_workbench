from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import close_pool, connection, database_diagnostics, database_target, execute_schema
from app.features import estimate_feature_build
from app.exports import build_candidate_export_bundle, export_filename
from app.jobs import create_job
from app.preflight import database_sql_preflight, local_sql_preflight
from app.models import (
    DiscoveryConfig, FeatureBuildConfig, FeatureEstimateRequest, JobCreateRequest,
    QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,
)
from app.utils import json_safe

VERSION = "2.3.0"
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
        "robustness_analysis": RobustnessAnalysisConfig,
        "sealed_evaluation": SealedEvaluationConfig,
    }
    model = validators[payload.job_type].model_validate(payload.config)
    if payload.job_type in {"discovery_scan", "robustness_analysis", "sealed_evaluation"}:
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
                    SELECT t.family,t.direction,t.holding_horizon_minutes,t.status,t.stage,t.groups_tested,
                        t.candidates_retained,t.attempts,t.error,t.started_at,t.completed_at,
                        count(c.id) FILTER (WHERE c.status<>'split') AS scan_chunks,
                        count(c.id) FILTER (WHERE c.status='completed') AS completed_scan_chunks,
                        COALESCE(sum(c.observations_scanned),0)::bigint AS observations_scanned
                    FROM ra_discovery_tasks t
                    LEFT JOIN ra_discovery_task_chunks c ON c.discovery_task_id=t.id
                    WHERE t.discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s)
                    GROUP BY t.id
                    ORDER BY t.id
                    """,
                    (jid,),
                )
                chunks = cur.fetchall()
            elif job["job_type"] == "robustness_analysis":
                cur.execute(
                    """
                    SELECT variant_key,trade_date,
                        CASE
                            WHEN count(*) FILTER (WHERE status='failed') > 0 THEN 'failed'
                            WHEN count(*) FILTER (WHERE status='running') > 0 THEN 'running'
                            WHEN count(*) FILTER (WHERE status='pending') > 0 THEN 'pending'
                            WHEN count(*) FILTER (WHERE status='cancelled') > 0 THEN 'cancelled'
                            ELSE 'completed'
                        END AS status,
                        count(*) FILTER (WHERE status<>'split') AS chunks,
                        count(*) FILTER (WHERE status='completed') AS completed_chunks,
                        COALESCE(sum(rows_written) FILTER (WHERE status='completed'),0)::bigint AS rows_written,
                        max(attempts) AS max_attempts,
                        max(error) FILTER (WHERE status='failed') AS error
                    FROM ra_robustness_chunks
                    WHERE robustness_run_id IN (SELECT id FROM ra_robustness_runs WHERE job_id=%s)
                    GROUP BY variant_key,trade_date
                    ORDER BY trade_date,variant_key
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
                    cur.execute(
                        "SELECT count(*) AS n FROM ra_robustness_runs WHERE target_feature_set_id IN "
                        "(SELECT id FROM ra_feature_sets WHERE job_id=%s) OR source_feature_set_id IN "
                        "(SELECT id FROM ra_feature_sets WHERE job_id=%s)",
                        (jid, jid),
                    )
                    if int(cur.fetchone()["n"]):
                        raise HTTPException(409, "Cannot delete this feature set because robustness evidence depends on it")
                    cur.execute(
                        "SELECT count(*) AS n FROM ra_candidate_rules WHERE sealed_feature_set_id IN "
                        "(SELECT id FROM ra_feature_sets WHERE job_id=%s)",
                        (jid,),
                    )
                    if int(cur.fetchone()["n"]):
                        raise HTTPException(409, "Cannot delete this feature set because sealed-test evidence depends on it")
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
                cur.execute("UPDATE ra_discovery_tasks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','running','failed')", (jid,))
                cur.execute("UPDATE ra_discovery_sample_chunks SET status='cancelled' WHERE discovery_run_id IN (SELECT id FROM ra_discovery_runs WHERE job_id=%s) AND status IN ('pending','running','failed')", (jid,))
                cur.execute("UPDATE ra_discovery_task_chunks SET status='cancelled' WHERE discovery_task_id IN (SELECT t.id FROM ra_discovery_tasks t JOIN ra_discovery_runs r ON r.id=t.discovery_run_id WHERE r.job_id=%s) AND status IN ('pending','running','failed')", (jid,))
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
            where_sql = ' AND '.join('c.' + clause if clause != 'TRUE' else clause for clause in clauses)
            cur.execute(f"""
                SELECT c.*,rr.id AS robustness_run_id,rr.verdict AS robustness_verdict,rr.summary AS robustness_summary,
                       rr.mode AS robustness_mode,rr.target_feature_set_id AS robustness_target_feature_set_id,
                       rr.completed_at AS robustness_completed_at
                FROM ra_candidate_rules c
                LEFT JOIN LATERAL (
                    SELECT * FROM ra_robustness_runs r
                    WHERE r.candidate_id=c.id AND r.status='completed'
                    ORDER BY r.completed_at DESC NULLS LAST,r.created_at DESC LIMIT 1
                ) rr ON TRUE
                WHERE {where_sql}
                ORDER BY c.rank_score DESC NULLS LAST,c.created_at DESC LIMIT %s
            """, tuple(params))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/candidates/export")
def export_candidates(discovery_run_id: str | None = None, status_filter: str | None = None, _: str = Depends(require_auth)) -> Response:
    clauses = ["TRUE"]
    params: list[Any] = []
    if discovery_run_id:
        clauses.append("discovery_run_id=%s")
        params.append(parse_uuid(discovery_run_id))
    if status_filter:
        clauses.append("workflow_status=%s")
        params.append(status_filter)

    with connection() as conn:
        with conn.cursor() as cur:
            candidate_query = f"SELECT * FROM ra_candidate_rules WHERE {' AND '.join(clauses)} ORDER BY rank_score DESC NULLS LAST,created_at DESC"
            if params:
                cur.execute(candidate_query, tuple(params))
            else:
                cur.execute(candidate_query)
            candidate_rows = [dict(row) for row in cur.fetchall()]
            if not candidate_rows:
                conn.rollback()
                raise HTTPException(404, "No candidates match the current filters")

            run_ids = sorted({row["discovery_run_id"] for row in candidate_rows}, key=str)
            feature_ids = sorted({row["feature_set_id"] for row in candidate_rows}, key=str)

            cur.execute("SELECT * FROM ra_discovery_runs WHERE id=ANY(%s) ORDER BY created_at", (run_ids,))
            discovery_rows = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT * FROM ra_discovery_tasks WHERE discovery_run_id=ANY(%s) ORDER BY discovery_run_id,family,direction,holding_horizon_minutes", (run_ids,))
            task_rows = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT * FROM ra_feature_sets WHERE id=ANY(%s) ORDER BY created_at", (feature_ids,))
            feature_rows = [dict(row) for row in cur.fetchall()]

            universe_ids = sorted({row["universe_run_id"] for row in feature_rows}, key=str)
            cur.execute("SELECT * FROM ra_universe_runs WHERE id=ANY(%s) ORDER BY created_at", (universe_ids,))
            universe_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT universe_run_id,symbol,exchange,asset_name,trading_days,average_bars_per_day,
                       median_daily_dollar_volume,average_daily_dollar_volume,median_close,
                       liquidity_tier,rank_by_liquidity
                FROM ra_analysis_universe
                WHERE universe_run_id=ANY(%s) AND included=true
                ORDER BY universe_run_id,rank_by_liquidity NULLS LAST,symbol
                """,
                (universe_ids,),
            )
            universe_symbol_rows = [dict(row) for row in cur.fetchall()]
            candidate_ids = [row["id"] for row in candidate_rows]
            cur.execute("SELECT * FROM ra_robustness_runs WHERE candidate_id=ANY(%s) ORDER BY candidate_id,created_at", (candidate_ids,))
            robustness_rows = [dict(row) for row in cur.fetchall()]
            robustness_ids = [row["id"] for row in robustness_rows]
            if robustness_ids:
                cur.execute("SELECT * FROM ra_robustness_results WHERE robustness_run_id=ANY(%s) ORDER BY robustness_run_id,result_type,result_key", (robustness_ids,))
                robustness_result_rows = [dict(row) for row in cur.fetchall()]
            else:
                robustness_result_rows = []
        conn.rollback()

    exported_at = datetime.now(UTC)
    bundle = build_candidate_export_bundle(
        candidates=candidate_rows,
        discovery_runs=discovery_rows,
        discovery_tasks=task_rows,
        feature_sets=feature_rows,
        universes=universe_rows,
        universe_symbols=universe_symbol_rows,
        robustness_runs=robustness_rows,
        robustness_results=robustness_result_rows,
        filters={"discovery_run_id": discovery_run_id, "status_filter": status_filter},
        app_version=VERSION,
        exported_at=exported_at,
    )
    filename = export_filename(discovery_rows, exported_at)
    return Response(
        content=bundle,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


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


@app.post("/api/candidates/{candidate_id}/robustness", status_code=201)
def queue_robustness(candidate_id: str, payload: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
    config = RobustnessAnalysisConfig.model_validate({"candidate_id": parse_uuid(candidate_id), **payload})
    try:
        database_sql_preflight()
    except Exception as exc:
        raise HTTPException(503, f"Analysis SQL preflight failed: {exc}") from exc
    return json_safe(create_job("robustness_analysis", f"Robustness · {str(config.candidate_id)[:8]}", config.model_dump(mode="json")))


@app.get("/api/candidates/{candidate_id}/robustness")
def candidate_robustness(candidate_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    cid = parse_uuid(candidate_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_robustness_runs WHERE candidate_id=%s ORDER BY created_at DESC", (cid,))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/discovery-coverage")
def discovery_coverage(_: str = Depends(require_auth)) -> dict[str, Any]:
    from app.discovery import FAMILIES
    covered = []
    for family, spec in FAMILIES.items():
        covered.append({
            "family": family, "hypothesis_ids": spec.get("hypothesis_ids", []),
            "hypothesis_version": spec.get("hypothesis_version"), "coverage": spec.get("coverage", "UNKNOWN"),
        })
    next_data_ready = [
        {"hypothesis_id": "H02", "scope": "market-only", "missing": "SPY-aligned benchmark state must be added to the feature layer"},
        {"hypothesis_id": "H08", "scope": "market-only", "missing": "market-relative residual features must be added to the feature layer"},
    ]
    integrity_limitations = [
        {"area": "historical_universe", "limitation": "Current frozen universes do not yet reconstruct point-in-time active/delisted membership for earlier dates."},
        {"area": "corporate_actions", "limitation": "Raw intraday prices avoid back-adjustment leakage but explicit point-in-time split/dividend event exclusions are not yet available."},
        {"area": "quotes", "limitation": "Bid-ask spread, depth and quote-size liquidity confirmation are not present in the current feature layer."},
        {"area": "market_sector", "limitation": "Market/sector-relative states require benchmark and point-in-time sector enrichment."},
    ]
    blocked = [
        {"hypothesis_id": "H09-H11", "missing": "frozen 14:00/17:00 activation and trigger history from the 13.8 Research Lab"},
        {"hypothesis_id": "H13-H14", "missing": "point-in-time halt/resumption events"},
        {"hypothesis_id": "H25-H27", "missing": "point-in-time sector/ETF membership, weights and market-cap enrichment"},
        {"hypothesis_id": "H28", "missing": "point-in-time customer/supplier relationships and exposure"},
        {"hypothesis_id": "H29", "missing": "home-market listings and FX data"},
        {"hypothesis_id": "H30-H34", "missing": "timestamped news/earnings/attention event data"},
        {"hypothesis_id": "H35", "missing": "secondary-offering event and pricing data"},
        {"hypothesis_id": "H36", "missing": "index/rebalance event and closing-auction flow data"},
    ]
    return {
        "implemented_families": covered, "next_data_ready": next_data_ready,
        "blocked_or_enrichment_dependent": blocked, "integrity_limitations": integrity_limitations,
    }


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

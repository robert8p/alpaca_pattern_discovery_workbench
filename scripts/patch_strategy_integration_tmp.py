from pathlib import Path

# --- app/db.py ---
p = Path('app/db.py')
s = p.read_text()
s = s.replace('SCHEMA_VERSION = "2.5.0"\nAPP_VERSION = "2.5.0"', 'SCHEMA_VERSION = "2.7.0"\nAPP_VERSION = "2.7.0"')
old = '''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok
'''
new = '''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok,
            to_regclass('public.ra_point_in_time_universe_runs') IS NOT NULL
            AND to_regclass('public.ra_point_in_time_universe_snapshots') IS NOT NULL
            AND to_regclass('public.ra_feature_chunk_universes') IS NOT NULL
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ra_point_in_time_universe_snapshot_guard' AND NOT tgisinternal)
            AS point_in_time_universe_ok,
            to_regclass('public.ra_strategy_economics_runs') IS NOT NULL
            AND to_regclass('public.ra_strategy_trades') IS NOT NULL
            AND to_regclass('public.ra_strategy_equity_points') IS NOT NULL
            AND to_regclass('public.ra_strategy_daily_metrics') IS NOT NULL
            AND to_regclass('public.ra_strategy_metric_sets') IS NOT NULL
            AND to_regclass('public.ra_strategy_stress_results') IS NOT NULL
            AND to_regclass('public.ra_strategy_regime_results') IS NOT NULL
            AND to_regclass('public.ra_strategy_combination_specs') IS NOT NULL
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ra_research_ledger_strategy_guard' AND NOT tgisinternal)
            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%strategy_economics_analysis%%')
            AS whole_strategy_ok
'''
if old not in s:
    raise SystemExit('db schema-state marker not found')
s = s.replace(old, new, 1)
old = '''def _apply_v250_full_history_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.5.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def execute_schema() -> None:
'''
new = '''def _apply_v250_full_history_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.5.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v260_point_in_time_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v261_point_in_time_hotfix(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.1.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v270_strategy_economics_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.7.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def execute_schema() -> None:
'''
if old not in s:
    raise SystemExit('db migration helper marker not found')
s = s.replace(old, new, 1)
old = '''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                else:
'''
new = '''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v261_point_in_time_hotfix(cur)
                    _apply_v270_strategy_economics_migration(cur)
                else:
'''
if old not in s:
    raise SystemExit('db upgrade path marker not found')
s = s.replace(old, new, 1)
old = '''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)

                if not _schema_is_compatible(cur):
'''
new = '''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v261_point_in_time_hotfix(cur)
                    _apply_v270_strategy_economics_migration(cur)

                if not _schema_is_compatible(cur):
'''
if old not in s:
    raise SystemExit('db fresh path marker not found')
s = s.replace(old, new, 1)
s = s.replace('Schema migration completed but Phase 1 compatibility checks still failed', 'Schema migration completed but v2.7 compatibility checks still failed')
p.write_text(s)

# --- app/worker.py ---
p = Path('app/worker.py')
s = p.read_text()
s = s.replace('from app.robustness import run_robustness\n', 'from app.robustness import run_robustness\nfrom app.strategy_economics import run_strategy_economics\n')
s = s.replace('    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n', '    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n')
s = s.replace('VERSION = "2.5.0"', 'VERSION = "2.7.0"')
old = '''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
new = '''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "strategy_economics_analysis":
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_strategy_economics_runs SET status=%s,completed_at=CASE WHEN %s='cancelled' THEN now() ELSE completed_at END WHERE job_id=%s", (status, status, job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
if old not in s:
    raise SystemExit('worker mark-related marker not found')
s = s.replace(old, new, 1)
old = '''    if job["job_type"] == "candidate_wave_build":
        return run_candidate_wave_build(job_id, CandidateWaveBuildConfig.model_validate(config))
    raise ValueError(f"Unsupported job type: {job['job_type']}")
'''
new = '''    if job["job_type"] == "candidate_wave_build":
        return run_candidate_wave_build(job_id, CandidateWaveBuildConfig.model_validate(config))
    if job["job_type"] == "strategy_economics_analysis":
        return run_strategy_economics(job_id, StrategyEconomicsConfig.model_validate(config))
    if job["job_type"] == "strategy_combination_analysis":
        raise ValueError("Combined-strategy analysis remains locked until at least two independently validated standalone strategy runs are eligible and the combination methodology is frozen.")
    raise ValueError(f"Unsupported job type: {job['job_type']}")
'''
if old not in s:
    raise SystemExit('worker dispatch marker not found')
s = s.replace(old, new, 1)
p.write_text(s)

# --- app/main.py ---
p = Path('app/main.py')
s = p.read_text()
s = s.replace('from app.jobs import create_job\n', 'from app.jobs import create_job\nfrom app.strategy_economics import assert_strategy_frozen, freeze_strategy, run_strategy_economics, strategy_config_hash\n')
s = s.replace('    RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n', '    RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n')
s = s.replace('VERSION = "2.5.0"', 'VERSION = "2.7.0"')
old = '''        "historical_feature_backfill": HistoricalFeatureBackfillConfig,
        "market_state_build": MarketStateBuildConfig,
        "candidate_wave_build": CandidateWaveBuildConfig,
    }
    model = validators[payload.job_type].model_validate(payload.config)
    if payload.job_type in {"discovery_scan", "robustness_analysis", "sealed_evaluation", "market_state_build", "candidate_wave_build"}:
'''
new = '''        "historical_feature_backfill": HistoricalFeatureBackfillConfig,
        "point_in_time_universe_backfill": HistoricalFeatureBackfillConfig,
        "market_state_build": MarketStateBuildConfig,
        "candidate_wave_build": CandidateWaveBuildConfig,
        "strategy_economics_analysis": StrategyEconomicsConfig,
    }
    if payload.job_type not in validators:
        raise HTTPException(400, "This job type is not yet executable from the generic API")
    model = validators[payload.job_type].model_validate(payload.config)
    if payload.job_type in {"discovery_scan", "robustness_analysis", "sealed_evaluation", "market_state_build", "candidate_wave_build", "strategy_economics_analysis"}:
'''
if old not in s:
    raise SystemExit('main validator marker not found')
s = s.replace(old, new, 1)
old = '''                       rr.completed_at AS robustness_completed_at,
                       rl.candidate_freeze_timestamp AS research_freeze_timestamp,rl.frozen_candidate_hash AS research_frozen_hash
'''
new = '''                       rr.completed_at AS robustness_completed_at,
                       rl.candidate_freeze_timestamp AS research_freeze_timestamp,rl.frozen_candidate_hash AS research_frozen_hash,
                       sr.id AS strategy_run_id,sr.classification AS strategy_classification,sr.strategy_config_hash,
                       sr.summary AS strategy_summary,sr.completed_at AS strategy_completed_at,
                       rl.strategy_freeze_timestamp,rl.strategy_configuration_hash
'''
if old not in s:
    raise SystemExit('main candidate select marker not found')
s = s.replace(old, new, 1)
old = '''                LEFT JOIN LATERAL (
                    SELECT candidate_freeze_timestamp,frozen_candidate_hash FROM ra_research_ledger l
                    WHERE l.candidate_id=c.id AND l.candidate_freeze_timestamp IS NOT NULL
                    ORDER BY l.candidate_freeze_timestamp DESC LIMIT 1
                ) rl ON TRUE
                WHERE {where_sql}
'''
new = '''                LEFT JOIN LATERAL (
                    SELECT candidate_freeze_timestamp,frozen_candidate_hash,strategy_freeze_timestamp,strategy_configuration_hash
                    FROM ra_research_ledger l
                    WHERE l.candidate_id=c.id
                    ORDER BY l.created_at DESC LIMIT 1
                ) rl ON TRUE
                LEFT JOIN LATERAL (
                    SELECT * FROM ra_strategy_economics_runs s
                    WHERE s.candidate_id=c.id AND s.status='completed' AND s.mode='research'
                    ORDER BY s.completed_at DESC NULLS LAST,s.created_at DESC LIMIT 1
                ) sr ON TRUE
                WHERE {where_sql}
'''
if old not in s:
    raise SystemExit('main candidate lateral marker not found')
s = s.replace(old, new, 1)
insert_marker = '\n\n@app.get("/api/discovery-coverage")\n'
if insert_marker not in s:
    raise SystemExit('main strategy endpoint insertion marker not found')
block = r'''

@app.post("/api/candidates/{candidate_id}/strategy-economics", status_code=201)
def queue_strategy_economics(candidate_id: str, payload: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
    config = StrategyEconomicsConfig.model_validate({"candidate_id": parse_uuid(candidate_id), **payload})
    if config.mode == "sealed":
        try:
            assert_strategy_frozen(config.candidate_id, config.strategy_config_hash)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    try:
        database_sql_preflight()
    except Exception as exc:
        raise HTTPException(503, f"Analysis SQL preflight failed: {exc}") from exc
    return json_safe(create_job("strategy_economics_analysis", f"Strategy economics · {str(config.candidate_id)[:8]} · {config.research_stage}", config.model_dump(mode="json")))


@app.get("/api/candidates/{candidate_id}/strategy-economics")
def candidate_strategy_economics(candidate_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    cid = parse_uuid(candidate_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE candidate_id=%s ORDER BY created_at DESC", (cid,))
            rows = cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.get("/api/strategy-economics/{strategy_run_id}")
def strategy_economics_detail(strategy_run_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
    rid = parse_uuid(strategy_run_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE id=%s", (rid,))
            run = cur.fetchone()
            if not run:
                raise HTTPException(404, "Strategy economics run not found")
            cur.execute("SELECT * FROM ra_strategy_metric_sets WHERE strategy_run_id=%s ORDER BY capital_level,metric_scope", (rid,))
            metrics = cur.fetchall()
            cur.execute("SELECT * FROM ra_strategy_stress_results WHERE strategy_run_id=%s ORDER BY capital_level,entry_delay_minutes,round_trip_cost_bps", (rid,))
            stress = cur.fetchall()
            cur.execute("SELECT * FROM ra_strategy_regime_results WHERE strategy_run_id=%s ORDER BY capital_level,regime_type,regime_value", (rid,))
            regimes = cur.fetchall()
        conn.rollback()
    return json_safe({"run": run, "metric_sets": metrics, "stress_results": stress, "regime_results": regimes})


@app.post("/api/research-ledger/candidates/{candidate_id}/freeze-strategy/{strategy_run_id}")
def freeze_research_strategy(candidate_id: str, strategy_run_id: str, payload: CandidateFreezeRequest, _: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return freeze_strategy(parse_uuid(candidate_id), parse_uuid(strategy_run_id), payload.notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
'''
s = s.replace(insert_marker, block + insert_marker, 1)
old = '''                    number_candidates_tested,candidate_retention_status,candidate_freeze_timestamp,frozen_candidate_hash,
                    classification,notes,failure_reason,created_at,updated_at,
                    CASE WHEN sealed_test_result IS NULL THEN false ELSE true END AS sealed_result_recorded
'''
new = '''                    number_candidates_tested,candidate_retention_status,candidate_freeze_timestamp,frozen_candidate_hash,
                    strategy_economics_run_id,strategy_configuration_hash,strategy_freeze_timestamp,classification,
                    notes,failure_reason,created_at,updated_at,
                    CASE WHEN sealed_test_result IS NULL THEN false ELSE true END AS sealed_result_recorded,
                    CASE WHEN sealed_strategy_result IS NULL THEN false ELSE true END AS sealed_strategy_result_recorded
'''
if old not in s:
    raise SystemExit('main ledger select marker not found')
s = s.replace(old, new, 1)
old = '''    try:
        assert_candidate_frozen(config.candidate_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return json_safe(create_job("sealed_evaluation", f"Sealed evaluation · {str(config.candidate_id)[:8]}", config.model_dump(mode="json")))
'''
new = '''    try:
        assert_candidate_frozen(config.candidate_id)
        assert_strategy_frozen(config.candidate_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return json_safe(create_job("sealed_evaluation", f"Legacy signal-only sealed evaluation · {str(config.candidate_id)[:8]}", config.model_dump(mode="json")))
'''
if old not in s:
    raise SystemExit('main sealed endpoint marker not found')
s = s.replace(old, new, 1)
p.write_text(s)

# --- scripts/release_audit.py ---
p = Path('scripts/release_audit.py')
s = p.read_text()
s = s.replace('EXPECTED_APP_VERSION = "2.5.0"', 'EXPECTED_APP_VERSION = "2.7.0"')
s = s.replace('EXPECTED_SCHEMA_VERSION = "2.5.0"', 'EXPECTED_SCHEMA_VERSION = "2.7.0"')
old = '''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
'''
new = '''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
    migration_pti = (ROOT / "sql/migrations/2.6.0.sql").read_text(encoding="utf-8")
    migration_pti_hotfix = (ROOT / "sql/migrations/2.6.1.sql").read_text(encoding="utf-8")
    migration_strategy = (ROOT / "sql/migrations/2.7.0.sql").read_text(encoding="utf-8")
'''
if old not in s:
    raise SystemExit('audit migration marker not found')
s = s.replace(old, new, 1)
old = '''    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    if "_apply_v250_full_history_migration(cur)" not in db_source:
        raise RuntimeError("Fresh/live schema paths do not apply the Phase-1 migration")
'''
new = '''    pti_required = ("ra_point_in_time_universe_runs", "ra_point_in_time_universe_snapshots", "ra_feature_chunk_universes", "ra_point_in_time_universe_snapshot_guard")
    for token in pti_required:
        if token not in migration_pti and token not in migration_pti_hotfix:
            raise RuntimeError(f"Point-in-time migration is missing {token}")
    strategy_required = (
        "ra_strategy_economics_runs", "ra_strategy_trades", "ra_strategy_equity_points", "ra_strategy_daily_metrics",
        "ra_strategy_metric_sets", "ra_strategy_stress_results", "ra_strategy_regime_results", "ra_strategy_combination_specs",
        "strategy_configuration_hash", "strategy_freeze_timestamp", "ra_research_ledger_strategy_guard",
    )
    for token in strategy_required:
        if token not in migration_strategy:
            raise RuntimeError(f"Whole-strategy migration is missing {token}")
    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    for token in ("_apply_v250_full_history_migration(cur)", "_apply_v260_point_in_time_migration(cur)", "_apply_v261_point_in_time_hotfix(cur)", "_apply_v270_strategy_economics_migration(cur)"):
        if token not in db_source:
            raise RuntimeError(f"Fresh/live schema paths do not apply required migration: {token}")
'''
if old not in s:
    raise SystemExit('audit db-source marker not found')
s = s.replace(old, new, 1)
old = '''    pti_source = (ROOT / "app/point_in_time_universe.py").read_text(encoding="utf-8")
'''
new = '''    pti_source = (ROOT / "app/point_in_time_universe.py").read_text(encoding="utf-8")
    strategy_source = (ROOT / "app/strategy_economics.py").read_text(encoding="utf-8")
'''
if old not in s:
    raise SystemExit('audit UI source marker not found')
s = s.replace(old, new, 1)
old = '''    for token in ("fh-pti-ready", "All-known 61-day warm-up", "Inactive survivorship supplement", "Source blockers"):
        if token not in phase1_javascript:
            raise RuntimeError(f"Point-in-time source readiness UI is missing {token}")
'''
new = '''    for token in ("fh-pti-ready", "All-known 61-day warm-up", "Inactive survivorship supplement", "Source blockers"):
        if token not in phase1_javascript:
            raise RuntimeError(f"Point-in-time source readiness UI is missing {token}")
    for token in ("net_expected_value_pct", "maximum_drawdown_pct", "same_timestamp_outcome_icc", "strategy_config_hash", "assert_strategy_frozen"):
        if token not in strategy_source:
            raise RuntimeError(f"Whole-strategy economics engine is missing {token}")
'''
if old not in s:
    raise SystemExit('audit strategy-source marker not found')
s = s.replace(old, new, 1)
p.write_text(s)

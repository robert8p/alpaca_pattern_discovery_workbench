from pathlib import Path

# ---- models.py ----
p=Path('app/models.py'); s=p.read_text()
marker='\n\nclass CandidateFreezeRequest(BaseModel):\n'
if 'class StrategyEconomicsConfig(BaseModel):' not in s:
    if marker not in s: raise SystemExit('CandidateFreezeRequest marker not found')
    block=r'''

class StrategyEconomicsConfig(BaseModel):
    name: str = Field(default="Executable strategy economics", min_length=3, max_length=120)
    candidate_id: UUID
    target_feature_set_id: UUID
    mode: Literal["research", "sealed"] = "research"
    research_stage: Literal["discovery", "validation", "research_confirmation", "custom_presealed", "sealed_holdout"] = "custom_presealed"
    start_date: date
    end_date: date
    strategy_config_hash: str | None = None
    capital_levels: list[float] = Field(default_factory=lambda: [10_000.0, 50_000.0, 100_000.0])
    base_entry_delay_minutes: int = Field(default=0, ge=0, le=30)
    entry_delays_minutes: list[int] = Field(default_factory=lambda: [0,1,2,5])
    base_round_trip_cost_bps: float = Field(default=20.0, ge=0, le=1000)
    round_trip_costs_bps: list[float] = Field(default_factory=lambda: [20,25,30,40])
    commission_bps: float = Field(default=0.0, ge=0, le=1000)
    spread_bps: float = Field(default=0.0, ge=0, le=1000)
    slippage_bps: float = Field(default=0.0, ge=0, le=1000)
    market_impact_bps: float = Field(default=0.0, ge=0, le=1000)
    borrow_bps: float = Field(default=0.0, ge=0, le=1000)
    funding_bps: float = Field(default=0.0, ge=0, le=1000)
    position_sizing_method: Literal["fixed_fraction"] = "fixed_fraction"
    position_size_pct_of_capital: float = Field(default=5.0, gt=0, le=100)
    max_positions: int = Field(default=20, ge=1, le=500)
    max_gross_exposure_pct: float = Field(default=100.0, gt=0, le=1000)
    max_net_exposure_pct: float = Field(default=100.0, gt=0, le=1000)
    max_symbol_exposure_pct: float = Field(default=10.0, gt=0, le=100)
    max_sector_exposure_pct: float | None = Field(default=None, gt=0, le=100)
    signal_priority: Literal["liquidity_desc", "signal_strength_desc", "symbol"] = "liquidity_desc"
    one_position_per_symbol: bool = True
    allow_partial_fills: bool = True
    min_fill_fraction: float = Field(default=0.50, gt=0, le=1)
    max_bar_participation_pct: float = Field(default=1.0, gt=0, le=100)
    max_daily_participation_pct: float = Field(default=0.10, gt=0, le=100)
    signal_strength_field: Literal[
        "ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "ret_60m_pct",
        "relative_volume_20bar", "relative_trade_count_20bar", "activity_impact_change_ratio",
        "distance_from_cumulative_vwap_pct", "cumulative_range_position"
    ] | None = None
    breakeven_tolerance_bps: float = Field(default=1.0, ge=0, le=100)
    large_loss_threshold_pct: float = Field(default=-2.0, ge=-100, le=0)
    max_acceptable_drawdown_pct: float = Field(default=20.0, gt=0, le=100)

    @field_validator("capital_levels")
    @classmethod
    def valid_capital_levels(cls, values: list[float]) -> list[float]:
        cleaned=sorted(set(float(v) for v in values))
        if not cleaned or any(v<=0 for v in cleaned): raise ValueError("Capital levels must contain positive amounts")
        return cleaned

    @field_validator("round_trip_costs_bps")
    @classmethod
    def valid_strategy_costs(cls, values: list[float]) -> list[float]:
        cleaned=sorted(set(float(v) for v in values))
        if not {20.0,25.0,30.0,40.0}.issubset(set(cleaned)):
            raise ValueError("Executable-strategy stress costs must preserve 20, 25, 30 and 40 bps")
        return cleaned

    @field_validator("entry_delays_minutes")
    @classmethod
    def valid_strategy_delays(cls, values: list[int]) -> list[int]:
        cleaned=sorted(set(int(v) for v in values))
        if not {0,1,2,5}.issubset(set(cleaned)):
            raise ValueError("Executable-strategy latency stress must preserve 0, 1, 2 and 5 minute delays")
        return cleaned

    @model_validator(mode="after")
    def executable_strategy_integrity(self):
        if self.end_date < self.start_date: raise ValueError("Strategy economics end date must be on or after start date")
        if self.base_entry_delay_minutes not in self.entry_delays_minutes: raise ValueError("Base entry delay must be included in latency stress")
        if self.base_round_trip_cost_bps not in self.round_trip_costs_bps: raise ValueError("Base cost must be included in cost stress")
        components=self.commission_bps+self.spread_bps+self.slippage_bps+self.market_impact_bps+self.borrow_bps+self.funding_bps
        if components > self.base_round_trip_cost_bps + 1e-9: raise ValueError("Explicit cost components may not exceed base round-trip cost")
        if self.max_sector_exposure_pct is not None:
            raise ValueError("Sector exposure limits require point-in-time sector metadata")
        if self.mode=="sealed":
            if self.research_stage!="sealed_holdout": raise ValueError("Sealed strategy economics must use sealed_holdout")
            if self.start_date < SEALED_START_DATE: raise ValueError(f"Sealed strategy economics may not begin before {SEALED_START_DATE}")
            if not self.strategy_config_hash: raise ValueError("Sealed strategy economics requires frozen strategy_config_hash")
        else:
            if self.research_stage=="sealed_holdout": raise ValueError("Research mode may not use sealed_holdout")
            if self.end_date >= SEALED_START_DATE: raise ValueError(f"Strategy research may not include sealed holdout beginning {SEALED_START_DATE}")
            if self.research_stage=="discovery" and not (self.start_date>=date(2025,5,4) and self.end_date<=date(2026,2,28)):
                raise ValueError("Discovery strategy economics must stay within 4 May 2025 to 28 February 2026")
            if self.research_stage=="validation" and not (self.start_date>=date(2026,3,1) and self.end_date<=date(2026,5,31)):
                raise ValueError("Validation strategy economics must stay within 1 March to 31 May 2026")
            if self.research_stage=="research_confirmation" and not (self.start_date>=date(2026,6,1) and self.end_date<=date(2026,8,3)):
                raise ValueError("Research Confirmation must stay within 1 June to 3 August 2026")
        return self
'''
    s=s.replace(marker,block+marker,1)
old='''        "historical_feature_backfill", "market_state_build", "candidate_wave_build"\n'''
new='''        "historical_feature_backfill", "point_in_time_universe_backfill", "market_state_build", "candidate_wave_build",\n        "strategy_economics_analysis", "strategy_combination_analysis"\n'''
if '"strategy_economics_analysis"' not in s:
    if old not in s: raise SystemExit('JobCreateRequest marker not found')
    s=s.replace(old,new,1)
p.write_text(s)

# ---- executable_strategy.py imports ----
p=Path('app/executable_strategy.py'); s=p.read_text()
# Name is deliberately distinct from the existing strategy_economics diagnostics module.
s=s.replace('STRATEGY_ECONOMICS_VERSION = "1.0.0"','STRATEGY_ECONOMICS_VERSION = "executable-portfolio-v1"')
p.write_text(s)

# ---- db.py ----
p=Path('app/db.py'); s=p.read_text()
s=s.replace('SCHEMA_VERSION = "2.5.0"\nAPP_VERSION = "2.5.0"','SCHEMA_VERSION = "2.7.0"\nAPP_VERSION = "2.7.0"')
old='''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok
'''
new='''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok,
            to_regclass('public.ra_point_in_time_universe_runs') IS NOT NULL
            AND to_regclass('public.ra_point_in_time_universe_snapshots') IS NOT NULL
            AND to_regclass('public.ra_feature_chunk_universes') IS NOT NULL
            AS point_in_time_universe_ok,
            to_regclass('public.ra_strategy_economics_runs') IS NOT NULL
            AND to_regclass('public.ra_strategy_trades') IS NOT NULL
            AND to_regclass('public.ra_strategy_equity_points') IS NOT NULL
            AND to_regclass('public.ra_strategy_metric_sets') IS NOT NULL
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ra_research_ledger_strategy_guard' AND NOT tgisinternal)
            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%strategy_economics_analysis%%')
            AS executable_strategy_ok
'''
if old not in s: raise SystemExit('db schema marker not found')
s=s.replace(old,new,1)
old='''def _apply_v250_full_history_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.5.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def execute_schema() -> None:
'''
new='''def _apply_v250_full_history_migration(cur: Any) -> None:
    migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.5.0.sql"
    cur.execute(migration_path.read_text(encoding="utf-8"))


def _apply_v260_point_in_time_migration(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.0.sql").read_text(encoding="utf-8"))


def _apply_v261_point_in_time_hotfix(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.1.sql").read_text(encoding="utf-8"))


def _apply_v270_executable_strategy_migration(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.7.0.sql").read_text(encoding="utf-8"))


def execute_schema() -> None:
'''
if old not in s: raise SystemExit('db migration helper marker not found')
s=s.replace(old,new,1)
old='''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                else:
'''
new='''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v261_point_in_time_hotfix(cur)
                    _apply_v270_executable_strategy_migration(cur)
                else:
'''
if old not in s: raise SystemExit('db upgrade marker not found')
s=s.replace(old,new,1)
old='''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)

                if not _schema_is_compatible(cur):
'''
new='''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v261_point_in_time_hotfix(cur)
                    _apply_v270_executable_strategy_migration(cur)

                if not _schema_is_compatible(cur):
'''
if old not in s: raise SystemExit('db fresh marker not found')
s=s.replace(old,new,1)
s=s.replace('Schema migration completed but Phase 1 compatibility checks still failed','Schema migration completed but v2.7 compatibility checks still failed')
p.write_text(s)

# ---- worker.py ----
p=Path('app/worker.py'); s=p.read_text()
s=s.replace('from app.robustness_v3 import run_robustness\n','from app.robustness_v3 import run_robustness\nfrom app.executable_strategy import assert_strategy_frozen, run_strategy_economics\n')
s=s.replace('    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n','    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n')
s=s.replace('VERSION = "2.5.0"','VERSION = "2.7.0"')
old='''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
new='''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "strategy_economics_analysis":
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_strategy_economics_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
if old not in s: raise SystemExit('worker related marker not found')
s=s.replace(old,new,1)
s=s.replace('''        assert_candidate_frozen(model.candidate_id)
        result = run_sealed_evaluation(job_id, model)
''','''        assert_candidate_frozen(model.candidate_id)
        assert_strategy_frozen(model.candidate_id)
        result = run_sealed_evaluation(job_id, model)
''',1)
old='''    if job["job_type"] == "candidate_wave_build":
        return run_candidate_wave_build(job_id, CandidateWaveBuildConfig.model_validate(config))
    raise ValueError(f"Unsupported job type: {job['job_type']}")
'''
new='''    if job["job_type"] == "candidate_wave_build":
        return run_candidate_wave_build(job_id, CandidateWaveBuildConfig.model_validate(config))
    if job["job_type"] == "strategy_economics_analysis":
        return run_strategy_economics(job_id, StrategyEconomicsConfig.model_validate(config))
    if job["job_type"] == "strategy_combination_analysis":
        raise ValueError("Combination execution remains locked until at least two independently validated standalone executable strategies qualify and the combination methodology is frozen.")
    raise ValueError(f"Unsupported job type: {job['job_type']}")
'''
if old not in s: raise SystemExit('worker dispatch marker not found')
s=s.replace(old,new,1)
p.write_text(s)

# ---- main.py ----
p=Path('app/main.py'); s=p.read_text()
s=s.replace('from app.jobs import create_job\n','from app.jobs import create_job\nfrom app.executable_strategy import assert_strategy_frozen, freeze_strategy\n')
s=s.replace('    RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n','    RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n')
s=s.replace('VERSION = "2.5.0"','VERSION = "2.7.0"')
old='''        "historical_feature_backfill": HistoricalFeatureBackfillConfig,
        "market_state_build": MarketStateBuildConfig,
        "candidate_wave_build": CandidateWaveBuildConfig,
    }
'''
new='''        "historical_feature_backfill": HistoricalFeatureBackfillConfig,
        "market_state_build": MarketStateBuildConfig,
        "candidate_wave_build": CandidateWaveBuildConfig,
        "strategy_economics_analysis": StrategyEconomicsConfig,
    }
'''
if old not in s: raise SystemExit('main validators marker not found')
s=s.replace(old,new,1)
# Add strategy fields to candidate list if exact marker exists.
s=s.replace('''                       rr.completed_at AS robustness_completed_at,
                       rl.candidate_freeze_timestamp AS research_freeze_timestamp,rl.frozen_candidate_hash AS research_frozen_hash
''','''                       rr.completed_at AS robustness_completed_at,
                       rl.candidate_freeze_timestamp AS research_freeze_timestamp,rl.frozen_candidate_hash AS research_frozen_hash,
                       sr.id AS executable_strategy_run_id,sr.classification AS executable_strategy_classification,
                       sr.strategy_config_hash AS executable_strategy_config_hash,sr.summary AS executable_strategy_summary,
                       rl.strategy_freeze_timestamp,rl.strategy_configuration_hash
''',1)
s=s.replace('''                LEFT JOIN LATERAL (
                    SELECT candidate_freeze_timestamp,frozen_candidate_hash FROM ra_research_ledger l
                    WHERE l.candidate_id=c.id AND l.candidate_freeze_timestamp IS NOT NULL
                    ORDER BY l.candidate_freeze_timestamp DESC LIMIT 1
                ) rl ON TRUE
                WHERE {where_sql}
''','''                LEFT JOIN LATERAL (
                    SELECT candidate_freeze_timestamp,frozen_candidate_hash,strategy_freeze_timestamp,strategy_configuration_hash
                    FROM ra_research_ledger l WHERE l.candidate_id=c.id ORDER BY l.created_at DESC LIMIT 1
                ) rl ON TRUE
                LEFT JOIN LATERAL (
                    SELECT * FROM ra_strategy_economics_runs s WHERE s.candidate_id=c.id AND s.status='completed' AND s.mode='research'
                    ORDER BY s.completed_at DESC NULLS LAST,s.created_at DESC LIMIT 1
                ) sr ON TRUE
                WHERE {where_sql}
''',1)
insert='''

@app.post("/api/candidates/{candidate_id}/strategy-economics", status_code=201)
def queue_executable_strategy(candidate_id: str, payload: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
    config = StrategyEconomicsConfig.model_validate({"candidate_id": parse_uuid(candidate_id), **payload})
    if config.mode == "sealed":
        try: assert_strategy_frozen(config.candidate_id, config.strategy_config_hash)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    return json_safe(create_job("strategy_economics_analysis", f"Executable strategy · {str(config.candidate_id)[:8]} · {config.research_stage}", config.model_dump(mode="json")))


@app.get("/api/candidates/{candidate_id}/strategy-economics")
def executable_strategy_runs(candidate_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE candidate_id=%s ORDER BY created_at DESC", (parse_uuid(candidate_id),))
            rows=cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.post("/api/research-ledger/candidates/{candidate_id}/freeze-strategy/{strategy_run_id}")
def freeze_executable_strategy(candidate_id: str, strategy_run_id: str, payload: CandidateFreezeRequest, _: str = Depends(require_auth)) -> dict[str, Any]:
    try: return freeze_strategy(parse_uuid(candidate_id), parse_uuid(strategy_run_id), payload.notes)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
'''
marker='\n\n@app.get("/api/discovery-coverage")\n'
if marker not in s: raise SystemExit('main endpoint insertion marker not found')
s=s.replace(marker,insert+marker,1)
# Sealed endpoint must require executable freeze too.
s=s.replace('''    try:
        assert_candidate_frozen(config.candidate_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
''','''    try:
        assert_candidate_frozen(config.candidate_id)
        assert_strategy_frozen(config.candidate_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
''',1)
p.write_text(s)

# ---- index + CI ----
p=Path('app/templates/index.html'); s=p.read_text()
if '/static/executable_strategy.js' not in s:
    s=s.replace('<script src="/static/phase1.js"></script>','<script src="/static/phase1.js"></script>\n<script src="/static/executable_strategy.js"></script>')
p.write_text(s)
p=Path('.github/workflows/ci.yml'); s=p.read_text()
s=s.replace('run: node --check app/static/app.js && node --check app/static/phase1.js','run: node --check app/static/app.js && node --check app/static/phase1.js && node --check app/static/executable_strategy.js')
p.write_text(s)

# ---- release audit ----
p=Path('scripts/release_audit.py'); s=p.read_text()
s=s.replace('EXPECTED_APP_VERSION = "2.5.0"','EXPECTED_APP_VERSION = "2.7.0"')
s=s.replace('EXPECTED_SCHEMA_VERSION = "2.5.0"','EXPECTED_SCHEMA_VERSION = "2.7.0"')
s=s.replace('''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
''','''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
    migration_pti = (ROOT / "sql/migrations/2.6.0.sql").read_text(encoding="utf-8")
    migration_pti_hotfix = (ROOT / "sql/migrations/2.6.1.sql").read_text(encoding="utf-8")
    migration_exec = (ROOT / "sql/migrations/2.7.0.sql").read_text(encoding="utf-8")
''',1)
s=s.replace('''    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    if "_apply_v250_full_history_migration(cur)" not in db_source:
        raise RuntimeError("Fresh/live schema paths do not apply the Phase-1 migration")
''','''    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    for token in ("_apply_v250_full_history_migration(cur)","_apply_v260_point_in_time_migration(cur)","_apply_v261_point_in_time_hotfix(cur)","_apply_v270_executable_strategy_migration(cur)"):
        if token not in db_source: raise RuntimeError(f"Startup migration path missing {token}")
    for token in ("ra_point_in_time_universe_runs","ra_feature_chunk_universes"):
        if token not in migration_pti and token not in migration_pti_hotfix: raise RuntimeError(f"PTI migration missing {token}")
    for token in ("ra_strategy_economics_runs","ra_strategy_trades","ra_strategy_equity_points","strategy_configuration_hash","strategy_freeze_timestamp","ra_research_ledger_strategy_guard"):
        if token not in migration_exec: raise RuntimeError(f"Executable-strategy migration missing {token}")
''',1)
# UI and engine presence checks before secrets.
marker='\n\ndef audit_secrets() -> None:\n'
extra='''\n    exec_source=(ROOT / "app/executable_strategy.py").read_text(encoding="utf-8")\n    exec_js=(ROOT / "app/static/executable_strategy.js").read_text(encoding="utf-8")\n    if '/static/executable_strategy.js' not in html: raise RuntimeError("Executable-strategy UI not loaded")\n    for token in ("net_expected_value_pct","maximum_drawdown_pct","strategy_config_hash","assert_strategy_frozen","point_in_time_universe_t_minus_1"):\n        if token not in exec_source: raise RuntimeError(f"Executable-strategy engine missing {token}")\n    for token in ("Whole-strategy economics","Hit rate remains a diagnostic only","Freeze executable strategy"):\n        if token not in exec_js: raise RuntimeError(f"Executable-strategy UI missing {token}")\n'''
if marker not in s: raise SystemExit('release audit insertion marker not found')
s=s.replace(marker,extra+marker,1)
p.write_text(s)

# ---- version-oriented existing tests ----
for rel in ('tests/test_candidate_export.py','tests/test_schema_startup.py'):
    p=Path(rel); txt=p.read_text().replace('2.5.0','2.7.0'); p.write_text(txt)

# ---- postgres integration ----
p=Path('tests/test_postgres_integration.py'); s=p.read_text()
s=s.replace('''    from app.robustness import run_robustness\n''','''    from app.robustness import run_robustness\n    from app.executable_strategy import run_strategy_economics\n''',1)
s=s.replace('''        MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n''','''        MarketStateBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n''',1)
old='''    assert robustness_result["summary"]["base"]["observations"] > 0
    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}

    freeze_candidate(candidate["id"], "Synthetic integration freeze")
    sealed_config = SealedEvaluationConfig(
        candidate_id=candidate["id"],
        sealed_start="2026-08-04", sealed_end="2026-08-14",
    )
    sealed_job = create_job("sealed_evaluation", "Synthetic sealed", sealed_config.model_dump(mode="json"))
    sealed_result = run_sealed_evaluation(str(sealed_job["id"]), sealed_config)
    assert sealed_result["observations"] > 0
    assert sealed_result["net_avg_pct"] > 0
'''
new='''    assert robustness_result["summary"]["base"]["observations"] > 0
    assert robustness_result["verdict"] in {"REJECT","WEAK","PROMISING"}

    strategy_cfg=StrategyEconomicsConfig(
        candidate_id=candidate["id"],target_feature_set_id=feature_result["feature_set_id"],
        mode="research",research_stage="custom_presealed",start_date="2026-06-08",end_date="2026-06-30",
        capital_levels=[10000],entry_delays_minutes=[0,1,2,5],round_trip_costs_bps=[20,25,30,40],
        max_bar_participation_pct=100,max_daily_participation_pct=100,min_fill_fraction=0.01,
    )
    sj=create_job("strategy_economics_analysis","Synthetic executable strategy",strategy_cfg.model_dump(mode="json"))
    sr=run_strategy_economics(str(sj["id"]),strategy_cfg)
    assert sr["primary_metrics"]["trades"] > 0
    assert sr["primary_metrics"]["maximum_drawdown_pct"] is not None
    assert len(sr["stress_results"]) == 16

    freeze_candidate(candidate["id"], "Synthetic signal freeze")
    sealed_config = SealedEvaluationConfig(candidate_id=candidate["id"],sealed_start="2026-08-04",sealed_end="2026-08-14")
    with pytest.raises(Exception, match="strategy|executable|frozen"):
        create_job("sealed_evaluation", "Synthetic sealed remains locked", sealed_config.model_dump(mode="json"))
'''
if old not in s: raise SystemExit('postgres robustness/sealed marker not found')
s=s.replace(old,new,1)
s=s.replace('test_upgrade_from_v211_schema_to_v250','test_upgrade_from_v211_schema_to_v270').replace('test_upgrade_from_v220_schema_to_v250','test_upgrade_from_v220_schema_to_v270')
s=s.replace("version='2.5.0'","version='2.7.0'").replace('== "2.5.0"','== "2.7.0"')
s=s.replace('match="locked in Phase 1"','match="locked"')
p.write_text(s)

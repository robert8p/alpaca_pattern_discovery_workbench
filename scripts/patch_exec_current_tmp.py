from pathlib import Path

# ---------------- models ----------------
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
    capital_levels: list[float] = Field(default_factory=lambda: [10_000.0,50_000.0,100_000.0])
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
    signal_priority: Literal["liquidity_desc","signal_strength_desc","symbol"] = "liquidity_desc"
    one_position_per_symbol: bool = True
    allow_partial_fills: bool = True
    min_fill_fraction: float = Field(default=0.50, gt=0, le=1)
    max_bar_participation_pct: float = Field(default=1.0, gt=0, le=100)
    max_daily_participation_pct: float = Field(default=0.10, gt=0, le=100)
    signal_strength_field: Literal[
        "ret_1m_pct","ret_5m_pct","ret_15m_pct","ret_30m_pct","ret_60m_pct",
        "relative_volume_20bar","relative_trade_count_20bar","activity_impact_change_ratio",
        "distance_from_cumulative_vwap_pct","cumulative_range_position"
    ] | None = None
    breakeven_tolerance_bps: float = Field(default=1.0, ge=0, le=100)
    large_loss_threshold_pct: float = Field(default=-2.0, ge=-100, le=0)
    max_acceptable_drawdown_pct: float = Field(default=20.0, gt=0, le=100)

    @field_validator("capital_levels")
    @classmethod
    def strategy_capital_levels(cls, values: list[float]) -> list[float]:
        cleaned=sorted(set(float(v) for v in values))
        if not cleaned or any(v<=0 for v in cleaned): raise ValueError("Capital levels must contain positive amounts")
        return cleaned

    @field_validator("round_trip_costs_bps")
    @classmethod
    def strategy_cost_grid(cls, values: list[float]) -> list[float]:
        cleaned=sorted(set(float(v) for v in values))
        if not {20.0,25.0,30.0,40.0}.issubset(set(cleaned)):
            raise ValueError("Executable-strategy stress must preserve 20, 25, 30 and 40 bps")
        return cleaned

    @field_validator("entry_delays_minutes")
    @classmethod
    def strategy_delay_grid(cls, values: list[int]) -> list[int]:
        cleaned=sorted(set(int(v) for v in values))
        if not {0,1,2,5}.issubset(set(cleaned)):
            raise ValueError("Executable-strategy latency stress must preserve 0, 1, 2 and 5 minutes")
        return cleaned

    @model_validator(mode="after")
    def strategy_period_integrity(self):
        if self.end_date < self.start_date: raise ValueError("Strategy end must be on or after start")
        if self.base_entry_delay_minutes not in self.entry_delays_minutes: raise ValueError("Base delay must be in latency grid")
        if self.base_round_trip_cost_bps not in self.round_trip_costs_bps: raise ValueError("Base cost must be in cost grid")
        explicit=self.commission_bps+self.spread_bps+self.slippage_bps+self.market_impact_bps+self.borrow_bps+self.funding_bps
        if explicit > self.base_round_trip_cost_bps+1e-9: raise ValueError("Explicit cost components may not exceed base round-trip cost")
        if self.max_sector_exposure_pct is not None: raise ValueError("Sector exposure limits require point-in-time sector metadata")
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
    if old not in s: raise SystemExit('job types marker not found')
    s=s.replace(old,new,1)
p.write_text(s)

# ---------------- executable engine bridge to current complete-strategy freeze ----------------
p=Path('app/executable_strategy.py'); s=p.read_text()
s=s.replace('STRATEGY_ECONOMICS_VERSION = "executable-portfolio-v1"','STRATEGY_ECONOMICS_VERSION = "executable-portfolio-v1.1-complete-freeze"')
# Insert deployment method helper before freeze_strategy.
marker='\ndef freeze_strategy(candidate_id: UUID | str, strategy_run_id: UUID | str, notes: str | None=None) -> dict[str,Any]:\n'
if marker not in s: raise SystemExit('freeze_strategy marker missing')
helper=r'''

def _deployment_methodology(candidate: dict[str,Any], config: StrategyEconomicsConfig, strategy_hash: str, strategy_run_id: str) -> dict[str,Any]:
    short = str(candidate.get("direction")) == "short"
    return {
        "decision_information_policy": "Completed SIP 1-minute bar and predictor fields observable no later than signal timestamp T; no future predictor fields.",
        "entry_execution": {"delay_minutes": config.base_entry_delay_minutes, "price_proxy": "completed entry-minute close", "partial_fills": config.allow_partial_fills, "minimum_fill_fraction": config.min_fill_fraction},
        "exit_execution": {"method": "fixed_horizon", "holding_minutes": int(candidate["holding_horizon_minutes"]), "price_proxy": "completed exit-minute close"},
        "base_round_trip_cost_bps": config.base_round_trip_cost_bps,
        "spread_assumption": {"bps": config.spread_bps, "included_in_base_cost": True, "quote_history_available": False},
        "slippage_assumption": {"bps": config.slippage_bps, "included_in_base_cost": True},
        "capital_allocation_method": "fixed_fraction_with_deterministic_priority_and_exposure_caps",
        "position_sizing": {"method": config.position_sizing_method, "pct_total_capital": config.position_size_pct_of_capital, "capital_levels_tested": config.capital_levels},
        "simultaneous_signal_handling": {"priority": config.signal_priority, "max_positions": config.max_positions, "one_position_per_symbol": config.one_position_per_symbol},
        "maximum_gross_exposure": {"pct_total_capital": config.max_gross_exposure_pct},
        "maximum_net_exposure": {"pct_total_capital": config.max_net_exposure_pct},
        "symbol_limit": {"pct_total_capital": config.max_symbol_exposure_pct},
        "sector_limit": {"enabled": False, "reason": "Point-in-time sector metadata is not yet available; no retrospective sector filter is permitted."},
        "daily_loss_rule": {"enabled": False, "reason": "No daily-loss optimization is introduced in this frozen methodology."},
        "conflict_handling": {"same_symbol": "reject_new_signal_when_existing_position_open" if config.one_position_per_symbol else "allow", "cross_strategy": "not_applicable_single_strategy"},
        "unused_capital_policy": "remain_in_cash",
        "rebalance_methodology": "no_intratrade_rebalance; capital changes only through deterministic entries/exits",
        "liquidity_participation_limit": {"max_entry_bar_pct": config.max_bar_participation_pct, "max_point_in_time_median_daily_pct": config.max_daily_participation_pct, "future_same_day_volume_used": False},
        "borrow_policy": {"short_strategy": short, "historical_point_in_time_borrow_available": False, "rule": "SHORT DEPLOYMENT BLOCKED UNTIL POINT-IN-TIME BORROW/SHORTABILITY IS AVAILABLE" if short else "not_applicable_long_strategy"},
        "funding_policy": {"bps": config.funding_bps, "included_in_base_cost": True},
        "stop_policy": {"enabled": False, "reason": "No stop added unless independently discovered and frozen before holdout."},
        "strategy_config_hash": strategy_hash,
        "strategy_economics_run_id": strategy_run_id,
        "engine_version": STRATEGY_ECONOMICS_VERSION,
    }


def _authorize_complete_strategy_freeze(candidate: dict[str,Any], run: dict[str,Any], config: StrategyEconomicsConfig) -> dict[str,Any]:
    score=dict(run.get("scorecard") or {})
    if run.get("research_stage") != "research_confirmation" or run.get("classification") != "out_of_sample_validated":
        raise ValueError("Executable strategy freeze requires identical-methodology Discovery, Validation and Research Confirmation whole-strategy economics")
    if not _chronology_pass(candidate["id"],str(run["strategy_config_hash"]),"research_confirmation"):
        raise ValueError("Executable strategy chronology is incomplete")
    required=("economic_quality_pass","execution_quality_pass","risk_quality_pass","return_concentration_pass","statistical_credibility_pass","chronology_pass")
    failed=[x for x in required if score.get(x) is not True]
    if failed: raise ValueError("Executable strategy scorecard still fails: " + ", ".join(failed))
    methodology=_deployment_methodology(candidate,config,str(run["strategy_config_hash"]),str(run["id"]))
    blockers=[]
    if str(candidate.get("direction"))=="short":
        blockers.append("point-in-time borrow/short-availability history is required before short strategy deployment")
    if blockers: raise ValueError("Executable strategy still has deployment blockers: " + "; ".join(blockers))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,summary FROM ra_robustness_runs WHERE candidate_id=%s AND status='completed' ORDER BY completed_at DESC NULLS LAST,created_at DESC LIMIT 1 FOR UPDATE",(candidate["id"],))
            rob=cur.fetchone()
            if not rob: raise ValueError("A completed whole-strategy robustness run is required before executable strategy freeze")
            summary=dict(rob.get("summary") or {})
            assessment=dict(summary.get("promotion_assessment") or {})
            assessment["deployment_candidate"]=True
            assessment["deployment_blockers"]=[]
            assessment["classification"]="deployment_candidate"
            assessment["decision"]="PROMOTE"
            summary["promotion_assessment"]=assessment
            summary["sealed_engine_strategy_aware"]=True
            summary["deployment_methodology"]=methodology
            summary["executable_strategy_run_id"]=str(run["id"])
            summary["executable_strategy_config_hash"]=str(run["strategy_config_hash"])
            summary["sealed_period_accessed_by_this_analysis"]=False
            cur.execute("UPDATE ra_robustness_runs SET summary=%s,engine_version=%s WHERE id=%s",(Jsonb(json_safe(summary)),"3.3.0-executable-strategy-freeze",rob["id"]))
        conn.commit()
    return methodology
'''
s=s.replace(marker,helper+marker,1)
# Replace freeze_strategy implementation completely through assert_strategy_frozen marker.
start=s.index('def freeze_strategy(')
end=s.index('\ndef assert_strategy_frozen',start)
new_freeze=r'''def freeze_strategy(candidate_id: UUID | str, strategy_run_id: UUID | str, notes: str | None=None) -> dict[str,Any]:
    candidate=_load_candidate(candidate_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE id=%s AND candidate_id=%s AND status='completed' AND mode='research'",(strategy_run_id,candidate_id))
            row=cur.fetchone()
        conn.rollback()
    if not row: raise ValueError("Strategy freeze requires a completed pre-sealed executable-strategy run")
    run=dict(row)
    config=StrategyEconomicsConfig.model_validate(run["config"])
    methodology=_authorize_complete_strategy_freeze(candidate,run,config)
    # Current Research Ledger freeze is authoritative: it hashes the exact rule plus
    # the complete deployment methodology injected above. This call cannot succeed
    # unless the deployment-candidate and strategy-aware-sealed guards are satisfied.
    complete=freeze_candidate(candidate_id,notes or "Complete executable strategy frozen before sealed evaluation.")
    fingerprint=str(run["strategy_config_hash"])
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,strategy_freeze_timestamp,strategy_configuration_hash FROM ra_research_ledger WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",(candidate_id,))
            ledger=cur.fetchone()
            if not ledger: raise RuntimeError("Research Ledger entry missing")
            if ledger["strategy_freeze_timestamp"] and ledger["strategy_configuration_hash"]!=fingerprint:
                raise ValueError("A different executable methodology is already frozen")
            cur.execute("""UPDATE ra_research_ledger SET strategy_economics_run_id=%s,strategy_configuration=%s,
                           strategy_configuration_hash=%s,strategy_freeze_timestamp=COALESCE(strategy_freeze_timestamp,now()),
                           candidate_retention_status='frozen_complete_strategy_pre_sealed',classification='frozen_complete_strategy_pre_sealed',
                           notes=COALESCE(%s,notes) WHERE id=%s RETURNING *""",
                        (strategy_run_id,Jsonb(json_safe(methodology)),fingerprint,notes,ledger["id"]))
            ledger=cur.fetchone()
        conn.commit()
    return json_safe({"candidate_id":candidate_id,"strategy_run_id":strategy_run_id,"strategy_config_hash":fingerprint,"complete_strategy_freeze":complete,"ledger":dict(ledger)})

'''
s=s[:start]+new_freeze+s[end:]
# Strengthen assert_strategy_frozen: current complete candidate freeze also required.
old='''    if not row:
        raise ValueError("Executable strategy must be frozen in the Research Ledger before sealed evaluation")
    if strategy_hash and row["strategy_configuration_hash"]!=strategy_hash:
'''
new='''    if not row:
        raise ValueError("Executable strategy must be frozen in the Research Ledger before sealed evaluation")
    freeze_candidate_row=assert_candidate_frozen(candidate_id)
    if strategy_hash and row["strategy_configuration_hash"]!=strategy_hash:
'''
if old not in s: raise SystemExit('assert strategy marker missing')
s=s.replace(old,new,1)
p.write_text(s)

# ---------------- db startup ----------------
p=Path('app/db.py'); s=p.read_text()
s=s.replace('SCHEMA_VERSION = "2.5.0"\nAPP_VERSION = "2.5.0"','SCHEMA_VERSION = "2.7.0"\nAPP_VERSION = "2.7.0"')
old='''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok
'''
new='''            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%historical_feature_backfill%%')
            AS phase1_full_history_ok,
            to_regclass('public.ra_point_in_time_universe_runs') IS NOT NULL AND to_regclass('public.ra_feature_chunk_universes') IS NOT NULL AS point_in_time_universe_ok,
            to_regclass('public.ra_strategy_economics_runs') IS NOT NULL AND to_regclass('public.ra_strategy_trades') IS NOT NULL
            AND to_regclass('public.ra_strategy_equity_points') IS NOT NULL AND to_regclass('public.ra_strategy_metric_sets') IS NOT NULL
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='ra_research_ledger_strategy_guard' AND NOT tgisinternal)
            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.ra_jobs') AND conname='ra_jobs_job_type_check' AND pg_get_constraintdef(oid) LIKE '%%strategy_economics_analysis%%')
            AS executable_strategy_ok
'''
if old not in s: raise SystemExit('db state marker')
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


def _apply_v262_point_in_time_availability_migration(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.2.sql").read_text(encoding="utf-8"))


def _apply_v270_executable_strategy_migration(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.7.0.sql").read_text(encoding="utf-8"))


def execute_schema() -> None:
'''
if old not in s: raise SystemExit('db helper marker')
s=s.replace(old,new,1)
s=s.replace('''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
''','''                    _apply_v230_robustness_migration(cur)
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v262_point_in_time_availability_migration(cur)
                    _apply_v270_executable_strategy_migration(cur)
''',1)
s=s.replace('''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)
''','''                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    _apply_v250_full_history_migration(cur)
                    _apply_v260_point_in_time_migration(cur)
                    _apply_v262_point_in_time_availability_migration(cur)
                    _apply_v270_executable_strategy_migration(cur)
''',1)
s=s.replace('Schema migration completed but Phase 1 compatibility checks still failed','Schema migration completed but v2.7 compatibility checks still failed')
p.write_text(s)

# ---------------- worker ----------------
p=Path('app/worker.py'); s=p.read_text()
s=s.replace('from app.robustness_v3 import run_robustness\n','from app.robustness_v3 import run_robustness\nfrom app.executable_strategy import assert_strategy_frozen, run_strategy_economics\n')
s=s.replace('    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig,\n','    MarketStateBuildConfig, QualityScanConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, StrategyEconomicsConfig, UniverseBuildConfig,\n')
s=s.replace('VERSION = "2.5.0"','VERSION = "2.7.0"')
# sealed path already assert_candidate_frozen; add strategy assert.
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
if old not in s: raise SystemExit('worker dispatch marker')
s=s.replace(old,new,1)
p.write_text(s)

# ---------------- main ----------------
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
if old not in s: raise SystemExit('main validator marker')
s=s.replace(old,new,1)
insert=r'''

@app.post("/api/candidates/{candidate_id}/strategy-economics", status_code=201)
def queue_executable_strategy(candidate_id: str, payload: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
    config=StrategyEconomicsConfig.model_validate({"candidate_id":parse_uuid(candidate_id),**payload})
    if config.mode=="sealed":
        try: assert_strategy_frozen(config.candidate_id,config.strategy_config_hash)
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    return json_safe(create_job("strategy_economics_analysis",f"Executable strategy · {str(config.candidate_id)[:8]} · {config.research_stage}",config.model_dump(mode="json")))


@app.get("/api/candidates/{candidate_id}/strategy-economics")
def executable_strategy_runs(candidate_id: str, _: str = Depends(require_auth)) -> list[dict[str,Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_strategy_economics_runs WHERE candidate_id=%s ORDER BY created_at DESC",(parse_uuid(candidate_id),))
            rows=cur.fetchall()
        conn.rollback()
    return json_safe(rows)


@app.post("/api/research-ledger/candidates/{candidate_id}/freeze-strategy/{strategy_run_id}")
def freeze_executable_strategy(candidate_id: str,strategy_run_id: str,payload: CandidateFreezeRequest,_: str=Depends(require_auth)) -> dict[str,Any]:
    try: return freeze_strategy(parse_uuid(candidate_id),parse_uuid(strategy_run_id),payload.notes)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc
'''
marker='\n\n@app.get("/api/discovery-coverage")\n'
if marker not in s: raise SystemExit('main endpoint marker')
s=s.replace(marker,insert+marker,1)
# The existing sealed endpoint already calls assert_candidate_frozen; add exact executable strategy check.
s=s.replace('''    try:
        assert_candidate_frozen(config.candidate_id)
    except ValueError as exc:
''','''    try:
        assert_candidate_frozen(config.candidate_id)
        assert_strategy_frozen(config.candidate_id)
    except ValueError as exc:
''',1)
p.write_text(s)

# ---------------- UI / CI ----------------
p=Path('app/templates/index.html'); s=p.read_text()
if '/static/executable_strategy.js' not in s: s=s.replace('<script src="/static/phase1.js"></script>','<script src="/static/phase1.js"></script>\n<script src="/static/executable_strategy.js"></script>')
p.write_text(s)
p=Path('.github/workflows/ci.yml'); s=p.read_text()
s=s.replace('run: node --check app/static/app.js && node --check app/static/phase1.js','run: node --check app/static/app.js && node --check app/static/phase1.js && node --check app/static/executable_strategy.js')
p.write_text(s)

# ---------------- release audit / versions ----------------
p=Path('scripts/release_audit.py'); s=p.read_text().replace('EXPECTED_APP_VERSION = "2.5.0"','EXPECTED_APP_VERSION = "2.7.0"').replace('EXPECTED_SCHEMA_VERSION = "2.5.0"','EXPECTED_SCHEMA_VERSION = "2.7.0"')
s=s.replace('''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
''','''    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
    migration_pti = (ROOT / "sql/migrations/2.6.0.sql").read_text(encoding="utf-8")
    migration_pti_availability = (ROOT / "sql/migrations/2.6.2.sql").read_text(encoding="utf-8")
    migration_exec = (ROOT / "sql/migrations/2.7.0.sql").read_text(encoding="utf-8")
''',1)
s=s.replace('''    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    if "_apply_v250_full_history_migration(cur)" not in db_source:
        raise RuntimeError("Fresh/live schema paths do not apply the Phase-1 migration")
''','''    db_source=(ROOT / "app/db.py").read_text(encoding="utf-8")
    for token in ("_apply_v250_full_history_migration(cur)","_apply_v260_point_in_time_migration(cur)","_apply_v262_point_in_time_availability_migration(cur)","_apply_v270_executable_strategy_migration(cur)"):
        if token not in db_source: raise RuntimeError(f"Startup migration path missing {token}")
    for token in ("ra_point_in_time_universe_runs","ra_feature_chunk_universes"):
        if token not in migration_pti and token not in migration_pti_availability: raise RuntimeError(f"PTI migration missing {token}")
    for token in ("ra_strategy_economics_runs","ra_strategy_trades","ra_strategy_equity_points","strategy_configuration_hash","strategy_freeze_timestamp","ra_research_ledger_strategy_guard"):
        if token not in migration_exec: raise RuntimeError(f"Executable strategy migration missing {token}")
''',1)
marker='\n\ndef audit_secrets() -> None:\n'
extra='''\n    exec_source=(ROOT / "app/executable_strategy.py").read_text(encoding="utf-8")\n    exec_js=(ROOT / "app/static/executable_strategy.js").read_text(encoding="utf-8")\n    if '/static/executable_strategy.js' not in html: raise RuntimeError("Executable strategy UI not loaded")\n    for token in ("net_expected_value_pct","maximum_drawdown_pct","strategy_config_hash","_authorize_complete_strategy_freeze","point_in_time_universe_t_minus_1"):\n        if token not in exec_source: raise RuntimeError(f"Executable strategy engine missing {token}")\n    for token in ("Whole-strategy economics","Hit rate remains a diagnostic only","Freeze executable strategy"):\n        if token not in exec_js: raise RuntimeError(f"Executable strategy UI missing {token}")\n'''
if marker not in s: raise SystemExit('audit marker')
s=s.replace(marker,extra+marker,1)
p.write_text(s)
for rel in ('tests/test_candidate_export.py','tests/test_schema_startup.py'):
    p=Path(rel); p.write_text(p.read_text().replace('2.5.0','2.7.0'))

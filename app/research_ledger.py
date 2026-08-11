from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import connection
from app.research_policy import PRESEALED_END_DATE, SEALED_START_DATE
from app.utils import json_safe

REQUIRED_DEPLOYMENT_METHODOLOGY_FIELDS = (
    "decision_information_policy",
    "entry_execution",
    "exit_execution",
    "base_round_trip_cost_bps",
    "spread_assumption",
    "slippage_assumption",
    "capital_allocation_method",
    "position_sizing",
    "simultaneous_signal_handling",
    "maximum_gross_exposure",
    "maximum_net_exposure",
    "symbol_limit",
    "sector_limit",
    "daily_loss_rule",
    "conflict_handling",
    "unused_capital_policy",
    "rebalance_methodology",
    "liquidity_participation_limit",
    "borrow_policy",
    "funding_policy",
    "stop_policy",
)


def _candidate_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(row["id"]),
        "discovery_run_id": str(row["discovery_run_id"]),
        "feature_set_id": str(row["feature_set_id"]),
        "family": row["family"],
        "direction": row["direction"],
        "holding_horizon_minutes": int(row["holding_horizon_minutes"]),
        "conditions": row.get("conditions") or [],
        "entry_sampling_mode": row.get("entry_sampling_mode"),
        "entry_stride_minutes": int(row.get("entry_stride_minutes") or 1),
        "entry_anchor_minute": int(row.get("entry_anchor_minute") or 570),
        "rule_definition_version": row.get("rule_definition_version"),
        "statistics_method": row.get("statistics_method"),
        "engine_version": row.get("engine_version"),
        "hypothesis_ids": row.get("hypothesis_ids") or [],
        "hypothesis_version": row.get("hypothesis_version"),
    }


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_deployment_methodology(methodology: Any) -> dict[str, Any]:
    if not isinstance(methodology, dict) or not methodology:
        raise ValueError("A complete deployment methodology must be fixed before the candidate can be frozen for sealed evaluation")
    missing = [field for field in REQUIRED_DEPLOYMENT_METHODOLOGY_FIELDS if field not in methodology]
    if missing:
        raise ValueError("Deployment methodology is incomplete; missing: " + ", ".join(missing))
    return dict(methodology)


def _deployment_freeze_context(candidate_id: str | UUID) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,engine_version,summary,completed_at
                FROM ra_robustness_runs
                WHERE candidate_id=%s AND status='completed'
                ORDER BY completed_at DESC NULLS LAST,created_at DESC
                LIMIT 1
                """,
                (candidate_id,),
            )
            run = cur.fetchone()
        conn.rollback()
    if not run:
        raise ValueError("A completed whole-strategy robustness run is required before sealed freeze")
    summary = dict(run.get("summary") or {})
    assessment = dict(summary.get("promotion_assessment") or {})
    if assessment.get("deployment_candidate") is not True:
        raise ValueError("Candidate is not a deployment candidate under the whole-strategy promotion standard")
    blockers = list(assessment.get("deployment_blockers") or [])
    if blockers:
        raise ValueError("Candidate still has deployment blockers: " + "; ".join(str(x) for x in blockers))
    if summary.get("sealed_engine_strategy_aware") is not True:
        raise ValueError("Sealed evaluation is locked until the evaluator consumes the frozen complete strategy methodology")
    methodology = _validate_deployment_methodology(summary.get("deployment_methodology"))
    return {
        "robustness_run_id": str(run["id"]),
        "robustness_engine_version": run.get("engine_version"),
        "research_objective": summary.get("research_objective"),
        "research_objective_version": summary.get("research_objective_version"),
        "promotion_classification": assessment.get("classification"),
        "sealed_engine_strategy_aware": True,
        "deployment_methodology": methodology,
    }


def register_research_campaign(discovery_run_id: str | UUID) -> str:
    """Register a discovery campaign without viewing or deriving any sealed result."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*,f.universe_run_id
                FROM ra_discovery_runs r JOIN ra_feature_sets f ON f.id=r.feature_set_id
                WHERE r.id=%s
                """,
                (discovery_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Discovery run does not exist")
            cfg = dict(row["config"] or {})
            cur.execute(
                """
                INSERT INTO ra_research_campaigns(
                    campaign_name,discovery_run_id,feature_set_id,universe_id,engine_version,code_rule_version,
                    parameters_searched,discovery_start,discovery_end,validation_start,validation_end,
                    research_confirmation_start,research_confirmation_end,sealed_test_start,
                    number_candidates_tested,classification,notes
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'research',%s)
                ON CONFLICT (discovery_run_id) DO UPDATE SET
                    campaign_name=EXCLUDED.campaign_name,feature_set_id=EXCLUDED.feature_set_id,universe_id=EXCLUDED.universe_id,
                    engine_version=EXCLUDED.engine_version,code_rule_version=EXCLUDED.code_rule_version,
                    parameters_searched=EXCLUDED.parameters_searched,discovery_start=EXCLUDED.discovery_start,
                    discovery_end=EXCLUDED.discovery_end,validation_start=EXCLUDED.validation_start,
                    validation_end=EXCLUDED.validation_end,number_candidates_tested=EXCLUDED.number_candidates_tested
                RETURNING id
                """,
                (
                    row.get("campaign_name") or row["name"], row["id"], row["feature_set_id"], row["universe_run_id"],
                    row.get("campaign_definition_version"), row.get("campaign_definition_version"), Jsonb(cfg),
                    cfg.get("discovery_start"), cfg.get("discovery_end"), cfg.get("validation_start"), cfg.get("validation_end"),
                    date(2026, 6, 1), PRESEALED_END_DATE, SEALED_START_DATE, int(row.get("candidates_tested") or 0),
                    "Phase 1 ledger record. Sealed outcomes remain unavailable until a retained candidate is explicitly frozen.",
                ),
            )
            campaign_id = str(cur.fetchone()["id"])
        conn.commit()
    return campaign_id


def sync_candidate_ledger(discovery_run_id: str | UUID) -> int:
    campaign_id = register_research_campaign(discovery_run_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*,r.config discovery_config,r.candidates_tested,f.universe_run_id
                FROM ra_candidate_rules c
                JOIN ra_discovery_runs r ON r.id=c.discovery_run_id
                JOIN ra_feature_sets f ON f.id=c.feature_set_id
                WHERE c.discovery_run_id=%s
                """,
                (discovery_run_id,),
            )
            candidates = [dict(r) for r in cur.fetchall()]
            for candidate in candidates:
                cfg = dict(candidate.get("discovery_config") or {})
                snapshot = _candidate_snapshot(candidate)
                validation = {
                    "observations": candidate.get("validation_observations"),
                    "net_avg_pct": candidate.get("validation_net_avg_pct"),
                    "median_pct": candidate.get("validation_median_pct"),
                    "win_rate_pct": candidate.get("validation_win_rate_pct"),
                    "t_stat": candidate.get("validation_t_stat"),
                    "profit_factor": candidate.get("validation_profit_factor"),
                }
                cur.execute(
                    """
                    INSERT INTO ra_research_ledger(
                        campaign_id,campaign_name,discovery_run_id,feature_set_id,universe_id,engine_version,code_rule_version,
                        candidate_family,candidate_id,complete_candidate_configuration,parameters_searched,
                        discovery_start,discovery_end,validation_start,validation_end,
                        research_confirmation_start,research_confirmation_end,sealed_test_start,
                        number_candidates_tested,candidate_retention_status,validation_result,classification,notes
                    )
                    SELECT %s,rc.campaign_name,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'research','Candidate retained by Discovery; not frozen.'
                    FROM ra_research_campaigns rc WHERE rc.id=%s
                    ON CONFLICT (campaign_id,candidate_id) DO UPDATE SET
                        validation_result=CASE WHEN ra_research_ledger.candidate_freeze_timestamp IS NULL THEN EXCLUDED.validation_result ELSE ra_research_ledger.validation_result END,
                        candidate_retention_status=CASE WHEN ra_research_ledger.candidate_freeze_timestamp IS NULL THEN EXCLUDED.candidate_retention_status ELSE ra_research_ledger.candidate_retention_status END
                    """,
                    (
                        campaign_id, candidate["discovery_run_id"], candidate["feature_set_id"], candidate["universe_run_id"],
                        candidate.get("engine_version"), candidate.get("rule_definition_version"), candidate["family"], candidate["id"],
                        Jsonb(snapshot), Jsonb(cfg), cfg.get("discovery_start"), cfg.get("discovery_end"), cfg.get("validation_start"),
                        cfg.get("validation_end"), date(2026, 6, 1), PRESEALED_END_DATE, SEALED_START_DATE,
                        int(candidate.get("candidates_tested") or 0), candidate.get("workflow_status"), Jsonb(validation), campaign_id,
                    ),
                )
        conn.commit()
    return len(candidates)


def freeze_candidate(candidate_id: str | UUID, notes: str | None = None) -> dict[str, Any]:
    """Freeze the exact signal plus complete deployment methodology before true sealed outcome access."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*,r.config discovery_config,f.universe_run_id
                FROM ra_candidate_rules c
                JOIN ra_discovery_runs r ON r.id=c.discovery_run_id
                JOIN ra_feature_sets f ON f.id=c.feature_set_id
                WHERE c.id=%s
                """,
                (candidate_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate does not exist")
    candidate = dict(row)
    sync_candidate_ledger(candidate["discovery_run_id"])
    strategy_context = _deployment_freeze_context(candidate_id)
    snapshot = _candidate_snapshot(candidate)
    snapshot["deployment_methodology"] = strategy_context["deployment_methodology"]
    snapshot["robustness_freeze_context"] = {
        key: value for key, value in strategy_context.items() if key != "deployment_methodology"
    }
    fingerprint = _snapshot_hash(snapshot)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.id,l.candidate_freeze_timestamp,l.frozen_candidate_hash
                FROM ra_research_ledger l
                WHERE l.candidate_id=%s ORDER BY l.created_at DESC LIMIT 1
                FOR UPDATE
                """,
                (candidate_id,),
            )
            ledger = cur.fetchone()
            if not ledger:
                raise RuntimeError("Research Ledger row was not created")
            if ledger["candidate_freeze_timestamp"]:
                if ledger["frozen_candidate_hash"] != fingerprint:
                    raise RuntimeError("Frozen Research Ledger definition differs from the current complete strategy")
            else:
                cur.execute(
                    """
                    UPDATE ra_research_ledger SET complete_candidate_configuration=%s,
                        candidate_freeze_timestamp=now(),frozen_candidate_hash=%s,
                        candidate_retention_status='frozen',classification='frozen_complete_strategy_pre_sealed',
                        notes=COALESCE(%s,notes)
                    WHERE id=%s
                    RETURNING *
                    """,
                    (Jsonb(snapshot), fingerprint, notes, ledger["id"]),
                )
                ledger = cur.fetchone()
        conn.commit()
    return json_safe({"candidate_id": candidate_id, "frozen_hash": fingerprint, "ledger": dict(ledger)})


def assert_candidate_frozen(candidate_id: str | UUID) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_candidate_rules WHERE id=%s", (candidate_id,))
            candidate = cur.fetchone()
            cur.execute(
                """
                SELECT * FROM ra_research_ledger
                WHERE candidate_id=%s AND candidate_freeze_timestamp IS NOT NULL
                ORDER BY candidate_freeze_timestamp DESC LIMIT 1
                """,
                (candidate_id,),
            )
            ledger = cur.fetchone()
        conn.rollback()
    if not candidate or not ledger:
        raise ValueError("Complete strategy must be frozen in the Research Ledger before sealed evaluation")
    stored = dict(ledger.get("complete_candidate_configuration") or {})
    methodology = _validate_deployment_methodology(stored.get("deployment_methodology"))
    freeze_context = dict(stored.get("robustness_freeze_context") or {})
    if freeze_context.get("sealed_engine_strategy_aware") is not True:
        raise ValueError("Frozen candidate is not approved for strategy-aware sealed execution")
    snapshot = _candidate_snapshot(dict(candidate))
    snapshot["deployment_methodology"] = methodology
    snapshot["robustness_freeze_context"] = freeze_context
    fingerprint = _snapshot_hash(snapshot)
    if fingerprint != ledger["frozen_candidate_hash"]:
        raise ValueError("Complete strategy definition changed after the Research Ledger freeze")
    return dict(ledger)


def record_sealed_result(candidate_id: str | UUID, start: date, end: date, result: dict[str, Any]) -> None:
    assert_candidate_frozen(candidate_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_research_ledger SET sealed_test_start=%s,sealed_test_end=%s,
                    sealed_test_result=%s,classification='sealed_evaluated'
                WHERE id=(SELECT id FROM ra_research_ledger WHERE candidate_id=%s AND candidate_freeze_timestamp IS NOT NULL ORDER BY candidate_freeze_timestamp DESC LIMIT 1)
                """,
                (start, end, Jsonb(result), candidate_id),
            )
        conn.commit()

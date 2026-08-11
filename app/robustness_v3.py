from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import add_event
from app.robustness import run_robustness as run_legacy_robustness
from app.strategy_economics import OBJECTIVE_VERSION, promotion_assessment, strategy_economics
from app.utils import json_safe


def _load_base_rows(run_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol,bar_ts,trade_date,minute_of_day,liquidity_tier,price_group,
                       gross_return_pct,mfe_pct,mae_pct
                FROM ra_robustness_samples
                WHERE robustness_run_id=%s AND variant_key='delay:0'
                ORDER BY trade_date,symbol,bar_ts
                """,
                (run_id,),
            )
            rows = cur.fetchall()
        conn.rollback()
    return [dict(row) for row in rows]


def _candidate_thresholds(candidate_id: Any) -> tuple[int, int, int]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.holding_horizon_minutes,d.config
                FROM ra_candidate_rules c
                JOIN ra_discovery_runs d ON d.id=c.discovery_run_id
                WHERE c.id=%s
                """,
                (candidate_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Candidate disappeared while finalising whole-strategy economics")
    discovery_config = dict(row.get("config") or {})
    return (
        int(row["holding_horizon_minutes"]),
        int(discovery_config.get("minimum_observations") or 250),
        int(discovery_config.get("minimum_dates") or 10),
    )


def run_robustness(job_id: str, config: Any) -> dict[str, Any]:
    result = run_legacy_robustness(job_id, config)
    summary = dict(result.get("summary") or {})
    run_id = str(result["robustness_run_id"])
    horizon, minimum_observations, minimum_dates = _candidate_thresholds(config.candidate_id)
    rows = _load_base_rows(run_id)
    base_cost = float((summary.get("base") or {}).get("cost_bps") or 20.0)
    economics = strategy_economics(rows, cost_bps=base_cost, holding_horizon_minutes=horizon)
    assessment = promotion_assessment(
        economics,
        summary,
        mode=str(config.mode),
        minimum_observations=minimum_observations,
        minimum_dates=minimum_dates,
    )

    legacy_verdict = summary.get("verdict")
    verdict = assessment["legacy_compatible_verdict"]
    summary["legacy_signal_level_verdict"] = legacy_verdict
    summary["verdict"] = verdict
    summary["research_objective"] = "whole_strategy_net_economics"
    summary["research_objective_version"] = OBJECTIVE_VERSION
    summary["whole_strategy_economics"] = economics
    summary["promotion_assessment"] = assessment
    summary["sealed_period_accessed_by_this_analysis"] = False

    safe_summary = json_safe(summary)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ra_robustness_runs
                SET verdict=%s,summary=%s,engine_version=%s
                WHERE id=%s
                """,
                (verdict, Jsonb(safe_summary), "3.0.0-whole-strategy", run_id),
            )
        conn.commit()

    add_event(
        job_id,
        "whole_strategy_economics_completed",
        f"Whole-strategy economics applied; promotion decision={assessment['decision']} classification={assessment['classification']}.",
        details={
            "research_objective_version": OBJECTIVE_VERSION,
            "legacy_verdict": legacy_verdict,
            "whole_strategy_verdict": verdict,
            "classification": assessment["classification"],
            "deployment_candidate": False,
        },
    )
    result["legacy_verdict"] = legacy_verdict
    result["verdict"] = verdict
    result["summary"] = safe_summary
    return result

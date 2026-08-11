from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import add_event
from app.portfolio_diagnostics import portfolio_diagnostics
from app.robustness import run_robustness as run_legacy_robustness
from app.strategy_economics import OBJECTIVE_VERSION, promotion_assessment, strategy_economics
from app.utils import json_safe


def _load_base_rows(run_id: str, target_feature_set_id: Any) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.symbol,s.bar_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,s.price_group,
                       s.gross_return_pct,s.mfe_pct,s.mae_pct,
                       f.close,f.volume,f.bar_dollar_volume,
                       CASE WHEN f.previous_20bar_avg_volume > 0 AND f.close > 0
                            THEN f.previous_20bar_avg_volume * f.close END AS trailing_20bar_dollar_volume_proxy
                FROM ra_robustness_samples s
                LEFT JOIN ra_intraday_features f
                  ON f.feature_set_id=%s AND f.symbol=s.symbol AND f.bar_ts=s.bar_ts
                WHERE s.robustness_run_id=%s AND s.variant_key='delay:0'
                ORDER BY s.trade_date,s.symbol,s.bar_ts
                """,
                (target_feature_set_id, run_id),
            )
            rows = cur.fetchall()
        conn.rollback()
    return [dict(row) for row in rows]


def _candidate_context(candidate_id: Any) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.direction,c.holding_horizon_minutes,c.multiple_testing_method,c.multiple_testing_adjusted_p,
                       c.variants_tested_campaign,c.variants_defined_campaign,d.config
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
    return {
        "direction": str(row["direction"]),
        "holding_horizon_minutes": int(row["holding_horizon_minutes"]),
        "minimum_observations": int(discovery_config.get("minimum_observations") or 250),
        "minimum_dates": int(discovery_config.get("minimum_dates") or 10),
        "multiple_testing_method": row.get("multiple_testing_method"),
        "multiple_testing_adjusted_p": float(row["multiple_testing_adjusted_p"]) if row.get("multiple_testing_adjusted_p") is not None else None,
        "variants_tested_campaign": row.get("variants_tested_campaign"),
        "variants_defined_campaign": row.get("variants_defined_campaign"),
    }


def _apply_statistical_credibility_cap(assessment: dict[str, Any], context: dict[str, Any]) -> None:
    adjusted_p = context.get("multiple_testing_adjusted_p")
    multiple_testing_credible = adjusted_p is not None and adjusted_p <= 0.05
    assessment["statistical_credibility"] = {
        "multiple_testing_method": context.get("multiple_testing_method"),
        "multiple_testing_adjusted_p": adjusted_p,
        "variants_tested_campaign": context.get("variants_tested_campaign"),
        "variants_defined_campaign": context.get("variants_defined_campaign"),
        "multiple_testing_credible_at_5pct": multiple_testing_credible,
    }
    if assessment.get("classification") == "statistically_credible" and not multiple_testing_credible:
        assessment["classification"] = "promising"
        assessment.setdefault("statistical_credibility_blockers", []).append(
            "Multiple-testing credibility is absent or fails the 5% adjusted-p standard; economic promise is retained but statistical credibility is not claimed."
        )


def _apply_portfolio_evidence(assessment: dict[str, Any], diagnostics: dict[str, Any]) -> None:
    assessment["portfolio_diagnostics_available"] = bool(diagnostics.get("available"))
    if not diagnostics.get("available"):
        return
    blockers = list(assessment.get("deployment_blockers") or [])
    blockers = [
        blocker for blocker in blockers
        if blocker not in {"actual capital utilisation", "instrument-level liquidity/capacity"}
    ]
    blockers.extend([
        "freeze one production position-sizing/capital-allocation policy before sealed holdout",
        "calibrate market impact beyond minute-volume participation proxies",
    ])
    assessment["deployment_blockers"] = list(dict.fromkeys(blockers))
    assessment["portfolio_scenario_policy"] = (
        "Capital/liquidity scenarios are diagnostic only. No best scenario is selected and none may be chosen using sealed results."
    )


def run_robustness(job_id: str, config: Any) -> dict[str, Any]:
    result = run_legacy_robustness(job_id, config)
    summary = dict(result.get("summary") or {})
    run_id = str(result["robustness_run_id"])
    context = _candidate_context(config.candidate_id)
    rows = _load_base_rows(run_id, config.target_feature_set_id)
    base_cost = float((summary.get("base") or {}).get("cost_bps") or 20.0)
    economics = strategy_economics(
        rows,
        cost_bps=base_cost,
        holding_horizon_minutes=context["holding_horizon_minutes"],
    )
    diagnostics = portfolio_diagnostics(
        rows,
        cost_bps=base_cost,
        horizon_minutes=context["holding_horizon_minutes"],
        direction=context["direction"],
    )
    economics["capital_and_capacity"] = diagnostics
    assessment = promotion_assessment(
        economics,
        summary,
        mode=str(config.mode),
        minimum_observations=context["minimum_observations"],
        minimum_dates=context["minimum_dates"],
    )
    _apply_statistical_credibility_cap(assessment, context)
    _apply_portfolio_evidence(assessment, diagnostics)

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
                (verdict, Jsonb(safe_summary), "3.1.0-whole-strategy-capacity", run_id),
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
            "portfolio_diagnostics_available": diagnostics.get("available"),
            "deployment_candidate": False,
        },
    )
    result["legacy_verdict"] = legacy_verdict
    result["verdict"] = verdict
    result["summary"] = safe_summary
    return result

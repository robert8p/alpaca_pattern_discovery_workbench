from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import add_event
from app.portfolio_diagnostics import portfolio_diagnostics
from app.regime_diagnostics import regime_diagnostics
from app.robustness import run_robustness as run_legacy_robustness
from app.strategy_economics import OBJECTIVE_VERSION, promotion_assessment, strategy_economics
from app.utils import json_safe


def _market_state_context(target_feature_set_id: Any, start_date: Any, end_date: Any) -> dict[str, Any]:
    if target_feature_set_id is None:
        return {"available": False, "reason": "No target feature set was resolved."}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,row_count,min_trade_date,max_trade_date,completed_at
                FROM ra_market_state_runs
                WHERE feature_set_id=%s AND status='completed'
                  AND min_trade_date<=%s AND max_trade_date>=%s
                ORDER BY completed_at DESC NULLS LAST,created_at DESC
                LIMIT 1
                """,
                (target_feature_set_id, start_date, end_date),
            )
            run = cur.fetchone()
            if not run:
                conn.rollback()
                return {
                    "available": False,
                    "reason": "No completed point-in-time market-state run covers the full robustness period.",
                }
            cur.execute(
                """
                SELECT count(*) AS timestamps,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY dispersion_30m_pct)
                           FILTER (WHERE dispersion_30m_pct IS NOT NULL) AS dispersion_p25,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY dispersion_30m_pct)
                           FILTER (WHERE dispersion_30m_pct IS NOT NULL) AS dispersion_p75,
                       count(dispersion_30m_pct) AS dispersion_n,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY pct_abnormal_volatility)
                           FILTER (WHERE pct_abnormal_volatility IS NOT NULL) AS volatility_p25,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY pct_abnormal_volatility)
                           FILTER (WHERE pct_abnormal_volatility IS NOT NULL) AS volatility_p75,
                       count(pct_abnormal_volatility) AS volatility_n,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY median_relative_volume)
                           FILTER (WHERE median_relative_volume IS NOT NULL) AS liquidity_p25,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY median_relative_volume)
                           FILTER (WHERE median_relative_volume IS NOT NULL) AS liquidity_p75,
                       count(median_relative_volume) AS liquidity_n
                FROM ra_market_state_features
                WHERE market_state_run_id=%s
                """,
                (run["id"],),
            )
            calibration = cur.fetchone()
        conn.rollback()
    return {
        "available": True,
        "market_state_run_id": str(run["id"]),
        "timestamps": int(calibration["timestamps"] or 0),
        "min_trade_date": str(run["min_trade_date"]),
        "max_trade_date": str(run["max_trade_date"]),
        "thresholds": {
            "dispersion_30m_pct": {
                "p25": calibration["dispersion_p25"],
                "p75": calibration["dispersion_p75"],
                "observations": int(calibration["dispersion_n"] or 0),
            },
            "pct_abnormal_volatility": {
                "p25": calibration["volatility_p25"],
                "p75": calibration["volatility_p75"],
                "observations": int(calibration["volatility_n"] or 0),
            },
            "median_relative_volume": {
                "p25": calibration["liquidity_p25"],
                "p75": calibration["liquidity_p75"],
                "observations": int(calibration["liquidity_n"] or 0),
            },
        },
    }


def _load_base_rows(run_id: str, target_feature_set_id: Any, market_state_run_id: str | None) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.symbol,s.bar_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,s.price_group,
                       s.gross_return_pct,s.mfe_pct,s.mae_pct,
                       f.close,f.volume,f.bar_dollar_volume,
                       CASE WHEN f.previous_20bar_avg_volume > 0 AND f.close > 0
                            THEN f.previous_20bar_avg_volume * f.close END AS trailing_20bar_dollar_volume_proxy,
                       ms.market_state_run_id,ms.pct_positive_30m,ms.dispersion_30m_pct,
                       ms.pct_abnormal_volatility,prior_ms.pct_abnormal_volatility AS prior_pct_abnormal_volatility,
                       ms.median_relative_volume,ms.spy_return_30m_pct
                FROM ra_robustness_samples s
                LEFT JOIN ra_intraday_features f
                  ON f.feature_set_id=%s AND f.symbol=s.symbol AND f.bar_ts=s.bar_ts
                LEFT JOIN ra_market_state_features ms
                  ON ms.market_state_run_id=%s AND ms.bar_ts=s.bar_ts
                LEFT JOIN ra_market_state_features prior_ms
                  ON prior_ms.market_state_run_id=%s AND prior_ms.bar_ts=s.bar_ts-interval '5 minutes'
                WHERE s.robustness_run_id=%s AND s.variant_key='delay:0'
                ORDER BY s.trade_date,s.symbol,s.bar_ts
                """,
                (target_feature_set_id, market_state_run_id, market_state_run_id, run_id),
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


def _apply_regime_evidence(assessment: dict[str, Any], regimes: dict[str, Any]) -> None:
    available = bool(regimes.get("available"))
    assessment["regime_diagnostics_available"] = available
    blockers = list(assessment.get("deployment_blockers") or [])
    if not available:
        blockers.append("objective pre-sealed market-state regime diagnostics")
        assessment["regime_stability"] = "not_measured"
    else:
        blockers = [b for b in blockers if b != "objective pre-sealed market-state regime diagnostics"]
        negative = int(regimes.get("negative_supported_regime_count") or 0)
        assessment["regime_stability"] = "mixed" if negative else "no_negative_supported_regime_detected"
        assessment["negative_supported_regime_count"] = negative
        if negative:
            assessment.setdefault("robustness_flags", []).append(
                "One or more objectively defined, adequately sampled regimes have negative net/event expectancy. Do not add a rescue filter post hoc."
            )
    assessment["deployment_blockers"] = list(dict.fromkeys(blockers))


def run_robustness(job_id: str, config: Any) -> dict[str, Any]:
    result = run_legacy_robustness(job_id, config)
    summary = dict(result.get("summary") or {})
    run_id = str(result["robustness_run_id"])
    context = _candidate_context(config.candidate_id)
    target_feature_set_id = config.target_feature_set_id or summary.get("target_feature_set_id") or summary.get("source_feature_set_id")
    state_context = _market_state_context(target_feature_set_id, summary.get("start_date"), summary.get("end_date"))
    rows = _load_base_rows(
        run_id,
        target_feature_set_id,
        state_context.get("market_state_run_id") if state_context.get("available") else None,
    )
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
    regimes = regime_diagnostics(
        rows,
        cost_bps=base_cost,
        calibration_thresholds=state_context.get("thresholds") if state_context.get("available") else None,
        calibration_market_state_timestamps=int(state_context.get("timestamps") or 0),
        market_state_run_id=state_context.get("market_state_run_id"),
    )
    economics["capital_and_capacity"] = diagnostics
    economics["regime_sensitivity"] = regimes
    assessment = promotion_assessment(
        economics,
        summary,
        mode=str(config.mode),
        minimum_observations=context["minimum_observations"],
        minimum_dates=context["minimum_dates"],
    )
    _apply_statistical_credibility_cap(assessment, context)
    _apply_portfolio_evidence(assessment, diagnostics)
    _apply_regime_evidence(assessment, regimes)

    legacy_verdict = summary.get("verdict")
    verdict = assessment["legacy_compatible_verdict"]
    summary["legacy_signal_level_verdict"] = legacy_verdict
    summary["verdict"] = verdict
    summary["research_objective"] = "whole_strategy_net_economics"
    summary["research_objective_version"] = OBJECTIVE_VERSION
    summary["whole_strategy_economics"] = economics
    summary["regime_sensitivity"] = regimes
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
                (verdict, Jsonb(safe_summary), "3.2.0-whole-strategy-regimes", run_id),
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
            "regime_diagnostics_available": regimes.get("available"),
            "negative_supported_regime_count": regimes.get("negative_supported_regime_count"),
            "deployment_candidate": False,
        },
    )
    result["legacy_verdict"] = legacy_verdict
    result["verdict"] = verdict
    result["summary"] = safe_summary
    return result

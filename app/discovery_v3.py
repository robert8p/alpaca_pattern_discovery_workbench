from __future__ import annotations

import math
from typing import Any

from app import discovery as base
from app.models import DiscoveryConfig, SealedEvaluationConfig
from app.utils import finite_or_none

DISCOVERY_VERSION = "2.3.0-whole-economics-screen"
CAMPAIGN_DEFINITION_VERSION = "2026-08-research-integrity-pack2-whole-economics"
BLANK_CANVAS_VERSION = "blank-canvas-state-lattice-v1"

# These families do not encode a predicted direction or a named trading thesis.
# They systematically cross observable point-in-time state dimensions and allow
# the engine to evaluate both long/short and every configured horizon. The grid
# is intentionally finite so multiple-testing burden remains measurable.
BLANK_CANVAS_FAMILIES: dict[str, list[Any]] = {
    "blank_canvas_price_activity": [base.RET5, base.RET30, base.RVOL],
    "blank_canvas_location_liquidity": [base.VWAP_SIDE, base.RANGE_POS, base.RVOL],
    "blank_canvas_state_change": [base.RVOL_CHANGE, base.RTCOUNT_CHANGE, base.IMPACT_CHANGE],
    "blank_canvas_range_volatility": [base.RANGE_RATIO, base.VOL_RATIO, base.RVOL],
    "blank_canvas_temporal_state": [base.TIME, base.WEEKDAY, base.RET5],
    "blank_canvas_gap_prior_state": [base.GAP, base.PREV_DAY, base.RVOL],
}


def economic_rank_score(discovery: dict[str, Any], validation: dict[str, Any] | None) -> float:
    """Coarse pre-robustness economic screen.

    This is deliberately not a hit-rate score and is not a deployment score.
    It broadens the original mean-return/t-stat ranking with payoff quality,
    median outcome, left-tail shape, concentration and chronological stability.
    Full strategy economics, overlap, liquidity, capacity and capital allocation
    remain the responsibility of robustness_v3.
    """
    d_net = max(float(discovery.get("net_avg_pct") or 0.0), 0.0)
    if d_net <= 0:
        return 0.0

    observations = float(discovery.get("observations") or 0.0)
    t_stat = max(float(discovery.get("t_stat") or 0.0), 0.0)
    profit_factor = float(discovery.get("profit_factor") or 1.0)
    median = float(discovery.get("median_pct") or 0.0)
    p05 = float(discovery.get("p05_pct") or 0.0)
    worst = float(discovery.get("worst_pct") or 0.0)

    concentration = max(0.05, 1.0 - float(discovery.get("max_symbol_share_pct") or 100.0) / 100.0)
    concentration *= max(0.05, 1.0 - float(discovery.get("max_date_share_pct") or 100.0) / 100.0)

    # No component uses win_rate_pct. A lower-hit-rate strategy with strong
    # expectancy/payoff can outrank a higher-hit-rate but fragile strategy.
    payoff_factor = math.sqrt(min(4.0, max(0.25, profit_factor)))
    median_factor = 1.0 + 0.20 * math.tanh(median / max(abs(d_net), 0.10))

    adverse_p05_ratio = max(0.0, -p05) / max(d_net, 0.05)
    p05_factor = 1.0 / (1.0 + 0.08 * adverse_p05_ratio)
    worst_ratio = max(0.0, -worst) / max(d_net, 0.05)
    worst_factor = 1.0 / (1.0 + 0.015 * worst_ratio)

    # Significance matters as credibility evidence but no longer multiplies the
    # score almost linearly. This prevents t-stat from overwhelming economics.
    credibility_factor = 0.80 + 0.20 * math.tanh(t_stat / 2.0)

    score = (
        d_net
        * math.log1p(observations)
        * payoff_factor
        * median_factor
        * p05_factor
        * worst_factor
        * credibility_factor
        * concentration
    )

    if validation:
        v_net = float(validation.get("net_avg_pct") or 0.0)
        if v_net <= 0:
            score *= 0.20
        else:
            stability = 1.0 - min(abs(d_net - v_net) / max(abs(d_net), abs(v_net), 0.01), 1.0)
            v_pf = float(validation.get("profit_factor") or 1.0)
            v_median = float(validation.get("median_pct") or 0.0)
            validation_payoff = math.sqrt(min(3.0, max(0.50, v_pf))) / math.sqrt(1.5)
            validation_median = 1.0 + 0.15 * math.tanh(v_median / max(abs(v_net), 0.10))
            score *= (0.55 + 0.45 * stability) * validation_payoff * validation_median

    return finite_or_none(score) or 0.0


def _install_blank_canvas_families() -> None:
    for family_name, dimensions in BLANK_CANVAS_FAMILIES.items():
        base.FAMILIES[family_name] = {
            "hypothesis_ids": [],
            "hypothesis_version": BLANK_CANVAS_VERSION,
            "coverage": "HYPOTHESIS_FREE_GRID",
            "dimensions": dimensions,
            "filter": "TRUE",
            "constraints": [],
            "constraint_descriptions": [],
        }


def _augment_config(config: DiscoveryConfig) -> DiscoveryConfig:
    augmented = config.model_copy(deep=True)
    requested = list(augmented.families)
    for family_name in BLANK_CANVAS_FAMILIES:
        if family_name not in requested:
            requested.append(family_name)
    # DiscoveryConfig uses a closed Literal list at the API boundary to preserve
    # backwards compatibility. Internally, these versioned engine-owned family
    # names are appended after successful validation and persisted in the run.
    augmented.families = requested  # type: ignore[assignment]
    return augmented


def _configure() -> None:
    # The base discovery module owns the mature chunking/query engine. Override
    # only its ranking objective/version and add engine-owned hypothesis-free
    # grids, preserving the hardened chunking, retry and multiple-testing logic.
    _install_blank_canvas_families()
    base._rank_score = economic_rank_score
    base.DISCOVERY_VERSION = DISCOVERY_VERSION
    base.CAMPAIGN_DEFINITION_VERSION = CAMPAIGN_DEFINITION_VERSION


def _ensure_discovery_run(job_id: str, config: DiscoveryConfig):
    _configure()
    return base._ensure_discovery_run(job_id, _augment_config(config))


def run_discovery(job_id: str, config: DiscoveryConfig):
    _configure()
    return base.run_discovery(job_id, _augment_config(config))


def run_sealed_evaluation(job_id: str, config: SealedEvaluationConfig):
    # Sealed execution rules are unchanged. Configuration remains frozen before
    # this function may be reached through the worker's sealed guard.
    return base.run_sealed_evaluation(job_id, config)

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from typing import Any, Iterable

from app.utils import json_safe

CANDIDATE_COLUMNS = [
    "id", "discovery_run_id", "discovery_run_name", "feature_set_id", "feature_set_name",
    "universe_run_id", "universe_name", "family", "direction", "holding_horizon_minutes",
    "workflow_status", "rank_score", "plain_english_rule", "conditions_json",
    "discovery_start", "discovery_end", "validation_start", "validation_end",
    "round_trip_cost_bps", "minimum_observations", "minimum_symbols", "minimum_dates",
    "maximum_symbol_concentration_pct", "maximum_date_concentration_pct",
    "entry_sampling_mode", "entry_stride_minutes", "entry_anchor_minute",
    "rule_definition_version", "statistics_method", "engine_version",
    "discovery_observations", "discovery_symbols", "discovery_dates",
    "discovery_gross_avg_pct", "discovery_net_avg_pct", "discovery_median_pct",
    "discovery_win_rate_pct", "discovery_t_stat", "discovery_profit_factor",
    "discovery_p05_pct", "discovery_worst_pct", "discovery_max_symbol_share_pct",
    "discovery_max_date_share_pct", "validation_observations", "validation_symbols",
    "validation_dates", "validation_gross_avg_pct", "validation_net_avg_pct",
    "validation_median_pct", "validation_win_rate_pct", "validation_t_stat",
    "validation_profit_factor", "validation_p05_pct", "validation_worst_pct",
    "validation_max_symbol_share_pct", "validation_max_date_share_pct",
    "sealed_start", "sealed_end", "sealed_observations", "sealed_net_avg_pct",
    "sealed_median_pct", "sealed_win_rate_pct", "sealed_t_stat",
    "sealed_profit_factor", "sealed_evaluated_at", "created_at",
]

TASK_COLUMNS = [
    "discovery_run_id", "discovery_run_name", "family", "direction",
    "holding_horizon_minutes", "status", "groups_tested", "candidates_retained",
    "attempts", "error", "started_at", "completed_at",
]

UNIVERSE_SYMBOL_COLUMNS = [
    "universe_run_id", "universe_name", "rank_by_liquidity", "symbol", "exchange",
    "asset_name", "liquidity_tier", "trading_days", "average_bars_per_day",
    "median_daily_dollar_volume", "average_daily_dollar_volume", "median_close",
]


def _csv_bytes(rows: Iterable[dict[str, Any]], columns: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for raw in rows:
        row = json_safe(raw)
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue().encode("utf-8-sig")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(json_safe(value), indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")


def safe_filename_component(value: str | None, fallback: str = "candidates") -> str:
    value = (value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return (value or fallback)[:60]


def export_filename(discovery_runs: list[dict[str, Any]], exported_at: datetime | None = None) -> str:
    exported_at = exported_at or datetime.now(UTC)
    stem = "candidate-results"
    if len(discovery_runs) == 1:
        stem = safe_filename_component(discovery_runs[0].get("name"), stem)
    return f"alpaca_{stem}_{exported_at:%Y%m%dT%H%M%SZ}.zip"


def build_candidate_export_bundle(
    *,
    candidates: list[dict[str, Any]],
    discovery_runs: list[dict[str, Any]],
    discovery_tasks: list[dict[str, Any]],
    feature_sets: list[dict[str, Any]],
    universes: list[dict[str, Any]],
    universe_symbols: list[dict[str, Any]],
    filters: dict[str, Any],
    app_version: str,
    exported_at: datetime | None = None,
) -> bytes:
    exported_at = exported_at or datetime.now(UTC)

    runs_by_id = {str(row["id"]): row for row in discovery_runs}
    features_by_id = {str(row["id"]): row for row in feature_sets}
    universes_by_id = {str(row["id"]): row for row in universes}

    enriched_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(json_safe(candidate))
        run = runs_by_id.get(str(row.get("discovery_run_id")), {})
        feature = features_by_id.get(str(row.get("feature_set_id")), {})
        universe = universes_by_id.get(str(feature.get("universe_run_id")), {})
        run_config = run.get("config") or {}
        row.update({
            "discovery_run_name": run.get("name"),
            "feature_set_name": feature.get("name"),
            "universe_run_id": feature.get("universe_run_id"),
            "universe_name": universe.get("name"),
            "conditions_json": row.get("conditions"),
            "discovery_start": run_config.get("discovery_start"),
            "discovery_end": run_config.get("discovery_end"),
            "validation_start": run_config.get("validation_start"),
            "validation_end": run_config.get("validation_end"),
            "round_trip_cost_bps": run_config.get("round_trip_cost_bps"),
            "minimum_observations": run_config.get("minimum_observations"),
            "minimum_symbols": run_config.get("minimum_symbols"),
            "minimum_dates": run_config.get("minimum_dates"),
            "maximum_symbol_concentration_pct": run_config.get("maximum_symbol_concentration_pct"),
            "maximum_date_concentration_pct": run_config.get("maximum_date_concentration_pct"),
        })
        enriched_candidates.append(row)

    enriched_tasks: list[dict[str, Any]] = []
    for task in discovery_tasks:
        row = dict(json_safe(task))
        run = runs_by_id.get(str(row.get("discovery_run_id")), {})
        row["discovery_run_name"] = run.get("name")
        enriched_tasks.append(row)

    enriched_symbols: list[dict[str, Any]] = []
    for symbol in universe_symbols:
        row = dict(json_safe(symbol))
        universe = universes_by_id.get(str(row.get("universe_run_id")), {})
        row["universe_name"] = universe.get("name")
        enriched_symbols.append(row)

    manifest = {
        "export_format_version": "1.0",
        "app_version": app_version,
        "exported_at": exported_at.isoformat(),
        "filters": json_safe(filters),
        "candidate_count": len(enriched_candidates),
        "discovery_run_count": len(discovery_runs),
        "feature_set_count": len(feature_sets),
        "universe_count": len(universes),
        "included_universe_symbol_rows": len(enriched_symbols),
        "files": {
            "candidates.csv": "Flattened candidate leaderboard for spreadsheet-style analysis.",
            "candidates.json": "Complete candidate records including frozen JSON rule conditions.",
            "discovery_runs.json": "Discovery configuration and run-level test/retention counts.",
            "discovery_tasks.csv": "Family × direction × holding-horizon task outcomes.",
            "feature_sets.json": "Feature-set configuration and source coverage.",
            "universes.json": "Frozen universe configuration and tier counts.",
            "universe_symbols.csv": "Included symbols and liquidity/coverage metrics for the relevant universes.",
            "ANALYSIS_PROMPT.txt": "Suggested prompt to use when uploading this ZIP to ChatGPT.",
            "SUMMARY.md": "Compact human-readable overview of the exported candidate set.",
            "README.txt": "Description of the export and interpretation safeguards.",
        },
    }


    def _count_by(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in enriched_candidates:
            value = str(row.get(key) or "missing")
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    top = sorted(enriched_candidates, key=lambda row: (row.get("rank_score") is not None, row.get("rank_score") or float("-inf")), reverse=True)[:20]
    summary_lines = [
        "# Candidate export summary", "",
        f"- Exported: {exported_at.isoformat()}",
        f"- Workbench version: {app_version}",
        f"- Candidates: {len(enriched_candidates)}",
        f"- Discovery runs: {len(discovery_runs)}",
        f"- Filters: `{json.dumps(json_safe(filters), sort_keys=True)}`", "",
        "## Candidate mix", "",
        f"- Workflow status: `{json.dumps(_count_by('workflow_status'))}`",
        f"- Family: `{json.dumps(_count_by('family'))}`",
        f"- Direction: `{json.dumps(_count_by('direction'))}`",
        f"- Holding horizon: `{json.dumps(_count_by('holding_horizon_minutes'))}`", "",
        "## Top candidates by stored rank score", "",
        "| # | Rule | Status | Disc. net | Val. net | Obs. | PF | t-stat |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(top, 1):
        rule = str(row.get("plain_english_rule") or "").replace("|", "\\|")
        def _fmt(value: Any, digits: int = 3) -> str:
            return "—" if value is None else f"{float(value):.{digits}f}"
        summary_lines.append(
            f"| {idx} | {rule} | {row.get('workflow_status') or '—'} | "
            f"{_fmt(row.get('discovery_net_avg_pct'))}% | {_fmt(row.get('validation_net_avg_pct'))}% | "
            f"{row.get('discovery_observations') or 0} | {_fmt(row.get('discovery_profit_factor'), 2)} | "
            f"{_fmt(row.get('discovery_t_stat'), 2)} |"
        )
    summary = "\n".join(summary_lines) + "\n"

    readme = f"""Alpaca Pattern Discovery Workbench — Candidate Analysis Export

Exported: {exported_at.isoformat()}
Workbench version: {app_version}
Candidates exported: {len(enriched_candidates)}

This package is designed to be uploaded directly into ChatGPT for analysis.
It contains the candidate leaderboard plus the frozen discovery configuration,
feature-set context and relevant universe composition.

Important interpretation rules
------------------------------
1. Discovery, validation and sealed results are separate evidence stages.
2. A missing sealed result means the candidate has NOT yet been tested on the final holdout.
3. conditions_json contains the exact frozen candidate rule definition.
4. Net-return fields include the round-trip cost assumption frozen in the discovery run.
5. Candidate ranking is a screening device, not proof of future profitability.
6. universe_symbols.csv contains only symbols INCLUDED in the relevant frozen universe(s).
7. Do not tune a rule using a sealed period after its result has been revealed.

Recommended first file for human review: candidates.csv
Recommended machine-complete file: candidates.json
"""

    prompt = """Analyse this Alpaca Pattern Discovery Workbench candidate export comprehensively.

Please:
1. Rank the genuinely strongest candidates, prioritising validation persistence, net expected return, sample size, profit factor, t-statistic, tail behaviour and low symbol/date concentration rather than raw discovery rank alone.
2. Identify likely overfit, regime-specific, illiquid or economically implausible candidates.
3. Compare related candidates across families, directions and holding horizons to identify stable underlying relationships rather than isolated thresholds.
4. Explain the most promising rules in plain English and the likely market mechanism behind each.
5. Tell me which candidates are worth promoting to a sealed test and which should be rejected or held back.
6. Propose the exact next data loads, robustness tests and threshold-neighbourhood tests needed before any live trading decision.
7. Flag any important information missing from the export that would materially change your conclusions.

Do not assume that a high discovery score alone constitutes an edge.
"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("manifest.json", _json_bytes(manifest))
        archive.writestr("candidates.csv", _csv_bytes(enriched_candidates, CANDIDATE_COLUMNS))
        archive.writestr("candidates.json", _json_bytes(enriched_candidates))
        archive.writestr("discovery_runs.json", _json_bytes(discovery_runs))
        archive.writestr("discovery_tasks.csv", _csv_bytes(enriched_tasks, TASK_COLUMNS))
        archive.writestr("feature_sets.json", _json_bytes(feature_sets))
        archive.writestr("universes.json", _json_bytes(universes))
        archive.writestr("universe_symbols.csv", _csv_bytes(enriched_symbols, UNIVERSE_SYMBOL_COLUMNS))
        archive.writestr("SUMMARY.md", summary.encode("utf-8"))
        archive.writestr("README.txt", readme.encode("utf-8"))
        archive.writestr("ANALYSIS_PROMPT.txt", prompt.encode("utf-8"))
    return output.getvalue()

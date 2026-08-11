from __future__ import annotations

import ast
import importlib
import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import APP_VERSION, SCHEMA_VERSION
from app.discovery import DISCOVERY_ENGINE_VERSION, all_discovery_families
from app.preflight import local_sql_preflight
from app.sql_validation import SqlBindingError, inspect_psycopg_placeholders

EXPECTED_APP_VERSION = "2.5.0"
EXPECTED_DISCOVERY_VERSION = "2.2.0"
EXPECTED_SCHEMA_VERSION = "2.5.0"


def audit_sql_literals() -> int:
    count = 0
    for path in ROOT.glob("app/**/*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany"} or not node.args:
                continue
            query_node = node.args[0]
            if not isinstance(query_node, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(query_node, ast.Constant):
                if not isinstance(query_node.value, str):
                    continue
                query = query_node.value
            else:
                parts: list[str] = []
                for value in query_node.values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        parts.append(value.value)
                    else:
                        parts.append("generated_fragment")
                query = "".join(parts)
            if "%s" not in query and "%" not in query:
                continue
            try:
                inspect_psycopg_placeholders(query)
            except SqlBindingError as exc:
                raise RuntimeError(f"Invalid SQL placeholder syntax in {path.relative_to(ROOT)}:{getattr(node,'lineno','?')}: {exc}") from exc
            count += 1
    return count


def audit_python_compile() -> None:
    subprocess.run([sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"], cwd=ROOT, check=True)


def audit_javascript() -> None:
    for relative in ("app/static/app.js", "app/static/phase1.js"):
        subprocess.run(["node", "--check", relative], cwd=ROOT, check=True)


def audit_static_sql_bindings() -> int:
    count = 0
    for path in ROOT.glob("app/**/*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "execute" or len(node.args) < 2:
                continue
            query_node, params_node = node.args[0], node.args[1]
            if not isinstance(query_node, ast.Constant) or not isinstance(query_node.value, str) or "%s" not in query_node.value:
                continue
            if not isinstance(params_node, (ast.Tuple, ast.List)):
                continue
            placeholders = len(inspect_psycopg_placeholders(query_node.value))
            values = len(params_node.elts)
            if placeholders != values:
                raise RuntimeError(
                    f"SQL binding count mismatch in {path.relative_to(ROOT)}:{getattr(node,'lineno','?')}: {placeholders} placeholders / {values} values"
                )
            count += 1
    return count


def audit_versions() -> None:
    if APP_VERSION != EXPECTED_APP_VERSION or SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(f"Version mismatch: app={APP_VERSION}, schema={SCHEMA_VERSION}")
    if DISCOVERY_ENGINE_VERSION != EXPECTED_DISCOVERY_VERSION:
        raise RuntimeError(f"Discovery engine mismatch: {DISCOVERY_ENGINE_VERSION}")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    worker = (ROOT / "app/worker.py").read_text(encoding="utf-8")
    if f'VERSION = "{EXPECTED_APP_VERSION}"' not in main or f'VERSION = "{EXPECTED_APP_VERSION}"' not in worker:
        raise RuntimeError("Main/worker application versions are not aligned")


def audit_raw_write_policy() -> None:
    write_re = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:public\.)?(rd_[a-zA-Z0-9_]+)", re.I)
    offenders: list[str] = []
    for path in [*ROOT.glob("app/**/*.py"), *ROOT.glob("sql/**/*.sql")]:
        text = path.read_text(encoding="utf-8")
        for match in write_re.finditer(text):
            offenders.append(f"{path.relative_to(ROOT)}:{match.group(1)}")
    if offenders:
        raise RuntimeError("Analysis application writes raw rd_ tables: " + ", ".join(sorted(set(offenders))))


def audit_schema() -> None:
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    migration_v2 = (ROOT / "sql/migrations/2.0.0.sql").read_text(encoding="utf-8")
    migration_pack = (ROOT / "sql/migrations/2.2.0.sql").read_text(encoding="utf-8")
    migration_robustness = (ROOT / "sql/migrations/2.3.0.sql").read_text(encoding="utf-8")
    migration_phase1 = (ROOT / "sql/migrations/2.5.0.sql").read_text(encoding="utf-8")
    v2_required = (
        "ra_discovery_samples", "ra_discovery_sample_chunks", "ra_discovery_task_chunks",
        "ra_discovery_partials", "ra_sealed_chunks", "sample_stride_minutes",
        "entry_stride_minutes", "entry_anchor_minute", "rule_definition_version",
    )
    for token in v2_required:
        if token not in schema or token not in migration_v2:
            raise RuntimeError(f"Discovery-v2 schema or migration is missing {token}")
    coverage_required = (
        "campaign_name", "campaign_definition_version", "defined_variant_count", "variant_count",
        "hypothesis_ids", "hypothesis_version", "p25_pct", "p75_pct", "p95_pct", "best_pct",
        "variants_tested_campaign", "p_value_raw", "p_value_bonferroni", "discovery_status",
    )
    for token in coverage_required:
        if token not in schema or token not in migration_pack:
            raise RuntimeError(f"Coverage-pack schema or migration is missing {token}")
    robustness_required = (
        "ra_robustness_runs", "ra_robustness_observations", "ra_robustness_results",
        "ra_robustness_chunks", "ra_robustness_samples", "engine_version", "signal_time_source",
        "opening_range_high", "opening_range_low", "opening_range_position", "touched_session_high",
        "touched_session_low", "fwd_return_60m_pct",
    )
    for token in robustness_required:
        if token not in schema or token not in migration_robustness:
            raise RuntimeError(f"Robustness-v2 schema or migration is missing {token}")
    phase1_required = (
        "ra_full_history_backfills", "ra_full_history_backfill_partitions",
        "ra_market_state_features", "ra_candidate_wave_stats",
        "ra_research_campaigns", "ra_research_ledger", "ra_research_periods",
        "ra_research_controls", "ra_jobs_research_period_guard",
        "ra_candidate_rules_frozen_guard", "ra_research_ledger_sealed_guard",
        "ra_ensure_market_state_partitions", "ra_ensure_candidate_wave_partitions",
    )
    for token in phase1_required:
        if token not in migration_phase1:
            raise RuntimeError(f"Phase-1 full-history migration is missing {token}")
    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    if "_apply_v250_full_history_migration(cur)" not in db_source:
        raise RuntimeError("Fresh/live schema paths do not apply the Phase-1 migration")
    if "CREATE TABLE IF NOT EXISTS rd_" in schema:
        raise RuntimeError("Schema creates raw rd_ tables")


def audit_blueprint() -> None:
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    if blueprint.count("- type:") != 2:
        raise RuntimeError("Render Blueprint must contain exactly two services")
    if blueprint.count("value: 3.12.7") != 2:
        raise RuntimeError("Both services must pin Python 3.12.7")


def audit_research_integrity_ui() -> None:
    html = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    phase1_javascript = (ROOT / "app/static/phase1.js").read_text(encoding="utf-8")
    pti_source = (ROOT / "app/point_in_time_universe.py").read_text(encoding="utf-8")
    for token in (
        'value="dip_repair"', 'value="compression_expansion"', 'value="gap_state"',
        'value="activity_absorption"', 'value="price_efficiency"',
        'value="new_high_liquidity_divergence"', 'id="robustness-dialog"',
        'id="sealed-feature-set"', 'id="discovery-campaign"',
    ):
        if token not in html:
            raise RuntimeError(f"Coverage-pack UI is missing {token}")
    for token in ("openRobustness", "round_trip_costs_bps", "target_feature_set_id", "campaign_name"):
        if token not in javascript:
            raise RuntimeError(f"Coverage-pack browser flow is missing {token}")
    if '/static/phase1.js' not in html:
        raise RuntimeError("Phase-1 browser extension is not loaded by index.html")
    for token in ('data-view="full-history"', 'id="view-full-history"', 'id="fh-backfill-status"', 'id="fh-infrastructure-status"'):
        if token not in phase1_javascript:
            raise RuntimeError(f"Phase-1 Full-History UI is missing {token}")
    for token in ("refreshFullHistory", "/api/full-history/status", "candidate-freeze", "Freeze in Research Ledger", "openFullHistoryView"):
        if token not in phase1_javascript:
            raise RuntimeError(f"Phase-1 browser flow is missing {token}")
    for token in ("inactive_known", "all_known_warmup_ready", "inactive_survivorship_ready", "Point-in-time historical source is not ready"):
        if token not in pti_source:
            raise RuntimeError(f"Point-in-time source gate is missing {token}")
    for token in ("fh-pti-ready", "All-known 61-day warm-up", "Inactive survivorship supplement", "Source blockers"):
        if token not in phase1_javascript:
            raise RuntimeError(f"Point-in-time source readiness UI is missing {token}")


def audit_secrets() -> None:
    suspicious = re.compile(r"(?:eyJ[a-zA-Z0-9_-]{30,}|AKIA[0-9A-Z]{16}|postgresql://[^\s:]+:[^@\s]{12,}@)")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".zip"} or ".git" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if suspicious.search(text) and "YOUR_PASSWORD" not in text and "postgres:postgres" not in text and "localhost" not in text:
            raise RuntimeError(f"Possible credential in {path.relative_to(ROOT)}")


def main() -> None:
    audit_versions()
    audit_schema()
    audit_blueprint()
    audit_raw_write_policy()
    audit_research_integrity_ui()
    audit_secrets()
    audit_python_compile()
    audit_javascript()
    sql_literals = audit_sql_literals()
    static_bindings = audit_static_sql_bindings()
    preflight = local_sql_preflight(exhaustive=True)
    print(
        "Release audit passed: "
        f"Python/JS syntax, {sql_literals} literal SQL statements, {static_bindings} static binding sites, "
        f"{preflight['queries']} generated production SQL checks, schema/migration tokens, raw-write policy, "
        "research-integrity UI, PTI source gate, secrets and blueprint."
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
import re
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEPENDENCY_MODE = "real"
try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
except ImportError:
    DEPENDENCY_MODE = "offline-stub"
    runpy.run_path(str(ROOT / "tests" / "conftest.py"))

from app.discovery import DISCOVERY_VERSION, RULE_DEFINITION_VERSION, STATISTICS_METHOD
from app.db import APP_VERSION, SCHEMA_VERSION
from app.preflight import local_sql_preflight
from app.sql_validation import SqlBindingError, inspect_psycopg_placeholders

EXPECTED_APP_VERSION = "2.2.0"
EXPECTED_DISCOVERY_VERSION = "2.2.0"
EXPECTED_SCHEMA_VERSION = "2.2.0"


def audit_sql_literals() -> int:
    checked = 0
    errors: list[str] = []
    for path in (ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany", "copy"} or not node.args:
                continue
            query = node.args[0]
            if isinstance(query, ast.Constant) and isinstance(query.value, str):
                checked += 1
                try:
                    inspect_psycopg_placeholders(query.value)
                except SqlBindingError as exc:
                    errors.append(f"{path.name}:{node.lineno}: {exc}")
    if errors:
        raise RuntimeError("Invalid literal SQL:\n" + "\n".join(errors))
    return checked


def audit_execute_parameter_counts() -> int:
    """Check literal cursor.execute SQL where the parameter tuple is statically visible."""
    checked = 0
    errors: list[str] = []
    for path in (ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
                continue
            if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            if not isinstance(node.args[1], (ast.Tuple, ast.List)):
                continue
            # Starred elements (for example ``(*common, threshold)``) are
            # dynamically sized and cannot be counted accurately from the AST.
            # Generated-query validation covers those paths separately.
            if any(isinstance(element, ast.Starred) for element in node.args[1].elts):
                continue
            report = inspect_psycopg_placeholders(node.args[0].value)
            expected = len(node.args[1].elts)
            checked += 1
            if report.placeholder_count != expected:
                errors.append(
                    f"{path.name}:{node.lineno}: {report.placeholder_count} placeholders but {expected} tuple elements"
                )
    if errors:
        raise RuntimeError("Literal execute binding mismatch:\n" + "\n".join(errors))
    return checked




def audit_on_conflict_update_targets() -> int:
    """Reject PostgreSQL UPSERTs that use DO UPDATE without a conflict target.

    PostgreSQL allows a target-less ``ON CONFLICT DO NOTHING``, but ``DO UPDATE``
    requires either ``(column, ...)`` or ``ON CONSTRAINT name``. This check is
    deliberately available offline so migration syntax is not dependent on CI.
    """
    checked = 0
    errors: list[str] = []
    paths = list((ROOT / "app").glob("*.py")) + [ROOT / "sql" / "schema.sql"]
    paths += list((ROOT / "sql" / "migrations").glob("*.sql"))
    invalid = re.compile(r"ON\s+CONFLICT\s+DO\s+UPDATE", re.I)
    valid_update = re.compile(
        r"ON\s+CONFLICT\s*(?:\([^)]*\)|ON\s+CONSTRAINT\s+[A-Za-z_][A-Za-z0-9_]*)\s+DO\s+UPDATE",
        re.I | re.S,
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"ON\s+CONFLICT\b", text, re.I):
            window = text[match.start():match.start() + 500]
            if re.search(r"DO\s+UPDATE", window, re.I):
                checked += 1
                if invalid.search(window) or not valid_update.search(window):
                    line = text[:match.start()].count("\n") + 1
                    errors.append(f"{path.relative_to(ROOT)}:{line}: ON CONFLICT DO UPDATE requires a target")
    if errors:
        raise RuntimeError("Invalid PostgreSQL UPSERT syntax:\n" + "\n".join(errors))
    return checked

def audit_raw_write_policy() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").glob("*.py"))
    forbidden = re.compile(r"\b(insert\s+into|update|delete\s+from|create\s+table|drop\s+table|alter\s+table)\s+rd_", re.I)
    if forbidden.search(text):
        raise RuntimeError("Raw rd_ write policy violated")


def audit_versions() -> None:
    for relative in ("app/main.py", "app/worker.py"):
        if EXPECTED_APP_VERSION not in (ROOT / relative).read_text(encoding="utf-8"):
            raise RuntimeError(f"{relative} does not contain app release version {EXPECTED_APP_VERSION}")
    if DISCOVERY_VERSION != EXPECTED_DISCOVERY_VERSION:
        raise RuntimeError(f"Discovery version mismatch: {DISCOVERY_VERSION}")
    if SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(f"Schema version mismatch: {SCHEMA_VERSION}")
    if APP_VERSION != EXPECTED_APP_VERSION:
        raise RuntimeError(f"DB migration app-version marker mismatch: {APP_VERSION}")
    if RULE_DEFINITION_VERSION != "2026-08-coverage-pack1-v1":
        raise RuntimeError(f"Unexpected rule definition: {RULE_DEFINITION_VERSION}")
    if "histogram" not in STATISTICS_METHOD:
        raise RuntimeError("v2 statistics method must identify its mergeable histogram")


def audit_schema() -> None:
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    migration_v2 = (ROOT / "sql/migrations/2.0.0.sql").read_text(encoding="utf-8")
    migration_pack = (ROOT / "sql/migrations/2.2.0.sql").read_text(encoding="utf-8")
    v2_required = (
        "ra_discovery_samples", "ra_discovery_sample_chunks", "ra_discovery_task_chunks",
        "ra_discovery_partials", "ra_sealed_chunks", "sample_stride_minutes",
        "fwd_return_60m_pct", "statistics_method", "engine_version",
    )
    for token in v2_required:
        if token not in schema or token not in migration_v2:
            raise RuntimeError(f"Base v2 schema or migration is missing {token}")
    pack_required = (
        "ra_robustness_runs", "ra_robustness_observations", "ra_robustness_results",
        "campaign_name", "hypothesis_ids", "variant_count", "defined_variant_count",
        "variants_tested_campaign", "variants_defined_campaign", "multiple_testing_adjusted_p",
        "discovery_p25_pct", "validation_p95_pct", "sealed_feature_set_id",
        "relative_trade_count_20bar", "activity_impact_change_ratio", "opening_range_position",
        "touched_session_high", "best_pct",
    )
    for token in pack_required:
        if token not in schema or token not in migration_pack:
            raise RuntimeError(f"Coverage-pack schema or migration is missing {token}")
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
    literal_queries = audit_sql_literals()
    static_bindings = audit_execute_parameter_counts()
    conflict_updates = audit_on_conflict_update_targets()
    preflight = local_sql_preflight()
    print(
        f"Release audit passed ({DEPENDENCY_MODE}): {literal_queries} literal SQL statements, "
        f"{static_bindings} static execute bindings, {conflict_updates} conflict-update statements, "
        f"{preflight['checks']} generated-query checks, definition {preflight['definition_hash'][:16]}"
    )


if __name__ == "__main__":
    main()

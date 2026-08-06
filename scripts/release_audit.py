from __future__ import annotations

import ast
import re
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# CI and Render use the real pinned dependencies. The execution sandbox used to
# assemble an archive may be offline; in that case install only the test import
# stubs so the static/generated-SQL release audit can still run. This mode does
# not replace the PostgreSQL-backed CI job.
DEPENDENCY_MODE = "real"
try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
except ImportError:
    DEPENDENCY_MODE = "offline-stub"
    runpy.run_path(str(ROOT / "tests" / "conftest.py"))

from app.discovery import DISCOVERY_VERSION, RULE_DEFINITION_VERSION
from app.features import FEATURE_VERSION
from app.preflight import local_sql_preflight
from app.sql_validation import SqlBindingError, inspect_psycopg_placeholders

EXPECTED_VERSION = "1.1.0"


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


def audit_raw_write_policy() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").glob("*.py"))
    forbidden = re.compile(
        r"\b(insert\s+into|update|delete\s+from|create\s+table|drop\s+table|alter\s+table)\s+rd_",
        re.I,
    )
    matches = forbidden.findall(text)
    if matches:
        raise RuntimeError(f"Raw rd_ write policy violated: {matches}")


def audit_versions() -> None:
    sources = {
        "main": (ROOT / "app/main.py").read_text(),
        "worker": (ROOT / "app/worker.py").read_text(),
        "db": (ROOT / "app/db.py").read_text(),
    }
    for name, source in sources.items():
        if EXPECTED_VERSION not in source:
            raise RuntimeError(f"{name} does not contain release version {EXPECTED_VERSION}")
    if DISCOVERY_VERSION != EXPECTED_VERSION:
        raise RuntimeError(f"Discovery version mismatch: {DISCOVERY_VERSION}")
    if FEATURE_VERSION != EXPECTED_VERSION:
        raise RuntimeError(f"Feature version mismatch: {FEATURE_VERSION}")
    if RULE_DEFINITION_VERSION == "legacy":
        raise RuntimeError("Rule definition cannot be legacy")


def audit_schema() -> None:
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    for column in (
        "entry_sampling_mode",
        "entry_stride_minutes",
        "entry_anchor_minute",
        "rule_definition_version",
    ):
        if column not in schema:
            raise RuntimeError(f"Schema is missing {column}")
    if "CREATE TABLE IF NOT EXISTS rd_" in schema:
        raise RuntimeError("Schema creates raw rd_ tables")
    if "DEFAULT '1.1.0'" not in schema:
        raise RuntimeError("Schema feature-version default is not 1.1.0")


def audit_blueprint() -> None:
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    if blueprint.count("- type:") != 2:
        raise RuntimeError("Render Blueprint must contain exactly two services")
    if blueprint.count("value: 3.12.7") != 2:
        raise RuntimeError("Both services must pin Python 3.12.7")


def audit_secrets() -> None:
    suspicious = re.compile(r"(?:eyJ[a-zA-Z0-9_-]{30,}|AKIA[0-9A-Z]{16}|postgresql://[^\s:]+:[^@\s]{12,}@)")
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or path.suffix in {".pyc", ".zip"}
            or ".git" in path.parts
            or "tests" in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if (
            suspicious.search(text)
            and "YOUR_PASSWORD" not in text
            and "postgres:postgres" not in text
            and "localhost" not in text
        ):
            raise RuntimeError(f"Possible credential in {path.relative_to(ROOT)}")


def main() -> None:
    audit_versions()
    audit_schema()
    audit_blueprint()
    audit_raw_write_policy()
    audit_secrets()
    literal_queries = audit_sql_literals()
    preflight = local_sql_preflight()
    print(
        f"Release audit passed ({DEPENDENCY_MODE}): {literal_queries} literal SQL statements, "
        f"{preflight['checks']} generated-query checks, definition {preflight['definition_hash'][:16]}"
    )


if __name__ == "__main__":
    main()

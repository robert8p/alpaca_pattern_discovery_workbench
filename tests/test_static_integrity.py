import ast
import re
from pathlib import Path

from app.sql_validation import SqlBindingError, inspect_psycopg_placeholders

ROOT = Path(__file__).resolve().parents[1]


def test_literal_sql_placeholder_counts_match_literal_parameter_tuples():
    issues = []
    for path in (ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany"} or len(node.args) < 2:
                continue
            query, params = node.args[0], node.args[1]
            if not isinstance(query, ast.Constant) or not isinstance(query.value, str):
                continue
            try:
                report = inspect_psycopg_placeholders(query.value)
            except SqlBindingError as exc:
                issues.append((path.name, node.lineno, str(exc)))
                continue
            if isinstance(params, ast.Tuple) and not any(isinstance(item, ast.Starred) for item in params.elts):
                if report.placeholder_count != len(params.elts):
                    issues.append((path.name, node.lineno, report.placeholder_count, len(params.elts)))
    assert not issues


def test_javascript_id_selectors_exist_and_html_ids_are_unique():
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    selectors = set(re.findall(r"\$\('#([^']+)'\)", javascript))
    assert len(ids) == len(set(ids))
    assert selectors <= set(ids)


def test_render_blueprint_pins_python_and_has_two_services():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert blueprint.count("- type:") == 2
    assert blueprint.count("value: 3.12.7") == 2
    assert "alpaca-pattern-workbench-web" in blueprint
    assert "alpaca-pattern-workbench-worker" in blueprint


def test_candidate_export_uses_browser_native_attachment_transport():
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert '<form class="top-actions candidate-export-form" id="candidate-export-form" action="/api/candidates/export" method="get">' in html
    assert 'name="discovery_run_id"' in html
    assert 'name="status_filter"' in html
    assert 'id="candidate-export-btn" type="submit"' in html
    for forbidden in ("downloadCandidateExport", "URL.createObjectURL", "response.blob()"):
        assert forbidden not in javascript


def test_feature_job_deletion_protects_robustness_and_sealed_provenance():
    main=(ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "Cannot delete this feature set because robustness evidence depends on it" in main
    assert "Cannot delete this feature set because sealed-test evidence depends on it" in main
    assert "target_feature_set_id" in main and "source_feature_set_id" in main


def test_coverage_pack_schema_tracks_actual_and_defined_variants():
    schema=(ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    migration=(ROOT / "sql" / "migrations" / "2.2.0.sql").read_text(encoding="utf-8")
    for token in ("variant_count", "defined_variant_count", "variants_tested_campaign", "variants_defined_campaign"):
        assert token in schema
        assert token in migration


def test_candidate_feature_picker_filters_for_frozen_definition_compatibility():
    javascript=(ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for token in (
        "compatibleFeatureSetsForCandidate", "universe_run_id!==source.universe_run_id",
        "liquidity_tiers", "time_of_day_baseline_days", "predictor_horizons_minutes",
        "outcome_horizons_minutes",
    ):
        assert token in javascript


def test_discovery_coverage_surfaces_integrity_limitations():
    main=(ROOT / "app" / "main.py").read_text(encoding="utf-8")
    javascript=(ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for token in ("historical_universe", "corporate_actions", "quotes", "market_sector"):
        assert token in main
    assert "Known integrity limitations" in javascript

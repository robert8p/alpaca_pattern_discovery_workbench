import ast
import re
from pathlib import Path

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
            if not isinstance(query, ast.Constant) or not isinstance(query.value, str) or not isinstance(params, ast.Tuple):
                continue
            if any(isinstance(item, ast.Starred) for item in params.elts):
                continue
            if query.value.count("%s") != len(params.elts):
                issues.append((path.name, node.lineno, query.value.count("%s"), len(params.elts)))
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

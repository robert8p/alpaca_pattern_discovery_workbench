from __future__ import annotations

import csv
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.exports import build_candidate_export_bundle, export_filename


def fixture_data():
    run_id, feature_id, universe_id, candidate_id = uuid4(), uuid4(), uuid4(), uuid4()
    candidates = [{
        "id": candidate_id,
        "discovery_run_id": run_id,
        "feature_set_id": feature_id,
        "family": "oversold_reversal",
        "direction": "long",
        "holding_horizon_minutes": 30,
        "conditions": {"ret_30m_bucket": "lt_minus_3"},
        "plain_english_rule": "Buy after a sharp 30-minute decline.",
        "rank_score": 7.5,
        "workflow_status": "new",
        "discovery_observations": 1000,
        "discovery_symbols": 80,
        "discovery_dates": 12,
        "discovery_net_avg_pct": 0.21,
        "validation_observations": 300,
        "validation_net_avg_pct": 0.14,
        "entry_sampling_mode": "non_overlapping",
        "entry_stride_minutes": 30,
        "entry_anchor_minute": 570,
        "rule_definition_version": "2026-08-staged-v2",
        "statistics_method": "mergeable_histogram",
        "engine_version": "2.0.0",
        "created_at": datetime(2026,8,7,tzinfo=UTC),
    }]
    runs = [{"id": run_id, "feature_set_id": feature_id, "name": "Initial interpretable rule scan", "config": {"round_trip_cost_bps": 20}, "candidates_tested": 10000, "candidates_retained": 30}]
    tasks = [{"discovery_run_id": run_id, "family": "oversold_reversal", "direction": "long", "holding_horizon_minutes": 30, "status": "completed", "groups_tested": 100, "candidates_retained": 5, "attempts": 1}]
    features = [{"id": feature_id, "universe_run_id": universe_id, "name": "June/July regular features", "config": {"liquidity_tiers": ["A","B"]}, "row_count": 123456}]
    universes = [{"id": universe_id, "name": "Liquid equities", "source_config": {"feed": "sip"}, "selection_config": {"maximum_symbols": 1500}, "included_symbols": 2}]
    symbols = [{"universe_run_id": universe_id, "symbol": "AAPL", "exchange": "NASDAQ", "asset_name": "Apple", "liquidity_tier": "A", "trading_days": 20, "average_bars_per_day": 390, "median_daily_dollar_volume": 1_000_000_000, "average_daily_dollar_volume": 1_100_000_000, "median_close": 200, "rank_by_liquidity": 1}]
    return candidates, runs, tasks, features, universes, symbols


def test_candidate_export_contains_analysis_package():
    candidates, runs, tasks, features, universes, symbols = fixture_data()
    payload = build_candidate_export_bundle(candidates=candidates, discovery_runs=runs, discovery_tasks=tasks, feature_sets=features, universes=universes, universe_symbols=symbols, filters={"status_filter": None}, app_version="2.2.0", exported_at=datetime(2026,8,7,11,0,tzinfo=UTC))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {"manifest.json","candidates.csv","candidates.json","discovery_runs.json","discovery_tasks.csv","feature_sets.json","universes.json","universe_symbols.csv","SUMMARY.md","README.txt","ANALYSIS_PROMPT.txt","robustness_runs.json","robustness_results.json"}
        manifest=json.loads(archive.read("manifest.json"))
        assert manifest["candidate_count"] == 1
        assert manifest["app_version"] == "2.2.0"
        assert manifest["export_format_version"] == "1.1"
        rows=list(csv.DictReader(io.StringIO(archive.read("candidates.csv").decode("utf-8-sig"))))
        assert rows[0]["discovery_run_name"] == "Initial interpretable rule scan"
        assert json.loads(rows[0]["conditions_json"])["ret_30m_bucket"] == "lt_minus_3"
        assert rows[0]["round_trip_cost_bps"] == "20"
        assert b"Top candidates" in archive.read("SUMMARY.md")
        assert b"promoting to a sealed test" in archive.read("ANALYSIS_PROMPT.txt")


def test_export_filename_uses_run_name_and_timestamp():
    _, runs, *_ = fixture_data()
    name=export_filename(runs, datetime(2026,8,7,11,2,3,tzinfo=UTC))
    assert name == "alpaca_initial-interpretable-rule-scan_20260807T110203Z.zip"


def test_candidate_tab_has_export_button_and_endpoint():
    root=Path(__file__).resolve().parents[1]
    html=(root/'app/templates/index.html').read_text()
    js=(root/'app/static/app.js').read_text()
    main=(root/'app/main.py').read_text()
    assert 'id="candidate-export-form"' in html
    assert 'action="/api/candidates/export"' in html
    assert 'method="get"' in html
    assert 'name="discovery_run_id"' in html
    assert 'name="status_filter"' in html
    assert 'id="candidate-export-btn" type="submit"' in html
    assert 'downloadCandidateExport' not in js
    assert 'URL.createObjectURL' not in js
    assert 'response.blob()' not in js
    assert '@app.get("/api/candidates/export")' in main


def test_export_endpoint_is_read_only_and_versions_are_consistent():
    root=Path(__file__).resolve().parents[1]
    main=(root/'app/main.py').read_text()
    worker=(root/'app/worker.py').read_text()
    db=(root/'app/db.py').read_text()
    assert 'VERSION = "2.2.0"' in main
    assert 'VERSION = "2.2.0"' in worker
    assert 'APP_VERSION = "2.2.0"' in db
    start=main.index('@app.get("/api/candidates/export")')
    end=main.index('@app.post("/api/candidates/{candidate_id}/actions/{action}")')
    block=main[start:end].lower()
    assert 'insert into' not in block
    assert 'update ' not in block
    assert 'delete from' not in block


def test_export_endpoint_returns_browser_attachment(monkeypatch):
    candidates, runs, tasks, features, universes, symbols = fixture_data()

    class FakeCursor:
        def __init__(self):
            self.rows = []
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def execute(self, query, params=None):
            sql = " ".join(str(query).split()).lower()
            if "from ra_candidate_rules" in sql:
                self.rows = candidates
            elif "from ra_discovery_runs" in sql:
                self.rows = runs
            elif "from ra_discovery_tasks" in sql:
                self.rows = tasks
            elif "from ra_feature_sets" in sql:
                self.rows = features
            elif "from ra_universe_runs" in sql:
                self.rows = universes
            elif "from ra_analysis_universe" in sql:
                self.rows = symbols
            elif "from ra_robustness_runs" in sql:
                self.rows = []
            elif "from ra_robustness_results" in sql:
                self.rows = []
            else:
                raise AssertionError(f"Unexpected export SQL: {query}")
        def fetchall(self):
            return self.rows

    class FakeConnection:
        def cursor(self):
            return FakeCursor()
        def rollback(self):
            pass

    @contextmanager
    def fake_connection():
        yield FakeConnection()

    import app.main as main
    monkeypatch.setattr(main, "connection", fake_connection)
    response = main.export_candidates(discovery_run_id=None, status_filter=None, _="test")
    assert response.status_code == 200
    assert response.media_type == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="alpaca_initial-interpretable-rule-scan_')
    assert response.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["candidate_count"] == 1
        assert manifest["app_version"] == "2.2.0"
        assert manifest["export_format_version"] == "1.1"

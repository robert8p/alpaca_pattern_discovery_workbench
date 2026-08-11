from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from app import worker


def test_point_in_time_universe_backfill_dispatches_without_feature_materialization(monkeypatch):
    feature_set_id = UUID("b9f2296a-8860-4737-93f2-763b78f5a217")
    universe_run_id = UUID("4ede8c55-7f81-4626-9834-7f7ed60f9728")
    calls = {}

    def fake_backfill_feature_config(model):
        calls["model"] = model
        return SimpleNamespace(universe_run_id=universe_run_id)

    def fake_ensure_point_in_time_universes(job_id, model, reference_universe_run_id):
        calls["job_id"] = job_id
        calls["reference_universe_run_id"] = reference_universe_run_id
        return {"ok": True, "mode": "universe_only"}

    def forbidden_feature_backfill(*args, **kwargs):
        raise AssertionError("universe-only dispatch must not materialize historical features")

    monkeypatch.setattr(worker, "backfill_feature_config", fake_backfill_feature_config)
    monkeypatch.setattr(worker, "ensure_point_in_time_universes", fake_ensure_point_in_time_universes)
    monkeypatch.setattr(worker, "run_historical_feature_backfill", forbidden_feature_backfill)

    job = {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "job_type": "point_in_time_universe_backfill",
        "config": {
            "name": "PTI universe-only broad history",
            "reference_feature_set_id": str(feature_set_id),
            "start_date": "2025-05-04",
            "end_date": "2026-08-03",
            "scope": "full_history",
        },
    }

    result = worker._dispatch(job)

    assert result == {"ok": True, "mode": "universe_only"}
    assert calls["job_id"] == str(job["id"])
    assert calls["reference_universe_run_id"] == universe_run_id
    assert calls["model"].reference_feature_set_id == feature_set_id

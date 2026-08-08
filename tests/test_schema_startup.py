from pathlib import Path


def test_startup_schema_uses_version_marker_and_targeted_migration():
    source = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text(encoding="utf-8")
    assert "ra_schema_versions" in source
    assert "_schema_is_compatible" in source
    assert "startup DDL skipped" in source
    assert "already_applied and _schema_is_compatible(cur)" in source
    assert "_apply_v200_discovery_migration" in source
    assert "_apply_v220_coverage_migration" in source
    assert "_apply_v230_robustness_migration" in source
    assert 'sql" / "migrations" / "2.5.0.sql' in source
    assert "sql\" / \"migrations\" / \"2.0.0.sql" in source
    assert "Replaying the full" in source


def test_schema_version_upsert_has_explicit_conflict_target():
    source = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (version) DO UPDATE" in source
    assert "ON CONFLICT DO UPDATE SET app_version" not in source
    assert 'APP_VERSION = "2.5.0"' in source
    assert "(SCHEMA_VERSION, APP_VERSION)" in source


def test_all_conflict_updates_name_a_target():
    import re
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "app").glob("*.py")) + [root / "sql" / "schema.sql"]
    paths += list((root / "sql" / "migrations").glob("*.sql"))
    invalid = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"ON\s+CONFLICT\b", text, re.I):
            window = text[match.start():match.start() + 500]
            if re.search(r"DO\s+UPDATE", window, re.I):
                valid = re.search(
                    r"ON\s+CONFLICT\s*(?:\([^)]*\)|ON\s+CONSTRAINT\s+[A-Za-z_][A-Za-z0-9_]*)\s+DO\s+UPDATE",
                    window, re.I | re.S
                )
                if not valid:
                    invalid.append(f"{path.name}:{text[:match.start()].count(chr(10))+1}")
    assert invalid == []


def test_schema_compatibility_requires_full_coverage_pack_surface():
    source=Path("app/db.py").read_text(encoding="utf-8")
    for token in (
        "campaign_definition_version", "variants_tested_campaign", "validation_best_pct",
        "activity_impact_change_ratio", "touched_session_low", "ra_robustness_observations",
        "ra_jobs_job_type_check", "robustness_analysis", "ra_robustness_chunks", "ra_robustness_samples",
    ):
        assert token in source

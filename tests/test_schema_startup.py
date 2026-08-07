from pathlib import Path


def test_startup_schema_uses_version_marker_and_targeted_migration():
    source = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text(encoding="utf-8")
    assert "ra_schema_versions" in source
    assert "_schema_is_compatible" in source
    assert "startup DDL skipped" in source
    assert "already_applied and _schema_is_compatible(cur)" in source
    assert "_apply_v200_discovery_migration" in source
    assert "sql\" / \"migrations\" / \"2.0.0.sql" in source
    assert "Replaying the full" in source

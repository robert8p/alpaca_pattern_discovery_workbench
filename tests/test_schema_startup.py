from pathlib import Path


def test_startup_schema_uses_version_marker_and_compatibility_short_circuit():
    source = (Path(__file__).resolve().parents[1] / "app" / "db.py").read_text(encoding="utf-8")
    assert "ra_schema_versions" in source
    assert "_schema_is_compatible" in source
    assert "startup DDL skipped" in source
    assert "pg_advisory_xact_lock" in source


def test_feature_partition_creation_is_once_per_chunk_not_per_batch():
    source = (Path(__file__).resolve().parents[1] / "app" / "features.py").read_text(encoding="utf-8")
    build_batch = source.split("def _build_batch", 1)[1].split("def _is_statement_timeout", 1)[0]
    assert "ra_ensure_feature_partitions" not in build_batch
    assert "def _ensure_partitions_for_chunk" in source
    assert "ra_feature_partition_ddl" in source

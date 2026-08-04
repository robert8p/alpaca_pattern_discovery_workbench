import re
from pathlib import Path


def test_application_never_writes_to_rd_tables():
    root = Path(__file__).resolve().parents[1]
    text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "app").glob("*.py"))
    forbidden = re.compile(r"\b(insert\s+into|update|delete\s+from|create\s+table|drop\s+table|alter\s+table)\s+rd_", re.I)
    matches = forbidden.findall(text)
    assert not matches


def test_schema_only_creates_ra_objects():
    root = Path(__file__).resolve().parents[1]
    schema = (root / "sql" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS rd_" not in schema
    assert "CREATE TABLE IF NOT EXISTS ra_jobs" in schema

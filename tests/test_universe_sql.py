from pathlib import Path


def test_nullable_regex_parameters_are_explicitly_text_typed():
    source = (Path(__file__).resolve().parents[1] / "app" / "universe.py").read_text(encoding="utf-8")
    assert "WHEN %s::text IS NOT NULL AND NOT (s.symbol ~ %s::text)" in source
    assert "WHEN %s::text IS NOT NULL AND s.symbol ~ %s::text" in source
    assert "WHEN %s IS NOT NULL AND NOT (s.symbol ~ %s)" not in source

from datetime import date
from pathlib import Path

from app.history_backfill import _uncovered_date_ranges


def test_uncovered_date_ranges_excludes_completed_bootstrap_chunk():
    assert _uncovered_date_ranges(
        date(2025, 5, 27),
        date(2025, 6, 1),
        [(date(2025, 5, 27), date(2025, 5, 27))],
    ) == [(date(2025, 5, 28), date(2025, 6, 1))]


def test_uncovered_date_ranges_handles_multiple_existing_chunks():
    assert _uncovered_date_ranges(
        date(2025, 5, 27),
        date(2025, 6, 5),
        [
            (date(2025, 5, 27), date(2025, 5, 27)),
            (date(2025, 5, 28), date(2025, 5, 30)),
            (date(2025, 6, 2), date(2025, 6, 3)),
        ],
    ) == [
        (date(2025, 5, 31), date(2025, 6, 1)),
        (date(2025, 6, 4), date(2025, 6, 5)),
    ]


def test_backfill_record_grows_existing_month_status_and_range():
    source = (Path(__file__).resolve().parents[1] / "app" / "history_backfill.py").read_text(encoding="utf-8")
    assert "requested_start=%s,requested_end=%s" in source
    assert "months_available=%s" in source
    assert "partition_start=LEAST(partition_start,%s),partition_end=GREATEST(partition_end,%s)" in source
    assert "research_stage=%s,status='pending',completed_at=NULL,error=NULL" in source
    assert "_uncovered_date_ranges(seg_start, seg_end, covered_chunks)" in source
    assert "RETURNING id" in source

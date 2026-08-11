from __future__ import annotations

from datetime import date
from pathlib import Path

from app.point_in_time_universe import PTI_LOOKBACK_CALENDAR_DAYS, PTI_UNIVERSE_VERSION, _covers_range


def test_pti_source_gate_preserves_existing_61_calendar_day_methodology():
    assert PTI_LOOKBACK_CALENDAR_DAYS == 61
    assert PTI_UNIVERSE_VERSION == "1.1.0"
    first_research_trade = date(2025, 5, 5)
    assert first_research_trade - __import__('datetime').timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) == date(2025, 3, 5)


def test_cover_range_accepts_contiguous_completed_source_jobs():
    intervals = [
        (date(2025, 3, 5), date(2025, 4, 10)),
        (date(2025, 4, 11), date(2025, 5, 2)),
    ]
    assert _covers_range(intervals, date(2025, 3, 5), date(2025, 5, 2)) is True


def test_cover_range_rejects_even_one_calendar_day_gap():
    intervals = [
        (date(2025, 3, 5), date(2025, 4, 9)),
        (date(2025, 4, 11), date(2025, 5, 2)),
    ]
    assert _covers_range(intervals, date(2025, 3, 5), date(2025, 5, 2)) is False


def test_cover_range_handles_overlaps_and_supersets():
    intervals = [
        (date(2025, 3, 1), date(2025, 4, 20)),
        (date(2025, 4, 1), date(2025, 5, 10)),
    ]
    assert _covers_range(intervals, date(2025, 3, 5), date(2025, 5, 2)) is True


def test_full_history_code_requires_all_three_source_components():
    source = (Path(__file__).resolve().parents[1] / "app" / "point_in_time_universe.py").read_text(encoding="utf-8")
    for token in (
        '"all_active"',
        '"all_known"',
        '"inactive_known"',
        'active_history_ready',
        'all_known_warmup_ready',
        'inactive_survivorship_ready',
        'Point-in-time historical source is not ready',
    ):
        assert token in source


def test_full_history_status_and_ui_expose_source_gate():
    root = Path(__file__).resolve().parents[1]
    status_source = (root / "app" / "full_history.py").read_text(encoding="utf-8")
    ui = (root / "app" / "static" / "phase1.js").read_text(encoding="utf-8")
    assert '"point_in_time_source_readiness"' in status_source
    for token in ('fh-pti-ready', 'All-known 61-day warm-up', 'Inactive survivorship supplement', 'Source blockers'):
        assert token in ui

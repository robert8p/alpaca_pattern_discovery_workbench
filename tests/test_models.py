from datetime import date
from uuid import uuid4

import pytest

from app.features import date_chunks
from app.models import DiscoveryConfig, FeatureBuildConfig, UniverseBuildConfig


def test_date_chunks_cover_period_without_overlap():
    chunks = date_chunks(date(2026, 7, 4), date(2026, 7, 10), 3)
    assert chunks == [
        (date(2026, 7, 4), date(2026, 7, 6)),
        (date(2026, 7, 7), date(2026, 7, 9)),
        (date(2026, 7, 10), date(2026, 7, 10)),
    ]


def test_feature_horizons_must_fit_candle_size():
    with pytest.raises(ValueError):
        FeatureBuildConfig(
            name="Bad horizons", universe_run_id=uuid4(), start_date="2026-07-01",
            end_date="2026-07-02", timeframe="5Min",
            predictor_horizons_minutes=[1, 5], outcome_horizons_minutes=[5],
        )


def test_validation_must_follow_discovery():
    with pytest.raises(ValueError):
        DiscoveryConfig(
            name="Overlap", feature_set_id=uuid4(), discovery_start="2026-07-01",
            discovery_end="2026-07-20", validation_start="2026-07-20",
            validation_end="2026-07-25",
        )


def test_universe_regex_is_validated():
    with pytest.raises(ValueError):
        UniverseBuildConfig(
            name="Bad regex", start_date="2026-07-01", end_date="2026-07-02",
            include_symbol_regex="[",
        )

from datetime import date
from uuid import uuid4

import pytest

from app.features import date_chunks
from app.models import DiscoveryConfig, FeatureBuildConfig, RobustnessAnalysisConfig, SealedEvaluationConfig, UniverseBuildConfig


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


def test_feature_symbol_batch_size_defaults_for_legacy_jobs():
    config = FeatureBuildConfig(
        name="Legacy feature job", universe_run_id=uuid4(), start_date="2026-07-01",
        end_date="2026-07-02", timeframe="1Min",
    )
    assert config.symbol_batch_size == 100


def test_discovery_defaults_to_non_overlapping_entries():
    from app.models import DiscoveryConfig

    cfg = DiscoveryConfig.model_validate({
        "feature_set_id": "00000000-0000-0000-0000-000000000001",
        "discovery_start": "2026-07-01",
        "discovery_end": "2026-07-10",
        "directions": ["long"],
        "holding_horizons_minutes": [15],
        "families": ["time_of_day"],
    })
    assert cfg.entry_sampling_mode == "non_overlapping"


def test_expanded_discovery_families_and_campaign_are_accepted():
    cfg = DiscoveryConfig.model_validate({
        "feature_set_id": str(uuid4()), "discovery_start": "2026-07-01", "discovery_end": "2026-07-10",
        "directions": ["long","short"], "holding_horizons_minutes": [30],
        "families": ["dip_repair","compression_expansion","gap_state","activity_absorption","price_efficiency","new_high_liquidity_divergence"],
        "campaign_name": "Coverage pack test",
    })
    assert cfg.campaign_name == "Coverage pack test"
    assert len(cfg.families) == 6


def test_robustness_requires_zero_delay_and_complete_date_pair():
    cid=uuid4()
    cfg=RobustnessAnalysisConfig(candidate_id=cid, round_trip_costs_bps=[20,30], entry_delays_minutes=[0,2,5])
    assert cfg.entry_delays_minutes == [0,2,5]
    with pytest.raises(ValueError):
        RobustnessAnalysisConfig(candidate_id=cid, entry_delays_minutes=[1,2])
    with pytest.raises(ValueError):
        RobustnessAnalysisConfig(candidate_id=cid, start_date="2026-01-01")


def test_sealed_can_target_another_feature_set():
    cfg=SealedEvaluationConfig(candidate_id=uuid4(), target_feature_set_id=uuid4(), sealed_start="2026-08-04", sealed_end="2026-08-06")
    assert cfg.target_feature_set_id is not None

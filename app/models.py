from __future__ import annotations

from datetime import date
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

Timeframe = Literal["1Min", "2Min", "3Min", "5Min", "10Min", "15Min", "20Min", "30Min", "45Min", "1Hour"]
Feed = Literal["sip", "iex", "boats", "otc"]
Adjustment = Literal["raw", "split", "dividend", "all"]
Session = Literal["regular", "premarket", "postmarket", "overnight", "all"]


class SourceSelection(BaseModel):
    start_date: date
    end_date: date
    timeframe: Timeframe = "1Min"
    feed: Feed = "sip"
    adjustment: Adjustment = "raw"
    session: Session = "regular"

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after start date")
        return self


class QualityScanConfig(SourceSelection):
    name: str = Field(default="Data quality scan", min_length=3, max_length=120)
    complete_day_bar_tolerance_pct: float = Field(default=95.0, ge=50.0, le=100.0)
    minimum_symbol_days: int = Field(default=5, ge=1, le=3000)


class UniverseBuildConfig(SourceSelection):
    name: str = Field(default="Liquidity-ranked universe", min_length=3, max_length=120)
    minimum_trading_days: int = Field(default=15, ge=1, le=3000)
    minimum_average_bars_per_day: float = Field(default=300.0, ge=1.0, le=1500.0)
    minimum_median_daily_dollar_volume: float = Field(default=5_000_000, ge=0)
    minimum_median_close: float = Field(default=1.0, ge=0)
    maximum_symbols: int | None = Field(default=1500, ge=1, le=20000)
    include_symbol_regex: str | None = None
    exclude_symbol_regex: str | None = r"[/]"
    exclude_likely_funds: bool = True
    tier_a_min_bars_per_day: float = Field(default=380.0, ge=1, le=1500)
    tier_a_min_dollar_volume: float = Field(default=100_000_000, ge=0)
    tier_b_min_bars_per_day: float = Field(default=350.0, ge=1, le=1500)
    tier_b_min_dollar_volume: float = Field(default=20_000_000, ge=0)
    tier_c_min_bars_per_day: float = Field(default=300.0, ge=1, le=1500)
    tier_c_min_dollar_volume: float = Field(default=5_000_000, ge=0)


    @model_validator(mode="after")
    def regexes_are_valid(self):
        for pattern in (self.include_symbol_regex, self.exclude_symbol_regex):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid symbol regular expression: {exc}") from exc
        return self


class FeatureBuildConfig(SourceSelection):
    name: str = Field(default="Intraday feature set", min_length=3, max_length=120)
    universe_run_id: UUID
    liquidity_tiers: list[Literal["A", "B", "C", "D"]] = Field(default_factory=lambda: ["A", "B"])
    date_chunk_days: int = Field(default=3, ge=1, le=31)
    symbol_batch_size: int = Field(default=100, ge=10, le=500)
    time_of_day_baseline_days: int = Field(default=10, ge=2, le=60)
    predictor_horizons_minutes: list[int] = Field(default_factory=lambda: [1, 5, 15, 30, 60])
    outcome_horizons_minutes: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    conflict_policy: Literal["replace_slice", "skip_existing"] = "replace_slice"

    @field_validator("predictor_horizons_minutes", "outcome_horizons_minutes")
    @classmethod
    def allowed_horizons(cls, values: list[int]) -> list[int]:
        allowed = {1, 5, 15, 30, 60}
        cleaned = sorted(set(values))
        if not cleaned or any(value not in allowed for value in cleaned):
            raise ValueError("Horizons must be selected from 1, 5, 15, 30 and 60 minutes")
        return cleaned

    @model_validator(mode="after")
    def horizons_fit_timeframe(self):
        minutes = timeframe_minutes(self.timeframe)
        invalid = [h for h in set(self.predictor_horizons_minutes + self.outcome_horizons_minutes) if h % minutes]
        if invalid:
            raise ValueError(f"Horizons {invalid} are not divisible by the {self.timeframe} candle size")
        return self


class DiscoveryConfig(BaseModel):
    name: str = Field(default="Pattern discovery scan", min_length=3, max_length=120)
    feature_set_id: UUID
    discovery_start: date
    discovery_end: date
    validation_start: date | None = None
    validation_end: date | None = None
    directions: list[Literal["long", "short"]] = Field(default_factory=lambda: ["long", "short"])
    holding_horizons_minutes: list[Literal[5, 15, 30, 60]] = Field(default_factory=lambda: [30, 60])
    families: list[Literal[
        "time_of_day", "oversold_reversal", "momentum_continuation",
        "vwap_reversion", "gap_behavior", "volume_shock"
    ]] = Field(default_factory=lambda: [
        "time_of_day", "oversold_reversal", "momentum_continuation",
        "vwap_reversion", "gap_behavior", "volume_shock"
    ])
    round_trip_cost_bps: float = Field(default=20.0, ge=0, le=1000)
    minimum_observations: int = Field(default=250, ge=20, le=1_000_000)
    minimum_symbols: int = Field(default=20, ge=1, le=20000)
    minimum_dates: int = Field(default=10, ge=2, le=3000)
    maximum_symbol_concentration_pct: float = Field(default=15.0, ge=1, le=100)
    maximum_date_concentration_pct: float = Field(default=20.0, ge=1, le=100)
    top_candidates_per_family: int = Field(default=30, ge=1, le=200)
    entry_sampling_mode: Literal["non_overlapping", "all_bars"] = "non_overlapping"
    date_chunk_days: int = Field(default=3, ge=1, le=14)
    symbol_shards: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def periods_are_valid(self):
        if self.discovery_end < self.discovery_start:
            raise ValueError("Discovery end must be on or after discovery start")
        if (self.validation_start is None) != (self.validation_end is None):
            raise ValueError("Provide both validation dates or neither")
        if self.validation_start and self.validation_end:
            if self.validation_end < self.validation_start:
                raise ValueError("Validation end must be on or after validation start")
            if self.validation_start <= self.discovery_end:
                raise ValueError("Validation must begin after the discovery period")
        if not self.directions or not self.holding_horizons_minutes or not self.families:
            raise ValueError("Select at least one direction, holding horizon and discovery family")
        return self


class SealedEvaluationConfig(BaseModel):
    candidate_id: UUID
    sealed_start: date
    sealed_end: date

    @model_validator(mode="after")
    def dates_are_valid(self):
        if self.sealed_end < self.sealed_start:
            raise ValueError("Sealed end must be on or after sealed start")
        return self


class JobCreateRequest(BaseModel):
    job_type: Literal["quality_scan", "universe_build", "feature_build", "discovery_scan", "sealed_evaluation"]
    config: dict


class FeatureEstimateRequest(BaseModel):
    config: FeatureBuildConfig


def timeframe_minutes(timeframe: str) -> int:
    if timeframe.endswith("Min"):
        return int(timeframe[:-3])
    if timeframe == "1Hour":
        return 60
    raise ValueError(f"Unsupported feature timeframe: {timeframe}")

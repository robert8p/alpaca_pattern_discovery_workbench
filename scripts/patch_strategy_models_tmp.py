from pathlib import Path

p = Path('app/models.py')
s = p.read_text()
marker = '\n\nclass CandidateFreezeRequest(BaseModel):\n'
if 'class StrategyEconomicsConfig(BaseModel):' not in s:
    if marker not in s:
        raise SystemExit('CandidateFreezeRequest marker not found')
    block = r'''

class StrategyEconomicsConfig(BaseModel):
    name: str = Field(default="Whole-strategy economics", min_length=3, max_length=120)
    candidate_id: UUID
    target_feature_set_id: UUID
    mode: Literal["research", "sealed"] = "research"
    research_stage: Literal["discovery", "validation", "research_confirmation", "custom_presealed", "sealed_holdout"] = "custom_presealed"
    start_date: date
    end_date: date
    strategy_config_hash: str | None = None

    capital_levels: list[float] = Field(default_factory=lambda: [10_000.0, 50_000.0, 100_000.0])
    base_entry_delay_minutes: int = Field(default=0, ge=0, le=30)
    entry_delays_minutes: list[int] = Field(default_factory=lambda: [0, 1, 2, 5])
    base_round_trip_cost_bps: float = Field(default=20.0, ge=0, le=1000)
    round_trip_costs_bps: list[float] = Field(default_factory=lambda: [20.0, 25.0, 30.0, 40.0])
    commission_bps: float = Field(default=0.0, ge=0, le=1000)
    spread_bps: float = Field(default=0.0, ge=0, le=1000)
    slippage_bps: float = Field(default=0.0, ge=0, le=1000)
    market_impact_bps: float = Field(default=0.0, ge=0, le=1000)
    borrow_bps: float = Field(default=0.0, ge=0, le=1000)
    funding_bps: float = Field(default=0.0, ge=0, le=1000)

    position_sizing_method: Literal["fixed_fraction"] = "fixed_fraction"
    position_size_pct_of_capital: float = Field(default=5.0, gt=0, le=100)
    max_positions: int = Field(default=20, ge=1, le=500)
    max_gross_exposure_pct: float = Field(default=100.0, gt=0, le=1000)
    max_net_exposure_pct: float = Field(default=100.0, gt=0, le=1000)
    max_symbol_exposure_pct: float = Field(default=10.0, gt=0, le=100)
    max_sector_exposure_pct: float | None = Field(default=None, gt=0, le=100)
    signal_priority: Literal["liquidity_desc", "signal_strength_desc", "symbol"] = "liquidity_desc"
    one_position_per_symbol: bool = True
    allow_partial_fills: bool = True
    min_fill_fraction: float = Field(default=0.50, gt=0, le=1)
    max_bar_participation_pct: float = Field(default=1.0, gt=0, le=100)
    max_daily_participation_pct: float = Field(default=0.10, gt=0, le=100)
    signal_strength_field: Literal[
        "ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "ret_60m_pct",
        "relative_volume_20bar", "relative_trade_count_20bar", "activity_impact_change_ratio",
        "distance_from_cumulative_vwap_pct", "cumulative_range_position"
    ] | None = None
    breakeven_tolerance_bps: float = Field(default=1.0, ge=0, le=100)
    large_loss_threshold_pct: float = Field(default=-2.0, ge=-100, le=0)
    max_acceptable_drawdown_pct: float = Field(default=20.0, gt=0, le=100)

    @field_validator("capital_levels")
    @classmethod
    def valid_capital_levels(cls, values: list[float]) -> list[float]:
        cleaned = sorted(set(float(v) for v in values))
        if not cleaned or any(v <= 0 for v in cleaned):
            raise ValueError("Capital levels must contain positive amounts")
        return cleaned

    @field_validator("round_trip_costs_bps")
    @classmethod
    def valid_strategy_costs(cls, values: list[float]) -> list[float]:
        cleaned = sorted(set(float(v) for v in values))
        required = {20.0, 25.0, 30.0, 40.0}
        if not required.issubset(set(cleaned)):
            raise ValueError("Whole-strategy stress costs must preserve 20, 25, 30 and 40 bps")
        if any(v < 0 or v > 1000 for v in cleaned):
            raise ValueError("Strategy cost stress values must be between 0 and 1000 bps")
        return cleaned

    @field_validator("entry_delays_minutes")
    @classmethod
    def valid_strategy_delays(cls, values: list[int]) -> list[int]:
        cleaned = sorted(set(int(v) for v in values))
        if not {0, 1, 2, 5}.issubset(set(cleaned)):
            raise ValueError("Whole-strategy latency stress must preserve 0, 1, 2 and 5 minute delays")
        if any(v < 0 or v > 30 for v in cleaned):
            raise ValueError("Strategy entry delays must be between 0 and 30 minutes")
        return cleaned

    @model_validator(mode="after")
    def whole_strategy_integrity(self):
        if self.end_date < self.start_date:
            raise ValueError("Strategy economics end date must be on or after start date")
        if self.base_entry_delay_minutes not in self.entry_delays_minutes:
            raise ValueError("Base entry delay must be included in the latency-stress grid")
        if self.base_round_trip_cost_bps not in self.round_trip_costs_bps:
            raise ValueError("Base round-trip cost must be included in the cost-stress grid")
        components = self.commission_bps + self.spread_bps + self.slippage_bps + self.market_impact_bps + self.borrow_bps + self.funding_bps
        if components > self.base_round_trip_cost_bps + 1e-9:
            raise ValueError("Explicit cost components may not exceed the declared base round-trip cost")
        if self.max_sector_exposure_pct is not None:
            raise ValueError("Sector exposure limits require point-in-time sector metadata; leave unset until that data is available")
        if self.mode == "sealed":
            if self.research_stage != "sealed_holdout":
                raise ValueError("Sealed strategy economics must use research_stage=sealed_holdout")
            if self.start_date < SEALED_START_DATE:
                raise ValueError(f"Sealed whole-strategy evaluation may not begin before {SEALED_START_DATE}")
            if not self.strategy_config_hash:
                raise ValueError("Sealed whole-strategy evaluation requires the frozen strategy_config_hash")
        else:
            if self.research_stage == "sealed_holdout":
                raise ValueError("Research mode may not use the sealed_holdout stage")
            if self.end_date >= SEALED_START_DATE:
                raise ValueError(f"Strategy research may not include the sealed holdout beginning {SEALED_START_DATE}")
            if self.research_stage == "discovery" and not (self.start_date >= date(2025, 5, 4) and self.end_date <= date(2026, 2, 28)):
                raise ValueError("Discovery strategy economics must stay within 4 May 2025 to 28 February 2026")
            if self.research_stage == "validation" and not (self.start_date >= date(2026, 3, 1) and self.end_date <= date(2026, 5, 31)):
                raise ValueError("Validation strategy economics must stay within 1 March to 31 May 2026")
            if self.research_stage == "research_confirmation" and not (self.start_date >= date(2026, 6, 1) and self.end_date <= date(2026, 8, 3)):
                raise ValueError("Research-confirmation strategy economics must stay within 1 June to 3 August 2026")
        return self
'''
    s = s.replace(marker, block + marker, 1)

old = '''        "historical_feature_backfill", "market_state_build", "candidate_wave_build"\n'''
new = '''        "historical_feature_backfill", "point_in_time_universe_backfill", "market_state_build", "candidate_wave_build",\n        "strategy_economics_analysis", "strategy_combination_analysis"\n'''
if '"strategy_economics_analysis"' not in s:
    if old not in s:
        raise SystemExit('JobCreateRequest job type marker not found')
    s = s.replace(old, new, 1)

p.write_text(s)

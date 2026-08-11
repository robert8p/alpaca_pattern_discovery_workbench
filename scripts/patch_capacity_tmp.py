from pathlib import Path

p=Path('app/strategy_economics.py')
s=p.read_text()
old='''               a.exchange,COALESCE(a.attributes->>'sector',a.raw->>'sector') AS sector,
               CASE WHEN a.raw ? 'shortable' THEN NULLIF(a.raw->>'shortable','')::boolean END AS current_reference_shortable,
               (d.volume*COALESCE(d.vwap,d.close))::double precision AS daily_dollar_volume,
               {path_cols}
        FROM signal s
        LEFT JOIN ra_intraday_features en
'''
new='''               a.exchange,COALESCE(a.attributes->>'sector',a.raw->>'sector') AS sector,
               CASE WHEN a.raw ? 'shortable' THEN NULLIF(a.raw->>'shortable','')::boolean END AS current_reference_shortable,
               CASE WHEN pts.status='completed' AND pts.lookback_end < s.trade_date THEN au.median_daily_dollar_volume END::double precision AS daily_dollar_volume,
               CASE WHEN pts.status='completed' AND pts.lookback_end < s.trade_date THEN 'point_in_time_universe_t_minus_1' ELSE 'entry_bar_only_no_point_in_time_daily_capacity' END AS liquidity_metadata_temporal_status,
               {path_cols}
        FROM signal s
        LEFT JOIN ra_intraday_features en
'''
if old not in s: raise SystemExit('signal select capacity marker not found')
s=s.replace(old,new,1)
old='''        LEFT JOIN rd_assets a ON a.symbol=s.symbol
        LEFT JOIN rd_daily_features d
          ON d.symbol=s.symbol AND d.trade_date=s.trade_date AND d.timeframe='1Min'
         AND d.feed='sip' AND d.adjustment='raw' AND d.session_label='regular'
        {path_join}
'''
new='''        LEFT JOIN rd_assets a ON a.symbol=s.symbol
        LEFT JOIN ra_feature_chunks fc
          ON fc.feature_set_id=s.feature_set_id AND s.trade_date BETWEEN fc.chunk_start AND fc.chunk_end
        LEFT JOIN ra_feature_chunk_universes fcu ON fcu.feature_chunk_id=fc.id
        LEFT JOIN ra_point_in_time_universe_snapshots pts
          ON pts.id=fcu.point_in_time_snapshot_id AND s.trade_date BETWEEN pts.effective_start AND pts.effective_end
        LEFT JOIN ra_analysis_universe au
          ON au.universe_run_id=fcu.universe_run_id AND au.symbol=s.symbol AND au.included
        {path_join}
'''
if old not in s: raise SystemExit('same-day daily feature join marker not found')
s=s.replace(old,new,1)
old='''    metadata_status = "current_reference_structural_metadata"

    def priority(row: dict[str, Any]) -> tuple[Any, ...]:
'''
new='''    def priority(row: dict[str, Any]) -> tuple[Any, ...]:
'''
if old not in s: raise SystemExit('simulation metadata constant marker not found')
s=s.replace(old,new,1)
old='''        row = dict(signal)
        row.update({"accepted": False, "rejection_reason": None, "metadata_temporal_status": metadata_status})
'''
new='''        row = dict(signal)
        row.update({"accepted": False, "rejection_reason": None, "metadata_temporal_status": signal.get("liquidity_metadata_temporal_status") or "entry_bar_only_no_point_in_time_daily_capacity"})
'''
if old not in s: raise SystemExit('simulation row metadata marker not found')
s=s.replace(old,new,1)
# Explicitly document the no-lookahead capacity rule in the output.
old='''            "historical_bid_ask":"Minute SIP bars do not contain quote-level spread/depth; configured spread/slippage/impact assumptions are explicit proxies.",
'''
new='''            "historical_bid_ask":"Minute SIP bars do not contain quote-level spread/depth; configured spread/slippage/impact assumptions are explicit proxies.",
            "capacity_information_timing":"Entry-bar dollar volume is observable at entry. Daily-capacity limits use only point-in-time universe median daily dollar volume whose lookback ended before the trade date; when unavailable the engine falls back to entry-bar capacity only and does not use same-day future volume.",
'''
if old not in s: raise SystemExit('execution limitations marker not found')
s=s.replace(old,new,1)
p.write_text(s)

p=Path('tests/test_strategy_economics.py')
s=p.read_text()
old='''    assert "ra_intraday_features en" in lower and "ra_intraday_features ex" in lower
    assert len(params) > 8
'''
new='''    assert "ra_intraday_features en" in lower and "ra_intraday_features ex" in lower
    assert "rd_daily_features" not in lower
    assert "ra_feature_chunk_universes" in lower
    assert "pts.lookback_end < s.trade_date" in lower
    assert len(params) > 8
'''
if old not in s: raise SystemExit('strategy test query marker not found')
s=s.replace(old,new,1)
p.write_text(s)

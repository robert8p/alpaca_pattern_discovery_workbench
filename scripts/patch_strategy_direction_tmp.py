from pathlib import Path

p=Path('app/strategy_economics.py')
s=p.read_text()
old='''        row = dict(signal)\n        row.update({"accepted": False, "rejection_reason": None, "metadata_temporal_status": signal.get("liquidity_metadata_temporal_status") or "entry_bar_only_no_point_in_time_daily_capacity"})\n'''
new='''        row = dict(signal)\n        row["direction"] = candidate["direction"]\n        row.update({"accepted": False, "rejection_reason": None, "metadata_temporal_status": signal.get("liquidity_metadata_temporal_status") or "entry_bar_only_no_point_in_time_daily_capacity"})\n'''
if old not in s: raise SystemExit('strategy simulation row marker not found')
s=s.replace(old,new,1)
p.write_text(s)

p=Path('tests/test_strategy_economics.py')
s=p.read_text()
if 'test_simulation_carries_direction_for_overlap_accounting' not in s:
    s += r'''


def test_simulation_carries_direction_for_overlap_accounting():
    from datetime import datetime, UTC, timedelta
    from app.strategy_economics import _simulate
    candidate = _candidate()
    cfg = _base_config(candidate_id=candidate["id"], capital_levels=[10000], max_bar_participation_pct=100, max_daily_participation_pct=100, min_fill_fraction=0.01)
    ts = datetime(2026, 6, 8, 14, 0, tzinfo=UTC)
    signals = [
        {"symbol":"AAA","signal_ts":ts,"entry_ts":ts,"exit_ts":ts+timedelta(minutes=30),"trade_date":date(2026,6,8),"entry_price":100.0,"exit_price":99.0,"gross_return_pct":1.0,"entry_bar_dollar_volume":1_000_000.0,"daily_dollar_volume":10_000_000.0,"liquidity_metadata_temporal_status":"point_in_time_universe_t_minus_1"},
        {"symbol":"BBB","signal_ts":ts,"entry_ts":ts,"exit_ts":ts+timedelta(minutes=30),"trade_date":date(2026,6,8),"entry_price":50.0,"exit_price":49.5,"gross_return_pct":1.0,"entry_bar_dollar_volume":1_000_000.0,"daily_dollar_volume":10_000_000.0,"liquidity_metadata_temporal_status":"point_in_time_universe_t_minus_1"},
    ]
    rows = _simulate(signals, candidate, cfg, 10_000.0, 20.0)
    accepted = [r for r in rows if r["accepted"]]
    assert len(accepted) == 2
    assert all(r["direction"] == "short" for r in accepted)
'''
p.write_text(s)

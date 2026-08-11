from pathlib import Path

p=Path('app/strategy_economics.py')
s=p.read_text()
# Rolling end date should be a JSON-safe string in retained metrics.
s=s.replace('''            "end_date":window[-1]["trade_date"],"compounded_return_pct":_compound_pct(returns),
''','''            "end_date":window[-1]["trade_date"].isoformat(),"compounded_return_pct":_compound_pct(returns),
''')
# Persist stress/metric JSON through the common JSON-safe converter.
s=s.replace('''(run_id,float(capital),delay,float(cost),Jsonb(metrics)))''','''(run_id,float(capital),delay,float(cost),Jsonb(json_safe(metrics))))''')
s=s.replace('''(run_id,float(capital),Jsonb(all_metrics[str(capital)]),Jsonb(scorecard),classification))''','''(run_id,float(capital),Jsonb(json_safe(all_metrics[str(capital)])),Jsonb(json_safe(scorecard)),classification))''')
# Summary stage dates are retained as ISO strings.
s=s.replace('''        "start_date":config.start_date,"end_date":config.end_date,"capital_levels":config.capital_levels,
''','''        "start_date":config.start_date.isoformat(),"end_date":config.end_date.isoformat(),"capital_levels":config.capital_levels,
''')
# Defensive JSON normalization for Research Ledger and run summary writes.
s=s.replace('''(Jsonb(summary),classification,row["id"]))''','''(Jsonb(json_safe(summary)),classification,row["id"]))''')
s=s.replace('''(run_id,Jsonb(config_payload),config_hash,Jsonb(summary),classification,row["id"]))''','''(run_id,Jsonb(json_safe(config_payload)),config_hash,Jsonb(json_safe(summary)),classification,row["id"]))''')
s=s.replace('''(primary_classification,Jsonb(summary),Jsonb(primary_scorecard or {}),summary["regime_coverage_pct"],run_id))''','''(primary_classification,Jsonb(json_safe(summary)),Jsonb(json_safe(primary_scorecard or {})),summary["regime_coverage_pct"],run_id))''')
s=s.replace('''(strategy_run_id,Jsonb(payload),fingerprint,notes,ledger["id"]))''','''(strategy_run_id,Jsonb(json_safe(payload)),fingerprint,notes,ledger["id"]))''')
p.write_text(s)

p=Path('tests/test_strategy_economics.py')
s=p.read_text()
if 'test_strategy_json_payloads_are_safe_for_long_rolling_windows' not in s:
    s += r'''


def test_strategy_json_payloads_are_safe_for_long_rolling_windows():
    from app.utils import json_safe
    import json
    payload = {
        "start_date": date(2025, 5, 5),
        "rolling_20_market_day": [{"end_date": date(2025, 6, 2), "profit_factor": 1.2}],
    }
    safe = json_safe(payload)
    encoded = json.dumps(safe)
    assert "2025-05-05" in encoded and "2025-06-02" in encoded
'''
p.write_text(s)

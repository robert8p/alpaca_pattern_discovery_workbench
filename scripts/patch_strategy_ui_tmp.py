from pathlib import Path

p=Path('app/templates/index.html')
s=p.read_text()
old='<script src="/static/phase1.js"></script>\n</body>'
new='<script src="/static/phase1.js"></script>\n<script src="/static/strategy.js"></script>\n</body>'
if '/static/strategy.js' not in s:
    if old not in s: raise SystemExit('index script marker not found')
    s=s.replace(old,new,1)
p.write_text(s)

p=Path('scripts/release_audit.py')
s=p.read_text()
old='''    strategy_source = (ROOT / "app/strategy_economics.py").read_text(encoding="utf-8")\n'''
new='''    strategy_source = (ROOT / "app/strategy_economics.py").read_text(encoding="utf-8")\n    strategy_javascript = (ROOT / "app/static/strategy.js").read_text(encoding="utf-8")\n'''
if 'strategy_javascript =' not in s:
    if old not in s: raise SystemExit('release audit strategy source marker not found')
    s=s.replace(old,new,1)
old='''    for token in ("net_expected_value_pct", "maximum_drawdown_pct", "same_timestamp_outcome_icc", "strategy_config_hash", "assert_strategy_frozen"):\n        if token not in strategy_source:\n            raise RuntimeError(f"Whole-strategy economics engine is missing {token}")\n'''
new='''    for token in ("net_expected_value_pct", "maximum_drawdown_pct", "same_timestamp_outcome_icc", "strategy_config_hash", "assert_strategy_frozen"):\n        if token not in strategy_source:\n            raise RuntimeError(f"Whole-strategy economics engine is missing {token}")\n    if '/static/strategy.js' not in html:\n        raise RuntimeError("Whole-strategy economics browser extension is not loaded")\n    for token in ("Whole-strategy economics", "Hit rate remains a diagnostic only", "strategy-economics", "Freeze executable strategy"):\n        if token not in strategy_javascript:\n            raise RuntimeError(f"Whole-strategy economics UI is missing {token}")\n'''
if 'Whole-strategy economics UI is missing' not in s:
    if old not in s: raise SystemExit('release audit strategy token marker not found')
    s=s.replace(old,new,1)
p.write_text(s)

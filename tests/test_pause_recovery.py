from pathlib import Path


def test_stale_pause_and_cancel_are_recovered():
    source = Path('app/jobs.py').read_text(encoding='utf-8')
    assert "status='pause_requested'" in source
    assert "status='paused'" in source
    assert "status='cancel_requested'" in source
    assert "status='cancelled'" in source


def test_feature_batch_has_control_monitor_and_cancel():
    source = Path('app/features.py').read_text(encoding='utf-8')
    assert 'monitor_control' in source
    assert 'conn.cancel()' in source
    assert 'except JobInterrupted' in source
    assert "SET status='pending',error=NULL" in source

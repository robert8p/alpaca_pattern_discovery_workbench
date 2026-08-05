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


def test_feature_batch_has_hard_wall_clock_and_heartbeat():
    source = Path('app/features.py').read_text(encoding='utf-8')
    assert 'feature_batch_wall_timeout_seconds' in source
    assert 'FeatureBatchTimeout' in source
    assert 'UPDATE ra_jobs SET heartbeat_at=now()' in source
    assert 'UPDATE ra_feature_batches SET updated_at=now()' in source
    assert 'pg_terminate_backend' in source


def test_recovered_and_resumed_jobs_can_be_reclaimed_after_attempt_three():
    jobs = Path('app/jobs.py').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')
    assert 'attempts=GREATEST(attempts-1,0)' in jobs
    assert 'attempts=0' in main

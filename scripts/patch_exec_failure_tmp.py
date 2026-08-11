from pathlib import Path
p=Path('app/worker.py'); s=p.read_text()
old='''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
new='''            elif job["job_type"] == "candidate_wave_build":
                if status == "paused":
                    cur.execute("UPDATE ra_candidate_wave_chunks SET status='pending' WHERE candidate_wave_run_id IN (SELECT id FROM ra_candidate_wave_runs WHERE job_id=%s) AND status='running'", (job["id"],))
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_candidate_wave_runs SET status=%s WHERE job_id=%s", (status, job["id"]))
            elif job["job_type"] == "strategy_economics_analysis":
                if status in {"cancelled", "failed"}:
                    cur.execute("UPDATE ra_strategy_economics_runs SET status=%s,completed_at=CASE WHEN %s='cancelled' THEN now() ELSE completed_at END WHERE job_id=%s", (status,status,job["id"]))
            elif job["job_type"] == "discovery_scan":
'''
if old not in s: raise SystemExit('worker candidate-wave marker not found')
s=s.replace(old,new,1)
p.write_text(s)

p=Path('tests/test_executable_strategy.py'); s=p.read_text()
if 'test_worker_propagates_executable_strategy_failure_state' not in s:
    s += '''\n\ndef test_worker_propagates_executable_strategy_failure_state():\n    source=(Path(__file__).resolve().parents[1]/"app/worker.py").read_text()\n    assert 'job["job_type"] == "strategy_economics_analysis"' in source\n    assert 'UPDATE ra_strategy_economics_runs SET status=%s' in source\n'''
p.write_text(s)

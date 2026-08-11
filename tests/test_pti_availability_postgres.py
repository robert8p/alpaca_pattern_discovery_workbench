from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 with TEST_DATABASE_URL to run PostgreSQL integration tests",
)


def test_pti_availability_removes_stale_name_and_refills_top_n():
    import psycopg

    from app.config import get_settings
    from app.db import close_pool, connection, execute_schema
    from app.models import UniverseBuildConfig
    from app.pti_availability import NOT_TRADING_REASON, apply_point_in_time_availability_filter
    from tests.test_postgres_integration import _reset_and_seed

    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    close_pool()
    _reset_and_seed()
    execute_schema()

    root = Path(__file__).resolve().parents[1]
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as raw:
        with raw.cursor() as cur:
            cur.execute((root / "sql" / "migrations" / "2.6.0.sql").read_text(encoding="utf-8"))
            cur.execute((root / "sql" / "migrations" / "2.6.2.sql").read_text(encoding="utf-8"))

    parent_job = uuid4()
    child_job = uuid4()
    reference_run = uuid4()
    snapshot_run = uuid4()
    pti_run = uuid4()
    snapshot_id = uuid4()

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ra_jobs(id,job_type,name,config) VALUES (%s,'quality_scan','pti parent',%s),(%s,'universe_build','pti child',%s)",
                (parent_job, Jsonb({}), child_job, Jsonb({})),
            )
            cur.execute(
                "INSERT INTO ra_universe_runs(id,job_id,name,source_config,selection_config) VALUES (%s,%s,'reference',%s,%s),(%s,%s,'snapshot',%s,%s)",
                (
                    reference_run,parent_job,Jsonb({}),Jsonb({}),
                    snapshot_run,child_job,Jsonb({}),Jsonb({}),
                ),
            )
            cur.execute(
                """
                INSERT INTO ra_point_in_time_universe_runs(
                    id,parent_job_id,reference_universe_run_id,name,requested_start,requested_end,cadence,
                    source_config,selection_config,methodology_version,status
                ) VALUES (%s,%s,%s,'pti test','2026-06-09','2026-06-09','single_date',%s,%s,'test','running')
                """,
                (pti_run,parent_job,reference_run,Jsonb({}),Jsonb({})),
            )
            cur.execute(
                """
                INSERT INTO ra_point_in_time_universe_snapshots(
                    id,point_in_time_universe_run_id,snapshot_date,effective_start,effective_end,
                    lookback_start,lookback_end,child_job_id,snapshot_universe_run_id,status
                ) VALUES (%s,%s,'2026-06-09','2026-06-09','2026-06-09','2026-04-09','2026-06-08',%s,%s,'running')
                """,
                (snapshot_id,pti_run,child_job,snapshot_run),
            )
            for rank,(symbol,tier,included,reason) in enumerate([
                ('AAA','A',True,None),
                ('BBB','A',True,None),
                ('CCC','B',True,None),
                ('QQQ','B',False,'outside maximum-symbol limit'),
            ], start=1):
                cur.execute(
                    """
                    INSERT INTO ra_analysis_universe(
                        universe_run_id,symbol,trading_days,average_bars_per_day,median_daily_dollar_volume,
                        average_daily_dollar_volume,median_close,liquidity_tier,included,rank_by_liquidity,exclusion_reason
                    ) VALUES (%s,%s,20,390,100000000,100000000,50,%s,%s,%s,%s)
                    """,
                    (snapshot_run,symbol,tier,included,rank,reason),
                )
            cur.execute(
                "DELETE FROM rd_bars WHERE symbol='BBB' AND (bar_ts AT TIME ZONE 'America/New_York')::date=DATE '2026-06-08'"
            )
        conn.commit()

    config = UniverseBuildConfig(
        name='PTI liquid universe · 2026-06-09',
        start_date='2026-04-09',end_date='2026-06-08',timeframe='1Min',feed='sip',adjustment='raw',session='all',
        minimum_trading_days=15,minimum_average_bars_per_day=300,minimum_median_daily_dollar_volume=1,
        minimum_median_close=1,maximum_symbols=3,exclude_likely_funds=False,
    )
    result = apply_point_in_time_availability_filter(str(child_job), str(snapshot_run), config)
    assert result is not None
    assert result['reference_trade_date'] == date(2026,6,8)
    assert result['removed_symbols'] == 1
    assert result['refilled_symbols'] == 1
    assert result['included_symbols'] == 3

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,included,exclusion_reason FROM ra_analysis_universe WHERE universe_run_id=%s ORDER BY rank_by_liquidity", (snapshot_run,))
            rows = {r['symbol']: dict(r) for r in cur.fetchall()}
            assert rows['BBB']['included'] is False
            assert rows['BBB']['exclusion_reason'] == NOT_TRADING_REASON
            assert rows['QQQ']['included'] is True
            assert rows['QQQ']['exclusion_reason'] is None
            cur.execute("SELECT availability_reference_date,availability_removed_symbols,availability_refilled_symbols,availability_method_version FROM ra_point_in_time_universe_snapshots WHERE id=%s", (snapshot_id,))
            provenance = cur.fetchone()
            assert provenance['availability_reference_date'] == date(2026,6,8)
            assert provenance['availability_removed_symbols'] == 1
            assert provenance['availability_refilled_symbols'] == 1
            assert provenance['availability_method_version'] == '1.0.0'
        conn.rollback()
    close_pool()

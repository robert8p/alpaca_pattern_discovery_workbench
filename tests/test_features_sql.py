from datetime import date
from uuid import uuid4

from app.features import FeatureBatchTimeout, _feature_sql, _is_statement_timeout
from app.models import FeatureBuildConfig


def test_feature_sql_reads_raw_and_writes_analysis_only():
    config = FeatureBuildConfig(
        name="Feature SQL", universe_run_id=uuid4(), start_date="2026-07-01",
        end_date="2026-07-03", timeframe="1Min",
    )
    sql, params = _feature_sql(config, date(2026, 7, 1), date(2026, 7, 3), ["AAPL", "MSFT"])
    lowered = sql.lower()
    assert "from rd_bars" in lowered
    assert "insert into ra_intraday_features" in lowered
    assert "insert into rd_" not in lowered
    assert len(params) == 21
    assert sql.count("%s") == len(params)


def test_feature_sql_uses_symbol_batches_and_timestamp_bounds():
    config = FeatureBuildConfig(
        name="Batched SQL", universe_run_id=uuid4(), start_date="2026-07-01",
        end_date="2026-07-03", timeframe="1Min",
    )
    sql, params = _feature_sql(config, date(2026, 7, 1), date(2026, 7, 3), ["AAPL"])
    lowered = sql.lower()
    assert "symbol=any(%s::text[])" in lowered
    assert "bar_ts >= %s and b.bar_ts < %s" in lowered
    assert "::date between %s and %s" not in lowered
    assert params[7] == ["AAPL"]


def test_statement_timeout_detection():
    assert _is_statement_timeout(RuntimeError("canceling statement due to statement timeout"))
    assert _is_statement_timeout(FeatureBatchTimeout("Feature SQL batch exceeded the wall-clock limit"))
    assert not _is_statement_timeout(RuntimeError("connection refused"))


def test_deadlock_is_treated_as_transient_database_conflict():
    from app.features import _is_transient_database_conflict

    class DeadlockError(RuntimeError):
        sqlstate = "40P01"

    assert _is_transient_database_conflict(DeadlockError("deadlock detected"))
    assert _is_transient_database_conflict(RuntimeError("canceling statement due to lock timeout"))
    assert not _is_transient_database_conflict(RuntimeError("undefined column"))


def test_partition_ddl_is_not_inside_feature_batch_sql():
    config = FeatureBuildConfig(
        name="No batch DDL", universe_run_id=uuid4(), start_date="2026-07-01",
        end_date="2026-07-03", timeframe="1Min",
    )
    sql, _ = _feature_sql(config, date(2026, 7, 1), date(2026, 7, 3), ["AAPL"])
    assert "ra_ensure_feature_partitions" not in sql.lower()

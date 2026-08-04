from datetime import date
from uuid import uuid4

from app.features import _feature_sql
from app.models import FeatureBuildConfig


def test_feature_sql_reads_raw_and_writes_analysis_only():
    config = FeatureBuildConfig(
        name="Feature SQL", universe_run_id=uuid4(), start_date="2026-07-01",
        end_date="2026-07-03", timeframe="1Min",
    )
    sql, params = _feature_sql(config, date(2026, 7, 1), date(2026, 7, 3))
    lowered = sql.lower()
    assert "from rd_bars" in lowered
    assert "insert into ra_intraday_features" in lowered
    assert "insert into rd_" not in lowered
    assert len(params) == 19
    assert sql.count("%s") == len(params)

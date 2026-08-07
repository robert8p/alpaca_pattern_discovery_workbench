from datetime import date
from pathlib import Path

from app.discovery import (
    FAMILIES, RANGE_POS, STATISTICS_METHOD, TIME, _bucket_ranges, _condition_sql,
    _date_chunks, _finalise_stats, _hist_quantile, _merge_partial_rows,
    _is_retryable_database_error, _partial_insert_query, _plain_rule,
    _planned_variant_count, _sample_insert_query, _sealed_partial_query,
)


def test_date_chunks_cover_period_without_overlap():
    chunks = _date_chunks(date(2026, 7, 1), date(2026, 7, 8), 3)
    assert chunks == [
        (date(2026, 7, 1), date(2026, 7, 3)),
        (date(2026, 7, 4), date(2026, 7, 6)),
        (date(2026, 7, 7), date(2026, 7, 8)),
    ]


def test_bucket_ranges_cover_1024_exactly():
    ranges = _bucket_ranges(7)
    assert ranges[0][0] == 0 and ranges[-1][1] == 1024
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    assert sum(end - start for start, end in ranges) == 1024


def test_sample_query_is_narrow_and_non_overlapping():
    query = _sample_insert_query([5, 15, 30, 60], 5, 570)
    assert "INSERT INTO ra_discovery_samples" in query
    assert "fwd_return_15m_pct" in query and "fwd_return_60m_pct" in query
    assert "mod(minute_of_day - 570, 5) = 0" in query
    assert "symbol_bucket" in query


def test_partial_queries_are_bounded_and_mergeable():
    for family, spec in FAMILIES.items():
        for direction in ("long", "short"):
            query = _partial_insert_query(spec["dimensions"], spec["filter"], direction, 30, 30, 570)
            assert "FROM ra_discovery_samples" in query
            assert "trade_date BETWEEN" in query
            assert "symbol_bucket >=" in query
            assert "percentile_cont" not in query
            assert "jsonb_object_agg" in query
            assert "net_sum_squares" in query
            assert family


def test_condition_sql_uses_parameters():
    sql, params = _condition_sql([
        {"column": "minute_of_day", "operator": "range", "low": 870, "high": 930},
        {"column": "weekday_iso", "operator": "eq", "value": 3},
    ])
    assert sql == "minute_of_day>=%s AND minute_of_day<%s AND weekday_iso=%s"
    assert params == [870, 930, 3]


def test_sealed_query_uses_same_sampling_and_conditions():
    conditions = [TIME.labels["t1430_1530"][0]]
    query, params = _sealed_partial_query(conditions, "long", 30, 30, 570)
    assert "fwd_return_30m_pct" in query
    assert "mod(minute_of_day - 570, 30) = 0" in query
    assert "minute_of_day>=%s" in query
    assert params == [870, 930]


def test_histogram_merge_produces_statistics():
    rows = [
        {
            "group_key": "x", "group_values": {"time_bin": "t1430_1530"},
            "observations": 3, "gross_sum": 0.9, "net_sum": 0.6,
            "net_sum_squares": 0.18, "wins": 2, "positive_sum": 0.8,
            "negative_sum_abs": 0.2, "worst_pct": -0.2,
            "histogram": {"-2": 1, "2": 2},
            "symbol_counts": {"AAA": 2, "BBB": 1},
            "date_counts": {"2026-07-01": 3},
        },
        {
            "group_key": "x", "group_values": {"time_bin": "t1430_1530"},
            "observations": 2, "gross_sum": 0.5, "net_sum": 0.3,
            "net_sum_squares": 0.09, "wins": 1, "positive_sum": 0.5,
            "negative_sum_abs": 0.2, "worst_pct": -0.2,
            "histogram": {"-2": 1, "5": 1},
            "symbol_counts": {"AAA": 1, "CCC": 1},
            "date_counts": {"2026-07-02": 2},
        },
    ]
    merged = _merge_partial_rows(rows)["x"]
    stats = _finalise_stats(merged)
    assert stats["observations"] == 5
    assert stats["symbols"] == 3
    assert stats["dates"] == 2
    assert round(stats["net_avg_pct"], 4) == 0.18
    assert stats["max_symbol_share_pct"] == 60
    assert stats["max_date_share_pct"] == 60
    assert stats["statistics_method"] == STATISTICS_METHOD


def test_hist_quantile_uses_documented_bins():
    assert _hist_quantile({"-2": 1, "0": 2, "5": 1}, 0.5, 4) == 0.05


def test_plain_rule_is_readable():
    rule = _plain_rule("long", ["between 14:30 and 15:30 ET", "relative volume is at least 3x"], 30)
    assert rule.startswith("Buy when") and rule.endswith("exit after 30 minutes.")


def test_transient_database_failures_are_replayable_but_logic_errors_are_not():
    assert _is_retryable_database_error(RuntimeError("deadlock detected"))
    assert _is_retryable_database_error(RuntimeError("could not serialize access due to concurrent update"))
    assert _is_retryable_database_error(RuntimeError("canceling statement due to lock timeout"))
    assert not _is_retryable_database_error(RuntimeError("column does_not_exist does not exist"))


def test_family_constraints_preserve_filters_during_sealed_replay():
    momentum = FAMILIES["momentum_continuation"]
    assert {c["operator"] for c in momentum["constraints"]} >= {"gt", "gte", "not_null"}
    query, params = _sealed_partial_query(
        [RANGE_POS.labels["p40_p60"][0], *momentum["constraints"]],
        "long", 30, 30, 570,
    )
    assert "cumulative_range_position>=%s" in query
    assert 0.5 in params


def test_abs_and_not_null_conditions_are_parameterised():
    sql, params = _condition_sql([
        {"column": "gap_from_previous_regular_close_pct", "operator": "abs_gte", "value": 1},
        {"column": "previous_day_return_pct", "operator": "not_null"},
    ])
    assert sql == "abs(gap_from_previous_regular_close_pct)>=%s AND previous_day_return_pct IS NOT NULL"
    assert params == [1]


def test_sample_schema_does_not_duplicate_predictors_per_horizon():
    schema = Path("sql/schema.sql").read_text(encoding="utf-8")
    sample_block = schema.split("CREATE TABLE IF NOT EXISTS ra_discovery_samples (", 1)[1].split(");", 1)[0]
    assert "holding_horizon_minutes" not in sample_block
    for horizon in (5, 15, 30, 60):
        assert f"fwd_return_{horizon}m_pct" in sample_block


def test_coverage_pack_families_are_versioned_and_mapped_to_hypotheses():
    expected={
        "dip_repair":{"H01"}, "compression_expansion":{"H03"}, "gap_state":{"H04","H05"},
        "activity_absorption":{"H06"}, "price_efficiency":{"H07"},
        "new_high_liquidity_divergence":{"H12"},
    }
    for family,hids in expected.items():
        spec=FAMILIES[family]
        assert set(spec["hypothesis_ids"]) == hids
        assert spec["coverage"] == "PARTIAL"
        assert spec["hypothesis_version"] != "legacy-v1"


def test_sample_query_carries_unused_feature_inputs_into_discovery_layer():
    q=_sample_insert_query([30],30,570)
    for name in ("relative_trade_count_20bar","rolling_realised_volatility_30bar","opening_range_position","activity_impact_change_ratio","price_group"):
        assert name in q


def test_legacy_family_filters_are_not_redefined():
    assert FAMILIES["oversold_reversal"]["filter"] == "ret_30m_pct<0 AND distance_from_cumulative_vwap_pct<0 AND relative_volume_20bar IS NOT NULL"
    assert FAMILIES["gap_behavior"]["filter"] == "abs(gap_from_previous_regular_close_pct)>=1 AND previous_day_return_pct IS NOT NULL"


def test_defined_variant_count_includes_full_parameter_grid():
    from app.models import DiscoveryConfig
    from uuid import uuid4
    cfg=DiscoveryConfig.model_validate({
        "feature_set_id": str(uuid4()), "discovery_start": "2026-07-01", "discovery_end": "2026-07-10",
        "directions": ["long","short"], "holding_horizons_minutes": [15,30],
        "families": ["time_of_day","activity_absorption"],
    })
    expected=0
    for family in cfg.families:
        per_family=1
        for dimension in FAMILIES[family]["dimensions"]:
            per_family *= len(dimension.labels)
        expected += per_family * len(cfg.directions) * len(cfg.holding_horizons_minutes)
    assert _planned_variant_count(cfg) == expected
    assert expected > 0


def test_candidate_write_tracks_actual_and_defined_variant_counts_separately():
    source=Path("app/discovery.py").read_text(encoding="utf-8")
    assert "variants_tested_campaign,variants_defined_campaign" in source
    assert 'groups_tested,planned_variants,"bonferroni_normal_approx"' in source
    assert "variant_count=%s,defined_variant_count=%s" in source

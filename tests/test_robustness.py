from datetime import date

from app.robustness import _compatible, _metrics, _observation_query, _perturb_conditions, _verdict


def rows():
    out=[]
    for i in range(20):
        out.append({
            "symbol": f"S{i%5}", "trade_date": date(2026,7,1+i%10),
            "gross_return_pct": 0.6 if i%3 else -0.2,
            "mfe_pct": 0.9, "mae_pct": -0.35,
            "liquidity_tier": "A" if i%2 else "B", "price_group":"25_100",
        })
    return out


def test_metrics_include_cluster_and_concentration_outputs():
    m=_metrics(rows(),20)
    assert m["observations"] == 20
    assert m["symbols"] == 5
    assert m["dates"] == 10
    assert "date_clustered_t_stat" in m
    assert "leave_one_date_out_min_net_avg_pct" in m
    assert "top_1pct_return_share_pct" in m and "top_10pct_return_share_pct" in m
    assert m["mfe_avg_pct"] == 0.9
    assert m["mae_avg_pct"] == -0.35
    assert m["missing_data_rate_pct"] == 0


def test_robustness_query_replays_signal_conditions_and_delayed_entry():
    q, params=_observation_query([{
        "column":"ret_30m_pct","operator":"range","low":-5,"high":-3,
        "low_inclusive":True,"high_inclusive":False,
    }],"long",30,30,570,2)
    assert "e.bar_ts=s.bar_ts+(%s::integer * interval '1 minute')" in q
    assert "s.ret_30m_pct>=%s" in q and "s.ret_30m_pct<%s" in q
    assert params == (-5,-3)
    assert "LEFT JOIN LATERAL" in q
    assert "AS mfe_pct" in q and "AS mae_pct" in q


def test_threshold_perturbation_and_verdict():
    exact=[{"column":"gap_from_previous_regular_close_pct","operator":"abs_gte","value":1}]
    assert _perturb_conditions(exact,10,"relaxed")[0]["value"] == 0.9
    summary={"base":{"net_avg_pct":0.2,"date_clustered_t_stat":1.6,"profit_factor":1.3},
             "cost_sensitivity":{"30":{"net_avg_pct":0.1}},
             "neighbourhood":{"relaxed":{"net_avg_pct":0.1},"tightened":{"net_avg_pct":0.08}}}
    assert _verdict(summary,"development") == "PROMISING"
    assert _verdict(summary,"historical_holdout") == "HISTORICAL_HOLDOUT"


def test_cross_feature_holdout_requires_same_frozen_universe():
    source={"universe_run_id":"u1","config":{"timeframe":"1Min","feed":"sip","adjustment":"raw","session":"regular"}}
    target={"universe_run_id":"u1","config":{"timeframe":"1Min","feed":"sip","adjustment":"raw","session":"regular","outcome_horizons_minutes":[30]}}
    _compatible(source,target,30)
    target["universe_run_id"]="u2"
    import pytest
    with pytest.raises(ValueError,match="same frozen analysis universe"):
        _compatible(source,target,30)


def test_metrics_report_missing_outcomes_without_treating_them_as_zero_returns():
    sample=rows()[:3] + [{
        "symbol":"MISS","trade_date":date(2026,7,11),"gross_return_pct":None,
        "mfe_pct":None,"mae_pct":None,"liquidity_tier":"A","price_group":"25_100",
    }]
    m=_metrics(sample,20)
    assert m["candidate_signals"] == 4
    assert m["observations"] == 3
    assert m["missing_outcomes"] == 1
    assert m["missing_data_rate_pct"] == 25


def test_cross_feature_holdout_rejects_changed_feature_definition():
    import pytest
    base={
        "timeframe":"1Min","feed":"sip","adjustment":"raw","session":"regular",
        "liquidity_tiers":["A","B"],"time_of_day_baseline_days":10,
        "predictor_horizons_minutes":[1,5,15,30,60],"outcome_horizons_minutes":[5,15,30,60],
    }
    source={"universe_run_id":"u1","config":dict(base)}
    changed_tiers=dict(base); changed_tiers["liquidity_tiers"]=["A"]
    with pytest.raises(ValueError,match="same liquidity tiers"):
        _compatible(source,{"universe_run_id":"u1","config":changed_tiers},30)
    changed_baseline=dict(base); changed_baseline["time_of_day_baseline_days"]=20
    with pytest.raises(ValueError,match="same time-of-day baseline"):
        _compatible(source,{"universe_run_id":"u1","config":changed_baseline},30)
    changed_predictors=dict(base); changed_predictors["predictor_horizons_minutes"]=[5,15,30,60]
    with pytest.raises(ValueError,match="same predictor horizons"):
        _compatible(source,{"universe_run_id":"u1","config":changed_predictors},30)


def test_threshold_perturbation_changes_open_ended_bins():
    ge=[{"column":"ret_30m_pct","operator":"range","low":3,"high":None}]
    lt=[{"column":"ret_30m_pct","operator":"range","low":None,"high":-3}]
    assert _perturb_conditions(ge,10,"relaxed")[0]["low"] == 2.7
    assert _perturb_conditions(ge,10,"tightened")[0]["low"] == 3.3
    assert _perturb_conditions(lt,10,"relaxed")[0]["high"] == -2.7
    assert _perturb_conditions(lt,10,"tightened")[0]["high"] == -3.3


def test_development_query_uses_staged_discovery_samples_and_bucket_bounds():
    from app.robustness import _development_observation_query
    q, params = _development_observation_query([
        {"column":"ret_30m_pct","operator":"gte","value":3}
    ], "short", 15, 15, 570, 0)
    assert "FROM ra_discovery_samples s" in q
    assert "s.symbol_bucket >= %s AND s.symbol_bucket < %s" in q
    assert "f.feature_set_id=%s" not in q
    assert params == (3,)


def test_holdout_query_is_bounded_by_date_and_symbol_bucket():
    q, _ = _observation_query([
        {"column":"ret_30m_pct","operator":"gte","value":3}
    ], "short", 15, 15, 570, 0)
    assert "f.trade_date=%s" in q
    assert "hashtext(f.symbol)" in q
    assert ">= %s" in q and "< %s" in q


def test_robustness_variant_specs_include_each_delay_and_two_neighbourhoods():
    from app.models import RobustnessAnalysisConfig
    from app.robustness import _variant_specs
    cfg=RobustnessAnalysisConfig(candidate_id="00000000-0000-0000-0000-000000000001",entry_delays_minutes=[0,1,2,5])
    specs=_variant_specs(cfg,[{"column":"ret_30m_pct","operator":"gte","value":3}])
    assert set(specs)=={"delay:0","delay:1","delay:2","delay:5","neighbour:relaxed","neighbour:tightened"}

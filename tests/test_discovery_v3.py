from app.discovery_v3 import economic_rank_score


def _stats(**overrides):
    base = {
        "net_avg_pct": 0.20,
        "median_pct": 0.10,
        "profit_factor": 1.5,
        "p05_pct": -1.0,
        "worst_pct": -4.0,
        "observations": 1000,
        "t_stat": 2.0,
        "max_symbol_share_pct": 5.0,
        "max_date_share_pct": 5.0,
        "win_rate_pct": 50.0,
    }
    base.update(overrides)
    return base


def test_hit_rate_does_not_change_discovery_rank_score():
    low_hit = economic_rank_score(_stats(win_rate_pct=35.0), None)
    high_hit = economic_rank_score(_stats(win_rate_pct=80.0), None)
    assert low_hit == high_hit


def test_lower_mean_better_distribution_can_outrank_fragile_higher_mean():
    fragile = _stats(
        net_avg_pct=0.25,
        median_pct=-0.10,
        profit_factor=1.10,
        p05_pct=-3.0,
        worst_pct=-10.0,
        t_stat=3.0,
    )
    balanced = _stats(
        net_avg_pct=0.18,
        median_pct=0.12,
        profit_factor=1.80,
        p05_pct=-1.0,
        worst_pct=-4.0,
        t_stat=2.0,
    )
    assert economic_rank_score(balanced, None) > economic_rank_score(fragile, None)


def test_negative_validation_expectancy_is_strongly_penalised():
    discovery = _stats()
    positive_validation = _stats(net_avg_pct=0.16, observations=300)
    negative_validation = _stats(net_avg_pct=-0.05, observations=300)
    assert economic_rank_score(discovery, positive_validation) > economic_rank_score(discovery, negative_validation)


def test_tail_risk_reduces_score_without_becoming_a_hard_hit_rate_gate():
    controlled = economic_rank_score(_stats(p05_pct=-0.8, worst_pct=-3.0), None)
    heavy_tail = economic_rank_score(_stats(p05_pct=-4.0, worst_pct=-15.0), None)
    assert controlled > heavy_tail

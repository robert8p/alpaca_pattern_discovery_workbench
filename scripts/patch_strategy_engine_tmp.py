from pathlib import Path

p=Path('app/strategy_economics.py')
s=p.read_text()

# Add correlation / ES helpers.
marker='''def _losing_streak(values: list[float]) -> int:\n'''
if marker not in s:
    raise SystemExit('losing streak marker not found')
helpers=r'''def _expected_shortfall(values: list[float], tail_q: float) -> float | None:
    threshold = _quantile(values, tail_q)
    if threshold is None:
        return None
    tail = [v for v in values if v <= threshold]
    return mean(tail) if tail else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    ax = mean(x for x, _ in pairs)
    ay = mean(y for _, y in pairs)
    sx = sum((x-ax)**2 for x, _ in pairs)
    sy = sum((y-ay)**2 for _, y in pairs)
    if sx <= 0 or sy <= 0:
        return None
    return finite_or_none(sum((x-ax)*(y-ay) for x, y in pairs) / math.sqrt(sx*sy))


def _compound_pct(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    for value in values:
        wealth *= max(0.0, 1.0 + float(value)/100.0)
    return (wealth-1.0)*100.0


'''
if '_expected_shortfall(' not in s:
    s=s.replace(marker,helpers+marker,1)

# Replace signal query with the same enriched point-in-time predictor layer used by cross-feature robustness.
start=s.index('def _signal_query(')
end=s.index('\ndef _fetch_signals', start)
new_signal=r'''def _signal_query(candidate: dict[str, Any], config: StrategyEconomicsConfig, delay_minutes: int, include_path: bool) -> tuple[str, tuple[Any, ...]]:
    where, condition_params = _condition_sql(candidate["conditions"], alias="s")
    horizon = int(candidate["holding_horizon_minutes"])
    stride = max(1, int(candidate.get("entry_stride_minutes") or 1))
    anchor = int(candidate.get("entry_anchor_minute") or 570)
    strength = f"s.{config.signal_strength_field}" if config.signal_strength_field in _SAFE_STRENGTH_FIELDS else "NULL::double precision"
    path_cols = "path.max_high,path.min_low" if include_path else "NULL::double precision AS max_high,NULL::double precision AS min_low"
    path_join = """
        LEFT JOIN LATERAL (
            SELECT max(b.high)::double precision AS max_high,min(b.low)::double precision AS min_low
            FROM rd_bars b
            WHERE b.symbol=s.symbol AND b.timeframe='1Min' AND b.feed='sip' AND b.adjustment='raw'
              AND b.session_label='regular' AND en.bar_ts IS NOT NULL AND ex.bar_ts IS NOT NULL
              AND b.bar_ts BETWEEN en.bar_ts AND ex.bar_ts
        ) path ON true
    """ if include_path else ""
    sql = f"""
        WITH source AS MATERIALIZED (
            SELECT f.*,
                CASE WHEN f.close < 5 THEN 'lt_5' WHEN f.close < 10 THEN '5_10'
                     WHEN f.close < 25 THEN '10_25' WHEN f.close < 100 THEN '25_100' ELSE 'ge_100' END AS price_group,
                CASE WHEN f.ret_5m_pct IS NOT NULL AND f.relative_volume_20bar > 0
                     THEN abs(f.ret_5m_pct)/f.relative_volume_20bar END AS activity_adjusted_return_5m,
                CASE WHEN p.ret_5m_pct IS NOT NULL AND p.relative_volume_20bar > 0
                     THEN abs(p.ret_5m_pct)/p.relative_volume_20bar END AS prior_activity_adjusted_return_5m,
                p.relative_volume_20bar AS prior_relative_volume_20bar,
                p.relative_trade_count_20bar AS prior_relative_trade_count_20bar,
                max(f.high) FILTER (WHERE f.minute_of_day < 600) OVER (PARTITION BY f.symbol,f.trade_date) AS opening_range_high,
                min(f.low) FILTER (WHERE f.minute_of_day < 600) OVER (PARTITION BY f.symbol,f.trade_date) AS opening_range_low
            FROM ra_intraday_features f
            LEFT JOIN ra_intraday_features p
              ON p.feature_set_id=f.feature_set_id AND p.symbol=f.symbol
             AND p.bar_ts=f.bar_ts-interval '5 minutes'
            WHERE f.feature_set_id=%s AND f.trade_date BETWEEN %s AND %s
        ), enriched AS (
            SELECT source.*,
                CASE WHEN activity_adjusted_return_5m IS NOT NULL AND prior_activity_adjusted_return_5m > 0
                     THEN activity_adjusted_return_5m/prior_activity_adjusted_return_5m END AS activity_impact_change_ratio,
                CASE WHEN relative_volume_20bar IS NOT NULL AND prior_relative_volume_20bar > 0
                     THEN relative_volume_20bar/prior_relative_volume_20bar END AS relative_volume_change_ratio,
                CASE WHEN relative_trade_count_20bar IS NOT NULL AND prior_relative_trade_count_20bar > 0
                     THEN relative_trade_count_20bar/prior_relative_trade_count_20bar END AS relative_trade_count_change_ratio,
                CASE WHEN rolling_range_30bar_pct IS NOT NULL AND previous_day_range_pct > 0
                     THEN rolling_range_30bar_pct/previous_day_range_pct END AS range_vs_previous_day_ratio,
                CASE WHEN rolling_realised_volatility_30bar IS NOT NULL AND previous_day_realised_volatility > 0
                     THEN rolling_realised_volatility_30bar/previous_day_realised_volatility END AS volatility_vs_previous_day_ratio,
                CASE WHEN minute_of_day < 600 OR opening_range_high IS NULL OR opening_range_low IS NULL THEN NULL
                     WHEN close > opening_range_high THEN 'above' WHEN close < opening_range_low THEN 'below' ELSE 'inside' END AS opening_range_position,
                (high>=cumulative_high) AS touched_session_high,
                (low<=cumulative_low) AS touched_session_low
            FROM source
        ), signal AS MATERIALIZED (
            SELECT s.* FROM enriched s
            WHERE mod((s.minute_of_day-%s)::integer,%s)=0 AND ({where})
        )
        SELECT s.symbol,s.bar_ts AS signal_ts,s.trade_date,s.minute_of_day,s.liquidity_tier,
               {strength} AS signal_strength,
               en.bar_ts AS entry_ts,en.close AS entry_price,en.bar_dollar_volume AS entry_bar_dollar_volume,
               ex.bar_ts AS exit_ts,ex.close AS exit_price,
               a.exchange,COALESCE(a.attributes->>'sector',a.raw->>'sector') AS sector,
               CASE WHEN a.raw ? 'shortable' THEN NULLIF(a.raw->>'shortable','')::boolean END AS current_reference_shortable,
               (d.volume*COALESCE(d.vwap,d.close))::double precision AS daily_dollar_volume,
               {path_cols}
        FROM signal s
        LEFT JOIN ra_intraday_features en
          ON en.feature_set_id=s.feature_set_id AND en.symbol=s.symbol
         AND en.bar_ts=s.bar_ts+(%s*interval '1 minute') AND en.trade_date=s.trade_date
        LEFT JOIN ra_intraday_features ex
          ON ex.feature_set_id=s.feature_set_id AND ex.symbol=s.symbol
         AND ex.bar_ts=en.bar_ts+(%s*interval '1 minute') AND ex.trade_date=s.trade_date
        LEFT JOIN rd_assets a ON a.symbol=s.symbol
        LEFT JOIN rd_daily_features d
          ON d.symbol=s.symbol AND d.trade_date=s.trade_date AND d.timeframe='1Min'
         AND d.feed='sip' AND d.adjustment='raw' AND d.session_label='regular'
        {path_join}
        ORDER BY s.bar_ts,s.symbol
    """
    params: tuple[Any, ...] = (
        config.target_feature_set_id, config.start_date, config.end_date,
        anchor, stride, *condition_params, delay_minutes, horizon,
    )
    return sql, params
'''
s=s[:start]+new_signal+s[end:]

# Trade metrics: correct expected shortfall and add tail/MAE/concentration diagnostics.
s=s.replace('''        "expected_shortfall_95_pct": mean([v for v in net if v <= (_quantile(net,.05) or -math.inf)]) if net else None,
        "expected_shortfall_99_pct": mean([v for v in net if v <= (_quantile(net,.01) or -math.inf)]) if net else None,
''','''        "adverse_var_95_pct": _quantile(net,.05), "adverse_var_99_pct": _quantile(net,.01),
        "expected_shortfall_95_pct": _expected_shortfall(net,.05),
        "expected_shortfall_99_pct": _expected_shortfall(net,.01),
''')
old='''        "mae_mean_winners_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])>0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])>0 for r in trades) else None,
        "mae_mean_losers_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])<0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])<0 for r in trades) else None,
'''
new='''        "mae_mean_winners_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])>0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])>0 for r in trades) else None,
        "mae_mean_losers_pct": mean([float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and float(r["net_return_pct"])<0]) if any(r.get("mae_pct") is not None and float(r["net_return_pct"])<0 for r in trades) else None,
        "mae_final_outcome_correlation": _pearson(
            [float(r["mae_pct"]) for r in trades if r.get("mae_pct") is not None and r.get("net_return_pct") is not None],
            [float(r["net_return_pct"]) for r in trades if r.get("mae_pct") is not None and r.get("net_return_pct") is not None],
        ),
'''
if old not in s:
    raise SystemExit('MAE metrics marker not found')
s=s.replace(old,new,1)
old='''        "capital_level": capital,
    }
'''
new='''        "rejection_reason_counts": dict(__import__("collections").Counter(str(r.get("rejection_reason")) for r in rows if not r.get("accepted"))),
        "capital_level": capital,
    }
'''
# Only first occurrence is trade metrics return.
if old not in s:
    raise SystemExit('trade metrics return marker not found')
s=s.replace(old,new,1)

# Replace week/month aggregation with lists and compounded returns; add rolling metrics and period concentration.
old='''    weeks: dict[tuple[int,int],float]=defaultdict(float)
    months: dict[tuple[int,int],float]=defaultdict(float)
    for d in daily_rows:
        iso=d["trade_date"].isocalendar(); weeks[(iso.year,iso.week)]+=d["net_return_pct"]
        months[(d["trade_date"].year,d["trade_date"].month)]+=d["net_return_pct"]
    total_market_days=len(daily_rows)
'''
new='''    weeks: dict[tuple[int,int],list[float]]=defaultdict(list)
    months: dict[tuple[int,int],list[float]]=defaultdict(list)
    daily_pnl: list[float] = []
    prior = capital
    for d in daily_rows:
        iso=d["trade_date"].isocalendar(); weeks[(iso.year,iso.week)].append(d["net_return_pct"])
        months[(d["trade_date"].year,d["trade_date"].month)].append(d["net_return_pct"])
        daily_pnl.append(float(d["end_equity"])-prior)
        prior=float(d["end_equity"])
    week_returns={k:_compound_pct(v) or 0.0 for k,v in weeks.items()}
    month_returns={k:_compound_pct(v) or 0.0 for k,v in months.items()}
    total_market_days=len(daily_rows)
    rolling_20=[]
    accepted_by_date: dict[date,list[float]]=defaultdict(list)
    for t in accepted:
        accepted_by_date[t["trade_date"]].append(float(t["net_return_pct"]))
    for i in range(19,len(daily_rows)):
        window=daily_rows[i-19:i+1]
        returns=[float(x["net_return_pct"]) for x in window]
        mu=mean(returns); sd=(sum((x-mu)**2 for x in returns)/(len(returns)-1))**0.5 if len(returns)>1 else 0.0
        downside=[x for x in returns if x<0]
        dsd=(sum(x*x for x in downside)/len(downside))**0.5 if downside else 0.0
        window_trade_returns=[]
        for x in window:
            window_trade_returns.extend(accepted_by_date.get(x["trade_date"],[]))
        local_peak=1.0; local_wealth=1.0; local_dd=0.0
        for x in returns:
            local_wealth*=max(0.0,1+x/100.0); local_peak=max(local_peak,local_wealth); local_dd=min(local_dd,(local_wealth/local_peak-1)*100)
        rolling_20.append({
            "end_date":window[-1]["trade_date"],"compounded_return_pct":_compound_pct(returns),
            "mean_market_day_return_pct":mu,"sharpe":mu/sd*(252**0.5) if sd>0 else None,
            "sortino":mu/dsd*(252**0.5) if dsd>0 else None,"profit_factor":_profit_factor(window_trade_returns),
            "maximum_drawdown_pct":local_dd,
        })
'''
if old not in s:
    raise SystemExit('week/month aggregation marker not found')
s=s.replace(old,new,1)
s=s.replace('''        "profitable_week_pct":100*sum(x>0 for x in weeks.values())/len(weeks) if weeks else None,
        "profitable_month_pct":100*sum(x>0 for x in months.values())/len(months) if months else None,
''','''        "profitable_week_pct":100*sum(x>0 for x in week_returns.values())/len(week_returns) if week_returns else None,
        "profitable_month_pct":100*sum(x>0 for x in month_returns.values())/len(month_returns) if month_returns else None,
        "weekly_returns_pct":{f"{y}-W{w:02d}":v for (y,w),v in week_returns.items()},
        "monthly_returns_pct":{f"{y}-{m:02d}":v for (y,m),v in month_returns.items()},
        "monthly_return_dispersion_pct":(sum((x-mean(month_returns.values()))**2 for x in month_returns.values())/(len(month_returns)-1))**0.5 if len(month_returns)>1 else None,
''')
old='''        "estimated_total_trading_friction":sum(d["estimated_costs"] for d in daily_rows),
    }
'''
new='''        "estimated_total_trading_friction":sum(d["estimated_costs"] for d in daily_rows),
        "best_market_day_pnl_share_pct":100*max(daily_pnl)/sum(daily_pnl) if daily_pnl and sum(daily_pnl)>0 else None,
        "best_week_return_share_pct":100*max(week_returns.values())/sum(week_returns.values()) if week_returns and sum(week_returns.values())>0 else None,
        "best_month_return_share_pct":100*max(month_returns.values())/sum(month_returns.values()) if month_returns and sum(month_returns.values())>0 else None,
        "rolling_20_market_day":rolling_20,
        "rolling_20_min_compounded_return_pct":min((x["compounded_return_pct"] for x in rolling_20 if x["compounded_return_pct"] is not None),default=None),
        "rolling_20_min_profit_factor":min((x["profit_factor"] for x in rolling_20 if x["profit_factor"] is not None and math.isfinite(x["profit_factor"])),default=None),
        "rolling_20_min_sharpe":min((x["sharpe"] for x in rolling_20 if x["sharpe"] is not None),default=None),
        "rolling_20_min_sortino":min((x["sortino"] for x in rolling_20 if x["sortino"] is not None),default=None),
    }
'''
if old not in s:
    raise SystemExit('portfolio metrics end marker not found')
s=s.replace(old,new,1)

# Insert chronology helpers before scorecard.
marker='''def _scorecard(metrics: dict[str,Any], stress: list[dict[str,Any]], config: StrategyEconomicsConfig, stage: str, mode: str) -> tuple[dict[str,Any],str]:
'''
if marker not in s:
    raise SystemExit('scorecard marker not found')
helpers=r'''def _chronology_pass(candidate_id: UUID | str, config_hash: str, stage: str) -> bool:
    required = {
        "discovery": [],
        "validation": ["discovery"],
        "research_confirmation": ["discovery", "validation"],
        "custom_presealed": [],
        "sealed_holdout": ["discovery", "validation", "research_confirmation"],
    }.get(stage, [])
    if not required:
        return True
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT research_stage,classification,summary,scorecard FROM ra_strategy_economics_runs
                   WHERE candidate_id=%s AND strategy_config_hash=%s AND mode='research' AND status='completed'""",
                (candidate_id, config_hash),
            )
            rows=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    by_stage={r["research_stage"]:r for r in rows}
    for needed in required:
        row=by_stage.get(needed)
        if not row:
            return False
        metrics=dict((row.get("summary") or {}).get("primary_metrics") or {})
        score=dict(row.get("scorecard") or {})
        if (metrics.get("net_expected_value_pct") or 0)<=0 or (metrics.get("average_return_per_market_day_pct") or 0)<=0:
            return False
        if not score.get("economic_quality_pass"):
            return False
    return True


def _frozen_presealed_evidence(candidate_id: UUID | str, config_hash: str) -> tuple[dict[str,Any],list[dict[str,Any]]]:
    ledger=assert_strategy_frozen(candidate_id,config_hash)
    run_id=ledger.get("strategy_economics_run_id")
    if not run_id:
        raise ValueError("Frozen strategy is missing its pre-sealed economics run")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scorecard,research_stage,classification FROM ra_strategy_economics_runs WHERE id=%s AND status='completed' AND mode='research'",(run_id,))
            run=cur.fetchone()
            if not run or run["research_stage"]!='research_confirmation':
                raise ValueError("Sealed strategy requires a frozen research-confirmation economics run")
            cur.execute("SELECT capital_level,entry_delay_minutes,round_trip_cost_bps,metrics FROM ra_strategy_stress_results WHERE strategy_run_id=%s",(run_id,))
            stress=[dict(r) for r in cur.fetchall()]
        conn.rollback()
    return dict(run.get("scorecard") or {}),stress


'''
s=s.replace(marker,helpers+marker,1)
# Replace scorecard signature/body classification core by targeted edits.
s=s.replace('def _scorecard(metrics: dict[str,Any], stress: list[dict[str,Any]], config: StrategyEconomicsConfig, stage: str, mode: str) -> tuple[dict[str,Any],str]:',
            'def _scorecard(metrics: dict[str,Any], stress: list[dict[str,Any]], config: StrategyEconomicsConfig, stage: str, mode: str, chronology_pass: bool=True, inherited_scorecard: dict[str,Any] | None=None) -> tuple[dict[str,Any],str]:')
old='''    economic = bool((metrics.get("net_expected_value_pct") or 0)>0 and (metrics.get("profit_factor") or 0)>1 and (metrics.get("average_return_per_market_day_pct") or 0)>0)
    execution = bool((stress_net(30,0) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,1) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,2) or -1)>0)
    delay5 = (stress_net(config.base_round_trip_cost_bps,5) or -1)>0
    maxdd=metrics.get("maximum_drawdown_pct")
    risk = bool(maxdd is not None and maxdd >= -abs(config.max_acceptable_drawdown_pct))
    concentration=metrics.get("top_10pct_return_share_pct")
    tail = bool(concentration is None or concentration<=150)
    credibility=bool((metrics.get("independent_event_count") or 0)>=30 and (metrics.get("trades") or 0)>=100)
'''
new='''    economic = bool((metrics.get("net_expected_value_pct") or 0)>0 and (metrics.get("profit_factor") or 0)>1 and (metrics.get("average_return_per_market_day_pct") or 0)>0)
    if mode=="sealed" and inherited_scorecard:
        execution=bool(inherited_scorecard.get("execution_quality_pass"))
        delay5=bool(inherited_scorecard.get("five_minute_delay_positive"))
        risk=bool(inherited_scorecard.get("risk_quality_pass"))
        tail=bool(inherited_scorecard.get("return_concentration_pass"))
        credibility=bool(inherited_scorecard.get("statistical_credibility_pass"))
    else:
        execution = bool((stress_net(30,0) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,1) or -1)>0 and (stress_net(config.base_round_trip_cost_bps,2) or -1)>0)
        delay5 = (stress_net(config.base_round_trip_cost_bps,5) or -1)>0
        maxdd=metrics.get("maximum_drawdown_pct")
        risk = bool(maxdd is not None and maxdd >= -abs(config.max_acceptable_drawdown_pct))
        concentration=metrics.get("top_10pct_return_share_pct")
        tail = bool(concentration is None or concentration<=150)
        credibility=bool((metrics.get("independent_event_count") or 0)>=30 and (metrics.get("trades") or 0)>=100)
    concentration=metrics.get("top_10pct_return_share_pct")
'''
if old not in s:
    raise SystemExit('scorecard core marker not found')
s=s.replace(old,new,1)
s=s.replace('''        "economic_quality_pass":economic,"execution_quality_pass":execution,"five_minute_delay_positive":delay5,
''','''        "economic_quality_pass":economic,"execution_quality_pass":execution,"five_minute_delay_positive":delay5,
        "chronology_pass":chronology_pass,
''',1)
old='''    if not economic:
        classification="exploratory"
    elif economic and not (execution and risk and credibility):
        classification="promising"
    elif mode=="sealed" and execution and risk and credibility:
        classification="deployment_candidate"
    elif stage in {"validation","research_confirmation"} and execution and risk and credibility:
        classification="out_of_sample_validated"
    else:
        classification="statistically_credible"
'''
new='''    if not economic:
        classification="exploratory"
    elif economic and not (execution and risk and credibility and chronology_pass):
        classification="promising"
    elif mode=="sealed" and execution and risk and credibility and chronology_pass:
        classification="deployment_candidate"
    elif stage in {"validation","research_confirmation"} and execution and risk and credibility and chronology_pass:
        classification="out_of_sample_validated"
    else:
        classification="statistically_credible"
'''
if old not in s:
    raise SystemExit('scorecard classification marker not found')
s=s.replace(old,new,1)

# run_strategy_economics: use frozen presealed evidence in sealed mode and chronology gate everywhere.
old='''    if config.mode=="sealed":
        assert_strategy_frozen(candidate["id"],config_hash)
    run_id=_ensure_run(job_id,candidate,config,config_hash)
'''
new='''    inherited_scorecard=None
    inherited_stress=[]
    if config.mode=="sealed":
        inherited_scorecard,inherited_stress=_frozen_presealed_evidence(candidate["id"],config_hash)
    chronology_pass=_chronology_pass(candidate["id"],config_hash,config.research_stage)
    if not chronology_pass and config.research_stage in {"validation","research_confirmation","sealed_holdout"}:
        raise ValueError(f"Identical executable strategy methodology has not passed all prerequisite chronological stages for {config.research_stage}")
    run_id=_ensure_run(job_id,candidate,config,config_hash)
'''
if old not in s:
    raise SystemExit('run chronology marker not found')
s=s.replace(old,new,1)
old='''        capital_stress=[x for x in stress_output if float(x["capital_level"])==float(capital)]
        scorecard,classification=_scorecard(all_metrics[str(capital)],capital_stress,config,config.research_stage,config.mode)
'''
new='''        capital_stress=[x for x in (stress_output if config.mode=="research" else inherited_stress) if float(x["capital_level"])==float(capital)]
        scorecard,classification=_scorecard(all_metrics[str(capital)],capital_stress,config,config.research_stage,config.mode,chronology_pass,inherited_scorecard)
'''
if old not in s:
    raise SystemExit('run scorecard marker not found')
s=s.replace(old,new,1)
s=s.replace('''        "primary_scorecard":primary_scorecard,"all_capital_metrics":all_metrics,
''','''        "primary_scorecard":primary_scorecard,"chronology_pass":chronology_pass,"all_capital_metrics":all_metrics,
''',1)

# Freeze only after research-confirmation and full standalone economics gates.
old='''            if not run:
                raise ValueError("Strategy freeze requires a completed pre-sealed strategy-economics run")
            config=StrategyEconomicsConfig.model_validate(run["config"])
'''
new='''            if not run:
                raise ValueError("Strategy freeze requires a completed pre-sealed strategy-economics run")
            if run["research_stage"]!="research_confirmation" or run["classification"]!="out_of_sample_validated":
                raise ValueError("Strategy freeze requires the identical methodology to pass Discovery, Validation and Research Confirmation whole-strategy economics")
            if not _chronology_pass(candidate_id,str(run["strategy_config_hash"]),"research_confirmation"):
                raise ValueError("Strategy chronology is incomplete; freeze is not permitted")
            config=StrategyEconomicsConfig.model_validate(run["config"])
'''
if old not in s:
    raise SystemExit('freeze gate marker not found')
s=s.replace(old,new,1)

p.write_text(s)

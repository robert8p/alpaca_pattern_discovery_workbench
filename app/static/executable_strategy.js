/* Whole-strategy economics UI. Hit rate is diagnostic, not the optimisation target. */
(function(){
  const $q=(s,r=document)=>r.querySelector(s);
  const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
  const fmt=(v,d=2)=>v===null||v===undefined||Number.isNaN(Number(v))?'—':Number(v).toFixed(d);
  let featureSets=[];

  async function getJson(url,opts){
    const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...opts});
    if(!r.ok){let m=`HTTP ${r.status}`;try{const j=await r.json();m=j.detail||j.message||m;}catch{}throw new Error(m);} return r.json();
  }
  function csvNumbers(value){return String(value||'').split(',').map(x=>Number(x.trim())).filter(Number.isFinite);}

  function inject(){
    if($q('[data-view="strategy-economics"]')) return;
    const jobsNav=$q('[data-view="jobs"]');
    jobsNav?.insertAdjacentHTML('beforebegin','<button class="nav-item" data-view="strategy-economics"><span>◫</span> Strategy economics</button>');
    const jobsView=$q('#view-jobs');
    jobsView?.insertAdjacentHTML('beforebegin',`
      <section class="view" id="view-strategy-economics">
        <div class="readonly-banner"><b>Governing objective:</b> rank complete executable strategies on net expectancy, portfolio return, drawdown, tail risk, capital utilisation, capacity, execution and robustness. Hit rate remains a diagnostic only.</div>
        <div class="split-grid">
          <article class="panel"><div class="panel-head"><div><h2>Whole-strategy economics</h2><p>Evaluate one frozen rule under an explicit executable portfolio methodology.</p></div></div>
            <form id="strategy-form">
              <label>Candidate UUID<input id="strategy-candidate-id" required placeholder="candidate UUID"></label>
              <label>Completed feature set<select id="strategy-feature-set" required></select></label>
              <div class="two-col"><label>Research stage<select id="strategy-stage"><option value="discovery">Discovery</option><option value="validation">Validation</option><option value="research_confirmation">Research Confirmation</option><option value="custom_presealed">Custom pre-sealed diagnostic</option></select></label><label>Signal-strength diagnostic<select id="strategy-strength"><option value="">None</option><option value="ret_5m_pct">5m return</option><option value="relative_volume_20bar">Relative volume</option><option value="relative_trade_count_20bar">Relative trade count</option><option value="activity_impact_change_ratio">Activity-adjusted impact change</option></select></label></div>
              <div class="two-col"><label>Start date<input id="strategy-start" type="date" required></label><label>End date<input id="strategy-end" type="date" required></label></div>
              <div class="two-col"><label>Capital levels<input id="strategy-capital" value="10000,50000,100000"></label><label>Position size (% capital)<input id="strategy-position" type="number" step="0.1" value="5"></label></div>
              <div class="two-col"><label>Max simultaneous positions<input id="strategy-max-positions" type="number" value="20"></label><label>Max symbol exposure (%)<input id="strategy-symbol-exposure" type="number" step="0.1" value="10"></label></div>
              <div class="two-col"><label>Max gross exposure (%)<input id="strategy-gross" type="number" step="1" value="100"></label><label>Max net exposure (%)<input id="strategy-net" type="number" step="1" value="100"></label></div>
              <div class="two-col"><label>Cost stress (bps)<input id="strategy-costs" value="20,25,30,40"></label><label>Latency stress (minutes)<input id="strategy-delays" value="0,1,2,5"></label></div>
              <div class="two-col"><label>Max bar participation (%)<input id="strategy-bar-participation" type="number" step="0.1" value="1"></label><label>Max daily participation (%)<input id="strategy-day-participation" type="number" step="0.01" value="0.1"></label></div>
              <div class="callout"><strong>Execution model</strong><span>Fixed-fraction sizing, simultaneous-position limits, partial fills, liquidity participation, overlapping portfolio exposure, 20/25/30/40 bps stress and 0/1/2/5-minute latency stress. Sector limits stay disabled until point-in-time sector metadata exists.</span></div>
              <div class="action-row"><button class="primary" type="submit">Queue whole-strategy economics</button><button class="ghost" type="button" id="strategy-refresh">Refresh candidate runs</button></div>
            </form>
          </article>
          <article class="panel"><div class="panel-head"><div><h2>Economic scorecard</h2><p>Underlying metrics stay visible; no single composite score hides failures.</p></div></div><div id="strategy-results" class="result-stack">Select a candidate and run or refresh.</div></article>
        </div>
        <article class="panel"><div class="panel-head"><div><h2>Freeze executable methodology</h2><p>Only a Research Confirmation run that passed identical-methodology Discovery + Validation chronology can be frozen before sealed testing.</p></div></div><div class="two-col"><label>Candidate UUID<input id="strategy-freeze-candidate"></label><label>Strategy run UUID<input id="strategy-freeze-run"></label></div><label>Freeze note<input id="strategy-freeze-note" placeholder="optional"></label><div class="action-row"><button class="ghost" id="strategy-freeze" type="button">Freeze executable strategy</button></div></article>
        <div class="callout warning"><strong>Sealed holdout remains protected.</strong><span>This screen does not expose 4 August 2026 onward. Sealed whole-strategy evaluation requires the exact frozen strategy hash and cannot be used to change sizing, costs, conflict rules or risk limits.</span></div>
      </section>`);
  }

  function openView(){
    document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='view-strategy-economics'));
    document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view==='strategy-economics'));
    const title=$q('#page-title'),sub=$q('#page-subtitle'); if(title)title.textContent='Whole-Strategy Economics'; if(sub)sub.textContent='Executable portfolio economics after costs, capacity, overlap, drawdown and tail risk.';
    loadFeatureSets();
  }

  async function loadFeatureSets(){
    try{
      featureSets=(await getJson('/api/feature-sets?limit=200')).filter(x=>x.status==='completed');
      const sel=$q('#strategy-feature-set'); if(sel)sel.innerHTML='<option value="">Select completed feature set</option>'+featureSets.map(f=>`<option value="${esc(f.id)}">${esc(f.name)} · ${esc(String(f.min_trade_date||'').slice(0,10))} → ${esc(String(f.max_trade_date||'').slice(0,10))}</option>`).join('');
    }catch(e){console.warn(e);}
  }

  async function queueStrategy(e){
    e.preventDefault(); const cid=$q('#strategy-candidate-id').value.trim(); if(!cid)return;
    const costs=csvNumbers($q('#strategy-costs').value),delays=csvNumbers($q('#strategy-delays').value).map(Number),capital=csvNumbers($q('#strategy-capital').value);
    const payload={
      target_feature_set_id:$q('#strategy-feature-set').value,mode:'research',research_stage:$q('#strategy-stage').value,
      start_date:$q('#strategy-start').value,end_date:$q('#strategy-end').value,capital_levels:capital,
      base_entry_delay_minutes:0,entry_delays_minutes:delays,base_round_trip_cost_bps:20,round_trip_costs_bps:costs,
      position_size_pct_of_capital:Number($q('#strategy-position').value),max_positions:Number($q('#strategy-max-positions').value),
      max_gross_exposure_pct:Number($q('#strategy-gross').value),max_net_exposure_pct:Number($q('#strategy-net').value),max_symbol_exposure_pct:Number($q('#strategy-symbol-exposure').value),
      max_bar_participation_pct:Number($q('#strategy-bar-participation').value),max_daily_participation_pct:Number($q('#strategy-day-participation').value),
      signal_strength_field:$q('#strategy-strength').value||null,signal_priority:'liquidity_desc',one_position_per_symbol:true,allow_partial_fills:true,min_fill_fraction:0.5
    };
    try{const job=await getJson(`/api/candidates/${encodeURIComponent(cid)}/strategy-economics`,{method:'POST',body:JSON.stringify(payload)});$q('#strategy-results').innerHTML=`<div class="health-line"><strong>Queued</strong><span>Job ${esc(job.id)} · ${esc(payload.research_stage)}</span></div>`;}catch(err){alert(err.message);}
  }

  async function refreshRuns(){
    const cid=$q('#strategy-candidate-id').value.trim(); if(!cid)return;
    try{
      const rows=await getJson(`/api/candidates/${encodeURIComponent(cid)}/strategy-economics`);
      if(!rows.length){$q('#strategy-results').textContent='No whole-strategy runs yet.';return;}
      $q('#strategy-results').innerHTML=rows.map(r=>{const m=(r.summary||{}).primary_metrics||{},s=r.scorecard||{};return `<div class="candidate-card"><div class="candidate-meta"><span class="badge ${esc(r.status)}">${esc(r.status)}</span><span class="badge">${esc(r.research_stage)}</span><span class="badge">${esc(r.classification)}</span></div><strong>${esc(String(r.id).slice(0,8))} · ${fmt(m.return_on_total_capital_pct)}% total capital</strong><div class="health-line"><span>Net EV / trade</span><b>${fmt(m.net_expected_value_pct)}%</b></div><div class="health-line"><span>Median trade</span><b>${fmt(m.median_net_trade_return_pct)}%</b></div><div class="health-line"><span>Profit factor</span><b>${fmt(m.profit_factor)}</b></div><div class="health-line"><span>All-market-day mean</span><b>${fmt(m.average_return_per_market_day_pct)}%</b></div><div class="health-line"><span>Max drawdown</span><b>${fmt(m.maximum_drawdown_pct)}%</b></div><div class="health-line"><span>Capital utilisation</span><b>${fmt(m.average_capital_utilisation_pct)}%</b></div><div class="health-line"><span>Trade win rate (diagnostic)</span><b>${fmt(m.trade_win_rate_pct)}%</b></div><div class="health-line"><span>30bps / latency gate</span><b>${s.cost_30bps_positive?'pass':'fail'} / ${s.execution_quality_pass?'pass':'fail'}</b></div><div class="health-line"><span>Chronology</span><b>${s.chronology_pass?'pass':'not yet'}</b></div></div>`;}).join('');
    }catch(err){$q('#strategy-results').textContent=err.message;}
  }

  async function freezeStrategy(){
    const cid=$q('#strategy-freeze-candidate').value.trim(),rid=$q('#strategy-freeze-run').value.trim(); if(!cid||!rid)return alert('Enter candidate and strategy-run UUIDs.');
    if(!confirm('Freeze this exact executable strategy methodology before sealed testing?'))return;
    try{await getJson(`/api/research-ledger/candidates/${encodeURIComponent(cid)}/freeze-strategy/${encodeURIComponent(rid)}`,{method:'POST',body:JSON.stringify({notes:$q('#strategy-freeze-note').value||null})});alert('Executable strategy frozen in the Research Ledger.');}catch(err){alert(err.message);}
  }

  function addCandidateButtons(){
    document.querySelectorAll('.candidate-card').forEach(card=>{const inspect=card.querySelector('.candidate-inspect');const actions=card.querySelector('.candidate-actions');if(!inspect||!actions||actions.querySelector('.strategy-open'))return;const id=inspect.dataset.id;actions.insertAdjacentHTML('beforeend',`<button class="ghost strategy-open" data-id="${esc(id)}">Strategy economics</button>`);});
  }

  inject();
  $q('[data-view="strategy-economics"]')?.addEventListener('click',openView);
  $q('#strategy-form')?.addEventListener('submit',queueStrategy);
  $q('#strategy-refresh')?.addEventListener('click',refreshRuns);
  $q('#strategy-freeze')?.addEventListener('click',freezeStrategy);
  document.addEventListener('click',e=>{const b=e.target.closest('.strategy-open');if(!b)return;$q('#strategy-candidate-id').value=b.dataset.id;$q('#strategy-freeze-candidate').value=b.dataset.id;openView();refreshRuns();});
  const target=$q('#candidate-list'); if(target)new MutationObserver(addCandidateButtons).observe(target,{childList:true,subtree:true}); addCandidateButtons();
})();

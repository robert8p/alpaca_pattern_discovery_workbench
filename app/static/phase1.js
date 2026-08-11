/* Phase 1 Full-History Clustered Discovery UI extension. */
(function(){
  const $q=(s,r=document)=>r.querySelector(s);
  const esc=v=>String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
  const n=v=>Number(v||0).toLocaleString();
  const day=v=>v?String(v).slice(0,10):'—';

  function injectPhase1View(){
    if($q('[data-view="full-history"]')) return;
    const jobsNav=$q('[data-view="jobs"]');
    jobsNav?.insertAdjacentHTML('beforebegin','<button class="nav-item" data-view="full-history"><span>▦</span> Full history</button>');
    const jobsView=$q('#view-jobs');
    jobsView?.insertAdjacentHTML('beforebegin',`
      <section class="view" id="view-full-history">
        <div class="readonly-banner"><b>Phase 1 only:</b> infrastructure is installed, but full historical backfill and clustered discovery remain locked until the point-in-time historical source gate is green. The true sealed holdout begins 4 August 2026.</div>
        <div class="metric-grid">
          <article class="metric"><span>Historical source</span><strong id="fh-source-range">—</strong><small>Alpaca SIP · 1Min · raw</small></article>
          <article class="metric"><span>PTI source gate</span><strong id="fh-pti-ready">—</strong><small>61-day warm-up + inactive survivorship supplement</small></article>
          <article class="metric"><span>Months available</span><strong id="fh-months-available">—</strong><small>4 May 2025 through pre-sealed history</small></article>
          <article class="metric"><span>Months completed</span><strong id="fh-months-completed">—</strong><small>Historical engineered-feature backfill</small></article>
          <article class="metric"><span>Rows processed</span><strong id="fh-rows-processed">—</strong><small>Idempotent feature rows</small></article>
        </div>
        <div class="split-grid">
          <article class="panel"><div class="panel-head"><div><h2>Full-History Clustered Discovery</h2><p>Backfill readiness and resumable execution state</p></div><button class="ghost" id="fh-refresh">Refresh status</button></div><div id="fh-backfill-status" class="result-stack">Loading…</div></article>
          <article class="panel"><div class="panel-head"><div><h2>Phase 1 infrastructure</h2><p>Point-in-time source, market state, candidate waves and audit controls</p></div></div><div id="fh-infrastructure-status" class="result-stack">Loading…</div></article>
        </div>
        <article class="panel"><div class="panel-head"><div><h2>Research chronology</h2><p>Enforced by application validation and a PostgreSQL job guard.</p></div></div><div id="fh-periods" class="workflow-grid"></div></article>
        <article class="panel"><div class="panel-head"><div><h2>Candidate freeze</h2><p>Freeze the exact rule in the Research Ledger before any true sealed outcome can be evaluated.</p></div></div><div class="two-col"><label>Candidate UUID<input id="fh-freeze-candidate" placeholder="candidate UUID"></label><label>Freeze note<input id="fh-freeze-note" placeholder="optional note"></label></div><div class="action-row"><button class="ghost" id="fh-freeze">Freeze candidate</button></div></article>
        <div class="callout warning"><strong>Clustered discovery has not been launched.</strong><span>The next research execution step remains a controlled one-day May 2025 point-in-time feature test after all raw-source readiness checks are green.</span></div>
      </section>`);
  }

  async function getJson(url,opts){
    const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...opts});
    if(!r.ok){let m=`HTTP ${r.status}`;try{const j=await r.json();m=j.detail||j.message||m;}catch{}throw new Error(m);} return r.json();
  }

  async function refreshFullHistory(){
    const d=await getJson('/api/full-history/status');
    const inv=d.historical_source_coverage||{}, b=d.backfill||{}, j=d.resume_retry_state||{}, cp=d.current_processing_partition||{}, pti=d.point_in_time_source_readiness||{};
    $q('#fh-source-range').textContent=inv.min_bar_ts?`${day(inv.min_bar_ts)} → ${day(inv.max_bar_ts)}`:'Not available';
    $q('#fh-pti-ready').textContent=pti.ready?'READY':'BLOCKED';
    $q('#fh-months-available').textContent=n(d.months_available); $q('#fh-months-completed').textContent=n(d.months_completed); $q('#fh-rows-processed').textContent=n(d.rows_processed);
    const blockers=(pti.blockers||[]).map(x=>esc(x)).join(' · ')||'None';
    const ptiWindow=pti.required_warmup_start?`${day(pti.required_warmup_start)} → ${day(pti.required_warmup_end)}`:'—';
    $q('#fh-backfill-status').innerHTML=`<div class="health-line"><strong>Point-in-time source gate</strong><span>${pti.ready?'READY':'BLOCKED'} · warm-up ${ptiWindow}</span></div><div class="health-line"><strong>Source blockers</strong><span>${blockers}</span></div><div class="health-line"><strong>Active-history coverage</strong><span>${pti.active_history_ready?'Ready':'Pending'}</span></div><div class="health-line"><strong>All-known 61-day warm-up</strong><span>${pti.all_known_warmup_ready?'Ready':'Pending'}</span></div><div class="health-line"><strong>Inactive survivorship supplement</strong><span>${pti.inactive_survivorship_ready?'Ready':'Pending'}</span></div><div class="health-line"><strong>Feature-backfill readiness</strong><span>${esc(b.status||'Architecture ready · no full run launched')}</span></div><div class="health-line"><strong>Feature definition</strong><span>v${esc(d.feature_engine_version||'—')} · ${esc(String(d.feature_definition_hash||'').slice(0,16))}</span></div><div class="health-line"><strong>Current partition</strong><span>${cp.chunk_start?`${day(cp.chunk_start)} → ${day(cp.chunk_end)} · ${esc(cp.status)}`:'None'}</span></div><div class="health-line"><strong>Resume / retry</strong><span>${j.status?`${esc(j.status)} · ${esc(j.phase||'—')} · ${n(j.progress_current)}/${n(j.progress_total)} · attempts ${n(j.attempts)}`:'No active historical feature job'}</span></div><div class="health-line"><strong>Latest error</strong><span>${esc(d.latest_error||'None')}</span></div>`;
    const ms=d.market_state_feature_status||{},wv=d.candidate_wave_infrastructure_status||{},rl=d.research_ledger_status||{},sp=d.sealed_period_protection||{},infra=d.infrastructure||{};
    $q('#fh-infrastructure-status').innerHTML=`<div class="health-line"><strong>PTI methodology</strong><span>${esc(pti.methodology_version||'—')} · ${n(pti.prior_trading_dates)} prior trading dates</span></div><div class="health-line"><strong>Market-state feature layer</strong><span>${infra.market_state_table?'Ready':'Missing'} · ${n(ms.runs)} runs · ${n(ms.rows)} rows</span></div><div class="health-line"><strong>Candidate-wave layer</strong><span>${infra.wave_table?'Ready':'Missing'} · ${n(wv.runs)} runs · ${n(wv.rows)} rows</span></div><div class="health-line"><strong>Research Ledger</strong><span>${infra.ledger_table?'Ready':'Missing'} · ${n(rl.entries)} candidates · ${n(rl.frozen)} frozen</span></div><div class="health-line"><strong>Sealed protection</strong><span>${sp.enabled?'ENFORCED':'NOT READY'} · starts ${day(sp.sealed_start)} · freeze required</span></div><div class="health-line"><strong>Full-history execution</strong><span>${sp.full_history_execution_enabled?'Enabled':'Locked for Phase 1'}</span></div>`;
    $q('#fh-periods').innerHTML=(d.research_periods||[]).map(p=>`<article class="workflow-card"><em>${p.stage_order}</em><strong>${esc(p.stage.replaceAll('_',' '))}</strong><span>${day(p.start_date)} → ${p.end_date?day(p.end_date):'onward'}${p.sealed?' · SEALED':''}</span></article>`).join('');
  }

  async function freezeFromPanel(){
    const id=$q('#fh-freeze-candidate').value.trim(); if(!id) return alert('Enter the exact candidate UUID first.');
    if(!confirm('Freeze this exact candidate definition in the Research Ledger?')) return;
    try{await getJson(`/api/research-ledger/candidates/${encodeURIComponent(id)}/freeze`,{method:'POST',body:JSON.stringify({notes:$q('#fh-freeze-note').value||null})}); alert('Candidate frozen in the Research Ledger.'); await refreshFullHistory(); await refreshFrozenCandidateIds(); if(typeof refreshCandidates==='function') await refreshCandidates();}catch(e){alert(e.message);}
  }

  let frozenCandidateIds=new Set();
  async function refreshFrozenCandidateIds(){
    try{
      const rows=await getJson('/api/research-ledger?limit=1000');
      frozenCandidateIds=new Set((rows||[]).filter(x=>x.candidate_freeze_timestamp).map(x=>x.candidate_id));
    }catch(e){console.warn('Research Ledger freeze status unavailable',e);}
    enforceCandidateFreezeUi();
  }

  function enforceCandidateFreezeUi(){
    document.querySelectorAll('.candidate-card').forEach(card=>{
      const id=card.querySelector('.candidate-inspect')?.dataset.id; if(!id)return;
      const actions=card.querySelector('.candidate-actions');
      const sealed=actions?.querySelector('.sealed-open');
      const frozen=frozenCandidateIds.has(id);
      if(sealed){sealed.disabled=!frozen;sealed.title=frozen?'Candidate frozen in Research Ledger':'Freeze this exact rule in the Research Ledger first';}
      if(actions&&!frozen&&!actions.querySelector('.candidate-freeze')) actions.insertAdjacentHTML('beforeend',`<button class="ghost candidate-freeze" data-id="${esc(id)}">Freeze in Research Ledger</button>`);
      if(frozen){actions?.querySelector('.candidate-freeze')?.remove();if(!card.querySelector('.ledger-frozen-badge')) card.querySelector('.candidate-meta')?.insertAdjacentHTML('beforeend','<span class="badge completed ledger-frozen-badge">ledger frozen</span>');}
    });
  }

  function openFullHistoryView(){
    document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='view-full-history'));
    document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view==='full-history'));
    const title=$q('#page-title'),subtitle=$q('#page-subtitle');
    if(title)title.textContent='Full-History Clustered Discovery';
    if(subtitle)subtitle.textContent='Phase 1 infrastructure, point-in-time source integrity, research chronology and sealed-holdout protection.';
    refreshFullHistory().catch(e=>{const x=$q('#fh-backfill-status');if(x)x.textContent=e.message;});
  }

  injectPhase1View();
  $q('[data-view="full-history"]')?.addEventListener('click',openFullHistoryView);
  $q('#fh-refresh')?.addEventListener('click',()=>refreshFullHistory());
  $q('#fh-freeze')?.addEventListener('click',freezeFromPanel);
  document.addEventListener('click',async e=>{const b=e.target.closest('.candidate-freeze');if(!b)return; e.preventDefault(); $q('#fh-freeze-candidate').value=b.dataset.id; await freezeFromPanel();});
  const target=$q('#candidate-list'); if(target) new MutationObserver(enforceCandidateFreezeUi).observe(target,{childList:true,subtree:true});
  refreshFrozenCandidateIds();
})();

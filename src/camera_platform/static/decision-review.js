'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const escape = v => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (v, places=2) => v === null || v === undefined ? '해당 없음' : Number(v).toFixed(places);
  const percent = v => v === null || v === undefined ? '해당 없음' : number(v*100,3)+'%';
  const splits = {train:'선택용 입력',development:'선택용 개발 입력',holdout:'미사용 평가',stress:'스트레스 · 적용 경계'};
  const reasons = {minimum_signal:'신호 부족',minimum_snr:'예측 SNR 부족',well_saturation:'Well 포화',raw_clipping:'RAW 상단',exposure_time:'노출 상한',dynamic_contrast_retention:'대비 유지 미달',RAW_channel_ratio_consistency:'채널비 차이 초과'};
  let records=[], current=null, epoch=0, controller=null;
  async function get(url, signal) {
    const response=await fetch(url,{signal,cache:'no-store'});
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
    return data;
  }
  function error(value) { $('error').hidden=!value; $('error').textContent=value || ''; }
  function cards() {
    $('cards').innerHTML=records.map(r => `<article class="card ${r.id===current?.id?'active':''}" data-decision="${escape(r.id)}"><div class="eyebrow">${r.id==='signal'?'SIGNAL PRIORITY':'MOTION + CHANNEL CONSISTENCY'}</div><h2>${escape(r.title)}</h2><div class="winner">${escape(r.selected_candidate_id)}</div><p>${r.id==='signal'?'신호·SNR·포화·노출을 함께 고려한 결정입니다. 움직임과 채널비 비용은 목적에 포함하지 않았습니다.':'기존 두 후보에 움직임·고정 보정 후 채널비 조건을 추가한 별도 결정입니다.'}</p><p class="note">적합 후보 ${r.feasible_count}/${r.candidate_count} · 고정 시각 ${escape(r.frozen_at_utc)}</p><button type="button" data-profile="${escape(r.id)}" aria-pressed="${r.id===current?.id}">이 결정 기록 보기</button></article>`).join('');
  }
  function choose(id, push=true) {
    ++epoch; controller?.abort();
    current=records.find(r=>r.id===id) || records[0];
    if(!current) return;
    cards(); $('decision').hidden=false; $('raw-panel').hidden=true; error('');
    $('decision-title').textContent=current.title;
    $('decision-meta').textContent=`저장된 선택: ${current.selected_candidate_id} · 선택을 변경하는 화면이 아닙니다.`;
    $('limits').innerHTML=current.limits.map(l=>`<dt>${escape(l.label)}</dt><dd>${number(l.value, l.value<1?3:1)}</dd>`).join('');
    $('groups').innerHTML=current.evaluation_groups.map(g=>`<p><strong>${escape(splits[g.split])}</strong> · ${g.noise_enabled?'잡음 ON 표본':'잡음 OFF 입력'}: <span class="${g.passed===g.count?'good':'bad'}">${g.passed}/${g.count} 통과</span></p>`).join('');
    $('scope-notes').innerHTML=current.scope_notes.map(n=>`<li>${escape(n)}</li>`).join('');
    $('calibration').innerHTML=current.calibration ? `<h3>고정 neutral 보정 · 원본 RAW에 적용하지 않음</h3><pre>${escape(JSON.stringify(current.calibration,null,2))}</pre>` : '';
    $('documents').innerHTML=current.documents.map(d=>`<a href="${escape(d.url)}" download title="SHA256 ${escape(d.sha256)}">${escape(d.name)} ↓</a>`).join('');
    $('provenance').textContent=JSON.stringify(current.source_identity,null,2);
    for(const option of $('split').options) option.disabled=option.value!=='all' && !current.cases.some(c=>c.split===option.value);
    const desired=new URL(location.href).searchParams.get('split');
    $('split').value=['holdout','stress','all','development','train'].includes(desired) && !$('split').querySelector(`option[value="${desired}"]`).disabled ? desired : 'holdout';
    $('noise').checked=false; $('failures').checked=false; fillScenarios();
    if(push) updateLocation();
  }
  function subset() {
    return current.cases.filter(r=>($('split').value==='all'||r.split===$('split').value) && ($('noise').checked||!r.noise_enabled));
  }
  function fillScenarios() {
    const values=subset(), previous=$('scenario').value;
    const ids=[...new Set(values.map(r=>r.scenario_id))].filter(id=>!$('failures').checked || values.some(r=>r.scenario_id===id && r.failures.length));
    $('scenario').innerHTML='<option value="all">구분 내 모든 입력</option>'+ids.map(id=>`<option value="${escape(id)}">${escape(id)}</option>`).join('');
    $('scenario').value=ids.includes(previous)?previous:'all';
    renderRows();
  }
  function renderRows() {
    ++epoch; controller?.abort(); $('raw-panel').hidden=true;
    const values=subset();
    const failedScenarios=new Set(values.filter(r=>r.failures.length).map(r=>r.scenario_id));
    const visible=values.filter(r=>($('scenario').value==='all'||r.scenario_id===$('scenario').value)&&(!$('failures').checked||failedScenarios.has(r.scenario_id)));
    $('case-count').textContent=`${visible.length}개 저장 결과 · 실패 사례를 고르면 같은 입력의 통과 후보도 함께 남겨 비교합니다.`;
    $('rows').innerHTML=visible.map(r=>`<tr data-case="${escape(r.case_id)}"><td><strong>${escape(r.candidate_id)}</strong>${r.selected?' <span class="pill">저장 선택</span>':''}<small>${escape(r.scenario_id)}</small><small>${r.exposure_ms} ms / gain ${r.gain} · ${r.noise_enabled?'잡음 ON · seed '+r.seed:'잡음 OFF'}</small></td><td class="${r.failures.length?'bad':'good'}">${r.failures.length?r.failures.map(f=>escape(reasons[f]||f)).join('<br>'):'통과'}</td><td>${number(r.green_dn)}</td><td>${number(r.green_snr_p10)}</td><td>${percent(r.well_fraction)} / ${percent(r.white_fraction)}</td><td class="${r.signed_charge_transfer<0?'bad':''}">${percent(r.signed_charge_transfer)}</td><td>${r.raw_transfer_clipped?'포화로 평가 제외':percent(r.signed_raw_transfer)}</td><td>${percent(r.channel_ratio_error)}</td><td><button type="button" data-open="${escape(r.case_id)}">같은 입력 RAW</button></td></tr>`).join('');
    updateLocation();
  }
  function updateLocation() {
    if(!current)return;
    const url=new URL(location.href); url.searchParams.set('profile',current.id);url.searchParams.set('split',$('split').value);
    history.replaceState(null,'',url);
  }
  async function openCase(id) {
    const row=current.cases.find(r=>r.case_id===id); if(!row)return;
    controller?.abort();controller=new AbortController();const stamp=++epoch;
    const peers=current.cases.filter(r=>r.scenario_id===row.scenario_id && r.noise_enabled===row.noise_enabled && r.seed===row.seed);
    $('raw-panel').hidden=false; $('raw-title').textContent=row.scenario_id;
    $('raw-cards').textContent='선택한 원본을 확인하고 있습니다.';
    try {
      const details=await Promise.all(peers.map(r=>get(r.detail_url,controller.signal)));
      if(stamp!==epoch)return;
      $('raw-cards').innerHTML=details.map(d=>`<article class="card"><h3>${escape(d.candidate_id)}</h3><p class="note">원본 ${d.shape.join(' × ')} · ${escape(d.dtype)} · RAW 해시 일치</p><div class="image-pair"><figure><img src="${escape(d.raw_png)}" alt="${escape(d.candidate_id)} 원본 Bayer RAW"><figcaption>원본 RAW · 64–4095 DN</figcaption></figure><figure><img src="${escape(d.final_rail_png)}" alt="${escape(d.candidate_id)} 최종 RAW 상단 마스크"><figcaption>최종 코드 4095 · well 마스크 아님</figcaption></figure></div><p class="hash">SHA256 ${escape(d.raw_sha256)}</p><a href="${escape(d.raw_url)}" download>원본 정수 RAW NPY ↓</a></article>`).join('');
    } catch(e) {if(e.name!=='AbortError' && stamp===epoch){$('raw-cards').textContent='원본을 표시하지 않았습니다.';error(e.message);}}
  }
  async function load() {
    ++epoch; controller?.abort();$('loading').hidden=false;error('');
    try {const value=await get('/api/v1/decision-review');records=value.decisions;
      const home=new URL(value.workbench_url,location.origin);
      if(['127.0.0.1','localhost'].includes(home.hostname)&&home.protocol==='http:')$('workspace-link').href=home.href;
      if(records.length)choose(new URL(location.href).searchParams.get('profile')||'signal');
      else {$('cards').textContent='이 작업공간에는 연결된 저장 연구가 없습니다.';$('decision').hidden=true;}
      if(value.unavailable.length) error(value.unavailable.map(x=>x.reason).join(' '));
    } catch(e){error(e.message);$('decision').hidden=true;$('cards').textContent='기록 확인 실패 — 이전 결과로 대체하지 않습니다.';}
    finally{$('loading').hidden=true;}
  }
  $('cards').addEventListener('click',e=>{const b=e.target.closest('[data-profile]');if(b)choose(b.dataset.profile);});
  $('rows').addEventListener('click',e=>{const b=e.target.closest('[data-open]');if(b)openCase(b.dataset.open);});
  for(const id of ['split','noise','failures'])$(id).addEventListener('change',fillScenarios);
  $('scenario').addEventListener('change',renderRows);$('reload').addEventListener('click',load);
  load();
})();

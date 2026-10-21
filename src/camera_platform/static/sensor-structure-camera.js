/* Stored, actually executed OCL response sensitivity results. No synthetic fallback. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const endpoint = '/api/v1/sensor-structure-camera-studies';
  const query = new URLSearchParams(location.search);
  let run = query.get('run') || '';
  let summary = null;
  let timer = null;
  let selectedCase = null;
  const fieldNames = {center: '중심', 'outer-ne': '주변부 · NE'};
  const chartNames = {uniform: '균일장', color: '색 패치', texture: '질감', resolution: '해상력'};
  const profileNames = {green_only: 'Green만 적용', achromatic_shared: '모든 채널 공통 적용'};
  const classificationNames = {magnitude_stable:'영향 크기 안정', direction_stable:'방향 안정', indistinguishable:'구분 어려움', unresolved:'미결정', numerical_evidence:'수치 해석 근거', observed:'수치 해석 근거', measured:'취득·계산값', assumption:'적용 가정', assumed:'적용 가정', model_assumption:'모델 가정', limitation:'지원 범위 제한', computed_stable:'계산상 안정 확인', explicit_hypothesis:'명시적인 적용 가정', reused_model:'기존 모델 재사용', model_proxy:'모델 근사값', fixed_policy:'고정 처리 정책', not_validated:'검증되지 않은 범위'};
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const fmt = (x, digits=2) => finite(x) ? x.toLocaleString('en-US', {maximumFractionDigits:digits, minimumFractionDigits:digits}) : '—';
  const signed = (x,digits=2,suffix='') => finite(x) ? `${x>0?'+':''}${fmt(x,digits)}${suffix}` : '—';
  const pair = (a,b,digits=2) => `${fmt(a,digits)} → ${fmt(b,digits)}`;
  const asText = value => typeof value === 'string' ? value : value == null ? '—' : JSON.stringify(value);
  const make = (tag, text, className) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
  const appendCell = (row,text,className) => row.appendChild(make('td',text,className));
  const fileUrl = path => {
    if(typeof path !== 'string' || !path || path.startsWith('/') || path.split('/').some(x=>x==='..')) return null;
    return `${endpoint}/${encodeURIComponent(run)}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
  };
  async function json(url) {const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`${response.status} ${response.statusText} · ${url}`);return response.json();}
  function link(parent,label,url) {if(!url)return;const a=make('a',label);a.href=url;a.target='_blank';a.rel='noopener';parent.append(a);}
  function selectOptions(id,values,names,initial) {
    const select=$(id), previous=select.value || query.get(id) || initial;
    select.replaceChildren();
    values.forEach(value=>{const option=make('option',names?names[value]||String(value):String(value));option.value=String(value);select.append(option);});
    if(values.some(v=>String(v)===String(previous)))select.value=String(previous);
  }
  function setImage(id,missing,path) {
    const img=$(id),url=fileUrl(path);img.hidden=!url;$(missing).hidden=!!url;
    img.onload=()=>{$(missing).hidden=true;img.hidden=false;};
    img.onerror=()=>{img.hidden=true;$(missing).hidden=false;$(missing).textContent='저장 영상 로딩 실패 · 다운로드·실행 상태 확인';};
    if(url)img.src=url;else img.removeAttribute('src');
  }
  function renderEvidence() {
    const rows=$('evidence');rows.replaceChildren();
    (summary.evidence||[]).forEach(item=>{
      const row=make('tr');appendCell(row,`${fmt(item.angle_deg,0)}°`);
      const total=make('td',`${fmt(item.total_active_si_gain,5)}× (${signed((item.total_active_si_gain-1)*100,2,'%')})`);
      total.append(make('small',classificationNames[item.total_active_si_classification]||asText(item.total_active_si_classification)));row.append(total);
      appendCell(row,`${fmt(item.central_gain,5)}× (${signed((item.central_gain-1)*100,2,'%')})`);
      appendCell(row,signed(item.neighbor_delta_pp,3,'%p'));
      appendCell(row,classificationNames[item.classification]||asText(item.classification),['magnitude_stable','direction_stable'].includes(item.classification)?'good':'warning');
      appendCell(row,Array.isArray(item.polarization_notes)?item.polarization_notes.join(' · '):asText(item.polarization_notes));rows.append(row);
    });
  }
  function renderAssumptions() {
    const container=$('assumptions');container.replaceChildren();
    (summary.assumptions||[]).forEach(item=>{
      const card=make('div',undefined,'assumption'),kind=String(item.classification||'assumption');
      card.append(make('span',classificationNames[kind]||kind,`classification${/evidence|observed|source|computed_stable/.test(kind)?' evidence':''}`),make('h3',item.label||item.id||'가정'),make('p',item.description||''));container.append(card);
    });
  }
  function renderMetrics(c) {
    const metrics=c.metrics||{},a=metrics.baseline||{},b=metrics.projected||{},changes=metrics.comparison||{};
    const table=$('site-metrics');table.replaceChildren();const bars=$('signal-bars');bars.replaceChildren();
    const allSignal=[...(a.site_mean_e||[]),...(b.site_mean_e||[])].filter(finite),maximum=Math.max(1,...allSignal);
    ['R','Gr','Gb','B'].forEach((site,i)=>{
      const row=make('tr');appendCell(row,site);appendCell(row,pair(a.site_mean_e?.[i],b.site_mean_e?.[i]));appendCell(row,signed(changes.signal_change_pct?.[i],2,'%'),'neutral');
      appendCell(row,pair(a.site_temporal_noise_e?.[i],b.site_temporal_noise_e?.[i]));appendCell(row,signed(changes.noise_change_pct?.[i],2,'%'));
      appendCell(row,pair(a.site_snr?.[i],b.site_snr?.[i]));appendCell(row,signed(changes.snr_change_pct?.[i],2,'%'));table.append(row);
      const group=make('div',undefined,'bar-group');group.append(make('h3',`${site} · 평균 신호 e⁻`));
      [a.site_mean_e?.[i],b.site_mean_e?.[i]].forEach(value=>{const bar=make('div',undefined,'bar-row'),track=make('div',undefined,'bar-track'),fill=make('div',undefined,'bar-fill');fill.style.width=`${finite(value)?Math.max(0,Math.min(100,value/maximum*100)):0}%`;track.append(fill);bar.append(track,make('span',fmt(value,1),'bar-number'));group.append(bar);});bars.append(group);
    });
    const rgb=$('rgb-metrics');rgb.replaceChildren();['R','G','B'].forEach((channel,i)=>{const row=make('tr');appendCell(row,channel);appendCell(row,fmt(a.linear_rgb_temporal_noise?.[i],5));appendCell(row,fmt(b.linear_rgb_temporal_noise?.[i],5));rgb.append(row);});
    const luma=make('tr');appendCell(luma,'휘도');appendCell(luma,fmt(a.luma_temporal_noise,5));appendCell(luma,fmt(b.luma_temporal_noise,5));rgb.append(luma);
    const other=$('other-metrics');other.replaceChildren();
    [['Well 포화',a.well_saturation_fraction,b.well_saturation_fraction,100,'%'],['ADC 포화',a.adc_saturation_fraction,b.adc_saturation_fraction,100,'%']].forEach(([name,x,y,scale,suffix])=>{const row=make('tr');appendCell(row,name);appendCell(row,finite(x)?fmt(x*scale,3)+suffix:'—');appendCell(row,finite(y)?fmt(y*scale,3)+suffix:'—');other.append(row);});
    const shift=make('tr');appendCell(shift,'기준 → 적용 색 이동 ΔE00');appendCell(shift,'—');appendCell(shift,fmt(changes.color_shift_delta_e00_mean,3));other.append(shift);
    const patches=changes.color_shift_delta_e00_patches;const hasPatches=Array.isArray(patches)&&patches.length>0;$('patch-panel').hidden=!hasPatches;$('patches').replaceChildren();
    if(hasPatches)patches.forEach((value,i)=>{const numeric=finite(value)?value:value?.delta_e00;const el=make('div',undefined,'patch');el.append(make('small',value?.label||`PATCH ${i+1}`),make('span',fmt(numeric,2)));$('patches').append(el);});
  }
  function drawCase() {
    const cases=summary?.cases||[];
    selectedCase=cases.find(c=>String(c.field_id)===$('field').value&&String(c.chart)===$('chart').value&&String(c.light_scale)===$('light').value&&String(c.profile_id)===$('profile').value);
    $('no-case').hidden=!!selectedCase;
    if(!selectedCase){$('no-case').textContent='이 조합의 완료 결과가 없습니다. 다른 저장 조건을 선택하세요.';$('image-pair').hidden=true;$('difference-panel').hidden=true;$('case-details').replaceChildren();$('case-downloads').replaceChildren();renderMetrics({});return;}
    const c=selectedCase,stage=$('stage').value,files=c.files||{},isDifference=stage==='difference';
    $('image-pair').hidden=isDifference;$('difference-panel').hidden=!isDifference;
    $('stage-a').textContent=$('stage-b').textContent=stage==='raw'?'NATIVE RAW':'FIXED ISP';
    setImage('image-a','missing-a',files[`baseline_${stage==='raw'?'raw':'isp'}`]);setImage('image-b','missing-b',files[`projected_${stage==='raw'?'raw':'isp'}`]);setImage('image-diff','missing-diff',files.difference);
    const params=new URLSearchParams(location.search);params.set('run',run);['field','chart','light','profile','stage'].forEach(id=>params.set(id,$(id).value));history.replaceState(null,'',`${location.pathname}?${params}`);
    $('case-details').replaceChildren();
    [`CASE ${c.id}`,`적용한 2D 각도 ${fmt(c.applied_angle_deg,0)}°`, `저장 광학 CRA ${fmt(c.host_cra_deg,2)}°`, `응답 배율 ${Array.isArray(c.response_gain)?c.response_gain.map(x=>fmt(x,4)).join(' / '):asText(c.response_gain)}`,`광량 ${fmt(c.light_scale,2)}×`].forEach(value=>$('case-details').append(make('span',value,'chip')));
    $('profile-explanation').textContent=c.profile_id==='green_only'?'Green-only 가정: 550 nm 단면의 중심 흡수 비율을 Gr·Gb 응답에만 확장합니다. R·B는 고정합니다. Green 전 파장에 같은 비율을 적용하는 것은 확인된 분광 응답이 아니라 선택한 가정입니다.':'Achromatic 가정: 같은 중심 흡수 비율을 모든 채널·파장에 공통 적용합니다. 이는 색 중립적인 응답 변화와 Green-only 가정의 차이를 보기 위한 비교 가정이며, 2D 계산으로 입증한 분광 효과가 아닙니다.';
    $('case-downloads').replaceChildren();[['baseline_raw','기준 RAW 표시'],['projected_raw','적용 RAW 표시'],['arrays','원본 RAW · 배열'],['metrics','지표 JSON']].forEach(([key,label])=>link($('case-downloads'),label,fileUrl(files[key])));
    $('difference-note').textContent=c.difference_display||summary.conditions?.difference_preview||'고정 표시 범위의 차이 영상';renderMetrics(c);toggleSampling();
  }
  function toggleSampling() {document.querySelectorAll('.image-frame').forEach(el=>el.classList.toggle('pixelated',$('sampling').value==='pixelated'));}
  function render() {
    $('result').hidden=false;const source=summary.source||{},conditions=summary.conditions||{},cases=summary.cases||[];
    if(summary.title)document.title=`${summary.title} | CameraE2E`;
    $('host-sensor').textContent=asText(source.host_sensor);
    $('capture').textContent=`${fmt(conditions.exposure_ms,1)} ms · ${fmt(conditions.gain,0)}×`;
    $('capture-note').textContent=`독립 취득 ${conditions.frames??'—'}회 · ${asText(conditions.illumination)}`;
    $('evidence-count').textContent=`${(summary.evidence||[]).length}개 조건`;$('case-count').textContent=`${cases.length}개 저장 결과`;
    if(source.trend_run_id)$('trend-link').href=`/sensor-structure.html?run=${encodeURIComponent(source.trend_run_id)}`;
    $('source-revision').textContent=`구조 revision: ${asText(source.structure_revision)} · 구조 계산 ${source.trend_run_id||'—'} · 카메라 취득 ${source.camera_run_id||'—'}`;
    const unique=key=>[...new Set(cases.map(c=>c[key]))];
    selectOptions('field',unique('field_id'),fieldNames,'center');selectOptions('chart',unique('chart'),chartNames,'color');selectOptions('light',unique('light_scale').sort((a,b)=>a-b),null,1);selectOptions('profile',unique('profile_id'),profileNames,'green_only');
    if(query.has('stage')&&['isp','raw','difference'].includes(query.get('stage')))$('stage').value=query.get('stage');
    renderEvidence();renderAssumptions();$('provenance').textContent=JSON.stringify({schema:summary.schema,status:summary.status,source,conditions,assumptions:summary.assumptions},null,2);
    $('summary-downloads').replaceChildren();link($('summary-downloads'),'전체 실행 · 결과 JSON',`${endpoint}/${encodeURIComponent(run)}`);
    drawCase();
  }
  async function load() {
    clearTimeout(timer);$('refresh').disabled=true;$('error').textContent='';
    try {
      if(!run){try{const latest=await json(`${endpoint}/latest`);run=latest.id||latest.run_id||'';}catch(_){/* A missing ready run is a neutral empty state. */}}
      if(!run){$('status').textContent='선택한 실행이 없습니다.';$('empty-help').hidden=false;return;}
      const job=await json(`${endpoint}/${encodeURIComponent(run)}`);const status=job.status||'unknown';
      $('run-info').textContent=`실행 ${job.id||run} · ${status}${job.progress?` · ${job.progress.completed}/${job.progress.total} · ${job.progress.stage}`:''}`;$('status-wrap').classList.toggle('ready',status==='succeeded');
      $('status').replaceChildren(make('span','', 'status-dot'),document.createTextNode(status==='succeeded'?'실제 취득·ISP 계산 완료 · 저장된 결과 탐색':status==='running'?'계산 진행 중 · 완료된 결과만 표시':status==='queued'?'실행 대기 중':`실행 상태: ${status}`));
      if(job.error)$('error').textContent=asText(job.error);
      summary=job.result?.summary||job.summary||null;
      if(summary&&Array.isArray(summary.cases))render();else $('result').hidden=true;
      if(['running','queued','cancelling','cancel_requested'].includes(status))timer=setTimeout(load,5000);
    } catch(error) {$('error').textContent=`결과를 읽지 못했습니다. ${error.message}`;$('status').textContent='조회 실패 · 저장 계산을 완료로 표시하지 않습니다.';}
    finally {$('refresh').disabled=false;}
  }
  ['field','chart','light','profile','stage'].forEach(id=>$(id).addEventListener('change',drawCase));$('sampling').addEventListener('change',toggleSampling);$('refresh').addEventListener('click',load);window.addEventListener('pagehide',()=>clearTimeout(timer));load();
})();

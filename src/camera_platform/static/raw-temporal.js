/* Explicit user-started research replay. No implicit submission or source mutation. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let token = '', sources = [], active = null, timer = null, generation = 0;
  const numeric = id => Number($(id).value);
  function fail(error) { $('error').textContent = error.message || String(error); }
  async function api(path, payload) {
    const response = await fetch('/api/v1/' + path, payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type':'application/json','X-Platform-Token':token},
      body: JSON.stringify(payload)
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || body.detail || `HTTP ${response.status}`);
    return body;
  }
  function option(value, label) { const o = document.createElement('option'); o.value=value; o.textContent=label; return o; }
  function ready() { $('submit').disabled = !$('acknowledge').checked || $('source').value === ''; }
  function sourceInfo() {
    const source = sources[Number($('source').value)];
    $('source-info').textContent = $('source').value === '' ? '' :
      `${source.raw_shape.join(' × ')} · ${source.readout_id} · 원본 SHA ${source.raw_sha256.slice(0,16)}…`;
    ready();
  }
  async function lists() {
    const rows = await api('raw-temporal/replays');
    $('saved').replaceChildren(option('', '선택하세요'));
    for (const row of rows) $('saved').append(option(row.id, `${row.name} · ${row.status}`));
  }
  function showFrame() {
    if (!active?.result?.summary) return;
    const s=active.result.summary, i=Number($('frame').value), f=s.frames[i];
    $('frame-label').textContent=`${i+1} / ${s.frames.length}`;
    $('original-image').src=f.previews.raw_frames;
    $('processed-image').src=f.previews.processed_code_dn;
    $('mask-image').src=f.previews.blend_mask;
    const gain=$('display-gain').value;
    $('original-image').style.filter=`brightness(${gain})`;
    $('processed-image').style.filter=`brightness(${gain})`;
    $('frame-info').textContent=`평균 적용 ${(100*f.blend_fraction).toFixed(2)}% · 최대 추가 정보 나이 ${(f.maximum_added_sample_age_ns/1e6).toFixed(3)} ms · 변경 MAE ${f.mean_absolute_change_dn.toFixed(4)} DN · 초기화: ${f.reset_reason} · 실제 행 전달 ${f.first_row_transfer_ns}…${f.last_row_transfer_ns} ns`;
  }
  function render(job) {
    active=job; const s=job.result.summary;
    $('empty').hidden=true; $('result').hidden=false; $('result-title').textContent=job.name;
    $('provenance').textContent=`${s.acquisition.source_id} · ${s.acquisition.readout_id}\n원본 RAW SHA-256: ${s.acquisition.raw_sha256}`;
    $('frame').max=String(s.frames.length-1); $('frame').value='0';
    $('frames-table').replaceChildren();
    for (const f of s.frames) {
      const tr=document.createElement('tr');
      for (const value of [f.frame_index+1,(f.applied.exposure_ns/1e6).toFixed(3),f.applied.gain,
        `${(100*f.blend_fraction).toFixed(1)}%`,`${(f.maximum_added_sample_age_ns/1e6).toFixed(3)} ms`,
        f.mean_absolute_change_dn.toFixed(4),f.reset_reason]) {
        const td=document.createElement('td');td.textContent=String(value);tr.append(td);
      }
      $('frames-table').append(tr);
    }
    $('artifacts').replaceChildren();
    for (const a of job.artifacts.filter(a=>a.name.endsWith('.npy'))) {
      const link=document.createElement('a');link.href=a.url;link.download=a.name;link.textContent=a.name;
      $('artifacts').append(link);
    }
    $('export').href=`/api/v1/raw-temporal/replays/${encodeURIComponent(job.id)}/export`;
    showFrame();
  }
  async function watch(id, ticket) {
    if (ticket !== generation) return;
    try {
      const job=await api(`raw-temporal/replays/${encodeURIComponent(id)}`);
      if (ticket !== generation) return;
      $('status').textContent=`선택 기록: ${job.status}`;
      if (job.status === 'succeeded') {render(job);await lists();$('saved').value=id;}
      else if (['failed','cancelled','interrupted'].includes(job.status)) throw new Error(job.error || job.status);
      else { $('status').textContent += ' · 이 개발 작업실의 Worker가 처리합니다.';timer=setTimeout(()=>watch(id,ticket),1500); }
    } catch(error) {fail(error);}
  }
  $('replay-form').addEventListener('submit',async event=>{
    event.preventDefault();$('error').textContent='';$('submit').disabled=true;
    try {
      const source=sources[Number($('source').value)];
      const cadence=numeric('cadence')*1e6;
      if (!Number.isSafeInteger(cadence) || cadence<=0) throw new Error('간격은 정확한 정수 ns가 되어야 합니다.');
      const queued=await api('raw-temporal/replays', {schema:'camera_raw_temporal_replay_v1',
        name:$('name').value,source_id:source.source_id,readout_id:source.readout_id,cadence_ns:cadence,
        config:{absolute_threshold_dn:numeric('absolute'),relative_threshold:numeric('relative'),
          noise_sigma_dn:numeric('noise'),noise_multiplier:numeric('multiplier'),maximum_pair_fraction:numeric('pair')},
        acknowledge_period_two_risk:$('acknowledge').checked});
      clearTimeout(timer);await watch(queued.id,++generation);
    }catch(error){fail(error);}finally{ready();}
  });
  $('source').addEventListener('change',sourceInfo);$('acknowledge').addEventListener('change',ready);
  $('frame').addEventListener('input',showFrame);$('display-gain').addEventListener('change',showFrame);
  $('refresh').addEventListener('click',()=>lists().catch(fail));
  $('saved').addEventListener('change',()=>{clearTimeout(timer);const id=$('saved').value;const g=++generation;if(id)watch(id,g);});
  window.addEventListener('pagehide',()=>{clearTimeout(timer);generation++;});
  (async()=>{
    token=(await api('session')).token;sources=await api('raw-temporal/sources');
    $('source').replaceChildren(option('',sources.length?'원본을 선택하세요':'완료된 취득 기록이 없습니다'));
    sources.forEach((s,i)=>$('source').append(option(String(i),`${s.name} · ${s.raw_shape[0]}프레임`)));
    await lists();
    const id=new URLSearchParams(location.search).get('replay');if(id)await watch(id,++generation);
  })().catch(fail);
})();

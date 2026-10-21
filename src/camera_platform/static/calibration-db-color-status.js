'use strict';
(function(global) {
  const ROOT = '/api/v1/db-design';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  function sameSource(a,b) {
    return !!(a?.run_id && a?.candidate_id && b?.run_id && b?.candidate_id && a.run_id === b.run_id && a.candidate_id === b.candidate_id && (a.condition_id || '') === (b.condition_id || ''));
  }
  function workbenchURL(model, source) {
    const q = new URLSearchParams();
    if (model?.id) q.set('model', model.id);
    if (model?.color_calibration_id) q.set('calibration', model.color_calibration_id);
    if (source?.run_id) q.set('source_run', source.run_id);
    if (source?.candidate_id) q.set('candidate', source.candidate_id);
    if (source?.condition_id) q.set('condition', source.condition_id);
    return '/calibration-db-color.html'+(q.size ? '?'+q : '');
  }
  function view(models, source) {
    const selectedSource = source;
    const selected = !!(source?.run_id && source?.candidate_id);
    const matching = models.filter(model => sameSource(model.source_context, source));
    let body = '<p class="muted">이 카드는 저장된 활성 모델 상태를 조회합니다. 활성화와 새 Macbeth 계산은 별도 작업실에서 실행합니다. 현재 표시 중인 보존 DB 실행은 변경되지 않습니다.</p>';
    if (selected && !matching.length) body += '<p>현재 선택한 원본·후보·조건에 연결된 활성 색 응답 모델이 없습니다.</p>';
    if (!models.length) return body+'<p>저장된 활성 색 응답 모델이 없습니다.</p>';
    body += '<div class="table-scroll"><table><thead><tr><th>활성 파생 모델</th><th>원본 DB 조건</th><th>QE / CCM SHA256</th><th>새 계산</th></tr></thead><tbody>';
    for (const model of models) {
      const source = model.source_context || {}, match = sameSource(source, selectedSource);
      body += '<tr><td>'+esc(model.name || model.id)+'<br><span class="mono">'+esc(model.id)+'</span><br>'+esc(selected ? match ? '현재 선택 원본에 연결' : '다른 원본·후보·조건' : '활성 파생 모델')+'</td><td class="mono">'+esc(source.run_id)+'<br>'+esc(source.candidate_id)+' / '+esc(source.condition_id || '조건 없음')+'</td><td class="mono">QE '+esc(model.resolved?.qe_profile?.profile_sha256 || '미기록')+'<br>CCM '+esc(model.resolved?.ccm_profile?.profile_sha256 || '미기록')+'</td><td><a href="'+esc(workbenchURL(model, source))+'">모델·Macbeth 재실행 ↗</a></td></tr>';
    }
    body += '</tbody></table></div>';
    for(const model of models){
      if(selected&&!sameSource(model.source_context,selectedSource))continue;
      const run=model.latest_replay, v=run?.validation;
      const expected=run?ROOT+'/color-replays/'+encodeURIComponent(run.id)+'/files/holdout/report.html':'';
      if(v?.status==='synthetic_holdout_completed'&&v.report_url===expected){
        body += '<p><b>가상 실측 자동 비교 완료</b> · RAW RMSE '+esc(v.aggregate?.raw?.after?.rmse)+' DN · ISP RMSE '+esc(v.aggregate?.isp?.after?.rmse)+' · <a href="'+esc(expected)+'">가상 실측·보정 전후 오차 보고서 ↗</a></p>';
      }else if(run)body += '<p>자동 비교 실행 '+esc(run.id)+' · '+esc(run.status)+'</p>';
    }
    return body;
  }
  global.CameraDBColorStatus = {sameSource,workbenchURL,view};
  const root = global.document?.getElementById('db-color-model-status');
  if (!root) return;
  const entry = global.document.getElementById('db-color-model-link');
  let models = [], epoch = 0, alive = true;
  function source() {
    const query = new URL(global.location.href).searchParams;
    return {run_id:query.get('run'),candidate_id:global.document.getElementById('candidate-b')?.value || query.get('candidate-b'),condition_id:query.get('condition')};
  }
  function render() { if (alive) { root.innerHTML = view(models,source()); if(entry) entry.href = workbenchURL(null,source()); } }
  async function refresh() {
    const token = ++epoch;
    try {
      const response = await fetch(ROOT+'/active-color-models');
      if (!response.ok) throw Error('활성 모델 목록을 읽지 못했습니다.');
      const active = await response.json();
      const ids = [...new Set((Array.isArray(active.items) ? active.items : []).map(item => item.model_id))];
      const values = await Promise.all(ids.map(async id => {
        const response = await fetch(ROOT+'/color-models/'+encodeURIComponent(id));
        if (!response.ok) throw Error('활성 모델 '+id+'의 보존 정보를 읽지 못했습니다.');
        const model = await response.json();
        if(model.id !== id) throw Error('활성 모델 식별자가 일치하지 않습니다.');
        return model;
      }));
      if(token !== epoch || !alive) return;
      const replayResponse = await fetch(ROOT+'/color-replays');
      if (!replayResponse.ok) throw Error('자동 비교 실행 목록을 읽지 못했습니다.');
      const replayBody = await replayResponse.json();
      if(token !== epoch || !alive) return;
      const replays = Array.isArray(replayBody.items) ? replayBody.items : [];
      models = values.map(model=>({...model,latest_replay:replays.find(row=>row.model_id===model.id)})); render();
    } catch(error) { if(token === epoch && alive) root.innerHTML = '<p role="alert">'+esc(error.message)+'</p>'; }
  }
  global.document.getElementById('db-color-status-refresh')?.addEventListener('click',refresh);
  global.document.addEventListener('change',event=>{if(['saved-runs','candidate-b','condition'].includes(event.target.id))setTimeout(render,0);});
  entry?.addEventListener('click',()=>{entry.href=workbenchURL(null,source());});
  const identity = global.document.getElementById('source-run-b');
  const observer = identity && global.MutationObserver ? new global.MutationObserver(render) : null;
  if(observer) observer.observe(identity,{childList:true,subtree:true,characterData:true});
  global.addEventListener('beforeunload',()=>{alive=false;epoch++;observer?.disconnect();});
  refresh();
})(typeof window === 'undefined' ? globalThis : window);

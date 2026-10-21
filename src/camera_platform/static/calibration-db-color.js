'use strict';
(function (global) {
  const ROOT = '/api/v1/db-design';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const list = value => Array.isArray(value) ? value : [];
  const json = value => JSON.stringify(value, null, 2);
  const fmt = value => value === undefined || value === null ? '—' : typeof value === 'number' ? Number.isFinite(value) ? new Intl.NumberFormat('ko-KR', {maximumSignificantDigits:5}).format(value) : '유효하지 않은 값' : typeof value === 'object' ? esc(json(value)) : esc(value);
  const labels = {queued:'대기',running:'계산 중',succeeded:'계산 완료',failed:'계산 실패',cancelled:'취소',not_evaluated:'미평가',not_established:'물리 검증 미확립',pass:'수치 기준 충족',fail:'수치 기준 미충족',synthetic_model_transfer:'합성 모델 이전',synthetic:'합성 데이터'};
  const title = value => labels[value] || value || '미기록';
  const badge = value => `<span class="badge ${value === 'failed' || value === 'fail' ? 'fail' : ''}">${esc(title(value))}</span>`;
  const detail = (name, value) => `<details><summary>${esc(name)}</summary><pre>${esc(json(value) || '미기록')}</pre></details>`;
  const empty = value => `<p class="empty">${esc(value)}</p>`;
  function table(headers, rows) {
    return rows.length ? `<div class="table-scroll"><table><thead><tr>${headers.map(x => '<th>'+esc(x)+'</th>').join('')}</tr></thead><tbody>${rows.map(row => '<tr>'+row.map(x => '<td>'+fmt(x)+'</td>').join('')+'</tr>').join('')}</tbody></table></div>` : empty('제공된 수치가 없습니다.');
  }
  function fileURL(summary, relative) {
    if (!summary || typeof summary.run_id !== 'string' || typeof relative !== 'string') return null;
    if (!relative.split('/').every(part => /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(part) && part !== '.' && part !== '..')) return null;
    const hash = summary.files?.[relative];
    if (!/^[a-f0-9]{64}$/.test(typeof hash === 'string' ? hash : hash?.sha256 || '')) return null;
    const base = ROOT+'/color-replays/'+encodeURIComponent(summary.run_id)+'/files/';
    if (summary.files_base !== base) return null;
    return base+relative.split('/').map(encodeURIComponent).join('/');
  }
  function sourceURL(source) {
    const q = new URLSearchParams();
    if (source?.run_id) q.set('run', source.run_id);
    if (source?.candidate_id) q.set('candidate-b', source.candidate_id);
    if (source?.condition_id) q.set('condition', source.condition_id);
    q.set('chart', 'color');
    return '/db-design.html?'+q;
  }
  function colorURL(source,calibration) {
    const q=new URLSearchParams();
    if(calibration)q.set('calibration',calibration);
    if(source?.run_id)q.set('source_run',source.run_id);
    if(source?.candidate_id)q.set('candidate',source.candidate_id);
    if(source?.condition_id)q.set('condition',source.condition_id);
    return '/calibration-color.html'+(q.size?'?'+q:'');
  }
  function queryContext(query) {
    return {
      calibration:query.get('calibration') || '',
      model:query.get('model') || '',
      replay:query.get('replay') || query.get('run') || '',
      source:{run_id:query.get('source_run') || '',candidate_id:query.get('candidate') || query.get('candidate-b') || '',condition_id:query.get('condition') || ''}
    };
  }
  function modelView(s) {
    const model = s.model;
    if (!model) return empty('저장된 모델을 선택하거나 새 모델을 생성하세요.');
    const active = s.active.find(item => item.target_key === model.target_key);
    const applied = active?.model_id === model.id;
    return `<section class="card stage"><p class="eyebrow">02 / ACTIVE MODEL</p><h2>${esc(model.name || '저장된 DB 색 응답 모델')}</h2><div class="scope-status">${badge(model.binding?.mode)}${badge('not_established')}<span class="badge">${applied ? '이 버전 활성' : '저장된 버전'}</span></div><p class="identity">${esc(model.id)}<br>프로파일 SHA256 ${esc(model.profile_sha256 || '미기록')}<br>현재 활성 모델 ${esc(active?.model_id || '없음')}</p><p class="scope">활성화는 다음 재실행에 사용할 버전을 선택합니다. 기존 DB 실행이나 기존 RAW는 변경하지 않습니다.</p><div class="model-actions"><button data-action="activate" ${s.busy || applied ? 'disabled' : ''}>이 모델 활성화</button><button class="primary" data-action="replay" ${s.busy || !applied ? 'disabled' : ''}>활성 모델 Macbeth RAW · ISP 재실행</button></div><p class="help">중심 · 주변 우상, native 256 × 256, 8프레임. QE만 적용한 결과와 같은 RAW에 CCM을 적용한 결과를 함께 보존합니다.</p><div class="actions"><a class="button" href="${esc(sourceURL(model.source_context))}">원본 DB 실행 ↗</a><a class="button" href="${esc(colorURL(model.source_context,model.color_calibration_id))}">원본 QE · RAW · CCM 보정 ↗</a></div>${detail('고정된 모델·이전 가정', model)}</section>`;
  }
  const variants = {db_baseline:'원본 DB 모델 · 기존 ISP',qe_only_db_isp:'새 QE · 기존 DB ISP',qe_ccm_baseline:'새 QE · 보정 전 CCM',qe_ccm_fitted:'새 QE · 추정한 CCM'};
  function evidenceView(summary, s) {
    const fields = list(summary.fields), field = fields.find(item => item.field_id === s.field) || fields[0];
    if (!field) return empty('시야별 결과가 제공되지 않았습니다.');
    const items = list(field.variants), rows = items.map(item => [variants[item.variant] || item.variant, item.output_space, list(item.raw?.frames).length, item.raw?.temporal_noise_dn, item.raw?.clipped_fraction]);
    function pair(keys, heading, note) {
      return `<h3>${esc(heading)}</h3><p class="help">${esc(note)}</p><div class="comparison-grid">${keys.map(key => {
        const item = items.find(value => value.variant === key);
        if (!item) return empty('결과 없음: '+key);
        const path = item.images?.[s.stage === 'raw' ? 'raw_preview' : 'isp'], url = fileURL(summary, path);
        return `<figure>${url ? `<img src="${url}" alt="${esc(field.field_id+' · '+variants[key]+' · '+s.stage)}" loading="lazy">` : empty('검증된 이미지 파일이 없습니다.')}<figcaption><b>${esc(variants[key])}</b><br>${esc(item.output_space || '출력 공간 미기록')}</figcaption></figure>`;
      }).join('')}</div>`;
    }
    const comparisons = list(summary.comparisons).filter(item => item.field_id === field.field_id);
    const names = {qe_raw_change:'QE 적용에 따른 RAW 변화',db_isp_change:'동일 DB ISP에서 QE 변경 영향',ccm_output_change:'같은 RAW · 같은 출력 공간에서 CCM 변경 영향'};
    return `<div class="stage-controls"><label>시야<select data-field="field">${fields.map(item => `<option value="${esc(item.field_id)}" ${item.field_id === field.field_id ? 'selected' : ''}>${esc(item.field_id === 'center' ? '중심' : item.field_id === 'outer-ne' ? '주변 우상' : item.field_id)}</option>`).join('')}</select></label><label>표시 단계<select data-field="stage"><option value="isp" ${s.stage === 'isp' ? 'selected' : ''}>ISP 출력</option><option value="raw" ${s.stage === 'raw' ? 'selected' : ''}>RAW 미리보기</option></select></label></div>${pair(['db_baseline','qe_only_db_isp'],'QE 적용 전·후 · 같은 DB ISP','원본 DB와 새 QE의 RAW·출력 변화를 봅니다. 아래 CCM 비교와 목표 색 공간이 다를 수 있습니다.')}${pair(['qe_ccm_baseline','qe_ccm_fitted'],'CCM 적용 전·후 · 같은 RAW와 출력 공간','QE와 RAW를 고정하고 보정 전 행렬과 추정 행렬을 비교합니다. camera_linear_rgb 미리보기는 색도 기준 sRGB 표현이 아닙니다.')}<h3>보존 RAW와 출력 공간</h3>${table(['변형','출력 공간','프레임 수','시간 잡음 (DN)','잘림 비율'],rows)}<p class="scope">새 QE 세 변형의 RAW 동일성: ${field.raw_identity?.qe_variants_identical === true ? '저장 원본 동일 확인' : '확인 근거 없음'}. CCM은 RAW를 바꾸지 않습니다.</p><h3>24패치의 변화량 · 정답 대비 오차 아님</h3>${table(['비교','단위','변화량 RMSE','최대 절대 변화'],comparisons.map(item => [names[item.kind] || item.kind,item.unit,item.rmse_change,item.max_abs_change]))}<p class="help">DB 조건의 독립 실측 정답이 없으므로 개선율·정확도·ΔE·검증 통과로 해석하지 않습니다.</p>${comparisons.map(item => detail(names[item.kind] || item.kind,{unit:item.unit,comparison_type:item.comparison_type,baseline:item.baseline,candidate:item.candidate,difference:item.difference})).join('')}`;
  }
  function holdoutView(summary, s) {
    const h = summary?.holdout;
    if (!h) return empty('이전 실행에는 자동 가상 실측 비교가 없습니다. 새로 실행하면 보고서를 생성합니다.');
    if (h.status !== 'synthetic_holdout_completed') return `<section class="holdout"><h3>가상 실측 자동 비교 · 미평가</h3><p>${esc(h.reason || '별도 관측값이나 알려진 합성 기준 생성기가 필요합니다.')}</p></section>`;
    if (h.source?.model_id !== summary.model_id || h.source?.replay_id !== summary.run_id) return '<p class="notice error">자동 비교 결과의 모델·실행 연결이 다릅니다.</p>';
    const fields = list(h.fields), field = fields.find(f=>f.field_id===s.field) || fields[0];
    if (!field) return empty('자동 비교의 시야별 결과가 없습니다.');
    const src = path=>fileURL(summary,(h.path_prefix || '')+path);
    const report = fileURL(summary,h.report_path), bundle = fileURL(summary,h.bundle_path);
    const pct = (a,b)=>Number.isFinite(a)&&a>0&&Number.isFinite(b)?100*(1-b/a):null;
    const aggregate = ['raw','isp'].map(k=>[k==='raw'?'RAW · DN':'ISP · 선형 RGB',h.aggregate?.[k]?.before?.rmse,h.aggregate?.[k]?.after?.rmse,h.aggregate?.[k]?.reduction_percent,h.aggregate?.[k]?.replicate?.rmse]);
    const stage = s.stage === 'raw' ? 'raw' : 'isp';
    const panels = ['before','observation','after'].map(role=>{
      const names = {before:'보정 전',observation:'가상 실측 · 독립 취득 A',after:'보정 후'};
      const url = src(field.series?.[role]?.images?.[stage]);
      return `<figure><figcaption><b>${names[role]}</b></figcaption>${url?`<img src="${url}" alt="${esc(field.field_id+' · '+names[role]+' · '+stage)}">`:empty('검증된 이미지가 없습니다.')}</figure>`;
    }).join('');
    const rows = ['raw','isp'].map(k=>{const m=field.metrics?.[k];return [k==='raw'?'RAW · DN':'ISP · 선형 RGB',m?.before?.rmse,m?.qe_only?.rmse,m?.after?.rmse,pct(m?.before?.rmse,m?.after?.rmse),m?.repeat?.rmse];});
    return `<section class="holdout"><p class="eyebrow">AUTOMATIC SYNTHETIC HOLDOUT</p><h2>가상 실측 · 보정 전후 자동 비교</h2><p class="scope">기존 보정 프로파일을 고정하고 별도 seed의 가상 실측을 생성했습니다. 새 관측으로 재피팅하지 않습니다. 공통 광학·ISP 모델을 사용하는 합성 검증이며 실물 카메라 검증은 미확립입니다.</p><div class="actions">${report?`<a class="button primary" href="${report}" target="_blank" rel="noopener">자동 오차 보고서 ↗</a>`:''}${bundle?`<a class="button" href="${bundle}" download>가상 실측·RAW·ISP 근거 ZIP ↓</a>`:''}</div><h3>중심·주변부 합산 오차</h3>${table(['지표','보정 전 RMSE','보정 후 RMSE','감소 (%)','독립 반복 A/B 차이'],aggregate)}<div class="stage-controls"><label>비교 시야<select data-holdout="field">${fields.map(f=>`<option value="${esc(f.field_id)}" ${f.field_id===field.field_id?'selected':''}>${f.field_id==='center'?'중심':'주변 우상'}</option>`).join('')}</select></label><label>비교 표시<select data-holdout="stage"><option value="isp" ${stage==='isp'?'selected':''}>ISP 출력</option><option value="raw" ${stage==='raw'?'selected':''}>RAW 미리보기</option></select></label></div><div class="holdout-grid">${panels}</div>${table(['시야별 지표','보정 전','QE만 적용','보정 후','감소 (%)','독립 반복 차이'],rows)}<p class="help">RAW: DN. ISP: 동일한 선형 camera RGB 공간, clipping/gamma 이전. A/B 반복 차이는 잡음 수준 비교이며 합격 임계값은 아닙니다. RAW 미리보기의 표시 구간은 모든 결과에 동일합니다.</p>${detail('검증 seed·취득·파라미터 고정 근거',h.protocol)}</section>`;
  }

  function resultView(s) {
    const job = s.replay;
    if (!job) return empty('저장된 재실행을 선택하면 RAW · ISP 결과와 보고서를 확인할 수 있습니다.');
    const summary = job.result?.summary;
    let content = `<section class="card stage"><p class="eyebrow">03 / PRESERVED REPLAY</p><h2>Macbeth RAW · ISP 재실행</h2>${badge(job.status)}<p class="identity">실행 ${esc(job.id)}<br>실행 모델 ${esc(job.specification?.model_id || summary?.model_id || '미기록')}</p>`;
    if (['queued','running'].includes(job.status)) return content+`<p class="progress" role="status">${esc(job.progress?.message || job.message || '새 모델의 결과를 계산하고 있습니다.')}</p></section>`;
    if (job.status !== 'succeeded' || !summary) return content+`<p class="notice error">${esc(typeof job.error === 'object' ? json(job.error) : job.error || '완료된 결과가 없습니다.')}</p></section>`;
    if (summary.model_id !== s.model?.id) content += '<p class="notice warning">이 결과는 현재 선택 모델과 다른 버전으로 계산되었습니다. 아래에 표시된 실행 모델의 보존 결과입니다.</p>';
    const report = fileURL(summary, 'report.html'), data = fileURL(summary, 'report.json');
    content += `<p class="scope">기존 보정 판정은 해당 취득 조건의 근거입니다. 자동 비교는 별도 합성 관측을 사용하며 DB 조건의 실물 카메라 정합은 미확립입니다.</p><div class="actions">${report ? `<a class="button" href="${report}" target="_blank" rel="noopener">새 실행 표준 보고서 ↗</a>` : ''}${data ? `<a class="button" href="${data}" download>결과 JSON ↓</a>` : ''}<a class="button" href="${ROOT}/color-replays/${encodeURIComponent(job.id)}/bundle" download>RAW · ISP · 근거 ZIP ↓</a></div>`;
    content += `<div class="scope-status">${badge(summary.physical_validation || 'not_established')}</div>`;
    content += holdoutView(summary, s);
    content += detail("기존 DB 파라미터 전후 변화량", summary.comparisons);
    content += detail('새 실행의 수치·파일·출처', summary);
    return content+'</section>';
  }
  function view(s) {
    const form = s.form;
    return `${s.error ? '<p class="notice error" role="alert">'+esc(s.error)+'</p>' : ''}${s.notice ? '<p class="notice" role="status">'+esc(s.notice)+'</p>' : ''}<section class="card"><div class="saved-results"><label>저장된 DB 색 응답 모델<select data-field="model"><option value="">모델 선택</option>${s.models.map(item => `<option value="${esc(item.id)}" ${item.id === s.selectedModel ? 'selected' : ''}>${esc(item.name || item.id)}${item.active ? ' · 활성' : ''}</option>`).join('')}</select></label><label>저장된 Macbeth 재실행<select data-field="replay"><option value="">실행 선택</option>${s.replays.map(item => `<option value="${esc(item.id)}" ${item.id === s.selectedReplay ? 'selected' : ''}>${esc(item.name || item.id)} · ${esc(title(item.status))}</option>`).join('')}</select></label></div><button data-action="refresh" ${s.busy ? 'disabled' : ''}>목록 새로고침</button></section><div class="layout"><aside><section class="card"><p class="eyebrow">01 / EXPLICIT MODEL TRANSFER</p><h2>적용할 원본과 프로파일</h2><p class="help">색 응답 보정의 QE와 CCM을 DB 카메라의 새 합성 모델 버전으로 연결합니다. 실측 카메라와 DB 카메라가 같다는 의미가 아닙니다.</p><form data-form="create"><fieldset ${s.busy ? 'disabled' : ''}><label>모델 이름<input data-input="name" value="${esc(form.name)}" maxlength="160" required></label><label>저장된 QE · CCM 보정<select data-input="color_calibration_id" required><option value="">보정 선택</option>${s.calibrations.map(item => `<option value="${esc(item.id)}" ${item.id === form.color_calibration_id ? 'selected' : ''}>${esc(item.name || item.id)}</option>`).join('')}</select></label><div class="source-grid"><label class="wide">원본 DB 실행 ID<input data-input="run_id" value="${esc(form.run_id)}" required placeholder="run-…"></label><label>후보 ID<input data-input="candidate_id" value="${esc(form.candidate_id)}" required placeholder="candidate-03"></label><label>조건 ID · 선택<input data-input="condition_id" value="${esc(form.condition_id)}" placeholder="condition-006"></label></div><label>새 합성 카메라 ID<input data-input="target_camera_id" value="${esc(form.target_camera_id)}" required pattern="synthetic-db-.+" placeholder="synthetic-db-my-camera"></label><p class="scope">선언하는 가정: 센서면의 절대 QE, 입사각에 따른 응답 변화 없음, 두 Green 사이트에 같은 응답. 원본 DB 센서의 각도 응답을 실측으로 식별한 결과가 아닙니다.</p><button type="button" data-action="workflow" class="primary full">모델 적용 → 자동 비교·보고서 생성</button><button type="submit" class="full">모델 버전만 생성</button></fieldset></form></section></aside><div>${modelView(s)}${resultView(s)}</div></div>`;
  }
  function createAPI(request) {
    return async (path, body) => {
      const headers = {};
      if (body !== undefined) {
        const response = await request('/api/v1/session');
        if (!response.ok) throw Error('로컬 세션을 확인하지 못했습니다.');
        const session = await response.json();
        if (!session.token) throw Error('로컬 세션 토큰이 없습니다.');
        headers['X-Platform-Token'] = session.token;
        headers['Content-Type'] = 'application/json';
      }
      const url = path === '/calibrations' ? '/api/v1/calibration-lab/color-calibrations' : ROOT+path;
      const response = await request(url, {method:body === undefined ? 'GET' : 'POST',headers,body:body === undefined ? undefined : json(body)});
      let value;
      try { value = await response.json(); } catch { throw Error('서버 응답을 읽지 못했습니다 ('+response.status+').'); }
      if (!response.ok) { const error = value.error || value.detail || value.message || value; throw Error(typeof error === 'string' ? error : json(error)); }
      return value;
    };
  }
  function createController(deps) {
    const api = deps.api, s = {models:[],active:[],replays:[],calibrations:[],model:null,replay:null,selectedModel:'',selectedReplay:'',field:'center',stage:'isp',busy:false,error:'',notice:'',form:{name:'QE · CCM 적용 DB 모델',color_calibration_id:deps.calibration || '',run_id:deps.source?.run_id || '',candidate_id:deps.source?.candidate_id || '',condition_id:deps.source?.condition_id || '',target_camera_id:''}};
    let modelEpoch = 0, replayEpoch = 0, listEpoch = 0, timer = null;
    const render = () => deps.render?.(s);
    function cancelTimer() { if (timer !== null) deps.clearTimer?.(timer); timer = null; }
    async function refresh() {
      const token = ++listEpoch;
      const [models,active,replays,calibrations] = await Promise.all([api('/color-models'),api('/active-color-models'),api('/color-replays'),api('/calibrations')]);
      if (token !== listEpoch) return;
      s.models = list(models.items); s.active = list(active.items); s.replays = list(replays.items); s.calibrations = list(calibrations.items); render();
    }
    async function selectModel(id) {
      const token = ++modelEpoch;
      s.selectedModel = id; s.model = null; s.error = ''; render();
      if (!id) { deps.onModel?.(''); deps.onSource?.(deps.source || {},deps.calibration || ''); return; }
      try {
        const value = await api('/color-models/'+encodeURIComponent(id));
        if (token !== modelEpoch || s.selectedModel !== id) return;
        if (value.id !== id) throw Error('요청한 모델과 반환된 모델 ID가 다릅니다.');
        s.model = value; deps.onModel?.(id); deps.onSource?.(value.source_context,value.color_calibration_id);
      } catch (error) { if (token === modelEpoch) s.error = error.message; }
      render();
    }
    async function selectReplay(id) {
      cancelTimer();
      const token = ++replayEpoch;
      s.selectedReplay = id; s.replay = null; s.error = ''; render();
      if (!id) { deps.onReplay?.(''); return; }
      async function load() {
        try {
          const value = await api('/color-replays/'+encodeURIComponent(id));
          if (token !== replayEpoch || s.selectedReplay !== id) return;
          if (value.id !== id) throw Error('요청한 실행과 반환된 실행 ID가 다릅니다.');
          const summary = value.result?.summary;
          if (summary && (summary.run_id !== id || summary.model_id !== value.specification?.model_id)) throw Error('실행 결과의 모델·실행 식별자가 일치하지 않습니다.');
          s.replay = value; s.replays = s.replays.map(row=>row.id===id?{...row,status:value.status}:row); deps.onReplay?.(id); render();
          if (['queued','running'].includes(value.status) && deps.setTimer) timer = deps.setTimer(load, 1800);
        } catch (error) { if (token === replayEpoch) { s.error = error.message; render(); } }
      }
      await load();
    }
    async function init() {
      try {
        await refresh();
        if (deps.model) await selectModel(deps.model);
        if (deps.replay) {
          await selectReplay(deps.replay);
          // A saved replay URL owns its model even when no model query was supplied.
          const modelId = s.replay?.specification?.model_id;
          if (!s.selectedModel && modelId) await selectModel(modelId);
        }
      }
      catch (error) { s.error = error.message; render(); }
    }
    function payload() {
      const f = s.form;
      for (const key of ['name','color_calibration_id','run_id','candidate_id','target_camera_id']) if (!f[key]?.trim()) throw Error('모델 이름, 보정, 원본 실행·후보, 새 합성 카메라 ID를 입력하세요.');
      if (!/^synthetic-db-[A-Za-z0-9_.-]+$/.test(f.target_camera_id.trim())) throw Error('새 카메라 ID는 synthetic-db-로 시작하고 영문·숫자·-·_·.을 사용하세요.');
      const source = {run_id:f.run_id.trim(),candidate_id:f.candidate_id.trim()};
      if (f.condition_id.trim()) source.condition_id = f.condition_id.trim();
      return {name:f.name.trim(),source,color_calibration_id:f.color_calibration_id,binding:{mode:'synthetic_model_transfer',angular_assumption:'angle_independent',green_sites:'shared_green',target_camera_id:f.target_camera_id.trim()}};
    }
    async function perform(action) {
      if (s.busy) return;
      s.busy = true; s.error = ''; s.notice = ''; render();
      const token = modelEpoch, model = s.model;
      try {
        if (action === 'refresh') { await refresh(); return; }
        if (action === 'workflow') {
          const input = payload(), config = {name:input.name,source:input.source,binding:input.binding};
          const flow = await api('/color-workflows',{model:config,color_calibration_id:input.color_calibration_id});
          await refresh();
          if(token !== modelEpoch) return;
          if(flow.model) await selectModel(flow.model.id);
          if(flow.run) await selectReplay(flow.run.id);
          if(flow.status!=='submitted') throw Error(flow.error || '보정 기준을 충족하지 않아 DB 적용을 진행하지 않았습니다.');
          s.notice = '모델 적용과 새 RAW·ISP 계산을 제출했습니다. 완료 후 독립 가상 실측 비교 보고서가 자동 생성됩니다.';
          return;
        }
        if (action === 'create') {
          const value = await api('/color-models', payload());
          if (!value.id) throw Error('생성된 모델 ID가 없습니다.');
          await refresh();
          if (token === modelEpoch) { await selectModel(value.id); s.notice = '모델 버전을 보존했습니다. 선택한 버전을 활성화한 다음 새 계산을 실행하세요.'; }
          return;
        }
        if (!model || model.id !== s.selectedModel) throw Error('적용할 모델을 먼저 선택하세요.');
        const active = s.active.find(item => item.target_key === model.target_key);
        if (action === 'activate') {
          const value = await api('/color-models/'+encodeURIComponent(model.id)+'/activate',{expected_active_model_id:active?.model_id || null});
          if (value.model_id !== model.id || value.target_key !== model.target_key) throw Error('활성화 응답의 모델 연결이 다릅니다.');
          await refresh(); if (token === modelEpoch) s.notice = '이 모델을 활성화했습니다. Macbeth 재실행 버튼으로 새 RAW · ISP를 계산할 수 있습니다.';
        } else if (action === 'replay') {
          if (active?.model_id !== model.id) throw Error('현재 선택한 모델을 활성화한 다음 실행하세요.');
          const selection = replayEpoch, value = await api('/color-replays',{model_id:model.id});
          if (!value.id || value.specification?.model_id && value.specification.model_id !== model.id) throw Error('새 실행의 모델 연결이 다릅니다.');
          await refresh(); if (selection === replayEpoch) await selectReplay(value.id);
          if (token === modelEpoch) s.notice = '새 Macbeth 계산을 제출했습니다. 완료되면 독립 가상 실측 비교와 오차 보고서가 자동 생성됩니다.';
        }
      } catch (error) { s.error = error.message; }
      finally { s.busy = false; render(); }
    }
    return {state:s,init,refresh,selectModel,selectReplay,perform,payload,input(key,value){if (Object.hasOwn(s.form,key)) s.form[key] = value;},display(key,value){if(key==='stage'&&['raw','isp'].includes(value)||key==='field'){s[key]=value;render();}},destroy(){modelEpoch++;replayEpoch++;listEpoch++;cancelTimer();}};
  }
  global.CameraDBColorCalibration = {esc,fileURL,sourceURL,colorURL,queryContext,view,holdoutView,createAPI,createController};
  const root = global.document?.getElementById('db-color-lab');
  if (!root) return;
  const query = new URL(global.location.href).searchParams;
  function update(key,id) { const url = new URL(global.location.href); if(key==='replay')url.searchParams.delete('run');if(id) url.searchParams.set(key,id); else url.searchParams.delete(key); global.history.replaceState({},'',url.pathname+url.search); }
  const context = queryContext(query);
  function updateSource(source,calibration) {
    global.document.getElementById('color-back').href = colorURL(source,calibration);
    global.document.getElementById('db-back').href = sourceURL(source);
  }
  updateSource(context.source,context.calibration);
  const ui = createController({api:createAPI((...args)=>fetch(...args)),render:s=>{root.innerHTML=view(s);},...context,onSource:updateSource,onModel:id=>update('model',id),onReplay:id=>update('replay',id),setTimer:(fn,ms)=>setTimeout(fn,ms),clearTimer:id=>clearTimeout(id)});
  root.addEventListener('click',event=>{const button=event.target.closest('[data-action]');if(button&&!button.disabled)ui.perform(button.dataset.action);});
  root.addEventListener('submit',event=>{if(event.target.matches('[data-form=create]')){event.preventDefault();ui.perform('create');}});
  root.addEventListener('input',event=>{if(event.target.dataset.input)ui.input(event.target.dataset.input,event.target.value);});
  root.addEventListener('change',event=>{if(event.target.dataset.input)ui.input(event.target.dataset.input,event.target.value);if(event.target.dataset.field==='model')ui.selectModel(event.target.value);if(event.target.dataset.field==='replay')ui.selectReplay(event.target.value);if(['field','stage'].includes(event.target.dataset.field))ui.display(event.target.dataset.field,event.target.value);if(event.target.dataset.holdout)ui.display(event.target.dataset.holdout,event.target.value);});
  global.addEventListener('beforeunload',()=>ui.destroy());
  ui.init();
})(typeof window === 'undefined' ? globalThis : window);

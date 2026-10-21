'use strict';
function CameraDesignExperimentSpecification(template,catalog,input) {
  if(!template || typeof template!=='object')throw new Error('서버의 설계 실험 입력 계약이 없습니다.');
  const {model_ids,scene_id,stops,acknowledge,...common}=input;
  const spec={...template,...common};
  if(template.schema==='camera_design_experiment_v2'){
    const models=Array.isArray(catalog?.models)?catalog.models:[],scenes=Array.isArray(catalog?.scenes)?catalog.scenes:[];
    if(!Array.isArray(model_ids)||model_ids.length<1||model_ids.length>3||new Set(model_ids).size!==model_ids.length||model_ids.some(id=>!models.some(item=>item.id===id)))throw new Error('등록된 모델 1–3개를 중복 없이 선택하세요.');
    if(!scenes.some(item=>item.id===scene_id))throw new Error('등록된 분광 장면을 선택하세요.');
    spec.model_ids=model_ids;spec.scene_id=scene_id;spec.acknowledge_simulation_limits=acknowledge;
  }else if(template.schema==='camera_design_experiment_v1'){
    if(!Array.isArray(stops)||stops.length<1||stops.length>3||new Set(stops).size!==stops.length||stops.some(value=>!['clear','quarter','radial'].includes(value)))throw new Error('구 연구 예제의 투과 조건을 선택하세요.');
    spec.stops=stops;spec.acknowledge_research_only=acknowledge;
  }else throw new Error('현재 화면이 지원하지 않는 설계 실험 계약입니다.');
  return spec;
}
function CameraDesignExperimentVisibleSummary(job) {
  const summary=job?.result?.summary;
  return job?.status==='succeeded'&&summary?.status==='completed'?summary:null;
}
function CameraDesignExperimentCaseAssets(detail,caseId,schema) {
  if(detail?.row?.case_id!==caseId)throw new Error('선택한 RAW 사례와 원본 응답이 일치하지 않습니다.');
  if(!detail.images?.raw||!detail.images?.final_code_rail||!detail.row.raw_artifact?.url||!detail.row.diagnostics_artifact?.url)throw new Error('원본 RAW 또는 진단 산출물이 없습니다.');
  const rgb=Boolean(detail.images.rgb&&detail.row.rgb_artifact?.url);
  if(schema==='camera_design_experiment_v2'&&!rgb)throw new Error('R1 결과의 고정 ISP RGB 원본 또는 미리보기가 없습니다.');
  return {rgb};
}
(() => {
  const $ = id => document.getElementById(id), esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (v,n=3) => Number.isFinite(v) ? v.toFixed(n) : '—';
  const reasons={minimum_signal:'최소 신호',minimum_snr:'최소 SNR',well_saturation:'well 포화',raw_clipping:'RAW 상단',exposure_time:'노출 상한'};
  const label = list => (list||[]).map(x=>reasons[x]||x).join(', ') || '통과';
  let token='', template, catalog, active='', result=null, timer=null, busy=false, epoch=0, readOnly=false;
  let currentJob=null,currentCase=null,replayEpoch=0,replayTimer=null,replayBusy=false;
  async function api(path, body){
    const r=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','x-platform-token':token},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const value=await r.json();if(!r.ok)throw new Error(value.error||value.detail||`HTTP ${r.status}`);return value;
  }
  function fail(error){$('error').hidden=false;$('error').textContent=error.message||String(error);}
  function clearError(){$('error').hidden=true;$('error').textContent='';}
  function values(id){const chunks=$(id).value.split(',').map(x=>x.trim());if(chunks.some(x=>!x))throw new Error('후보는 쉼표로 구분한 숫자로 입력하세요.');const v=chunks.map(Number);if(v.some(x=>!Number.isFinite(x)))throw new Error('유한한 숫자만 입력하세요.');return v;}
  function specification(){return CameraDesignExperimentSpecification(template,catalog,{name:$('name').value,
    model_ids:[...document.querySelectorAll('input[name=model]:checked')].map(x=>x.value),scene_id:$('scene').value,
    stops:[...document.querySelectorAll('input[name=stop]:checked')].map(x=>x.value),
    exposures_ms:values('exposures'),gains:values('gains'),training_scales:values('training'),evaluation_scales:values('evaluation'),
    evaluation_noise_repeats:Number($('repeats').value),seed:Number($('seed').value),acknowledge:$('acknowledge').checked,
    limits:{minimum_green_dn:Number($('signal').value),minimum_green_snr_p10:Number($('snr').value),maximum_well_fraction:Number($('well').value)/100,maximum_white_fraction:Number($('white').value)/100,maximum_exposure_ms:Number($('cap').value)}});}
  function renderCatalog(){
    const registered=template.schema==='camera_design_experiment_v2';
    $('registered-models').hidden=!registered;$('registered-scene').hidden=!registered;$('legacy-stops').hidden=registered;
    $('acknowledge-label').textContent=registered?'R1 엔지니어링 시뮬레이션이며 실물 예측 정확도와 제품 승인은 미확립임을 확인했습니다.':'구 연구 예제의 가정과 평가 범위를 확인했습니다.';
    $('scope-warning').textContent=registered?'R1 설계 실험 범위: 등록된 16×16·500/600 nm 정적 분광 예제, 최대 36개 후보·128회 취득, 프레임 간 상태 없는 독립 노출. 정수 RAW와 고정 ISP RGB를 저장하고 저장된 RAW의 고정 WB·CCM·tone 재처리를 비교할 수 있습니다. 움직임·HDR/LFM 결합과 실물 예측 정확도 검증은 포함되지 않습니다.':'구 합성 처방의 정적·독립 노출 연구입니다. ISP RGB와 실제 센서 정확도는 보증하지 않습니다. 움직임·색 정확도·객체 검출 성능은 선택 목적에 포함되지 않습니다.';
    if(!registered)return;
    if(!Array.isArray(catalog?.models)||!catalog.models.length||!Array.isArray(catalog?.scenes)||!catalog.scenes.length)throw new Error('등록 모델·장면 목록을 확인할 수 없습니다.');
    $('model-options').innerHTML=catalog.models.map(item=>`<label class="inline"><input type="checkbox" name="model" value="${esc(item.id)}" ${template.model_ids.includes(item.id)?'checked':''}> ${esc(item.name||item.id)} · r${esc(item.revision||'—')} <small>${esc(item.id)}</small></label>`).join('');
    $('scene').innerHTML=catalog.scenes.map(item=>`<option value="${esc(item.id)}" ${template.scene_id===item.id?'selected':''}>${esc(item.name||item.id)} · r${esc(item.revision||'—')} · ${esc(item.id)}</option>`).join('');
    $('catalog-source').hidden=false;$('catalog-source').textContent='예제 자산: '+(catalog.bundle_id||'ID 미기록')+' · v'+(catalog.bundle_version||'버전 미기록');
  }
  function budget(){
    if(readOnly||!template){$('submit').disabled=true;$('budget').textContent='새 실험 계약을 사용할 수 없어 저장 기록 조회만 가능합니다.';return;}
    $('submit').disabled=busy||!token||!$('acknowledge').checked;
    try{const s=specification(),models=s.model_ids||s.stops,all=models.length*s.exposures_ms.length*s.gains.length,eligible=models.length*s.exposures_ms.filter(e=>e<=s.limits.maximum_exposure_ms).length*s.gains.length;
      const count=eligible*s.training_scales.length+(eligible?s.evaluation_scales.length*(1+s.evaluation_noise_repeats):0);
      $('budget').textContent=`${all}개 후보 · 노출 상한 이내 ${eligible}개 · 최대 ${count}회 취득. 적합 후보가 없으면 평가를 건너뜁니다.`;
      if(all>36||count>128||!all){$('submit').disabled=true;$('budget').textContent+=' 후보 1–36개, 최대 128회로 줄여 주세요.';}
    }catch(error){$('submit').disabled=true;$('budget').textContent=error.message;}
  }
  async function listing(){const items=await api('design-experiments');$('saved').innerHTML='<option value="">선택하세요</option>'+items.map(r=>`<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.status)}${r.status==='succeeded'&&r.choice?' · '+esc(r.choice):''}</option>`).join('');if(active)$('saved').value=active;}
  function showReadOnlyHistory(){
    readOnly=true;
    const form=$('experiment-form'),status=$('status');
    form.after(status);form.hidden=true;
    form.parentElement.querySelector('h2').textContent='저장 기록 조회 전용';
    const notice=document.createElement('p');notice.className='warning';
    notice.textContent='이 작업공간은 새 R1 설계 실험 계약을 제공하지 않습니다. 기존 실험의 결과와 원본만 읽을 수 있습니다.';
    form.before(notice);
    $('connection').textContent+=' · 새 실험 불가, 저장 기록 조회 전용';
  }
  function artifactLinks(job){const names=['experiment-plan.json','selection-before-evaluation.json','experiment-result.json','metrics.csv'];return (job.artifacts||job.result?.artifacts||[]).filter(a=>names.includes(a.name)).map(a=>`<a href="${esc(a.url)}" download>${esc(a.name)}</a>`).join('');}
  const assetLabel=item=>item.model_id?`<small>모델 ${esc(item.model_id)} · 장면 ${esc(item.scene_id||'—')}</small>`:'<small>구 연구 예제</small>';
  function renderRows(){if(!result)return;const split=$('split').value,rows=result.rows.filter(r=>split==='all'||(split==='failed'?r.violations.length:r.split===split));$('rows').innerHTML=rows.map(r=>`<tr><td>${esc(r.candidate_id)}<br>${assetLabel(r)}<br>${esc(r.scenario_id)}</td><td>${number(r.green_dn)}</td><td>${number(r.green_snr_p10)}</td><td>${number(100*r.well_fraction,2)}% / ${number(100*r.white_fraction,2)}%</td><td>${r.noise_enabled?'ON · '+esc(r.seed):'OFF'}</td><td class="${r.violations.length?'fail':'pass'}">${esc(label(r.violations))}</td><td><button class="small" data-case="${esc(r.case_id)}">RAW 보기</button></td></tr>`).join('');}
  function resetReplay(){
    replayEpoch++;clearTimeout(replayTimer);replayBusy=false;currentCase=null;
    $('isp-replay').hidden=true;$('replay-preview').hidden=true;$('replay-comparison').hidden=true;
    $('replay-provenance').hidden=true;$('replay-rgb-download').hidden=true;$('replay-export').hidden=true;
    $('replay-rgb-image').removeAttribute('src');$('replay-rgb-download').removeAttribute('href');
    $('replay-saved').innerHTML='<option value="">선택하세요</option>';$('replay-status').textContent='';
  }
  function replayProfile(){
    if(!currentJob?.isp_profile||!currentCase)throw new Error('원본 ISP 프로필과 RAW 사례를 먼저 선택하세요.');
    const profile=JSON.parse(JSON.stringify(currentJob.isp_profile));
    const gains=['wb-r','wb-g','wb-b'].map(id=>Number($(id).value));
    if(gains.some(v=>!Number.isFinite(v)||v<1/16||v>16))throw new Error('WB gain은 1/16–16의 유한한 값이어야 합니다.');
    const rows=$('replay-ccm').value.split(';').map(row=>row.split(',').map(v=>v.trim()));
    if(rows.some(row=>row.some(v=>!v)))throw new Error('CCM의 모든 값을 입력하세요.');
    rows.forEach((row,i)=>{rows[i]=row.map(Number);});
    if(rows.length!==3||rows.some(row=>row.length!==3||row.some(v=>!Number.isFinite(v)||v< -8||v>8)))throw new Error('CCM을 유한한 3×3 행렬로 입력하세요.');
    profile.white_balance.gains_rgb=gains;profile.ccm.matrix_rgb=rows;profile.tone.curve=$('replay-tone').value;
    return profile;
  }
  function replayRequest(){return {schema:'camera_design_isp_replay_v1',name:$('replay-name').value.trim(),
    experiment_id:active,case_id:currentCase.row.case_id,profile:replayProfile()};}
  async function listReplays(caseId,stamp,selected=''){
    const rows=await api('design-isp-replays?experiment_id='+encodeURIComponent(active)+'&case_id='+encodeURIComponent(caseId));
    if(stamp!==replayEpoch||currentCase?.row.case_id!==caseId)return;
    $('replay-saved').innerHTML='<option value="">선택하세요</option>'+rows.map(r=>`<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.status)}</option>`).join('');
    if(selected&&rows.some(r=>r.id===selected))$('replay-saved').value=selected;
  }
  async function loadReplay(id,stamp){
    const job=await api('design-isp-replays/'+encodeURIComponent(id));
    if(stamp!==replayEpoch||currentCase?.row.case_id!==job.source?.case_id||active!==job.source?.experiment_id)return;
    if(job.id!==id||job.source.row.raw_artifact.sha256!==currentCase.row.raw_artifact.sha256)throw new Error('ISP 재처리 기록의 원본 RAW가 현재 사례와 다릅니다.');
    $('replay-saved').value=id;$('replay-status').textContent=`${job.name} · ${job.status}${job.error?' · '+job.error:''}`;
    $('replay-export').href='/api/v1/design-isp-replays/'+encodeURIComponent(id)+'/export';$('replay-export').hidden=false;
    $('replay-preview').hidden=true;$('replay-comparison').hidden=true;$('replay-provenance').hidden=true;$('replay-rgb-download').hidden=true;
    if(['queued','running'].includes(job.status))replayTimer=setTimeout(()=>loadReplay(id,stamp).catch(fail),900);
    if(job.status!=='succeeded')return;
    const s=job.result?.summary,artifacts=job.artifacts||[],png=artifacts.find(a=>a.name==='replay-rgb.png'),npy=artifacts.find(a=>a.name==='replay-rgb.npy');
    if(s?.status!=='completed'||s.source_raw_array_sha256!==currentCase.row.raw_array_sha256||s.profile_sha256!==job.profile_sha256||!png?.url||!npy?.url)throw new Error('ISP 재처리 결과의 원본·프로필·산출물 계약이 일치하지 않습니다.');
    $('replay-rgb-image').src=png.url;$('replay-rgb-download').href=npy.url;$('replay-preview').hidden=false;$('replay-rgb-download').hidden=false;
    $('replay-comparison').textContent=`동일 원본 RAW의 RGB 차이 · 평균 절대 차이 ${number(s.comparison.mean_abs_rgb_delta,6)}, 최대 ${number(s.comparison.max_abs_rgb_delta,6)} (0–1 정규화). 새로운 촬영 0회. 이 수치는 색 정확도 검증이 아닙니다.`;
    $('replay-comparison').hidden=false;$('replay-evidence').textContent=JSON.stringify({source_experiment_id:s.source_experiment_id,source_case_id:s.source_case_id,source_raw_sha256:s.source_raw_sha256,source_original_rgb_sha256:s.source_original_rgb_sha256,source_original_profile_sha256:s.source_original_profile_sha256,original_profile:job.source.original_profile,profile_sha256:s.profile_sha256,replay_profile:job.profile,profile_source:s.profile_source,physical_validation:s.physical_validation},null,2);$('replay-provenance').hidden=false;
  }
  async function openReplayForCase(detail){
    resetReplay();if(currentJob?.specification?.schema!=='camera_design_experiment_v2')return;
    if(!detail.row.geometry||!detail.row.raw_format?.bit_depth){$('replay-status').textContent='이전 기록은 완전한 RAW 형식·좌표가 없어 재처리를 지원하지 않습니다.';$('isp-replay').hidden=false;$('replay-form').hidden=true;return;}
    currentCase=detail;const stamp=replayEpoch,profile=currentJob.isp_profile;
    if(!profile||profile.schema!=='camera_isp_profile_v1')throw new Error('원본 ISP v1 프로필이 없습니다.');
    ['wb-r','wb-g','wb-b'].forEach((id,i)=>$(id).value=profile.white_balance.gains_rgb[i]);
    $('replay-ccm').value=profile.ccm.matrix_rgb.map(row=>row.join(',')).join('; ');
    $('replay-tone').value=profile.tone.curve;$('replay-form').hidden=false;$('isp-replay').hidden=false;
    await listReplays(detail.row.case_id,stamp);
  }
  function render(job){
    currentJob=job;result=CameraDesignExperimentVisibleSummary(job);$('result').hidden=!result;$('preview').hidden=true;resetReplay();if(!result)return;
    const chosen=result.selection?.selected_candidate_id;$('choice').textContent=chosen?'조건부 선택: '+chosen:'선택 없음 · 충족 후보 없음';
    const evaluation=result.evaluation;$('verdict').textContent=`취득 ${result.new_captures}회 · 적합 후보 ${result.selection?.feasible_count??'—'}개. `+(evaluation?.count?`별도 평가 ${evaluation.passed}/${evaluation.count}회 기준 충족. 실패 후 재선택하지 않았습니다.`:evaluation?.skipped_reason?'적합 후보가 없어 평가하지 않았습니다.':'');
    $('scope').textContent='신호·SNR·포화 제약의 조건부 결과입니다. 움직임·색 정확도는 선택 목적에 포함되지 않습니다. 반복한 평가를 독립 검증으로 해석하지 마세요. 고정 ISP RGB는 시각화 산출물이며 하드웨어 등가성이나 실물 정확도 근거가 아닙니다.';
    $('downloads').innerHTML=artifactLinks(job)+`<a href="/api/v1/design-experiments/${encodeURIComponent(job.id)}/export">전체 기록 JSON</a>`;
    $('provenance').textContent=JSON.stringify({specification:result.specification,source_identities:result.source_identities,evaluation:result.evaluation},null,2);
    $('candidates').innerHTML=(result.selection?.candidates||[]).map(c=>`<tr><td>${esc(c.candidate_id)}<br>${assetLabel(c)}</td><td>${number(c.minimum_green_dn)}</td><td>${number(c.worst_green_snr_p10)}</td><td class="${c.feasible?'pass':'fail'}">${esc(label(c.failures))}</td></tr>`).join('');renderRows();
  }
  async function load(id){
    const mine=++epoch,switched=active!==id;active=id;clearTimeout(timer);
    if(switched){result=null;$('result').hidden=true;$('preview').hidden=true;$('job-export').hidden=true;$('status').textContent='선택한 실행 원본을 불러오고 있습니다…';}
    const job=await api('design-experiments/'+encodeURIComponent(id));if(mine!==epoch)return;
    if(job.id!==id)throw new Error('선택한 설계 실험과 원본 응답이 일치하지 않습니다.');
    busy=['queued','running'].includes(job.status);$('cancel').hidden=!busy;$('status').textContent=`${job.name} · ${job.status}${job.error?' · '+job.error:''}`;budget();render(job);
    $('job-export').hidden=false;$('job-export').href='/api/v1/design-experiments/'+encodeURIComponent(id)+'/export';
    history.replaceState(null,'','?experiment='+encodeURIComponent(id));$('saved').value=id;
    if(busy)timer=setTimeout(()=>load(id).catch(fail),900);else await listing();
  }
  $('experiment-form').addEventListener('input',budget);
  $('experiment-form').addEventListener('submit',async e=>{e.preventDefault();if(busy||readOnly)return;clearError();busy=true;budget();$('status').textContent='명시한 입력을 고정해 제출합니다…';try{const job=await api('design-experiments',specification());await listing();await load(job.id);}catch(error){busy=false;budget();fail(error);}});
  $('cancel').addEventListener('click',async()=>{try{await api('design-experiments/'+encodeURIComponent(active)+'/cancel',{});await load(active);}catch(error){fail(error);}});
  $('saved').addEventListener('change',()=>{if($('saved').value)load($('saved').value).catch(fail);});
  $('reload').addEventListener('click',()=>listing().catch(fail));$('split').addEventListener('change',renderRows);
  $('replay-form').addEventListener('submit',async e=>{
    e.preventDefault();if(replayBusy||!currentCase)return;clearError();replayBusy=true;$('replay-submit').disabled=true;
    const caseId=currentCase.row.case_id,stamp=replayEpoch;
    try{
      const queued=await api('design-isp-replays',replayRequest());
      if(stamp!==replayEpoch||currentCase?.row.case_id!==caseId)return;
      await listReplays(caseId,stamp,queued.id);await loadReplay(queued.id,stamp);
    }catch(error){if(stamp===replayEpoch)fail(error);}
    finally{replayBusy=false;$('replay-submit').disabled=false;}
  });
  $('replay-saved').addEventListener('change',()=>{const id=$('replay-saved').value;if(id){clearTimeout(replayTimer);loadReplay(id,replayEpoch).catch(fail);}});
  $('rows').addEventListener('click',async e=>{
    const button=e.target.closest('[data-case]');if(!button)return;
    const id=active,caseId=button.dataset.case;$('preview').hidden=true;resetReplay();const stamp=replayEpoch;
    try{
      const d=await api(`design-experiments/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}`);
      if(active!==id||stamp!==replayEpoch)return;
      const {rgb}=CameraDesignExperimentCaseAssets(d,caseId,result?.specification?.schema);
      $('preview-title').textContent=d.row.candidate_id+' · '+d.row.scenario_id;
      $('raw-image').src=d.images.raw;$('rail-image').src=d.images.final_code_rail;
      $('raw-download').href=d.row.raw_artifact.url;$('diagnostics-download').href=d.row.diagnostics_artifact.url;
      $('rgb-preview').hidden=!rgb;$('rgb-download').hidden=!rgb;
      if(rgb){$('rgb-image').src=d.images.rgb;$('rgb-download').href=d.row.rgb_artifact.url;}
      else{$('rgb-image').removeAttribute('src');$('rgb-download').removeAttribute('href');}
      $('preview').hidden=false;
      if(rgb)await openReplayForCase(d);
    }catch(error){fail(error);}
  });
  (async()=>{const session=await api('session');token=session.token;$('connection').textContent='현재 작업공간: '+session.state_dir;
    let data;
    try{data=await api('design-experiments/template');}
    catch(error){await listing();showReadOnlyHistory();const id=new URL(location.href).searchParams.get('experiment');if(id)await load(id);return;}
    template=data.specification;catalog=data.catalog;
    if(template?.schema==='camera_design_experiment_v2'&&data.project_mode?.mode!=='r1_engineering')throw new Error('R1 프로젝트 실행 모드를 확인할 수 없습니다.');
    renderCatalog();$('name').value=template.name;await listing();budget();const id=new URL(location.href).searchParams.get('experiment');if(id)await load(id);
  })().catch(error=>{$('connection').textContent='설계 실험 계약을 확인하지 못했습니다.';fail(error);});
})();

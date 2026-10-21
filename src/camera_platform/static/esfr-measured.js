'use strict';
(function (global) {
  const ROOT = '/api/v1/camera-validation/esfr';
  const EDGE_COUNT = 112, CAPTURE_COUNT = 3;
  const list = value => Array.isArray(value) ? value : [];
  const copy = value => JSON.parse(JSON.stringify(value));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => typeof value === 'number' && Number.isFinite(value) ? new Intl.NumberFormat('ko-KR', {maximumSignificantDigits:5}).format(value) : '—';
  const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$/.test(value);
  function parseJSON(text, label, array = false) {
    let value; try { value = JSON.parse(text); } catch (error) { throw Error(label + ': JSON 형식을 확인하세요. ' + error.message); }
    if (array ? !Array.isArray(value) : !value || typeof value !== 'object' || Array.isArray(value)) throw Error(label + (array ? '는 배열이어야 합니다.' : '는 객체여야 합니다.'));
    return value;
  }
  function sourceFrom(query) { return {run_id:query.get('source_run') || query.get('run') || '', candidate_id:query.get('candidate') || query.get('candidate-b') || '', condition_id:query.get('condition') || ''}; }
  function sourceValue(source) {
    const result = {run_id:String(source?.run_id || '').trim(), candidate_id:String(source?.candidate_id || '').trim()};
    if (!identity(result.run_id) || !identity(result.candidate_id)) throw Error('DB 실행 ID와 후보 ID를 정확히 입력하세요.');
    if (source?.condition_id) { if (!identity(String(source.condition_id).trim())) throw Error('DB 조건 ID 형식을 확인하세요.'); result.condition_id = String(source.condition_id).trim(); }
    return result;
  }
  function sourceURL(source, path = '/esfr-measured.html', workflow = '') {
    const query = new URLSearchParams();
    if (source?.run_id) query.set(path === '/db-design.html' ? 'run' : 'source_run', source.run_id);
    if (source?.candidate_id) query.set(path === '/db-design.html' ? 'candidate-b' : 'candidate', source.candidate_id);
    if (source?.condition_id) query.set('condition', source.condition_id);
    if (workflow) query.set('workflow', workflow);
    return path + (query.size ? '?' + query : '');
  }
  function safeURL(value) {
    if (typeof value !== 'string' || /[\\\r\n]/.test(value)) return null;
    try {
      const parsed = new URL(value, 'http://camera.local');
      if (parsed.origin !== 'http://camera.local' || !value.startsWith('/') || value.startsWith('//')) return null;
      const decoded = decodeURIComponent(parsed.pathname);
      if (decoded.split('/').some(part => part === '..' || part === '.') || /[\\]/.test(decoded)) return null;
      if (!decoded.startsWith('/api/v1/calibration-lab/') && !decoded.startsWith(ROOT + '/') && !['/calibration-lab.html','/db-design.html','/camera-validation-esfr.html','/esfr-measured.html'].includes(decoded)) return null;
      return parsed.pathname + parsed.search + parsed.hash;
    } catch (_) { return null; }
  }
  function contractOf(workflow) { return workflow?.contract || workflow?.acquisition_contract || workflow?.validation_contract || null; }
  function candidateFrozen(workflow) {
    const contract = contractOf(workflow);
    return !!contract && identity(contract.id || contract.contract_id) && /^[a-f0-9]{64}$/i.test(contract.candidate_sha256||'') && /^[a-f0-9]{64}$/i.test(contract.content_hash||'');
  }
  function roleFor(workflow) { return candidateFrozen(workflow) ? 'validation' : 'development'; }
  function metadataForRole(template, role, workflow = null) {
    const value = copy(template?.capture_metadata || template?.capture_metadata_template || template?.metadata_template || {});
    if (role === 'validation') {
      const contract = contractOf(workflow);
      Object.assign(value,copy(contract?.capture_context || {}),copy(template?.validation_additions || {}));
      if (contract?.id && contract?.content_hash) value.validation_contract={id:contract.id,content_hash:contract.content_hash};
    }
    value.source_kind = 'measured'; value.role = role;
    return value;
  }
  function captureList(value) { return Array.isArray(value) ? value : list(value?.captures || value?.items); }
  function measuredCapture(capture) { return capture?.metadata?.source_kind === 'measured' && ['development','validation'].includes(capture.metadata.role); }
  function captureShape(capture) {
    const shape = capture?.raw_shape || [capture?.metadata?.raw_format?.height, capture?.metadata?.raw_format?.width];
    return list(shape).length === 2 && shape.every(value => Number.isInteger(value) && value > 0) ? shape : null;
  }
  function normalizeRois(value, shape = null) {
    const rows = Array.isArray(value) ? value : value?.rois;
    if (!Array.isArray(rows)) throw Error('ROI JSON은 배열 또는 rois 배열을 포함한 객체여야 합니다.');
    const seen = new Set();
    for (const row of rows) {
      if (!row || !identity(row.id) || seen.has(row.id)) throw Error('각 ROI에는 중복되지 않는 ID가 필요합니다.');
      seen.add(row.id);
      if (!['edge','gray','color','wedge'].includes(row.kind)) throw Error(row.id + ': ROI kind를 확인하세요.');
      const box = row.box;
      if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isSafeInteger) || box[0] < 0 || box[1] < 0 || box[2] < 8 || box[3] < 8) throw Error(row.id + ': box는 유효한 [행, 열, 높이, 너비] 정수이며 크기는 8px 이상이어야 합니다.');
      if(row.kind==='edge' && Math.min(box[2],box[3])<32)throw Error(row.id+': Bayer SFR용 edge는 가로·세로 32 native pixel 이상이어야 합니다.');
      if (shape && (box[0] + box[2] > shape[0] || box[1] + box[3] > shape[1])) throw Error(row.id + ': ROI가 원본 촬영 범위를 벗어납니다.');
    }
    const count = rows.filter(row => row.kind === 'edge').length;
    if (count !== EDGE_COUNT) throw Error('112개 edge 전체가 필요합니다. 현재 ' + count + '개입니다.');
    return copy(rows);
  }
  function metadataIssues(metadata, role) {
    const errors = [];
    if (metadata.source_kind !== 'measured') errors.push('source_kind는 measured여야 합니다.');
    if (metadata.role !== role) errors.push('현재 단계의 role은 ' + role + '입니다.');
    for (const key of ['camera_id','session_id','acquisition_id']) if (!identity(metadata[key]) || /^enter[-_]/i.test(metadata[key])) errors.push(key + '에 실제 식별자를 입력하세요.');
    if (!metadata.captured_at || !Number.isFinite(Date.parse(metadata.captured_at)) || !/(?:Z|[+-]\d\d:\d\d)$/i.test(metadata.captured_at)) errors.push('captured_at에 실제 노출 시각과 시간대를 입력하세요.');
    const raw = metadata.raw_format || {};
    for (const key of ['width','height','stride_bytes','bit_depth']) if (!Number.isInteger(raw[key]) || raw[key] <= 0) errors.push('raw_format.' + key + '를 확인하세요.');
    for (const key of ['packing','byte_order','cfa']) if (typeof raw[key] !== 'string' || !raw[key]) errors.push('raw_format.' + key + '를 확인하세요.');
    for (const key of ['header_bytes','bit_shift','black_level_dn','white_level_dn']) if (raw[key] == null) errors.push('raw_format.' + key + '를 실제 값으로 입력하세요.');
    const pair = metadata.frame_pair || {};
    if (pair.same_exposure !== true || !pair.pair_id || !pair.raw_frame_id || pair.raw_frame_id!==pair.isp_frame_id) errors.push('동일 노출 RAW·ISP frame_pair와 같은 sensor frame ID를 입력하세요.');
    if (!metadata.settings_provenance?.mode || !metadata.settings_provenance?.reference) errors.push('설정 기록의 출처(settings_provenance)를 입력하세요.');
    if(typeof metadata.pixel_pitch_um!=='number'||!Number.isFinite(metadata.pixel_pitch_um)||metadata.pixel_pitch_um<=0)errors.push('실제 pixel_pitch_um을 입력하세요.');
    for(const key of ['gain','exposure_s','temperature_c','aperture_f_number','focus_distance_m','distance_m'])if(typeof metadata.conditions?.[key]!=='number'||!Number.isFinite(metadata.conditions[key]))errors.push('conditions.'+key+'에 확인된 고정 설정을 입력하세요.');
    for(const key of ['mode_id','light_id','focus_setting'])if(typeof metadata.conditions?.[key]!=='string'||!metadata.conditions[key].trim())errors.push('conditions.'+key+'를 입력하세요.');
    const sampling=metadata.sampling||{};
    if(JSON.stringify(sampling.binning)!=='[1,1]'||JSON.stringify(sampling.subsampling)!=='[1,1]'||sampling.resized!==false||sampling.mirrored!==false||sampling.rotation_deg!==0)errors.push('이 경로는 binning/subsampling [1,1], resize·mirror false, rotation 0의 native 좌표만 지원합니다.');
    const rawProcessing={linear_response:true,demosaiced:false,denoised:false,sharpened:false,companded:false,geometric_transform:false};
    if(Object.keys(metadata.raw_processing||{}).length!==Object.keys(rawProcessing).length||Object.entries(rawProcessing).some(([key,value])=>metadata.raw_processing?.[key]!==value))errors.push('raw_processing의 6개 항목으로 선형 Bayer·demosaic/NR/sharpening/companding/기하 변환 미적용을 직접 확인하세요.');
    if (!['srgb','linear'].includes(metadata.isp?.transfer)) errors.push('이 경로는 확인된 srgb/linear ISP transfer만 지원합니다. unknown을 임의로 sRGB로 바꾸지 마세요.');
    if(metadata.isp?.coordinate_mapping!=='same_native_raster')errors.push('ISP는 RAW와 같은 native raster여야 합니다. crop/resize 대응 미확인 자료는 등록할 수 없습니다.');
    if(raw.cfa!=='RGGB')errors.push('현재 112개 DB 보정 연결은 RGGB만 지원합니다. 다른 CFA를 RGGB로 바꾸어 선언하지 마세요.');
    if(role==='validation'){
      if(!identity(metadata.validation_contract?.id)||!/^[a-f0-9]{64}$/i.test(metadata.validation_contract?.content_hash||''))errors.push('검증 metadata에 발급된 취득 계약 ID와 hash가 필요합니다.');
      const attestation=metadata.acquisition_attestation||{};
      if(!identity(attestation.operator_id)||!['captured_after_contract','original_raw_isp_pair','camera_and_chart_unchanged'].every(key=>attestation[key]===true))errors.push('검증은 후보 고정 이후 취득·원본 쌍·고정 배치에 대한 검토자 확인이 필요합니다.');
    }
    return errors;
  }
  function workflowRunning(workflow) {
    if (!workflow) return false;
    return ['queued','running','pending'].includes(workflow.status) || ['queued','running'].includes(workflow.current_job?.status);
  }
  function frozenIDs(workflow, key) { return list(workflow?.[key] || workflow?.specification?.[key] || workflow?.request?.[key]); }
  function readiness(state, role) {
    const errors = [], selected = [...state[role]], captures = captureList(state.captures);
    if (selected.length !== CAPTURE_COUNT) errors.push((role === 'development' ? '개발' : '새 검증') + ' 촬영을 정확히 3쌍 선택하세요.');
    if (selected.some(id => !captures.some(c => c.id === id && measuredCapture(c) && c.metadata.role === role))) errors.push('선택한 원본 촬영의 measured 역할이 일치하지 않습니다.');
    if (selected.some(id => !state.reviewed.has(id))) errors.push('선택한 모든 촬영에서 112개 ROI 확인을 기록하세요.');
    try { normalizeRois(state.rois); } catch (error) { errors.push(error.message); }
    if (!identity(state.operator)) errors.push('검토자 ID를 입력하세요.');
    if (!identity(state.physicalChart)) errors.push('실물 차트 개체 ID를 입력하세요.');
    try { sourceValue(state.source); } catch (error) { errors.push(error.message); }
    if (role === 'development' && state.workflow) errors.push('이미 시작한 작업입니다. 새 피팅은 새 작업에서 진행하세요.');
    if (role === 'validation' && !candidateFrozen(state.workflow)) errors.push('개발 후보 고정과 취득 계약이 먼저 필요합니다.');
    if(role==='validation'&&candidateFrozen(state.workflow)){
      const contract=contractOf(state.workflow);
      if(selected.some(id=>{const binding=captures.find(c=>c.id===id)?.metadata?.validation_contract;return binding?.id!==contract.id||binding?.content_hash!==contract.content_hash;}))errors.push('선택한 검증 촬영이 현재 후보의 취득 계약과 연결되지 않았습니다.');
      const sessions=new Set(captures.filter(c=>state.development.has(c.id)).map(c=>c.metadata.session_id));
      if(captures.some(c=>selected.includes(c.id)&&sessions.has(c.metadata.session_id)))errors.push('개발과 새 검증은 서로 다른 session_id를 사용해야 합니다.');
    }
    if (role === 'validation' && frozenIDs(state.workflow, 'validation_ids').length) errors.push('이 작업은 이미 검증 촬영을 고정했습니다. 기존 결과를 확인하세요.');
    if (workflowRunning(state.workflow)) errors.push('현재 작업의 계산이 끝날 때까지 기다리세요.');
    if(state.workflow?.source_kind==='synthetic')errors.push('합성 동작 검증 기록은 결과 열람용입니다. 실측 업로드는 새 작업에서 시작하세요.');
    errors.push(...iqReadiness(state,role));
    return errors;
  }
  const IQ_REFERENCE='imatest_esfr_2023_nominal_v1';
  function parseCaptureIDs(text){const ids=String(text||'').split(/[\s,]+/).filter(Boolean);if(ids.some(id=>!identity(id)))throw Error('반복 촬영 ID 형식을 확인하세요.');if(new Set(ids).size!==ids.length)throw Error('반복 촬영 ID는 중복될 수 없습니다.');if(ids.length>5)throw Error('기본 3개 외 추가 반복 촬영은 0–5개입니다 (총 3–8프레임).');return ids;}
  function iqPatchCounts(rois){return {color:list(rois).filter(row=>row.kind==='color').length,gray:list(rois).filter(row=>row.kind==='gray').length};}
  function iqReadiness(state,role){
    if(!state.iq?.enabled)return [];
    const errors=[],counts=iqPatchCounts(state.rois),selected=[...state[role]];
    if(counts.color!==18||counts.gray!==20)errors.push('IQ에는 color 18개와 gray 20개 패치 ROI 전체가 필요합니다.');
    if(state.iq.reference_id!==IQ_REFERENCE)errors.push('지원되는 버전 고정 IQ 명목 기준을 선택하세요.');
    if(!state.iq.nominal_confirmed)errors.push('명목 기준은 실물 인쇄물의 실측 기준값이 아님을 확인하세요.');
    if(!['srgb','unknown'].includes(state.iq.actual_isp_color_space))errors.push('실제 ISP의 색 공간을 sRGB 또는 미확인으로 명시하세요.');
    if(state.iq.actual_isp_color_space==='srgb'&&!state.iq.srgb_confirmed)errors.push('실제 ISP의 sRGB 색 공간 근거를 확인하세요.');
    if(selected.some(id=>!state.iqReviewed?.has(id)))errors.push('선택한 촬영마다 색 18개·계조 20개 ROI를 확인하세요.');
    try{const noise=parseCaptureIDs(state.noiseText);if(noise.some(id=>selected.includes(id)))errors.push('추가 반복 촬영은 기본 3개와 겹칠 수 없습니다.');if(role==='validation'&&noise.length!==list(state.workflow?.iq?.noise_repeat_ids).length)errors.push('새 검증의 추가 반복 수는 개발에서 고정한 수와 같아야 합니다.');if(noise.some(id=>!state.captures.some(c=>c.id===id&&c.metadata?.role===role)))errors.push('반복 촬영은 등록된 현재 단계의 역할과 일치해야 합니다.');if(noise.some(id=>!state.iqReviewed?.has(id)))errors.push('추가 반복 촬영도 색·계조 38개 패치 ROI를 확인하세요.');}catch(error){errors.push(error.message);}
    return errors;
  }
  function iqRequest(state,role){
    if(!state.iq?.enabled)return {};
    const errors=iqReadiness(state,role);if(errors.length)throw Error(errors.join(' '));
    const noise=parseCaptureIDs(state.noiseText),ids=[...new Set([...state[role],...noise])];
    return role==='development'?{iq:{enabled:true,reference_id:state.iq.reference_id,actual_isp_color_space:state.iq.actual_isp_color_space,patch_roi_reviewed_capture_ids:ids,noise_repeat_ids:noise}}:{iq_patch_roi_reviewed_capture_ids:ids,iq_noise_repeat_ids:noise};
  }
  function comparisonRequest(a,b){if(!identity(a)||!identity(b)||!a.startsWith('measured-esfr-')||!b.startsWith('measured-esfr-'))throw Error('완료된 두 measured-esfr workflow ID를 입력하세요.');if(a===b)throw Error('서로 다른 두 모드의 workflow를 선택하세요.');return{workflow_a_id:a,workflow_b_id:b};}
  function reviewPayload(state, role) {
    const errors = readiness(state, role); if (errors.length) throw Error(errors.join(' '));
    return {rois:copy(state.rois), capture_ids:[...state[role]], operator_id:state.operator, physical_chart_id:state.physicalChart,
      all_112_edges_reviewed:true, registration_suitable:true, fixed_camera_chart:true, no_clipping_or_occlusion:true};
  }
  function mergeValidationReview(workflow, validationReview, validationIDs) {
    const development=workflow?.measured_review,oldRows=list(development?.capture_reviews),newRows=list(validationReview?.capture_reviews),oldIDs=frozenIDs(workflow,'development_ids');
    const sameIDs=(rows,ids)=>rows.length===CAPTURE_COUNT&&ids.length===CAPTURE_COUNT&&new Set(ids).size===CAPTURE_COUNT&&new Set(rows.map(row=>row.capture_id)).size===CAPTURE_COUNT&&rows.every(row=>ids.includes(row.capture_id));
    if(!development||!sameIDs(oldRows,oldIDs)||!sameIDs(newRows,list(validationIDs)))throw Error('고정된 개발 3개 확인과 새 검증 3개 확인이 모두 필요합니다.');
    if(newRows.some(row=>oldIDs.includes(row.capture_id)))throw Error('개발과 검증의 ROI 확인 원본이 겹칩니다.');
    for(const key of ['roi_sha256','physical_chart_id','chart_model'])if(!development[key]||development[key]!==validationReview[key])throw Error('개발과 새 검증의 고정 ROI·실물 차트 근거가 다릅니다.');
    return {...copy(validationReview),capture_reviews:[...copy(oldRows),...copy(newRows)],development_review:copy(development)};
  }
  function stageLabel(workflow) {
    const labels = {queued:'실행 대기',running:'진행 중',failed:'실행 실패',succeeded:'완료',completed:'완료',
      awaiting_validation:'후보 고정 완료 · 새 검증 촬영 대기',awaiting_validation_upload:'후보 고정 완료 · 새 검증 촬영 대기',
      development_measurement:'개발 영상 측정',measuring_development:'개발 영상 측정',exporting_development:'개발 측정값 연결',
      fitting:'112개 native RAW 응답 보정',fit:'112개 native RAW 응답 보정',validation_measurement:'새 검증 영상 측정',
      measuring_validation:'새 검증 영상 측정',validating:'고정 후보 검증',applying:'조건부 프로파일 연결',reporting:'통합 보고서 생성'};
    return [labels[workflow?.status] || workflow?.status, labels[workflow?.stage] || workflow?.stage].filter(Boolean).join(' · ');
  }
  function resultLinks(workflow) {
    const value = workflow?.links || {}, rows = [];
    const add = (label, url) => { const valid = safeURL(url); if (valid && !rows.some(row => row.url === valid)) rows.push({label,url:valid}); };
    for (const [key,label] of [['model_report','선택 카메라 모델·실측 통합 비교'],['model_bundle','카메라 모델 비교 ZIP'],['iq_report','색·계조·노이즈 IQ 보고서'],['iq_bundle','IQ 후보·근거 ZIP'],['report','통합 보정·검증 보고서'],['bundle','원본·근거 ZIP'],['lab','Calibration Lab'],['image_report','새 검증 RAW·ISP 보고서'],['report_url','통합 보정·검증 보고서'],['completion_report_url','통합 보정·검증 보고서'],['report_html','통합 보고서'],['zip_url','원본·근거 ZIP'],['bundle_url','원본·근거 ZIP'],['calibration_lab_url','Calibration Lab'],['lab_url','Calibration Lab'],['db_url','DB Design Lab'],['development_report_url','개발 측정 보고서'],['validation_report_url','새 검증 RAW·ISP 보고서']]) add(label,value[key]);
    add('통합 보정·검증 보고서',workflow?.report?.report_url || workflow?.report?.url || workflow?.report_url);
    add('원본·근거 ZIP',workflow?.report?.bundle_url || workflow?.report?.zip_url || workflow?.zip_url);
    return rows;
  }
  function createAPI(request){return async(path,body)=>{
    const options={method:body===undefined?'GET':'POST',cache:'no-store',headers:{}};
    if(body!==undefined){
      const session=await request('/api/v1/session',{cache:'no-store'});if(!session.ok)throw Error('로컬 세션을 읽을 수 없습니다. 새로고침 후 다시 시도하세요.');
      const token=(await session.json()).token;if(typeof token!=='string'||!token)throw Error('로컬 세션 토큰을 확인할 수 없습니다.');
      options.headers['X-Platform-Token']=token;
      if(typeof global.FormData==='function'&&body instanceof global.FormData)options.body=body;
      else{options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
    }
    const response=await request(ROOT+path,options);let data;
    try{data=await response.json();}catch(_){throw Error('서버 응답을 읽을 수 없습니다. HTTP '+response.status);}
    if(!response.ok){const error=data.error||data.detail||data.message||'HTTP '+response.status;throw Error(typeof error==='string'?error:JSON.stringify(error));}
    return data;
  };}
  const exported = {ROOT,EDGE_COUNT,CAPTURE_COUNT,esc,parseJSON,sourceFrom,sourceValue,sourceURL,safeURL,contractOf,candidateFrozen,
    roleFor,metadataForRole,captureList,measuredCapture,captureShape,normalizeRois,metadataIssues,workflowRunning,frozenIDs,readiness,reviewPayload,mergeValidationReview,stageLabel,resultLinks,createAPI,IQ_REFERENCE,parseCaptureIDs,iqPatchCounts,iqReadiness,iqRequest,comparisonRequest};
  global.CameraESFRMeasured = exported;
  const entry=global.document?.getElementById('esfr-measured-entry');
  if(entry){const update=()=>{const query=sourceFrom(new URL(global.location.href).searchParams),read=(id,fallback)=>global.document.getElementById('esfr-'+id)?.value||fallback;entry.href=sourceURL({run_id:read('source-run',query.run_id),candidate_id:read('source-candidate',query.candidate_id),condition_id:read('source-condition',query.condition_id)});};entry.addEventListener('click',update);global.document.addEventListener('change',update);global.addEventListener('popstate',update);update();}
  if (!global.document?.getElementById('measured-app')) return;

  const doc = global.document, $ = id => doc.getElementById('measured-' + id);
  const state = {template:null,captures:[],source:sourceFrom(new URL(global.location.href).searchParams),workflow:null,
    development:new Set(),validation:new Set(),reviewed:new Set(),rois:[],chartProfile:{},operator:'',physicalChart:'',preview:'',busy:false,ready:false,
    iq:{enabled:true,reference_id:IQ_REFERENCE,actual_isp_color_space:'unknown',nominal_confirmed:false,srgb_confirmed:false},iqReviewed:new Set(),noiseText:''};
  let timer = null, epoch = 0, previewEpoch = 0, previewReady = false;
  function notice(message, error = false) { $('notice').hidden = !message; $('notice').textContent = message; $('notice').classList.toggle('error',error); }
  const api=createAPI(global.fetch.bind(global));
  async function busy(action) {
    if (state.busy) return; state.busy = true; renderControls(); notice('');
    try { await action(); } catch (error) { notice(error.message,true); }
    finally { state.busy = false; render(); }
  }
  function syncInputs() {
    state.source = {run_id:$('source-run').value.trim(),candidate_id:$('source-candidate').value.trim(),condition_id:$('source-condition').value.trim()};
    state.operator = $('operator').value.trim(); state.physicalChart = $('chart-id').value.trim();
    state.iq={enabled:$('iq-enabled').checked,reference_id:$('iq-reference').value,actual_isp_color_space:$('iq-color-space').value,nominal_confirmed:$('iq-nominal-confirmed').checked,srgb_confirmed:$('iq-srgb-confirmed').checked};state.noiseText=$('iq-noise-ids').value;
  }
  function updateLinks() {
    $('db-link').href = sourceURL(state.source,'/db-design.html');
    for (const id of ['editor-link','roi-editor']) $(id).href = sourceURL(state.source,'/camera-validation-esfr.html');
  }
  function download(name, value) {
    const blob = new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}), url = URL.createObjectURL(blob), a = doc.createElement('a');
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url),0);
  }
  function fillMetadata() { $('metadata').value = JSON.stringify(metadataForRole(state.template,roleFor(state.workflow),state.workflow),null,2); $('metadata-confirmed').checked = false; }
  function resetReviewChecks() { for (const key of ['review-edges','review-fixed','review-quality','review-patches']) $(key).checked = false; }
  function resetReviews() { state.reviewed.clear(); state.iqReviewed.clear(); resetReviewChecks(); }
  function selectedCaptures() { return state.captures.filter(capture => state.development.has(capture.id) || state.validation.has(capture.id)); }
  function renderCaptures() {
    const rows = state.captures.filter(measuredCapture), role = roleFor(state.workflow), usedValidation = frozenIDs(state.workflow,'validation_ids');
    $('counts').textContent = '개발 ' + state.development.size + ' / 3 · 새 검증 ' + state.validation.size + ' / 3';
    $('captures').innerHTML = rows.length ? rows.map(capture => {
      const meta = capture.metadata, selected = state[meta.role].has(capture.id), locked = state.busy || (meta.role === 'development' && !!state.workflow) || (meta.role === 'validation' && (!candidateFrozen(state.workflow) || usedValidation.length > 0));
      const shape = captureShape(capture);
      return '<div class="measured-capture"><input aria-label="'+esc(capture.id)+' 선택" type="checkbox" data-capture-select="'+esc(capture.id)+'" '+(selected?'checked ':'')+(locked?'disabled':'')+'><label>'+esc(capture.id)+'<small>'+esc(meta.camera_id)+' · '+esc(meta.session_id)+' · '+esc(meta.captured_at||'촬영 시각 미기록')+'</small><small>'+esc(shape?shape[1]+' × '+shape[0]+' px':'크기 미확인')+' · RAW '+esc(meta.raw_format?.packing||'미확인')+' / ISP '+esc(meta.isp?.transfer||'미확인')+'</small><span class="tag '+(meta.role===role?'':'muted')+'">'+(meta.role==='development'?'개발 촬영':'검증 촬영')+'</span><span class="tag '+(state.reviewed.has(capture.id)?'success':'warn')+'">'+(state.reviewed.has(capture.id)?'112 ROI 확인됨':'ROI 확인 필요')+'</span>'+(state.iq.enabled?'<small class="iq-review-status">IQ 38패치 '+(state.iqReviewed.has(capture.id)?'확인됨':'확인 필요')+'</small>':'')+'</label><button type="button" class="quiet" data-preview="'+esc(capture.id)+'">촬영 확인</button></div>';
    }).join('') : '<p class="empty">등록된 실측 촬영이 없습니다. 합성 시험 데이터는 이 작업에 자동 사용하지 않습니다.</p>';
    $('preview-capture').innerHTML = '<option value="">촬영 선택</option>'+rows.map(capture=>'<option value="'+esc(capture.id)+'" '+(capture.id===state.preview?'selected':'')+'>'+esc(capture.id)+' · '+(capture.metadata.role==='development'?'개발':'검증')+'</option>').join('');
  }
  function renderPreview() {
    previewReady = false; const token = ++previewEpoch, capture = state.captures.find(c => c.id === state.preview), shape = captureShape(capture), svg = $('preview'); resetReviewChecks();
    if (!capture || !shape) { svg.setAttribute('viewBox','0 0 1600 900'); svg.innerHTML='<text x="800" y="450" text-anchor="middle">촬영을 선택하세요.</text>'; renderControls(); return; }
    const [height,width] = shape, url = safeURL(capture.preview_url), origin = capture.metadata.geometry?.crop_origin || [0,0];
    svg.setAttribute('viewBox','0 0 '+width+' '+height);
    svg.innerHTML = (url?'<image href="'+esc(url)+'" x="0" y="0" width="'+width+'" height="'+height+'" preserveAspectRatio="none"/>':'<text x="'+width/2+'" y="'+height/2+'" text-anchor="middle">원본 미리보기 주소가 없습니다.</text>')+state.rois.map((roi,index)=>'<g class="roi-item" tabindex="0"><title>'+esc(roi.id)+'</title><rect class="roi-box '+esc(roi.kind)+'" x="'+roi.box[1]+'" y="'+roi.box[0]+'" width="'+roi.box[3]+'" height="'+roi.box[2]+'"/><text class="roi-label" x="'+(roi.box[1]+3)+'" y="'+(roi.box[0]+20)+'">'+(index+1)+'</text></g>').join('');
    $('preview-caption').textContent = capture.id+' · '+width+' × '+height+' px · 센서 crop 원점 ['+origin.join(', ')+'] · 원본 hash '+(capture.files?.raw?.sha256||'서버 검증 시 확인');
    const image = svg.querySelector('image');
    if (image) { image.addEventListener('load',()=>{if(token===previewEpoch){previewReady=true;renderControls();}},{once:true}); image.addEventListener('error',()=>{if(token===previewEpoch){previewReady=false;notice('원본 촬영 미리보기를 불러오지 못해 ROI 확인을 기록할 수 없습니다.',true);renderControls();}},{once:true}); }
    renderControls();
  }
  function renderContract() {
    const contract = contractOf(state.workflow), frozen = candidateFrozen(state.workflow); $('contract').hidden = !contract;
    if (!contract) return;
    $('contract').innerHTML = '<div class="contract"><h3>'+(frozen?'후보 고정 완료 · 이제 새 검증 촬영을 취득하세요':'취득 계약 기록 · 후보 고정 확인 필요')+'</h3><p class="identity">'+esc(contract.id||contract.contract_id)+'</p><p>고정 시각: '+esc(contract.created_at||contract.frozen_at||'계약 근거 참조')+'</p><p class="identity">후보 SHA256 '+esc(contract.candidate_sha256)+'</p><p>검증은 이 후보와 기준을 유지합니다. 이후 취득한 별도 세션의 RAW·ISP 3쌍을 등록하세요.</p><details><summary>고정 계약 상세</summary><pre class="workflow-details">'+esc(JSON.stringify(contract,null,2))+'</pre></details></div>';
  }
  function renderResult() {
    const workflow = state.workflow, container = $('result-body');
    if (!workflow) return;
    const links = resultLinks(workflow), validation = workflow.validation_summary || workflow.validation?.summary || workflow.validation || {}, image = workflow.image_validation || validation.esfr_image_validation || {}, metrics = image.metrics || validation.metrics || workflow.metrics || {};
    const judgment=image.validation||validation.image_validation||validation,rawOnly=judgment.scope==='raw_native_sfr_only',verdict = judgment.status, verdictLabels = {pass:'선언한 수치 기준 충족',fail:'선언한 수치 기준 미충족',inconclusive:'판정 보류',not_evaluated:'미평가'};
    container.innerHTML = '<p class="identity">workflow '+esc(workflow.id)+'</p><p>'+esc(stageLabel(workflow))+'</p>'+(workflow.error?'<p class="measured-error">'+esc(typeof workflow.error==='string'?workflow.error:JSON.stringify(workflow.error))+'</p>':'')+(verdict?'<p><b>'+(rawOnly?'RAW 독립 검증 · ISP는 진단: ':'선언된 비교 범위의 검증: ')+esc(verdictLabels[verdict]||verdict)+'</b></p>':'')+(workflow.profile_id?'<p class="identity">프로파일 '+esc(workflow.profile_id)+' · '+(workflow.profile_activated===true?'이번 작업의 조건부 프로파일 활성화':'이번 작업의 후보를 비활성 상태로 저장')+'</p>':'')+(Object.keys(metrics).length?'<table class="result-table"><thead><tr><th>오차 RMSE</th><th>보정 전 기준</th><th>고정 후보</th><th>최대 오차</th></tr></thead><tbody>'+[['raw_sfr_rmse','RAW Gr'],['isp_sfr_rmse',rawOnly?'ISP · 진단 전용':'ISP · 비교 범위 확인']].map(([key,label])=>'<tr><td>'+label+'</td><td>'+number(metrics[key]?.before)+'</td><td>'+number(metrics[key]?.after)+'</td><td>'+number(metrics[key]?.worst_after)+'</td></tr>').join('')+'</tbody></table>':'')+'<div class="result-links">'+links.map(link=>'<a class="button" href="'+esc(link.url)+'" target="_blank" rel="noopener">'+esc(link.label)+' ↗</a>').join('')+'</div><p class="measured-result-scope">RAW는 lens·sensor·차트·촬영 조건이 합쳐진 유효 응답입니다. ISP 처리 미일치 시 ISP는 진단으로 남습니다. 측정 파일을 등록했다는 사실만으로 물리 검증이나 ISO 인증을 선언하지 않습니다.</p><details><summary>단계·원본 연결·적용 근거</summary><pre class="workflow-details">'+esc(JSON.stringify({id:workflow.id,status:workflow.status,stage:workflow.stage,source:workflow.source,source_kind:workflow.source_kind,camera_model_id:workflow.camera_model_id,camera_model_sha256:workflow.camera_model_sha256,camera_model_scope:workflow.camera_model_scope,fit:workflow.fit,contract:workflow.contract?{id:workflow.contract.id,content_hash:workflow.contract.content_hash,created_at:workflow.contract.created_at,candidate_sha256:workflow.contract.candidate_sha256}:null,validation:workflow.validation?{id:workflow.validation.id,metrics:workflow.validation.metrics}:null,model_comparison:workflow.model_comparison?{id:workflow.model_comparison.id,summary:workflow.model_comparison.summary,report_url:workflow.model_comparison.report_url}:null,links:workflow.links,physical_validation:workflow.physical_validation},null,2))+'</pre></details>';
    const kind=workflow.source_kind||workflow.iq_result?.source_kind;container.insertAdjacentHTML('afterbegin','<p class="source-status"><strong>입력 출처: '+esc(kind==='synthetic'?'합성 동작 검증 · 실제 촬영 아님':kind==='measured'?'실측으로 등록된 사용자 원본 · 장비/시각은 사용자 선언':'저장된 원본 출처 확인 필요')+'</strong></p>');
    if(workflow.iq_result||workflow.iq_frozen)container.insertAdjacentHTML('afterbegin','<div class="iq-summary"><h3>IQ 후보 · 판정 기준 미선언</h3><p>명목 기준의 색·계조·반복 노이즈를 진단합니다. CCM 후보는 같은 RAW의 reference ISP에만 적용하며 자동 활성화하지 않습니다. 실제 ISP의 색 공간: '+esc(state.iq.actual_isp_color_space==='srgb'?'sRGB · 사용자 기록으로 확인':'미확인 · 실제 ISP 색차 미평가')+'</p><p class="identity">Reference '+esc(state.iq.reference_id)+'</p></div>');
  }
  function renderControls() {
    const role = roleFor(state.workflow), locked = state.busy || !state.ready, running = workflowRunning(state.workflow), validationLocked = frozenIDs(state.workflow,'validation_ids').length > 0;
    $('upload-fields').disabled = locked || running || validationLocked || state.workflow?.source_kind==='synthetic';
    $('upload-stage').textContent = role === 'development' ? '개발 촬영' : '새 독립 검증 촬영';
    $('upload').textContent = role === 'development' ? '개발 촬영 쌍 등록' : '새 검증 촬영 쌍 등록';
    $('upload-instruction').textContent = role === 'development' ? '개발 촬영 3쌍을 등록합니다. 검증 촬영은 후보 고정 뒤 새 세션에서 취득합니다.' : '계약의 후보 고정 시각 이후 취득한 새 검증 3쌍을 등록하세요. 동일 개발 촬영을 재사용하지 않습니다.';
    const devErrors=readiness(state,'development'),valErrors=readiness(state,'validation'), errors=role==='development'?devErrors:valErrors;
    if (!state.workflow && !doc.getElementById('camera-model-select')?.value) errors.push('실측 카메라와 대응 DB 수치 모델을 선택·고정하세요.');
    $('create').disabled = locked || devErrors.length > 0 || !doc.getElementById('camera-model-select')?.value;
    $('validate').disabled = locked || valErrors.length > 0;
    $('readiness').innerHTML = errors.length ? '<ul class="readiness-list">'+errors.map(error=>'<li>'+esc(error)+'</li>').join('')+'</ul>' : '<b>촬영 선택과 ROI 검토가 준비되었습니다.</b><p>서버가 원본 hash·조건·역할·시간 순서와 112개 전체 범위를 다시 확인합니다.</p>';
    for (const id of ['source-run','source-candidate','source-condition','rois','roi-file','rois-apply','chart-profile']) $(id).disabled = locked || !!state.workflow;
    $('review-fields').disabled = locked || !previewReady || !state.rois.length || running;
    for (const id of ['refresh','load','captures-refresh','template-download','template-fill']) $(id).disabled = locked;
    $('resume').hidden = !state.workflow?.resume_required; $('resume').disabled = locked;
    $('iq-settings').hidden=!state.iq.enabled;$('iq-review-wrap').hidden=!state.iq.enabled;$('iq-srgb-wrap').hidden=state.iq.actual_isp_color_space!=='srgb';
    for(const id of ['iq-enabled','iq-reference','iq-color-space','iq-nominal-confirmed','iq-srgb-confirmed'])$(id).disabled=locked||!!state.workflow;
    $('iq-noise-ids').disabled=locked||running||validationLocked;
    try{comparisonRequest($('iq-mode-a').value.trim(),$('iq-mode-b').value.trim());$('iq-compare').disabled=locked;}catch(_){$('iq-compare').disabled=true;}
    $('roi-count').textContent = 'edge '+state.rois.filter(row=>row.kind==='edge').length+' / 112 · 촬영별 확인 '+state.reviewed.size+'건';
    $('step-setup').className = !state.workflow?'current':'done';
    $('step-fit').className = state.workflow&&!candidateFrozen(state.workflow)?'current':candidateFrozen(state.workflow)?'done':'';
    $('step-validation').className = candidateFrozen(state.workflow)&&!resultLinks(state.workflow).some(link=>link.label.startsWith('통합'))?'current':'';
    $('step-report').className = resultLinks(state.workflow).some(link=>link.label.startsWith('통합'))?'current':'';
  }
  function render() { updateLinks(); renderCaptures(); renderContract(); renderResult(); renderControls(); }
  async function refreshCaptures() { state.captures=captureList(await api('/captures')); renderCaptures(); }
  function adoptWorkflow(value) {
    const workflow=value?.workflow||value;
    if (!identity(workflow?.id)) throw Error('응답에서 workflow ID를 확인할 수 없습니다.');
    const wasFrozen=candidateFrozen(state.workflow); state.workflow=workflow;
    const specification=workflow.specification||workflow.request||workflow, source=workflow.source||specification.source;
    if(source){state.source=source;for(const[key,id]of[['run_id','source-run'],['candidate_id','source-candidate'],['condition_id','source-condition']])$(id).value=source[key]||'';}
    const dev=frozenIDs(workflow,'development_ids'), val=frozenIDs(workflow,'validation_ids');
    if(dev.length)state.development=new Set(dev); if(val.length)state.validation=new Set(val);
    const rois=workflow.rois||specification.rois; if(rois){state.rois=normalizeRois(rois);$('rois').value=JSON.stringify(state.rois,null,2);}
    const chart=workflow.chart_profile||specification.chart_profile; if(chart){state.chartProfile=chart;$('chart-profile').value=JSON.stringify(chart,null,2);}
    const review=workflow.measured_review||specification.measured_review;
    if(review){if(review.operator_id){state.operator=review.operator_id;$('operator').value=state.operator;}if(review.physical_chart_id){state.physicalChart=review.physical_chart_id;$('chart-id').value=state.physicalChart;}for(const item of list(review.capture_reviews))if(item.all_112_edges_reviewed&&item.registration_suitable&&item.fixed_camera_chart&&item.no_clipping_or_occlusion)state.reviewed.add(item.capture_id);}
    const iq=workflow.iq||specification.iq||workflow.iq_frozen?.settings;
    if(iq?.enabled){state.iq={...iq,nominal_confirmed:true,srgb_confirmed:iq.actual_isp_color_space==='srgb'};$('iq-enabled').checked=true;$('iq-reference').value=iq.reference_id;$('iq-color-space').value=iq.actual_isp_color_space;$('iq-nominal-confirmed').checked=true;$('iq-srgb-confirmed').checked=state.iq.srgb_confirmed;for(const id of list(iq.patch_roi_reviewed_capture_ids))state.iqReviewed.add(id);}else{state.iq.enabled=false;$('iq-enabled').checked=false;}
    $('workflow-id').value=workflow.id;
    if (typeof global.CustomEvent === 'function') global.dispatchEvent(new CustomEvent('esfr-camera-workflow', {detail:workflow}));
    global.history.replaceState({},'',sourceURL(state.source,'/esfr-measured.html',workflow.id));
    $('progress').textContent=stageLabel(workflow)+' · '+workflow.id+(workflow.current_job?.id?' · 계산 '+workflow.current_job.id:'');
    if(!wasFrozen&&candidateFrozen(workflow)){fillMetadata();state.noiseText='';$('iq-noise-ids').value='';}
    render();
  }
  async function pollWorkflow(id, ticket=epoch) {
    clearTimeout(timer);timer=null;
    try { const value=await api('/measured/workflows/'+encodeURIComponent(id));if(ticket!==epoch)return;const workflow=value?.workflow||value;if(workflow.id!==id)throw Error('요청과 응답의 workflow ID가 다릅니다.');adoptWorkflow(workflow);if(workflowRunning(workflow))timer=setTimeout(()=>pollWorkflow(id,ticket),2500); }
    catch(error){if(ticket===epoch)notice('작업 조회: '+error.message+' · 상태 새로고침으로 재시도하세요.',true);}
  }
  async function getReview(role) {
    const payload=reviewPayload(state,role), result=await api('/measured/review-contract',payload);
    const review=result.measured_review||result;
    if(!review.roi_sha256||!Array.isArray(review.capture_reviews))throw Error('서버의 ROI 확인 계약을 읽을 수 없습니다.');
    return role==='validation'?mergeValidationReview(state.workflow,review,[...state.validation]):review;
  }
  $('upload-form').addEventListener('submit',event=>{event.preventDefault();busy(async()=>{
    const metadata=parseJSON($('metadata').value,'촬영 metadata'),issues=metadataIssues(metadata,roleFor(state.workflow));
    if(issues.length)throw Error(issues.join(' ')); if(!$('metadata-confirmed').checked)throw Error('실제 카메라 설정과 RAW·ISP 동일 노출 확인란을 선택하세요.');
    const raw=$('raw-file').files[0],isp=$('isp-file').files[0];if(!raw||!isp)throw Error('RAW와 ISP 원본을 모두 선택하세요.');
    const body=new FormData();body.append('metadata',JSON.stringify(metadata));body.append('raw',raw);body.append('isp',isp);
    const capture=await api('/captures',body);await refreshCaptures();state[metadata.role].add(capture.id);state.preview=capture.id;
    $('raw-file').value='';$('isp-file').value='';$('metadata-confirmed').checked=false;
    metadata.acquisition_id=null;metadata.captured_at=null;if(metadata.frame_pair){metadata.frame_pair.pair_id=null;metadata.frame_pair.raw_frame_id=null;metadata.frame_pair.isp_frame_id=null;}$('metadata').value=JSON.stringify(metadata,null,2);
    renderPreview();notice('원본 RAW·ISP 쌍을 저장했습니다. 이 촬영의 112개 ROI 위치를 확인하세요.');
  });});
  $('captures').addEventListener('change',event=>{const id=event.target.dataset.captureSelect;if(!id)return;const capture=state.captures.find(c=>c.id===id);if(!measuredCapture(capture))return;event.target.checked?state[capture.metadata.role].add(id):state[capture.metadata.role].delete(id);render();});
  $('captures').addEventListener('click',event=>{const id=event.target.closest('[data-preview]')?.dataset.preview;if(!id)return;state.preview=id;renderCaptures();renderPreview();$('preview').scrollIntoView({block:'center',behavior:'smooth'});});
  $('preview-capture').addEventListener('change',()=>{state.preview=$('preview-capture').value;renderPreview();});
  $('review-save').addEventListener('click',()=>busy(async()=>{
    if(!previewReady||!state.preview)throw Error('확인할 촬영 미리보기를 먼저 불러오세요.');
    normalizeRois(state.rois,captureShape(state.captures.find(c=>c.id===state.preview)));
    if(!['review-edges','review-fixed','review-quality'].every(id=>$(id).checked))throw Error('112개 ROI, 고정 위치, 촬영 품질의 세 확인란을 직접 확인하세요.');
    if(state.iq.enabled){const counts=iqPatchCounts(state.rois);if(counts.color!==18||counts.gray!==20||!$('review-patches').checked)throw Error('IQ는 색 18개·계조 20개 ROI 모두를 이 촬영에서 확인해야 합니다.');state.iqReviewed.add(state.preview);}
    state.reviewed.add(state.preview);notice('이 촬영의 ROI 확인을 기록했습니다. 실행 시 서버가 원본·ROI hash를 고정합니다.');
  }));
  function applyRois(){state.rois=normalizeRois(parseJSON($('rois').value,'ROI 목록',true));state.chartProfile=parseJSON($('chart-profile').value,'차트 프로파일');resetReviews();renderPreview();render();notice('ROI를 적용했습니다. 선택한 촬영마다 112개 위치를 다시 확인하세요.');}
  $('rois-apply').addEventListener('click',()=>busy(async()=>applyRois()));
  $('roi-file').addEventListener('change',()=>busy(async()=>{const file=$('roi-file').files[0];if(!file)return;const value=JSON.parse(await file.text()),rois=normalizeRois(value);$('rois').value=JSON.stringify(rois,null,2);if(value.chart_profile)$('chart-profile').value=JSON.stringify(value.chart_profile,null,2);applyRois();}));
  for(const id of ['rois','chart-profile'])$(id).addEventListener('input',()=>{state.rois=[];resetReviews();renderControls();});
  for(const id of ['source-run','source-candidate','source-condition','operator','chart-id'])$(id).addEventListener('input',()=>{syncInputs();updateLinks();renderControls();});
  for(const id of ['iq-enabled','iq-reference','iq-color-space','iq-nominal-confirmed','iq-srgb-confirmed','iq-noise-ids'])$(id).addEventListener(id==='iq-noise-ids'?'input':'change',()=>{const wasEnabled=state.iq.enabled;syncInputs();if(wasEnabled!==state.iq.enabled)state.iqReviewed.clear();render();});
  for(const id of ['iq-mode-a','iq-mode-b'])$(id).addEventListener('input',renderControls);
  $('metadata').addEventListener('input',()=>{$('metadata-confirmed').checked=false;});
  $('template-fill').addEventListener('click',()=>{fillMetadata();notice('현재 단계의 빈 양식을 불러왔습니다. null 필드는 실제 장비 기록으로 채우세요.');});
  $('template-download').addEventListener('click',()=>download('esfr-'+roleFor(state.workflow)+'-metadata-template.json',metadataForRole(state.template,roleFor(state.workflow),state.workflow)));
  $('captures-refresh').addEventListener('click',()=>busy(refreshCaptures));
  $('create').addEventListener('click',()=>busy(async()=>{
    syncInputs();const review=await getReview('development');
    const chart=parseJSON($('chart-profile').value,'차트 프로파일');
    if(chart.physical_chart_id&&chart.physical_chart_id!==state.physicalChart)throw Error('차트 프로파일과 검토한 실물 차트 개체 ID가 다릅니다.');
    chart.physical_chart_id=state.physicalChart;$('chart-profile').value=JSON.stringify(chart,null,2);
    const body={source:sourceValue(state.source),development_ids:[...state.development],rois:copy(state.rois),chart_profile:chart,roi_reviewed_capture_ids:[...state.development],measured_review:review,...iqRequest(state,'development')};
    body.camera_model_id=doc.getElementById('camera-model-select')?.value;if(!body.camera_model_id)throw Error('실측 카메라와 대응 DB 모델을 먼저 선택·고정하세요.');
    const value=await api('/measured/model-workflows',body);adoptWorkflow(value);epoch++;await pollWorkflow(state.workflow.id,epoch);notice('개발 영상으로 측정·보정을 시작했습니다. 후보 고정 완료 뒤 새 검증 촬영 단계가 열립니다.');
  }));
  $('validate').addEventListener('click',()=>busy(async()=>{
    syncInputs();const review=await getReview('validation'),id=state.workflow.id;
    const value=await api('/measured/workflows/'+encodeURIComponent(id)+'/validate',{validation_ids:[...state.validation],roi_reviewed_capture_ids:[...state.validation],measured_review:review,...iqRequest(state,'validation')});
    adoptWorkflow(value);epoch++;await pollWorkflow(id,epoch);notice('새 검증을 고정 후보에 제출했습니다. 재피팅 없이 112개 전체 비교·보고서를 생성합니다.');
  }));
  $('load').addEventListener('click',()=>busy(async()=>{const id=$('workflow-id').value.trim();if(!identity(id))throw Error('workflow ID를 확인하세요.');epoch++;clearTimeout(timer);state.workflow=null;state.reviewed.clear();state.development.clear();state.validation.clear();await refreshCaptures();await pollWorkflow(id,epoch);if(state.workflow)renderPreview();}));
  $('refresh').addEventListener('click',()=>busy(async()=>{await refreshCaptures();if(state.workflow){epoch++;await pollWorkflow(state.workflow.id,epoch);}}));
  $('resume').addEventListener('click',()=>busy(async()=>{if(!state.workflow?.resume_required)throw Error('재개가 필요한 작업이 아닙니다.');const result=await api('/measured/workflows/'+encodeURIComponent(state.workflow.id)+'/resume',{});adoptWorkflow(result);epoch++;await pollWorkflow(state.workflow.id,epoch);}));
  $('iq-compare').addEventListener('click',()=>busy(async()=>{const body=comparisonRequest($('iq-mode-a').value.trim(),$('iq-mode-b').value.trim()),result=await api('/measured/iq-comparisons',body),url=safeURL(result.report_url||result.links?.report||result.links?.report_url);if(!url)throw Error('두 모드 비교 보고서 주소를 확인할 수 없습니다.');$('iq-comparison-result').innerHTML='<p>각 모드의 고정 조건을 유지한 보고서 비교입니다.</p><a class="button" href="'+esc(url)+'" target="_blank" rel="noopener">모드 A/B 비교 보고서 ↗</a>';}));
  global.addEventListener('esfr-camera-selection',renderControls);
  global.addEventListener('beforeunload',()=>{epoch++;previewEpoch++;clearTimeout(timer);});
  async function init(){
    for(const[key,id]of[['run_id','source-run'],['candidate_id','source-candidate'],['condition_id','source-condition']])$(id).value=state.source[key]||'';
    updateLinks();renderControls();
    const results=await Promise.allSettled([api('/measured/template'),refreshCaptures()]);
    if(results[0].status==='fulfilled'){state.template=results[0].value;state.chartProfile=copy(state.template.chart_profile||{});$('chart-profile').value=JSON.stringify(state.chartProfile,null,2);fillMetadata();state.ready=true;const ref=state.template.iq?.reference||state.template.iq?.nominal_reference||{};$('iq-reference-info').textContent='명목 reference '+(ref.id||IQ_REFERENCE)+' · SHA256 '+(ref.sha256||ref.content_hash||'서버에서 실행 시 고정')+' · 실물 인쇄물의 실측 기준값 아님';}
    for(const result of results)if(result.status==='rejected')notice(result.reason.message,true);
    const id=new URL(global.location.href).searchParams.get('workflow');if(id){if(!identity(id))notice('URL의 workflow ID가 유효하지 않습니다.',true);else{epoch++;await pollWorkflow(id,epoch);}}
    render();
  }
  init();
})(typeof window === 'undefined' ? globalThis : window);

/* Explicit device/model selection; no capture metadata or old records mutated. */
(() => {
  'use strict';
  if (typeof document === 'undefined' || !document.getElementById('camera-model-panel')) return;
  const $ = id => document.getElementById('camera-model-'+id);
  const measured = id => document.getElementById('measured-'+id);
  const api = window.CameraESFRMeasured.createAPI(window.fetch.bind(window));
  const root = '/camera-models';
  let models = [], sources = [], frozen = false, currentWorkflow = null;
  const status = text => {$('status').textContent=text;};
  const signal = () => window.dispatchEvent(new CustomEvent('esfr-camera-selection'));
  function source() {const s={run_id:measured('source-run').value.trim(),candidate_id:measured('source-candidate').value.trim()};const c=measured('source-condition').value.trim();if(c)s.condition_id=c;return s;}
  function same(a,b){return ['run_id','candidate_id','condition_id'].every(k=>(a?.[k]||'')===(b?.[k]||''));}
  function setSource(s){for(const[k,id]of[['run_id','source-run'],['candidate_id','source-candidate'],['condition_id','source-condition']]){measured(id).value=s[k]||'';measured(id).dispatchEvent(new Event('input',{bubbles:true}));}}
  function add(select,value,label){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}
  function chooseModel(){const row=models.find(r=>r.id===$('select').value);if(!row){status('카메라 모델을 선택·고정해야 새 실측 보정을 시작할 수 있습니다.');signal();return;}setSource(row.source);$('select').value=row.id;for(const k of ['manufacturer','model','serial','lens','sensor'])$(k).value=row.camera[k];$('capture-context').textContent=row.camera.camera_id+' / '+row.camera.mode;$('spatial').value=row.spatial.mode;$('mapping-wrap').hidden=row.spatial.mode!=='explicit_roi_assignment';$('mapping').value=row.spatial.roi_fields?JSON.stringify(row.spatial.roi_fields,null,2):'';if(row.spatial.source_field_id){if(!Array.from($('field').options).some(o=>o.value===row.spatial.source_field_id))add($('field'),row.spatial.source_field_id,row.spatial.source_field_id);$('field').value=row.spatial.source_field_id;}$('ack').checked=true;status(row.camera.manufacturer+' / '+row.camera.model+' · '+row.camera.serial+' · '+row.camera.mode+' · '+row.id+' · SHA256 '+row.content_hash+' · 시야 배치 '+row.spatial.mode);signal();}
  async function loadFields(){const s=source();if(!s.run_id||!s.candidate_id)throw Error('완료된 DB 실행과 후보를 먼저 선택하세요.');const q=new URLSearchParams(s);const d=await api(root+'/source-fields?'+q);$('field').replaceChildren();for(const f of d.fields)add($('field'),f.id,f.id+' · '+JSON.stringify(f.image_xy_mm||f.field_xy||'sensor 위치 미확인'));$('source-info').textContent='선택 DB: '+d.lens.label+' × '+d.sensor.label+' · 광학 '+d.optical_profile+(d.optical_profile==='geometric_retrace'?' (회절 미포함)':'')+' · 필수 조건 '+JSON.stringify(d.requirements)+' · 정확한 physical sensor field 대응은 자동 추정하지 않습니다.';}
  async function refresh(){const old=$('select').value;const [m,s]=await Promise.all([api(root),api(root+'/sources')]);models=m.models;sources=s.sources;$('select').replaceChildren();add($('select'),'','새 모델 등록 / 선택 필요');for(const row of models)add($('select'),row.id,row.camera.model+' / '+row.camera.serial+' / '+row.camera.mode+' · '+row.id.slice(-8));$('select').value=old;$('source').replaceChildren();add($('source'),'','완료된 DB 렌즈·센서 후보 선택');sources.forEach((row,i)=>add($('source'),String(i),row.label));const index=sources.findIndex(r=>same(r.source,source()));$('source').value=index<0?'':String(index);if(currentWorkflow)adopt(currentWorkflow);signal();}
  async function guarded(fn){try{await fn();}catch(e){status('모델 연결 확인: '+e.message);}}
  function adopt(flow){currentWorkflow=flow;frozen=true;$('fields').disabled=true;$('select').disabled=true;$('refresh').disabled=true;if(flow.camera_model_id){if(!Array.from($('select').options).some(o=>o.value===flow.camera_model_id))add($('select'),flow.camera_model_id,flow.camera_model_id);$('select').value=flow.camera_model_id;status('이 작업의 고정 모델: '+flow.camera_model_id+' · SHA256 '+flow.camera_model_sha256+' · '+JSON.stringify(flow.camera_model_scope));}else{$('select').value='';status('이전 작업: DB 설계 출처만 연결됨. 선택 카메라 모델의 직접 예측 비교는 새 작업에서 시작하세요.');}signal();}
  $('select').addEventListener('change',()=>{if(!frozen)chooseModel();});
  $('source').addEventListener('change',()=>guarded(async()=>{if(frozen)return;const row=sources[Number($('source').value)];if(!row||$('source').value==='')return;setSource(row.source);$('select').value='';$('ack').checked=false;signal();await loadFields();}));
  $('source-load').addEventListener('click',()=>guarded(loadFields));
  $('refresh').addEventListener('click',()=>guarded(refresh));
  $('spatial').addEventListener('change',()=>{$('mapping-wrap').hidden=$('spatial').value!=='explicit_roi_assignment';$('ack').checked=false;});
  $('register').addEventListener('click',()=>guarded(async()=>{
    if(frozen)return;const ids=Array.from(document.querySelectorAll('[data-capture-select]:checked')).map(n=>n.dataset.captureSelect);if(!ids.length)throw Error('아래에서 개발 RAW·ISP 촬영을 먼저 등록하고 선택하세요.');
    const data=await api('/captures');const captures=Array.isArray(data)?data:data.captures;const capture=captures.find(c=>ids.includes(c.id)&&c.metadata.role==='development');if(!capture)throw Error('선택한 개발 촬영을 찾을 수 없습니다.');
    const meta=capture.metadata;const camera={camera_id:meta.camera_id,mode:meta.conditions.mode_id};for(const k of ['manufacturer','model','serial','lens','sensor'])camera[k]=$(k).value.trim();
    $('capture-context').textContent=meta.camera_id+' / '+meta.conditions.mode_id;
    const spatial={mode:$('spatial').value,acknowledged:$('ack').checked};if(spatial.mode==='explicit_roi_assignment')spatial.roi_fields=JSON.parse($('mapping').value);else spatial.source_field_id=$('field').value;
    const row=await api(root,{source:source(),development_capture_id:capture.id,camera,spatial});await refresh();$('select').value=row.id;chooseModel();
  }));
  for(const id of ['source-run','source-candidate','source-condition'])measured(id).addEventListener('input',()=>{if(frozen)return;const row=models.find(r=>r.id===$('select').value);if(row&&!same(row.source,source())){$('select').value='';status('DB 선택이 달라졌습니다. 대응 카메라 모델을 다시 선택하세요.');signal();}});
  function invalidate(){if(frozen||!$('select').value)return;$('select').value='';status('장비 정보 또는 PSF 배치가 변경됐습니다. 새 카메라 모델을 고정한 뒤 진행하세요.');signal();}
  for(const id of ['manufacturer','model','serial','lens','sensor','mapping'])$(id).addEventListener('input',invalidate);
  for(const id of ['spatial','field','ack'])$(id).addEventListener('change',invalidate);
  window.addEventListener('esfr-camera-workflow',e=>adopt(e.detail));
  guarded(refresh);
})();

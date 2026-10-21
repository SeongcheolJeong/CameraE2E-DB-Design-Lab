'use strict';

// Pure draft/scope helpers; the browser and Node boundary tests use the same code.
function CameraCaptureDrafts() {
  const drafts=new Map();
  const slot=id=>{if(!drafts.has(id))drafts.set(id,{text:'',epoch:0,profile:null});return drafts.get(id);};
  return {
    get:id=>slot(id).text,
    set(id,text){const value=slot(id);value.text=text;value.epoch++;},
    snapshot(id){return {id,epoch:slot(id).epoch};},
    accept(token,document,currentAssembly){const value=slot(token.id);if(currentAssembly!==token.id||value.epoch!==token.epoch)return false;value.text=JSON.stringify(document,null,2);value.profile=document.payload?.hdr_merge||value.profile;value.epoch++;return true;},
    parse(id){const doc=JSON.parse(slot(id).text);if(!doc||doc.assembly_id!==id||!['camera_capture_program_v1','camera_dcg_capture_program_v1','camera_reference_hdr_capture_program_v1','camera_capture_program_v2','camera_dcg_capture_program_v2','camera_reference_hdr_capture_program_v2','camera_retained_charge_capture_program_v1','camera_retained_charge_capture_program_v2','camera_retained_charge_capture_program_v3','camera_retained_charge_capture_program_v4','camera_retained_charge_capture_program_v5','camera_electrical_control_capture_program_v1','camera_electrical_control_capture_program_v2','camera_electrical_control_capture_program_v3'].includes(doc.payload?.schema))throw Error('프로그램의 고정 카메라 구성과 현재 선택이 다릅니다.');return doc;},
    update(id,path,value){const doc=this.parse(id);const keys=path.split('.');let parent=doc;for(const key of keys.slice(0,-1)){if(!Object.hasOwn(parent,key))throw Error('원본 계약에 없는 입력입니다.');parent=parent[key];}const last=keys.at(-1);if(!Object.hasOwn(parent,last))throw Error('원본 계약에 없는 입력입니다.');parent[last]=value;this.set(id,JSON.stringify(doc,null,2));},
    profile(id){return slot(id).profile;},
    rememberProfile(id,profile){if(profile)slot(id).profile=structuredClone(profile);}
  };
}
function CameraNonlinearParameterEdit(document,path) {
  if(!path.startsWith('payload.electrical_model.')||!['camera_capture_program_v2','camera_dcg_capture_program_v2','camera_reference_hdr_capture_program_v2'].includes(document.payload?.schema))return document;
  const doc=structuredClone(document),model=doc.payload.electrical_model;
  const source={kind:'estimated',description:'Authored nonlinear Workbench parameter edits. Parent records retain original evidence; edited values are not measured calibration.'};
  Object.assign(doc,{source_kind:'estimated',physical_instance:null,source_uri:'fixture://workbench/nonlinear-authored',method_and_version:'workbench_nonlinear_parameter_edit:1'});
  model.source=structuredClone(source);if(model.read_noise)model.read_noise.source=structuredClone(source);
  for(const curve of [model.charge_voltage_response,...(model.paths||[]).map(p=>p.charge_voltage_response)].filter(Boolean))curve.source=structuredClone(source);
  return doc;
}
function CameraRetainedParameterEdit(document,path) {
  if(!['camera_retained_charge_capture_program_v1','camera_retained_charge_capture_program_v2','camera_retained_charge_capture_program_v3','camera_retained_charge_capture_program_v4','camera_retained_charge_capture_program_v5','camera_electrical_control_capture_program_v1','camera_electrical_control_capture_program_v2','camera_electrical_control_capture_program_v3'].includes(document.payload?.schema)||!path.startsWith('payload.'))return document;
  const doc=structuredClone(document);
  Object.assign(doc,{source_kind:'estimated',physical_instance:null,source_uri:'fixture://workbench/retained-charge-authored',method_and_version:'workbench_retained_charge_parameter_edit:1'});
  if(path.startsWith('payload.electrical_model.')) {
    const model=doc.payload.electrical_model;
    const source={kind:'estimated',description:'Authored retained-charge readout edits; parent records preserve original calibration.'};
    if(model.source)model.source=structuredClone(source);
    if(model.read_noise?.source)model.read_noise.source=structuredClone(source);
    for(const curve of [model.charge_voltage_response,...(model.paths||[]).map(p=>p.charge_voltage_response)].filter(Boolean))curve.source=structuredClone(source);
  }
  return doc;
}
function CameraRunReplayUnsupportedReason(summary,plugin) {
  if(['retained_charge_v1','electrical_control_v1'].includes(plugin))return '잔류 전하 RAW는 이전 프레임 전하가 섞인 전달 신호입니다. 현재 노출의 광전하를 추정하는 기존 ISP 재처리는 지원하지 않습니다.';
  if(plugin==='shared_charge_dcg_v1'&&!Object.values(summary?.raw_formats||{}).some(f=>f?.response_type==='nonlinear_charge_voltage'))return 'DCG 재처리는 읽기 경로별 추출·보정 계약이 필요하며, 현재 이 메뉴에서 지원하지 않습니다.';
  return '';
}
function CameraCaptureRuntime(schema) {
  return {camera_capture_program_v1:'hdr_bracket_v1',camera_dcg_capture_program_v1:'shared_charge_dcg_v1',camera_reference_hdr_capture_program_v1:'reference_hdr_v1',camera_capture_program_v2:'hdr_bracket_v1',camera_dcg_capture_program_v2:'shared_charge_dcg_v1',camera_reference_hdr_capture_program_v2:'reference_hdr_v1',camera_retained_charge_capture_program_v1:'retained_charge_v1',camera_retained_charge_capture_program_v2:'retained_charge_v1',camera_retained_charge_capture_program_v3:'retained_charge_v1',camera_retained_charge_capture_program_v4:'retained_charge_v1',camera_retained_charge_capture_program_v5:'retained_charge_v1',camera_electrical_control_capture_program_v1:'electrical_control_v1',camera_electrical_control_capture_program_v2:'electrical_control_v1',camera_electrical_control_capture_program_v3:'electrical_control_v1'}[schema] || '';
}
function CameraCaptureSchema(runtime) {
  return {hdr_bracket_v1:'camera_capture_program_v1',shared_charge_dcg_v1:'camera_dcg_capture_program_v1',reference_hdr_v1:'camera_reference_hdr_capture_program_v1',retained_charge_v1:'camera_retained_charge_capture_program_v1',electrical_control_v1:'camera_electrical_control_capture_program_v1'}[runtime] || '';
}
function CameraCaptureTemplateMode(schema) {
  return {camera_dcg_capture_program_v1:'&mode=shared_charge',camera_reference_hdr_capture_program_v1:'&mode=reference_guarded',camera_capture_program_v2:'&mode=independent_bracket_nonlinear',camera_dcg_capture_program_v2:'&mode=shared_charge_nonlinear',camera_reference_hdr_capture_program_v2:'&mode=reference_guarded_nonlinear',camera_retained_charge_capture_program_v1:'&mode=retained_charge',camera_retained_charge_capture_program_v2:'&mode=trapped_charge',camera_retained_charge_capture_program_v3:'&mode=retained_gain_merge',camera_retained_charge_capture_program_v4:'&mode=trapped_gain_merge',camera_retained_charge_capture_program_v5:'&mode=retained_stateful_hdr',camera_electrical_control_capture_program_v1:'&mode=electrical_control'}[schema] || '';
}
function CameraElectricalControlTemplateQuery(mode,controlId) {
  const selected=mode||'electrical_control';
  if(!['electrical_control','electrical_control_hcg','electrical_control_lcg','electrical_control_hcg_nonlinear','electrical_control_lcg_nonlinear','electrical_control_trapped','electrical_control_trapped_pwm_period','electrical_control_trapped_nonlinear','electrical_control_trapped_hcg','electrical_control_trapped_lcg','electrical_control_trapped_hcg_nonlinear','electrical_control_trapped_lcg_nonlinear'].includes(selected))throw Error('지원하지 않는 고정 관측 경로 초안입니다.');
  if(!controlId)throw Error('전기 노출 제어에 연결할 적용 설정을 선택하세요.');
  return '&mode='+encodeURIComponent(selected)+'&control_sequence_id='+encodeURIComponent(controlId);
}
function CameraCaptureScope(state) {
  return [state.runtime,state.assembly,state.sceneSequence,state.controlSequence,['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(state.runtime)?state.captureProgram:'',state.test||''].join('|');
}
function CameraCaptureMatches(run,state) {
  return !['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(state.runtime)||(Boolean(state.captureProgram)&&run.capture_program_id===state.captureProgram);
}
function CameraCaptureTestMatches(test,state) {
  const stimulus=test?.stimulus;
  return !['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(state.runtime)||Boolean(state.captureProgram&&[({shared_charge_dcg_v1:'shared_charge_dcg_test_v1',reference_hdr_v1:'reference_hdr_test_v1',hdr_bracket_v1:'electrical_hdr_test_v1',retained_charge_v1:'retained_charge_test_v1',electrical_control_v1:'electrical_control_test_v1'}[state.runtime]),({shared_charge_dcg_v1:'shared_charge_dcg_test_v2',reference_hdr_v1:'reference_hdr_test_v2',hdr_bracket_v1:'electrical_hdr_test_v2',retained_charge_v1:'retained_charge_components_test_v1'}[state.runtime])].filter(Boolean).includes(stimulus?.schema)&&stimulus.capture_program_id===state.captureProgram&&stimulus.scene_sequence_id===state.sceneSequence&&stimulus.control_sequence_id===state.controlSequence);
}
function CameraDesignExperimentAvailability(response) {
  const schema=response?.specification?.schema;
  const catalog=response?.catalog;
  const available=schema==='camera_design_experiment_v1'||(schema==='camera_design_experiment_v2'&&response?.project_mode?.mode==='r1_engineering'&&Array.isArray(catalog?.models)&&catalog.models.length>0&&Array.isArray(catalog?.scenes)&&catalog.scenes.length>0);
  return {available,schema};
}

function CameraRetainedChargeUI(ctx) {
  const {state,esc,array,number,badge,details,options,records,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,frameBrowser}=ctx;
  const trapNote='Trap 모델은 PD의 자유 전하와 포획 전하를 구분하고 시간 순서대로 계산합니다. PD clear는 자유 전하만 제거합니다. 유한 시간 간격의 근사 계산이며 실물 trap·온도 반응의 검증은 미확립입니다.';
  const scopeNote='전하 이동 뒤 남은 PD 전하는 다음 프레임에 이어집니다. 노출 사이에도 암전류가 누적됩니다. PD clear는 잔류 전하를 제거하고 읽기 노드 reset은 이를 유지합니다. 고정 이동 효율을 사용하는 작성 모델이며 실측 검증은 미확립입니다.';
  function actions() {
    const controls=records('control_sequences').filter(c=>c.assembly_id===state.assembly);
    const valid=controls.some(c=>c.id===state.controlSequence);
    return `<section class="panel"><div class="panel-content"><h3>프레임 사이의 잔류 전하</h3><p>밝음→어두움 잔상, 전하 이동 효율, clear 조건을 비교합니다.</p><div class="form-field"><label for="retained-control-sequence">시간 장면과 연결된 적용 설정</label><select id="retained-control-sequence"><option value="">현재 카메라의 적용 설정 선택</option>${controls.map(c=>`<option value="${esc(c.id)}" ${c.id===state.controlSequence?'selected':''}>${esc(c.name)}</option>`).join('')}</select></div>${controls.length?'':'<p class="list-note">시간 장면과 적용 설정을 먼저 등록하세요.</p>'}<button class="button secondary" data-action="capture-retained-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>잔류 전하 프로그램 초안</button><button class="button secondary" data-action="capture-trapped-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>Trap 포획·방출 프로그램 초안</button><button class="button secondary" data-action="capture-retained-gain-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>잔류 전하 · HCG/LCG 결합 초안</button><button class="button secondary" data-action="capture-trapped-gain-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>Trap 이력 · HCG/LCG 결합 초안</button><button class="button secondary" data-action="capture-retained-hdr-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>잔류 PD · 인과적 다중 노출 HDR 초안</button><button class="button secondary" data-action="capture-electrical-control-template" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>단일 RAW 관측 · 노출 AE 초안</button>${[['electrical_control_hcg','선형 HCG'],['electrical_control_lcg','선형 LCG'],['electrical_control_hcg_nonlinear','비선형 HCG'],['electrical_control_lcg_nonlinear','비선형 LCG']].map(([mode,label])=>`<button class="button secondary" data-action="capture-electrical-control-template" data-electrical-mode="${mode}" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>${label} 고정 관측 · 노출 AE 초안</button>`).join('')}<details class="data-detail"><summary>Trap 상태를 유지하는 노출 AE</summary><p>포획·방출 이력과 실제 노출 시간을 함께 계산합니다. 관측 경로를 고정하고 새 프로그램을 작성하세요.</p><div class="form-row">${[['electrical_control_trapped','선형 single'],['electrical_control_trapped_nonlinear','비선형 single'],['electrical_control_trapped_hcg','선형 HCG'],['electrical_control_trapped_lcg','선형 LCG'],['electrical_control_trapped_hcg_nonlinear','비선형 HCG'],['electrical_control_trapped_lcg_nonlinear','비선형 LCG']].map(([mode,label])=>`<button class="button secondary" data-action="capture-electrical-control-template" data-electrical-mode="${mode}" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>Trap · ${label} AE 초안</button>`).join('')}<button class="button secondary" data-action="capture-electrical-control-template" data-electrical-mode="electrical_control_trapped_pwm_period" ${!state.assembly||!valid||state.captureLoading?'disabled':''}>Trap · PWM 정수 주기 AE 초안</button></div></details></div></section>`;
  }
  function editor(document,text,error) {
    const id=state.assembly,model=document.payload?.retained_charge_model;
    const field=(path,label,value,extra='')=>`<div class="form-field"><label for="retained-${esc(path)}">${esc(label)}</label><input id="retained-${esc(path)}" data-capture-field="${esc(path)}" value="${esc(value??'')}" ${extra} required></div>`;
    const efficiencies=array(model?.transfer_efficiency),trapped=model?.schema==='camera_trapped_photodiode_v1',gainMerge=Boolean(document.payload?.gain_merge_profile),photoHDR=Boolean(document.payload?.photo_hdr_profile);
    const trapFields=trapped?`<h3>Native 사이트별 포획·방출</h3>${[['trap_capacity_e','Trap 용량 · electron','min="1" step="1"'],['capture_rate_s','포획률 · 1/s','min="0" step="any"'],['release_time_s','방출 시간 상수 · s','min="0" step="any"']].map(([key,label,bounds])=>`<p>${esc(label)}</p><div class="form-row">${['R','Gr','Gb','B'].map((site,i)=>field('payload.retained_charge_model.'+key+'.'+i,site,model[key]?.[i],'type="number" '+bounds)).join('')}</div>`).join('')}${field('payload.retained_charge_model.integration.max_step_ns','최대 적분 간격 · ns',model.integration?.max_step_ns,'type="number" min="1" step="1"')}<p class="list-note">${trapNote} PWM 경계와 장면 변화 시각을 보존하며 계산 한도를 넘으면 실행을 거절합니다.</p>`:'';
    return `<section class="panel" id="capture-program-editor">
      <div class="panel-header"><div><span class="eyebrow">PHOTODIODE CHARGE · FRAME HISTORY</span><h2>잔류 전하와 불완전 이동</h2><p>전달된 전하와 남은 전하를 같은 전자 집단에서 나눕니다.</p></div></div>
      <div class="panel-content"><div class="form-field"><label for="capture-program-assembly">고정 카메라 구성</label><select id="capture-program-assembly">${options('assemblies',id)}</select></div>
      <div class="form-field"><label for="factory-capture-program">등록된 프로그램</label><select id="factory-capture-program"><option value="">고정 프로그램 선택</option>${matchingCapturePrograms().map(p=>`<option value="${esc(p.id)}" ${p.id===state.captureProgram?'selected':''}>${esc(p.name)} · r${esc(p.revision)}</option>`).join('')}</select></div>
      <div class="form-row"><button class="button secondary" data-action="capture-use" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전으로 실험</button><button class="button quiet" data-action="capture-copy" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전에서 새 초안</button></div>
      ${state.captureError||error?`<p class="callout error" role="alert">${esc(state.captureError||error)} · 입력 초안은 유지됩니다.</p>`:''}
      <form data-form="capture-program"><div class="form-row">${field('name','프로그램 이름',document.name)}${field('revision','새 버전',document.revision)}</div>
      <h3>Native RGGB 사이트별 이동 효율</h3><div class="form-row">${['R','Gr','Gb','B'].map((site,i)=>field('payload.retained_charge_model.transfer_efficiency.'+i,site,efficiencies[i],'type="number" min="0" max="1" step="any"')).join('')}</div>
      ${trapFields}
      <p class="list-note">저장 용량: ${esc(model?.capacity_e??'미지정')} electron. 현재 범위는 크롭 없는 전체 native 센서이며 초기 전하는 0입니다. clear할 프레임 ID·전기 읽기 모델·광원·출처는 아래 전체 설정에서 고정합니다.</p>
      <div class="callout neutral">${scopeNote}</div>
      <details class="data-detail" open><summary>Clear·시간 사건·전기 모델·출처 · 전체 설정</summary><div class="form-field"><label for="capture-program-json">검토 후 등록할 전체 원문</label><textarea id="capture-program-json" name="document" spellcheck="false" required>${esc(text)}</textarea></div></details>
      <button class="button primary full" type="submit" ${state.captureSaving?'disabled':''}>${state.captureSaving?'등록 중…':'새 프로그램 버전을 등록하고 선택'}</button></form>
      <p class="list-note">RAW는 전달된 총전하의 읽기 신호입니다. ${gainMerge?'HCG/LCG 결합은 같은 프레임의 전달 총전하 추정이며 이전 프레임 전하와 암전류를 제거하지 않습니다.':'gain 경로 결합은 선택하지 않았습니다.'} ${photoHDR?'정적 장면·광원·서로 다른 노출에서 RAW 기반 현재 노출 광전자율을 추정합니다. Trap·PWM·실측 보정과 물리 정확도는 지원하지 않습니다.':'현재 프레임 광전하 역산·다중 노출 HDR 합성·기존 보정 ISP 재처리는 이 계약에 포함되지 않습니다.'}</p></div></section>`;
  }
  function panel(run) {
    const summary=runSummary(run),stateful=summary.retained_charge||{},succeeded=run?.status==='succeeded';
    const ledgers=array(stateful.frame_ledger),trapped=stateful.model?.schema==='camera_trapped_photodiode_v1',gain=summary.gain_path_merge,photoHDR=summary.photo_hdr;
    const gainRows=array(gain?.frames).map(frame=>`<tr><th scope="row">${esc(frame.frame_id)}</th><td>${number(frame.valid_pixels,0)}</td><td>${number(frame.invalid_pixels,0)}</td><td>${number(frame.agreement_pixels,0)}</td></tr>`).join('');
    const ledgerKeys=['delivered_e','residual_e',...(trapped?['trapped_e','captured_e','released_e']:[]),'clear_loss_e','gap_overflow_e','exposure_overflow_e'];
    const rows=ledgers.map((event,index)=>`<tr><th scope="row">${esc(event.frame_id??index)}</th>${ledgerKeys.map(key=>`<td>${Number.isFinite(event.totals_e?.[key])?number(event.totals_e[key],3):'—'}</td>`).join('')}</tr>`).join('');
    return `<section class="panel" id="retained-charge-result"><div class="panel-header"><div><span class="eyebrow">DELIVERED CHARGE · CONSERVATION</span><h2>프레임 잔류와 전하 수지</h2><p>${esc(run?.capture_program_snapshot?.name||'잔류 전하 프로그램')}</p></div>${badge(run?.status||'pending')}</div><div class="panel-content">
      ${succeeded?`${frameBrowser(run)}${photoHDR?`<h3>잔류 전하를 고려한 다중 노출 광전자율</h3><dl class="frame-facts"><div><dt>유효 픽셀</dt><dd>${number(photoHDR.valid_pixels,0)}</dd></div><div><dt>무효 픽셀</dt><dd>${number(photoHDR.invalid_pixels,0)}</dd></div><div><dt>노출별 선택 픽셀</dt><dd>${esc(array(photoHDR.selected_frame_counts).join(' / '))}</dd></div></dl><p class="list-note">원본 RAW와 작성 전기·잔류 모델로 이전 PD 전하와 암전류를 순서대로 추정해 뺍니다. 정적 장면·일정 광원을 선언한 경우에만 실행하며 무효 상태는 clear 전까지 전파합니다. PRNU가 포함된 광전자율이며 실측 교정, trap 역산, 움직임 정합, 물리 HDR 성능을 뜻하지 않습니다.</p>`:''}${gainRows?`<h3>같은 프레임의 HCG/LCG 전달 전하 결합</h3><div class="table-wrap"><table aria-label="Gain 경로 결합 유효성"><thead><tr><th>프레임</th><th>유효 픽셀</th><th>무효 픽셀</th><th>경로 일치 픽셀</th></tr></thead><tbody>${gainRows}</tbody></table></div><p class="list-note">원본 HCG/LCG RAW와 작성 readout 모델로 전달 총전하를 추정합니다. 잔류·trap·암전류는 포함되며 현재 노출 광전하, 다중 노출 HDR, 물리 정확도를 뜻하지 않습니다.</p>`:''}${rows?`<div class="table-wrap"><table aria-label="프레임별 전하 수지"><thead><tr><th>프레임</th><th>전달</th><th>PD 잔류</th>${trapped?'<th>Trap 잔류</th><th>포획</th><th>방출</th>':''}<th>Clear 배출</th><th>간격 중 overflow</th><th>노출 중 overflow</th></tr></thead><tbody>${rows}</tbody></table></div><p class="list-note">electron · 전체 센서 합계. 시뮬레이션 내부 전하이며 RAW로 측정한 전하가 아닙니다.</p>`:''}${details('전하 상태·사건 순서·보존 근거',stateful)}${details('읽기 변환과 원본 실행',summary)}`:empty(run?.status==='failed'?'잔류 전하 계산 실패':'잔류 전하 계산 대기',run?.error||'프로그램·시간 장면·적용 설정·시험을 고정한 뒤 실행하세요.')}
      <div class="callout neutral">${trapped?trapNote:scopeNote} ${photoHDR?'원본 미리보기는 RAW 코드의 표시 영상입니다. 위 HDR 추정과 별도입니다.':'미리보기는 RAW 코드의 표시 영상입니다. 이전 프레임 기여를 제거하거나 현재 노출의 광전하를 복구하지 않습니다.'}</div></div></section>`;
  }
  return {actions,editor,panel,scopeNote};
}

function CameraReferenceHDRValid(document) {
  const payload=document?.payload,merge=payload?.hdr_merge;
  const translation=merge?.schema==='camera_reference_hdr_merge_global_translation_v1',reg=merge?.registration;
  const translationValid=!translation||Boolean([2,4,6,8].includes(reg?.max_shift_px)&&Number.isInteger(reg?.minimum_common_pixels)&&reg.minimum_common_pixels>=32&&reg.minimum_common_pixels<=1048576&&Number.isFinite(reg?.maximum_mean_clipped_chi2)&&reg.maximum_mean_clipped_chi2>0&&reg.maximum_mean_clipped_chi2<=25&&Number.isFinite(reg?.minimum_score_gap)&&reg.minimum_score_gap>0&&reg.minimum_score_gap<=25);
  return Boolean(['camera_reference_hdr_merge_v1','camera_reference_hdr_merge_global_translation_v1'].includes(merge?.schema)&&translationValid&&merge.base_profile?.schema==='camera_hdr_merge_v1'&&Number.isInteger(merge.reference_frame_index)&&merge.reference_frame_index>=0&&merge.reference_frame_index<=15&&payload.electrical_model&&(payload.waveform===null||payload.waveform?.kind==='constant'));
}
function CameraReferenceHDRUI(ctx) {
  const {state,esc,array,number,badge,details,options,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,imagePreviews,frameBrowser}=ctx;
  const scopeNote='같은 위치의 RAW가 일치한다고 판정한 영역만 합성합니다. 별도 전역 평행이동 프로파일은 global shutter의 짝수 픽셀 이동만 RAW로 추정합니다. 후보가 모호하거나 국소적으로 불일치하면 기준 노출로 돌아갑니다. 기준 노출이 무효인 픽셀은 unknown으로 남으며 완전한 잔상 제거·포화된 기준 노출 복구는 지원하지 않습니다. 감지되지 않는 움직임은 남을 수 있습니다.';
  function editor(document,text,error) {
    const id=state.assembly,payload=document.payload,merge=payload?.hdr_merge;
    const valid=CameraReferenceHDRValid(document);
    if(!valid)error='기준 프레임 0–15, 합성·정합 프로파일, 전기 모델, null 또는 constant 광원 설정을 확인하세요.';
    return `<section class="panel" id="capture-program-editor"><div class="panel-header"><div><span class="eyebrow">REFERENCE EXPOSURE · GUARDED FUSION</span><h2>기준 노출 보존 합성 프로그램</h2><p>기준 프레임을 지정하고 전체 JSON의 전기 모델·합성 조건·출처를 검토합니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="capture-program-assembly">고정 카메라 구성</label><select id="capture-program-assembly">${options('assemblies',id)}</select></div><div class="form-row"><button type="button" class="button secondary" data-action="capture-reference-template" ${!id||state.captureLoading?'disabled':''}>새 기준 노출 HDR 프로그램</button><button type="button" class="button secondary" data-action="capture-reference-translation-template" ${!id||state.captureLoading?'disabled':''}>새 RAW 전역 평행이동 HDR 프로그램</button><button type="button" class="button quiet" data-action="capture-template" ${!id||state.captureLoading?'disabled':''}>순차 HDR 기본 프로그램</button><button type="button" class="button quiet" data-action="capture-dcg-template" ${!id||state.captureLoading?'disabled':''}>새 DCG 프로그램</button><button type="button" class="button quiet" data-action="capture-copy" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전에서 새 초안</button></div><div class="form-field"><label for="factory-capture-program">등록된 프로그램</label><select id="factory-capture-program"><option value="">고정 프로그램 선택</option>${matchingCapturePrograms().map(p=>`<option value="${esc(p.id)}" ${p.id===state.captureProgram?'selected':''}>${esc(p.name)} · r${esc(p.revision)}</option>`).join('')}</select></div>${selectedCaptureProgram()?'<button type="button" class="button secondary" data-action="capture-use">이 버전으로 실험</button>':''}${state.captureError?`<div class="callout error" role="alert">${esc(state.captureError)}</div>`:''}${error?`<div class="callout error" role="alert">${esc(error)} · 입력 원문은 유지됩니다.</div>`:''}<form data-form="capture-program"><div class="form-row"><div class="form-field"><label for="capture-name">프로그램 이름</label><input id="capture-name" data-capture-field="name" value="${esc(document.name)}" required></div><div class="form-field"><label for="capture-revision">새 버전</label><input id="capture-revision" data-capture-field="revision" value="${esc(document.revision)}" required></div>${merge?`<div class="form-field"><label for="capture-payload-hdr_merge-reference_frame_index">기준 프레임 인덱스 <span>0부터 시작</span></label><input id="capture-payload-hdr_merge-reference_frame_index" data-capture-field="payload.hdr_merge.reference_frame_index" type="number" min="0" max="15" step="1" value="${esc(merge.reference_frame_index)}" required></div>`:''}</div><div class="callout neutral">${scopeNote} 프레임 인덱스는 연결할 적용 설정의 실제 프레임 수보다 작아야 합니다. 광원은 null 또는 constant만 지원하며 PWM 합성은 지원하지 않습니다. 전역 평행이동 프로파일은 global shutter 적용 설정이 필요합니다.</div><details class="data-detail" open><summary>전기 모델 · 합성 base_profile · 출처 · 전체 JSON</summary><div class="form-field"><label for="capture-program-json">검토 후 등록할 전체 원문</label><textarea id="capture-program-json" name="document" spellcheck="false" required>${esc(text)}</textarea></div></details><div class="inline-spacer"></div><button type="submit" class="button primary full" ${!valid||state.captureSaving?'disabled':''}>${state.captureSaving?'등록 중…':'새 프로그램 버전을 등록하고 선택'}</button></form><p class="list-note">실물 검증·제품 LFM 성능은 미확립입니다. 기존 순차 HDR과 별도 Runtime·시험으로 기록합니다.</p></div></section>`;
  }
  function panel(run) {
    const summary=runSummary(run),hdr=summary.hdr_merge||{},succeeded=run?.status==='succeeded',fused=succeeded&&summary.hdr_merge_applied===true;
    const count=(key,label)=>`<div><dt>${label}</dt><dd>${number(hdr[key],0)}</dd></div>`;
    return `<section class="panel" id="reference-hdr-result"><div class="panel-header"><div><span class="eyebrow">REFERENCE EXPOSURE · GUARDED FUSION</span><h2>기준 노출 보존 합성 결과</h2><p>${esc(run?.capture_program_snapshot?.name||run?.capture_program_id||'고정 캡처 프로그램을 선택하세요')}</p></div>${badge(run?.status||'pending')}</div><div class="panel-content">${fused?`<dl class="frame-facts"><div><dt>기준 프레임 · 0부터 시작</dt><dd>${number(hdr.reference_frame_index,0)} · ${esc(hdr.reference_frame_id||'ID 미기록')}</dd></div>${count('fused_pixels','일치 판정 · 합성 픽셀')}${count('reference_fallback_pixels','기준 노출로 돌아간 픽셀')}${count('reference_only_unknown_pixels','기준 노출만 유효 · 일관성 미확인')}${count('reference_invalid_pixels','기준 노출 무효 · unknown')}</dl>${imagePreviews(array(run.artifacts).filter(a=>a.name==='sensor-rgb.png'),{kind:'hdr',summary})}${array(summary.frames).length?frameBrowser(run):''}${details('기준 노출 · 합성 가중치 · 유효성과 변화 마스크',hdr)}${details('고정 캡처 프로그램 · 전체 결과',summary)}`:succeeded?'<p class="list-note">기준 노출 보존 합성 진단이 없습니다. 저장된 전체 결과를 확인하세요.</p>'+details('저장된 전체 결과',summary):empty(run?.status==='failed'?'기준 노출 HDR 계산 실패':'기준 노출 HDR 실행 대기',run?.error||'기준 노출 프로그램·시간 장면·적용 설정·별도 시험을 고정한 뒤 실행하세요.')}<div class="callout neutral">${scopeNote} 광원은 일정한 배율만 지원하며 PWM 합성은 지원하지 않습니다. 미리보기는 톤 처리 영상입니다. 원본 RAW·signed 선형 전자율·마스크·모델 추정 분산은 별도로 보존하며 실물 검증은 미확립입니다.</div></div></section>`;
  }
  return {editor,panel,scopeNote};
}

(() => {
  const BASE = '/api/v1';
  const MAX_JSON_BYTES = 1_000_000;
  const state = {data: null, view: 'overview', assembly: '', run: '', measurement: '', assetFilter: 'all', search: '', compatibility: null, validation: null, polling: false, toastTimer: null, uploadedMeasurement: null, exposure: '4', seed: '42', retainedNoiseEnabled: true, test: '', modelDraft: '', measurementDraft: '', validationTest: '', allowCalibrated: false, captureDraft: '', captureRaw: null, captureDark: null, lastCapture: null, calibrationBase: '', calibrationMeasurements: [], calibrationBounds: '', calibrationName: '', calibrationRevision: 'calibrated-1', calibrationReport: '', builderSensor: ''};
  Object.assign(state,{factoryPlugins:[],factoryError:'',factoryPlugin:'',factoryDrafts:{},factoryJob:'',factoryPolling:false,factoryModelName:'',factoryModelRevision:'1',factoryConditionsAssembly:'',factoryPublished:null,builderLens:''});
  Object.assign(state,{conditionAxis:'gain',conditionValues:'0.5, 1, 1.5',conditionReport:null,conditionError:''});
  Object.assign(state,{lfmPlanRun:'',lfmPlanDraft:'',lfmPlanReport:null,lfmPlanError:''});
  Object.assign(state,{runtime:'response_db_v1',scene:'',experimentMode:'camera',replaySources:[],replaySourcesError:'',replaySource:'',replayISP:'',replayName:'',replayJob:'',replayPolling:false,sceneDraft:'',sceneMetadata:'',sceneFile:null,sceneInputMode:'fixture',spatialTestDraft:'',factoryBaseLens:''});
  Object.assign(state,{sceneSequence:'',controlSequence:'',temporalTestDraft:'',temporalSceneMode:'fixture',temporalFixtureDraft:'',temporalSceneDraft:'',controlDraft:'',sequenceUploads:{},frameSelections:{},selectionInitialized:false,factoryConditionScope:'all_declared'});
  Object.assign(state,{captureProgram:'',captureDrafts:CameraCaptureDrafts(),captureError:'',captureLoading:false,captureSaving:false});
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const array = value => Array.isArray(value) ? value : [];
  const json = value => JSON.stringify(value, null, 2);
  const short = value => String(value ?? '').length > 24 ? String(value).slice(0, 20) + '…' : String(value ?? '');
  const number = (value, digits = 3) => {if(value===null || value===undefined || !Number.isFinite(Number(value)))return '—';const n=Number(value);return n!==0 && Math.abs(n)<10**(-digits) ? n.toExponential(Math.min(digits,4)) : n.toLocaleString('en-US',{maximumFractionDigits:digits});};
  const when = value => value ? new Date(value).toLocaleString('ko-KR', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const paths = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1.3"/><rect x="14" y="3" width="7" height="7" rx="1.3"/><rect x="3" y="14" width="7" height="7" rx="1.3"/><rect x="14" y="14" width="7" height="7" rx="1.3"/>',
    assets: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    builder: '<path d="M9 5H4v14h16V5h-5M9 3h6v4H9z"/><circle cx="12" cy="13" r="3"/>',
    experiments: '<path d="M9 3h6m-5 0v6l-6 10a1.3 1.3 0 0 0 1 2h14a1.3 1.3 0 0 0 1-2L14 9V3M7 15h10"/>',
    validation: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    factory: '<path d="M3 21V10l6 3V7l6 4V3h5v18H3Z"/><path d="M7 17h1m4 0h1m4 0h1"/>',
    releases: '<path d="m12 3 8 4v10l-8 4-8-4V7l8-4Z"/><path d="m4 7 8 5 8-5M12 12v9M8 5l8 5"/>',
    play: '<path d="m8 4 12 8-12 8V4Z"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    chart: '<path d="M4 3v17h17M7 14l4-5 4 3 5-8"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
    lens: '<ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M2 7h6M2 17h6M16 9l6 3-6 3M2 12h6"/>',
    sensor: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 4v16M15 4v16M4 9h16M4 15h16"/>',
    light: '<path d="M3 12h4M17 12h4M12 3v4M12 17v4M5 5l3 3m8 8 3 3M5 19l3-3m8-8 3-3"/><circle cx="12" cy="12" r="3"/>',
    process: '<path d="M4 7h16M4 17h16M8 3v8M16 13v8"/><circle cx="8" cy="7" r="2"/><circle cx="16" cy="17" r="2"/>'
  };
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.assets}</svg>`;
  const views = [{id:'overview', label:'사례 개요', english:'Overview'}, {id:'assets', label:'자산 레지스트리', english:'Assets'}, {id:'builder', label:'카메라 구성', english:'Camera Builder'}, {id:'experiments', label:'실험 · 비교', english:'Experiments'}, {id:'scenes', label:'3D 장면 · 영상', english:'Scene Engineering'}, {id:'controls', label:'AE · AWB 제어', english:'Camera Controls'}, {id:'validation', label:'실측 검증', english:'Validation'}, {id:'factory', label:'모델 팩토리', english:'Model Factory'}, {id:'releases', label:'근거 · 릴리스', english:'Releases'}];
  const labels = {succeeded:'계산 완료', queued:'대기 중', running:'계산 중', failed:'실행 실패', PASS:'통과', FAIL:'실패', INCONCLUSIVE:'판정 보류', INVALID_COMPARISON:'비교 조건 불일치', OUT_OF_DOMAIN:'적용 범위 밖', blocked:'배포 차단', draft:'검토용 초안', estimated:'추정 모델', simulated:'계산 결과', measured:'실측 원본', vendor:'제공 자료', available:'사용 가능', unavailable:'미연결', not_established:'미확립', pending:'대기', warning:'확인 필요', success:'확인 완료', compatible:'호환', incompatible:'호환 불가', unknown:'미확인',missing:'미확보',review_required:'검토 필요',implemented:'구현됨'};
  Object.assign(labels,{PROPOSED:'보정 후보 생성',REFUSED:'제안 거부',unapproved:'미승인',cancelled:'취소됨',NUMERICALLY_CONVERGED:'수치 수렴 확인'});
  const badge = (status, custom) => {
    const good = ['succeeded','PASS','available','success','compatible','ready','verified'].includes(status);
    const bad = ['FAIL','failed','INVALID_COMPARISON','incompatible','error','REFUSED'].includes(status);
    return `<span class="badge ${good ? 'green' : bad ? 'red' : 'orange'}">${esc(custom || labels[status] || status || '미확인')}</span>`;
  };
  const go = (view, label, style='secondary', name='arrow') => `<a class="button ${style}" href="#${view}">${esc(label)}${icon(name)}</a>`;
  const workbenchData=window.CameraWorkbenchData({api});
  const records = collection => array(state.data?.[collection]).map(workbenchData.resolve);
  const findRecord = (collection, id) => records(collection).find(item => item.id === id);
  const selectedAssembly = () => findRecord('assemblies', state.assembly) || records('assemblies')[0];
  const completedRuns = () => records('runs').filter(run => run.status === 'succeeded');
  const runSummary = run => run?.result?.summary || run?.result || run?.display_summary || {};
  const runLabel = run => `${run.name || short(run.id)} · ${run.plugin_id==='shared_charge_dcg_v1' ? (runSummary(run).acquisition_count || '—')+' 획득 · 각 2읽기' : ['temporal_scene_v1','closed_loop_scene_v1','hdr_bracket_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(run.plugin_id) ? ((runSummary(run).frame_count || array(runSummary(run).frames).length) ? (runSummary(run).frame_count || array(runSummary(run).frames).length)+' 프레임' : '시간 시퀀스') : number(run.parameters?.exposure_ms ?? Number(runSummary(run).conditions?.exposure_s?.value)*1000,2)+' ms'} · ${String(run.id).slice(-6)}`;
  const runtimeId = value => ({response_db:'response_db_v1',quick_estimate:'quick_estimate_v1'}[value] || value);
  const runtimeName = value => ({response_db_v1:'평탄장 · 응답 DB',quick_estimate_v1:'빠른 광량 추정',spatial_scene_v1:'분광 영상 · 공간 PSF',temporal_scene_v1:'시간 장면 · 프레임별 촬영',closed_loop_scene_v1:'RAW 피드백 · AE/AWB',retained_charge_v1:'잔류 전하 · 프레임 간 이동',electrical_control_v1:'전기 RAW 관측 · 노출 AE',hdr_bracket_v1:'전기 Readout · HDR · 주기광',reference_hdr_v1:'기준 노출 보존 · 움직임 HDR',shared_charge_dcg_v1:'공유 전하 · HCG/LCG · DCG'}[runtimeId(value)] || value);
  const selectedScene = () => findRecord('spectral_images',state.scene);
  const isSpatial = () => state.runtime==='spatial_scene_v1';
  const isElectrical = () => ['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(state.runtime);
  const isDCG = () => state.runtime==='shared_charge_dcg_v1';
  const isTemporal = () => ['temporal_scene_v1','hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(state.runtime);
  const selectedCaptureProgram = () => findRecord('capture_programs',state.captureProgram);
  const matchingCapturePrograms = () => records('capture_programs').filter(item=>item.assembly_id===state.assembly);
  const isSceneRuntime = () => isSpatial() || isTemporal();
  const selectedSceneSequence = () => findRecord('scene_sequences',state.sceneSequence);
  const selectedControlSequence = () => findRecord('control_sequences',state.controlSequence);
  const matchingControls = () => records('control_sequences').filter(item=>item.assembly_id===state.assembly && item.scene_sequence_id===state.sceneSequence);
  function clearSceneTest() {state.run='';state.test='';state.spatialTestDraft='';state.temporalTestDraft='';}
  const lensCapability = model => (model?.payload?.psf?.spatial_model || model?.display_summary?.psf_spatial_model)==='field_grid' ? '위치별 PSF + 분광·각도 응답' : (model?.payload?.psf || model?.display_summary?.has_psf) ? 'PSF + 분광·각도 응답' : '분광·각도 응답';
  const selectedRun = () => {
    const exact=findRecord('runs',state.run);
    const matches=run=>run.assembly_id===state.assembly && (!state.test || run.test_id===state.test) && runtimeId(run.plugin_id)===state.runtime && (state.runtime!=='spatial_scene_v1' || (state.test && run.scene_id===state.scene)) && (!isTemporal() || (state.test && run.scene_sequence_id===state.sceneSequence && run.control_sequence_id===state.controlSequence)) && CameraCaptureMatches(run,state);
    if(exact && matches(exact))return exact;
    return completedRuns().find(matches) || records('runs').find(matches);
  };
  function chooseRun(id) {
    state.run=id;
    const run=findRecord('runs',id);
    if(run){state.assembly=run.assembly_id;state.test=run.test_id;state.validationTest=run.test_id;state.runtime=runtimeId(run.plugin_id);state.scene=run.scene_id || '';state.sceneSequence=run.scene_sequence_id || '';state.controlSequence=run.control_sequence_id || '';state.captureProgram=run.capture_program_id || '';}
  }
  const testId = () => state.data?.demo?.test_id || records('tests')[0]?.id;
  const recordButton = (collection, item, label='기록 원본') => `<button class="link-button" data-action="record" data-collection="${esc(collection)}" data-id="${esc(item.id)}">${esc(label)}</button>`;
  const details = (title, value) => `<details class="data-detail"><summary>${esc(title)}</summary><pre>${esc(json(value))}</pre></details>`;
  const empty = (title, text, action='') => `<div class="empty-state"><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
  const issues = values => `<ul class="issue-list">${array(values).map(value => { const item=typeof value==='string'?{message:value}:value; return `<li class="issue ${['error','fail'].includes(item.severity) ? 'error' : item.severity === 'success' ? 'success' : ''}"><span class="issue-symbol">${item.severity === 'success' ? '✓' : '!'}</span><div><strong>${esc(item.code || item.label || item.severity || '검토 항목')}</strong>${esc(item.message || item.detail || '')}</div></li>`; }).join('')}</ul>`;
  const options = (collection, chosen, includeEmpty='') => `${includeEmpty ? `<option value="">${esc(includeEmpty)}</option>` : ''}${records(collection).map(item => `<option value="${esc(item.id)}" ${item.id === chosen ? 'selected' : ''}>${esc(item.name || short(item.id))}${item.revision ? ` · r${esc(item.revision)}` : ''}</option>`).join('')}`;
  const heading = (eyebrow, title, description, actions='') => `<div class="page-heading"><div><span class="eyebrow">${esc(eyebrow)}</span><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</div>`;

  const dcgUI=window.CameraDCGUI({state,esc,array,json,number,badge,details,options,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,render});
  const referenceHDRUI=CameraReferenceHDRUI({state,esc,array,number,badge,details,options,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,imagePreviews,frameBrowser});
  const electricalControlUI=window.CameraElectricalControlUI({state,esc,array,number,badge,details,options,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,frameBrowser});
  const retainedUI=CameraRetainedChargeUI({state,esc,array,number,badge,details,options,records,matchingCapturePrograms,selectedCaptureProgram,runSummary,empty,frameBrowser});
  const advancedISPUI=window.CameraAdvancedISPUI({api,esc,render,refresh,toast,source:replaySource,onModel:model=>{state.replayISP=model.id;}});
  const studyUI=window.CameraStudyUI({api,esc,records,badge,details,number,render,refresh,toast,busy,state,chooseRun,downloadObject});
  const surrogateUI=window.CameraSurrogateUI({api,esc,records,details,number,render,refresh,toast,busy,state,downloadObject});
  const sceneUI=window.CameraSceneUI({api,esc,records,badge,details,heading,recordButton,render,refresh,toast,busy,state,chooseRun});
  const controlUI=window.CameraControlUI({api,esc,records,badge,details,number,heading,render,refresh,toast,busy,state,downloadObject});
  const captureAnalysisUI=window.CameraCaptureAnalysisUI({api,esc,records,badge,details,recordButton,render,refresh,toast,busy,state,chooseCapture});
  const nonlinearCaptureUI=window.CameraNonlinearCaptureUI({api,esc,records,render,refresh,toast,busy,state,recordButton,details,onReplay:chooseReplaySource});
  const readoutFittingUI=window.CameraReadoutFittingUI({api,esc,records,render,refresh,toast,busy,state,recordButton,details});
  const responseImportUI=window.CameraResponseImportUI({api,esc,records,render,refresh,toast,busy,state,onImport:result=>{chooseCapture(result);captureAnalysisUI.selectCapture(result.capture.id);}});
  const caseUI=window.CameraCaseUI({api,esc,records,state,render,refresh,toast,downloadObject,openTarget:openCaseTarget});
  const ispStudyUI=window.CameraISPStudiesUI({api,esc,render,refresh,toast,records,isActive:()=>state.view==='experiments'&&state.experimentMode==='isp-study',onStudy:id=>{caseUI.offerISPStudy(id);location.hash='overview';render();}});
  const retainedISPUI=window.CameraRetainedISPUI({api,esc,render,records,isActive:()=>state.view==='experiments'&&state.experimentMode==='retained-isp'});
  const electricalResponseUI=window.CameraElectricalResponseController({api,esc,number,render,records,isActive:()=>state.view==='experiments'&&state.experimentMode==='electrical-response'});
  async function api(path, body) {
    const multipart=body instanceof FormData;
    const response = await fetch(BASE + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {...(multipart?{}:{'Content-Type':'application/json'}), 'X-Platform-Token':state.data?.token || ''}, body: body === undefined ? undefined : multipart?body:JSON.stringify(body)});
    const type = response.headers.get('content-type') || '';
    const result = type.includes('json') ? await response.json() : {detail: await response.text()};
    if (!response.ok) {
      const detail = result.detail || result.error || response.statusText;
      const error=new Error(typeof detail === 'string' ? detail : json(detail));
      error.status=response.status;
      error.preflight=result.preflight;
      throw error;
    }
    return result;
  }
  function toast(message, error=false) {
    clearTimeout(state.toastTimer);
    $('#toast').textContent = message;
    $('#toast').classList.toggle('error', error);
    $('#toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, error ? 13000 : 6000);
  }
  async function refresh({renderPage=true}={}) {
    const sequence=state.refreshSequence=(state.refreshSequence || 0)+1;
    const [boot,plugins,designTemplate]=await Promise.allSettled([api('/workbench'),api('/factory/plugins'),api('/design-experiments/template')]);
    if(sequence!==state.refreshSequence)return;
    if(boot.status==='rejected')throw boot.reason;
    const data=boot.value;
    if (data.schema !== 'camera_platform_v1' || data.workbench_schema !== 'camera_workbench_v1') throw new Error('현재 서버의 플랫폼 식별자가 다릅니다. Camera Platform 서버 주소를 확인하세요.');
    state.data = data;
    caseUI.sync();
    invalidateReplaySources();
    if(!state.replayJob)state.replayJob=data.replays?.[0]?.id || '';
    state.factoryPlugins=plugins.status==='fulfilled'?array(plugins.value):[];
    state.factoryError=plugins.status==='rejected'?plugins.reason.message:'';
    state.designExperiment=designTemplate.status==='fulfilled'?CameraDesignExperimentAvailability(designTemplate.value):{available:false,schema:''};
    if(!state.factoryPlugin)state.factoryPlugin=state.factoryPlugins.find(item=>item.available)?.plugin_id || state.factoryPlugins[0]?.plugin_id || '';
    if(!state.factoryJob)state.factoryJob=data.factory_jobs?.[0]?.id || '';
    if (!state.assembly) state.assembly = data.demo?.assembly_ids?.[0] || data.assemblies?.[0]?.id || '';
    if (!state.selectionInitialized) {
      state.selectionInitialized=true;
      state.run = completedRuns().find(r=>r.plugin_id!=='closed_loop_scene_v1')?.id || data.runs?.find(r=>r.plugin_id!=='closed_loop_scene_v1')?.id || '';
      const run=findRecord('runs',state.run);
      if(run?.assembly_id)chooseRun(run.id);
    }
    $('#connection').className = 'connection';
    $('#connection').innerHTML = '<i></i> Local · 연결됨';
    $('#workspace-path').textContent = typeof data.workspace === 'string' ? data.workspace : '조건 · 버전 · 출처 · 적용 범위';
    if (renderPage) render();
    beginPolling();
    beginFactoryPolling();
    beginReplayPolling();
  }
  function beginReplayPolling() {
    if(state.replayPolling || !records('replays').some(job=>['queued','running'].includes(job.status)))return;
    state.replayPolling=true;
    setTimeout(async()=>{
      try{
        const pending=records('replays').filter(j=>['queued','running'].includes(j.status));
        const jobs=await Promise.all(pending.map(j=>api('/replays/'+encodeURIComponent(j.id))));
        if(!Array.isArray(jobs))throw new Error('RAW 재처리 작업 목록 형식이 올바르지 않습니다.');
        const changed=jobs.filter(job=>findRecord('replays',job.id)?.status!==job.status);
        state.data.replays=state.data.replays.map(j=>jobs.find(update=>update.id===j.id) || j);
        for(const job of changed){if(job.status==='succeeded')toast('같은 RAW의 ISP 재처리 결과를 저장했습니다.');if(job.status==='failed')toast('RAW 재처리 실패: '+(job.error || '작업 원본 확인'),true);}
        if(changed.length && state.view==='experiments')render();
      }catch(error){toast(error.message,true);}finally{state.replayPolling=false;beginReplayPolling();}
    },1600);
  }
  function beginFactoryPolling() {
    if(state.factoryPolling || !records('factory_jobs').some(job=>['queued','running'].includes(job.status)))return;
    state.factoryPolling=true;
    setTimeout(async()=>{
      try{
        const pending=records('factory_jobs').filter(j=>['queued','running'].includes(j.status));
        const jobs=await Promise.all(pending.map(j=>api('/factory/jobs/'+encodeURIComponent(j.id))));
        if(!Array.isArray(jobs))throw new Error('Factory 작업 목록 응답 형식이 올바르지 않습니다.');
        const changed=jobs.filter(job=>findRecord('factory_jobs',job.id)?.status!==job.status);
        state.data.factory_jobs=state.data.factory_jobs.map(j=>jobs.find(update=>update.id===j.id) || j);
        for(const job of changed){
          if(job.status==='succeeded')toast(`${job.name || short(job.id)} · 해석 산출물을 저장했습니다. 수렴성과 지원 범위를 확인하세요.`);
          if(job.status==='failed')toast(`${job.name || short(job.id)} · 해석 실패. 작업 원본에서 오류를 확인하세요.`,true);
        }
        if(changed.length && state.view==='factory')render();
      }catch(error){toast(error.message,true);}
      finally{state.factoryPolling=false;beginFactoryPolling();}
    },1800);
  }
  function beginPolling() {
    if (state.polling || !records('runs').some(run => ['queued','running'].includes(run.status))) return;
    state.polling = true;
    setTimeout(async () => {
      try {
        const pending = records('runs').filter(run => ['queued','running'].includes(run.status));
        const settled = await Promise.allSettled(pending.map(run => api('/runs/' + encodeURIComponent(run.id))));
        let changed = false;
        settled.forEach((result, index) => {
          if (result.status === 'rejected') { toast(result.reason.message, true); return; }
          const run = result.value;
          const previous = pending[index];
          const slot = records('runs').findIndex(item => item.id === run.id);
          if (slot >= 0) state.data.runs[slot] = run;
          if (previous.status !== run.status) changed = true;
          if (previous.status !== 'succeeded' && run.status === 'succeeded') toast(`${run.name || short(run.id)} · 계산이 완료되었습니다.`);
          if (previous.status !== 'failed' && run.status === 'failed') toast(`계산 실패: ${typeof run.error === 'string' ? run.error : json(run.error || '실행 기록을 확인하세요.')}`, true);
        });
        if(changed)invalidateReplaySources();
        if (changed && ['overview','experiments','validation','controls'].includes(state.view)) render();
      } catch (error) { toast(error.message, true); }
      finally { state.polling = false; beginPolling(); }
    }, 1500);
  }
  function invalidateReplaySources() {
    state.replaySourcesLoaded=false;
    state.replaySourcesRevision=(state.replaySourcesRevision||0)+1;
  }
  async function loadReplaySources() {
    if(state.replaySourcesLoaded)return;
    if(state.replaySourcesLoading){await state.replaySourcesLoading;return loadReplaySources();}
    const revision=state.replaySourcesRevision;
    state.replaySourcesLoading=(async()=>{
      const sources=await api('/replay-sources');
      if(revision===state.replaySourcesRevision){state.replaySources=array(sources);state.replaySourcesLoaded=true;state.replaySourcesError='';}
    })();
    try{await state.replaySourcesLoading;}finally{state.replaySourcesLoading=null;}
    if(!state.replaySourcesLoaded)return loadReplaySources();
  }
  function visibleDetails() {
    const needed=[];
    if(state.view==='builder' || (state.view==='experiments'&&state.experimentMode==='camera')) {
      needed.push(...array(selectedAssembly()?.model_ids).map(id=>findRecord('models',id)));
    }
    if(state.view==='validation' || (state.view==='experiments'&&state.experimentMode==='camera'))needed.push(selectedRun());
    if(state.view==='factory'||state.view==='experiments')needed.push(selectedCaptureProgram(),selectedSceneSequence());
    if(state.view==='factory')needed.push(selectedFactoryJob(),calibrationBaseModel(),findRecord('models',state.factoryBaseLens));
    if(state.view==='experiments'&&state.experimentMode==='replay')needed.push(findRecord('replays',state.replayJob)||records('replays')[0],findRecord('models',state.replayISP));
    if(state.view==='controls')needed.push(...controlUI.needed());
    return needed.filter(item=>item?._projection);
  }
  function render() {
    const epoch=state.renderEpoch=(state.renderEpoch||0)+1;
    const focused=document.activeElement;
    const studyFocus=focused?.matches('[data-study-field]')?{field:focused.dataset.studyField,index:focused.dataset.index,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const surrogateFocus=focused?.matches('[data-surrogate-field]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const controlFocus=focused?.matches('[data-control-field]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const sceneFocus=focused?.closest('[data-scene-workbench]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const captureFocus=focused?.matches('[data-capture-field],#capture-program-json')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const advancedISPFocus=focused?.matches('[data-ispv2-edit],[data-ispv2-text]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const ispStudyFocus=focused?.matches('[data-ispstudy-field]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const retainedISPFocus=focused?.matches('[data-retainedisp-field]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const caseFocus=focused?.matches('[data-case-field]')&&focused.id?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const current = views.find(view => view.id === state.view) || views[0];
    $('#view-label').textContent = current.label;
    $('#navigation').innerHTML = views.map((view, i) => `${i === 5 ? '<div class="nav-divider" aria-hidden="true"></div>' : ''}<a class="nav-link ${view.id === state.view ? 'active' : ''}" href="#${view.id}" ${view.id === state.view ? 'aria-current="page"' : ''} title="${esc(view.english)} · ${esc(view.label)}">${icon(view.id)}<span>${esc(view.label)}</span></a>`).join('');
    if (!state.data) return;
    const missing=visibleDetails(),replayNeeded=state.view==='experiments'&&state.experimentMode==='replay'&&!state.replaySourcesLoaded;
    if(missing.length || replayNeeded){
      $('#main').innerHTML='<div class="loading-state" role="status"><span class="spinner"></span><p>선택한 기록의 원본을 불러오고 있습니다.</p></div>';
      Promise.all([...missing.map(workbenchData.load),...(replayNeeded?[loadReplaySources()]:[])]).then(()=>{if(epoch===state.renderEpoch)render();}).catch(error=>{
        if(epoch!==state.renderEpoch)return;
        $('#main').innerHTML=empty('기록 원본을 불러오지 못했습니다',esc(error.message),'<button class="button secondary" data-action="retry-details">다시 불러오기</button>');
      });
      return;
    }
    const renderers = {overview: overviewView, assets: assetsView, builder: builderView, experiments: experimentsView, validation: validationView, factory: factoryView, releases: releasesView, controls: controlUI.view, scenes: sceneUI.view};
    $('#main').innerHTML = (renderers[state.view] || overviewView)();
    if(captureFocus){const replacement=document.getElementById(captureFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&captureFocus.start!==null)replacement.setSelectionRange(captureFocus.start,captureFocus.end);}
    if(advancedISPFocus){const replacement=document.getElementById(advancedISPFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&advancedISPFocus.start!==null)replacement.setSelectionRange(advancedISPFocus.start,advancedISPFocus.end);}
    if(ispStudyFocus){const replacement=document.getElementById(ispStudyFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&ispStudyFocus.start!==null)replacement.setSelectionRange(ispStudyFocus.start,ispStudyFocus.end);}
    if(retainedISPFocus){const replacement=document.getElementById(retainedISPFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&retainedISPFocus.start!==null)replacement.setSelectionRange(retainedISPFocus.start,retainedISPFocus.end);}
    if(caseFocus){const replacement=document.getElementById(caseFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&caseFocus.start!==null)replacement.setSelectionRange(caseFocus.start,caseFocus.end);}
    if(sceneFocus){const replacement=document.getElementById(sceneFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&sceneFocus.start!==null)replacement.setSelectionRange(sceneFocus.start,sceneFocus.end);}
    if(controlFocus){const replacement=document.getElementById(controlFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&controlFocus.start!==null)replacement.setSelectionRange(controlFocus.start,controlFocus.end);}
    if(surrogateFocus){const replacement=document.getElementById(surrogateFocus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&surrogateFocus.start!==null)replacement.setSelectionRange(surrogateFocus.start,surrogateFocus.end);}
    if(studyFocus){
      const replacement=Array.from(document.querySelectorAll('[data-study-field]')).find(element=>element.dataset.studyField===studyFocus.field&&element.dataset.index===studyFocus.index);
      replacement?.focus({preventScroll:true});
      if(replacement?.setSelectionRange&&studyFocus.start!==null)replacement.setSelectionRange(studyFocus.start,studyFocus.end);
    }
  }
  function pipeline(assembly=selectedAssembly()) {
    const models = array(assembly?.model_ids).map(id => findRecord('models', id)).filter(Boolean);
    const get = (kind) => models.find(model => String(model.kind).includes(kind));
    const nodes = [{kind:'scene', title:'분광 입력', type:'Spectral target', icon:'light'}, {kind:'lens', title:get('lens')?.name || 'Lens response', type:lensCapability(get('lens')), icon:'lens'}, {kind:'sensor', title:get('sensor')?.name || 'Sensor response', type:'분광 · 각도별 QE', icon:'sensor'}, {kind:'isp', title:get('isp')?.name || 'Readout · RAW', type:'고정 조건 · 원인 분리', icon:'process'}];
    return `<div class="pipeline">${nodes.map(node => `<div class="pipeline-node ${node.kind}"><div class="node-icon">${icon(node.icon)}</div><h3 title="${esc(node.title)}">${esc(node.title)}</h3><p>${esc(node.type)}</p></div>`).join('')}</div>`;
  }
  function runControls(compact=false) {
    const assembly=selectedAssembly(),scene=selectedScene(),sequence=selectedSceneSequence(),control=selectedControlSequence();
    const plugins=records('plugins').filter(p=>['response_db_v1','quick_estimate_v1','spatial_scene_v1','temporal_scene_v1','hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(p.plugin_id));
    const blocked=!assembly || (isSpatial()&&!scene) || (isTemporal()&&(!sequence || !control || control.assembly_id!==state.assembly || control.scene_sequence_id!==state.sceneSequence)) || (isElectrical()&&(!selectedCaptureProgram()||selectedCaptureProgram().assembly_id!==state.assembly||CameraCaptureRuntime(selectedCaptureProgram().payload?.schema)!==state.runtime));
    const draft=isTemporal()?state.temporalTestDraft:state.spatialTestDraft;
    return `<section class="panel control-card"><div class="panel-header"><div><span class="eyebrow">NEXT EXPERIMENT</span><h2>입력과 실행 방식을 고정합니다</h2></div>${badge('draft',runtimeName(state.runtime))}</div><div class="panel-content"><form data-form="run">
      <div class="form-field"><label for="run-runtime">계산 방식</label><select id="run-runtime" name="plugin_id">${plugins.map(p=>`<option value="${esc(p.plugin_id)}" ${p.plugin_id===state.runtime?'selected':''} ${p.available?'':'disabled'}>${esc(runtimeName(p.plugin_id))}${p.available?'':' · 미연결'}</option>`).join('')}</select>${details('실행 방식의 지원 범위 · 원본 계약',plugins.find(p=>p.plugin_id===state.runtime) || {})}</div>
      <div class="form-field"><label for="run-assembly">카메라 구성 <span>버전 고정</span></label><select id="run-assembly" name="assembly_id" required>${options('assemblies',assembly?.id)}</select></div>
      ${isSpatial()?`<div class="form-field"><label for="run-scene">분광 장면 · 고정 버전</label><select id="run-scene" name="scene_id" required>${options('spectral_images',state.scene,'장면 선택')}</select></div>${scene?details('장면 원본 · 단위 · Sampling',scene):`<p class="list-note">등록한 분광 장면을 선택하세요. ${go('factory','장면 등록','quiet')}</p>`}<div class="scene-test-action"><button type="button" class="button secondary full" data-action="spatial-test-template" ${!scene?'disabled':''}>이 장면의 시험 정의 불러오기</button><p>시험의 입력 장면과 구성이 같아야 합니다.</p></div>`:''}
      ${isElectrical()?captureProgramSelector():''}
      ${isTemporal()?`<div class="form-field"><label for="run-scene-sequence">시간 장면 · 고정 버전</label><select id="run-scene-sequence" name="scene_sequence_id" required>${options('scene_sequences',state.sceneSequence,'시간 장면 선택')}</select></div>${sequence?`${badge(sequence.source_kind)}${details('장면 시각 · Clock · 보간 계약',sequence)}`:`<p class="list-note">Factory에서 장면 시퀀스를 등록하세요. ${go('factory','시간 장면 등록','quiet')}</p>`}<div class="inline-spacer"></div><div class="form-field"><label for="run-control-sequence">${state.runtime==='electrical_control_v1'?'프레임 시각과 초기 설정':'프레임별 적용 설정'}</label><select id="run-control-sequence" name="control_sequence_id" required><option value="">일치하는 설정 시퀀스 선택</option>${matchingControls().map(item=>`<option value="${esc(item.id)}" ${item.id===state.controlSequence?'selected':''}>${esc(item.name)} · r${esc(item.revision)}</option>`).join('')}</select></div>${control?`${badge(control.source_kind)}${details('적용 노출 · Gain · ISP · 타이밍',control)}`:''}<div class="scene-test-action"><button type="button" class="button secondary full" data-action="temporal-test-template" ${blocked?'disabled':''}>고정 입력의 시험 정의 불러오기</button><p>구성·장면·적용 설정을 모두 고정한 시험이 필요합니다.</p></div><div class="callout neutral">${state.runtime==='electrical_control_v1'?'시퀀스는 프레임 시작 시각·초기 노출·고정 Gain/ISP를 정합니다. 이후 노출은 완료된 RAW를 관측한 명령으로 결정됩니다.':'노출·Gain·ISP와 프레임 수는 적용 설정 기록을 따릅니다. 시간에 따른 장면 변화와 촬영 잡음을 구분하며, 프레임 간 변화량을 SNR로 해석하지 않습니다.'}</div><div class="inline-spacer"></div>`:''}
      <div class="form-field"><label for="run-test">시험 정의 <span>Test specification</span></label><select id="run-test" name="test_id" required>${isElectrical()?'<option value="">프로그램과 시퀀스에 맞는 시험 선택</option>'+records('tests').filter(test=>CameraCaptureTestMatches(test,state)).map(test=>`<option value="${esc(test.id)}" ${test.id===state.test?'selected':''}>${esc(test.name)} · r${esc(test.revision)}</option>`).join(''):options('tests',state.test || (!isSceneRuntime()?testId():''),isSceneRuntime()?'입력에 맞는 시험 선택':'')}</select></div>
      ${isSpatial()?'<div class="callout neutral">2D PSF가 연결된 Lens와 장면 Sampling을 검사합니다. 평면의 고정 장면 근사이며 깊이·가림·시간 효과의 지원은 Runtime 계약을 확인하세요.</div><div class="inline-spacer"></div>':''}
      <div class="${isTemporal()?'':'form-row'}">${!isTemporal()?`<div class="form-field"><label for="run-exposure">노출 <span>ms</span></label><input id="run-exposure" name="exposure_ms" type="number" step="0.01" min="0.01" max="10000" value="${esc(state.exposure)}" required></div>`:''}<div class="form-field"><label for="run-seed">난수 시드</label><input id="run-seed" name="seed" type="number" min="0" step="1" value="${esc(state.seed)}" required></div></div>${['retained_charge_v1','electrical_control_v1'].includes(state.runtime)?`<label class="check-row"><input id="retained-noise-enabled" type="checkbox" name="noise_enabled" ${state.retainedNoiseEnabled?'checked':''}> 전하·읽기 잡음 샘플링</label><p class="list-note">끄면 연속 전하의 결정론적 흐름을 계산합니다. 포화 부근의 확률적 평균값을 뜻하지 않습니다.</p>`:''}<div class="parameter-lock">${icon('lock')}<span>자산·시험·실행 조건과 원본 산출물의 버전을 고정합니다.</span></div><button type="submit" class="button primary full" ${blocked?'disabled':''}>${icon('play')}실험 실행</button></form>
      ${isSceneRuntime()&&draft?`<div class="inline-spacer"></div><form data-form="${isTemporal()?'temporal-test':'spatial-test'}"><div class="form-field"><label for="${isTemporal()?'temporal':'spatial'}-test-json">등록할 ${isTemporal()?'시간':'공간'} 시험 · JSON</label><textarea id="${isTemporal()?'temporal':'spatial'}-test-json" name="document" required spellcheck="false">${esc(draft)}</textarea></div><button class="button secondary full" type="submit">시험 정의를 등록하고 선택</button></form>`:''}${!compact&&!isSceneRuntime()&&records('assemblies').length>1?'<div class="inline-spacer"></div><button class="button secondary full" data-action="compare-assemblies">기준 두 렌즈의 평탄장 비교 실행 →</button>':''}</div></section>`;
  }
  function experimentModes() {
    return `<div class="experiment-modes" role="group" aria-label="실험 종류"><button class="mode-button ${state.experimentMode==='camera'?'active':''}" data-action="experiment-mode" data-mode="camera" aria-pressed="${state.experimentMode==='camera'}"><strong>카메라 계산</strong><span>장면 → Lens → Sensor → RAW → ISP</span></button><button class="mode-button ${state.experimentMode==='replay'?'active':''}" data-action="experiment-mode" data-mode="replay" aria-pressed="${state.experimentMode==='replay'}"><strong>저장된 RAW 재처리</strong><span>원본 촬영 조건 고정 → 새 ISP 프로파일</span></button><button class="mode-button ${state.experimentMode==='isp-study'?'active':''}" data-action="experiment-mode" data-mode="isp-study" aria-pressed="${state.experimentMode==='isp-study'}"><strong>같은 RAW의 ISP 탐색</strong><span>보정 고정 → 잡음 · 질감 · halo 비교</span></button><button class="mode-button ${state.experimentMode==='retained-isp'?'active':''}" data-action="experiment-mode" data-mode="retained-isp" aria-pressed="${state.experimentMode==='retained-isp'}"><strong>시간 이력 ISP 비교</strong><span>잔류 RAW → 공간·시간 처리 → 왜곡 비교</span></button><button class="mode-button ${state.experimentMode==='electrical-response'?'active':''}" data-action="experiment-mode" data-mode="electrical-response" aria-pressed="${state.experimentMode==='electrical-response'}"><strong>전기 AE · PWM 비교</strong><span>고정/AE 촬영 → 관측·지연 비교</span></button><button class="mode-button ${state.experimentMode==='study'?'active':''}" data-action="experiment-mode" data-mode="study" aria-pressed="${state.experimentMode==='study'}"><strong>설계 변수 탐색</strong><span>계획 검사 → 민감도 · 상충 관계 비교</span></button><button class="mode-button ${state.experimentMode==='surrogate'?'active':''}" data-action="experiment-mode" data-mode="surrogate" aria-pressed="${state.experimentMode==='surrogate'}"><strong>Surrogate 예측</strong><span>학습 → 별도 조건 검증 → 빠른 수치 예측</span></button></div>`;
  }
  function cameraPlot(run,compare=false) {
    if(runtimeId(run?.plugin_id || state.runtime)==='electrical_control_v1')return electricalControlUI.panel(run);
    if(runtimeId(run?.plugin_id || state.runtime)==='retained_charge_v1')return retainedUI.panel(run);
    if(runtimeId(run?.plugin_id || state.runtime)==='reference_hdr_v1')return referenceHDRUI.panel(run);
    if(runtimeId(run?.plugin_id || state.runtime)==='shared_charge_dcg_v1')return dcgUI.panel(run);
    if(runtimeId(run?.plugin_id || state.runtime)==='hdr_bracket_v1')return hdrCameraPanel(run);
    if(runtimeId(run?.plugin_id || state.runtime)==='temporal_scene_v1')return temporalCameraPanel(run);
    if(runtimeId(run?.plugin_id || state.runtime)!=='spatial_scene_v1')return chartPanel(run,compare);
    const summary=runSummary(run);
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">SPATIAL IMAGE FORMATION</span><h2>장면에서 영상으로</h2><p>${esc(findRecord('spectral_images',run?.scene_id || state.scene)?.name || '분광 장면을 선택하세요')}</p></div>${badge(run?.status || 'pending')}</div><div class="panel-content">${run?.status==='succeeded'?`${imagePreviews(array(run.artifacts),{kind:'spatial',summary})}${details('영상 좌표 · 물리 단위 · Sampling',{geometry:summary.geometry,spatial_sampling:summary.spatial_sampling,psf:summary.psf,array_units:summary.array_units})}`:empty(run?.status==='failed'?'공간 계산 실패':'공간 영상 계산 대기',run?.error || '장면·PSF Lens·시험을 고정하고 실행하면 저장된 결과가 표시됩니다.')}</div></section>`;
  }
  function resultFrames(job) {return array(runSummary(job).frames);}
  function nativeSitePanel(run) {
    const summary=runSummary(run),response=summary.native_pixel_response,prediction=summary.prediction;
    if(run?.status!=='succeeded' || !response || !prediction || prediction.unit!=='DN')return '';
    const axes=array(prediction.axes),sites=['R','Gr','Gb','B'],channel=axes.at(-1);
    if(channel?.name!=='channel' || JSON.stringify(channel.values)!==JSON.stringify(sites))return '';
    const frames=axes.length===1?['공간 크롭']:axes.length===2 && axes[0].name==='frame'?array(axes[0].values):[];
    const values=array(prediction.values);
    if(!frames.length || values.length!==frames.length*4)return '';
    const nonlinearSampled=prediction.method_and_version==='sampled_nonlinear_raw_black_relative_v1';
    const sampled=prediction.method_and_version==='sampled_raw_dark_corrected_v1';
    const knownExpected=String(prediction.method_and_version).startsWith('continuous_expected_');
    const label=nonlinearSampled?'실제 저장 RAW−black 평균 · 암전류 응답 포함':sampled?'실제 저장 RAW에서 계산한 보정 평균':knownExpected?'잡음·양자화 전의 기대 평균':'저장된 예측 평균';
    const rows=frames.map((frame,index)=>{
      const conditions=axes.length===1?prediction.conditions:array(prediction.conditions_per_frame).find(item=>item.frame_id===frame)?.conditions;
      const exposure=conditions?.exposure_s?.value;
      return `<tr><th scope="row">${esc(frame)}</th><td>${Number.isFinite(exposure)?number(exposure*1000,3)+' ms':'—'}</td>${values.slice(index*4,index*4+4).map(value=>`<td>${number(value,3)}</td>`).join('')}</tr>`;
    }).join('');
    return `<section class="panel"><div class="panel-header"><div><h2>네 픽셀 사이트의 응답</h2><p>${label} · DN</p></div>${badge(response.source?.kind || 'estimated')}</div><div class="panel-content"><div class="table-wrap"><table aria-label="네 픽셀 사이트별 RAW 평균"><thead><tr><th scope="col">프레임</th><th scope="col">노출</th>${sites.map(site=>`<th scope="col">${site}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div><p class="list-note">두 green 사이트를 따로 보존합니다. ${nonlinearSampled?'Black level만 뺀 실제 DN 평균이며, 비선형 암전류 응답을 빼거나 전자 수로 해석하지 않습니다.':'Black level·암전류 보정 정의는 원본 계산 계약을 따릅니다.'} ${sampled||nonlinearSampled?'잡음과 양자화 때문에 RAW 평균의 노출 비율은 정확한 정수배가 아닐 수 있습니다.':'기대값과 실제 저장 RAW의 표본 평균은 다를 수 있습니다.'}</p>${details('픽셀 응답 · 입사 방향 근거',response)}</div></section>`;
  }
  function chargeCollectionPanel(run) {
    const summary=runSummary(run),model=summary.charge_collection;
    if(run?.status!=='succeeded'||!model)return '';
    const scenes=array(summary.flux_diagnostics_per_scene);
    const entries=model.totals?[{label:'고정 노출',data:model}]:scenes.map((scene,index)=>({label:'장면 '+(index+1),data:scene.charge_collection})).filter(item=>item.data?.totals);
    const rows=entries.map(({label,data})=>`<tr><th scope="row">${esc(label)}</th><td>${esc(data.totals_unit)}</td>${['generated','collected','intrinsic_loss','off_sensor_loss'].map(key=>`<td>${number(data.totals[key],4)}</td>`).join('')}</tr>`).join('');
    return `<section class="panel"><div class="panel-header"><div><h2>광학 생성과 전하 수집</h2><p>실제 생성 위치에서 수신 픽셀로 이동한 전하와 손실을 보존합니다.</p></div>${badge('not_established','실물 검증 미확립')}</div><div class="panel-content">${rows?`<div class="table-wrap"><table aria-label="생성 수집 손실 보존"><thead><tr><th scope="col">계산 입력</th><th scope="col">단위</th><th scope="col">생성</th><th scope="col">수집</th><th scope="col">수집 손실</th><th scope="col">센서 밖 손실</th></tr></thead><tbody>${rows}</tbody></table></div>`:''}<p class="list-note">${model.totals?'고정 노출 동안의 기대 전하입니다.':'장면별 노출 전 전자율입니다. 프레임 전하는 수신 행의 노출 시간을 적분해 계산합니다.'} 수신 픽셀의 CFA를 다시 적용하지 않습니다.</p><div class="callout neutral">단일 목적지 확률 수집·즉시 이동·전체 센서 격자를 가정합니다. 일반 TCAD 수송, 아날로그 전하 분할, 부분 영역의 외부 유입은 지원하지 않습니다.</div>${details('생성·수집 계약 · 보존 근거',model)}</div></section>`;
  }
  function temporalCameraPanel(run) {
    const sequence=findRecord('scene_sequences',run?.scene_sequence_id || state.sceneSequence);
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">TIME RESOLVED IMAGE FORMATION</span><h2>시간에 따른 장면과 촬영 결과</h2><p>${esc(sequence?.name || '장면·설정 시퀀스를 선택하세요')}</p></div>${badge(run?.status || 'pending')}</div><div class="panel-content">${run?.status==='succeeded'?frameBrowser(run):empty(run?.status==='failed'?'시간 계산 실패':'시간 영상 계산 대기',run?.error || '장면·프레임 설정·시험을 고정하면 실제 저장된 프레임을 탐색할 수 있습니다.')}</div></section>`;
  }
  function hdrCameraPanel(run) {
    const summary=runSummary(run),succeeded=run?.status==='succeeded',fused=succeeded && summary.hdr_merge_applied===true;
    const hdr=fused?(summary.hdr_merge || summary.hdr || {}):{},total=Number(hdr.valid_pixels)+Number(hdr.invalid_pixels);
    const program=run?.capture_program_snapshot || findRecord('capture_programs',run?.capture_program_id) || selectedCaptureProgram();
    const diagnostics=`<p class="list-note">${summary.hdr_merge_applied===false?'HDR 합성 사용 안 함 · 프레임별 전기/PWM 진단':'HDR 합성 상태 미기록 · 원본 결과를 확인하세요.'}</p><dl class="frame-facts"><div><dt>저장된 프레임</dt><dd>${array(summary.frames).length}</dd></div>${summary.waveform?.exposure_ratio_array?`<div><dt>행별 노출 대비 광량 비율 배열</dt><dd><code>${esc(summary.waveform.exposure_ratio_array)}</code></dd></div>`:''}${summary.waveform?.effective_seconds_array?`<div><dt>행별 광량 적분 시간 배열</dt><dd><code>${esc(summary.waveform.effective_seconds_array)}</code></dd></div>`:''}</dl>`;
    const note=fused?'미리보기는 톤 처리 영상입니다. 원본 RAW와 signed 선형 전자율·무효 마스크는 별도 산출물로 보존됩니다. 한 노출만 유효하면 시간 일관성을 확인할 수 없습니다. 분산은 모델 추정값입니다.':succeeded?'미리보기는 개별 RAW에 ISP를 적용한 영상이며 대표 영상은 첫 프레임입니다. 프레임별 원본 RAW와 전기·광량 적분 진단을 확인하세요.':'고정 프로그램에서 HDR 합성을 선택하거나, 합성을 끄고 프레임별 전기·주기광 진단을 실행할 수 있습니다.';
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">RAW BRACKET · ELECTRICAL MODEL</span><h2>전기 Readout과 HDR·주기광 결과</h2><p>${esc(program?.name || run?.capture_program_id || '고정 캡처 프로그램을 선택하세요')}</p></div>${badge(run?.status||'pending')}</div><div class="panel-content">${succeeded?`${fused?(total>0?`<dl class="frame-facts"><div><dt>유효 RAW 합성 픽셀</dt><dd>${number(100*hdr.valid_pixels/total,2)} %</dd></div><div><dt>시간 일관성 미확인</dt><dd>${number(hdr.temporal_consistency_unknown_pixels,0)}</dd></div><div><dt>단일 노출만 유효</dt><dd>${number(hdr.single_usable_exposure_pixels,0)}</dd></div><div><dt>시간 일관성 불일치</dt><dd>${number(hdr.temporal_inconsistent_pixels,0)}</dd></div></dl>`:'<p class="list-note">HDR 합성 진단은 원본 결과를 확인하세요.</p>'):diagnostics}${imagePreviews(array(run.artifacts).filter(a=>a.name==='sensor-rgb.png'),{kind:fused?'hdr':'electrical',summary})}${array(summary.frames).length?frameBrowser(run):''}${fused?details('HDR 가중치 · 무효 마스크 · 추정 분산',hdr):details('프레임별 전기·PWM 진단',{frames:summary.frames,waveform:summary.waveform})}${details('고정 캡처 프로그램 · 전체 결과',summary)}`:empty(run?.status==='failed'?'HDR·주기광 계산 실패':'HDR·주기광 실행 대기',run?.error || '카메라·캡처 프로그램·시간 장면·적용 설정·시험을 고정한 뒤 실행하세요.')}<div class="callout neutral">${note} 실물 검증·DCG·움직임 정합·상용 LFM 성능을 뜻하지 않습니다.</div></div></section>`;
  }
  function captureProgramSelector() {
    const program=selectedCaptureProgram(),schema=CameraCaptureSchema(state.runtime);
    const entries=matchingCapturePrograms().filter(p=>CameraCaptureRuntime(p.payload?.schema)===state.runtime);
    const note=state.runtime==='electrical_control_v1'?electricalControlUI.scopeNote:state.runtime==='retained_charge_v1'?retainedUI.scopeNote:state.runtime==='reference_hdr_v1'?referenceHDRUI.scopeNote+' null 또는 constant 광원만 지원합니다.':isDCG()?'한 번 얻은 전하를 HCG/LCG 두 경로로 읽습니다. 정적 장면·gain 1만 지원하며, 노출과 시각은 획득별 적용 설정을 따릅니다. 각 경로 이득은 프로그램에 고정됩니다.':'순차 독립 노출입니다. HDR 합성은 정적 장면과 최소 두 프레임만 지원하며, PWM 사용 시 모든 노출이 주기의 정수배여야 합니다.';
    return `<div class="form-field"><label for="run-capture-program">전기 캡처 프로그램 <span>고정 버전</span></label><select id="run-capture-program" name="capture_program_id" required><option value="">현재 카메라의 프로그램 선택</option>${entries.map(p=>`<option value="${esc(p.id)}" ${p.id===state.captureProgram?'selected':''}>${esc(p.name)} · r${esc(p.revision)}</option>`).join('')}</select></div>${program&&entries.includes(program)?details('고정 프로그램 원본',program):''}<div class="callout neutral">${note}</div><div class="inline-spacer"></div>${go('factory','캡처 프로그램 작성 · 등록','quiet')}<div class="inline-spacer"></div>`;
  }
  function nonlinearTemplateActions() {
    const disabled=!state.assembly||state.captureLoading?'disabled':'';
    return `<section class="panel"><div class="panel-content"><h3>비선형 전하·전압 응답</h3><p>단조 응답 곡선과 RAW 역보정을 함께 사용하는 작성 예제입니다. 기존 모델을 실측 보정으로 변환하지 않습니다.</p><div class="form-row"><button class="button secondary" data-action="capture-nonlinear-template" ${disabled}>비선형 순차 HDR 초안</button><button class="button secondary" data-action="capture-nonlinear-dcg-template" ${disabled}>비선형 HCG/LCG 초안</button><button class="button secondary" data-action="capture-nonlinear-reference-template" ${disabled}>비선형 기준 노출 HDR 초안</button></div></div></section>`;
  }
  function nonlinearCaptureEditor(document,text,error) {
    const model=document.payload.electrical_model,id=state.assembly;
    const input=(path,label,value,unit='',type='number')=>`<div class="form-field"><label>${esc(label)} <span>${esc(unit)}</span><input data-capture-field="${esc(path)}" type="${type}" ${type==='number'?'step="any"':''} value="${esc(value)}" required></label></div>`;
    const paired=document.payload.schema==='camera_dcg_capture_program_v2';
    const paths=paired?array(model?.paths).map((p,i)=>({value:p,prefix:'payload.electrical_model.paths.'+i,label:p.stream_id})):model?[{value:model,prefix:'payload.electrical_model',label:'독립 노출'}]:[];
    const curves=paths.map(({value,prefix,label})=>{
      const curve=value.charge_voltage_response;if(!curve)return '<p class="callout error">전하·전압 응답 곡선을 확인하세요.</p>';
      return `<section><h3>${esc(label)} · 전하→신호 전압</h3><p class="list-note">단위 electron / V · 온도 ${esc(json(curve.valid_domain?.temperature_c))} °C · analog gain ${esc(json(curve.valid_domain?.analog_gain))}. 원점은 (0,0), 두 축은 모두 증가해야 합니다.</p><div class="table-wrap"><table><thead><tr><th>전하 · electron</th><th>신호 전압 · V</th></tr></thead><tbody>${array(curve.charge_e).map((q,i)=>`<tr><td>${input(prefix+'.charge_voltage_response.charge_e.'+i,'전하 '+i,q,'electron')}</td><td>${input(prefix+'.charge_voltage_response.signal_voltage_v.'+i,'전압 '+i,curve.signal_voltage_v?.[i],'V')}</td></tr>`).join('')}</tbody></table></div>${details('곡선 출처와 signed 역보정 범위',{source:curve.source,signed_inverse:curve.signed_inverse})}</section>`;
    }).join('');
    return `<section class="panel" id="capture-program-editor"><div class="panel-header"><div><span class="eyebrow">NONLINEAR READOUT · RAW CALIBRATION</span><h2>비선형 전기 응답과 역보정</h2><p>곡선·읽기 잡음·적용 범위를 고정하고 새 버전으로 등록합니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="capture-program-assembly">고정 카메라 구성</label><select id="capture-program-assembly">${options('assemblies',id)}</select></div><div class="form-field"><label for="factory-capture-program">등록된 프로그램</label><select id="factory-capture-program"><option value="">고정 프로그램 선택</option>${matchingCapturePrograms().map(p=>`<option value="${esc(p.id)}" ${p.id===state.captureProgram?'selected':''}>${esc(p.name)} · r${esc(p.revision)}</option>`).join('')}</select></div><div class="form-row"><button class="button secondary" data-action="capture-use" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전으로 실험</button><button class="button quiet" data-action="capture-copy" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전에서 새 초안</button><button class="button quiet" data-action="capture-template">선형 기본 초안</button></div>${state.captureError||error?`<p role="alert" class="callout error">${esc(state.captureError||error)} · 입력 초안은 유지됩니다.</p>`:''}<form data-form="capture-program"><div class="form-row">${input('name','프로그램 이름',document.name,'','text')}${input('revision','새 버전',document.revision,'','text')}</div>${curves}<div class="callout neutral">읽기 잡음은 전하 변환 후·analog gain 전의 전압 잡음입니다. RAW 코드의 양자화 구간과 포화 마스크를 보존합니다. 미리보기는 보정된 전하와 고정 WB·CCM·tone 설정만 사용합니다. 고급 ISP 탐색·시간 ISP·실물 정확도 검증을 뜻하지 않습니다.</div>${details('읽기 잡음과 출처 · V²',model?.read_noise||{})}<details class="data-detail" open><summary>전체 설정 · 잡음 공분산 · 적용 범위 · 원본 JSON</summary><div class="form-field"><label for="capture-program-json">검토 후 등록할 전체 원문</label><textarea id="capture-program-json" name="document" spellcheck="false" required>${esc(text)}</textarea></div></details><button type="submit" class="button primary full" ${state.captureSaving?'disabled':''}>${state.captureSaving?'등록 중…':'새 프로그램 버전을 등록하고 선택'}</button></form><p class="list-note">작성 예제는 estimated 근거입니다. 비교 지표는 실제 RAW−black DN이며, 비선형 dark 응답을 빼거나 광자 수로 해석하지 않습니다. signed 전하율과 무효 마스크는 별도 산출물입니다.</p></div></section>`;
  }
  function captureProgramEditor() {
    const id=state.assembly,text=state.captureDrafts.get(id);let document=null,error='';
    if(text)try{document=state.captureDrafts.parse(id);}catch(e){error=e.message;}
    if(['camera_electrical_control_capture_program_v1','camera_electrical_control_capture_program_v2','camera_electrical_control_capture_program_v3'].includes(document?.payload?.schema))return electricalControlUI.editor(document,text,error);
    if(['camera_retained_charge_capture_program_v1','camera_retained_charge_capture_program_v2','camera_retained_charge_capture_program_v3','camera_retained_charge_capture_program_v4','camera_retained_charge_capture_program_v5'].includes(document?.payload?.schema))return retainedUI.editor(document,text,error);
    if(['camera_capture_program_v2','camera_dcg_capture_program_v2','camera_reference_hdr_capture_program_v2'].includes(document?.payload?.schema))return nonlinearCaptureEditor(document,text,error);
    if(document?.payload?.schema==='camera_dcg_capture_program_v1')return dcgUI.editor(document,text,error);
    if(document?.payload?.schema==='camera_reference_hdr_capture_program_v1')return referenceHDRUI.editor(document,text,error);
    const candidate=document?.payload?.electrical_model,wave=document?.payload?.waveform;
    const model=candidate&&typeof candidate==='object'&&!Array.isArray(candidate)&&candidate.adc&&typeof candidate.adc==='object'?candidate:null;
    if(document&&!model)error='전기 모델과 ADC 객체를 원본 JSON에서 확인하세요.';
    const field=(path,label,value,unit='',type='number')=>`<div class="form-field"><label for="capture-${esc(path.replaceAll('.','-'))}">${esc(label)} <span>${esc(unit)}</span></label><input id="capture-${esc(path.replaceAll('.','-'))}" data-capture-field="${esc(path)}" type="${type}" ${type==='number'?'step="any"':''} value="${esc(value??'')}" required></div>`;
    return `<section class="panel" id="capture-program-editor"><div class="panel-header"><div><span class="eyebrow">ELECTRICAL · HDR · PERIODIC LIGHT</span><h2>촬영 전기 모델과 합성 설정</h2><p>일반 입력을 조정하고 검토한 내용을 새 버전으로 등록합니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="capture-program-assembly">고정 카메라 구성</label><select id="capture-program-assembly">${options('assemblies',id)}</select></div><div class="form-row"><button type="button" class="button secondary" data-action="capture-template" ${!id||state.captureLoading?'disabled':''}>${state.captureLoading?'불러오는 중…':'새 기본 프로그램 불러오기'}</button><button type="button" class="button secondary" data-action="capture-dcg-template" ${!id||state.captureLoading?'disabled':''}>새 DCG 프로그램</button><button type="button" class="button secondary" data-action="capture-reference-template" ${!id||state.captureLoading?'disabled':''}>새 기준 노출 HDR 프로그램</button><button type="button" class="button quiet" data-action="capture-copy" ${!selectedCaptureProgram()?'disabled':''}>선택한 버전에서 새 초안</button></div><div class="form-field"><label for="factory-capture-program">등록된 프로그램</label><select id="factory-capture-program"><option value="">고정 프로그램 선택</option>${matchingCapturePrograms().map(p=>`<option value="${esc(p.id)}" ${p.id===state.captureProgram?'selected':''}>${esc(p.name)} · r${esc(p.revision)}</option>`).join('')}</select></div>${selectedCaptureProgram()?`<div class="locked-reference"><span>${esc(selectedCaptureProgram().id)}</span><button type="button" class="button secondary" data-action="capture-use">이 버전으로 실험</button></div>`:''}${state.captureError?`<div class="callout error" role="alert">${esc(state.captureError)}</div>`:''}${error?`<div class="callout error">JSON 확인 필요: ${esc(error)} · 입력 원문은 유지됩니다.</div>`:''}<form data-form="capture-program">${model?`<div class="form-row">${field('name','프로그램 이름',document.name,'','text')}${field('revision','새 버전',document.revision,'','text')}</div><div class="form-row">${field('payload.electrical_model.full_well_e','Full-well',model.full_well_e,'electron')}${field('payload.electrical_model.conversion_gain_v_per_e','전하→전압 변환 이득',model.conversion_gain_v_per_e,'V/electron')}${field('payload.electrical_model.read_noise_e','입력 읽기 잡음',model.read_noise_e,'electron RMS')}${field('payload.electrical_model.post_gain_noise_v','증폭 후 잡음',model.post_gain_noise_v,'V RMS')}${field('payload.electrical_model.digital_gain','디지털 이득',model.digital_gain,'×')}${field('payload.electrical_model.adc.full_scale_v','ADC 전압 범위',model.adc.full_scale_v,'V')}</div><p class="list-note">ADC ${esc(model.adc.bit_depth)} bit · black ${esc(model.adc.black_level_dn)} DN은 고정 카메라 RAW 형식과 일치해야 합니다. 아날로그 이득과 노출은 프레임별 적용 설정에서 지정합니다.</p><div class="form-row">${field('payload.electrical_model.dark_current_e_per_s','기준 암전류',model.dark_current_e_per_s,'electron/s')}${field('payload.electrical_model.reference_temperature_c','기준 온도',model.reference_temperature_c,'°C')}</div><label class="check-row"><input type="checkbox" id="capture-dark-temperature" ${model.dark_current_doubling_c!==null?'checked':''}> 온도에 따른 암전류 증가 모델 사용</label>${model.dark_current_doubling_c!==null?field('payload.electrical_model.dark_current_doubling_c','암전류가 두 배가 되는 온도 차',model.dark_current_doubling_c,'°C'):''}<label class="check-row"><input type="checkbox" id="capture-hdr-enabled" ${document.payload.hdr_merge?'checked':''}> 다중 노출 RAW를 선형 HDR로 합성</label>${document.payload.hdr_merge?`<div class="form-row">${field('payload.hdr_merge.saturation_margin_dn','포화 제외 여유',document.payload.hdr_merge.saturation_margin_dn,'DN')}${field('payload.hdr_merge.consistency_sigma','시간 일관성 기준',document.payload.hdr_merge.consistency_sigma,'σ')}${field('payload.hdr_merge.reference_exposure_s','표시 기준 노출',document.payload.hdr_merge.reference_exposure_s,'s')}</div>`:'<p class="list-note">합성 없이 프레임별 RAW·전기 진단과 PWM 위상 영향을 보존합니다.</p>'}<label class="check-row"><input type="checkbox" id="capture-pwm-enabled" ${wave?.kind==='pwm'?'checked':''}> 장면 전체 PWM 주기광</label>${wave?.kind==='pwm'?`<div class="form-row">${field('payload.waveform.period_ns','주기',wave.period_ns,'ns')}${field('payload.waveform.on_time_ns','켜짐 시간',wave.on_time_ns,'ns')}${field('payload.waveform.phase_origin_ns','위상 원점',wave.phase_origin_ns,'ns')}${field('payload.waveform.low','꺼짐 구간 배율',wave.low,'×')}${field('payload.waveform.high','켜짐 구간 배율',wave.high,'×')}</div>${field('payload.waveform.clock_id','시간 장면과 같은 Clock ID',wave.clock_id,'','text')}`:''}<div class="callout neutral">HDR는 정적 장면·독립 순차 노출만 지원합니다. HDR+PWM은 각 노출이 주기의 정수배여야 합니다. PWM은 장면 전체를 변조하며 독립 배경광·제품별 LED 회로·DCG를 재현하지 않습니다.</div>`:''}<details class="data-detail" ${error?'open':''}><summary>고급 설정 · 출처 · 원본 프로그램 JSON</summary><div class="form-field"><label for="capture-program-json">검토 후 등록할 전체 원문</label><textarea id="capture-program-json" name="document" spellcheck="false" required>${esc(text)}</textarea></div></details><div class="inline-spacer"></div><button type="submit" class="button primary full" ${!document||state.captureSaving?'disabled':''}>${state.captureSaving?'등록 중…':'새 프로그램 버전을 등록하고 선택'}</button></form><p class="list-note">화면의 추정 전기 모델은 실물 교정·출시 승인 근거가 아닙니다. 입력 초안은 데이터 새로고침·화면 이동·등록 오류에도 유지됩니다.</p></div></section>`;
  }
  function frameBrowser(job,replay=false) {
    const frames=resultFrames(job),summary=runSummary(job);
    if(!frames.length)return empty('프레임별 결과 계약이 없습니다','전체 실행 원본에서 저장된 프레임·타이밍 정보를 확인하세요.');
    const selected=state.frameSelections[job.id];
    const index=Math.max(0,frames.findIndex(frame=>String(frame.frame_id)===String(selected)));
    const frame=frames[index],applied=frame.applied || {},artifact=array(job.artifacts).find(item=>item.id===frame.preview_artifact_id);
    const url=safeArtifactUrl(frame.preview_url || artifact?.url);
    const preview=url?[{...artifact,name:artifact?.name || 'Frame '+frame.frame_id,media_type:artifact?.media_type || 'image/png',url}]:[];
    const gain=typeof applied.gain==='object'?applied.gain?.value:applied.gain;
    return `<div class="frame-browser"><div class="frame-navigation"><button class="button secondary small" type="button" data-action="frame-step" data-job="${esc(job.id)}" data-step="-1" ${index===0?'disabled':''} aria-label="이전 프레임">← 이전</button><div class="form-field"><label for="frame-${esc(job.id)}">표시할 저장 프레임 <span>${index+1} / ${frames.length}</span></label><select id="frame-${esc(job.id)}" data-frame-job="${esc(job.id)}">${frames.map((item,i)=>`<option value="${esc(item.frame_id)}" ${i===index?'selected':''}>Frame ${esc(item.frame_id)} · ${item.frame_start_ns==null?'시각 미제공':esc(item.frame_start_ns)+' ns'}</option>`).join('')}</select></div><button class="button secondary small" type="button" data-action="frame-step" data-job="${esc(job.id)}" data-step="1" ${index===frames.length-1?'disabled':''} aria-label="다음 프레임">다음 →</button></div>
      <dl class="frame-facts"><div><dt>Frame ID</dt><dd>${esc(frame.frame_id)}</dd></div><div><dt>시작 시각 · ns</dt><dd>${esc(frame.frame_start_ns ?? '미제공')}</dd></div><div><dt>원본 적용 노출</dt><dd>${applied.exposure_ns===undefined?'미제공':number(Number(applied.exposure_ns)/1e6,6)+' ms'}<small>${applied.exposure_ns===undefined?'':esc(applied.exposure_ns)+' ns'}</small></dd></div><div><dt>원본 적용 Gain</dt><dd>${number(gain,6)} ×</dd></div></dl>
      <div class="frame-isp"><span>원본 적용 ISP</span><strong>${esc(findRecord('models',applied.isp_model_id)?.name || applied.isp_model_id || '원본 적용 설정 확인')}</strong>${replay?`<span>이 재처리의 ISP · 전체 프레임 동일</span><strong>${esc(findRecord('models',job.isp_model_id)?.name || job.isp_model_id)}</strong>`:''}</div>
      ${preview.length?imagePreviews(preview,{kind:replay?'replay':'temporal',summary}):empty('저장된 프레임 미리보기가 없습니다','원본 프레임 산출물과 작업 계약을 확인하세요.')}
      ${details('선택 프레임 · 실제 적용 설정 · 타이밍',frame)}${details('시간 기준 · 좌표 · 전체 프레임 근거',{clock:summary.clock,geometry:summary.geometry,frame_conditions:summary.frame_conditions,frames})}
      <div class="inline-spacer"></div><div class="callout neutral">${replay?'원본의 프레임별 노출·Gain·시간 기록을 보존했습니다. 새 ISP를 모든 RAW 프레임에 적용하며 촬영을 반복하지 않습니다.':'장면의 움직임·점멸·설정 변화가 포함된 결과입니다. 프레임 간 영상 분산을 촬영 잡음이나 SNR로 표시하지 않습니다.'}</div></div>`;
  }
  function replayAcquisition(source) {
    const frames=array(source.frame_records),conditions=array(source.frame_conditions);
    return frames.length || conditions.length?`<p class="list-note">프레임별 원본 촬영 조건 · ${frames.length || conditions.length}개 기록. 각 프레임의 노출·Gain·ISP·타이밍을 고정합니다.</p>${details('원본 프레임별 취득 조건 · 기록',{clock:source.clock,frame_conditions:source.frame_conditions,frame_records:source.frame_records})}`:`<pre>${esc(json(source.conditions))}</pre>`;
  }
  function sourceKey(source) {return source.source_kind+':'+source.source_id+(source.selector?':'+JSON.stringify([source.selector.schema,source.selector.mode,source.selector.readout_id||'',...(source.selector.readout_calibration_id?[source.selector.readout_calibration_id]:[])]):'');}
  function replaySource() {return state.replaySources.find(source=>sourceKey(source)===state.replaySource);}
  function replayControls() {
    const source=replaySource(),profiles=records('models').filter(m=>m.kind==='isp'&&m.payload?.schema!=='camera_retained_code_isp_profile_v1'&&(source?.selector?m.payload?.schema==='camera_isp_profile_v3':m.payload?.schema!=='camera_isp_profile_v3'));
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">PRESERVED RAW → ISP</span><h2>같은 RAW로 처리 차이를 비교합니다</h2><p>노출과 Sensor를 다시 계산하지 않습니다.</p></div></div><div class="panel-content">${state.replaySourcesError?`<div class="callout error">RAW 원본 목록 연결 오류: ${esc(state.replaySourcesError)}</div>`:''}<form data-form="replay"><div class="form-field"><label for="replay-source">원본 RAW</label><select id="replay-source" required><option value="">보존된 RAW 선택</option>${['capture','run'].map(kind=>`<optgroup label="${kind==='capture'?'취득 RAW · 출처 확인':'카메라 계산 RAW'}">${state.replaySources.filter(item=>item.source_kind===kind).map(item=>`<option value="${esc(sourceKey(item))}" ${sourceKey(item)===state.replaySource?'selected':''}>${esc(item.name || item.source_id)} · ${esc(array(item.raw_shape).join('×'))}</option>`).join('')}</optgroup>`).join('')}</select></div>${source?`<div class="factory-condition-source"><strong>${esc(source.name)}</strong><code>${esc(source.source_id)}</code><small>RAW SHA-256 · ${esc(source.raw_sha256)}</small>${replayAcquisition(source)}<small>원본의 촬영 조건입니다. 이 작업에서는 편집하지 않습니다.</small></div>${details('원본 형상 · RAW 형식 · 출처',source)}`:empty('보존된 RAW를 선택하세요','실측 취득 또는 완료된 카메라 계산의 실제 RAW 배열을 사용합니다.')}<div class="form-field"><label for="replay-isp">새 ISP 프로파일 · 버전 고정</label><select id="replay-isp" name="isp_model_id" required><option value="">ISP 모델 선택</option>${profiles.map(m=>`<option value="${esc(m.id)}" ${m.id===state.replayISP?'selected':''}>${esc(m.name)} · r${esc(m.revision)}</option>`).join('')}</select></div>${state.replayISP?details('선택 ISP 계약',findRecord('models',state.replayISP)):''}<div class="form-field"><label for="replay-name">재처리 실험 이름</label><input id="replay-name" name="name" value="${esc(state.replayName)}" required maxlength="200" placeholder="예: 같은 RAW · ISP 프로파일 B"></div><div class="callout">원본 RAW에 이미 기록된 포화·노이즈·촬영 조건을 유지합니다. ISP 변경으로 다른 노출이나 다른 Sensor의 촬영을 재현하지 않습니다.</div><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!source?'disabled':''}>${icon('play')}같은 RAW로 ISP 재처리</button></form></div></section>`;
  }
  function replayResults() {
    const jobs=records('replays').filter(item=>item.plugin_id!=='retained_isp_replay_v1'),job=jobs.find(item=>item.id===state.replayJob) || jobs[0],summary=runSummary(job);
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">RAW REPROCESSING HISTORY</span><h2>RAW 재처리 결과</h2><p>카메라 촬영 Run과 별도로 보존한 작업입니다.</p></div><span class="stat-small">${jobs.length} REPLAYS</span></div><div class="panel-content">${jobs.length?`<div class="form-field"><label for="replay-job">표시할 재처리 작업</label><select id="replay-job">${jobs.map(item=>`<option value="${esc(item.id)}" ${job?.id===item.id?'selected':''}>${esc(item.name || item.id)} · ${esc(labels[item.status] || item.status)}</option>`).join('')}</select></div>`:''}${job?`<div class="result-status"><div><h3>${esc(job.name)}</h3><p>${esc(job.source_kind)} · ${esc(job.source_id)}</p><p>ISP · ${esc(findRecord('models',job.isp_model_id)?.name || job.isp_model_id)}</p></div>${badge(job.status)}</div>${job.error?`<div class="inline-spacer"></div><div class="callout error">${esc(job.error)}</div>`:''}${['queued','running'].includes(job.status)?`<div class="inline-spacer"></div><button class="button secondary" data-action="cancel-replay" data-id="${esc(job.id)}">재처리 작업 취소</button>`:''}${job.status==='succeeded'?`${resultFrames(job).length?frameBrowser(job,true):imagePreviews(array(job.artifacts),{kind:'replay',summary})}${details('고정 취득 조건 · 좌표 · ISP 적용 결과',summary)}<div class="inline-spacer"></div><a class="button secondary" href="/api/v1/replays/${encodeURIComponent(job.id)}/bundle" download>원본 근거 포함 ZIP ↓</a>`:''}${details('재처리 작업 전체 기록',job)}${artifactLinks(array(job.artifacts))}`:empty('아직 RAW 재처리 이력이 없습니다','원본 RAW와 ISP 프로파일을 선택하면 실제 처리 결과를 보존합니다.')}</div></section>`;
  }
  function artifactLinks(artifacts) {
    const unique=Array.from(new Map(artifacts.map(item=>[(item.sha256 || item.id)+':'+item.name,item])).values());
    const link=item=>{const url=safeArtifactUrl(item.url);return url?`<a class="button secondary small" href="${esc(url)}" download title="SHA-256 ${esc(item.sha256 || item.id)}">${esc(item.name)} ↓</a>`:'';};
    if(unique.length<=12)return `<div class="artifact-links">${unique.map(link).join('')}</div>`;
    const primary=['raw_frames.npy','rgb_frames.npy','summary.json','sensor-rgb.png'];
    const important=primary.map(name=>unique.find(item=>item.name===name)).filter(Boolean);
    return `<div class="artifact-summary"><div class="artifact-links">${important.map(link).join('')}</div><details class="data-detail artifact-browser"><summary>전체 산출물 ${unique.length}개 · 원본 및 중간 계산</summary><p class="list-note">같은 이름의 파일은 SHA-256으로 구분합니다. 입력 시각과 원본 연결은 실행 기록에 보존됩니다.</p><div class="artifact-file-list">${unique.map(item=>`<div class="artifact-file-row">${link(item)}<code title="${esc(item.sha256 || item.id)}">${esc(String(item.sha256 || item.id).slice(0,12))}</code><small>${number(item.bytes/1024,1)} KiB${Array.isArray(item.shape)?' · '+esc(item.shape.join(' × ')):''}</small></div>`).join('')}</div></details></div>`;
  }

  function readinessPanel() {
    const rows = records('readiness');
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">TRUST & COVERAGE</span><h2>무엇을 확인했는가</h2></div></div><div class="panel-content"><div class="readiness">${rows.length ? rows.map(row => `<div class="readiness-row"><span class="status-circle ${['pass','passed','ready','verified','available','succeeded','implemented'].includes(row.status) ? '' : 'pending'}">${['pass','passed','ready','verified','available','succeeded','implemented'].includes(row.status) ? '✓' : '·'}</span><div><h3>${esc(row.label)} ${badge(row.status)}</h3><p>${esc(row.detail)}</p></div></div>`).join('') : '<p class="muted">검증 수준 정보가 아직 등록되지 않았습니다.</p>'}</div></div><div class="panel-footer">구현 확인 · 도구 간 비교 · 실물 검증을 구분합니다.</div></section>`;
  }
  function overviewView() { return caseUI.view(); }
  async function openCaseTarget({kind,review,record,plan,isCurrent}) {
    if(kind==='isp-study'){
      await ispStudyUI.selectStudy(record.id);
      if(state.view!=='overview' || !isCurrent())return;
      state.experimentMode='isp-study';location.hash='experiments';render();await ispStudyUI.activate();return;
    }
    const source=kind==='run'?record:null;
    const assemblyId=kind==='assembly'?record.id:source?.assembly_id || plan?.assembly_id;
    const test=source?.test_id || plan?.test_id || '';
    if(!review.assembly_ids.includes(assemblyId) || !findRecord('assemblies',assemblyId))throw new Error('이 사례의 고정 카메라 구성을 현재 작업공간에서 찾을 수 없습니다.');
    if(kind!=='assembly' && (!test || !findRecord('tests',test)))throw new Error('이 사례의 고정 시험을 현재 작업공간에서 찾을 수 없습니다.');
    if(kind==='run') {
      const current=findRecord('runs',record.id);
      if(!current || current.assembly_id!==assemblyId || current.test_id!==test)throw new Error('선택한 사례 근거와 현재 실행 기록이 일치하지 않습니다.');
      await workbenchData.load(current);
      if(state.view!=='overview' || !isCurrent())return;
      chooseRun(record.id);
    } else {
      state.assembly=assemblyId;state.run='';state.test=test;state.validationTest=test;
      state.compatibility=null;state.builderSensor='';state.builderLens='';
      if(kind==='plan') {
        state.runtime=plan.mode==='quick_estimate'?'quick_estimate_v1':'response_db_v1';
        const inputs=window.CameraCasePlanInputs(plan);
        state.exposure=inputs.exposure===null?'':String(inputs.exposure);state.seed=inputs.seed===null?'':String(inputs.seed);
        state.scene='';state.sceneSequence='';state.controlSequence='';
      }
    }
    state.caseContext={review_id:review.id,name:review.name,revision:review.revision,assembly_id:assemblyId,test_id:test,run_id:source?.id || '',plan:plan || null,runtime:state.runtime};
    state.experimentMode='camera';state.view=kind==='assembly'?'builder':'experiments';
    location.hash=state.view;render();
  }
  function caseConditionText(value) {
    if(value===null || value===undefined)return '—';
    if(Array.isArray(value))return value.map(caseConditionText).join(' · ');
    if(typeof value==='object')return Object.entries(value).map(([key,item])=>key+': '+caseConditionText(item)).join(' / ');
    return String(value);
  }
  function caseContextBanner() {
    const context=state.caseContext;if(!context)return '';
    const matches=state.assembly===context.assembly_id && (!context.test_id || state.test===context.test_id) && (!context.run_id || selectedRun()?.id===context.run_id) && (!context.plan || state.runtime===context.runtime);
    const inputs=context.plan?window.CameraCasePlanInputs(context.plan):null;
    const plan=context.plan?`<h3>${esc(context.plan.question)}</h3><p>${esc(context.plan.reason)}</p><dl class="case-facts">${Object.entries(context.plan.conditions).map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(caseConditionText(value))}</dd>`).join('') || '<dt>계획 조건</dt><dd>아직 기록하지 않음</dd>'}</dl><p>${inputs.exposure===null?'노출: 계획에서 자동 적용할 명시적 ms/s 값을 찾지 못했습니다. 직접 입력하세요.':Number(state.exposure)===inputs.exposure&&state.exposure!==''?'노출: 기록된 계획과 일치합니다.':'노출: 현재 실행 입력이 기록된 계획과 다릅니다.'} ${inputs.seed===null?'Seed: 직접 입력하세요.':Number(state.seed)===inputs.seed&&state.seed!==''?'Seed: 기록된 계획과 일치합니다.':'Seed: 현재 실행 입력이 기록된 계획과 다릅니다.'}</p><p>그 밖의 계획 조건은 자동으로 적용하지 않습니다. 고정 구성·시험과 실행 입력에서 직접 확인하세요. 새 결과는 새 리뷰에서 연결해야 합니다.</p>`:'';
    return `<div class="case-context" data-case-context="${esc(context.review_id)}"><strong>${esc(context.name)} · r${esc(context.revision)}에서 연 ${context.plan?'실험 준비':'고정 기록'}</strong><p>카메라 ${esc(context.assembly_id)}${context.test_id?' · 시험 '+esc(context.test_id):''}${context.run_id?' · 실행 '+esc(context.run_id):''}</p>${matches?'':'<p>현재 선택은 사례에서 연 고정 기록과 달라졌습니다.</p>'}${plan}<a class="button quiet" href="#overview">사례 개요로 돌아가기 →</a></div>`;
  }
  function refreshCaseContext() {
    const element=document.querySelector('[data-case-context]');
    if(element && state.caseContext?.plan)element.outerHTML=caseContextBanner();
  }
  function chartPanel(run, compare=false) {
    const summary = runSummary(run);
    const radius = array(summary.field_radius);
    const ratios = array(summary.channel_ratios);
    const valid = radius.length && ratios.length === radius.length;
    let series = [];
    if (valid) {
      series = [{name:'R/G', color:'#db8858', values:ratios.map(row => row?.[0])}, {name:'B/G', color:'#718e64', values:ratios.map(row => row?.[1])}];
      if (compare) {
        const other = completedRuns().find(item => item.id !== run.id && item.assembly_id !== run.assembly_id && item.test_id === run.test_id && item.plugin_id === run.plugin_id && JSON.stringify(runSummary(item).field_radius) === JSON.stringify(radius) && JSON.stringify(runSummary(item).conditions) === JSON.stringify(summary.conditions));
        if (other) series.push({name:`R/G · ${findRecord('assemblies', other.assembly_id)?.name || short(other.id)}`,color:'#db8858',dashed:true,values:array(runSummary(other).channel_ratios).map(row => row?.[0])}, {name:`B/G · ${findRecord('assemblies', other.assembly_id)?.name || short(other.id)}`,color:'#718e64',dashed:true,values:array(runSummary(other).channel_ratios).map(row => row?.[1])});
      }
    }
    const runOptions = completedRuns().length > 1 ? `<div class="form-field run-selector"><label class="visually-hidden" for="chart-run">표시할 실행 결과</label><select id="chart-run" data-select="run">${!run?'<option value="" selected>선택 구성의 계산 결과 없음</option>':run.status!=='succeeded'?`<option value="${esc(run.id)}" selected>${esc(runLabel(run))} · ${esc(labels[run.status] || run.status)}</option>`:''}${completedRuns().map(item => `<option value="${esc(item.id)}" ${item.id === run?.id ? 'selected' : ''}>${esc(runLabel(item))}</option>`).join('')}</select></div>` : '';
    const centre = ratios[0] || [];
    const edge = ratios[ratios.length-1] || [];
    const shift = i => centre[i] !== null && edge[i] !== null && centre[i] > 0 ? (edge[i]/centre[i]-1)*100 : null;
    return `<section class="panel"><div class="panel-header"><div><h2>주변부 색 응답</h2><p>${valid ? `${esc(findRecord('assemblies', run.assembly_id)?.name || run.name || '선택 실행')} · 노출 ${number(Number(summary.conditions?.exposure_s?.value)*1000,2)} ms · Gain ${number(summary.conditions?.gain?.value,2)} · 예상 RAW` : '정규화 상고별 R/G · B/G 비율'}</p></div>${runOptions || badge(run?.status || 'pending', run ? undefined : '계산 대기')}</div>${valid ? `<div class="panel-content" style="padding-bottom:0"><div class="chart-legend">${series.map(line => `<span><i class="legend-mark" style="background:${line.color};${line.dashed ? 'opacity:.55' : ''}"></i>${esc(line.name)}${line.dashed ? ' (점선)' : ''}</span>`).join('')}</div></div><div class="chart-wrap">${chartSvg(radius, series, '정규화 상고 · 중심 → 주변부', '채널 비율 (1)')}</div><p class="chart-caption">실선: 선택 구성${series.length > 2 ? ' · 점선: 동일 시험·실행 방식·노출·Gain·온도의 다른 구성' : ''}. 저장된 계산 배열을 표시합니다. 예측 불확실도는 미확립 상태입니다.</p><div class="metric-strip"><div class="metric"><div class="metric-label">주변부 R/G 변화 · 중심 대비</div><div class="metric-value">${number(shift(0),2)}<small>%</small></div></div><div class="metric"><div class="metric-label">주변부 B/G 변화 · 중심 대비</div><div class="metric-value">${number(shift(1),2)}<small>%</small></div></div><div class="metric"><div class="metric-label">중심 G · 이론 SNR</div><div class="metric-value">${number(summary.theoretical_snr_db?.[0]?.[1],1)}<small>dB</small></div></div></div><div class="panel-content" style="padding-top:0"><details class="data-detail" style="margin-top:0"><summary>상고별 계산 수치 · ${radius.length} 지점</summary><div class="table-wrap"><table><thead><tr><th>정규화 상고</th><th>R/G</th><th>B/G</th></tr></thead><tbody>${radius.map((field,i)=>`<tr><td>${number(field,3)}</td><td>${number(ratios[i]?.[0],5)}</td><td>${number(ratios[i]?.[1],5)}</td></tr>`).join('')}</tbody></table></div></details></div>` : `<div class="empty-chart"><div class="empty-icon">${icon('chart')}</div><h3>${run?.status === 'failed' ? '계산을 완료하지 못했습니다' : ['running','queued'].includes(run?.status) ? '실제 응답 배열을 계산하고 있습니다' : '첫 실험으로 설계 차이를 확인하세요'}</h3><p>${run?.status === 'failed' ? esc(typeof run.error === 'string' ? run.error : json(run.error)) : '분광 입력과 Lens·Sensor 응답 DB를 결합한 결과가 여기에 표시됩니다. 오른쪽에서 실험을 실행하세요.'}</p></div>`}<div class="panel-footer"><span>${valid ? `RUN ${esc(short(run.id))} · ${esc(when(run.created_at))}` : '응답 DB 계산 → RAW 채널 비율 → 독립 실측 비교'}</span>${valid ? `<div style="display:flex;gap:16px;flex-wrap:wrap"><button class="link-button" data-action="export-run" data-id="${esc(run.id)}">계산 근거 JSON ↗</button><a class="link-button" href="/api/v1/runs/${encodeURIComponent(run.id)}/bundle" download>원본 포함 ZIP ↓</a></div>` : '<span>Physical validation · 미확립</span>'}</div></section>`;
  }
  function chartSvg(xs, series, xLabel, yLabel) {
    const width=680, height=280, left=51, right=22, top=24, bottom=46;
    const all = series.flatMap(line => line.values).filter(value => typeof value === 'number' && Number.isFinite(value));
    const numericXs = xs.map(Number);
    if (!all.length || numericXs.some(value => !Number.isFinite(value))) return empty('표시 가능한 수치가 없습니다','원본 결과의 축과 유효 값을 확인하세요.');
    const minX = Math.min(...numericXs), maxX=Math.max(...numericXs);
    const lo=Math.min(...all), hi=Math.max(...all), pad=Math.max((hi-lo)*.2, Math.max(Math.abs(hi),1)*.015);
    const minY=lo-pad, maxY=hi+pad;
    const x = value => left+(Number(value)-minX)/(maxX-minX || 1)*(width-left-right);
    const y = value => top+(maxY-Number(value))/(maxY-minY)*(height-top-bottom);
    let svg=`<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(yLabel)}와 ${esc(xLabel)} 계산 곡선"><title>${esc(yLabel)} · ${esc(xLabel)}</title>`;
    for(let i=0;i<=4;i++) {
      const value=minY+(maxY-minY)*i/4, pos=y(value);
      svg+=`<line class="grid" x1="${left}" y1="${pos}" x2="${width-right}" y2="${pos}"/><text x="${left-13}" y="${pos+3}" text-anchor="end">${number(value,3)}</text>`;
    }
    for(let i=0;i<=4;i++) {
      const value=minX+(maxX-minX)*i/4;
      svg+=`<text x="${x(value)}" y="${height-bottom+22}" text-anchor="middle">${number(value,2)}</text>`;
    }
    series.forEach(line => {
      let drawing=''; let open=false;
      line.values.forEach((value,index) => {
        if(typeof value !== 'number' || !Number.isFinite(value) || xs[index] === undefined) {open=false;return;}
        drawing+=`${open?'L':'M'}${x(xs[index]).toFixed(2)},${y(value).toFixed(2)} `;open=true;
      });
      svg+=`<path d="${drawing}" fill="none" stroke="${line.color}" stroke-width="2" ${line.dashed ? 'stroke-dasharray="5 5" opacity=".6"' : ''}/>`;
      line.values.forEach((value,index) => {
        if(typeof value==='number' && Number.isFinite(value) && xs[index] !== undefined) svg+=`<circle cx="${x(xs[index])}" cy="${y(value)}" r="${line.dashed ? 2 : 3}" fill="white" stroke="${line.color}" stroke-width="1.5"><title>${esc(line.name)} · ${esc(xs[index])}: ${esc(value)}</title></circle>`;
      });
    });
    svg+=`<text class="axis-label" x="${left}" y="12">${esc(yLabel)}</text><text class="axis-label" x="${width/2}" y="${height-2}" text-anchor="middle">${esc(xLabel)}</text></svg>`;
    return svg;
  }
  function recentRunTable(limit=0) {
    const runs=limit ? records('runs').slice(0,limit) : records('runs');
    return `<section class="panel"><div class="panel-header"><div><h2>실험 기록</h2><p>구성·시험·실행 조건이 함께 남는 재현 가능한 기록</p></div><span class="stat-small">${records('runs').length} RUNS</span></div><div class="table-spacer"></div>${runs.length ? `<div class="table-wrap"><table><thead><tr><th>실험 / 구성</th><th>상태</th><th>생성 시각</th><th>근거</th></tr></thead><tbody>${runs.map(run => `<tr><td><button class="link-button" data-action="select-run" data-id="${esc(run.id)}"><strong>${esc(runLabel(run))}</strong></button><small>${esc(findRecord('assemblies',run.assembly_id)?.name || short(run.assembly_id))}</small></td><td>${badge(run.status)}</td><td>${esc(when(run.created_at))}</td><td>${recordButton('runs',run,'JSON ↗')}</td></tr>`).join('')}</tbody></table></div>` : empty('아직 실행한 실험이 없습니다','카메라 구성과 시험 조건을 선택해 첫 계산 근거를 만드세요.')}</section>`;
  }
  function assetsView() {
    const all=records('assets');
    const kinds=[...new Set(all.map(item => item.kind))];
    const filtered=all.filter(item => (state.assetFilter==='all' || item.kind===state.assetFilter) && json(item).toLowerCase().includes(state.search.toLowerCase()));
    return `${heading('ASSET & EVIDENCE REGISTRY','자산마다 조건과 출처가 있습니다.','설계·실측·계산을 분리하고, 조합에 사용한 정확한 자산 버전을 추적합니다.',go('factory','새 응답 모델 등록','primary'))}<div class="filters"><button class="filter-button ${state.assetFilter==='all'?'active':''}" data-action="asset-filter" data-filter="all">전체 ${all.length}</button>${kinds.map(kind => `<button class="filter-button ${state.assetFilter===kind?'active':''}" data-action="asset-filter" data-filter="${esc(kind)}">${esc(kind)}</button>`).join('')}<label class="visually-hidden" for="asset-search">자산 검색</label><input class="search-input" id="asset-search" type="search" placeholder="이름 · 출처 · 속성 검색" value="${esc(state.search)}"></div><section class="panel">${filtered.length ? `<div class="table-wrap"><table><thead><tr><th>자산</th><th>데이터 종류</th><th>출처 / 권리</th><th>버전</th><th>상세</th></tr></thead><tbody>${filtered.map(item => `<tr><td><strong>${esc(item.name)}</strong><small>${esc(item.kind)} · ${esc(short(item.id))}</small></td><td>${badge(item.source_kind)}</td><td>${esc(item.source_uri || '출처 기록 확인')}<small>${esc(item.license || '라이선스 미등록')}</small></td><td>r${esc(item.revision || 1)}<small>${esc(short(item.content_hash))}</small></td><td>${recordButton('assets',item,'출처와 조건 ↗')}</td></tr>`).join('')}</tbody></table></div>` : empty('검색 조건에 맞는 자산이 없습니다','다른 종류나 검색어로 다시 확인하세요.')}</section><div class="section-title"><h2>Camera Property Dictionary</h2><span class="stat-small">${records('property_dictionary').length} DEFINED PROPERTIES</span></div><section class="panel"><div class="table-wrap"><table><thead><tr><th>특성</th><th>단위 · 기준면</th><th>조건 축</th><th>정의</th></tr></thead><tbody>${records('property_dictionary').map(property => `<tr><td><strong>${esc(property.id || property.property_id)}</strong></td><td>${esc(property.unit)}<small>${esc(property.plane || property.reference_plane)}</small></td><td>${esc(array(property.axes).join(' · '))}</td><td>${esc(property.description)}</td></tr>`).join('')}</tbody></table></div></section>`;
  }
  function builderView() {
    const assembly=selectedAssembly();
    const modelIds=array(assembly?.model_ids);
    return `${caseContextBanner()}${heading('CAMERA BUILDER','연결하기 전에, 의미부터 확인합니다.','렌즈·센서·ISP의 버전을 고정하고 적용 범위, 기준면, 물리 효과의 중복을 검사합니다.',go('experiments','이 구성으로 실험','primary'))}<div class="two-columns"><div class="stack"><section class="panel"><div class="panel-header"><div><h2>카메라 구성 선택</h2><p>Registry에 저장된 조합의 고정 버전</p></div>${badge('draft','설계 탐색')}</div><div class="panel-content"><div class="form-field"><label for="builder-assembly">Assembly revision</label><select id="builder-assembly" data-select="assembly">${options('assemblies',assembly?.id)}</select></div>${pipeline(assembly)}<div class="inline-spacer"></div>${modelIds.map(id => {const model=findRecord('models',id);return `<div class="locked-reference"><span>${esc(model?.kind || 'Model')}</span><div><strong>${esc(model?.name || id)}</strong><br><code>${esc(id)}</code></div>${model?recordButton('models',model,'원본 ↗'):badge('unknown','모델 누락')}</div>`;}).join('')}${assembly ? details('고정된 Assembly 계약 보기',assembly) : ''}</div></section>${assemblyForm(assembly)}</div><div class="stack"><section class="panel"><div class="panel-header"><div><span class="eyebrow">COMPATIBILITY</span><h2>물리적 입출력 계약</h2><p>형식이 같아도 의미가 다르면 실행할 수 없습니다.</p></div></div><div class="panel-content"><div class="readiness">${[['스펙트럼 범위','입력·Lens·Sensor 파장 범위가 겹치는가'],['입사각 · 기준면','Lens가 전달하는 빛을 Sensor가 받을 수 있는가'],['물리 효과 중복','CFA·Microlens·Shading이 이중 반영되는가'],['RAW 입력 계약','CFA·Bit depth·Packing·ISP 경계가 맞는가']].map(([title,text]) => `<div class="readiness-row"><span class="status-circle pending">·</span><div><h3>${title}</h3><p>${text}</p></div></div>`).join('')}</div><div class="inline-spacer"></div><button class="button primary full" data-action="check-assembly" ${!assembly?'disabled':''}>구성 호환성 검사 ${icon('arrow')}</button><div class="inline-spacer"></div><div id="compatibility-result">${state.compatibility ? compatibilityResult(state.compatibility) : '<p class="muted">검사를 실행하면 서버의 검토 결과와 원인을 표시합니다.</p>'}</div></div></section>${readinessPanel()}${conditionSpacePanel(assembly)}${lfmCandidatePlanPanel(assembly)}</div></div>`;
  }
  function assemblyForm(base) {
    if (!base) return '';
    const names={lens:'Lens response',sensor:'Sensor response',isp:'ISP / Readout'};
    const handoff=state.builderHandoff?.base===base.id && state.builderHandoff?.lens===state.builderLens?state.builderHandoff:null;
    const contract={conditions:handoff?.conditions || base.conditions,spectral_range:base.spectral_range,raw_format:base.raw_format};
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">REUSE RESPONSE MODELS</span><h2>모델을 교체한 새 구성 만들기</h2><p>선택 구성의 시험 조건·분광 범위·RAW 계약을 이어받습니다.</p></div></div><div class="panel-content"><form data-form="assembly" data-base="${esc(base.id)}"><div class="form-field"><label for="assembly-name">새 구성 이름</label><input id="assembly-name" name="name" required maxlength="200" placeholder="예: Lens A + 센서 응답 모델 v2"></div>${['lens','sensor','isp'].map(kind=>`<div class="form-field"><label for="assembly-${kind}">${names[kind]}</label><select id="assembly-${kind}" name="${kind}" required>${records('models').filter(model=>model.kind===kind).map(model=>`<option value="${esc(model.id)}" ${(kind==='sensor' && state.builderSensor ? model.id===state.builderSensor : kind==='lens' && state.builderLens ? model.id===state.builderLens : array(base.model_ids).includes(model.id))?'selected':''}>${esc(model.name)} · r${esc(model.revision)}${model.kind==='lens'?' · '+esc(lensCapability(model)):''}</option>`).join('')}</select></div>`).join('')}<div class="callout neutral">등록한 새 응답 모델을 바로 선택할 수 있습니다. 저장 후 호환성 검사에서 기준면·효과 중복·유효 범위를 확인하세요.</div><div class="form-field"><label for="assembly-contract">촬영 조건 · 분광 범위 · RAW 계약 · JSON</label><textarea id="assembly-contract" name="contract" required spellcheck="false">${esc(state.builderContracts?.[base.id] ?? json(contract))}</textarea><p class="field-hint">${handoff?'Factory에서 등록한 정확한 적용 조건을 가져왔습니다. ':'기준 구성의 계약을 가져왔습니다. '}선택한 Lens·Sensor에 맞는 파장 범위와 실제 촬영 조건을 확인하세요. 표시된 JSON으로 새 구성을 저장하며 원본 구성은 유지합니다.</p></div><div class="inline-spacer"></div><button class="button primary full" type="submit">버전을 고정해 구성 저장 ${icon('arrow')}</button></form></div></section>`;
  }
  function compatibilityResult(result) { return `<div class="result-status"><div><h3>구성 검사 결과</h3><p>현재 선택한 모델 버전 기준</p></div>${badge(result.status)}</div><div class="inline-spacer"></div>${issues(result.issues)}${details('검사별 원본 결과',result)}`; }
  function conditionSpacePanel(assembly) {
    const report=state.conditionReport?.assembly_id===assembly?.id?state.conditionReport:null;
    const axis=state.conditionAxis,space=report?.axes?.[axis];
    const names={gain:'Gain (1)',exposure_s:'노출 (s)',temperature_c:'온도 (°C)'};
    const candidateRows=space?space.candidates.map(item=>`<div class="readiness-row" data-condition-value="${esc(item.value)}" data-condition-status="${esc(item.status)}"><span class="status-circle ${item.status==='compatible'?'pass':item.status==='blocked'?'fail':'pending'}">${item.status==='compatible'?'✓':item.status==='blocked'?'×':'·'}</span><div><h3>${esc(number(item.value,5))} ${esc(space.intersection?.unit || '')} · ${esc(item.status==='compatible'?'구성 적합':item.status==='blocked'?'구성 거절':'범위 미확인')}</h3><p>${item.blocking_models.length?`차단 모델: ${item.blocking_models.map(id=>esc(findRecord('models',id)?.name || id)).join(' · ')}`:'선언 범위 및 Composer 검사 통과'}</p></div></div>`).join(''):'';
    return `<section class="panel" id="condition-space-panel"><div class="panel-header"><div><span class="eyebrow">MODEL APPLICABILITY</span><h2>후보 조건 범위</h2><p>Lens·Sensor·ISP 선언 범위의 교집합과 차단 모델을 확인합니다.</p></div></div><div class="panel-content"><div class="form-row"><div class="form-field"><label for="condition-axis">조건 축</label><select id="condition-axis">${Object.entries(names).map(([key,label])=>`<option value="${key}" ${key===axis?'selected':''}>${esc(label)}</option>`).join('')}</select></div><div class="form-field"><label for="condition-values">후보 값 · 쉼표로 구분</label><input id="condition-values" value="${esc(state.conditionValues)}" placeholder="0.5, 1, 1.5"></div></div><button type="button" class="button secondary full" data-action="condition-space" ${!assembly?'disabled':''}>후보별 적용 범위 검사</button>${state.conditionError?`<div class="callout error" role="alert">${esc(state.conditionError)}</div>`:''}${space?`<div class="inline-spacer"></div><p class="list-note">교집합: ${space.intersection?`${esc(number(space.intersection.min,5))}–${esc(number(space.intersection.max,5))} ${esc(space.intersection.unit)}`:esc(space.space_status)} · 모델 선언 기준</p><div class="readiness">${candidateRows}</div>${details('모델별 선언 범위와 검사 원본',space)}`:''}<p class="list-note">구성 적합은 실행 가능성이나 물리 정확도가 아닙니다. PWM 정책과 실제 RAW는 별도로 확인합니다.</p></div></section>`;
  }
  function lfmCandidatePlanPanel(assembly) {
    const reasonLabel=code=>({outside_effective_pwm_exposure_lattice:'PWM 주기·노출 격자 밖',model_gain_domain_blocked:'게인 모델 적용 범위 밖',model_exposure_domain_blocked:'노출 모델 적용 범위 밖',outside_linear_readout_gain_implementation:'전기 readout 구현 범위 밖',analog_gain_model_roles_undeclared:'아날로그 gain에 대한 모델별 역할 선언 부족',joint_composer_out_of_domain:'모델 조합 적용 범위 밖',joint_composer_fail:'모델 조합 계약 위반',joint_composer_inconclusive:'모델 조합 근거 부족'}[code] || code);
    const runs=records('runs').filter(run=>run.assembly_id===assembly?.id&&run.plugin_id==='electrical_control_v1'&&run.status==='succeeded');
    const selected=runs.some(run=>run.id===state.lfmPlanRun)?state.lfmPlanRun:runs[0]?.id || '';
    const report=state.lfmPlanReport?.source_run_id===selected?state.lfmPlanReport:null;
    const rows=report?report.candidates.map(item=>`<div class="readiness-row" data-lfm-candidate="${esc(item.exposure_ns)}:${esc(item.gain)}" data-lfm-status="${esc(item.status)}"><span class="status-circle">${item.status==='blocked'?'×':item.status==='unknown'?'·':'✓'}</span><div><h3>${esc(number(item.exposure_ns/1e6,5))} ms · analog gain ${esc(number(item.gain,5))} · ${esc(item.status==='blocked'?'사전 거절':item.status==='unknown'?'확인 필요':'새 run preflight 후보')}</h3><p>${item.reasons.length?esc(item.reasons.map(reasonLabel).join(' · ')):'모델 범위와 PWM 노출 격자 통과'}</p></div></div>`).join(''):'';
    return `<section class="panel" id="lfm-candidate-plan-panel"><div class="panel-header"><div><span class="eyebrow">PWM · LFM CANDIDATE PREFLIGHT</span><h2>촬영 정책까지 후보 검사</h2><p>완료된 전기 AE run의 고정 모델·프로그램을 기준으로 비교합니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="lfm-plan-run">기준 실행</label><select id="lfm-plan-run" ${runs.length?'':'disabled'}>${runs.length?runs.map(run=>`<option value="${esc(run.id)}" ${run.id===selected?'selected':''}>${esc(runLabel(run))}</option>`).join(''):'<option value="">완료된 전기 AE 실행 없음</option>'}</select></div><div class="form-field"><label for="lfm-plan-draft">후보 · 한 줄에 노출 ms, analog gain</label><textarea id="lfm-plan-draft" rows="4" spellcheck="false" placeholder="4, 1&#10;8, 0.5&#10;8, 1">${esc(state.lfmPlanDraft)}</textarea></div><button type="button" class="button secondary full" data-action="lfm-candidate-plan" ${selected?'':'disabled'}>모델 범위와 PWM 격자 함께 검사</button>${state.lfmPlanError?`<div class="callout error" role="alert">${esc(state.lfmPlanError)}</div>`:''}${report?`<div class="inline-spacer"></div><p class="list-note">실행 정책 노출: ${esc(number(report.effective_exposure_grid_ns.minimum/1e6,5))}–${esc(number(report.effective_exposure_grid_ns.maximum/1e6,5))} ms · ${esc(report.effective_exposure_grid_ns.point_count)}개 설정 · 전기 digital gain ${esc(number(report.frozen_electrical_digital_gain,5))} 고정</p><div class="readiness">${rows}</div>${details('후보 계획 원본',report)}`:''}<p class="list-note">통과한 후보도 새 Control/Test 등록과 전체 Worker preflight가 필요합니다. 밝기 목표 달성·물리 LFM 성능은 미확립입니다.</p></div></section>`;
  }
  function experimentsView() {
    const run=selectedRun(),summary=runSummary(run),artifacts=array(run?.artifacts),replayReason=CameraRunReplayUnsupportedReason(summary,run?.plugin_id);
    const design=state.designExperiment||{available:false,schema:''};
    const designAction=design.available?`<a class="button secondary" href="/design-experiments.html">${design.schema==='camera_design_experiment_v2'?'등록 모델·장면으로 새 설계 실험 →':'고정 연구 예제로 새 실험 →'}</a>`:'<button class="button secondary" type="button" disabled>새 설계 실험 · 현재 작업공간 미지원</button>';
    const designNote=design.available?(design.schema==='camera_design_experiment_v2'?'등록 모델·장면 ID, 노출·gain 후보와 선택·평가 입력을 명시해 같은 작업공간에 저장합니다.':'이 작업공간은 구 연구 예제 계약을 제공합니다. 등록 모델·장면 선택은 R1 계약에서 지원합니다.'):'현재 실행기의 설계 실험 계약을 확인하지 못했습니다. 등록 모델·장면 기반 R1 실험은 지원 모드에서만 실행할 수 있습니다.';
    const r1Scope=design.available&&design.schema==='camera_design_experiment_v2'?'<div class="callout neutral">새 R1 설계 실험 경로의 지원 조합: 등록된 16×16 정적 분광 예제 → 고정 광학·전기 모델 → 독립 노출 → 정수 RAW → 고정 ISP RGB → 유한 후보 선택·저장. 이 경로는 HDR/DCG·LFM·프레임 간 상태·범용 최적화를 결합하지 않습니다. 정적 HDR/DCG는 별도 등록 프로그램·시험의 지원 계약을 확인하세요. 실물 예측 정확도는 검증되지 않았습니다.</div>':'';
    const top=caseContextBanner()+heading('EXPERIMENTS & COMPARISON','차이를 계산하고, 원인을 좁힙니다.','장면·모델·시험을 고정한 카메라 계산과 같은 RAW의 ISP 재처리를 연결합니다.')+r1Scope+`<section class="panel"><div class="panel-content"><strong>저장된 설계 결정 비교</strong><p>신호 우선과 움직임·RAW 채널비 유지에서 선택이 달라진 이유, 원본 RAW와 실패 사례를 확인합니다. 새 계산이나 재선택은 하지 않습니다.</p><a class="button secondary" href="/decision-review.html">두 결정과 적용 한계 보기 →</a><div class="inline-spacer"></div>${designAction}<p class="list-note">${designNote}</p></div></section>`+experimentModes();
    if(state.experimentMode==='study')return top+studyUI.view();
    if(state.experimentMode==='retained-isp')return top+retainedISPUI.view();
    if(state.experimentMode==='electrical-response')return top+electricalResponseUI.view();
    if(state.experimentMode==='isp-study')return top+ispStudyUI.view();
    if(state.experimentMode==='surrogate')return top+surrogateUI.view();
    if(state.experimentMode==='replay')return top+`<div class="two-columns"><div class="stack">${replayControls()}${advancedISPUI.editor()}</div><div class="stack">${replayResults()}${advancedISPUI.result(findRecord('replays',state.replayJob) || records('replays')[0])}</div></div>`;
    return `${top}<div class="content-grid"><div class="stack">${cameraPlot(run,true)}${nativeSitePanel(run)}${chargeCollectionPanel(run)}${run?.status==='succeeded'?uncertaintyPanel(run):''}${run?.status==='succeeded'?`<section class="panel"><div class="panel-header"><div><h2>이 결과가 설명하는 범위</h2><p>사용한 Runtime · 지원 물리 · 저장된 원본</p></div>${badge(summary.physical_validation || 'not_established','실물 검증 미확립')}</div><div class="panel-content">${details('계산 가정 · 제한 사항',summary.limitations || [])}${details('입력 조건 · 단위 · 포함된 효과',summary)}${details('전체 실행 원본',run)}${['spatial_scene_v1','temporal_scene_v1','hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(runtimeId(run.plugin_id))?'':imagePreviews(artifacts)}<div class="inline-spacer"></div><button class="button secondary" data-action="replay-from-run" data-id="${esc(run.id)}" ${replayReason||!artifacts.some(artifact=>['raw_frames.npy','raw_groups.npy'].includes(artifact.name))?'disabled':''}>이 RAW를 다른 ISP로 재처리 →</button>${replayReason?`<p class="list-note">${esc(replayReason)}</p>`:''}${['retained_charge_v1','electrical_control_v1'].includes(runtimeId(run.plugin_id))?`<button class="button secondary" data-action="retained-isp-from-run" data-id="${esc(run.id)}">${runtimeId(run.plugin_id)==='electrical_control_v1'?'실제 AE RAW로 ISP 비교 →':'잔류 RAW로 시간 ISP 비교 →'}</button>`:''}</div>${artifactLinks(artifacts)}</section>`:''}${recentRunTable()}</div><div class="stack">${runControls(true)}${!isSceneRuntime()?`<section class="panel"><div class="panel-header"><div><span class="eyebrow">FLAT-FIELD BASELINE</span><h2>기준 렌즈·노출 비교</h2></div></div><div class="panel-content"><p class="list-note">별도 기준 예제입니다. 등록된 기준 두 렌즈를 사용하며 선택 사례의 실행이나 근거 연결을 대신하지 않습니다.</p><button class="button secondary full" data-action="compare-assemblies">예제 · 기준 렌즈 A/B 비교 →</button><div class="inline-spacer"></div><button class="button secondary full" data-action="sweep">예제 · 4 ms · 8 ms × 기준 렌즈 실행 →</button></div></section>`:''}</div></div>`;
  }

  function uncertaintyPanel(run) {
    const summary=runSummary(run),prediction=summary.prediction || run?.result?.prediction || {};
    const budget=prediction.uncertainty_budget || summary.uncertainty_budget;
    if(!budget)return '';
    const ranges=budget.conditional_intervals?.channel_ratios;
    const fields=array(budget.axes).find(axis=>axis.name==='field_radius')?.values || [];
    const unknown=array(budget.unknown_contributions),known=array(budget.propagated_paths);
    const interval=(row,column)=>ranges?.defined?.[row]?.[column]?`${number(ranges.lower[row][column],6)} – ${number(ranges.upper[row][column],6)}`:'범위 미정 · 분모 0 포함';
    return `<section class="panel uncertainty-panel" data-uncertainty-run="${esc(run.id)}"><div class="panel-header"><div><span class="eyebrow">INPUT BOUNDS → PREDICTION</span><h2>입력 오차가 결과에 미치는 범위</h2><p>${known.length}개 입력 경계 반영 · ${unknown.length}개 미확인 항목${budget.observation?' · 등록된 ROI의 CFA별 평균 후 비율':' · 모델 상고별 기대값'}</p></div>${badge('unknown','전체 불확실도 미확인')}</div><div class="panel-content"><div class="callout">${budget.status==='not_propagated'?'이 계산 방식에는 등록된 입력 경계의 전파가 아직 연결되지 않았습니다.':'아래 범위는 선언한 입력 경계를 반영한 연속 기대값입니다. 경계가 없는 입력은 이 계산에서 기준값으로 고정합니다.'} 미확인 모델 오차가 남아 있어 실물 예측의 오차 경계로 사용할 수 없습니다.</div>${ranges?`<div class="inline-spacer"></div><div class="table-wrap"><table class="uncertainty-ranges"><thead><tr><th>정규화 상고</th><th>R/G 범위 · 1</th><th>B/G 범위 · 1</th></tr></thead><tbody>${ranges.lower.map((row,i)=>`<tr><td>${fields[i]===undefined?'표본 '+(i+1):number(fields[i],4)}</td><td>${interval(i,0)}</td><td>${interval(i,1)}</td></tr>`).join('')}</tbody></table></div>`:''}<div class="inline-spacer"></div><details class="data-detail"><summary>미확인 항목 ${unknown.length}개</summary><ul>${unknown.map(item=>`<li><strong>${esc(item.path)}</strong> · ${esc(item.reason)}</li>`).join('')}</ul></details>${details('반영한 입력 경계 · 원본 모델/시험',budget.sources || {})}${details('계산 방법 · 조건부 구간 원본',budget)}</div></section>`;
  }

  function imagePreviews(artifacts,context={}) {
    const images=artifacts.filter(artifact=>['image/png','image/jpeg'].includes(artifact.media_type) && safeArtifactUrl(artifact.url));
    if(!images.length)return '';
    return `<div class="inline-spacer"></div><p class="muted">${context.kind==='hdr' && context.summary?.hdr_merge_applied===true?'고정 native 센서 크롭의 톤 처리 미리보기입니다. 선형 HDR 전자율과 원본 RAW·마스크는 별도 산출물입니다.':context.kind==='electrical' || context.kind==='hdr'?'고정 native 센서 크롭의 개별 RAW 처리 미리보기입니다. HDR 합성 영상이 아니며 프레임별 전기·PWM 진단을 함께 확인하세요.':context.kind==='temporal'?'선택 프레임의 실제 저장 영상입니다. 원본 프레임 ID와 적용 설정·시간 기준을 함께 확인하세요.':context.kind==='spatial'?'장면에 선언된 native sensor crop의 공간 배열입니다. 좌표·Sampling·각 배열의 물리 단위는 저장된 계약을 확인하세요.':context.kind==='replay'?'보존된 RAW를 처리한 영상입니다. 원본 Geometry와 취득 조건을 유지하며 새 촬영을 의미하지 않습니다.':'이 영상은 시야 전체를 정규화한 대표 격자입니다. 각 셀의 전하는 native pixel 면적 기준이며 연속된 실제 센서 크롭이나 전체 해상도 영상이 아닙니다.'}</p><div class="inline-spacer"></div><div class="preview-grid ${images.length===1?'single-preview':''}">${images.map(artifact=>`<figure><img class="preview-image" src="${esc(safeArtifactUrl(artifact.url))}" alt="${esc(artifact.name)} · 계산 산출물" loading="lazy"><figcaption>${esc(artifact.name)} · 실제 저장된 계산 산출물<span data-preview-size>저장 영상 크기 확인 중 · 화면 표시만 확대</span></figcaption></figure>`).join('')}</div>`;
  }
  function updatePreviewCaption(image) {
    if(!image?.matches?.('.preview-image') || !image.naturalWidth)return;
    const caption=image.closest('figure')?.querySelector('[data-preview-size]');
    if(caption)caption.textContent=`저장 영상 ${image.naturalWidth} × ${image.naturalHeight} px · 화면 표시 ${number(image.clientWidth/image.naturalWidth,2)}× · 원본 픽셀 변경 없음`;
  }
  document.addEventListener('load',event=>updatePreviewCaption(event.target),true);
  window.addEventListener('resize',()=>document.querySelectorAll('.preview-image').forEach(updatePreviewCaption));
  function safeArtifactUrl(url) {
    try {const parsed=new URL(url,location.origin);return parsed.origin===location.origin && parsed.pathname.startsWith('/api/v1/') ? parsed.href : null;} catch {return null;}
  }
  function editor(kind) {
    const measurement=kind==='measurement';
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">${measurement?'MEASUREMENT INGEST':'IMMUTABLE MODEL REVISION'}</span><h2>${measurement?'측정 데이터 연결':'재사용할 응답 모델 등록'}</h2><p>${measurement?'원본·시험 조건·측정 불확실도를 함께 보존합니다.':'새 응답 테이블을 조건·출처·적용 범위와 함께 등록합니다.'}</p></div></div><div class="panel-content"><form data-form="${kind}"><input type="file" id="${kind}-file" class="hidden-input" accept=".json,application/json" data-upload="${kind}"><label class="upload-zone" for="${kind}-file" data-drop="${kind}">${icon('upload')}<strong>JSON 파일을 선택하거나 여기에 놓으세요</strong><small>원본 데이터 파일 · 최대 1 MB</small></label><div class="form-toolbar"><label for="${kind}-json">입력 데이터 · JSON</label><button type="button" class="button quiet" data-action="template" data-kind="${kind}">입력 예제 불러오기 ↙</button>${measurement?'':`<button type="button" class="button quiet" data-action="isp-profile-template">ISP 프로파일 형식 ↙</button><label for="native-sensor-base">4사이트 응답의 기준 Sensor</label><select id="native-sensor-base">${records('models').filter(m=>m.kind==='sensor').map(m=>`<option value="${esc(m.id)}" ${state.nativeSensorBase===m.id?'selected':''}>${esc(m.name)} · r${esc(m.revision)}</option>`).join('')}</select><button type="button" class="button quiet" data-action="native-pixel-template" ${!records('models').some(m=>m.kind==='sensor')?'disabled':''}>Native 4사이트 형식 ↙</button>`}</div><div class="form-field"><textarea id="${kind}-json" name="document" required spellcheck="false" placeholder="입력 예제를 불러온 뒤 실제 데이터와 조건으로 채워 주세요." aria-label="${measurement?'측정 데이터':'응답 모델'} JSON">${esc(measurement?state.measurementDraft:state.modelDraft)}</textarea></div><div class="callout ${measurement?'':'neutral'}">${measurement?'입력 예제는 실제 계측 데이터가 아닙니다. 실제 장비·Serial·시험 조건·출처·불확실도를 확인하고 데이터 종류를 정확히 지정하세요.':'원본 자산을 덮어쓰지 않고 새 모델 버전을 만듭니다. 응답 테이블과 물리 계약을 검사한 후 Registry에 저장합니다.'}</div><div class="inline-spacer"></div><button type="submit" class="button primary full">${measurement?'데이터 등록':'응답 모델 등록'} ${icon('arrow')}</button></form></div></section>`;
  }
  function captureContext(id) {
    const capture=findRecord('captures',id);
    const measurement=records('measurements').find(item=>item.raw_capture_id===id && item.id===state.measurement) || records('measurements').find(item=>item.raw_capture_id===id);
    const test=findRecord('tests',measurement?.test_id);
    return capture && measurement && test ? {capture,measurement,test} : null;
  }
  function chooseCapture(context) {
    if(!context)throw new Error('RAW 원본에 연결된 시험과 Dataset을 찾을 수 없습니다. 새로고침 후 다시 선택하세요.');
    state.lastCapture=context;
    state.assembly=context.capture.assembly_id;
    state.test=context.test.id;
    state.validationTest=context.test.id;
    state.measurement=context.measurement.id;
    state.run=completedRuns().find(item=>item.assembly_id===state.assembly && item.test_id===state.test)?.id || '';
    if(state.run)chooseRun(state.run);
    else if(context.capture.capture_kind==='control_response_observation_v1')state.runtime='closed_loop_scene_v1';
    state.validation=null;state.allowCalibrated=false;
    const exposure=Number(context.test.conditions?.exposure_s?.value)*1000;
    if(Number.isFinite(exposure))state.exposure=String(exposure);
  }
  function capturePanel() {
    const imported=state.lastCapture;
    const fileField=(kind,title,help)=>{const file=state[kind==='raw'?'captureRaw':'captureDark'];return `<div class="form-field capture-file"><label for="capture-${kind}">${title}</label><input id="capture-${kind}" type="file" accept=".npy" data-capture-file="${kind}" aria-describedby="capture-${kind}-info"><small id="capture-${kind}-info">${file?`${esc(file.name)} · ${number(file.size/1024,1)} KiB 선택됨`:help}</small>${file?`<button type="button" class="link-button" data-action="clear-capture-file" data-kind="${kind}">선택 해제</button>`:''}</div>`;};
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">RAW → PINNED ROI OBSERVATION</span><h2>RAW 원본으로 측정 데이터 만들기</h2><p>원본 배열·관측 방법·ROI 시험을 함께 고정합니다.</p></div><span class="badge">NPY</span></div><div class="panel-content"><form data-form="capture">${fileField('raw','RAW NPY · 필수','선형 RGGB 정수 배열 · C 순서 [N,H,W]')}${fileField('dark','동일 조건 Dark NPY · 선택','보정 제안에는 matched dark가 필요합니다.')}<p class="list-note">RAW + Dark 합계 최대 64 MiB. Dark를 연결하면 observation.dark_conditions에 실제 노출·Gain·온도 조건을 명시하세요.</p><div class="form-toolbar"><label for="capture-json">취득 메타데이터 · JSON</label><button type="button" class="button quiet" data-action="template" data-kind="capture">입력 형식 불러오기 ↙</button></div><div class="form-field"><textarea id="capture-json" name="metadata" required spellcheck="false" placeholder="형식을 불러와 실제 assembly_id · test_id · 취득 조건 · ROI를 입력하세요.">${esc(state.captureDraft)}</textarea></div><div class="callout">제공 형식은 추정 데이터용 64×64 대표 격자입니다. 실물 측정은 실제 native sensor 형상·장비·Serial·교정 버전·출처가 필요합니다. 형식 예제에 measured만 지정해 실측으로 바꿀 수 없습니다.</div><div class="inline-spacer"></div><button type="submit" class="button primary full" ${!state.captureRaw?'disabled':''}>${icon('upload')}원본 보존 및 ROI Dataset 생성</button></form>${imported?`<div class="inline-spacer"></div><div class="capture-next"><span class="eyebrow">RETURNED OBSERVATION TEST</span><h3>${esc(imported.measurement.name)}</h3><p>Dataset과 ROI 시험을 선택했습니다. 같은 ROI 관측 방법으로 다시 계산해야 비교할 수 있습니다.</p><dl class="reference-pairs"><dt>고정 시험</dt><dd>${esc(imported.test.id)}</dd><dt>Dataset</dt><dd>${esc(imported.measurement.id)}</dd></dl><button class="button secondary full" data-action="run-capture" data-id="${esc(imported.capture.id)}">이 ROI 시험으로 계산 ${icon('play')}</button></div>`:''}${records('captures').filter(item=>item.schema!=='camera_nonlinear_capture_v1').length?`<details class="data-detail"><summary>등록된 RAW 취득 기록 · ${records('captures').filter(item=>item.schema!=='camera_nonlinear_capture_v1').length}개</summary><div class="capture-history">${records('captures').filter(item=>item.schema!=='camera_nonlinear_capture_v1').map(item=>`<div class="locked-reference"><div><strong>${esc(item.name)}</strong><br>${badge(item.source_kind)}<small>${esc(array(item.raw_shape).join(' × '))}${item.dark_shape?' · Dark 연결':' · Dark 없음'}</small></div><div class="compact-actions">${recordButton('captures',item,'원본 ↗')}<button class="link-button" data-action="select-capture" data-id="${esc(item.id)}">ROI 비교 선택</button><button class="link-button" data-action="replay-from-capture" data-id="${esc(item.id)}">이 RAW 재처리 →</button></div></div>`).join('')}</div></details>`:''}</div></section>`;
  }
  function calibrationBaseModel() {
    const base=findRecord('models',state.calibrationBase) || array(selectedAssembly()?.model_ids).map(id=>findRecord('models',id)).find(item=>item?.kind==='sensor') || records('models').find(item=>item.kind==='sensor');
    if(base)state.calibrationBase=base.id;
    return base;
  }
  function calibrationEligibility(measurement,base) {
    if(!measurement.raw_capture_id)return 'RAW 취득 원본 연결 필요';
    if(!array(findRecord('assemblies',measurement.assembly_id)?.model_ids).includes(base?.id))return '기준 센서 버전이 다름';
    if(findRecord('tests',measurement.test_id)?.observation_method?.dark_correction!=='mean_dark_frame')return '동일 조건 Dark 보정 필요';
    return '';
  }
  function calibrationPanel() {
    const base=calibrationBaseModel();
    const theta=array(base?.payload?.sensor_angular_response?.theta_deg).slice(1);
    const eligible=records('measurements').filter(item=>!calibrationEligibility(item,base));
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">BOUNDED CALIBRATION PROPOSAL</span><h2>실측에서 센서 보정 후보 만들기</h2><p>선택한 범위 안에서 R·B 각도 응답만 조정합니다.</p></div>${badge('unapproved')}</div><div class="panel-content"><form data-form="calibration"><div class="form-field"><label for="calibration-base">기준 센서 모델 · 원본 유지</label><select id="calibration-base" name="base_sensor_model_id" required>${records('models').filter(item=>item.kind==='sensor').map(item=>`<option value="${esc(item.id)}" ${base?.id===item.id?'selected':''}>${esc(item.name)} · r${esc(item.revision)}</option>`).join('')}</select></div><div class="form-row"><div class="form-field"><label for="calibration-name">후보 이름</label><input id="calibration-name" name="name" value="${esc(state.calibrationName)}" required maxlength="200" placeholder="예: Sensor A · R/B 각도 보정"></div><div class="form-field"><label for="calibration-revision">새 버전</label><input id="calibration-revision" name="revision" value="${esc(state.calibrationRevision)}" required maxlength="80"></div></div><fieldset class="dataset-choices"><legend>보정용 Dataset · 최대 32개</legend>${records('measurements').length?records('measurements').map(item=>{const reason=calibrationEligibility(item,base);return `<label class="check-option ${reason?'unavailable-choice':''}"><input type="checkbox" name="measurement_ids" value="${esc(item.id)}" data-calibration-measurement ${state.calibrationMeasurements.includes(item.id) && !reason?'checked':''} ${reason?'disabled':''}><span><strong>${esc(item.name || short(item.id))}</strong><small>${esc(reason || `${labels[item.source_kind] || item.source_kind} · RAW + Dark 연결`)} · ${esc(String(item.id).slice(-8))}</small></span></label>`;}).join(''):'<p class="list-note">등록된 Dataset이 없습니다. Validation에서 RAW와 동일 조건 Dark를 먼저 연결하세요.</p>'}</fieldset><div class="form-toolbar"><label for="calibration-bounds">명시적 배율 범위 · JSON</label><button type="button" class="button quiet" data-action="calibration-bounds" ${!theta.length?'disabled':''}>±20% 형식 예제 ↙</button></div><p class="list-note">각도 순서: ${theta.length?theta.map(value=>`${number(value)}°`).join(' · '):'지원되는 각도 노드 없음'}. R/B 각각 min·max 배열과 unit: "1"을 입력하세요. G와 0° 기준 응답은 고정됩니다.</p><div class="form-field"><textarea id="calibration-bounds" name="parameter_bounds" required spellcheck="false" placeholder='{"R":{"min":[...],"max":[...],"unit":"1"},"B":{"min":[...],"max":[...],"unit":"1"}}'>${esc(state.calibrationBounds)}</textarea></div><div class="callout">범위 예제는 물리적으로 입증된 허용 오차가 아닙니다. 식별 가능성·포화·선형성·출처를 검사하며, 거부 사유도 보존합니다. 생성된 후보는 미승인 상태이며 별도의 독립 검증이 필요합니다.</div><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!eligible.length || !theta.length?'disabled':''}>범위를 검사하고 보정 제안 ${icon('arrow')}</button></form></div></section>`;
  }
  function calibrationResultPanel() {
    const reports=records('calibrations');
    const report=findRecord('calibrations',state.calibrationReport);
    if(!reports.length)return `<section class="panel"><div class="panel-header"><h2>보정 제안 결과</h2></div>${empty('아직 보정 제안이 없습니다','원본 센서와 RAW 관측을 고정한 제안 결과가 여기에 표시됩니다. 훈련 잔차의 감소와 독립 실물 검증은 구분합니다.')}</section>`;
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">RECORDED PROPOSAL</span><h2>저장된 보정 제안</h2><p>기록에 고정된 입력과 후보 버전 기준입니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="calibration-report">표시할 보정 기록</label><select id="calibration-report"><option value="">기록 선택</option>${reports.map(item=>`<option value="${esc(item.id)}" ${report?.id===item.id?'selected':''}>${esc(item.name)} · ${esc(labels[item.status] || item.status)} · ${esc(String(item.id).slice(-6))}</option>`).join('')}</select></div>${report?`<div class="result-status"><div><h3>${esc(report.name)}</h3><p>${esc(findRecord('models',report.base_sensor_model_id)?.name || report.base_sensor_model_id)}</p></div>${badge(report.status)}</div><div class="inline-spacer"></div><div class="callout">${badge(report.approval_status || 'unapproved')} 독립 검증 미완료 · ${report.status==='PROPOSED'?'새 후보 모델을 보존했습니다. 훈련 데이터와 별개의 실측으로 검증하세요.':'후보를 만들지 않았습니다. 거부 사유와 입력 범위를 확인하세요.'}</div><div class="inline-spacer"></div>${issues(report.issues)}${report.candidate_model_id?`<div class="locked-reference"><div><strong>새 센서 후보 · r${esc(report.revision)}</strong><br><code>${esc(report.candidate_model_id)}</code></div>${recordButton('models',{id:report.candidate_model_id},'모델 계약 ↗')}</div><div class="inline-spacer"></div><button class="button secondary full" data-action="use-calibration-candidate" data-id="${esc(report.id)}">후보를 선택해 새 카메라 구성 ${icon('arrow')}</button>`:''}${array(report.parameters).length?`<details class="data-detail"><summary>각도 노드별 허용 범위와 제안 배율</summary><div class="table-wrap"><table><thead><tr><th>채널 · 각도</th><th>요청 범위</th><th>유효 상한</th><th>배율</th></tr></thead><tbody>${report.parameters.map(p=>`<tr><td>${esc(p.channel)} · ${number(p.theta_deg)}°</td><td>${number(p.requested_min)} – ${number(p.requested_max)}</td><td>${number(p.effective_max)}</td><td>${number(p.scale,5)}</td></tr>`).join('')}</tbody></table></div></details>`:''}${details('보정 Dataset · 식별 가능성 · 훈련 잔차 · 계보',report)}</div>`:'<p class="list-note">과거 기록을 선택하면 그 제안의 고정된 입력과 결과를 확인할 수 있습니다.</p></div>'}</section>`;
  }
  function validationView() {
    const reports=records('validations');
    const currentRun=selectedRun();
    const comparisonTest=state.validationTest || state.test || testId();
    const matches=record=>record && record.run_id===currentRun?.id && record.measurement_id===state.measurement && record.test_id===comparisonTest && Boolean(record.allow_calibrated_model)===state.allowCalibrated;
    const report=matches(state.validation)?state.validation:reports.find(matches);
    return `${heading('VALIDATION CENTER','예측과 실측 사이의 간격을 확인합니다.','조건 정합 → 적용 범위 → 잔차 → 불확실도 → 독립 검증. 근거가 부족하면 판정을 보류합니다.',badge('pending',`${records('measurements').length} 측정 데이터`)+`<a class="button secondary" href="/calibration-lab.html">차트 기반 보정 Lab ↗</a>`)}<div class="two-columns"><div class="stack">${nonlinearCaptureUI.view()}${readoutFittingUI.view()}${responseImportUI.view()}${captureAnalysisUI.view()}${capturePanel()}${editor('measurement')}<section class="panel"><div class="panel-header"><div><h2>등록된 측정 데이터</h2><p>관측 원본과 보정 모델은 별도로 보존합니다.</p></div></div><div class="table-spacer"></div>${records('measurements').length?`<div class="table-wrap"><table><thead><tr><th>Dataset</th><th>출처 종류</th><th>근거</th></tr></thead><tbody>${records('measurements').map(item=>`<tr><td><strong>${esc(item.name || short(item.id))}</strong><small>${esc(item.physical_instance || item.metric_id || '')}</small></td><td>${badge(item.source_kind)}</td><td>${recordButton('measurements',item,'원본 ↗')}</td></tr>`).join('')}</tbody></table></div>`:empty('연결된 실측이 없습니다','예제 계산 결과만으로 실물 예측 성능을 확인할 수 없습니다. 대응하는 실측 데이터셋을 등록하세요.')}</section></div><div class="stack"><section class="panel"><div class="panel-header"><div><span class="eyebrow">MATCHED COMPARISON</span><h2>실행 결과와 측정 비교</h2></div></div><div class="panel-content"><form data-form="validation"><div class="form-field"><label for="validation-run">Simulation Run</label><select id="validation-run" name="run_id" required><option value="">완료된 계산 선택</option>${completedRuns().map(item=>`<option value="${esc(item.id)}" ${item.id===currentRun?.id?'selected':''}>${esc(runLabel(item))}</option>`).join('')}</select></div><div class="form-field"><label for="validation-measurement">Measurement Dataset</label><select id="validation-measurement" name="measurement_id" required>${options('measurements',state.measurement,'등록한 측정 데이터 선택')}</select></div><div class="form-field"><label for="validation-test">공통 Test Specification</label><select id="validation-test" name="test_id" required>${options('tests',comparisonTest)}</select></div><label class="check-option" for="validation-calibrated"><input id="validation-calibrated" type="checkbox" name="allow_calibrated_model" ${state.allowCalibrated?'checked':''}><span><strong>같은 하드웨어의 보정 센서 버전 비교 허용</strong><small>등록된 보정 계보와 동일 Lens·ISP·센서 자산을 검사합니다. 독립 데이터·불확실도 검사는 유지됩니다.</small></span></label><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!completedRuns().length || !records('measurements').length?'disabled':''}>조건 검사 및 잔차 비교 ${icon('arrow')}</button></form></div></section><div id="validation-result">${report?validationResult(report):`<section class="panel">${empty('선택한 Run·Dataset·시험의 검증 근거가 없습니다','같은 시험 조건의 독립 측정과 알려진 불확실도가 필요합니다. 다른 조합의 과거 판정을 재사용하지 않습니다.')}</section>`}</div>${reports.length?`<section class="panel"><div class="panel-header"><h2>검증 기록</h2></div><div class="panel-content">${reports.map(item=>`<div class="locked-reference"><div><strong>${esc(item.name || short(item.id))}</strong><br>${badge(item.status)}</div>${recordButton('validations',item,'검증 근거 ↗')}</div>`).join('')}</div></section>`:''}</div></div>`;
  }
  function validationResult(record) {
    const report=record.report || record;
    const summary=report.summary || {};
    return `<section class="panel"><div class="panel-header"><div><h2>비교 결과</h2><p>${esc(short(record.id || report.prediction_id))}</p></div>${badge(report.status || record.status)}</div><div class="panel-content">${issues(report.issues || report.reasons)}${summary.sample_count?`<div class="inline-spacer"></div><div class="three-columns"><div class="metric"><div class="metric-label">RMSE</div><div class="metric-value">${number(summary.rmse,4)}</div></div><div class="metric"><div class="metric-label">최대 절대 잔차</div><div class="metric-value">${number(summary.max_abs_error,4)}</div></div><div class="metric"><div class="metric-label">비교 표본</div><div class="metric-value">${number(summary.sample_count,0)}</div></div></div>`:''}${residualChart(report)}${details('조건 정합 · 불확실도 · 판정 근거',record)}</div></section>`;
  }
  function residualChart(report) {
    if(report.response_comparison)return window.CameraResponseObservationReport(report,{esc,number});
    const residual=report.residuals;
    if(!residual?.values?.length) return '';
    const axes=array(residual.axes), field=axes.find(axis=>axis.name==='field_radius');
    const values=array(residual.values).flat(Infinity);
    if(!field || values.length!==field.values.length*2) return '';
    return `<div class="inline-spacer"></div>${chartSvg(field.values,[{name:'R/G 잔차',color:'#d68d5b',values:values.filter((_,i)=>i%2===0)},{name:'B/G 잔차',color:'#7e9e67',values:values.filter((_,i)=>i%2===1)}],'정규화 상고','예측 − 측정 (1)')}<p class="muted">주황 R/G · 녹색 B/G. 잔차 자체와 불확실도를 반영한 판정은 구분합니다.</p>`;
  }
  function selectedFactoryJob() {
    return findRecord('factory_jobs',state.factoryJob) || records('factory_jobs')[0];
  }
  function factoryRequestPanel() {
    const plugin=state.factoryPlugins.find(item=>item.plugin_id===state.factoryPlugin);
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">DETAILED CHARACTERIZATION</span><h2>설계에서 재사용할 응답 만들기</h2><p>Solver 작업·수치 설정·원본 산출물을 고정합니다.</p></div>${badge(plugin?.available?'available':'unavailable')}</div><div class="panel-content">${state.factoryError?`<div class="callout error">Factory API 연결을 확인하세요. ${esc(state.factoryError)}</div><div class="inline-spacer"></div>`:''}<form data-form="factory-job"><div class="form-field"><label for="factory-plugin">해석 방법</label><select id="factory-plugin" required><option value="">Solver 선택</option>${state.factoryPlugins.map(item=>`<option value="${esc(item.plugin_id)}" ${item.plugin_id===state.factoryPlugin?'selected':''}>${esc(item.name || item.plugin_id)}${item.available?'':' · 미연결'}</option>`).join('')}</select></div>${plugin?`<div class="callout neutral"><strong>${esc(plugin.name || plugin.plugin_id)}</strong><br>${esc(plugin.reason || (plugin.available?'실행 가능한 Backend가 연결되어 있습니다.':'현재 Backend를 사용할 수 없습니다.'))}</div>${details('지원 범위 · 수치/자원 제한',plugin)}`:''}<div class="inline-spacer"></div><div class="form-toolbar"><label for="factory-json">설계·재료·출처·해석 조건 · JSON</label><button class="button quiet" type="button" data-action="factory-template" ${!plugin?'disabled':''}>Solver 입력 형식 ↙</button>${['rayoptics_lens_v1','prescription_psf_v1'].includes(state.factoryPlugin)?'<button class="button quiet" type="button" data-action="factory-template" data-request-version="2">동공 투과율 포함 형식 ↙</button><button class="button quiet" type="button" data-action="factory-template" data-request-version="3">4사이트·방위각 연결 형식 ↙</button><button class="button quiet" type="button" data-action="factory-template" data-request-version="4">비구면 처방 · 4사이트 연결 형식 ↙</button><button class="button quiet" type="button" data-action="factory-template" data-request-version="5">유한 물체면 · 거리·배율 고정 형식 ↙</button>':''}</div><div class="form-field"><textarea id="factory-json" name="document" required spellcheck="false" placeholder="서버의 입력 형식을 불러와 실제 설계·재료·자산 ID와 출처를 확인하세요.">${esc(state.factoryDrafts[state.factoryPlugin] || '')}</textarea></div><p class="list-note">UTF-8 최대 1 MB. 요청에는 설계·물리·수치 조건을 기록합니다. 실행 장치와 Solver 실행 환경은 서버에서 관리합니다.</p><div class="callout">해석 완료와 실물 예측 검증은 다릅니다. 수렴성·지원 범위·불확실도를 확인한 후 모델을 등록하세요. Meep의 광흡수 결과는 수집전자 QE가 아니며, 센서 모델로 자동 변환하지 않습니다.</div><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!plugin?.available?'disabled':''}>${icon('play')}해석 작업 제출</button></form></div></section>`;
  }
  function factoryJobPanel() {
    const jobs=records('factory_jobs'),job=selectedFactoryJob();
    const summary=job?.result?.summary || {};
    const artifacts=[...new Map([...array(job?.input_artifacts),...array(job?.artifacts),...array(job?.result?.artifacts)].map(item=>[(item.sha256 || item.id)+'/'+item.name,item])).values()];
    const active=['queued','running'].includes(job?.status);
    const candidate=job?.status==='succeeded' && ['rayoptics_lens_v1','prysm_psf_v1','prescription_psf_v1'].includes(job.plugin_id) && job.result?.model_candidate;
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">DURABLE FACTORY JOBS</span><h2>해석 작업과 생성 근거</h2><p>카메라 평가 Run과 별도의 작업 이력입니다.</p></div><span class="stat-small">${jobs.length} JOBS</span></div><div class="panel-content">${jobs.length?`<div class="form-field"><label for="factory-job-select">표시할 해석 작업</label><select id="factory-job-select">${jobs.map(item=>`<option value="${esc(item.id)}" ${item.id===job?.id?'selected':''}>${esc(item.name || short(item.id))} · ${esc(labels[item.status] || item.status)} · ${esc(String(item.id).slice(-6))}</option>`).join('')}</select></div>`:''}${job?`<div class="result-status"><div><h3>${esc(job.name || short(job.id))}</h3><p>${esc(job.plugin_id)} · ${esc(when(job.created_at))}</p></div>${badge(job.status)}</div>${active?`<div class="inline-spacer"></div><div class="callout neutral">고정된 작업의 실제 상태를 확인하고 있습니다. 완료 후 산출물·수치 검사 결과가 표시됩니다.</div><div class="inline-spacer"></div><button class="button secondary full" data-action="cancel-factory-job" data-id="${esc(job.id)}">이 해석 작업 취소</button>`:''}${job.error?`<div class="inline-spacer"></div><div class="callout error">${esc(typeof job.error==='string'?job.error:json(job.error))}</div>`:''}${job.status==='succeeded'?`<div class="inline-spacer"></div><div class="factory-evidence-tags">${badge(summary.status || 'unknown','수치 판정 · '+({PROPOSED:'응답 후보 생성',REFUSED:'후보 생성 거부'}[summary.status] || labels[summary.status] || summary.status || '요약 원본 확인'))}${badge(summary.approval_status || 'unapproved')}${badge('unknown','예측 불확실도 · '+(labels[summary.uncertainty?.kind] || summary.uncertainty?.kind || '미확인'))}</div>${issues(summary.issues || summary.warnings)}${details('수렴성 · 보존량 · 적용 범위 · 불확실도',summary)}${candidate?factoryPublicationForm(job):`<div class="inline-spacer"></div><div class="callout">${job.plugin_id==='meep_planar_stack_v1'?'광학 응답 산출물을 보존했습니다. 전하 수집 계약이 없으므로 전자 QE 또는 센서 Runtime 모델로 등록하지 않습니다.':'현재 결과에 등록 가능한 Lens 후보가 없습니다. 수렴성·유효 범위와 해석 원본을 확인하세요.'}</div>`}`:''}${artifacts.length?`<div class="inline-spacer"></div><h3 class="factory-artifacts-title">보존된 원본 산출물</h3><div class="factory-artifacts">${artifacts.map(item=>{const url=safeArtifactUrl(item.url);return `<div><strong>${esc(item.name || item.id)}</strong><small>${number(item.bytes/1024,1)} KiB · SHA-256 ${esc(short(item.sha256))}</small>${url?`<a class="link-button" href="${esc(url)}" download>원본 다운로드 ↓</a>`:'<span class="muted">다운로드 주소 미제공</span>'}</div>`;}).join('')}</div>`:''}${details('고정 요청 · 실행 환경 · 작업 원본',job)}`:empty('아직 제출된 해석 작업이 없습니다','입력 형식의 설계와 출처를 확인하고 제출하면 별도 Worker에서 실행합니다. 임의 결과나 샘플 작업을 등록하지 않습니다.')}</div></section>`;
  }
  function factoryApplicationConditions(job, source) {
    if(!source)return null;
    const prescription=job?.plugin_id==='prescription_psf_v1'?job.request?.prescription:job?.request;
    const conjugate=prescription?.schema==='lens_prescription_v5'?prescription.object_conjugate:null;
    const conditions={...source.conditions,...(conjugate?{object_distance_mm:{value:conjugate.distance_mm,unit:'mm'},object_reference_magnification:{value:conjugate.reference_magnification,unit:'1'}}:{})};
    if(job?.plugin_id!=='prescription_psf_v1')return conditions;
    return {...conditions,f_number:{value:job.result.summary.geometric_reference_f_number,unit:'1'}};
  }
  function factoryPublicationForm(job) {
    const source=findRecord('assemblies',state.factoryConditionsAssembly);
    const published=state.factoryPublished?.job_id===job.id?state.factoryPublished.model:null;
    const prescription=job.plugin_id==='prescription_psf_v1';
    const applicationConditions=factoryApplicationConditions(job,source);
    const prysm=['prysm_psf_v1','prescription_psf_v1'].includes(job.plugin_id);
    const psfParentNote=prescription ? `동일한 처방에서 생성한 RayOptics 광량·입사각 모델을 선택하세요. 모든 처방 값이 같아야 등록할 수 있습니다. 필요한 F-number: ${number(job.result?.summary?.geometric_reference_f_number,8)}. 광로차에서 계산한 PSF이며 동공 진폭 전달은 근사합니다. 실물 정확도는 미검증입니다.` : '이 PSF는 별도로 선언한 광학 가정입니다. 선택 Lens와의 결합을 실물 일치 근거로 취급하지 않습니다.';
    const baseLenses=records('models').filter(model=>model.kind==='lens'&&model.asset_id===job.asset_id&&(!prescription||model.factory_job_id));
    return `<div class="inline-spacer"></div><div class="factory-publication"><span class="eyebrow">REUSABLE LENS RESPONSE</span><h3>해석 결과를 미승인 Lens 모델로 등록</h3><p class="list-note">후보 Payload와 원본 해석의 계보를 보존합니다. 선택한 구성의 운용 조건을 명시적으로 복사합니다.</p><form data-form="factory-model" data-job-id="${esc(job.id)}"><div class="form-row"><div class="form-field"><label for="factory-model-name">새 모델 이름</label><input id="factory-model-name" name="name" required maxlength="200" value="${esc(state.factoryModelName)}" placeholder="예: 설계 A · 분광 Lens 응답"></div><div class="form-field"><label for="factory-model-revision">새 버전</label><input id="factory-model-revision" name="revision" required maxlength="80" value="${esc(state.factoryModelRevision)}"></div></div>${prysm?`<div class="form-field"><label for="factory-base-lens">PSF와 결합할 기존 Lens · 같은 자산</label><select id="factory-base-lens" name="base_lens_model_id" required><option value="">기준 Lens를 직접 선택</option>${baseLenses.map(model=>`<option value="${esc(model.id)}" ${state.factoryBaseLens===model.id?'selected':''}>${esc(model.name)} · r${esc(model.revision)}${model.kind==='lens'?' · '+esc(lensCapability(model)):''}</option>`).join('')}</select></div><div class="callout">${esc(psfParentNote)}</div><div class="inline-spacer"></div>`:''}<div class="form-field"><label for="factory-conditions-assembly">운용 조건을 복사할 기존 구성</label><select id="factory-conditions-assembly" required>${options('assemblies',state.factoryConditionsAssembly,'조건의 출처 구성을 직접 선택')}</select></div>${source?`<div class="factory-condition-source"><strong>${esc(source.name)} · r${esc(source.revision)}</strong><code>${esc(source.id)}</code><pre>${esc(json(applicationConditions))}</pre><small>${prescription?'기준 구성의 운용 조건을 이어받고 F-number는 처방에서 계산된 값으로 지정합니다. 위 JSON이 등록되며 부모 Lens의 고정 조건과 다르면 서버가 거부합니다.':'위 conditions JSON을 등록합니다. 유한 물체면 처방의 거리·배율은 원본 처방 값으로 고정합니다. Solver 설계·수치 조건도 작업 기록에 보존됩니다.'}</small></div>`:'<p class="list-note">조건의 출처를 선택하면 복사될 정확한 JSON을 표시합니다.</p>'}${prysm?`<div class="form-field"><label for="factory-condition-scope">Lens 모델에 고정할 조건 범위</label><select id="factory-condition-scope" name="condition_scope"><option value="all_declared" ${state.factoryConditionScope==='all_declared'?'selected':''}>선언된 모든 운용 조건 고정 · 기본</option><option value="optical_response" ${state.factoryConditionScope==='optical_response'?'selected':''}>광학 응답 · Sensor 노출/Gain과 독립</option></select></div>${state.factoryConditionScope==='optical_response'?`<div class="callout">Lens 광학 응답을 Sensor 노출·Gain과 독립으로 취급하는 선택입니다. 복사한 조건의 exposure_s/gain을 Lens의 고정 조건에서 제외하며, 기준 Lens의 기존 유효 범위는 유지합니다.</div>${details('유지할 기준 Lens의 유효 범위',findRecord('models',state.factoryBaseLens)?.valid_domain || {})}`:''}`:''}${details('등록할 Lens 후보 원본',job.result.model_candidate)}<div class="inline-spacer"></div><button class="button primary full" type="submit" ${!source || (prysm&&!state.factoryBaseLens)?'disabled':''}>해석 근거와 Lens 모델 등록 ${icon('arrow')}</button></form>${published?`<div class="inline-spacer"></div><div class="locked-reference"><div><strong>${esc(published.name)} · r${esc(published.revision)}</strong><br>${badge('unapproved')}</div>${recordButton('models',published,'등록 원본 ↗')}</div><button class="button secondary full" data-action="use-factory-lens" data-id="${esc(published.id)}" data-assembly="${esc(state.factoryPublished.assembly_id)}">이 Lens를 선택해 새 카메라 구성 ${icon('arrow')}</button>`:''}</div>`;
  }
  function sceneFactoryPanel() {
    const fixture=state.sceneInputMode==='fixture',assembly=selectedAssembly();
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">SPECTRAL INPUT ASSETS</span><h2>공간 장면을 카메라 실험에 연결</h2><p>분광 배열·단위·Sampling·출처를 하나의 고정 기록으로 등록합니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="scene-assembly">Sampling 기준 카메라 구성</label><select id="scene-assembly">${options('assemblies',assembly?.id)}</select></div><div class="scene-input-modes" role="group" aria-label="장면 등록 방법"><button class="filter-button ${fixture?'active':''}" data-action="scene-input-mode" data-mode="fixture" aria-pressed="${fixture}">작성한 시험 장면</button><button class="filter-button ${fixture?'':'active'}" data-action="scene-input-mode" data-mode="import" aria-pressed="${!fixture}">분광 NPY 가져오기</button></div><form data-form="${fixture?'scene-fixture':'scene-import'}">${!fixture?`<div class="form-field capture-file"><label for="scene-file">분광 영상 · NPY</label><input id="scene-file" type="file" accept=".npy"><small>${state.sceneFile?esc(state.sceneFile.name)+' · '+number(state.sceneFile.size/1024,1)+' KiB':'최대 64 MiB. 배열 축과 단위는 메타데이터 계약을 따릅니다.'}</small></div>`:''}<div class="form-toolbar"><label for="scene-json">${fixture?'시험 장면 작성 조건':'원본 영상 메타데이터'} · JSON</label><button type="button" class="button quiet" data-action="scene-template">서버 입력 형식 ↙</button></div><div class="form-field"><textarea id="scene-json" name="document" spellcheck="false" required>${esc(fixture?state.sceneDraft:state.sceneMetadata)}</textarea></div><div class="callout">${fixture?'제공 형식은 명시적으로 작성한 추정 시험 장면입니다. 실제 세계를 측정한 영상으로 표시하지 않습니다. 유한 물체면은 Lens의 거리·배율을 고정하며, 입력 창 밖의 광휘를 0으로 둡니다. 경계 PSF 손실과 물체면 샘플링 근사가 포함됩니다.':'RGB 영상이나 Sensor QE가 적용된 영상을 범용 분광 입력으로 취급하지 않습니다. 원본 파장·단위·효과 포함 범위를 확인하세요.'} JSON은 UTF-8 최대 1 MB입니다.</div><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!fixture&&!state.sceneFile?'disabled':''}>${fixture?'작성 장면을 생성하고 등록':'분광 원본을 보존하고 등록'}</button></form>${records('spectral_images').length?`<details class="data-detail"><summary>등록된 분광 장면 · ${records('spectral_images').length}개</summary><div class="capture-history">${records('spectral_images').map(scene=>`<div class="locked-reference"><div><strong>${esc(scene.name)}</strong><br>${badge(scene.source_kind)}</div><div class="compact-actions">${recordButton('spectral_images',scene,'원본 ↗')}<button class="link-button" data-action="use-scene" data-id="${esc(scene.id)}">이 장면으로 실험 →</button></div></div>`).join('')}</div></details>`:''}</div></section>`;
  }
  function temporalFactoryPanel() {
    const fixture=state.temporalSceneMode==='fixture',sequence=selectedSceneSequence();
    const history=kind=>records(kind).length?`<details class="data-detail"><summary>${kind==='scene_sequences'?'등록된 시간 장면':'등록된 적용 설정'} · ${records(kind).length}개</summary><div class="capture-history">${records(kind).map(item=>`<div class="locked-reference"><div><strong>${esc(item.name)}</strong><br>${badge(item.source_kind)}<small>r${esc(item.revision)}</small></div><div class="compact-actions">${recordButton(kind,item,'원본 ↗')}<button class="link-button" data-action="use-sequence" data-kind="${kind}" data-id="${esc(item.id)}">이 기록으로 실험 →</button></div></div>`).join('')}</div></details>`:'';
    return `<section class="panel"><div class="panel-header"><div><span class="eyebrow">TEMPORAL SCENE & APPLIED SETTINGS</span><h2>움직임·점멸과 프레임 설정을 연결</h2><p>장면 시각과 촬영 설정을 별도 기록으로 보존합니다.</p></div></div><div class="panel-content">
      <div class="form-field"><label for="temporal-assembly">기준 카메라 구성</label><select id="temporal-assembly">${options('assemblies',state.assembly)}</select></div>
      <div class="sequence-editor"><h3>1 · 시간 장면 등록</h3><div class="scene-input-modes" role="group" aria-label="시간 장면 등록 방법"><button class="filter-button ${fixture?'active':''}" data-action="temporal-scene-mode" data-mode="fixture" aria-pressed="${fixture}">합성 이동·점멸 장면</button><button class="filter-button ${fixture?'':'active'}" data-action="temporal-scene-mode" data-mode="import" aria-pressed="${!fixture}">장면 시퀀스 JSON</button></div><form data-form="${fixture?'temporal-fixture':'scene-sequence'}">${!fixture?sequenceFileField('scene'):''}<div class="form-toolbar"><label for="temporal-scene-json">${fixture?'합성 장면 작성 조건':'시각별 장면 연결'} · JSON</label><button type="button" class="button quiet" data-action="sequence-template" data-kind="scene">서버 입력 형식 ↙</button>${fixture?'<button type="button" class="button quiet" data-action="sequence-template" data-kind="scene" data-pattern="static">HDR용 정적 장면 형식 ↙</button>':''}</div><div class="form-field"><textarea id="temporal-scene-json" name="document" required spellcheck="false">${esc(fixture?state.temporalFixtureDraft:state.temporalSceneDraft)}</textarea></div><div class="callout">${fixture?'서버 예제는 이상 영상으로 작성한 합성 이동·점멸 장면입니다. 실제 환경이나 장비에서 기록한 영상이 아닙니다.':'기존 분광 장면 ID·샘플 시각·Clock·보간 방식을 확인하세요. 가져온 JSON은 원문과 정규화된 계약으로 보존합니다.'} UTF-8 최대 1 MB.</div><div class="inline-spacer"></div><button class="button primary full" type="submit">${fixture?'합성 장면 시퀀스 생성':'장면 시퀀스 원문 보존 및 등록'}</button></form>${history('scene_sequences')}</div>
      <div class="sequence-editor"><h3>2 · 프레임별 적용 설정 등록</h3><div class="form-field"><label for="temporal-scene-sequence">설정을 연결할 시간 장면</label><select id="temporal-scene-sequence">${options('scene_sequences',state.sceneSequence,'등록된 시간 장면 선택')}</select></div>${sequence?details('연결할 장면 원본 · Clock',sequence):''}<div class="inline-spacer"></div><form data-form="control-sequence">${sequenceFileField('control')}<div class="form-toolbar"><label for="control-sequence-json">Frame ID · 적용 설정 · 타이밍 · JSON</label><button class="button quiet" type="button" data-action="sequence-template" data-kind="control" ${!sequence?'disabled':''}>서버 입력 형식 ↙</button></div><div class="form-field"><textarea id="control-sequence-json" name="document" required spellcheck="false">${esc(state.controlDraft)}</textarea></div><div class="callout">제공 기본값은 추정한 적용 설정입니다. 실제 하드웨어 기록으로 표시하지 않습니다. 명령한 설정과 실제 적용된 설정, 지연·Clock·노출 단위를 구분하세요. UTF-8 최대 1 MB.</div><div class="inline-spacer"></div><button class="button primary full" type="submit">적용 설정 원문 보존 및 등록</button></form>${history('control_sequences')}</div>
      <div class="inline-spacer"></div>${go('experiments','시퀀스와 시험을 고정해 실험','secondary')}
    </div></section>`;
  }
  function sequenceFileField(kind) {
    return `<div class="form-field capture-file"><label for="sequence-file-${kind}">${kind==='scene'?'장면 시퀀스':'프레임 적용 설정'} JSON 가져오기</label><input id="sequence-file-${kind}" data-sequence-upload="${kind}" type="file" accept=".json,application/json"><small>UTF-8 최대 1 MB. 내용을 확인하고 아래 등록 버튼으로 원문을 보존합니다.</small></div>`;
  }
  async function readSequenceFile(file,kind) {
    try {
      if(file.size>MAX_JSON_BYTES)throw new Error('1 MB 이하의 시퀀스 JSON 파일을 선택하세요.');
      const raw=await file.text();parseDocument(raw,'시퀀스 JSON');
      const editor=$(kind==='scene'?'#temporal-scene-json':'#control-sequence-json');
      if(!editor || (kind==='scene'&&state.temporalSceneMode!=='import'))return;
      editor.value=raw;state[kind==='scene'?'temporalSceneDraft':'controlDraft']=editor.value;
      state.sequenceUploads[kind]={rawText:raw,editorText:editor.value};toast(file.name+' · 원문을 불러왔습니다. 조건을 확인하고 등록하세요.');
    }catch(error){toast(error.message,true);}
  }
  function parseDocument(text,label='JSON') {
    if(new TextEncoder().encode(text).byteLength>MAX_JSON_BYTES)throw new Error(label+'은 UTF-8 기준 1 MB(1,000,000 bytes) 이하여야 합니다.');
    let value;try{value=JSON.parse(text);}catch{throw new Error(label+' 형식을 확인하세요.');}
    if(!value || typeof value!=='object' || Array.isArray(value))throw new Error(label+'은 최상위 JSON 객체여야 합니다.');
    return value;
  }
  async function chooseReplaySource(kind,id) {
    invalidateReplaySources();
    await loadReplaySources();
    const choices=state.replaySources.filter(item=>item.source_kind===kind&&item.source_id===id);
    const source=choices[0];
    if(!source)throw new Error('재처리 가능한 보존 RAW 원본이 없습니다. 실제 RAW 배열과 작업 완료 상태를 확인하세요.');
    state.replaySource=choices.length===1?sourceKey(source):'';state.experimentMode='replay';location.hash='experiments';render();
    if(choices.length>1)toast('원본 RAW의 경로와 응답 보정 버전을 직접 선택하세요.');
  }
  function factoryView() {
    return `${heading('MODEL FACTORY','한 번 만든 응답을, 다음 설계에서도.','정밀 해석·실측·추정에서 얻은 응답을 재사용 가능한 모델로 관리합니다. 출처와 적용 범위가 모델의 일부입니다.')}<div class="two-columns"><div class="stack">${factoryRequestPanel()}${sceneFactoryPanel()}${temporalFactoryPanel()}${nonlinearTemplateActions()}${retainedUI.actions()}${captureProgramEditor()}</div><div class="stack">${factoryJobPanel()}</div></div><div class="section-title"><h2>실측 보정과 응답 테이블 등록</h2><span class="stat-small">IMMUTABLE MODEL REVISIONS</span></div><div class="two-columns"><div class="stack">${calibrationPanel()}${editor('model')}</div><div class="stack">${calibrationResultPanel()}<section class="panel"><div class="panel-header"><div><h2>등록된 응답 모델</h2><p>각 모델의 새 버전은 독립된 원본 기록으로 남습니다.</p></div><span class="stat-small">${records('models').length} REVISIONS</span></div><div class="panel-content">${records('models').map(model=>`<div class="locked-reference"><div><strong>${esc(model.name)}</strong><br><span>${esc(model.kind)} · r${esc(model.revision || 1)} · ${esc(labels[model.source_kind] || model.source_kind)}</span></div>${recordButton('models',model,'계약 ↗')}</div>`).join('')}</div></section><section class="panel"><div class="panel-header"><div><h2>응답 모델의 재사용 조건</h2></div></div><div class="panel-content"><div class="readiness">${[['물리적 기준면','입력과 출력의 위치, 단위, 정의를 고정합니다.'],['포함된 효과','CFA·Microlens·Lens shading 중복을 검사합니다.'],['유효 범위','파장·입사각·온도 범위 밖 예측을 구분합니다.'],['계보와 독립 검증','보정용 Dataset과 검증용 Dataset을 분리합니다.']].map(([title,text])=>`<div class="readiness-row"><span class="status-circle">·</span><div><h3>${title}</h3><p>${text}</p></div></div>`).join('')}</div></div></section></div></div><div class="section-title"><h2>플러그인 실행 계약</h2><span class="stat-small">DECLARED CAPABILITIES</span></div><div class="three-columns">${records('plugins').map(plugin=>`<article class="model-card"><span class="eyebrow">${esc(plugin.role || plugin.kind || 'PLUGIN')}</span><h3>${esc(plugin.name || plugin.plugin_id || plugin.id)}</h3><p>${esc(plugin.description || array(plugin.supports || plugin.capabilities).join(' · ') || '지원 범위는 원본 실행 계약을 확인하세요.')}</p><div>${badge(plugin.available ? 'available' : 'unavailable')}</div>${details('지원 범위 · 제한 · 버전',plugin)}</article>`).join('')}</div>`;
  }
  function releasesView() {
    const run=selectedRun();
    return `${heading('EVIDENCE & RELEASE CONTROL','결정의 근거를, 재현 가능한 패키지로.','사용한 자산·모델·시험·실행·검증 기록을 함께 묶고 배포 조건을 검사합니다. 승인 상태와 미해결 문제를 보존합니다.')}<div class="two-columns"><div class="stack"><section class="panel"><div class="panel-header"><div><span class="eyebrow">EVIDENCE PACKAGE</span><h2>검토용 근거 패키지 생성</h2><p>미충족 조건은 릴리스 차단 사유로 남습니다.</p></div></div><div class="panel-content"><form data-form="release"><div class="form-field"><label for="release-name">패키지 이름</label><input id="release-name" name="name" required maxlength="160" value="Lens–Sensor color shading · 설계 검토"></div><div class="form-field"><label for="release-run">포함할 계산 결과</label><select id="release-run" name="run_id" required><option value="">완료된 Run 선택</option>${completedRuns().map(item=>`<option value="${esc(item.id)}" ${item.id===run?.id?'selected':''}>${esc(runLabel(item))}</option>`).join('')}</select></div><div class="form-field"><label for="release-validation">연결할 검증 기록 <span>선택 사항</span></label><select id="release-validation" name="validation_id">${options('validations','','검증 기록 없음')}</select></div><div class="callout">실물 검증·불확실도·독립성 등 필수 근거가 없으면 배포가 차단됩니다. 차단된 패키지도 미해결 문제를 포함해 검토 근거로 내보낼 수 있습니다.</div><div class="inline-spacer"></div><button class="button primary full" type="submit" ${!completedRuns().length?'disabled':''}>근거 연결 및 릴리스 조건 검사 ${icon('arrow')}</button></form></div></section>${readinessPanel()}</div><div class="stack"><section class="panel"><div class="panel-header"><div><h2>저장된 근거 패키지</h2><p>패키지는 승인된 제품의 의미와 구분합니다.</p></div><span class="stat-small">${records('releases').length} PACKAGES</span></div><div class="panel-content">${records('releases').length?records('releases').map(item=>`<article class="model-card"><div><h3>${esc(item.name || short(item.id))}</h3><p>${esc(when(item.created_at))}</p></div><div>${badge(item.status)}</div>${issues(item.issues || item.blockers || item.reasons)}${details('포함된 근거와 검사 결과',item)}<div class="model-card-footer"><span>${esc(short(item.id))}</span><button class="button secondary small" data-action="export-release" data-id="${esc(item.id)}">${icon('download')}JSON 내보내기</button><a class="button secondary small" href="/api/v1/releases/${encodeURIComponent(item.id)}/bundle" download>원본 포함 ZIP ↓</a></div></article>`).join('<div class="inline-spacer"></div>'):empty('저장된 근거 패키지가 없습니다','실험 기록을 선택하면 현재 충족한 조건과 남은 문제를 묶어 검토할 수 있습니다.')}</div></section></div></div>`;
  }

  async function busy(button, action) {
    if (button?.disabled) return;
    if (button) {button.disabled=true;button.setAttribute('aria-busy','true');}
    try { await action(); } catch(error) {toast(error.message,true);} finally {if(button?.isConnected){button.disabled=false;button.removeAttribute('aria-busy');}}
  }
  function downloadObject(value, name) {
    const url=URL.createObjectURL(new Blob([json(value)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function copy(text) {
    if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);return;}
    const field=document.createElement('textarea');field.value=text;field.style.position='fixed';field.style.opacity='0';document.body.append(field);field.select();const ok=document.execCommand('copy');field.remove();if(!ok)throw new Error('클립보드에 접근할 수 없습니다. 원본 JSON을 직접 선택해 복사하세요.');
  }
  async function runOne(body) {
    const scope=CameraCaptureScope(state);
    const run=await api('/runs',body);
    if(['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(body.plugin_id)&&scope!==CameraCaptureScope(state)){await refresh();toast('전기 모델 실행은 제출했지만 선택 입력이 바뀌어 현재 선택을 유지했습니다.');return run;}
    state.assembly=body.assembly_id;state.run=run.id;state.test=body.test_id;state.validationTest=body.test_id;state.runtime=runtimeId(body.plugin_id);state.scene=body.scene_id || '';state.sceneSequence=body.scene_sequence_id || '';state.controlSequence=body.control_sequence_id || '';state.captureProgram=body.capture_program_id || '';state.experimentMode='camera';
    await refresh();
    toast(run.status==='succeeded'?'계산 결과가 저장되었습니다.':'실험을 제출했습니다. 실행 상태를 확인하고 있습니다.');
    return run;
  }
  document.addEventListener('submit',event=> {
    const ispStudyForm=event.target.closest('form[data-ispstudy-form]');
    if(ispStudyForm){event.preventDefault();ispStudyUI.submit(ispStudyForm).catch(error=>toast(error.message,true));return;}
    const advancedForm=event.target.closest('form[data-ispv2-form]');
    if(advancedForm){event.preventDefault();advancedISPUI.submit(advancedForm).catch(error=>toast(error.message,true));return;}
    const form=event.target.closest('form[data-form]');if(!form)return;event.preventDefault();
    const button=form.querySelector('button[type="submit"]');
    busy(button,async()=> {
      const data=Object.fromEntries(new FormData(form));
      switch(form.dataset.form){
        case 'capture-program': {
          const assembly=state.assembly,token=state.captureDrafts.snapshot(assembly),selectedProgram=state.captureProgram;
          const document=state.captureDrafts.parse(assembly);
          if(new TextEncoder().encode(state.captureDrafts.get(assembly)).byteLength>MAX_JSON_BYTES)throw Error('프로그램 JSON은 1 MB 이하여야 합니다.');
          state.captureSaving=true;state.captureError='';
          try {
            const record=await api('/capture_programs',document);
            if(!record.id||record.assembly_id!==assembly)throw Error('프로그램 등록 응답의 고정 구성이 다릅니다. 재등록 전 Registry를 확인하세요.');
            state.data.capture_programs=[record,...records('capture_programs').filter(p=>p.id!==record.id)];
            if(state.assembly===assembly&&state.captureProgram===selectedProgram&&state.captureDrafts.snapshot(assembly).epoch===token.epoch){state.captureProgram=record.id;clearSceneTest();}
            await refresh();toast('새 캡처 프로그램을 등록했습니다. 고정 버전으로 실험을 이어가세요.');
          } catch(error) {state.captureError=error.message+(error.status?'':' · 응답이 불확실합니다. 재등록 전에 새로고침 후 등록된 프로그램을 확인하세요.');throw error;}
          finally {state.captureSaving=false;render();}
          break;
        }
        case 'run': if(isElectrical()&&!CameraCaptureTestMatches(findRecord('tests',data.test_id),state))throw Error('선택한 프로그램·시퀀스에 고정된 시험을 선택하세요.');await runOne({assembly_id:data.assembly_id,test_id:data.test_id,plugin_id:data.plugin_id,...(data.plugin_id==='spatial_scene_v1'?{scene_id:data.scene_id}:['temporal_scene_v1','hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(data.plugin_id)?{scene_sequence_id:data.scene_sequence_id,control_sequence_id:data.control_sequence_id,...(['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(data.plugin_id)?{capture_program_id:data.capture_program_id}:{})}:{}),parameters:{...(['temporal_scene_v1','hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1','retained_charge_v1','electrical_control_v1'].includes(data.plugin_id)?{}:{exposure_ms:Number(data.exposure_ms)}),seed:Number(data.seed),...(['retained_charge_v1','electrical_control_v1'].includes(data.plugin_id)?{noise_enabled:data.noise_enabled==='on'}:{})}});break;
        case 'replay': {
          const source=replaySource();if(!source)throw new Error('보존된 원본 RAW를 선택하세요.');
          const job=await api('/replays',{name:data.name,source_kind:source.source_kind,source_id:source.source_id,isp_model_id:data.isp_model_id,force:false,...(source.selector?{schema:'camera_isp_replay_v3',selector:source.selector}:{})});
          state.replayJob=job.id;await refresh();toast('원본 RAW의 촬영 조건을 고정한 ISP 재처리를 제출했습니다.');break;
        }
        case 'temporal-fixture':
        case 'scene-sequence':
        case 'control-sequence': {
          const fixture=form.dataset.form==='temporal-fixture',kind=form.dataset.form==='control-sequence'?'control':'scene';
          const value=parseDocument(data.document,'시퀀스 JSON');
          const original=state.sequenceUploads[kind],documentText=original && original.editorText===data.document?original.rawText:data.document;
          const record=await api(fixture?'/scene_sequences/fixture':'/'+(kind==='scene'?'scene_sequences':'control_sequences')+'/import',fixture?value:{document_text:documentText});
          if(!record.id)throw new Error('시퀀스 등록 ID가 없습니다. 재등록 전에 Registry를 확인하세요.');
          const collection=kind==='scene'?'scene_sequences':'control_sequences';
          state.data[collection]=[record,...records(collection).filter(item=>item.id!==record.id)];
          if(!isElectrical())state.runtime='temporal_scene_v1';state.experimentMode='camera';clearSceneTest();
          if(kind==='scene'){state.sceneSequence=record.id;state.controlSequence='';}else{state.controlSequence=record.id;state.sceneSequence=record.scene_sequence_id;state.assembly=record.assembly_id;}
          await refresh();toast(kind==='scene'?'시간 장면을 등록했습니다. 연결할 프레임 적용 설정을 등록하세요.':'적용 설정을 등록했습니다. 실험에서 두 시퀀스의 시험을 생성하고 실행하세요.');break;
        }
        case 'scene-fixture':
        case 'scene-import': {
          const metadata=parseDocument(data.document,'장면 JSON');let scene;
          if(form.dataset.form==='scene-fixture')scene=await api(metadata.schema==='camera_finite_plane_fixture_v1'?'/spectral_images/finite_plane':'/spectral_images/fixture',metadata);
          else{
            if(!state.sceneFile || state.sceneFile.size>64*1024*1024)throw new Error('64 MiB 이하의 분광 NPY 파일을 선택하세요.');
            const body=new FormData();body.append('metadata',data.document);body.append('image',state.sceneFile,state.sceneFile.name);
            scene=await api('/spectral_images/import',body);state.sceneFile=null;
          }
          if(!scene.id)throw new Error('장면 등록 ID를 확인할 수 없습니다. 재제출 전 Registry를 확인하세요.');
          state.data.spectral_images=[scene,...records('spectral_images').filter(item=>item.id!==scene.id)];state.scene=scene.id;
          await refresh();toast('분광 장면을 등록했습니다. 장면에 맞는 시험을 생성하고 공간 계산을 실행하세요.');break;
        }
        case 'temporal-test':
        case 'spatial-test': {
          const scope=CameraCaptureScope(state);
          const test=await api('/tests',parseDocument(data.document,'시험 JSON'));
          if(scope!==CameraCaptureScope(state)){await refresh();toast('시험은 등록했지만 입력 선택이 바뀌어 자동 선택하지 않았습니다.');break;}
          state.test=test.id;state.validationTest=test.id;state.run='';state.spatialTestDraft='';state.temporalTestDraft='';await refresh();toast('새 시험을 등록하고 선택했습니다. 고정된 입력과 적용 조건을 확인한 후 실행하세요.');break;
        }
        case 'assembly': {
          const base=findRecord('assemblies',form.dataset.base);
          if(!base)throw new Error('기준 구성을 찾을 수 없습니다. 데이터를 새로고침하세요.');
          if(new TextEncoder().encode(data.contract).byteLength>MAX_JSON_BYTES)throw new Error('촬영 계약 JSON은 1 MB 이하여야 합니다.');
          let contract;try{contract=JSON.parse(data.contract);}catch{throw new Error('촬영 계약 JSON 형식을 확인하세요.');}
          if(!contract || typeof contract!=='object' || Array.isArray(contract) || Object.keys(contract).sort().join(',')!=='conditions,raw_format,spectral_range')throw new Error('conditions, spectral_range, raw_format 세 계약을 명시하세요.');
          const result=await api('/assemblies',{name:data.name,revision:'1',model_ids:[data.lens,data.sensor,data.isp],...contract,description:'새 응답 모델 조합. 기준 구성: '+base.id});
          state.builderHandoff=null;delete state.builderContracts?.[base.id];
          state.assembly=result.id;state.compatibility=null;state.builderSensor='';state.builderLens='';await refresh();toast('새 카메라 구성을 저장했습니다. 실행 전 호환성을 검사하세요.');break;
        }
        case 'factory-job': {
          if(new TextEncoder().encode(data.document).byteLength>MAX_JSON_BYTES)throw new Error('Factory JSON 입력은 UTF-8 기준 1 MB(1,000,000 bytes) 이하여야 합니다.');
          let payload;try{payload=JSON.parse(data.document);}catch{throw new Error('Solver 입력의 JSON 형식을 확인하세요.');}
          if(!payload || typeof payload!=='object' || Array.isArray(payload))throw new Error('Solver 요청은 JSON 객체여야 합니다.');
          if(payload.plugin_id!==state.factoryPlugin)throw new Error('선택한 Solver와 JSON의 plugin_id가 다릅니다. 해당 Solver의 입력 형식을 확인하세요.');
          const job=await api('/factory/jobs',payload);state.factoryJob=job.id;
          await refresh();toast('해석 작업을 제출했습니다. 고정된 입력과 실제 작업 상태를 확인하고 있습니다.');break;
        }
        case 'factory-model': {
          const source=findRecord('assemblies',state.factoryConditionsAssembly);
          if(!source)throw new Error('운용 조건을 복사할 기준 구성을 선택하세요.');
          const job=selectedFactoryJob();
          if(job?.id!==form.dataset.jobId)throw new Error('해석 작업 선택이 바뀌었습니다. 현재 후보를 다시 확인하세요.');
          const model=await api('/factory/models',{job_id:form.dataset.jobId,name:data.name,revision:data.revision,conditions:factoryApplicationConditions(job,source),...(data.base_lens_model_id?{base_lens_model_id:data.base_lens_model_id,condition_scope:data.condition_scope || 'all_declared'}:{})});
          if(!model.id)throw new Error('모델 등록 응답에 ID가 없습니다. 재제출 전에 Registry 기록을 확인하세요.');
          state.data.models=[model,...records('models').filter(item=>item.id!==model.id)];
          state.factoryPublished={job_id:form.dataset.jobId,model,assembly_id:source.id};
          await refresh();toast('해석 근거를 연결한 미승인 Lens 모델을 등록했습니다. 새 구성에서 호환성과 출력을 확인하세요.');break;
        }
        case 'capture': {
          if(!state.captureRaw)throw new Error('원본 RAW NPY 파일을 선택하세요.');
          if(state.captureRaw.size+(state.captureDark?.size || 0)>64*1024*1024)throw new Error('RAW와 Dark 파일의 합계는 64 MiB 이하여야 합니다.');
          if(new TextEncoder().encode(data.metadata).byteLength>1024*1024)throw new Error('취득 메타데이터는 1 MiB 이하여야 합니다.');
          let metadata;try{metadata=JSON.parse(data.metadata);}catch{throw new Error('취득 메타데이터 JSON 형식을 확인하세요.');}
          if(!metadata || typeof metadata!=='object' || Array.isArray(metadata))throw new Error('메타데이터는 JSON 객체여야 합니다.');
          if(state.captureDark && !metadata.observation?.dark_conditions)throw new Error('Dark를 연결하려면 observation.dark_conditions에 실제 노출·Gain·온도를 명시하세요.');
          const payload=new FormData();payload.append('metadata',data.metadata);payload.append('raw',state.captureRaw,state.captureRaw.name);
          if(state.captureDark)payload.append('dark',state.captureDark,state.captureDark.name);
          const result=await api('/captures/import',payload);
          chooseCapture(result);state.captureRaw=null;state.captureDark=null;state.captureDraft='';
          await refresh();toast('RAW 원본과 ROI Dataset을 보존했습니다. 반환된 ROI 시험으로 다시 계산한 뒤 비교하세요.');break;
        }
        case 'calibration': {
          const ids=new FormData(form).getAll('measurement_ids');
          if(!ids.length || ids.length>32)throw new Error('보정에 사용할 RAW Dataset을 1–32개 선택하세요.');
          let bounds;try{bounds=JSON.parse(data.parameter_bounds);}catch{throw new Error('R/B 배율 범위의 JSON 형식을 확인하세요.');}
          const result=await api('/calibrations',{name:data.name,revision:data.revision,base_sensor_model_id:data.base_sensor_model_id,measurement_ids:ids,parameter_bounds:bounds});
          state.calibrationReport=result.id;
          await refresh();toast(`보정 제안: ${labels[result.status] || result.status} · 미승인. 저장된 결과와 사유를 확인하세요.`,result.status==='REFUSED');break;
        }
        case 'measurement':
        case 'model': {
          if(new TextEncoder().encode(data.document).byteLength>MAX_JSON_BYTES)throw new Error('JSON 입력은 UTF-8 기준 1 MB(1,000,000 bytes) 이하여야 합니다.');
          let parsed;try{parsed=JSON.parse(data.document);}catch{throw new Error('JSON 형식이 올바르지 않습니다. 쉼표와 따옴표를 확인하세요.');}
          if(!parsed || typeof parsed!=='object' || Array.isArray(parsed))throw new Error('최상위 JSON 객체가 필요합니다. 입력 예제에서 계약을 확인하세요.');
          const kind=form.dataset.form;
          const original=state.uploadedMeasurement;
          const documentText=kind==='measurement' && original && data.document===original.editorText ? original.rawText : data.document;
          const result=await api(kind==='model'?'/models':'/measurements/import',kind==='model'?parsed:{document_text:documentText});
          if(kind==='measurement'){state.measurement=result.id;state.measurementDraft='';state.uploadedMeasurement=null;}else{state.modelDraft='';if(result.kind==='isp')state.replayISP=result.id;}
          await refresh();toast(`${result.name || result.id} · ${kind==='model'?'새 응답 모델':'측정 데이터'}가 등록되었습니다.`);break;
        }
        case 'validation': {
          const result=await api('/validations',{...data,allow_calibrated_model:form.elements.allow_calibrated_model.checked});state.validation=result;
          await refresh();toast(`검증 결과: ${labels[result.status || result.report?.status] || result.status || result.report?.status}`);break;
        }
        case 'release': {
          const result=await api('/releases',{name:data.name,run_ids:[data.run_id],validation_ids:data.validation_id?[data.validation_id]:[]});
          await refresh();toast(`근거 패키지 저장 · ${labels[result.status] || result.status}`);break;
        }
      }
    });
  });
  document.addEventListener('click',event=> {
    const electricalResponseButton=event.target.closest('[data-electricalresponse-action]');
    if(electricalResponseButton){event.preventDefault();electricalResponseUI.click(electricalResponseButton).catch(error=>toast(error.message,true));return;}
    const retainedISPButton=event.target.closest('[data-retainedisp-action]');
    if(retainedISPButton){event.preventDefault();retainedISPUI.click(retainedISPButton).catch(error=>toast(error.message,true));return;}
    const ispStudyButton=event.target.closest('[data-ispstudy-action]');
    if(ispStudyButton){event.preventDefault();ispStudyUI.click(ispStudyButton).catch(error=>toast(error.message,true));return;}
    const advancedButton=event.target.closest('[data-ispv2-action]');
    if(advancedButton){event.preventDefault();advancedISPUI.click(advancedButton).catch(error=>toast(error.message,true));return;}
    const button=event.target.closest('[data-action]');if(!button)return;
    const action=button.dataset.action;
    if(action==='close-dialog'){state.dialogEpoch=(state.dialogEpoch||0)+1;$('#record-dialog').close();return;}
    if(action==='retry-details'){render();return;}
    if(action==='record'){
      const item=findRecord(button.dataset.collection,button.dataset.id);if(!item)return;
      const epoch=state.dialogEpoch=(state.dialogEpoch||0)+1;
      $('#record-title').textContent=item.name || item.id;
      $('#record-content').textContent='기록 원본을 불러오고 있습니다.';
      $('#record-dialog [data-action="copy-record"]').disabled=true;
      $('#record-dialog').showModal();
      workbenchData.load(item).then(record=>{if(epoch!==state.dialogEpoch || !$('#record-dialog').open)return;$('#record-content').textContent=json(record);$('#record-dialog [data-action="copy-record"]').disabled=false;}).catch(error=>{if(epoch===state.dialogEpoch)$('#record-content').textContent='원본 조회 실패: '+error.message;});return;
    }
    if(action==='asset-filter'){state.assetFilter=button.dataset.filter;render();return;}
    if(action==='select-run'&&findRecord('runs',button.dataset.id)?.plugin_id==='closed_loop_scene_v1'){controlUI.selectRun(button.dataset.id);location.hash='controls';return;}
    if(action==='select-run'){chooseRun(button.dataset.id);state.experimentMode='camera';location.hash='experiments';render();return;}
    if(action==='frame-step'){
      const job=findRecord('runs',button.dataset.job) || findRecord('replays',button.dataset.job),frames=resultFrames(job);
      const current=Math.max(0,frames.findIndex(frame=>String(frame.frame_id)===String(state.frameSelections[job?.id])));
      const frame=frames[Math.min(frames.length-1,Math.max(0,current+Number(button.dataset.step)))];
      if(job && frame){state.frameSelections[job.id]=String(frame.frame_id);render();const next=Array.from(document.querySelectorAll('[data-action=frame-step]')).find(item=>item.dataset.job===job.id && item.dataset.step===button.dataset.step && !item.disabled);(next || document.getElementById('frame-'+job.id))?.focus({preventScroll:true});}return;
    }
    if(action==='retained-isp-from-run'){state.experimentMode='retained-isp';location.hash='experiments';render();retainedISPUI.selectSource(button.dataset.id).catch(error=>toast(error.message,true));return;}
    if(action==='experiment-mode'){state.experimentMode=button.dataset.mode;render();if(state.experimentMode==='retained-isp')retainedISPUI.activate();if(state.experimentMode==='electrical-response')electricalResponseUI.activate().catch(error=>toast(error.message,true));if(state.experimentMode==='study')studyUI.activate();if(state.experimentMode==='isp-study')ispStudyUI.activate();if(state.experimentMode==='surrogate')surrogateUI.activate();return;}
    if(action==='clear-capture-file'){state[button.dataset.kind==='raw'?'captureRaw':'captureDark']=null;render();return;}
    busy(button,async()=> {
      switch(action){
        case 'refresh': await refresh();toast('최신 자산과 실험 기록을 불러왔습니다.');break;
        case 'capture-electrical-control-template':
        case 'capture-trapped-template':
        case 'capture-retained-gain-template':
        case 'capture-trapped-gain-template':
        case 'capture-retained-hdr-template':
        case 'capture-retained-template':
        case 'capture-template':
        case 'capture-dcg-template':
        case 'capture-reference-template':
        case 'capture-reference-translation-template':
        case 'capture-nonlinear-template':
        case 'capture-nonlinear-dcg-template':
        case 'capture-nonlinear-reference-template': {
          const id=state.assembly,controlId=state.controlSequence,token=state.captureDrafts.snapshot(id);if(!id)throw Error('고정 카메라 구성을 선택하세요.');if(['capture-retained-template','capture-trapped-template','capture-retained-gain-template','capture-trapped-gain-template','capture-retained-hdr-template','capture-electrical-control-template'].includes(action)&&!controlId)throw Error('잔류 전하 프로그램에 연결할 적용 설정을 선택하세요.');
          state.captureLoading=true;state.captureError='';render();
          try {const doc=await api('/templates/capture-program?assembly_id='+encodeURIComponent(id)+({ 'capture-electrical-control-template':action==='capture-electrical-control-template'?CameraElectricalControlTemplateQuery(button.dataset.electricalMode,controlId):'','capture-retained-template':'&mode=retained_charge&control_sequence_id='+encodeURIComponent(controlId),'capture-trapped-template':'&mode=trapped_charge&control_sequence_id='+encodeURIComponent(controlId),'capture-retained-gain-template':'&mode=retained_gain_merge&control_sequence_id='+encodeURIComponent(controlId),'capture-trapped-gain-template':'&mode=trapped_gain_merge&control_sequence_id='+encodeURIComponent(controlId),'capture-retained-hdr-template':'&mode=retained_stateful_hdr&control_sequence_id='+encodeURIComponent(controlId),'capture-dcg-template':'&mode=shared_charge','capture-reference-template':'&mode=reference_guarded','capture-reference-translation-template':'&mode=reference_guarded_translation','capture-nonlinear-template':'&mode=independent_bracket_nonlinear','capture-nonlinear-dcg-template':'&mode=shared_charge_nonlinear','capture-nonlinear-reference-template':'&mode=reference_guarded_nonlinear'}[action]||''));if(['capture-retained-template','capture-trapped-template','capture-retained-gain-template','capture-trapped-gain-template','capture-retained-hdr-template','capture-electrical-control-template'].includes(action)&&state.controlSequence!==controlId)toast('적용 설정이 바뀌어 이전 잔류 전하 초안을 적용하지 않았습니다.');else if(!state.captureDrafts.accept(token,doc,state.assembly))toast('선택이나 초안이 바뀌어 이전 기본 형식을 적용하지 않았습니다.');}
          catch(error){state.captureError=error.message;throw error;}
          finally{state.captureLoading=false;render();}break;
        }
        case 'capture-copy': {
          const id=state.assembly,chosen=state.captureProgram,token=state.captureDrafts.snapshot(id),record=selectedCaptureProgram();
          if(!record||record.assembly_id!==id)throw Error('현재 구성에 속한 프로그램을 선택하세요.');
          const full=await workbenchData.load(record),defaults=['camera_retained_charge_capture_program_v1','camera_retained_charge_capture_program_v2','camera_retained_charge_capture_program_v3','camera_retained_charge_capture_program_v4','camera_retained_charge_capture_program_v5','camera_electrical_control_capture_program_v1','camera_electrical_control_capture_program_v2','camera_electrical_control_capture_program_v3'].includes(full.payload.schema)?{payload:{}}:await api('/templates/capture-program?assembly_id='+encodeURIComponent(id)+CameraCaptureTemplateMode(full.payload.schema));
          if(state.captureProgram!==chosen||state.assembly!==id)break;
          const document=Object.fromEntries(['name','revision','assembly_id','source_kind','source_uri','license','method_and_version','physical_instance','parents','payload'].map(key=>[key,structuredClone(full[key])]));
          Object.assign(document,{revision:String(full.revision)+'-edit',source_kind:'estimated',physical_instance:null,parents:[full.id],method_and_version:'authored_capture_program_revision:1'});
          if(state.captureDrafts.accept(token,document,state.assembly)){if(document.payload.schema==='camera_dcg_capture_program_v1')dcgUI.rememberProfile(id,defaults.payload.merge_profile);else state.captureDrafts.rememberProfile(id,defaults.payload.hdr_merge);}
          render();break;
        }
        case 'capture-use':if(!selectedCaptureProgram()||selectedCaptureProgram().assembly_id!==state.assembly)throw Error('현재 카메라의 고정 프로그램을 선택하세요.');state.runtime=CameraCaptureRuntime(selectedCaptureProgram().payload.schema);state.experimentMode='camera';clearSceneTest();location.hash='experiments';render();break;
        case 'copy-record':await copy($('#record-content').textContent);toast('기록 JSON을 복사했습니다.');break;
        case 'temporal-test-template': {
          const assemblyId=state.assembly,sceneId=state.sceneSequence,controlId=state.controlSequence,scope=CameraCaptureScope(state),programId=isElectrical()?state.captureProgram:'';
          if(isElectrical()&&!programId)throw Error('고정 캡처 프로그램을 선택하세요.');
          if(!assemblyId || !sceneId || !controlId)throw new Error('구성과 두 시퀀스를 선택하세요.');
          const example=await api('/templates/temporal-test?assembly_id='+encodeURIComponent(assemblyId)+'&scene_sequence_id='+encodeURIComponent(sceneId)+'&control_sequence_id='+encodeURIComponent(controlId)+(programId?'&capture_program_id='+encodeURIComponent(programId):''));
          if(CameraCaptureScope(state)!==scope){toast('선택 입력이 바뀌어 이전 시험 형식을 적용하지 않았습니다.');break;}
          state.temporalTestDraft=json(example);render();toast('두 시퀀스의 시험을 불러왔습니다. 검토 후 시험 정의를 등록하세요.');break;
        }
        case 'sequence-template': {
          const kind=button.dataset.kind,fixture=kind==='scene'&&state.temporalSceneMode==='fixture',assemblyId=state.assembly,sceneId=state.sceneSequence,mode=state.temporalSceneMode;
          if(!assemblyId || (kind==='control'&&!sceneId))throw new Error('기준 구성과 연결할 장면 시퀀스를 선택하세요.');
          const example=await api('/templates/'+(kind==='control'?'control-sequence':fixture?'temporal-fixture':'temporal-scene')+'?assembly_id='+encodeURIComponent(assemblyId)+(kind==='control'?'&scene_sequence_id='+encodeURIComponent(sceneId):''));
          if(state.assembly!==assemblyId || state.sceneSequence!==sceneId || state.temporalSceneMode!==mode){toast('기준 입력이 바뀌어 이전 형식을 적용하지 않았습니다.');break;}
          if(fixture&&button.dataset.pattern==='static')example.pattern='static';
          state[kind==='control'?'controlDraft':fixture?'temporalFixtureDraft':'temporalSceneDraft']=json(example);delete state.sequenceUploads[kind];render();toast('추정 예제 형식입니다. 실제 출처·Clock·적용 설정과 단위를 확인하세요.');break;
        }
        case 'temporal-scene-mode':state.temporalSceneMode=button.dataset.mode;render();break;
        case 'use-sequence': {
          const record=findRecord(button.dataset.kind,button.dataset.id);if(!record)throw new Error('저장된 시퀀스를 찾을 수 없습니다.');
          if(!isElectrical())state.runtime='temporal_scene_v1';state.experimentMode='camera';clearSceneTest();
          if(button.dataset.kind==='control_sequences'){state.assembly=record.assembly_id;state.sceneSequence=record.scene_sequence_id;state.controlSequence=record.id;}else{state.sceneSequence=record.id;state.controlSequence='';}
          location.hash='experiments';render();break;
        }
        case 'spatial-test-template': {
          if(!state.assembly || !state.scene)throw new Error('공간 시험에 사용할 구성과 장면을 선택하세요.');
          const assemblyId=state.assembly,sceneId=state.scene;
          const example=await api('/templates/spatial-test?assembly_id='+encodeURIComponent(assemblyId)+'&scene_id='+encodeURIComponent(sceneId));
          if(state.assembly!==assemblyId || state.scene!==sceneId){toast('구성 또는 장면이 바뀌어 이전 시험 형식을 적용하지 않았습니다.');break;}
          state.spatialTestDraft=json(example);render();toast('시험 정의를 불러왔습니다. 내용을 검토하고 등록 버튼으로 고정하세요.');break;
        }
        case 'scene-template': {
          const fixture=state.sceneInputMode==='fixture',assemblyId=state.assembly;
          const example=await api('/templates/'+(fixture?'spatial-scene':'spectral-image')+'?assembly_id='+encodeURIComponent(assemblyId));
          if(state.assembly!==assemblyId){toast('기준 구성이 바뀌어 이전 장면 형식을 적용하지 않았습니다.');break;}
          state[fixture?'sceneDraft':'sceneMetadata']=json(example);render();toast('장면 형식을 불러왔습니다. 출처·단위·Sampling과 조건을 확인하세요.');break;
        }
        case 'isp-profile-template': {
          const assemblyId=state.assembly;const example=await api('/templates/isp-profile?assembly_id='+encodeURIComponent(assemblyId));
          if(state.assembly!==assemblyId){toast('기준 구성이 바뀌어 이전 ISP 형식을 적용하지 않았습니다.');break;}
          state.modelDraft=json(example);render();toast('ISP 프로파일 형식을 불러왔습니다. 파라미터·이름·버전을 확인하고 새 모델로 등록하세요.');break;
        }
        case 'scene-input-mode':state.sceneInputMode=button.dataset.mode;render();break;
        case 'use-scene':state.scene=button.dataset.id;state.runtime='spatial_scene_v1';state.experimentMode='camera';state.test='';state.run='';state.spatialTestDraft='';location.hash='experiments';render();break;
        case 'replay-from-run':await chooseReplaySource('run',button.dataset.id);break;
        case 'replay-from-capture':await chooseReplaySource('capture',button.dataset.id);break;
        case 'cancel-replay':await api('/replays/'+encodeURIComponent(button.dataset.id)+'/cancel',{});await refresh();toast('RAW 재처리 작업의 취소 상태를 저장했습니다.');break;
        case 'factory-template': {
          const pluginId=state.factoryPlugin;
          const requestVersion=button.dataset.requestVersion || '1';
          const example=await api('/templates/factory?plugin_id='+encodeURIComponent(pluginId)+'&request_version='+encodeURIComponent(requestVersion));
          state.factoryDrafts[pluginId]=json(example);
          if(state.factoryPlugin===pluginId && $('#factory-json'))$('#factory-json').value=state.factoryDrafts[pluginId];
          toast('Solver 입력 형식을 불러왔습니다. 실제 설계·재료·자산 ID·출처와 수치 조건을 확인하세요.');break;
        }
        case 'cancel-factory-job': {
          await api('/factory/jobs/'+encodeURIComponent(button.dataset.id)+'/cancel',{});await refresh();toast('작업 취소를 요청했습니다. 저장된 종료 상태를 확인하세요.');break;
        }
        case 'use-factory-lens': {
          const model=findRecord('models',button.dataset.id);
          if(model?.kind!=='lens')throw new Error('등록된 Lens 모델을 찾을 수 없습니다.');
          const source=findRecord('assemblies',button.dataset.assembly);
          if(!source)throw new Error('모델 조건의 출처 구성을 찾을 수 없습니다.');
          delete state.builderContracts?.[source.id];
          state.builderHandoff={base:source.id,lens:model.id,conditions:model.factory_application_conditions || source.conditions};
          state.assembly=source.id;state.builderLens=model.id;state.builderSensor='';state.compatibility=null;location.hash='builder';render();
          toast('새 구성 폼에 생성된 Lens 모델을 선택했습니다. 구성 이름을 입력해 별도 버전으로 저장하세요.');break;
        }
        case 'select-capture':chooseCapture(captureContext(button.dataset.id));render();toast('이 RAW 취득의 고정 ROI 시험과 Dataset을 선택했습니다.');break;
        case 'run-capture': {
          const context=captureContext(button.dataset.id);chooseCapture(context);
          if(context.capture.capture_kind==='control_response_observation_v1'){
            const run=await runOne({assembly_id:context.capture.assembly_id,test_id:context.test.id,plugin_id:'closed_loop_scene_v1',control_program_id:context.test.stimulus.control_program_id,parameters:{seed:Number(state.seed)}});
            controlUI.selectRun(run.id);location.hash='controls';render();break;
          }
          const exposure=Number(context.test.conditions?.exposure_s?.value)*1000;
          if(!Number.isFinite(exposure))throw new Error('고정 시험의 exposure_s 값이 없습니다. 시험 원본을 확인하세요.');
          await runOne({assembly_id:context.capture.assembly_id,test_id:context.test.id,plugin_id:'response_db',parameters:{exposure_ms:exposure,seed:Number(state.seed)}});
          location.hash='experiments';render();break;
        }
        case 'calibration-bounds': {
          const count=array(calibrationBaseModel()?.payload?.sensor_angular_response?.theta_deg).length-1;
          if(count<1)throw new Error('기준 모델에 지원되는 0° 이후 각도 노드가 없습니다.');
          state.calibrationBounds=json(Object.fromEntries(['R','B'].map(channel=>[channel,{min:Array(count).fill(.8),max:Array(count).fill(1.2),unit:'1'}])));
          $('#calibration-bounds').value=state.calibrationBounds;toast('배율 범위 형식 예제입니다. 실제 근거에 맞게 min/max를 검토하고 수정하세요.');break;
        }
        case 'use-calibration-candidate': {
          const report=findRecord('calibrations',button.dataset.id);
          if(report?.status!=='PROPOSED' || !findRecord('models',report.candidate_model_id))throw new Error('사용 가능한 보정 후보 모델이 없습니다.');
          const measurement=findRecord('measurements',array(report.measurement_ids)[0]);
          if(measurement){state.assembly=measurement.assembly_id;state.test=measurement.test_id;state.validationTest=measurement.test_id;}
          state.builderSensor=report.candidate_model_id;state.compatibility=null;location.hash='builder';render();
          toast('새 구성 폼에 보정 센서를 선택했습니다. 이름을 입력해 원본과 별도의 카메라 구성을 저장하세요.');break;
        }
        case 'native-pixel-template': {
          const baseId=$('#native-sensor-base')?.value;
          if(!baseId)throw new Error('기준 Sensor 모델을 선택하세요.');
          const example=await api('/templates/native-pixel-model?base_sensor_model_id='+encodeURIComponent(baseId));
          if($('#native-sensor-base')?.value!==baseId)break;
          state.modelDraft=json(example);$('#model-json').value=state.modelDraft;
          toast(example.payload?.charge_collection?'생성·수집 계약을 보존한 새 버전 초안입니다. 두 응답과 출처를 함께 검토하세요.':'4사이트 초안을 불러왔습니다. 기존 RGB 응답은 Gr=Gb·방위각 불변으로 옮깁니다. 실제 응답과 출처를 확인하세요.');break;
        }
        case 'template': {
          const kind=button.dataset.kind;
          const example=await api('/templates/'+kind);
          $('#'+kind+'-json').value=json(example);
          state[kind+'Draft']=json(example);
          if(kind==='measurement')state.uploadedMeasurement=null;
          toast('입력 예제를 불러왔습니다. 출처와 조건, 실제 데이터를 확인한 뒤 등록하세요.');break;
        }
        case 'check-assembly': {
          state.compatibility=await api('/assemblies/check',{assembly_id:state.assembly});
          $('#compatibility-result').innerHTML=compatibilityResult(state.compatibility);break;
        }
        case 'condition-space': {
          const assemblyId=state.assembly,axis=$('#condition-axis')?.value,raw=$('#condition-values')?.value || '';
          state.conditionAxis=axis;state.conditionValues=raw;
          try {
            const parts=raw.split(',').map(item=>item.trim());
            if(!parts.length||parts.length>32||parts.some(item=>item===''||!Number.isFinite(Number(item))))throw new Error('유한한 후보 숫자를 쉼표로 구분해 최대 32개 입력하세요.');
            const values=parts.map(Number);
            if(new Set(values).size!==values.length)throw new Error('중복 후보 값은 제거하세요.');
            const report=await api('/assemblies/condition-space',{schema:'camera_condition_space_request_v1',assembly_id:assemblyId,candidates:{[axis]:values}});
            if(state.assembly!==assemblyId)break;
            state.conditionReport=report;state.conditionError='';render();
          } catch(error) {if(state.assembly===assemblyId){state.conditionReport=null;state.conditionError=error.message;render();}}
          break;
        }
        case 'lfm-candidate-plan': {
          const runId=$('#lfm-plan-run')?.value,raw=$('#lfm-plan-draft')?.value || '';
          state.lfmPlanRun=runId;state.lfmPlanDraft=raw;
          try {
            const lines=raw.trim().split(/\r?\n/);
            if(!runId||!raw.trim()||lines.length>16)throw new Error('기준 run과 최대 16개 후보를 입력하세요.');
            const candidates=lines.map(line=>{
              const parts=line.split(',').map(value=>value.trim());
              if(parts.length!==2||parts.some(value=>value===''))throw new Error('각 줄을 노출 ms, gain 형식으로 입력하세요.');
              const milliseconds=Number(parts[0]),gain=Number(parts[1]),nanoseconds=milliseconds*1e6;
              if(!Number.isFinite(milliseconds)||milliseconds<=0||!Number.isFinite(gain)||gain<=0||!Number.isSafeInteger(Math.round(nanoseconds))||Math.abs(nanoseconds-Math.round(nanoseconds))>1e-6)throw new Error('노출은 ns로 정확히 표현 가능한 양수 ms, gain은 양수여야 합니다.');
              return {exposure_ns:Math.round(nanoseconds),gain};
            });
            const report=await api('/lfm/candidate-plans',{schema:'camera_lfm_candidate_plan_request_v1',source_run_id:runId,candidates});
            if(state.assembly!==report.assembly_id||state.lfmPlanRun!==runId)break;
            state.lfmPlanReport=report;state.lfmPlanError='';render();
          } catch(error) {if(state.lfmPlanRun===runId){state.lfmPlanReport=null;state.lfmPlanError=error.message;render();}}
          break;
        }
        case 'compare-assemblies': {
          const ids=array(state.data.demo?.assembly_ids).length?state.data.demo.assembly_ids:records('assemblies').map(item=>item.id);
          if(ids.length<2)throw new Error('서로 다른 두 카메라 구성이 필요합니다.');
          const exposure=Number($('#run-exposure')?.value || 4),seed=Number($('#run-seed')?.value || 42);
          const chosenTest=$('#run-test')?.value || testId();
          const created=[];
          for(const id of ids.slice(0,2)){created.push(await api('/runs',{assembly_id:id,test_id:chosenTest,plugin_id:'response_db',parameters:{exposure_ms:exposure,seed}}));}
          state.run=created[0].id;state.assembly=ids[0];state.test=chosenTest;state.validationTest=chosenTest;state.runtime='response_db_v1';state.scene='';await refresh();toast('두 렌즈의 비교 실험을 제출했습니다. 동일 조건의 실제 결과를 겹쳐 표시합니다.');break;
        }
        case 'sweep': {
          const ids=array(state.data.demo?.assembly_ids).length?state.data.demo.assembly_ids:records('assemblies').map(item=>item.id);
          if(ids.length<2)throw new Error('두 카메라 구성이 필요합니다.');
          const result=await api('/experiments',{name:'Lens A/B × 4/8 ms',assembly_ids:ids.slice(0,2),test_id:testId(),exposure_ms:[4,8],seed:42});
          if(result.run_ids?.[0])state.run=result.run_ids[0];state.assembly=ids[0];state.test=testId();state.validationTest=testId();state.runtime='response_db_v1';state.scene='';await refresh();toast(`${result.run_ids?.length || 4}개 실험을 제출했습니다.`);break;
        }
        case 'export-run':downloadObject(await api('/runs/'+encodeURIComponent(button.dataset.id)+'/export'),'camera-run-'+button.dataset.id+'.json');toast('계산 근거 JSON을 내보냈습니다.');break;
        case 'export-release':downloadObject(await api('/releases/'+encodeURIComponent(button.dataset.id)+'/export'),'camera-evidence-'+button.dataset.id+'.json');toast('근거 패키지 JSON을 내보냈습니다.');break;
      }
    });
  });
  document.addEventListener('change',event=> {
    if(event.target.dataset?.electricalresponseField){electricalResponseUI.change(event.target).catch(error=>toast(error.message,true));return;}
    if(Object.keys(event.target.dataset || {}).some(key=>key.startsWith('retainedisp'))){retainedISPUI.change(event.target).catch(error=>toast(error.message,true));return;}
    if(Object.keys(event.target.dataset || {}).some(key=>key.startsWith('ispstudy'))){ispStudyUI.change(event.target).catch(error=>toast(error.message,true));return;}
    if(Object.keys(event.target.dataset || {}).some(key=>key.startsWith('ispv2'))){advancedISPUI.change(event.target).catch(error=>toast(error.message,true));return;}
    if(dcgUI.change(event.target))return;
    if(event.target.id==='retained-control-sequence'){const selected=findRecord('control_sequences',event.target.value);state.controlSequence=selected?.id||'';if(selected)state.sceneSequence=selected.scene_sequence_id;clearSceneTest();render();return;}
    if(event.target.id==='capture-program-assembly'){state.assembly=event.target.value;state.captureProgram='';state.controlSequence='';state.captureError='';clearSceneTest();render();return;}
    if(event.target.id==='run-capture-program'||event.target.id==='factory-capture-program'){state.captureProgram=event.target.value;clearSceneTest();render();return;}
    if(event.target.id==='capture-program-json'){render();return;}
    if(['payload.hdr_merge.reference_frame_index','payload.observation_readout_id'].includes(event.target.dataset.captureField)){render();return;}
    if(['capture-dark-temperature','capture-hdr-enabled','capture-pwm-enabled'].includes(event.target.id)) {
      try {
        const id=state.assembly,document=state.captureDrafts.parse(id),enabled=event.target.checked;
        if(event.target.id==='capture-dark-temperature')document.payload.electrical_model.dark_current_doubling_c=enabled?6:null;
        if(event.target.id==='capture-hdr-enabled') {
          if(!enabled)state.captureDrafts.rememberProfile(id,document.payload.hdr_merge);
          document.payload.hdr_merge=enabled?structuredClone(state.captureDrafts.profile(id)):null;
          if(enabled&&!document.payload.hdr_merge)throw Error('HDR 기본 형식을 먼저 서버에서 불러오거나 기존 버전을 복사하세요.');
        }
        if(event.target.id==='capture-pwm-enabled') {
          const sequence=selectedSceneSequence();
          if(enabled&&sequence?._projection)throw Error('시간 장면 원본을 불러온 뒤 PWM을 켜세요.');
          const clock=sequence?.payload?.clock?.clock_id || sequence?.clock?.clock_id;
          if(enabled&&!clock)throw Error('먼저 시간 장면을 선택하세요. PWM과 장면의 Clock ID가 같아야 합니다.');
          document.payload.waveform=enabled?{schema:'camera_light_waveform_v1',kind:'pwm',clock_id:clock,period_ns:1000000,on_time_ns:500000,phase_origin_ns:0,low:0,high:1}:null;
        }
        state.captureDrafts.set(id,json(document));if(event.target.id==='capture-dark-temperature')dcgUI.markParameterEdit('payload.electrical_model.dark_current_doubling_c');state.captureError='';render();
      } catch(error) {state.captureError=error.message;render();}return;
    }
    const select=event.target.dataset.select;
    if(select==='assembly'){state.assembly=event.target.value;state.compatibility=null;state.conditionReport=null;state.conditionError='';state.lfmPlanRun='';state.lfmPlanReport=null;state.lfmPlanError='';state.builderSensor='';state.builderLens='';render();}
    if(event.target.id==='condition-axis'){state.conditionAxis=event.target.value;state.conditionReport=null;state.conditionError='';render();}
    if(event.target.id==='lfm-plan-run'){state.lfmPlanRun=event.target.value;state.lfmPlanReport=null;state.lfmPlanError='';render();}
    if(event.target.id==='run-runtime'){state.runtime=event.target.value;clearSceneTest();render();}
    if(event.target.id==='run-scene'){state.scene=event.target.value;clearSceneTest();render();}
    if(event.target.id==='run-assembly'){state.assembly=event.target.value;state.captureProgram='';if(isSceneRuntime()){clearSceneTest();state.controlSequence='';}render();}
    if(event.target.id==='run-test'){state.test=event.target.value;state.validationTest=event.target.value;render();}
    if(event.target.id==='validation-run'){chooseRun(event.target.value);render();}
    if(event.target.id==='validation-measurement'){state.measurement=event.target.value;render();}
    if(event.target.id==='validation-test'){state.validationTest=event.target.value;render();}
    if(event.target.id==='validation-calibrated'){state.allowCalibrated=event.target.checked;render();}
    if(select==='run'){chooseRun(event.target.value);render();}
    if(event.target.id==='calibration-base'){state.calibrationBase=event.target.value;state.calibrationMeasurements=[];state.calibrationBounds='';render();}
    if(event.target.id==='calibration-report'){state.calibrationReport=event.target.value;render();}
    if(event.target.dataset.frameJob){const jobId=event.target.dataset.frameJob;state.frameSelections[jobId]=event.target.value;render();document.getElementById('frame-'+jobId)?.focus({preventScroll:true});}
    if(event.target.id==='run-scene-sequence' || event.target.id==='temporal-scene-sequence'){state.sceneSequence=event.target.value;state.controlSequence='';clearSceneTest();render();}
    if(event.target.id==='run-control-sequence'){state.controlSequence=event.target.value;clearSceneTest();render();}
    if(event.target.id==='temporal-assembly'){state.assembly=event.target.value;state.captureProgram='';state.controlSequence='';clearSceneTest();render();}
    if(event.target.dataset.sequenceUpload && event.target.files?.[0])readSequenceFile(event.target.files[0],event.target.dataset.sequenceUpload);
    if(event.target.id==='factory-condition-scope'){state.factoryConditionScope=event.target.value;render();}
    if(event.target.id==='scene-assembly'){state.assembly=event.target.value;render();}
    if(event.target.id==='scene-file'){const file=event.target.files?.[0];if(file){if(!file.name.toLowerCase().endsWith('.npy')||file.size>64*1024*1024){event.target.value='';toast('64 MiB 이하의 NPY 파일을 선택하세요.',true);}else{state.sceneFile=file;render();}}}
    if(event.target.id==='replay-source'){state.replaySource=event.target.value;render();}
    if(event.target.id==='replay-isp'){state.replayISP=event.target.value;render();}
    if(event.target.id==='replay-job'){state.replayJob=event.target.value;render();}
    if(event.target.id==='factory-base-lens'){state.factoryBaseLens=event.target.value;render();}
    if(event.target.id==='factory-plugin'){state.factoryPlugin=event.target.value;render();}
    if(event.target.id==='factory-job-select'){state.factoryJob=event.target.value;state.factoryModelName='';state.factoryModelRevision='1';state.factoryConditionsAssembly='';state.factoryBaseLens='';state.factoryConditionScope='all_declared';render();}
    if(event.target.id==='factory-conditions-assembly'){state.factoryConditionsAssembly=event.target.value;render();}
    if(event.target.hasAttribute('data-calibration-measurement'))state.calibrationMeasurements=Array.from(document.querySelectorAll('[data-calibration-measurement]:checked')).map(input=>input.value);
    if(event.target.dataset.captureFile){
      const file=event.target.files?.[0];if(!file)return;
      if(!file.name.toLowerCase().endsWith('.npy')){event.target.value='';toast('변환하지 않은 NPY 파일을 선택하세요. 서버에서 배열 형식과 크기를 검사합니다.',true);return;}
      if(file.size>64*1024*1024){event.target.value='';toast('파일은 64 MiB 이하여야 합니다.',true);return;}
      state[event.target.dataset.captureFile==='raw'?'captureRaw':'captureDark']=file;render();
    }
    if(event.target.dataset.upload && event.target.files?.[0])readUpload(event.target.files[0],event.target.dataset.upload);
  });
  document.addEventListener('input',event=> {
    if(electricalResponseUI.input(event.target))return;
    if(retainedISPUI.input(event.target))return;
    if(ispStudyUI.input(event.target))return;
    if(advancedISPUI.input(event.target))return;
    if(event.target.id==='capture-program-json'){state.captureDrafts.set(state.assembly,event.target.value);state.captureError='';return;}
    if(event.target.dataset.captureField) {
      try {state.captureDrafts.update(state.assembly,event.target.dataset.captureField,event.target.type==='number'&&event.target.value!==''?Number(event.target.value):event.target.value);if(event.target.dataset.captureField==='payload.electrical_control_policy.period_lattice.period_ns')state.captureDrafts.update(state.assembly,'payload.waveform.period_ns',Number(event.target.value));if(event.target.dataset.captureField==='payload.electrical_model.read_noise.covariance_e2.0.1')state.captureDrafts.update(state.assembly,'payload.electrical_model.read_noise.covariance_e2.1.0',Number(event.target.value));dcgUI.markParameterEdit(event.target.dataset.captureField);state.captureDrafts.set(state.assembly,json(CameraRetainedParameterEdit(CameraNonlinearParameterEdit(state.captureDrafts.parse(state.assembly),event.target.dataset.captureField),event.target.dataset.captureField)));state.captureError='';const source=$('#capture-program-json');if(source)source.value=state.captureDrafts.get(state.assembly);if(event.target.dataset.captureField==='payload.hdr_merge.reference_frame_index'){const submit=event.target.form?.querySelector('button[type="submit"]');if(submit)submit.disabled=Boolean(state.captureSaving)||!CameraReferenceHDRValid(state.captureDrafts.parse(state.assembly));}}
      catch(error){state.captureError=error.message;toast(error.message,true);}return;
    }
    if(event.target.id==='assembly-contract'){state.builderContracts ||= {};state.builderContracts[event.target.form.dataset.base]=event.target.value;}
    if(event.target.id==='condition-values')state.conditionValues=event.target.value;
    if(event.target.id==='lfm-plan-draft')state.lfmPlanDraft=event.target.value;
    if(event.target.id==='temporal-scene-json')state[state.temporalSceneMode==='fixture'?'temporalFixtureDraft':'temporalSceneDraft']=event.target.value;
    if(event.target.id==='control-sequence-json')state.controlDraft=event.target.value;
    if(event.target.id==='temporal-test-json')state.temporalTestDraft=event.target.value;
    if(event.target.id==='scene-json')state[state.sceneInputMode==='fixture'?'sceneDraft':'sceneMetadata']=event.target.value;
    if(event.target.id==='replay-name')state.replayName=event.target.value;
    if(event.target.id==='spatial-test-json')state.spatialTestDraft=event.target.value;
    if(event.target.id==='run-exposure'){state.exposure=event.target.value;refreshCaseContext();}
    if(event.target.id==='retained-noise-enabled'){state.retainedNoiseEnabled=event.target.checked;}
    if(event.target.id==='run-seed'){state.seed=event.target.value;refreshCaseContext();}
    if(event.target.id==='model-json')state.modelDraft=event.target.value;
    if(event.target.id==='native-sensor-base')state.nativeSensorBase=event.target.value;
    if(event.target.id==='measurement-json')state.measurementDraft=event.target.value;
    if(event.target.id==='capture-json')state.captureDraft=event.target.value;
    if(event.target.id==='calibration-bounds')state.calibrationBounds=event.target.value;
    if(event.target.id==='calibration-name')state.calibrationName=event.target.value;
    if(event.target.id==='calibration-revision')state.calibrationRevision=event.target.value;
    if(event.target.id==='factory-json')state.factoryDrafts[state.factoryPlugin]=event.target.value;
    if(event.target.id==='factory-model-name')state.factoryModelName=event.target.value;
    if(event.target.id==='factory-model-revision')state.factoryModelRevision=event.target.value;
    if(event.target.id!=='asset-search')return;
    const position=event.target.selectionStart;state.search=event.target.value;render();const input=$('#asset-search');input.focus();try{input.setSelectionRange(position,position);}catch{}
  });
  async function readUpload(file,kind) {
    try{
      if(file.size>MAX_JSON_BYTES)throw new Error('1 MB(1,000,000 bytes) 이하의 JSON 메타데이터 파일을 사용하세요.');
      const text=await file.text();const parsed=JSON.parse(text);
      if(!parsed || typeof parsed!=='object' || Array.isArray(parsed))throw new Error('최상위 JSON 객체가 필요합니다.');
      const editor=$('#'+kind+'-json');editor.value=text;state[kind+'Draft']=editor.value;
      if(kind==='measurement')state.uploadedMeasurement={rawText:text,editorText:editor.value};
      toast(`${file.name} · 내용을 불러왔습니다. 등록 버튼으로 저장하세요.`);
    }catch(error){toast(error instanceof SyntaxError?'파일이 유효한 JSON이 아닙니다.':error.message,true);}
  }
  ['dragenter','dragover','dragleave','drop'].forEach(type=>document.addEventListener(type,event=> {
    const zone=event.target.closest('[data-drop]');if(!zone)return;event.preventDefault();
    zone.classList.toggle('dragover',type==='dragenter'||type==='dragover');
    if(type==='drop' && event.dataTransfer.files[0])readUpload(event.dataTransfer.files[0],zone.dataset.drop);
  }));
  window.addEventListener('hashchange',()=> {const view=location.hash.slice(1);state.view=views.some(item=>item.id===view)?view:'overview';render();window.scrollTo({top:0,behavior:'instant'});});
  async function start() {
    const view=location.hash.slice(1);state.view=views.some(item=>item.id===view)?view:'overview';render();
    try{await refresh();}catch(error){$('#connection').className='connection error';$('#connection').innerHTML='<i></i> 연결 실패';$('#main').innerHTML=`${heading('CONNECTION REQUIRED','플랫폼 서버에 연결하지 못했습니다.','현재 주소의 API와 작업공간 식별자를 확인하세요.')}<div class="callout error">${esc(error.message)}</div><div class="inline-spacer"></div><button class="button primary" data-action="refresh">다시 연결</button>`;}
  }
  start();
})();

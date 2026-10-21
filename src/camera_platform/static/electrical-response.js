'use strict';

// Pure view and draft transitions. The app owns requests, polling and saved records.
function CameraElectricalResponseUI(ctx) {
  const esc=ctx.esc, list=value=>Array.isArray(value)?value:[], copy=value=>JSON.parse(JSON.stringify(value));
  const json=value=>JSON.stringify(value,null,2), ids=['assembly_id','scene_sequence_id','control_sequence_id','capture_program_id'];
  const scopeNote='동일한 고정 입력에서 고정 노출과 RAW 관측 AE를 비교합니다. 프레임 ID로 대응하고 각 실행의 실제 관측 준비 시각을 보존합니다. 같은 seed는 서로 다른 노출의 픽셀별 잡음 일치를 보장하지 않습니다. 코드 변화·목표 만족은 물리 LFM 개선, 제어 안정성 또는 자동 최적해를 뜻하지 않습니다.';
  const canonical=value=>JSON.stringify(value,(_key,item)=>item&&typeof item==='object'&&!Array.isArray(item)?Object.fromEntries(Object.keys(item).sort().map(k=>[k,item[k]])):item);
  const finite=value=>typeof value==='number'&&Number.isFinite(value)?esc(ctx.number?ctx.number(value,4):String(value)):'미관측';
  const ns=value=>Number.isSafeInteger(value)?`${esc(value)} ns`:'미관측';
  const status=value=>esc({complete:'관측 유지 조건 충족',not_reached:'미도달',censored:'관측 범위 부족',not_applicable:'해당 없음',observed:'관측',missing:'미관측'}[value]||value||'미관측');
  const details=(label,value)=>`<details class="data-detail"><summary>${esc(label)}</summary><pre>${esc(json(value))}</pre></details>`;
  const panel=(title,content)=>`<section class="panel"><div class="panel-header"><h2>${esc(title)}</h2></div><div class="panel-content">${content}</div></section>`;
  const option=(id,label,current)=>`<option value="${esc(id)}" ${String(id)===String(current)?'selected':''}>${esc(label)}</option>`;
  const button=(s,action,label,disabled=false)=>`<button type="button" class="button secondary" data-electricalresponse-action="${action}" ${disabled||s.submitting||s.loading?'disabled':''}>${esc(label)}</button>`;
  const selectField=(field,label,values,current,disabled=false)=>`<div class="form-field"><label for="electricalresponse-${field}">${esc(label)}</label><select id="electricalresponse-${field}" data-electricalresponse-field="${field}" ${disabled?'disabled':''}>${values.map(x=>option(x.id,x.label,current)).join('')}</select></div>`;
  function invalidate(s) {s.epoch=(s.epoch||0)+1;s.plan=null;s.planText='';s.planning=false;s.error='';}
  function setText(s,text) {if(s.submitting)return false;s.text=String(text);invalidate(s);return true;}
  function setBindings(s,bindings) {
    if(s.submitting)return false;
    const next={...s.bindings,...Object.fromEntries(ids.filter(k=>Object.hasOwn(bindings,k)).map(k=>[k,bindings[k]]))};
    if(canonical(next)!==canonical(s.bindings||{})){s.bindings=next;invalidate(s);}
    return true;
  }
  function specification(s) {
    const spec=JSON.parse(s.text);
    if(!spec||Array.isArray(spec)||typeof spec!=='object')throw Error('비교 명세는 JSON 객체여야 합니다.');
    if(ids.some(k=>!s.bindings?.[k]||spec[k]!==s.bindings[k]))throw Error('비교 초안과 현재 고정 입력이 다릅니다. 선택한 입력으로 초안을 다시 만드세요.');
    return spec;
  }
  // Force controls queue reuse; it is intentionally outside the immutable plan.
  const plannedSpec=spec=>{const result=copy(spec);delete result.force;return result;};
  function beginPlan(s) {
    if(s.submitting||s.loading)throw Error('진행 중인 요청이 끝난 뒤 계획을 검사하세요.');
    const spec=specification(s);invalidate(s);s.planning=true;
    return {epoch:s.epoch,text:s.text,bindings:copy(s.bindings),specification:copy(spec)};
  }
  function currentTicket(s,ticket) {return ticket&&ticket.epoch===s.epoch&&ticket.text===s.text&&canonical(ticket.bindings)===canonical(s.bindings);}
  function acceptPlan(s,ticket,response) {
    if(!currentTicket(s,ticket))return false;
    s.planning=false;
    const frozen=response?.frozen_request?.specification;
    if(!/^[a-f0-9]{64}$/.test(response?.plan_hash||'')||!frozen||canonical(plannedSpec(frozen))!==canonical(plannedSpec(specification(s))))throw Error('검사된 계획이 요청한 고정 입력 또는 평가 명세와 다릅니다.');
    s.plan=copy(response);s.planText=s.text;s.error='';return true;
  }
  function rejectPlan(s,ticket,error) {if(!currentTicket(s,ticket))return false;s.planning=false;s.plan=null;s.planText='';s.error=error?.message||String(error);return true;}
  function submitPayload(s) {
    if(s.planning||s.submitting||s.loading||!s.plan||s.planText!==s.text)throw Error('현재 입력의 계획을 다시 검사하세요.');
    const spec=specification(s),frozen=s.plan.frozen_request?.specification;
    if(!frozen||canonical(plannedSpec(spec))!==canonical(plannedSpec(frozen)))throw Error('검사 후 입력이 변경됐습니다. 계획을 다시 검사하세요.');
    return {specification:{...copy(frozen),...(Object.hasOwn(spec,'force')?{force:spec.force}:{})},plan_hash:s.plan.plan_hash};
  }
  function select(s,selection) {
    if(Object.hasOwn(selection,'cell')&&selection.cell!==s.cell){s.cell=String(selection.cell);s.pair='';s.frame='';}
    if(Object.hasOwn(selection,'pair')&&selection.pair!==s.pair){s.pair=String(selection.pair);s.frame='';}
    if(Object.hasOwn(selection,'frame'))s.frame=String(selection.frame);
  }
  function evaluation(spec) {
    const e=spec?.evaluation;if(!e)return '<p class="callout">평가 명세 없음 · 유효한 초안을 생성하세요.</p>';
    return `<h3>고정 평가 구간과 관측 유지 조건</h3><p>목표: ${finite(e.target_black_relative_dn?.minimum)}–${finite(e.target_black_relative_dn?.maximum)} signed RAW−black DN<br>구간: ${ns(e.window?.start_ns)} → ${ns(e.window?.end_ns)} · 선언 변화: ${ns(e.window?.change_point_ns)}<br>연속 관측: ${Number.isSafeInteger(e.hold?.consecutive_observations)?esc(e.hold.consecutive_observations):'미지정'}개 · 최소 관측 span: ${ns(e.hold?.minimum_span_ns)}<br>자극: ${esc(e.stimulus?.kind||'미지정')} · 경계 노출: ${esc(e.crossing_exposure_policy||'미지정')}</p>${details('평가 ROI·native 사이트·clock·순서가 있는 프레임',e)}`;
  }
  function author(s) {
    let spec;try{spec=JSON.parse(s.text||'');}catch(_error){}
    const labels={assembly_id:['assemblies','고정 카메라 구성'],scene_sequence_id:['scene_sequences','시간 장면'],control_sequence_id:['control_sequences','시작 시각·초기 제어'],capture_program_id:['capture_programs','등록된 전기 AE 프로그램']};
    const controls=ids.map(k=>{const [collection,label]=labels[k];return selectField(k,label,[{id:'',label:'고정 기록 선택'},...list(s.options?.[collection]).map(row=>({id:row.id,label:`${row.name||row.id}${row.revision?' · r'+row.revision:''}`}))],s.bindings?.[k]||'',s.submitting||s.loading);}).join('');
    return panel('1. 같은 입력으로 고정 노출 · AE 비교',`<div class="form-row">${controls}</div>${button(s,'template','선택한 입력으로 비교 초안',ids.some(k=>!s.bindings?.[k]))}<p class="list-note">등록 기록을 수정하지 않습니다. 초안 변경 또는 입력 선택 변경 후에는 계획을 다시 검사해야 합니다.</p><div class="form-field"><label for="electricalresponse-json">축·seed·noise·평가 목표·구간·유지 조건 · 전체 JSON</label><textarea id="electricalresponse-json" data-electricalresponse-field="json" spellcheck="false" ${s.submitting?'disabled':''}>${esc(s.text||'')}</textarea></div>${evaluation(spec)}<p>Noise: ${spec?.noise_enabled===true?'확률 실행':spec?.noise_enabled===false?'결정론 흐름':'미지정'} · seed 목록: ${esc(json(spec?.seeds)||'미지정')}</p>${details('명시적으로 선택한 실험 축',spec?.axes||[])}${button(s,'plan',s.planning?'계획 검사 중…':'계획과 자원 검사',!s.text||s.planning)}${s.plan&&s.planText===s.text?`<div data-electricalresponse-plan="${esc(s.plan.plan_hash)}"><p>고정 계획 SHA-256 <span class="case-hash">${esc(s.plan.plan_hash)}</span></p>${details('고정 입력·평가·전체 실행 수와 자원 상한',s.plan)}${button(s,'submit',s.submitting?'제출 중…':'검사한 비교 작업 제출',s.planning)}</div>`:''}`);
  }
  function artifact(record,id) {return list(record.artifacts||record.result?.artifacts).find(a=>a.id===id&&/^[a-f0-9]{64}$/.test(a.sha256||'')&&a.id===a.sha256&&a.url==='/api/v1/artifacts/'+a.sha256);}
  function evidence(record,part,frame,label) {
    if(!frame)return '<p>원본 프레임 근거 없음</p>';
    const foundPNG=artifact(record,frame.preview_artifact_id),foundRAW=artifact(record,part?.raw_artifact?.id);
    const png=foundPNG&&(!frame.preview_url||frame.preview_url===foundPNG.url)?foundPNG:null;
    const raw=foundRAW&&foundRAW.sha256===part.raw_artifact.sha256?foundRAW:null;
    return `${png&&png.media_type==='image/png'?`<figure><img class="preview-image" src="${esc(png.url)}" alt="${esc(label)} · ${esc(frame.frame_id)}"><figcaption>${esc(label)} · ${esc(frame.frame_id)} · 저장 RAW 표시 PNG</figcaption></figure>`:'<p>검증된 저장 PNG 없음</p>'}${[raw,png].filter(Boolean).map(a=>`<p><a class="link-button" href="${esc(a.url)}" download>${esc(a===raw?'원본 RAW 전체 프레임':'원본 PNG')} ↓</a> <span class="case-hash">SHA-256 ${esc(a.sha256)}</span></p>`).join('')}`;
  }
  function outcome(value) {
    if(!value)return '<p>구간 평가 근거 없음</p>';
    return `<p data-electricalresponse-settling>${status(value.status)}${value.reason?' · '+esc(value.reason):''}<br>진입 지연: ${ns(value.entry_latency_ns)} · 확인 지연: ${ns(value.confirmation_latency_ns)}<br>실제 관측 유지 span: ${ns(value.observed_hold_span_ns)}</p>`;
  }
  function branch(record,part,frameId,label) {
    const e=part?.evaluation,observed=list(e?.frames).find(f=>f.frame_id===frameId),frame=list(part?.frames).find(f=>f.frame_id===frameId);
    const trace=part?.control_trace,control=list(trace?.frames).find(f=>f.frame_id===frameId);
    const commands=control?`<h4>실제 제어 이력</h4><p>이 관측의 정책 판정: ${esc(control.proposal?.status||'미관측')}<br>제안 노출: ${ns(control.proposal?.commanded?.exposure_ns)}<br>적용 명령: ${esc(control.applied_command_id||'초기 고정 설정')}<br>Latch: ${ns(control.latch_at_ns)}</p>${details('관측 프레임의 제안과 전체 명령 적용 이력',{frame:control,commands:trace.commands})}`:'';
    return `<div><h3>${esc(label)}</h3><p>원본 Run: ${esc(part?.run_id||'미관측')} · ${esc(part?.status||'미관측')}</p>${part?.error?`<p class="callout error">${esc(typeof part.error==='string'?part.error:json(part.error))}</p>`:''}${part?.observation_readout_id?`<p>고정 AE 관측·응답 평가: ${esc(part.observation_readout_id)} · 저장 PNG 표시: ${esc(part.preview_readout_id||'미확인')} · 두 RAW 경로는 같은 전달 전하를 읽습니다.</p>`:''}${outcome(e?.settling)}<p>선택 프레임 ${esc(frameId)} · ${status(observed?.status)}<br>실제 노출: ${ns(observed?.timing?.applied_exposure_ns)}<br>관측 준비: ${ns(observed?.timing?.statistics_ready_ns)}<br>관측 signed DN: ${finite(observed?.statistics?.value_dn)}<br>목표 이내: ${observed?.in_target===true?'예':observed?.in_target===false?'아니오':'미관측'} · 평가 포함: ${observed?.eligible===true?'예':observed?.eligible===false?'아니오':'미관측'}<br>Digital rail: low ${finite(observed?.statistics?.digital_low_rail_fraction)} / high ${finite(observed?.statistics?.digital_high_rail_fraction)}</p>${list(observed?.exclusion_reasons).length?`<p class="callout">${esc(observed.exclusion_reasons.join(' · '))}</p>`:''}${commands}${evidence(record,part,frame,label)}${details('실제 평가 프레임·노출 경계·관측 형식',{frame:observed||null,raw_format:e?.raw_format||null})}</div>`;
  }
  function comparison(pair,frameId) {
    const frame=list(pair.comparison?.frames).find(f=>f.frame_id===frameId);
    if(!frame)return '<p>선택한 프레임의 대응 평가 없음</p>';
    const rows=[['AE−고정 관측',finite(frame.delta_value_dn),'DN'],['Digital low rail 비율 차이',finite(frame.delta_digital_low_rail_fraction),'비율'],['Digital high rail 비율 차이',finite(frame.delta_digital_high_rail_fraction),'비율'],['실제 적용 노출 차이',ns(frame.delta_applied_exposure_ns),''],['관측 준비 시각 차이',ns(frame.delta_statistics_ready_ns),'']];
    return `<h3>선택한 프레임 · AE−고정 관측 차이</h3><p>${status(frame.status)}${list(frame.missing_or_excluded_reasons).length?' · '+esc(frame.missing_or_excluded_reasons.join(' · ')):''}</p><div class="table-wrap"><table><thead><tr><th>양</th><th>차이 · 단위</th></tr></thead><tbody>${rows.map(([label,value,unit])=>`<tr><th scope="row">${esc(label)}</th><td>${value} ${esc(unit)}</td></tr>`).join('')}</tbody></table></div><p class="list-note">차이의 부호는 AE에서 고정 노출을 뺀 값입니다. 작거나 음수인 결과를 성능 개선으로 자동 판정하지 않습니다.</p>`;
  }
  function result(s) {
    const record=s.record;
    if(!record)return panel('2. 조건별 원본과 관측 응답','<p>저장 결과를 선택하세요. 아직 확인하지 못한 결과의 영상이나 지표는 표시하지 않습니다.</p>');
    let content=`<h3>${esc(record.name||record.specification?.name||record.id)}</h3><p>${esc(record.status)} · ${esc(record.id)}</p>${button(s,'record-refresh','원본 결과 새로고침')}${['queued','running'].includes(record.status)?button(s,'cancel','이 비교 작업 취소'):''}${record.error?`<p class="callout error" role="alert">${esc(typeof record.error==='string'?record.error:json(record.error))}</p>`:''}`;
    if(/^[A-Za-z0-9_-]+$/.test(record.id||''))content+=`<a class="button secondary" href="/api/v1/electrical-response/studies/${esc(record.id)}/export" download>원본 연결 · JSON ↓</a><a class="button secondary" href="/api/v1/electrical-response/studies/${esc(record.id)}/bundle" download>입력·RAW·관측 평가 근거 ZIP ↓</a>`;
    const summary=record.result?.summary||record.summary;
    const terminalEvidence=record.status==='failed'&&summary?.status==='no_complete_pairs';
    if(record.status!=='succeeded'&&!terminalEvidence)return panel('2. 조건별 원본과 관측 응답',content+'<p>완료된 비교 결과 없음 · 실패·취소·진행 중 상태를 성공으로 표시하지 않습니다.</p>');
    if(terminalEvidence||summary?.status==='partial')content+=`<p class="callout error">${terminalEvidence?'완료된 비교 쌍 없음 · 실패한 작업의 남은 원본 근거':'일부 비교 쌍만 완료'} · ${esc(summary.complete_pair_count??'미확인')} / ${esc(summary.planned_pair_count??'미확인')}쌍. 누락·실패를 성능 결과로 합치지 않습니다.</p>`;
    const cells=list(summary?.cells),groups=[];
    for(const cell of cells){const key=canonical(cell.coordinates);let group=groups.find(g=>g.key===key);if(!group){group={key,cell_id:cell.cell_id,coordinates:cell.coordinates,pairs:[]};groups.push(group);}group.pairs.push(cell);}
    const cell=groups.find(c=>c.cell_id===s.cell)||groups[0],pairs=list(cell?.pairs),pair=pairs.find(p=>p.cell_id===s.pair)||pairs[0];
    if(!pair)return panel('2. 조건별 원본과 관측 응답',content+'<p class="callout">완료 상태이나 쌍별 결과 근거가 없습니다. 원본 결과를 확인하세요.</p>');
    content+=`<h3>이 저장 결과의 평가 계약</h3>${evaluation(summary.specification)}`;
    const frameIds=list(pair.baseline?.evaluation?.specification?.frame_ids||pair.candidate?.evaluation?.specification?.frame_ids||summary.specification?.evaluation?.frame_ids),frameId=frameIds.includes(s.frame)?s.frame:frameIds[0];
    const noise=summary.specification?.noise_enabled??summary.noise_enabled;
    content+=`<div class="form-row">${selectField('cell','실험 조건',groups.map(c=>({id:c.cell_id,label:c.cell_id+' · '+json(c.coordinates)})),cell.cell_id)}${selectField('pair','조건별 seed 쌍',pairs.map(p=>({id:p.cell_id,label:p.cell_id+' · seed '+p.seed})),pair.cell_id)}${selectField('frame','원본 프레임 ID',frameIds.map(id=>({id,label:id})),frameId)}</div><p>Noise: ${noise===true?'확률 실행':noise===false?'결정론 흐름':'미지정'} · seed ${esc(pair.seed??'미지정')}</p><div class="two-columns">${branch(record,pair.baseline,frameId,'고정 노출 기준선')}${branch(record,pair.candidate,frameId,'RAW 관측 AE')}</div>${comparison(pair,frameId)}${details('동일 프레임의 AE−고정 관측 차이 · 전체',pair.comparison||null)}${details('선언 평가·전체 조건·명령·원본 연결',summary)}`;
    return panel('2. 조건별 원본과 관측 응답',content);
  }
  function render(s) {
    return `<div id="electricalresponse-workspace" class="stack" aria-busy="${!!(s.loading||s.planning||s.submitting)}">${s.error?`<p class="callout error" role="alert">${esc(s.error)}</p>`:''}${s.loading?'<p role="status">고정 입력과 저장 결과를 확인 중입니다…</p>':''}<div class="case-toolbar">${button(s,'refresh','입력·저장 작업 새로고침')}${selectField('history','저장된 비교 작업',[{id:'',label:'결과 선택'},...list(s.history).map(r=>({id:r.id,label:`${r.name||r.specification?.name||r.id} · ${r.status}`}))],s.selectedId||'',s.submitting||s.loading)}</div><div class="callout neutral">${scopeNote}</div>${author(s)}${result(s)}</div>`;
  }
  return {render,setText,setBindings,beginPlan,acceptPlan,rejectPlan,submitPayload,select,scopeNote};
}

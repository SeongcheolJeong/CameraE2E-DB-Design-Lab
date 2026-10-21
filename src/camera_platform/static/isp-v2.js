'use strict';

// Kept independent from the v1 replay form. No acquisition or replay is started
// here: the host selects the returned immutable ISP model in its existing form.
window.CameraAdvancedISPUI = function(ctx) {
  const {api,esc,render,refresh,toast,source,onModel}=ctx;
  const drafts=new Map(),resultFrames=new Map(), json=value=>JSON.stringify(value,null,2), list=value=>Array.isArray(value)?value:[];
  const copy=value=>JSON.parse(JSON.stringify(value));
  const key=value=>value ? [value.source_kind,value.source_id,value.raw_sha256,...(value.selector?[JSON.stringify([value.selector.schema,value.selector.mode,value.selector.readout_id||'',...(value.selector.readout_calibration_id?[value.selector.readout_calibration_id]:[])])]:[])].join(':') : '';
  const detail=(title,value)=>`<details class="data-detail"><summary>${esc(title)}</summary><pre>${esc(json(value))}</pre></details>`;
  const native=value=>value?.geometry?.kind==='native_sensor' && list(value.raw_shape).length===3 && (['camera_nonlinear_raw_selection_v1','camera_nonlinear_raw_selection_v2'].includes(value.selector?.schema)||value.raw_format?.hdr_streams===1);
  const charge=value=>['camera_nonlinear_raw_selection_v1','camera_nonlinear_raw_selection_v2'].includes(value?.selector?.schema);
  const canonical=value=>JSON.stringify(value,(_key,item)=>item && !Array.isArray(item) && typeof item==='object' ? Object.fromEntries(Object.keys(item).sort().map(k=>[k,item[k]])) : item);
  function parse(text) {
    if(new TextEncoder().encode(text).byteLength>1048576)throw Error('JSON은 UTF-8 1 MiB 이하여야 합니다.');
    const doc=JSON.parse(text,(_k,v)=>{if(typeof v==='number' && !Number.isFinite(v))throw Error('유한한 숫자만 입력하세요.');return v;});
    if(!doc || typeof doc!=='object' || Array.isArray(doc))throw Error('객체 형식의 원본 JSON을 입력하세요.');
    return doc;
  }
  function state() {
    const current=source(),id=key(current);
    if(!drafts.has(id))drafts.set(id,{epoch:0,calibrationText:'',calibrationEdited:false,calibrationId:'',calibrations:[],profiles:new Map(),invalid:new Map(),row:0,column:0,pending:false,error:'',notice:'',uncertain:null,savedCalibration:''});
    return {s:drafts.get(id),current,id};
  }
  function profile(s) {
    if(!s.profiles.has(s.calibrationId))s.profiles.set(s.calibrationId,{text:'',saved:''});
    return s.profiles.get(s.calibrationId);
  }
  const selected=s=>s.calibrations.find(item=>item.id===s.calibrationId);
  const prefix=(s,kind)=>kind+':'+(kind==='profile'?s.calibrationId:'')+':';
  const clearInvalid=(s,kind)=>{for(const k of s.invalid.keys())if(k.startsWith(prefix(s,kind)))s.invalid.delete(k);};
  const fresh=(snapshot)=>key(source())===snapshot.id && snapshot.s.epoch===snapshot.epoch;
  const setError=(s,error)=>{s.error=error?.message || String(error);};
  function estimated(doc,kind) {
    Object.assign(doc,{source_kind:'estimated',source_uri:'fixture://workbench/advanced-isp-authored',physical_instance:null,method_and_version:'workbench_advanced_isp_parameter_edit:1'});
    if(kind==='calibration') {
      doc.instrument=null;
      doc.uncertainty={kind:'unknown',reason:'Authored calibration assumptions; numeric edits are not a physical calibration.'};
      doc.dataset_role='development';
    }
    return doc;
  }
  function setPath(doc,path,value) {
    const keys=path.split('.');
    if(keys.some(k=>['__proto__','constructor','prototype'].includes(k)))throw Error('허용하지 않는 필드입니다.');
    let parent=doc;
    for(const k of keys.slice(0,-1)){if(parent==null || !Object.hasOwn(parent,k))throw Error('원본 JSON의 필드가 없습니다.');parent=parent[k];}
    const end=keys.at(-1);if(parent==null || !Object.hasOwn(parent,end))throw Error('원본 JSON의 필드가 없습니다.');parent[end]=value;
  }
  function field(kind,path,label,value,{unit='',min,max,step='any',type='number'}={}) {
    const id=`ispv2-${kind}-${path.replaceAll('.','-')}`;
    const {s}=state(),invalid=prefix(s,kind)+path;if(s.invalid.has(invalid))value=s.invalid.get(invalid);
    return `<div class="form-field"><label for="${esc(id)}">${esc(label)}${unit?' · '+esc(unit):''}</label><input id="${esc(id)}" data-ispv2-edit="${kind}" data-path="${esc(path)}" type="${type}" value="${esc(value??'')}" ${type==='number'?`step="${esc(step)}" ${min===undefined?'':`min="${min}"`} ${max===undefined?'':`max="${max}"`}`:''} required></div>`;
  }
  const checkbox=(path,label,checked)=>`<label class="check-option" for="ispv2-${path}"><input id="ispv2-${path}" data-ispv2-mode="${path}" type="checkbox" ${checked?'checked':''}><span>${esc(label)}</span></label>`;
  function calibrationEditor(s) {
    let doc=null,error='';try{if(s.calibrationText)doc=parse(s.calibrationText);}catch(e){error=e.message;}
    const lsc=doc?.payload?.lsc,noise=doc?.payload?.noise,sites=['R','Gr','Gb','B'];
    let grid='';
    if(lsc && Array.isArray(lsc.gains) && Array.isArray(lsc.row_nodes) && Array.isArray(lsc.column_nodes)) {
      const row=Math.min(s.row,Math.max(0,lsc.row_nodes.length-1)),col=Math.min(s.column,Math.max(0,lsc.column_nodes.length-1));
      const gains=lsc.gains[row]?.[col];
      if(Array.isArray(gains)&&gains.length===4)grid=`<h4>공간 보정 격자 · 선택한 노드의 4사이트 gain</h4><div class="form-row">${['row','column'].map((axis,i)=>`<div class="form-field"><label for="ispv2-grid-${axis}">${i?'센서 열':'센서 행'} 좌표</label><select id="ispv2-grid-${axis}" data-ispv2-field="${axis}">${lsc[axis+'_nodes'].map((n,j)=>`<option value="${j}" ${j===(i?col:row)?'selected':''}>${esc(n)} · node ${j}</option>`).join('')}</select></div>`).join('')}</div><div class="form-row">${sites.map((site,i)=>field('calibration',`payload.lsc.gains.${row}.${col}.${i}`,site,gains[i],{unit:'×',min:.25,max:16})).join('')}</div><p class="list-note">전체 센서 좌표의 선언된 격자입니다. 현재 영상에서 자동 추정하지 않으며 Gr와 Gb를 분리합니다. 유효 마스크·기준 ROI는 원본 JSON에서 확인하세요.</p>`;
    }
    const noiseFields=noise && Array.isArray(noise.slope_dn)&&Array.isArray(noise.intercept_dn2)?`<h4>필터용 잡음 보정 가정 · RAW DN 기준</h4>${sites.map((site,i)=>`<div class="form-row">${field('calibration',`payload.noise.slope_dn.${i}`,site+' 신호 기울기',noise.slope_dn[i],{unit:'DN',min:0})}${field('calibration',`payload.noise.intercept_dn2.${i}`,site+' 분산 절편',noise.intercept_dn2[i],{unit:'DN²',min:0})}</div>`).join('')}`:'';
    return `<details class="data-detail" ${s.calibrationText?'open':''}><summary>새 보정 작성 · 추정 형식 또는 원본 가져오기</summary><div class="form-row"><button type="button" class="button secondary" data-ispv2-action="calibration-template" ${s.pending?'disabled':''}>추정 보정 형식 불러오기${s.calibrationText?' · 초안 교체':''}</button><div class="form-field"><label for="ispv2-calibration-file">보정 JSON 가져오기</label><input id="ispv2-calibration-file" type="file" accept=".json,application/json" data-ispv2-file="calibration" ${s.pending?'disabled':''}></div></div><form data-ispv2-form="calibration"><fieldset ${s.pending||s.uncertain?'disabled':''}>${doc?`<div class="form-row">${field('calibration','name','새 보정 이름',doc.name,{type:'text'})}${field('calibration','revision','새 버전',doc.revision,{type:'text'})}</div><p class="list-note">출처: <strong>${esc(s.calibrationEdited?'estimated · 편집한 가정':doc.source_kind)}</strong> · ${esc(doc.source_uri || '출처 URI 미입력')}</p>${grid}${noiseFields}${detail('고정 원본 · 보정 적용 범위 · 불확실도',{reference_source:doc.reference_source,binding:doc.binding,domain:doc.domain,uncertainty:doc.uncertainty})}`:''}${error?`<p class="callout error" role="alert">${esc(error)} · 입력 원문은 유지됩니다.</p>`:''}<details class="data-detail" ${error?'open':''}><summary>보정 원본 JSON · 좌표·범위·출처</summary><label for="ispv2-calibration-json">등록할 보정 원문</label><textarea id="ispv2-calibration-json" data-ispv2-text="calibration" spellcheck="false">${esc(s.calibrationText)}</textarea></details><p class="list-note">기본 형식과 수치 편집은 추정 가정입니다. 출처 label만으로 실측 교정·실물 검증을 확립하지 않습니다.</p><button type="submit" class="button secondary full" ${!doc||s.savedCalibration===s.calibrationText?'disabled':''}>새 보정 버전 등록</button></fieldset></form></details>`;
  }
  function profileEditor(s) {
    const draft=profile(s);let doc=null,error='';try{if(draft.text)doc=parse(draft.text);}catch(e){error=e.message;}
    const p=doc?.payload,isCharge=charge(source()),version=isCharge?3:2,valid=p?.schema===`camera_isp_profile_v${version}` && p.spatial_lsc && p.raw_denoise && p.linear_sharpen;
    const mismatch=valid && (p.calibration_id || '')!==s.calibrationId;
    return `<h3>2. 고정 ISP v${version} 프로파일</h3><button type="button" class="button secondary" data-ispv2-action="profile-template" ${s.pending?'disabled':''}>v${version} 프로파일 형식 불러오기${draft.text?' · 초안 교체':''}</button><form data-ispv2-form="profile"><fieldset ${s.pending||s.uncertain?'disabled':''}>${valid?`<div class="form-row">${field('profile','name','새 ISP 모델 이름',doc.name,{type:'text'})}${field('profile','revision','새 버전',doc.revision,{type:'text'})}</div>${checkbox('spatial_lsc','Native 4사이트 공간 LSC 사용',p.spatial_lsc.mode!=='bypass')}${checkbox('raw_denoise','보정 기반 same-site RAW denoise 사용',p.raw_denoise.mode!=='bypass')}${p.raw_denoise.mode!=='bypass'?`<div class="form-row">${field('profile','payload.raw_denoise.strength','Denoise 강도',p.raw_denoise.strength,{min:0,max:1})}${field('profile','payload.raw_denoise.'+(isCharge?'range_floor_e2':'range_floor_dn2'),'Range weight 분모 하한',isCharge?p.raw_denoise.range_floor_e2:p.raw_denoise.range_floor_dn2,{unit:isCharge?'전자²':'DN²',min:0})}</div>`:''}${checkbox('linear_sharpen','선형 RGB의 bounded sharpen 사용',p.linear_sharpen.mode!=='bypass')}${p.linear_sharpen.mode!=='bypass'?`<div class="form-row">${field('profile','payload.linear_sharpen.amount','Sharpen 강도',p.linear_sharpen.amount,{min:0,max:1})}${field('profile','payload.linear_sharpen.threshold','Detail 문턱',p.linear_sharpen.threshold,{unit:'normalized RGB',min:0,max:16})}${field('profile','payload.linear_sharpen.max_delta','최대 추가량',p.linear_sharpen.max_delta,{unit:'normalized RGB',min:0,max:1})}</div>`:''}<p class="list-note">${isCharge?'RAW 역보정 → 기대 암전하 차감 → 고정 전자 수 정규화':'BLC'} → 공간 LSC → same-site denoise → demosaic → 고정 WB → CCM → sharpen → tone. 끈 단계도 bypass로 기록합니다. 원본 RAW와 native 좌표는 바뀌지 않습니다.</p>`:''}${error||mismatch?`<p class="callout error" role="alert">${esc(error||'프로파일의 보정 ID와 선택한 버전이 다릅니다. 해당 보정을 선택하거나 형식을 다시 불러오세요.')} · 입력 원문은 유지됩니다.</p>`:''}<details class="data-detail" ${error?'open':''}><summary>고정 WB·CCM·tone · 전체 ISP JSON</summary><label for="ispv2-profile-json">등록할 ISP 모델 원문</label><textarea id="ispv2-profile-json" data-ispv2-text="profile" spellcheck="false">${esc(draft.text)}</textarea></details><div class="inline-spacer"></div><button type="submit" class="button primary full" ${!valid||mismatch||draft.saved===draft.text?'disabled':''}>새 ISP 버전을 등록하고 재처리용으로 선택</button></fieldset></form>`;
  }
  function editor() {
    const {s,current}=state();
    const head='<section class="panel" id="ispv2-editor"><div class="panel-header"><div><span class="eyebrow">ADVANCED ISP · SAME RETAINED RAW</span><h2>공간 보정·잡음·세부 표현 비교</h2><p>원본에 맞는 ISP 프로파일을 작성해 저장된 RAW를 재처리합니다.</p></div></div><div class="panel-content">';
    if(!native(current))return head+'<p class="callout neutral">원본 RAW에서 단일 stream의 native sensor/crop을 선택하세요. 대표 정규화 격자와 선택되지 않은 DCG 두 읽기 경로는 지원하지 않습니다. 기존 v1 재처리는 위에서 계속 사용할 수 있습니다.</p></div></section>';
    return head+`<div class="locked-reference"><div><strong>${esc(current.name||current.source_id)}</strong><br><code>${esc(current.source_kind)} · ${esc(current.source_id)}</code><small>RAW SHA-256 · ${esc(current.raw_sha256)}</small></div></div>${detail('변경하지 않는 원본 좌표·형식·취득 조건',{geometry:current.geometry,raw_format:current.raw_format,raw_shape:current.raw_shape,selector:current.selector,raw_container_shape:current.raw_container_shape,conditions:current.conditions,frame_conditions:current.frame_conditions})}${s.error?`<p class="callout error" role="alert">${esc(s.error)}</p>`:''}${s.notice?`<p class="callout neutral" role="status">${esc(s.notice)}</p>`:''}${s.uncertain?`<div class="callout error">등록 응답이 불명확하여 중복 등록을 멈췄습니다. 같은 요청의 저장 결과를 확인하세요.<button type="button" class="button secondary" data-ispv2-action="reconcile" ${s.pending?'disabled':''}>등록 결과 다시 확인</button></div>`:''}<h3>1. 불변 보정 버전 고정</h3><div class="form-row"><div class="form-field"><label for="ispv2-calibration-select">${charge(current)?'공간 LSC · 전하 영역':'공간 LSC · 잡음 보정'}</label><select id="ispv2-calibration-select" data-ispv2-field="calibrationId" ${s.pending?'disabled':''}><option value="">${charge(current)?'LSC 보정 없음 · RAW 역보정·잡음 모델은 원본에 고정':'보정 없음 · 보정 단계는 bypass'}</option>${s.calibrations.map(c=>`<option value="${esc(c.id)}" ${c.id===s.calibrationId?'selected':''}>${esc(c.name)} · ${esc(c.revision)} · ${esc(c.source_kind)}</option>`).join('')}</select></div><button type="button" class="button quiet" data-ispv2-action="calibration-list" ${s.pending?'disabled':''}>보정 목록 새로고침</button></div>${selected(s)?detail('선택한 보정의 고정 버전 · 출처 · 불확실도',selected(s)):''}${calibrationEditor(s)}${profileEditor(s)}<p class="list-note">등록 후 위의 재처리 버튼으로 실행하세요. 현재 원본별 초안은 화면 이동·데이터 새로고침·등록 오류에도 유지됩니다. 탭을 닫으면 미등록 초안은 사라집니다.</p></div></section>`;
  }
  function input(target) {
    const kind=target.dataset?.ispv2Edit || target.dataset?.ispv2Text;if(!kind)return false;
    const {s}=state();if(s.pending||s.uncertain)return true;
    const draft=profile(s);s.epoch++;s.error='';s.notice='';
    try {
      if(target.dataset.ispv2Text) {
        clearInvalid(s,kind);if(kind==='calibration'){s.calibrationText=target.value;s.calibrationEdited=true;}else draft.text=target.value;
      } else {
        const doc=parse(kind==='calibration'?s.calibrationText:draft.text),value=target.type==='number'?Number(target.value):target.value;
        if(target.type==='number' && (!target.value.trim()||!Number.isFinite(value))){s.invalid.set(prefix(s,kind)+target.dataset.path,target.value);throw Error('유한한 숫자를 입력하세요.');}
        s.invalid.delete(prefix(s,kind)+target.dataset.path);
        setPath(doc,target.dataset.path,value);
        if(target.dataset.path.startsWith('payload.'))estimated(doc,kind);
        if(kind==='calibration'){s.calibrationText=json(doc);s.calibrationEdited ||= target.dataset.path.startsWith('payload.');}else draft.text=json(doc);
        // Retain focus while typing; the host renders on change or data refresh.
        const area=target.closest?.('#ispv2-editor')?.querySelector?.(`#ispv2-${kind}-json`);if(area)area.value=kind==='calibration'?s.calibrationText:draft.text;
      }
    } catch(error){setError(s,error);}
    return true;
  }
  async function change(target) {
    if(target.dataset?.ispv2Result){resultFrames.set(target.dataset.ispv2Result,Math.max(0,Number(target.value)||0));render();return true;}
    const {s,id}=state(),field=target.dataset?.ispv2Field,mode=target.dataset?.ispv2Mode;
    if(field) {if(s.pending)return true;s.epoch++;s.error='';if(field==='calibrationId')s.calibrationId=target.value;else if(['row','column'].includes(field))s[field]=Math.max(0,Number(target.value)||0);render();return true;}
    if(mode) {
      if(s.pending||s.uncertain)return true;
      try {
        const d=profile(s),doc=parse(d.text),isCharge=charge(source()),names={spatial_lsc:'native_four_site_grid_v1',raw_denoise:isCharge?'local_charge_variance_filter_v1':'calibrated_same_site_filter_v1',linear_sharpen:'bounded_detail_v1'};
        if(!names[mode])return false;
        if(target.checked&&(mode==='spatial_lsc'||(mode==='raw_denoise'&&!isCharge))&&!s.calibrationId)throw Error('LSC·denoise에 사용할 보정 버전을 먼저 고정하세요.');
        doc.payload[mode].mode=target.checked?names[mode]:'bypass';estimated(doc,'profile');d.text=json(doc);s.epoch++;s.error='';
      }catch(error){setError(s,error);}render();return true;
    }
    if(target.dataset?.ispv2File) {
      if(s.pending||s.uncertain)return true;const file=target.files?.[0];if(!file)return true;const epoch=++s.epoch;
      try {
        if(file.size>1048576)throw Error('JSON은 UTF-8 1 MiB 이하여야 합니다.');
        const text=await file.text();parse(text);if(!fresh({s,id,epoch}))return true;
        s.calibrationText=text;s.calibrationEdited=false;s.savedCalibration='';clearInvalid(s,'calibration');s.error='';s.notice='가져온 출처와 적용 범위를 확인하세요. 등록 시 서버가 원본과 보정 계약을 검증합니다.';
      }catch(error){if(fresh({s,id,epoch}))setError(s,error);}render();return true;
    }
    if(target.dataset?.ispv2Edit||target.dataset?.ispv2Text){render();return true;}
    return false;
  }
  function assertSource(current) {if(!native(current))throw Error('지원하는 단일 stream native RAW 원본을 먼저 선택하세요.');}
  const query=current=>'source_kind='+encodeURIComponent(current.source_kind)+'&source_id='+encodeURIComponent(current.source_id)+(charge(current)?'&selector='+encodeURIComponent(JSON.stringify(current.selector)):'');
  async function operation(work) {
    const snapshot=state();if(snapshot.s.pending)return true;
    snapshot.epoch=snapshot.s.epoch;snapshot.s.pending=true;snapshot.s.error='';render();
    try {assertSource(snapshot.current);await work(snapshot);}catch(error){setError(snapshot.s,error);}finally{snapshot.s.pending=false;render();}return true;
  }
  async function acceptRecord(snapshot,kind,record,text) {
    const {s}=snapshot;
    if(!record?.id)throw Error('등록된 불변 기록의 ID가 응답에 없습니다.');
    if(kind==='calibration') {
      if(!s.calibrations.some(c=>c.id===record.id))s.calibrations.unshift(record);
      s.savedCalibration=text;
      if(fresh(snapshot))s.calibrationId=record.id;
    }else {
      profile(s).saved=text;
      if(fresh(snapshot))onModel(record);
    }
    s.uncertain=null;s.notice=`${record.name||record.id} · 불변 버전을 등록했습니다.`;
    try{await refresh({renderPage:false});}catch(error){s.error='등록은 완료되었습니다. 목록 새로고침 오류: '+error.message;}
    toast(s.notice);
  }
  async function click(button) {
    const action=button?.dataset?.ispv2Action;if(!action)return false;
    return operation(async snapshot=>{
      const {s,current}=snapshot;
      if(s.uncertain&&action!=='reconcile')throw Error('먼저 이전 등록 결과를 확인하세요.');
      if(action==='calibration-list') {const rows=await api('/isp-calibrations');if(fresh(snapshot))s.calibrations=list(rows).filter(c=>(c.schema==='camera_charge_isp_calibration_v1')===charge(current));}
      else if(action==='calibration-template') {
        const doc=await api('/templates/isp-calibration?'+query(current)+(charge(current)?'&version=2':''));
        if(fresh(snapshot)){s.calibrationText=json(doc);s.calibrationEdited=false;s.savedCalibration='';clearInvalid(s,'calibration');s.row=0;s.column=0;s.notice='이 형식은 추정 보정 가정입니다. 실제 실측 fitting 결과가 아닙니다.';}
      }else if(action==='profile-template') {
        const doc=await api('/templates/isp-profile?version='+(charge(current)?3:2)+'&'+query(current)+(s.calibrationId?'&calibration_id='+encodeURIComponent(s.calibrationId):''));
        if(fresh(snapshot)){profile(s).text=json(doc);profile(s).saved='';clearInvalid(s,'profile');s.notice='단계를 선택하고 새 이름·버전으로 등록하세요.';}
      }else if(action==='reconcile' && s.uncertain) {
        const pending=s.uncertain,rows=await api(pending.kind==='calibration'?'/isp-calibrations':'/models');
        const matches=list(rows).filter(r=>Object.keys(pending.body).every(k=>canonical(r[k])===canonical(pending.body[k])));
        if(matches.length!==1)throw Error('같은 내용의 저장 결과를 하나로 확인하지 못했습니다. 원본 초안을 유지했습니다. Registry 기록을 확인한 뒤 다시 조회하세요.');
        await acceptRecord(snapshot,pending.kind,matches[0],pending.text);
      }
    });
  }
  async function submit(form) {
    const kind=form?.dataset?.ispv2Form;if(!['calibration','profile'].includes(kind))return false;
    return operation(async snapshot=>{
      const {s,current}=snapshot;if(s.uncertain)throw Error('먼저 이전 등록 결과를 확인하세요.');
      if([...s.invalid.keys()].some(k=>k.startsWith(prefix(s,kind))))throw Error('빈 값 또는 잘못된 숫자를 수정하세요. 이전 수치로 등록하지 않았습니다.');
      const text=kind==='calibration'?s.calibrationText:profile(s).text,body=parse(text);
      if(text===(kind==='calibration'?s.savedCalibration:profile(s).saved)){s.notice='이 초안은 이미 등록했습니다. 수정을 새 버전으로 등록하거나 기존 버전을 사용하세요.';return;}
      if(kind==='calibration') {
        if(body.reference_source?.kind!==current.source_kind || body.reference_source?.id!==current.source_id)throw Error('보정 초안의 기준 원본이 현재 RAW와 다릅니다. 원본을 바꾸거나 해당 원본의 형식을 불러오세요.');
        if(charge(current)&&canonical(body.reference_source?.selector)!==canonical(current.selector))throw Error('보정 초안의 DCG 경로·RAW 선택이 현재 원본과 다릅니다.');
        if(s.calibrationEdited)estimated(body,kind);
      }else {
        if(body.kind!=='isp'||body.payload?.schema!==`camera_isp_profile_v${charge(current)?3:2}`)throw Error('선택한 RAW에 맞는 ISP 버전의 모델 원문이 필요합니다.');
        if((body.payload.calibration_id||'')!==s.calibrationId)throw Error('프로파일과 선택한 보정 버전이 다릅니다.');
        estimated(body,'profile');
      }
      const pending={kind,body:copy(body),text};let record;
      s.uncertain=pending;
      try {record=await api(kind==='calibration'?'/isp-calibrations':'/models',body);}
      catch(error){if(error.status>=400&&error.status<500)s.uncertain=null;throw error;}
      // A later refresh failure must never turn a completed POST into a retry.
      await acceptRecord(snapshot,kind,record,text);
    });
  }
  function result(job) {
    const summary=job?.result?.summary||{},p=job?.profile||summary.profile||job?.isp_model_snapshot?.payload;
    if(!['camera_isp_profile_v2','camera_isp_profile_v3'].includes(p?.schema) && summary.schema!=='camera_isp_profile_v2' && summary.implementation!=='camera_platform_float_isp_v2')return '';
    const version=p?.schema==='camera_isp_profile_v3'?3:2;
    const stages=summary.stage_outputs||{},stageNames=new Set(Object.values(stages).flatMap(s=>[s.array+'.npy',s.validity+'.npy']));
    const artifacts=list(job.artifacts),links=artifacts.filter(a=>a.name?.endsWith('.npy')&&(stageNames.has(a.name)||/(isp_|lsc|denois|sharpen|mask|valid|variance|linear|blc|baseline|charge|quantization)/.test(a.name)));
    const link=a=>{const url=String(a.url||'');return /^\/api\/v1\/artifacts\/[A-Za-z0-9._/-]+$/.test(url)?`<a class="button secondary small" href="${esc(url)}" download title="SHA-256 ${esc(a.sha256)}">${esc(a.name)} ↓</a>`:'';};
    const previews=list(summary.stage_previews),frames=[...new Set(previews.map(v=>v.frame_index))].filter(Number.isInteger).sort((a,b)=>a-b);
    const frame=frames.includes(resultFrames.get(job.id))?resultFrames.get(job.id):frames[0];
    const pair=['baseline_rgb_frames','rgb_frames'].map(name=>previews.find(v=>v.array===name&&v.frame_index===frame));
    const compare=pair.every(v=>v&&v.display_range?.[0]===0&&v.display_range?.[1]===1&&v.display_transfer==='clamp_normalized_values_no_autoscale'&&/^\/api\/v1\/artifacts\/[A-Za-z0-9._/-]+$/.test(v.preview_url||''))?`<div class="form-field"><label for="ispv2-result-frame">동일 RAW · 비교 프레임</label><select id="ispv2-result-frame" data-ispv2-result="${esc(job.id)}">${frames.map(i=>`<option value="${i}" ${i===frame?'selected':''}>Frame index ${i}</option>`).join('')}</select></div><div class="preview-grid">${pair.map((v,i)=>`<figure><img class="preview-image" src="${esc(v.preview_url)}" alt="${i?`v${version} 처리 결과`:'동일 WB·CCM·tone의 새 단계 bypass 기준'} · frame ${frame}" loading="lazy"><figcaption>${i?`선택한 v${version} 처리`:'새 단계 bypass 기준'} · frame ${frame}</figcaption></figure>`).join('')}</div><p class="list-note">동일 표시 범위 [0,1] · 자동 밝기 정규화 없음. 톤 처리 미리보기와 signed 선형 단계 배열은 별도입니다.</p>`:'';
    const graph=list(p?.graph).length?list(p.graph):list(summary.graph),declared=[...new Set(graph)].filter(name=>Object.hasOwn(stages,name));
    const stageOrder=[...declared,...Object.keys(stages).filter(name=>!declared.includes(name))];
    const table=stageOrder.length?`<div class="table-wrap"><table><thead><tr><th>처리 단계</th><th>적용</th><th>저장 배열 · 유효 마스크</th></tr></thead><tbody>${stageOrder.map(name=>{const s=stages[name];return `<tr><td>${esc(name)}${declared.includes(name)?'':' · 처리 순서 미확인'}</td><td>${s.applied===true?'적용':s.applied===false?'bypass':'미기록'}</td><td>${esc(s.array)}<small>${esc(s.validity||'미기록')} · ${esc(s.unit||'')}</small></td></tr>`;}).join('')}</tbody></table></div>`:'';
    return `<section class="panel" id="ispv2-result"><div class="panel-header"><div><span class="eyebrow">FIXED RAW · STAGE EVIDENCE</span><h2>ISP v${version} 처리 근거</h2><p>${esc(job.name||job.id)} · ${esc(job.status)}</p></div></div><div class="panel-content">${job.status==='succeeded'?`${compare}${table}${detail('고정 단계 · 적용 및 bypass',{graph:p?.graph||summary.graph,stage_outputs:stages,profile:p})}${detail('보정의 불변 출처 · 적용 범위 · 불확실도',summary.calibration_snapshot||job.calibration_snapshot||summary.calibration||{calibration_id:p?.calibration_id,uncertainty:{kind:'unknown'}})}<div class="artifact-links">${links.map(link).join('')}</div>${detail('유효 마스크 · 비교 진단 · 처리 한계',{masks:summary.masks,diagnostics:summary.diagnostics,limitations:summary.limitations,uncertainty:summary.uncertainty,source_provenance:summary.source_provenance,raw_sha256:summary.raw_sha256,selector:job.source?.selector,normalization:summary.normalization,calibrated_input:summary.calibrated_input})}`:'<p class="list-note">실행이 완료되면 저장된 단계 배열과 보정 근거를 표시합니다.</p>'}<div class="callout neutral">같은 RAW의 후처리 결과입니다. 마스크는 처리·보정 경계이며 실제 센서 포화 truth를 뜻하지 않습니다. 필터용 분산 가정과 처리 후 총 불확실도는 다릅니다. 실물 색 정확도·제조사 ISP 성능은 미확립입니다.</div></div></section>`;
  }
  return {editor,calibrationEditor:()=>calibrationEditor(state().s),profileEditor:()=>profileEditor(state().s),result,input,change,click,submit};
};

'use strict';

// The host owns navigation; this module owns retained drafts and request scopes.
// It displays server evaluations and never infers physical quality or a winner.
window.CameraISPStudiesUI = function(ctx) {
  const {api,esc,render,toast}=ctx, list=x=>Array.isArray(x)?x:[], copy=x=>JSON.parse(JSON.stringify(x));
  const canonical=x=>JSON.stringify(x,(_k,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
  const json=x=>JSON.stringify(x,null,2), sourceKey=x=>x.source_kind+':'+x.source_id+(x.selector?':'+JSON.stringify([x.selector.schema,x.selector.mode,x.selector.readout_id||'',...(x.selector.readout_calibration_id?[x.selector.readout_calibration_id]:[])]):'');
  const s={catalog:null,sources:[],calibrations:[],models:[],references:[],studies:[],selections:[],source:'',calibration:'',baseline:'',templateAxes:[],spec:null,text:'',invalid:new Map(),epoch:0,loadEpoch:0,plan:null,planDraft:'',planning:false,loading:false,error:'',notice:'',pending:null,study:'',record:null,analysis:null,cancellation:null,analysisEpoch:0,viewEpoch:0,selection:null,candidate:'',rationale:'',dataset:'',frame:0,chartX:'',chartY:'',active:false,timer:null};
  const parameters={'raw_denoise.strength':'Denoise 강도','linear_sharpen.amount':'Sharpen 강도'};
  const metrics={temporal_residual_std_dn:'반복 프레임 잔차 RMS',mean_bias_dn:'평균 signed bias',mean_abs_bias_dn:'평균 절대 bias',mean_baseline_delta_dn:'기준 대비 평균 변화',mean_abs_baseline_delta_dn:'기준 대비 평균 절대 변화',edge_overshoot_dn:'밝은 쪽 overshoot',edge_undershoot_dn:'어두운 쪽 undershoot',texture_amplitude_dn:'Texture 진폭',texture_reference_amplitude_dn:'독립 기준 texture 진폭',texture_amplitude_ratio:'Texture 진폭 비율',texture_attenuation_fraction:'Texture 감쇠 비율'};
  const charge=x=>['camera_nonlinear_raw_selection_v1','camera_nonlinear_raw_selection_v2'].includes(x?.selector?.schema);
  const chargeStudy=()=>s.spec?.schema==='camera_isp_study_v2';
  const metricKeys=()=>Object.keys(metrics).map(k=>chargeStudy()?k.replace(/_dn$/,'_e'):k);
  const unit=k=>k.endsWith('_e')?'electron_equivalent':s.catalog?.metrics?.[k]?.unit||(['texture_amplitude_ratio','texture_attenuation_fraction'].includes(k)?'1':'DN_equivalent');
  const metricName=k=>metrics[k]||metrics[k.replace(/_e$/,'_dn')]||s.catalog?.metrics?.[k]?.label||k;
  const finite=x=>typeof x==='number'&&Number.isFinite(x), number=x=>finite(x)?Number(x.toPrecision(6)).toString():'미확인';
  const native=x=>x?.geometry?.kind==='native_sensor'&&list(x.raw_shape).length===3&&(charge(x)||x.raw_format?.hdr_streams===1);
  const currentSource=()=>s.sources.find(x=>sourceKey(x)===s.source);
  const locked=()=>Boolean(s.pending);
  const detail=(title,value)=>`<details class="data-detail"><summary>${esc(title)}</summary><pre>${esc(json(value))}</pre></details>`;
  const option=(value,label,chosen)=>`<option value="${esc(value)}" ${value===chosen?'selected':''}>${esc(label)}</option>`;
  const panel=(title,body,subtitle='')=>`<section class="panel"><div class="panel-header"><div><h2>${esc(title)}</h2>${subtitle?`<p>${esc(subtitle)}</p>`:''}</div></div><div class="panel-content">${body}</div></section>`;
  const button=(action,label,extra='',disabled=false)=>`<button type="button" class="button secondary" data-ispstudy-action="${action}" ${extra} ${disabled?'disabled':''}>${esc(label)}</button>`;
  function inputField(path,label,value,{type='text',min,max,step='any'}={}) {
    const id='ispstudy-'+path.replaceAll('.','-');if(s.invalid.has(path))value=s.invalid.get(path);
    return `<div class="form-field"><label for="${id}">${esc(label)}</label><input id="${id}" data-ispstudy-field="edit" data-path="${esc(path)}" type="${type}" value="${esc(value??'')}" ${type==='number'?`step="${step}" ${min===undefined?'':`min="${min}"`} ${max===undefined?'':`max="${max}"`}`:''}></div>`;
  }
  function selectField(path,label,values,value,extra='') {
    const id='ispstudy-'+path.replaceAll('.','-');
    return `<div class="form-field"><label for="${id}">${esc(label)}</label><select id="${id}" data-ispstudy-field="edit" data-path="${esc(path)}" ${extra}>${values.map(x=>option(x[0],x[1],value)).join('')}</select></div>`;
  }
  function setPath(obj,path,value) {
    const keys=path.split('.');if(keys.some(x=>['__proto__','constructor','prototype'].includes(x)))throw Error('허용하지 않는 입력 경로입니다.');
    let node=obj;for(const k of keys.slice(0,-1)){if(!node||!Object.hasOwn(node,k))throw Error('계획 형식에 없는 필드입니다.');node=node[k];}
    if(!node||!Object.hasOwn(node,keys.at(-1)))throw Error('계획 형식에 없는 필드입니다.');node[keys.at(-1)]=value;
  }
  function invalidate() {
    s.epoch++;s.plan=null;s.planDraft='';s.error='';s.notice='';
    // Remove the old submit control immediately without replacing a focused input.
    if(typeof document!=='undefined')document.querySelector('[data-ispstudy-plan]')?.remove();
  }
  const syncText=()=>{s.text=json(s.spec);};
  function specification() {
    if(s.invalid.size)throw Error('비어 있거나 잘못된 수치를 고쳐 주세요. 이전 값으로 제출하지 않았습니다.');
    if(!s.spec)throw Error('원본·보정·기준 ISP를 선택하고 계획 형식을 불러오세요.');
    const spec=copy(s.spec);
    for(const axis of list(spec.axes)) {
      if(typeof axis.values==='string') {
        if(!axis.values.trim())throw Error('변수 값은 0~1의 숫자를 쉼표로 구분해 입력하세요.');
        axis.values=axis.values.trim().split(/[\s,]+/).map(Number);
      }
      if(!list(axis.values).length||axis.values.some(v=>!finite(v)||v<0||v>1)||new Set(axis.values).size!==axis.values.length)throw Error('변수 값은 중복 없는 0~1의 유한한 숫자여야 합니다.');
    }
    if(!spec.datasets?.length)throw Error('검토할 RAW 데이터셋을 명시적으로 추가하세요.');
    for(const d of spec.datasets)if(!d.source_id||!d.acquisition_group_id.trim()||!d.stationarity?.description?.trim())throw Error('데이터셋의 원본, 취득 그룹, 정지 장면 가정의 근거를 입력하세요.');
    return spec;
  }
  async function activate() {
    s.active=true;const token=++s.loadEpoch;s.loading=true;s.error='';render();
    const requests=[['catalog','/isp-studies/catalog'],['sources','/replay-sources'],['calibrations','/isp-calibrations'],['models','/models'],['references','/isp-references'],['studies','/isp-studies'],['selections','/isp-selections']];
    const results=await Promise.allSettled(requests.map(async([name,path])=>({name,value:await api(path)})));
    if(token!==s.loadEpoch)return;
    const errors=[];for(const result of results)if(result.status==='fulfilled'){const {name,value}=result.value;s[name]=name==='catalog'?value:list(value);}else errors.push(result.reason?.message||String(result.reason));
    s.loading=false;if(errors.length)s.error='목록을 불러오지 못했습니다: '+errors.join(' · ');render();
    if(s.study)await loadAnalysis();
  }
  function deactivate(){s.active=false;clearTimeout(s.timer);s.timer=null;}
  function schedule(){clearTimeout(s.timer);if(s.active&&s.analysis?.status==='incomplete'&&ctx.isActive?.()!==false)s.timer=setTimeout(()=>{if(s.active&&ctx.isActive?.()!==false)loadAnalysis();},2000);}
  async function loadAnalysis() {
    const id=s.study,token=++s.analysisEpoch;if(!id)return;
    try {
      const [record,analysis]=await Promise.all([api('/isp-studies/'+encodeURIComponent(id)),api('/isp-studies/'+encodeURIComponent(id)+'/analysis')]);
      if(s.study!==id||token!==s.analysisEpoch)return;
      if(analysis.study_id!==id||analysis.plan_hash!==record.plan_hash)throw Error('Study와 분석의 고정 계획이 다릅니다. 결과를 표시하지 않았습니다.');
      s.record=record;s.analysis=analysis;s.cancellation=null;s.error='';
      if(!list(record.candidates).some(x=>x.candidate_id===s.candidate))s.candidate='';
      if(!list(record.datasets).some(x=>x.key===s.dataset))s.dataset=record.datasets?.[0]?.key||'';
      render();schedule();
    }catch(error){if(s.study===id&&token===s.analysisEpoch){s.record=null;s.analysis=null;s.error=error.message;clearTimeout(s.timer);render();}}
  }
  async function selectStudy(id) {s.study=id;s.record=null;s.analysis=null;s.cancellation=null;s.selection=null;s.candidate='';s.rationale='';s.frame=0;s.chartX='';s.chartY='';s.viewEpoch++;s.analysisEpoch++;clearTimeout(s.timer);render();await loadAnalysis();}
  function freshDataset(source,index) {
    const shape=list(source?.raw_shape),h=shape.at(-2)||0,w=shape.at(-1)||0;
    const declared=s.catalog?.evaluation_halo_native_pixels,halo=Number.isInteger(declared)&&declared>=0?declared:4;
    let n=index+1;while(list(s.spec?.datasets).some(d=>d.key==='dataset_'+n))n++;
    return {...(chargeStudy()?{selector:source?.selector||null}:{}),key:'dataset_'+n,source_kind:source?.source_kind||'run',source_id:source?.source_id||'',purpose:s.spec?.phase||'tuning',acquisition_group_id:'',stationarity:{kind:'declared_static',source_kind:'estimated',description:''},metric_spec:{...(chargeStudy()?{schema:'camera_isp_metric_spec_v2'}:{}),kind:'flat',stationary:true,roi:{row_start:halo,row_end:Math.max(halo,h-halo),column_start:halo,column_end:Math.max(halo,w-halo)},texture:null,edge:null},reference_id:null};
  }
  function datasetEditor(d,i) {
    const p='datasets.'+i,source=s.sources.find(x=>sourceKey(x)===sourceKey(d)),m=d.metric_spec||{},r=m.roi||{};
    const sourceOptions=[option('','RAW 선택',sourceKey(d)),...s.sources.filter(x=>native(x)&&charge(x)===chargeStudy()).map(x=>option(sourceKey(x),`${x.name||x.source_id} · ${list(x.raw_shape).join('×')}`,sourceKey(d)))].join('');
    return `<div class="case-form-row"><h3>데이터셋 ${i+1} · ${esc(d.purpose)}</h3><div class="form-row">${inputField(p+'.key','평가 데이터 이름',d.key)}<div class="form-field"><label for="ispstudy-source-${i}">고정 RAW 원본</label><select id="ispstudy-source-${i}" data-ispstudy-field="dataset-source" data-index="${i}">${sourceOptions}</select></div></div>${source?`<p class="list-note">RAW SHA-256 · <code>${esc(source.raw_sha256)}</code> · ${esc(source.provenance?.source_kind||source.source_kind)} · ${list(source.raw_shape)[0]}개 프레임</p>`:'<p class="callout neutral">지원되는 native 단일 stream RAW를 선택하세요.</p>'}<div class="form-row">${selectField(p+'.metric_spec.kind','평가 장면',[['flat','Flat · 반복 잡음 / bias'],['edge','Edge · overshoot / undershoot'],['texture','Texture · 주기 신호 진폭']],m.kind)}${inputField(p+'.acquisition_group_id','취득 그룹 ID · 독립성 중복 검사',d.acquisition_group_id)}</div><div class="form-row">${selectField(p+'.stationarity.source_kind','정지 장면 가정의 출처',['estimated','simulated','measured','vendor'].map(x=>[x,x]),d.stationarity?.source_kind)}${inputField(p+'.stationarity.description','장면·노출·자세가 고정된 근거',d.stationarity?.description)}</div><h4>원본 좌표의 고정 ROI · 끝 좌표 제외</h4><div class="form-row">${['row_start','row_end','column_start','column_end'].map((k,j)=>inputField(p+'.metric_spec.roi.'+k,['첫 행','끝 행','첫 열','끝 열'][j],r[k],{type:'number',min:0,step:1})).join('')}</div>${m.kind==='texture'?`<div class="form-row">${inputField(p+'.metric_spec.texture.period_px','알려진 texture 주기 · pixel',m.texture?.period_px,{type:'number',min:1})}${selectField(p+'.metric_spec.texture.direction','주기 신호 변화 방향',[['column','열 방향'],['row','행 방향']],m.texture?.direction)}</div>`:''}${m.kind==='edge'?`<div class="form-row">${selectField(p+'.metric_spec.edge.direction','Edge 변화 방향',[['column','열 방향'],['row','행 방향']],m.edge?.direction)}${inputField(p+'.metric_spec.edge.position_px','Edge 위치 · 원본 pixel',m.edge?.position_px,{type:'number',min:0})}${inputField(p+'.metric_spec.edge.half_width_px','고정 band 반폭 · pixel',m.edge?.half_width_px,{type:'number',min:1,step:1})}</div>`:''}${selectField(p+'.reference_id','독립 기준 영상 · 없으면 bias/진폭 비율 미확인',[['','기준 없음'],...s.references.filter(x=>(x.schema==='camera_isp_reference_v2')===chargeStudy()).map(x=>[x.id,`${x.name||x.id} · ${x.source_kind} · ${x.acquisition_group_id}`])],d.reference_id||'')}<p class="list-note">평가 ROI와 필터 halo는 계획 전에 고정합니다. 그룹 ID나 출처 label만으로 물리적 독립성이 입증되지는 않습니다.</p>${button('remove-dataset','데이터셋 삭제',`data-index="${i}"`)}</div>`;
  }
  function reductions(kind) {
    const constraints=kind==='constraints';return list(s.spec?.[kind]).map((o,i)=>{
      const p=kind+'.'+i,keys=list(s.spec.datasets).map(d=>d.key);
      return `<div class="case-form-row"><div class="form-row">${selectField(p+'.metric',constraints?'제약 지표':'비교 축',metricKeys().map(k=>[k,metricName(k)+' · '+unit(k)]),o.metric)}${selectField(p+'.reducer','명시한 데이터의 집계',[['max','최댓값'],['min','최솟값'],['max_abs','절댓값의 최댓값']],o.reducer)}${constraints?selectField(p+'.operator','경계',[['<=','이하'],['>=','이상']],o.operator):selectField(p+'.direction','목표 방향',[['minimize','작을수록'],['maximize','클수록']],o.direction)}</div><fieldset class="case-requirement-links"><legend>집계에 포함할 데이터셋 · 명시적 선택</legend>${keys.map(k=>`<label class="check-option"><input type="checkbox" data-ispstudy-field="reduction-dataset" data-kind="${kind}" data-index="${i}" data-key="${esc(k)}" ${list(o.dataset_keys).includes(k)?'checked':''}>${esc(k)}</label>`).join('')}</fieldset>${constraints?inputField(p+'.value','허용 경계 · '+(o.unit||unit(o.metric)),o.value,{type:'number'}):''}${button('remove-'+kind,'삭제',`data-index="${i}"`)}</div>`;
    }).join('');
  }
  function editor() {
    const options=(rows,chosen,label)=>option('',label,chosen)+rows.map(x=>option(x.id,`${x.name||x.id} · ${x.revision||''}`,chosen)).join('');
    const start=`<fieldset class="case-fields" ${locked()?'disabled':''}><div class="form-row"><div class="form-field"><label for="ispstudy-source">형식을 만들 RAW 원본</label><select id="ispstudy-source" data-ispstudy-field="source">${option('','원본 선택',s.source)}${s.sources.filter(native).map(x=>option(sourceKey(x),`${x.name||x.source_id} · ${list(x.raw_shape).join('×')}`,s.source)).join('')}</select></div><div class="form-field"><label for="ispstudy-calibration">공통 불변 보정</label><select id="ispstudy-calibration" data-ispstudy-field="calibration">${options(s.calibrations.filter(x=>(x.schema==='camera_charge_isp_calibration_v1')===charge(currentSource())),s.calibration,charge(currentSource())?'LSC 보정 없음':'보정 선택')}</select></div><div class="form-field"><label for="ispstudy-baseline">기준 ISP v${charge(currentSource())?3:2} 버전</label><select id="ispstudy-baseline" data-ispstudy-field="baseline">${options(s.models.filter(x=>x.kind==='isp'&&x.payload?.schema===(charge(currentSource())?'camera_isp_profile_v3':'camera_isp_profile_v2')),s.baseline,'ISP 선택')}</select></div></div>${button('template',s.spec?'새 tuning 형식으로 초안 교체':'계획 형식 불러오기','',!s.source||(!charge(currentSource())&&!s.calibration)||!s.baseline||s.loading)}</fieldset>`;
    if(!s.spec)return panel('1. 같은 RAW에서 후보를 비교할 조건',start+'<p class="list-note">RAW 재처리에서 원본에 맞는 ISP를 등록하세요. 비선형 RAW는 전자 수 기준 보정과 명시적으로 선택한 DCG 경로를 사용합니다. 잡음 평가는 고정 조건의 4~16 반복 촬영이 필요합니다.</p>');
    const d=s.spec;
    return panel('1. 새 ISP 후보 탐색 계획',`<details class="data-detail"><summary>새 원본으로 계획 형식 다시 시작</summary>${start}</details><form data-ispstudy-form="plan"><fieldset class="case-fields" ${locked()?'disabled':''}><p class="case-context">${d.phase==='independent_validation'?'독립 검증 · 저장된 선택과 기준 후보만 실행합니다. 새 RAW와 취득 그룹을 지정하세요.':'Tuning · 동일 RAW마다 기준 후보를 포함합니다. 최대 후보×데이터셋 24개 작업입니다.'}<br>보정 ${esc(d.calibration_id)} · 기준 ISP ${esc(d.baseline_isp_model_id)}${d.selection_id?'<br>선택 기록 '+esc(d.selection_id):''}</p>${inputField('name','실험 이름',d.name)}<div class="form-field"><label for="ispstudy-question">해결할 설계 질문</label><textarea id="ispstudy-question" data-ispstudy-field="edit" data-path="question">${esc(d.question)}</textarea></div><h3>평가 데이터와 독립 기준</h3>${list(d.datasets).map(datasetEditor).join('')}${button('add-dataset','RAW 데이터셋 추가','',d.datasets.length>=12)}<div class="inline-spacer"></div><h3>후보 변수 · 나머지 단계는 기준 ISP 그대로 고정</h3>${d.phase==='tuning'?list(d.axes).map((a,i)=>`<div class="form-row">${selectField('axes.'+i+'.parameter','변수',s.templateAxes.map(k=>[k,parameters[k]||k]),a.parameter)}${inputField('axes.'+i+'.values','값 · 0~1, 쉼표로 구분',Array.isArray(a.values)?a.values.join(', '):a.values)}${button('remove-axis','변수 삭제',`data-index="${i}"`)}</div>`).join('')+button('add-axis','허용 변수 추가','',!s.templateAxes.some(k=>!d.axes.some(a=>a.parameter===k))):'<p>선택한 프로파일을 고정했으며 탐색 축은 없습니다.</p>'}<p class="list-note">Denoise나 sharpen이 bypass인 기준에는 해당 축을 적용할 수 없습니다. Texture 비율을 무조건 키우지 말고 1 근처의 양쪽 허용 경계를 지정하세요.</p><div class="inline-spacer"></div><h3>비교 목적과 허용 제약</h3>${reductions('objectives')}${button('add-objectives','비교 목적 추가','',d.objectives.length>=4)}${reductions('constraints')}${button('add-constraints','허용 제약 추가','',d.constraints.length>=12)}<details class="data-detail"><summary>전체 계획 JSON · 고급 입력</summary><label for="ispstudy-json">원본·ROI·집계·독립성 선언</label><textarea id="ispstudy-json" data-ispstudy-field="json" spellcheck="false">${esc(s.text)}</textarea></details><div class="inline-spacer"></div><button class="button primary full" type="submit" ${s.planning?'disabled':''}>${s.planning?'고정 입력과 자원 검사 중…':'전체 계획 검사'}</button></fieldset></form><p class="list-note">초안은 화면 이동·목록 새로고침·요청 오류에도 유지됩니다. 탭을 닫으면 사라집니다. 실물 검증과 통계적 우월성은 아직 확립하지 않습니다.</p>`);
  }
  function planPanel() {
    if(!s.plan)return '';const p=s.plan;
    return `<section class="panel" data-ispstudy-plan="${esc(p.plan_hash)}"><div class="panel-header"><h2>2. 검사된 계획 · 제출 전 확인</h2></div><div class="panel-content"><p>${list(p.candidates).length}개 후보 × ${list(p.datasets).length}개 데이터 · ${esc(p.job_count)}개 작업</p><p>예상 저장 ${number(p.resources?.estimated_output_bytes/1048576)} MiB · 작업당 최대 ${number(p.resources?.estimated_working_bytes_max/1048576)} MiB</p><div class="table-wrap"><table><thead><tr><th>후보</th><th>고정 변수</th><th>프로파일 SHA-256</th></tr></thead><tbody>${list(p.candidates).map(c=>`<tr><td>${esc(c.candidate_id)}${c.is_baseline?' · 기준':''}</td><td>${Object.entries(c.coordinates||{}).map(([k,v])=>esc(parameters[k]||k)+' '+(finite(v)?number(v):esc(v))).join('<br>')}</td><td class="case-hash">${esc(c.profile_sha256)}</td></tr>`).join('')}</tbody></table></div>${detail('원본 SHA · 평가 마스크 · 자원과 전체 계획',p)}${button('submit','검사한 계획 제출','',locked())}<p class="list-note">입력을 하나라도 바꾸면 이 계획을 다시 검사해야 합니다.</p></div></section>`;
  }
  function metricValue(value) {const v=value&&typeof value==='object'?value.value:value;return finite(v)&&(!value?.status||value.status==='ok')?number(v):'미확인'+(value?.reason?' · '+esc(value.reason):'');}
  const safeURL=url=>typeof url==='string'&&/^\/api\/v1\/artifacts\/[A-Za-z0-9._/-]+$/.test(url);
  function previews() {
    const a=s.analysis,r=s.record;if(!a||!r)return '';
    const row=list(a.rows).find(x=>x.dataset_key===s.dataset&&x.candidate_id===s.candidate&&x.status==='succeeded');
    const descriptor=list(r.datasets).find(x=>x.key===s.dataset);
    const summary=row?.summary||row?.preview||{},previews=list(summary.stage_previews);
    const frames=[...new Set(previews.map(x=>x.frame_index))].filter(Number.isInteger).sort((x,y)=>x-y),frame=frames.includes(s.frame)?s.frame:frames[0];
    const pair=['baseline_rgb_frames','rgb_frames'].map(array=>previews.find(x=>x.array===array&&x.frame_index===frame));
    const valid=pair.every(x=>x&&safeURL(x.preview_url)&&x.display_range?.[0]===0&&x.display_range?.[1]===1&&x.display_transfer==='clamp_normalized_values_no_autoscale')&&summary.raw_sha256===descriptor?.raw_sha256;
    return `<h3>동일 원본·동일 프레임의 처리 비교</h3><div class="form-row"><div class="form-field"><label for="ispstudy-preview-dataset">비교할 RAW 데이터</label><select id="ispstudy-preview-dataset" data-ispstudy-field="preview-dataset">${list(r.datasets).map(x=>option(x.key,x.key,s.dataset)).join('')}</select></div><div class="form-field"><label for="ispstudy-frame">고정 프레임</label><select id="ispstudy-frame" data-ispstudy-field="frame">${frames.map(x=>option(String(x),'Frame '+x,String(frame))).join('')}</select></div></div><p class="list-note">후보 ${esc(s.candidate||'미선택')} · RAW SHA ${esc(descriptor?.raw_sha256||'미확인')}</p>${valid?`<div class="preview-grid">${pair.map((x,i)=>`<figure><img class="preview-image" src="${esc(x.preview_url)}" alt="${i?'선택한 후보':'새 단계 bypass 기준'} · ${esc(s.dataset)} · frame ${frame}"><figcaption>${i?'선택한 후보':'새 단계 bypass 기준'} · frame ${frame} · 표시 범위 [0,1]</figcaption></figure>`).join('')}</div><p class="list-note">자동 밝기 정규화 없음. 이 미리보기 기준은 모든 새 단계를 끈 이미지이며, Study의 등록된 기준 후보와 다를 수 있습니다.</p>`:'<p class="list-note">완료된 후보를 선택하면 원본 SHA와 표시 범위가 일치하는 프레임 쌍만 표시합니다.</p>'}${row?`<div class="artifact-links">${list(row.artifacts).filter(x=>safeURL(x.url)).map(x=>`<a class="button secondary small" href="${esc(x.url)}" download title="SHA-256 ${esc(x.sha256)}">${esc(x.name)} ↓</a>`).join('')}</div>${detail('선택한 원본의 단계 배열 · 마스크 · 지표 근거',row.metric_result||row)}`:''}`;
  }
  function results() {
    const a=s.analysis,r=s.record;
    if(s.cancellation?.study_id===s.study)return panel('3. 취소 상태 · 계산 근거 확인 불가',`<p class="callout neutral">소유한 작업의 취소 상태는 기록했습니다. 원본 또는 결과 근거를 확인할 수 없어 이전 지표·미리보기를 표시하지 않습니다. 아래 상태는 계산 성공이나 근거 검증을 뜻하지 않습니다.</p><p>${esc(s.cancellation.evidence_error)}</p><ul class="case-list">${list(s.cancellation.rows).map(row=>`<li>${esc(row.dataset_key)} · ${esc(row.status)}<small> ${esc(row.job_id)}</small></li>`).join('')}</ul>${button('refresh-analysis','원본 복구 후 근거 다시 확인')}`);
    if(!a||!r)return panel('3. 후보별 잡음·bias·texture·halo',s.study?(s.error?`<p>현재 근거를 확인하지 못해 이전 결과를 표시하지 않습니다. 작성 중인 계획은 유지했습니다.</p>${button('refresh-analysis','근거 다시 확인')}`:'<p>선택한 Study의 원본과 분석을 확인 중입니다.</p>'):'<p>계획을 제출하면 데이터별 실제 처리 결과, 미확인 사유, 샘플 내 상충 관계가 표시됩니다.</p>');
    const rows=list(a.rows),keys=[...new Set(rows.flatMap(x=>Object.keys(x.metrics||{})))];
    const progress=Object.entries(a.coverage||{}).filter(([_k,v])=>finite(v)).map(([k,v])=>esc(k)+' '+v).join(' · ');
    return panel('3. 같은 RAW의 후보별 근거',`<div data-ispstudy-analysis="${esc(a.study_id)}"><p><strong>${esc(r.specification?.name||r.name||r.id)}</strong> · ${esc(a.status)} · ${esc(a.phase)}</p><p>${esc(r.specification?.question||'')}</p><p role="status">${progress}</p><div class="case-actions">${button('refresh-analysis','진행 상태 새로고침')}${button('cancel','남은 작업 취소','',locked()||a.status==='complete')}${button('use-study','이 Study를 사례 근거로 선택')}<a class="button secondary" href="/api/v1/isp-studies/${encodeURIComponent(a.study_id)}/bundle" download>고정 입력·결과 근거 ZIP ↓</a></div>${aggregateView(a)}<div class="table-wrap"><table><thead><tr><th>후보 / 데이터</th><th>상태</th>${keys.map(k=>`<th>${esc(metricName(k))}<small>${esc(unit(k))}</small></th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.candidate_id)}<small>${esc(row.dataset_key)}</small></td><td>${esc(row.status)}${row.error?'<small>'+esc(typeof row.error==='string'?row.error:json(row.error))+'</small>':''}</td>${keys.map(k=>`<td>${metricValue(row.metrics?.[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><div class="inline-spacer"></div>${s.selections.some(x=>x.study_id===s.study)?`<div class="form-field"><label for="ispstudy-selection">보존된 설계 선택 · 검증 계획 이어서 작성</label><select id="ispstudy-selection" data-ispstudy-field="saved-selection">${option('','저장된 선택 보기',s.selection?.id)}${s.selections.filter(x=>x.study_id===s.study).map(x=>option(x.id,`${x.candidate_id} · ${x.rationale}`,s.selection?.id)).join('')}</select></div>`:''}<div class="form-field"><label for="ispstudy-candidate">직접 비교하고 선택할 후보</label><select id="ispstudy-candidate" data-ispstudy-field="candidate">${option('','후보 선택',s.candidate)}${list(r.candidates).map(c=>option(c.candidate_id,c.candidate_id+(c.is_baseline?' · 기준':''),s.candidate)).join('')}</select></div>${previews()}<form data-ispstudy-form="selection"><fieldset class="case-fields" ${locked()?'disabled':''}><div class="form-field"><label for="ispstudy-rationale">이 후보를 다음 검증으로 넘기는 설계 이유</label><textarea id="ispstudy-rationale" data-ispstudy-field="rationale">${esc(s.rationale)}</textarea></div><button class="button primary" type="submit" ${!s.candidate||!s.rationale.trim()||a.status!=='complete'||a.phase!=='tuning'?'disabled':''}>후보와 선택 이유를 불변 기록으로 저장</button></fieldset></form>${s.selection?`<p class="callout neutral">선택 ${esc(s.selection.id)} · ${esc(s.selection.candidate_id)} · ${esc(s.selection.rationale)}</p>${button('validation','다른 RAW로 독립 검증 계획 작성','',locked())}`:''}<div class="inline-spacer"></div>${detail('계획·집계·미확인 사유와 원본 분석',a)}<p class="callout neutral">분산은 반복 프레임에서 추정한 기술 통계입니다. 단일 영상의 공간 분산·물리적 SNR과 다릅니다. 샘플 내 Pareto는 전역 최적해, 자동 우승 후보, 출시 승인 또는 실물 검증을 뜻하지 않습니다.</p></div>`);
  }
  function reductionName(a,id) {
    const defs=a.reduction_definitions||a.reductions,d=Array.isArray(defs)?defs.find(x=>x.id===id):defs?.[id];
    return d?`${metricName(d.metric)} · ${d.reducer}(${list(d.dataset_keys||d.dataset_ids).join(', ')})`:metricName(id);
  }
  function aggregateView(a) {
    const g=a.aggregate;if(!g)return '<p>집계와 Pareto 판정은 아직 없습니다.</p>';
    const validation=a.phase==='independent_validation';
    const objectives=list(g.objectives),keys=objectives.map(x=>x.metric),rows=list(g.rows);
    const feasibility={feasible:'선언한 수치 제약 충족',infeasible:'제약 미충족',unknown:'제약 미확인'};
    const pareto={non_dominated:'샘플 내 비지배',dominated:'샘플 내 지배됨',excluded:'비교 제외',not_ranked_validation:'독립 검증 · 순위 없음'};
    const checks={satisfied:'충족',violated:'위반',unknown:'미확인'};
    const x=keys.includes(s.chartX)?s.chartX:keys[0],y=keys.includes(s.chartY)?s.chartY:keys[1];
    const values=(row,key)=>row.metrics?.[key]?.status==='ok'&&finite(row.metrics[key].value)?row.metrics[key].value:null;
    const points=rows.filter(row=>row.status==='succeeded'&&values(row,x)!==null&&values(row,y)!==null);
    let chart='';
    if(x&&y&&x!==y&&points.length) {
      const range=key=>{const v=points.map(row=>values(row,key)),low=Math.min(...v),high=Math.max(...v),pad=(high-low||Math.max(1,Math.abs(low)))*.12;return [low-pad,high+pad];};
      const [x0,x1]=range(x),[y0,y1]=range(y),px=v=>85+(v-x0)/(x1-x0)*445,py=v=>265-(v-y0)/(y1-y0)*190;
      chart=`<div class="study-chart"><svg viewBox="0 0 620 360" role="img" aria-label="고정 데이터의 두 목적값 · 샘플 내 비교" style="width:100%;font-size:11px;fill:#536057"><rect x="0" y="0" width="620" height="360" fill="white"/>${[0,.25,.5,.75,1].map(t=>{const vx=x0+t*(x1-x0),vy=y0+t*(y1-y0);return `<path d="M${px(vx)} 75V265 M85 ${py(vy)}H530" stroke="#e4e8e5"/><text x="${px(vx)}" y="285" text-anchor="middle">${number(vx)}</text><text x="75" y="${py(vy)+4}" text-anchor="end">${number(vy)}</text>`;}).join('')}${points.map((row,i)=>`<circle cx="${px(values(row,x))}" cy="${py(values(row,y))}" r="6" fill="${validation?'#657586':row.feasibility!=='feasible'?'#bc7242':row.pareto_status==='non_dominated'?'#2b6651':'#a5b2aa'}"><title>${esc(row.candidate_id)} · ${number(values(row,x))}, ${number(values(row,y))}</title></circle><text x="${px(values(row,x))+9}" y="${py(values(row,y))+(i%2?-10:16)}">${"C"+(rows.indexOf(row)+1)}</text>`).join('')}<text x="85" y="27">Y: ${esc(reductionName(a,y))} · ${esc(g.metric_units?.[y]||'')}</text><text x="85" y="325">X: ${esc(reductionName(a,x))} · ${esc(g.metric_units?.[x]||'')}</text></svg></div>`;
    }else chart='<p class="list-note">서로 다른 두 목적에 유효한 값이 있는 완료 후보만 산점도에 표시합니다.</p>';
    return `<h3>고정 데이터 집계 · ${validation?'고정 기준·선택 후보 검증 · 순위 없음':g.pareto_provisional?'잠정 Pareto · 미확인/미완료 포함':'평가한 샘플 내 Pareto'}</h3><div class="form-row">${['X','Y'].map((axis,i)=>`<div class="form-field"><label for="ispstudy-chart-${axis}">그림 ${axis}축</label><select id="ispstudy-chart-${axis}" data-ispstudy-field="chart${axis}">${keys.map(k=>option(k,reductionName(a,k),i?y:x)).join('')}</select></div>`).join('')}</div>${chart}<p class="list-note">${validation?'독립 검증에서는 선택한 후보와 고정 기준의 결과만 비교합니다. 이 데이터로 후보를 다시 순위화하지 않습니다.':'녹색: 서버가 판정한 샘플 내 비지배 · 회색: 지배됨 · 주황색: 제약 미충족/미확인. 목적이 3개 이상이면 그림은 2축 투영이며 Pareto 판정은 전체 목적 기준입니다.'} C번호는 아래 후보표의 순번이며 원본 ID는 점의 설명과 전체 분석에 보존됩니다. 그래프에 없는 후보의 미확인 이유도 아래에 보존합니다.</p><div class="table-wrap"><table><thead><tr><th>후보</th>${objectives.map(o=>`<th>${esc(reductionName(a,o.metric))}<small>${o.direction==='minimize'?'작을수록':'클수록'} · ${esc(g.metric_units?.[o.metric]||'')}</small></th>`).join('')}<th>제약과 미확인</th><th>샘플 내 판정</th></tr></thead><tbody>${rows.map((row,i)=>`<tr><td title="${esc(row.candidate_id)}">C${i+1}<small>${esc(row.status)}</small></td>${keys.map(k=>`<td>${metricValue(row.metrics?.[k])}</td>`).join('')}<td>${list(row.constraints).map(c=>`${esc(reductionName(a,c.metric))} ${esc(c.operator)} ${number(c.value)} ${esc(c.unit)}<small>실제 ${number(c.actual)} · ${esc(checks[c.status]||c.status)}</small>`).join('<br>')||'별도 수치 제약 없음'}${row.exclusion_reason?'<small>'+esc(row.exclusion_reason)+'</small>':''}</td><td>${esc(feasibility[row.feasibility]||row.feasibility)}<small>${esc(pareto[row.pareto_status]||row.pareto_status)}</small></td></tr>`).join('')}</tbody></table></div>`;
  }
  function view() {
    const history=s.studies.length?s.studies:list(ctx.records?.('isp_studies'));
    return `<div id="ispstudy-workspace" class="stack">${s.error?`<p class="callout error" role="alert">${esc(s.error)}</p>`:''}${s.notice?`<p class="callout neutral" role="status">${esc(s.notice)}</p>`:''}${s.pending?.uncertain?`<div class="callout error">응답이 불명확하여 새 제출을 막았습니다. 저장된 동일 요청만 재확인합니다.${button('retry','같은 요청 다시 확인','',s.pending.inflight)}</div>`:''}<div class="case-toolbar"><div class="form-field"><label for="ispstudy-saved">저장된 ISP Study</label><select id="ispstudy-saved" data-ispstudy-field="study">${option('','저장 결과 선택',s.study)}${history.map(x=>option(x.id,x.name||x.specification?.name||x.id,s.study)).join('')}</select></div>${button('load',s.loading?'목록 확인 중…':'원본·보정·기록 새로고침','',s.loading)}</div>${editor()}${planPanel()}${results()}</div>`;
  }
  function input(target) {
    const field=target.dataset?.ispstudyField;if(!field)return false;
    if(field==='rationale'){if(!locked()){s.rationale=target.value;render();}return true;}
    if(!['edit','json'].includes(field))return true;if(locked())return true;
    invalidate();
    try {
      if(field==='json') {
        s.text=target.value;if(new TextEncoder().encode(s.text).byteLength>1048576)throw Error('계획 JSON은 1 MiB 이하여야 합니다.');
        const value=JSON.parse(s.text,(_k,v)=>{if(typeof v==='number'&&!Number.isFinite(v))throw Error('유한한 숫자만 입력하세요.');return v;});if(!value||Array.isArray(value)||!['camera_isp_study_v1','camera_isp_study_v2'].includes(value.schema)||['axes','datasets','objectives','constraints'].some(k=>!Array.isArray(value[k])))throw Error('배열 필드가 포함된 ISP Study v1/v2 객체를 입력하세요.');
        if(value.datasets.some(d=>!d||typeof d.key!=='string'||!d.stationarity||!d.metric_spec?.roi)||[...value.axes,...value.objectives,...value.constraints].some(x=>!x||typeof x!=='object'))throw Error('데이터·ROI·변수·집계 객체의 필수 형식을 유지하세요.');
        s.spec=value;s.invalid.clear();
      }else {
        const path=target.dataset.path;let value=target.type==='number'?Number(target.value):target.value;
        if(target.type==='number'&&(!target.value.trim()||!finite(value))){s.invalid.set(path,target.value);throw Error('유한한 수치를 입력하세요.');}
        s.invalid.delete(path);if(path.endsWith('.reference_id'))value=value||null;
        const renamed=path.match(/^datasets\.(\d+)\.key$/),oldKey=renamed?s.spec.datasets[Number(renamed[1])].key:null;
        setPath(s.spec,path,value);
        if(renamed)for(const reduction of [...s.spec.objectives,...s.spec.constraints])reduction.dataset_keys=list(reduction.dataset_keys).map(k=>k===oldKey?value:k);
        syncText();
      }
    }catch(error){s.error=error.message;if(field==='json')s.invalid.set('json',s.text);}return true;
  }
  async function change(target) {
    const field=target.dataset?.ispstudyField;if(!field)return false;
    if(field==='study'){await selectStudy(target.value);return true;}
    if(field==='candidate'){s.candidate=target.value;s.selection=null;s.rationale='';s.frame=0;s.viewEpoch++;render();return true;}
    if(field==='saved-selection') {
      const study=s.study,epoch=++s.viewEpoch,id=target.value;s.selection=null;
      if(id)try {const record=await api('/isp-selections/'+encodeURIComponent(id));if(study===s.study&&epoch===s.viewEpoch){if(record.study_id!==study)throw Error('선택 기록과 현재 Study가 다릅니다.');s.selection=record;s.candidate=record.candidate_id;s.rationale=record.rationale;s.frame=0;}}
      catch(error){if(study===s.study&&epoch===s.viewEpoch)s.error=error.message;}render();return true;
    }
    if(field==='preview-dataset'){s.dataset=target.value;s.frame=0;render();return true;}
    if(field==='frame'){s.frame=Math.max(0,Number(target.value)||0);render();return true;}
    if(field==='chartX'||field==='chartY'){s[field]=target.value;render();return true;}
    if(field==='rationale'){render();return true;}
    if(locked())return true;
    if(['source','calibration','baseline'].includes(field)){invalidate();s[field]=target.value;render();return true;}
    if(field==='dataset-source') {
      const i=Number(target.dataset.index),source=s.sources.find(x=>sourceKey(x)===target.value);invalidate();
      if(s.spec?.datasets?.[i]){const old=s.spec.datasets[i],next=freshDataset(source,i);s.spec.datasets[i]={...next,key:old.key,metric_spec:{...old.metric_spec,roi:next.metric_spec.roi}};syncText();}render();return true;
    }
    if(field==='reduction-dataset') {
      const item=s.spec?.[target.dataset.kind]?.[Number(target.dataset.index)];if(item){invalidate();item.dataset_keys=list(item.dataset_keys).filter(x=>x!==target.dataset.key);if(target.checked)item.dataset_keys.push(target.dataset.key);syncText();}render();return true;
    }
    if(field==='edit') {
      // Select controls do not necessarily emit an input event in every host.
      input(target);
      const path=target.dataset.path;if(path?.endsWith('.metric_spec.kind')){
        const d=s.spec.datasets[Number(path.split('.')[1])],m=d.metric_spec;
        m.texture=m.kind==='texture'?{period_px:8,direction:'column'}:null;m.edge=m.kind==='edge'?{direction:'column',position_px:Math.floor((m.roi.column_start+m.roi.column_end)/2),half_width_px:4}:null;syncText();
      }else if(path?.startsWith('constraints.')&&path.endsWith('.metric')){const c=s.spec.constraints[Number(path.split('.')[1])];c.unit=unit(c.metric);syncText();}
    }
    render();return true;
  }
  async function mutate(pending,retry=false) {
    if(s.pending?.inflight)return;if(s.pending&&s.pending!==pending)return;
    s.pending=pending;pending.inflight=true;s.error='';render();
    try {
      const result=await api(pending.path,copy(pending.body)),identity=pending.kind==='cancel'?result?.study_id:result?.id;
      if(!identity||(pending.kind==='cancel'&&identity!==pending.study))throw Error('저장된 기록의 ID를 확인하지 못했습니다.');
      s.pending=null;s.notice=(pending.kind==='cancel'?'취소 후 상태를 확인했습니다 · ':'불변 기록을 저장했습니다 · ')+identity;
      if(pending.kind==='submit') {
        if(!s.studies.some(x=>x.id===result.id))s.studies.unshift(result);
        s.plan=null;s.planDraft='';if(s.viewEpoch===pending.viewEpoch)await selectStudy(result.id);
      }else if(pending.kind==='selection') {
        if(!s.selections.some(x=>x.id===result.id))s.selections.unshift(result);
        if(s.study===pending.study&&s.candidate===pending.body.candidate_id&&s.viewEpoch===pending.viewEpoch)s.selection=result;
      }else if(pending.kind==='cancel') {
        if(s.study===pending.study) {
          if(result.status==='cancellation_recorded_evidence_unavailable') {
            s.record=null;s.analysis=null;s.cancellation=result;s.error=result.evidence_error||'취소 상태는 기록했지만 계산 근거를 확인할 수 없습니다.';
            s.notice='소유 작업의 취소 상태를 기록했습니다. 계산 근거는 확인 불가입니다.';s.analysisEpoch++;clearTimeout(s.timer);
          }else await loadAnalysis();
        }
      }
      try{await ctx.refresh?.({renderPage:false});}catch(error){s.error='저장은 완료됐지만 목록 새로고침에 실패했습니다: '+error.message;}
      toast?.(s.notice);
    }catch(error) {
      if(!retry&&!pending.uncertain&&error.status>=400&&error.status<500&&![408,429].includes(error.status))s.pending=null;
      else {pending.uncertain=true;pending.inflight=false;}
      s.error=error.message;
    }finally{if(s.pending)s.pending.inflight=false;render();}
  }
  async function submit(form) {
    const kind=form.dataset?.ispstudyForm;if(!kind)return false;if(locked())return true;
    try {
      if(kind==='plan') {
        if(s.planning)return true;const spec=specification(),before=canonical(spec),epoch=s.epoch;s.planning=true;s.error='';render();
        try {const plan=await api('/isp-studies/plan',spec);if(epoch!==s.epoch||canonical(specification())!==before)return true;if(!plan?.plan_hash||!plan.specification)throw Error('검사된 계획 해시와 명세가 응답에 없습니다.');s.plan=plan;s.planDraft=before;}
        catch(error){if(epoch===s.epoch)throw error;}
        finally{s.planning=false;render();}
      }else if(kind==='selection') {
        if(!s.analysis||!s.candidate||!s.rationale.trim()||s.analysis.status!=='complete'||s.analysis.phase!=='tuning')throw Error('완료된 tuning의 후보와 선택 이유가 필요합니다.');
        await mutate({kind,study:s.study,viewEpoch:s.viewEpoch,path:'/isp-studies/'+encodeURIComponent(s.study)+'/selection',body:{candidate_id:s.candidate,rationale:s.rationale.trim(),analysis_sha256:s.analysis.analysis_sha256}});
      }
    }catch(error){s.error=error.message;render();}return true;
  }
  async function click(target) {
    const action=target.dataset?.ispstudyAction;if(!action)return false;
    if(action==='load'){await activate();return true;}
    if(action==='refresh-analysis'){await loadAnalysis();return true;}
    if(action==='use-study'){if(s.study)ctx.onStudy?.(s.study);return true;}
    if(action==='retry'){if(s.pending&&!s.pending.inflight)await mutate(s.pending,true);return true;}
    if(locked())return true;
    try {
      if(action==='template') {
        const source=currentSource();if(!native(source)||(!charge(source)&&!s.calibration)||!s.baseline)throw Error('native RAW와 해당 ISP 버전·필요한 보정을 선택하세요.');
        invalidate();const epoch=s.epoch,identity=[s.source,s.calibration,s.baseline].join('|');let spec;
        try {spec=await api('/templates/isp-study?source_kind='+encodeURIComponent(source.source_kind)+'&source_id='+encodeURIComponent(source.source_id)+(s.calibration?'&calibration_id='+encodeURIComponent(s.calibration):'')+'&baseline_isp_model_id='+encodeURIComponent(s.baseline)+(charge(source)?'&version=2&selector='+encodeURIComponent(JSON.stringify(source.selector)):''));}
        catch(error){if(epoch===s.epoch)throw error;return true;}
        if(epoch!==s.epoch||identity!==[s.source,s.calibration,s.baseline].join('|'))return true;
        invalidate();s.spec=spec;s.templateAxes=list(spec.axes).map(a=>a.parameter);s.invalid.clear();syncText();
      }else if(action==='submit') {
        if(!s.plan||s.planDraft!==canonical(specification()))throw Error('입력이 변경되었습니다. 계획을 다시 검사하세요.');
        await mutate({kind:'submit',viewEpoch:s.viewEpoch,path:'/isp-studies',body:{specification:copy(s.plan.specification),plan_hash:s.plan.plan_hash}});
      }else if(action==='cancel') {
        if(s.study)await mutate({kind:'cancel',study:s.study,path:'/isp-studies/'+encodeURIComponent(s.study)+'/cancel',body:{}});
      }else if(action==='validation') {
        if(!s.selection||s.selection.study_id!==s.study)throw Error('저장된 후보 선택이 필요합니다.');
        const selection=s.selection,base=copy(s.record.specification);invalidate();
        s.spec={...base,name:base.name+' · 독립 검증',phase:'independent_validation',selection_id:selection.id,parent_study_id:s.study,attempt:1,axes:[],datasets:[],objectives:base.objectives.map(x=>({...x,dataset_keys:[]})),constraints:base.constraints.map(x=>({...x,dataset_keys:[]}))};
        s.invalid.clear();syncText();s.notice='새 검증용 RAW를 추가하고 목적·제약의 데이터셋 집계를 다시 지정하세요. Tuning 입력은 선택 기록에 보존했습니다.';ctx.onValidationStudy?.(selection);
      }else if(s.spec) {
        invalidate();const i=Number(target.dataset.index);
        if(action==='add-dataset'){if(s.spec.datasets.length<12)s.spec.datasets.push(freshDataset(null,s.spec.datasets.length));}
        else if(action==='remove-dataset'){const removed=s.spec.datasets.splice(i,1)[0];if(removed)for(const reduction of [...s.spec.objectives,...s.spec.constraints])reduction.dataset_keys=list(reduction.dataset_keys).filter(k=>k!==removed.key);}
        else if(action==='add-axis'){const parameter=s.templateAxes.find(k=>!s.spec.axes.some(x=>x.parameter===k));if(parameter&&s.spec.phase==='tuning')s.spec.axes.push({parameter,values:[0,.5,1]});}
        else if(action==='remove-axis')s.spec.axes.splice(i,1);
        else if(action==='add-objectives'&&s.spec.objectives.length<4)s.spec.objectives.push({metric:chargeStudy()?'temporal_residual_std_e':'temporal_residual_std_dn',direction:'minimize',dataset_keys:[],reducer:'max'});
        else if(action==='add-constraints'&&s.spec.constraints.length<12)s.spec.constraints.push({metric:chargeStudy()?'mean_abs_bias_e':'mean_abs_bias_dn',operator:'<=',value:1,unit:chargeStudy()?'electron_equivalent':'DN_equivalent',dataset_keys:[],reducer:'max'});
        else if(action==='remove-objectives')s.spec.objectives.splice(i,1);
        else if(action==='remove-constraints')s.spec.constraints.splice(i,1);
        syncText();
      }
    }catch(error){s.error=error.message;}render();return true;
  }
  return {activate,deactivate,view,input,change,click,submit,selectStudy};
};

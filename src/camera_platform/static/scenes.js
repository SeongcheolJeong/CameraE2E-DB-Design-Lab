'use strict';

window.CameraSceneUI = function(ctx) {
  const {api,esc,records,badge,details,heading,recordButton,render,refresh,toast,busy,state,chooseRun}=ctx;
  const s={kind:'scene',scene:'',assembly:'',assetText:'',renderText:'',templates:null,catalog:null,
    loading:false,attempted:false,error:'',pending:false,epoch:0,job:'',jobInfo:null,jobLoading:false,jobError:null,band:0,poll:false,pollError:'',templateIssues:[],cameraText:null,cameraPreview:null};
  const p={source:'',assembly:'',text:null,preview:null,issues:[],job:'',info:null,loading:false,error:null,pending:false,epoch:0,poll:false,pollError:''};
  const arr=v=>Array.isArray(v)?v:[], json=v=>JSON.stringify(v,null,2);
  const find=(kind,id)=>records(kind).find(r=>r.id===id);
  const assets=kind=>records('scene_assets').filter(a=>a.payload.kind===kind);
  const names={scene:'장면',geometry:'형상',material:'재질',light:'광원'};
  const jobScene=j=>j.scene_asset_id||j.request?.scene_asset_id;
  const jobs=()=>records('scene_renders').filter(j=>!s.scene||jobScene(j)===s.scene);
  const projectionSource=j=>j.source_render_id||j.request?.source_render_id;
  const projectionJobs=()=>records('scene_projections').filter(j=>projectionSource(j)===p.source);
  const completed=j=>['succeeded','failed','cancelled'].includes(j?.status);
  const projectionRecord=j=>({...j,artifacts:arr(j.artifacts??j.result?.artifacts)});
  function acceptProjection(job){
    const known=find('scene_projections',job.id);
    // A delayed queued response must not replace a later terminal observation.
    if((completed(known)||(p.info?.id===job.id&&completed(p.info)))&&!completed(job))return;
    const full=projectionRecord(job);
    state.data.scene_projections=arr(state.data.scene_projections).map(j=>j.id===full.id?full:j);
    if(p.job===full.id&&p.source===projectionSource(full)){p.info=full;p.error=null;}
  }
  const projectionKey=()=> 'camera-scene-projection:'+state.data?.state_dir;
  function rememberProjection(id){try{sessionStorage.setItem(projectionKey(),id);}catch{}}
  function resetProjection(source,assembly=''){
    p.source=source;p.assembly=assembly;p.text=null;p.preview=null;p.issues=[];p.job='';p.info=null;p.error=null;p.pending=false;p.pollError='';p.epoch++;
    p.job=projectionJobs()[0]?.id||'';
    const selected=find('scene_projections',p.job);if(selected)p.assembly=selected.assembly_id||selected.request?.assembly_id||assembly;
  }
  const selected=()=>find('scene_assets',s.scene);
  function parse(text) {
    if(new TextEncoder().encode(text).length>1000000)throw new Error('설계 JSON은 1 MB 이하여야 합니다.');
    return JSON.parse(text,(_k,v)=>{if(typeof v==='number'&&(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))))throw new Error('정확하게 표현할 수 있는 유한 수를 입력하세요.');return v;});
  }
  const safeDraft=()=>{try{return parse(s.assetText);}catch{return null;}};
  const canonical=v=>JSON.stringify(v,(_k,x)=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.keys(x).sort().map(k=>[k,x[k]])):x);
  const num=v=>v===null||v===undefined?'미확인':Number(v).toPrecision(5);
  const input=(label,id,path,value,kind='number')=>`<label class="field">${esc(label)}<input id="${id}" data-scene-render-path="${path}" type="${kind}" ${kind==='number'?'step="any"':''} value="${esc(value??'')}"></label>`;
  const opts=(items,id,empty='선택하세요')=>`<option value="">${esc(empty)}</option>${items.map(x=>`<option value="${esc(x.id)}" ${x.id===id?'selected':''}>${esc(x.name)} · ${esc(x.revision||'1')}</option>`).join('')}`;
  function draw() {
    if(state.view!=='scenes')return;
    const el=document.activeElement,focus=el?.closest('[data-scene-workbench]')&&el.id?{id:el.id,start:el.selectionStart,end:el.selectionEnd}:null;
    render();
    if(focus){const replacement=document.getElementById(focus.id);replacement?.focus({preventScroll:true});if(replacement?.setSelectionRange&&focus.start!==null)replacement.setSelectionRange(focus.start,focus.end);}
  }
  function edited(record) {
    return {name:record.name+' · 수정안',revision:'2',payload:structuredClone(record.payload),
      source_kind:'estimated',source_uri:'edit://'+record.id,license:record.license,
      method_and_version:'scene-workbench-declared-edit:1',physical_instance:null,parents:[record.id]};
  }
  function selectScene(id) {
    resetProjection('');
    s.scene=id;s.kind='scene';s.epoch++;s.cameraText=null;s.cameraPreview=null;s.renderText='';s.templateIssues=[];s.jobInfo=null;s.jobError=null;s.job='';
    const record=selected();s.assetText=record?json(edited(record)):json(s.templates?.assets.scene||{});
    const match=records('assemblies').find(a=>a.conditions.light_id?.value===record?.illumination_id);
    s.assembly=match?.id||s.assembly||records('assemblies')[0]?.id||'';
    s.job=jobs()[0]?.id||'';draw();
  }
  async function ensure() {
    if(s.loading||s.attempted)return;
    s.loading=true;s.attempted=true;s.error='';
    try {
      const [templates,catalog]=await Promise.all([api('/scene_authoring/templates'),api('/scene_renders/catalog')]);
      s.templates=templates;s.catalog=catalog;
      if(!s.scene&&assets('scene').length)selectScene(assets('scene')[0].id);
      if(!s.assetText)s.assetText=json(templates.assets[s.kind]);
      if(!s.assembly)s.assembly=records('assemblies')[0]?.id||'';
      let saved;try{saved=find('scene_projections',sessionStorage.getItem(projectionKey()));}catch{}
      const source=saved&&find('scene_renders',projectionSource(saved));
      if(source){selectScene(jobScene(source));s.job=source.id;resetProjection(source.id,saved.assembly_id||saved.request?.assembly_id);p.job=saved.id;}
    }catch(e){s.error=e.message;}finally{s.loading=false;draw();}
  }
  async function loadJob() {
    if(!s.job||s.jobLoading||s.jobInfo?.id===s.job||s.jobError?.id===s.job)return;
    const id=s.job,epoch=s.epoch;s.jobLoading=true;
    try {const job=await api('/scene_renders/'+encodeURIComponent(id));if(s.job===id&&s.epoch===epoch)s.jobInfo=job;}
    catch(e){if(s.job===id&&s.epoch===epoch)s.jobError={id,message:e.message};}
    finally{s.jobLoading=false;draw();}
  }
  function poll() {
    if(s.poll||s.pollError||!jobs().some(j=>['queued','running'].includes(j.status)))return;
    s.poll=true;
    setTimeout(async()=>{
      try {
        const pending=jobs().filter(j=>['queued','running'].includes(j.status));
        const updates=await Promise.all(pending.map(j=>api('/scene_renders/'+encodeURIComponent(j.id))));
        for(const job of updates){
          const old=find('scene_renders',job.id);
          state.data.scene_renders=arr(state.data.scene_renders).map(j=>j.id===job.id?job:j);
          if(s.job===job.id)s.jobInfo=job;
          if(old?.status!==job.status&&job.status==='failed')toast('장면 계산 실패: '+job.error,true);
          if(old?.status!==job.status&&job.status==='succeeded')toast('분광 렌더링 결과와 원본 근거를 저장했습니다.');
        }
        if(state.view==='scenes')draw();
      }catch(e){s.pollError=e.message;draw();}finally{s.poll=false;if(state.view==='scenes')poll();}
    },1600);
  }
  async function loadProjection() {
    const known=find('scene_projections',p.job),synced=p.info?.id===p.job&&(!completed(known)||completed(p.info));
    if(!p.job||p.loading||synced||p.error?.id===p.job)return;
    const id=p.job,epoch=p.epoch;p.loading=true;
    try{const job=await api('/scene_projections/'+encodeURIComponent(id));if(p.job===id&&p.epoch===epoch)acceptProjection(job);}
    catch(e){if(p.job===id&&p.epoch===epoch)p.error={id,message:e.message};}
    finally{p.loading=false;draw();}
  }
  function pollProjection(){
    const pending=projectionJobs().filter(j=>['queued','running'].includes(j.status)).map(j=>j.id);
    if(p.poll||p.pollError||!pending.length)return;
    p.poll=true;const epoch=p.epoch;
    setTimeout(async()=>{
      try{
        const updates=await Promise.all(pending.map(id=>api('/scene_projections/'+encodeURIComponent(id))));
        for(const job of updates)acceptProjection(job);
      }catch(e){if(epoch===p.epoch)p.pollError=e.message;}
      finally{p.poll=false;draw();if(state.view==='scenes')pollProjection();}
    },1600);
  }
  const absoluteProjection=projection=>projection?.projection_model==='prescription_radiometry_projection_v1';
  const projectionSpectrum=projection=>absoluteProjection(projection)?projection.on_axis_irradiance_per_radiance_sr:projection?.on_axis_transmittance;
  function projectionFields(projection,field,prefix){
    const spectrum=projectionSpectrum(projection);
    if(absoluteProjection(projection))return `<div data-scene-absolute-radiometry><h3>검증된 조리개 포함 광량 계수 · E/L · sr</h3><div class="scene-spectrum-grid">${spectrum.wavelength_nm.map((w,i)=>`<div><strong>${esc(w)} nm</strong><p>${esc(num(spectrum.values[i]))} sr</p></div>`).join('')}</div><p class="field-hint">고정 Lens의 조리개 투과율을 포함한 계수로 복사휘도를 변환합니다. PSF·상대 조도·입사각 응답은 Camera 계산에서 적용합니다.</p><p class="field-hint">Lens ${esc(projection.lens_model_id)} · Factory ${esc(projection.factory_job_id)}</p>${details('광량 계수의 원본 결속',{lens_model_sha256:projection.lens_model_sha256,factory_job_sha256:projection.factory_job_sha256,psf_factory_job_id:projection.psf_factory_job_id,psf_factory_job_sha256:projection.psf_factory_job_sha256,stop_transmission_sha256:projection.stop_transmission_sha256})}</div>`;
    return `<h3>렌즈 분광 투과율 · 0–1</h3><div class="scene-spectrum-grid">${spectrum.wavelength_nm.map((w,i)=>field(w+' nm',prefix+'-'+i,'projection.on_axis_transmittance.values.'+i,spectrum.values[i])).join('')}</div>${field('투과율 출처',prefix==='scene-projection-throughput'?'scene-projection-source':prefix+'-source','projection.source_uri',projection.source_uri,'text')}${field('방법 · 버전',prefix==='scene-projection-throughput'?'scene-projection-method':prefix+'-method','projection.method_and_version',projection.method_and_version,'text')}`;
  }
  function projectionView(source){
    if(source.status!=='succeeded')return '';
    if(p.source!==source.id)resetProjection(source.id,source.request.assembly_id);
    void loadProjection();pollProjection();
    let draft,error;
    if(p.text!==null){try{draft=parse(p.text);if(!draft?.projection||!Array.isArray(projectionSpectrum(draft.projection)?.wavelength_nm)||!Array.isArray(projectionSpectrum(draft.projection)?.values))throw new Error('광학 조건과 파장별 광량 계수 배열을 확인하세요.');}catch(e){error=e.message;}}
    const transmittance=projectionSpectrum(draft?.projection),absolute=absoluteProjection(draft?.projection);
    const complete=!error&&transmittance?.values.length>0&&transmittance.values.length===transmittance.wavelength_nm.length&&transmittance.values.every(v=>typeof v==='number'&&Number.isFinite(v)&&(absolute?v>0&&v<=Math.PI:v>=0&&v<=1))&&typeof draft.projection.source_uri==='string'&&draft.projection.source_uri.trim()&&typeof draft.projection.method_and_version==='string'&&draft.projection.method_and_version.trim();
    const field=(label,id,key,value,kind='number')=>`<label class="field">${esc(label)}<input id="${id}" data-scene-projection-path="${key}" type="${kind}" ${kind==='number'?'step="any"':''} value="${esc(value??'')}"></label>`;
    const job=p.info?.id===p.job?p.info:null;
    const outputLinks=arr(job?.artifacts).map((a,i)=>({...a,label:a.name==='spectral-image.npy'?(i===0?'새 센서면 광량 · ':'원본 센서면 광량 · ')+a.name:a.name==='irradiance-mc-standard-error.npy'?(i===1?'새 광량의 수치 표준오차 · ':'원본 수치 표준오차 · ')+a.name:a.name})).filter(a=>!a.name.endsWith('.png')).map(a=>`<a href="${esc(a.url)}">${esc(a.label)}</a>`).join('');
    return `<section class="panel" data-scene-projection><h3>저장된 장면 광량 재사용</h3><p class="field-hint">장면·촬영 위치·초점거리·픽셀 격자·온도가 같은 조건에서 투과율과 조리개를 변경해 센서면 광량을 다시 계산합니다. 조건이 달라지면 장면을 다시 렌더링해야 합니다.</p>
      <fieldset class="scene-fields" ${p.pending?'disabled':''}><label class="field">적용할 카메라<select id="scene-projection-assembly" data-scene-field="projectionAssembly">${opts(records('assemblies'),p.assembly)}</select></label><button class="button secondary" data-scene-action="projection-template" ${!p.assembly?'disabled':''}>재사용 조건 불러오기</button>
      ${p.issues.length?`<ul class="issue-list">${p.issues.map(x=>`<li>${esc(typeof x==='string'?x:x.message)}</li>`).join('')}</ul>`:''}
      ${p.text!==null?`${error?`<p class="scene-error" role="alert">${esc(error)}</p>`:`<p class="scene-optics">적용 Lens: ${esc(draft.projection.focal_length_mm??'미선언')} mm · F/${esc(draft.projection.f_number??'미선언')}</p>${projectionFields(draft.projection,field,'scene-projection-throughput')}${field('계산 이름','scene-projection-name','name',draft.name,'text')}`}
      <details class="data-detail" ${error?'open':''}><summary>재사용할 조건 · 출처 JSON</summary><textarea id="scene-projection-json" data-scene-field="projectionText" rows="12" spellcheck="false">${esc(p.text)}</textarea></details>${!complete?`<p class="field-hint" data-scene-projection-required>${absolute?'Lens의 검증된 광량 계수가 필요합니다. 재사용 조건을 다시 불러오세요.':'파장별 투과율(0–1)과 출처·방법을 먼저 입력하세요. 미확인 값은 자동으로 채우지 않습니다.'}</p>`:''}<div class="button-row"><button class="button secondary" data-scene-action="projection-preview" ${!complete?'disabled':''}>재사용 가능 여부 검사</button><button class="button primary" data-scene-action="projection-submit" ${p.preview?.status!=='ready'||!complete?'disabled':''}>센서면 광량 계산</button></div>`:''}
      ${p.preview?`<div data-scene-projection-preview>${badge(p.preview.status==='ready'?'PASS':'unknown',p.preview.status==='ready'?'재사용 가능':p.preview.status==='rerender_required'?'장면 재계산 필요':'추가 입력 필요')}<ul class="issue-list">${arr(p.preview.issues).map(x=>`<li>${esc(x.message)}</li>`).join('')}</ul><p class="field-hint">원본 복사휘도와 깊이를 유지합니다. 실물 예측 정확도와 총 불확실도는 미확립입니다.</p></div>`:''}
      <label class="field">저장된 광량 계산<select id="scene-projection-job" data-scene-field="projectionJob">${opts(projectionJobs(),p.job,'계산을 선택하세요')}</select></label>
      ${p.error?`<p class="scene-error">${esc(p.error.message)} <button class="button secondary" data-scene-action="projection-retry">같은 계산 다시 조회</button></p>`:''}
      ${p.pollError?`<p class="scene-error">${esc(p.pollError)} <button class="button secondary" data-scene-action="projection-retry-poll">상태 다시 조회</button></p>`:''}
      ${job?`<div data-scene-projection-result="${esc(job.id)}">${badge(job.status)} <strong>${esc(job.name)}</strong>${job.error?`<p class="scene-error">${esc(typeof job.error==='string'?job.error:json(job.error))}</p>`:''}<p class="field-hint">원본 장면의 복사휘도·깊이는 그대로 보존됩니다. 새 센서면 광량과 적용 카메라를 별도 기록으로 남깁니다.</p><div class="scene-artifact-list">${outputLinks}</div>${details('원본 장면 · 적용 조건',job.request)}${details('광량 계산 결과',job.result?.summary)}<button class="button primary" data-scene-action="projection-camera" ${job.status!=='succeeded'?'disabled':''}>이 광량으로 Camera 계산</button><p class="field-hint">실물 예측 정확도: 미확립</p></div>`:p.job?'<p class="field-hint">저장된 계산을 불러오는 중입니다.</p>':''}</fieldset></section>`;
  }
  function instanceEditor(draft) {
    if(draft?.payload?.kind!=='scene')return '';
    const p=draft.payload;
    return `<div class="scene-instance-editor"><label class="field">환경광<select id="scene-env" data-scene-field="environment">${opts(assets('light').filter(a=>a.payload.distribution==='constant_environment'),p.environment_light_id,'환경광 없음')}</select></label>
      <div class="scene-instance-list">${arr(p.instances).map((item,i)=>`<div class="scene-instance-row"><span class="eyebrow">OBJECT ${i+1}</span><label class="field">형상<select id="scene-geometry-${i}" data-scene-instance="${i}" data-key="geometry_id">${opts(assets('geometry'),item.geometry_id)}</select></label><label class="field">재질<select id="scene-material-${i}" data-scene-instance="${i}" data-key="material_id">${opts(assets('material'),item.material_id)}</select></label><div class="scene-position">${['X','Y','Z'].map((axis,k)=>`<label class="field">${axis} · m<input id="scene-position-${i}-${k}" type="number" step="any" value="${esc(item.to_world?.[k]?.[3]??0)}" data-scene-position="${i}" data-axis="${k}"></label>`).join('')}</div><button type="button" class="link-button" data-scene-remove="${i}">배치 제거</button></div>`).join('')}</div>
      <button type="button" class="button secondary" data-scene-action="add-instance">형상 배치 추가</button><p class="field-hint">단위는 m입니다. 회전·축척·면광원과 점광원은 아래 전체 설계 JSON에서 편집할 수 있습니다.</p></div>`;
  }
  function spectrumEditor(draft) {
    if(!['material','light'].includes(draft?.payload?.kind))return '';
    const p=draft.payload;
    if(!Array.isArray(p.spectrum?.wavelength_nm)||!Array.isArray(p.spectrum?.values))return '<p class="field-hint">분광 좌표와 값을 JSON에서 확인하세요.</p>';
    return `<div class="scene-spectrum"><h3>${p.kind==='light'?'광원 분광 값 · '+esc(p.unit):p.bsdf==='diffuse'?'분광 반사율 · 0–1':'복소 굴절률 · n, k'}</h3>${['spectrum','extinction_k'].filter(key=>p[key]).map(key=>`<div class="scene-spectrum-grid">${p[key].wavelength_nm.map((w,i)=>`<label class="field">${esc(w)} nm ${p.extinction_k?(key==='spectrum'?'n':'k'):''}<input id="scene-spectrum-${key}-${i}" type="number" step="any" min="0" ${p.kind==='material'&&p.bsdf==='diffuse'?'max="1"':''} value="${esc(p[key].values[i])}" data-scene-spectrum="${key}" data-index="${i}"></label>`).join('')}</div>`).join('')}</div>`;
  }
  function renderEditor() {
    let r;try{r=parse(s.renderText);}catch{return '<p class="field-hint">광학 조건을 불러오면 촬영 위치와 분광 투과율을 편집할 수 있습니다.</p>';}
    if(!Array.isArray(projectionSpectrum(r?.projection)?.wavelength_nm)||!Array.isArray(projectionSpectrum(r?.projection)?.values)||!Array.isArray(r.pose?.origin_m)||!Array.isArray(r.pose?.target_m)||!Array.isArray(r.shape))return '<p class="field-hint">촬영 JSON의 광학·위치·영상 크기 형식을 확인하세요.</p>';
    return `<div class="scene-render-fields"><p class="scene-optics">고정 Lens: 초점거리 ${esc(r.projection.focal_length_mm??'미선언')} mm · F/${esc(r.projection.f_number??'미선언')}</p>
      ${projectionFields(r.projection,input,'scene-throughput')}
      <div class="scene-form-grid">${input('실행 이름','scene-render-name','name',r.name,'text')}${input('픽셀·파장당 표본','scene-spp','samples_per_pixel',r.samples_per_pixel)}</div>
      <h3>카메라 위치 · m</h3><div class="scene-position">${['X','Y','Z'].map((axis,i)=>input(axis,'scene-camera-origin-'+i,'pose.origin_m.'+i,r.pose.origin_m[i])).join('')}</div>
      <h3>바라보는 위치 · m</h3><div class="scene-position">${['X','Y','Z'].map((axis,i)=>input(axis,'scene-camera-target-'+i,'pose.target_m.'+i,r.pose.target_m[i])).join('')}</div>
      <div class="scene-position">${input('높이 · pixel','scene-height','shape.0',r.shape[0])}${input('너비 · pixel','scene-width','shape.1',r.shape[1])}${input('난수 시드','scene-seed','seed',r.seed)}</div></div>`;
  }
  function cameraPreparation() {
    if(s.cameraText===null)return '';
    const editor=(open=false)=>`<details class="data-detail" ${open?'open':''}><summary>모든 조건 · 분광 범위 · RAW 계약</summary><textarea id="scene-camera-json" data-scene-field="cameraText" rows="14" spellcheck="false">${esc(s.cameraText)}</textarea></details>`;
    const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
    let d,error;
    try{
      d=parse(s.cameraText);
      if(!object(d)||!object(d.assembly)||!Array.isArray(d.assembly.model_ids)||d.assembly.model_ids.length!==3||!d.assembly.model_ids.every(id=>typeof id==='string')||!object(d.assembly.conditions)||typeof d.assembly.name!=='string'||typeof d.assembly.revision!=='string')throw new Error('assembly에 이름·Revision, 세 모델 ID와 conditions 객체가 필요합니다. JSON을 수정하거나 새 카메라 준비를 다시 불러오세요.');
    }catch(e){error=e.message;}
    if(error)return `<section class="scene-camera-preparation"><h3>새 카메라 · 촬영 조건 준비</h3><p class="scene-error" role="alert" data-scene-camera-format-error>${esc(error)}</p>${editor(true)}<div class="button-row"><button class="button secondary" data-scene-action="camera-preview" disabled>준비 조건 검사</button><button class="button primary" data-scene-action="camera-save" disabled>새 카메라 구성 저장</button></div></section>`;
    const a=d.assembly,p=s.cameraPreview,conditions={f_number:{value:null,unit:'1'},...a.conditions},labels={f_number:'조리개',exposure_s:'노출',gain:'Gain',temperature_c:'온도'};
    const kinds=['lens','sensor','isp'],slots={},used=new Set();
    for(const kind of kinds){const i=a.model_ids.findIndex(id=>find('models',id)?.kind===kind);if(i>=0){slots[kind]=i;used.add(i);}}
    for(const kind of kinds)if(slots[kind]===undefined){const i=a.model_ids.findIndex((_id,index)=>!used.has(index));slots[kind]=i;used.add(i);}
    return `<section class="scene-camera-preparation"><h3>새 카메라 · 촬영 조건 준비</h3><p class="field-hint">선택한 장면의 조명 조건을 불러왔습니다. 모델 버전과 촬영 조건을 검토하면 저장 전에 호환성·PSF·파장·조리개를 검사합니다.</p>
      <div class="scene-form-grid"><label class="field">구성 이름<input id="scene-prep-name" data-scene-camera-path="assembly.name" value="${esc(a.name)}"></label><label class="field">Revision<input id="scene-prep-revision" data-scene-camera-path="assembly.revision" value="${esc(a.revision)}"></label></div>
      ${kinds.map(kind=>{const i=slots[kind];return `<label class="field">${kind} · 고정 모델<select id="scene-prep-${kind}" data-scene-camera-model="${i}">${opts(records('models').filter(m=>m.kind===kind),a.model_ids[i])}</select></label>`;}).join('')}
      <div class="scene-form-grid">${Object.entries(conditions).filter(([k,v])=>k!=='light_id'&&v&&typeof v==='object'&&'value' in v).map(([k,v])=>`<label class="field">${esc(labels[k]||k)}${v.unit==='1'?'':' · '+esc(v.unit==='degC'?'°C':v.unit)}<input id="scene-prep-condition-${esc(k)}" data-scene-camera-condition="${esc(k)}" type="${(v.value===null||typeof v.value==='number')?'number':'text'}" step="any" value="${esc(v.value??'')}"></label>`).join('')}</div>
      <p class="field-hint">조명 조건: ${esc(selected()?.name||'선택 장면')} · 고정 버전</p>
      ${editor()}
      <div class="button-row"><button class="button secondary" data-scene-action="camera-preview">준비 조건 검사</button><button class="button primary" data-scene-action="camera-save" ${p?.status!=='ready'?'disabled':''}>새 카메라 구성 저장</button></div>
      ${p?`<div data-scene-camera-preview>${badge(p.status==='ready'?'PASS':'unknown',p.status==='ready'?'구성 준비 가능':'추가 입력 필요')}${p.issues.length?`<ul class="issue-list">${p.issues.map(x=>`<li>${esc(x.message)}</li>`).join('')}</ul>`:''}${details('기존 구성에서 바뀌는 조건',p.condition_changes)}${details('고정 광학 값 · Sensor 격자',{optics:p.optics,sampling:p.sampling,psf_coverage:p.psf_coverage})}${p.psf_coverage?`<p class="field-hint">위치별 PSF 범위(mm): Y ${esc(p.psf_coverage.field_y_mm.join(' · '))}, X ${esc(p.psf_coverage.field_x_mm.join(' · '))}. 범위 안의 작은 영역으로 연결을 검사했습니다. 실제 촬영 영역 전체의 포함 여부는 렌더링 전에 별도로 검사합니다.</p>`:''}<p class="field-hint">이 검사는 연결 조건을 확인합니다. 렌더링 단계에서 Lens의 검증된 광량 계수를 사용하거나, 기존 모델의 투과율을 별도로 입력합니다. 실물 정확도는 미확립입니다.</p></div>`:''}</section>`;
  }
  function resultView() {
    const job=s.jobInfo?.id===s.job?s.jobInfo:null;
    if(!job)return `<div class="empty-state"><h3>${s.jobError?.id===s.job?'원본 결과 조회 실패':s.job?'원본 결과를 불러오는 중':'아직 계산된 장면이 없습니다'}</h3><p>${s.jobError?.id===s.job?esc(s.jobError.message):'고정된 장면과 광학 조건으로 분광 계산을 실행하세요.'}</p>${s.jobError?.id===s.job?'<button class="button secondary" data-scene-action="retry-job">이 실행 다시 조회</button>':''}</div>`;
    const previews=arr(job.result?.display?.previews),bands=previews.filter(p=>p.kind==='spectral_radiance');
    const band=bands[Math.min(Number(s.band)||0,Math.max(0,bands.length-1))],depth=previews.find(p=>p.kind==='first_hit_depth');
    const figure=(p,title)=>p?`<figure><img src="${esc(p.artifact.url)}" alt="${esc(title)}" class="scene-preview" loading="lazy"><figcaption><strong>${esc(title)}</strong><span>${esc(num(p.display_min))} – ${esc(num(p.display_max))} ${esc(p.unit)}</span></figcaption></figure>`:'';
    const image=records('spectral_images').find(i=>i.render_job_snapshot?.id===job.id);
    return `<div data-scene-result>${badge(job.status)} <span class="muted">${esc(job.name)}</span>
      ${job.error?`<p class="scene-error">${esc(job.error)}</p>`:''}
      ${bands.length?`<label class="field scene-band-select">표시 파장<select id="scene-band" data-scene-field="band">${bands.map((p,i)=>`<option value="${i}" ${i===Number(s.band)?'selected':''}>${esc(p.wavelength_nm)} nm</option>`).join('')}</select></label>`:''}
      <div class="scene-preview-grid">${figure(band,`${band?.wavelength_nm} nm · 복사휘도`)}${figure(depth,'첫 표면까지의 거리')}</div>
      ${job.status==='succeeded'&&!bands.length?'<p class="field-hint">이전 개발 버전의 결과입니다. 원본 NPY는 보존되어 있으며 새 실행에서 미리보기를 생성합니다.</p>':''}
      <p class="field-hint">파장별 영상은 공통 범위의 선형 표시입니다. 깊이 영상의 검정은 표면 미검출입니다. 실제 RGB는 아래 Camera 계산에서 생성합니다.</p>
      <div class="button-row"><button class="button primary" data-scene-action="camera" ${job.status!=='succeeded'||s.pending?'disabled':''}>${image?'이 입력으로 Camera 계산':'입력 저장 · Camera 계산'}</button><a class="button secondary" href="/api/v1/scene_renders/${encodeURIComponent(job.id)}/bundle">원본 근거 ZIP</a>${['queued','running'].includes(job.status)?'<button class="button secondary" data-scene-action="cancel">계산 취소</button>':''}</div>
      <div class="scene-artifact-list">${arr(job.artifacts).filter(a=>!a.name.endsWith('.png')&&!a.name.endsWith('.json')).map(a=>`<a href="${esc(a.url)}">${esc(a.name)}</a>`).join('')}</div>
      ${details('실행 조건 · 원본 자산 · 광학 변환',job.request)}
      <div class="scene-trust"><strong>실물 예측 정확도: 미확립</strong><p>근축 투영·픽셀 중심 표본·유한 경로 계산입니다. Monte Carlo 표준오차는 총 예측 불확실도가 아닙니다.</p></div>${projectionView(job)}</div>`;
  }
  function view() {
    void ensure();void loadJob();poll();
    const draft=safeDraft(),library=assets(s.kind),scene=selected();
    let absoluteRender=false;try{absoluteRender=absoluteProjection(parse(s.renderText).projection);}catch{}
    return heading('SCENE ENGINEERING','장면을 바꾸고, 응답을 확인하세요','형상·재질·광원을 재사용하고 동일한 카메라 조건에서 변경 효과를 추적합니다.')+
      `<div data-scene-workbench><div class="scene-workflow"><span>01 자산 · 배치</span><span>02 광학 조건</span><span>03 분광 렌더링</span><span>04 RAW · RGB 비교</span></div>
      ${s.error?`<p class="scene-error" role="alert">${esc(s.error)} <button class="button secondary" data-scene-action="retry-templates">연결 다시 확인</button></p>`:''}
      ${s.pollError?`<p class="scene-error" role="alert">작업 상태 조회가 중단되었습니다. 작업 ID는 유지됩니다. ${esc(s.pollError)} <button class="button secondary" data-scene-action="retry-poll">같은 작업 상태 다시 조회</button></p>`:''}
      <fieldset class="scene-fields" ${s.pending?'disabled':''}>
      <div class="scene-layout"><section class="panel scene-library"><div class="panel-heading"><h2>재사용 자산</h2></div><div class="scene-tabs">${Object.entries(names).map(([kind,name])=>`<button class="button ${kind===s.kind?'primary':'secondary'}" data-scene-kind="${kind}">${name} ${assets(kind).length}</button>`).join('')}</div><div class="scene-library-list">${library.map(a=>`<div class="scene-library-item ${a.id===s.scene?'selected':''}"><button class="link-button" data-scene-edit="${esc(a.id)}"><strong>${esc(a.name)}</strong><small>r${esc(a.revision)} · ${esc(a.source_kind)}</small></button>${recordButton('scene_assets',a,'원본')}</div>`).join('')||'<p class="muted">등록된 자산이 없습니다. 새 설계안을 작성하세요.</p>'}</div><button class="button secondary" data-scene-action="new" ${!s.templates||s.pending?'disabled':''}>새 ${names[s.kind]} 설계안</button></section>
      <section class="panel scene-editor"><div class="panel-heading"><h2>${names[s.kind]} 설계안</h2><span class="badge orange">원본 유지 · 새 버전 저장</span></div><form data-scene-form="asset"><div class="scene-form-grid"><label class="field">이름<input id="scene-name" data-scene-field="name" value="${esc(draft?.name||'')}" required></label><label class="field">Revision<input id="scene-revision" data-scene-field="revision" value="${esc(draft?.revision||'1')}" required></label></div>${instanceEditor(draft)}${spectrumEditor(draft)}<details class="data-detail" ${s.kind==='geometry'?'open':''}><summary>전체 설계 · 분광 표 · 출처 JSON</summary><textarea id="scene-asset-json" data-scene-field="assetText" rows="16" spellcheck="false" aria-label="자산 설계 JSON">${esc(s.assetText)}</textarea></details><p class="field-hint">수정안은 추정/설계 데이터로 시작합니다. 실제 취득 자료는 정확한 출처와 방법을 선언하세요.</p><button type="submit" class="button primary" ${!draft||s.pending?'disabled':''}>${names[s.kind]} 버전 저장</button></form></section></div>
      <div class="scene-layout scene-execution"><section class="panel"><div class="panel-heading"><h2>장면 · 카메라 연결</h2>${badge(s.catalog?.available?'available':'unavailable',s.catalog?.available?'분광 계산 연결됨':'계산 연결 확인 필요')}</div><label class="field">저장된 장면<select id="scene-selected" data-scene-field="scene">${opts(assets('scene'),s.scene)}</select></label><label class="field">카메라 구성<select id="scene-render-assembly" data-scene-field="assembly">${opts(records('assemblies'),s.assembly)}</select></label><p class="field-hint">${scene?`형상 ${arr(scene.payload.instances).length}개 · 광원/배치 버전 고정`:'계산할 장면을 먼저 저장하세요.'}</p><button class="button secondary" data-scene-action="render-template" ${!s.scene||!s.assembly||s.pending?'disabled':''}>광학 조건 불러오기</button>
      <button class="button secondary" data-scene-action="camera-template" ${!s.scene||!s.assembly||s.pending?'disabled':''}>이 장면의 새 카메라 준비</button>${cameraPreparation()}
      ${s.templateIssues.length?`<ul class="issue-list">${s.templateIssues.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><p class="field-hint"><a href="#builder">카메라 구성</a> · <a href="#factory">PSF 모델 생성</a>에서 근거가 있는 모델 버전을 연결하세요.</p>`:''}<p class="field-hint">${absoluteRender?'센서면 광량으로 변환할 계수(E/L)는 검증된 Factory 결과에서 자동으로 불러왔습니다. 조리개 투과율과 출처는 고정 Lens에 결속되어 있습니다.':'센서면 광량으로 변환할 렌즈 투과율과 출처를 직접 입력하세요.'} 초점거리·F-number는 고정 Lens 모델과 일치해야 합니다.</p><form data-scene-form="render">${renderEditor()}<details class="data-detail"><summary>전체 촬영 조건 · 광학 변환 JSON</summary><label class="field">촬영 시점 · ${absoluteRender?'고정 광량 계수':'분광 투과율'} · 계산 조건<textarea id="scene-render-json" data-scene-field="renderText" rows="16" spellcheck="false">${esc(s.renderText)}</textarea></label></details><button type="submit" class="button primary" ${!s.renderText||s.pending||!s.catalog?.available?'disabled':''}>분광 렌더링 실행</button></form>${s.catalog?.reason?`<p class="scene-error">${esc(s.catalog.reason)}</p>`:''}</section>
      <section class="panel"><div class="panel-heading"><h2>계산 결과 · 원본 근거</h2></div><label class="field">저장된 실행<select id="scene-job" data-scene-field="job">${opts(jobs(),s.job,'실행을 선택하세요')}</select></label>${resultView()}</section></div></fieldset></div>`;
  }
  document.addEventListener('input',e=>{
    if(s.pending&&e.target.closest('[data-scene-workbench]'))return;
    if(e.target.dataset.sceneProjectionPath){
      if(p.pending)return;
      try{const d=parse(p.text),parts=e.target.dataset.sceneProjectionPath.split('.');let at=d;for(const k of parts.slice(0,-1))at=at[k];at[parts.at(-1)]=e.target.type==='number'?(e.target.value.trim()?Number(e.target.value):null):e.target.value;p.text=json(d);p.preview=null;draw();}catch(error){toast(error.message,true);}return;
    }
    if(e.target.dataset.sceneCameraPath||e.target.dataset.sceneCameraCondition){
      try{const d=parse(s.cameraText);if(e.target.dataset.sceneCameraCondition){const key=e.target.dataset.sceneCameraCondition;d.assembly.conditions[key]??={value:null,unit:'1'};d.assembly.conditions[key].value=e.target.type==='number'?(e.target.value.trim()?Number(e.target.value):null):e.target.value;}else{d.assembly[e.target.dataset.sceneCameraPath.split('.').at(-1)]=e.target.value;}s.cameraText=json(d);s.cameraPreview=null;draw();}catch(error){toast(error.message,true);}return;
    }
    if(e.target.dataset.sceneRenderPath){
      try{const d=parse(s.renderText),parts=e.target.dataset.sceneRenderPath.split('.');let at=d;for(const k of parts.slice(0,-1))at=at[k];at[parts.at(-1)]=e.target.type==='number'?(e.target.value.trim()?Number(e.target.value):null):e.target.value;s.renderText=json(d);const editor=document.getElementById('scene-render-json');if(editor)editor.value=s.renderText;}catch(error){toast(error.message,true);}return;
    }
    if(e.target.dataset.sceneSpectrum){try{const d=parse(s.assetText);d.payload[e.target.dataset.sceneSpectrum].values[Number(e.target.dataset.index)]=e.target.value.trim()?Number(e.target.value):null;s.assetText=json(d);document.getElementById('scene-asset-json').value=s.assetText;}catch(error){toast(error.message,true);}return;}
    const field=e.target.dataset.sceneField;
    if(field==='projectionText'){if(p.pending)return;p.text=e.target.value;p.preview=null;const submit=document.querySelector('[data-scene-action="projection-submit"]');if(submit)submit.disabled=true;document.querySelector('[data-scene-projection-preview]')?.remove();return;}
    if(field==='cameraText'){s.cameraText=e.target.value;s.cameraPreview=null;const save=document.querySelector('[data-scene-action="camera-save"]');if(save)save.disabled=true;document.querySelector('[data-scene-camera-preview]')?.remove();return;}
    if(field==='assetText'||field==='renderText'){s[field]=e.target.value;if(field==='assetText'){const button=e.target.closest('form')?.querySelector('button[type="submit"]');if(button)button.disabled=s.pending;}return;}
    if(field==='name'||field==='revision'){
      try{const draft=parse(s.assetText);draft[field]=e.target.value;s.assetText=json(draft);const editor=document.getElementById('scene-asset-json');if(editor)editor.value=s.assetText;}catch{}
    }
  });
  document.addEventListener('change',e=>{
    const field=e.target.dataset.sceneField;
    if(s.pending&&e.target.closest('[data-scene-workbench]'))return;
    if(field==='projectionText'){draw();return;}
    if(field==='projectionAssembly'){if(p.pending)return;p.assembly=e.target.value;p.epoch++;p.text=null;p.preview=null;p.issues=[];p.job='';p.info=null;p.error=null;draw();return;}
    if(field==='projectionJob'){if(p.pending)return;p.job=e.target.value;p.info=null;p.error=null;p.epoch++;p.text=null;p.preview=null;p.issues=[];const job=find('scene_projections',p.job);if(job)p.assembly=job.assembly_id||job.request?.assembly_id||p.assembly;rememberProjection(p.job);draw();return;}
    if(field==='renderText'){
      const panel=document.querySelector('.scene-render-fields');if(panel)panel.outerHTML=renderEditor();return;
    }
    if(field==='assetText'){
      const draft=safeDraft();if(!draft)return;
      const instances=document.querySelector('.scene-instance-editor'),spectrum=document.querySelector('.scene-spectrum');
      if(instances)instances.outerHTML=instanceEditor(draft);if(spectrum)spectrum.outerHTML=spectrumEditor(draft);
      document.getElementById('scene-name').value=draft.name||'';document.getElementById('scene-revision').value=draft.revision||'';return;
    }
    if(e.target.dataset.sceneCameraModel!==undefined){try{const d=parse(s.cameraText);d.assembly.model_ids[Number(e.target.dataset.sceneCameraModel)]=e.target.value;s.cameraText=json(d);s.cameraPreview=null;draw();}catch(error){toast(error.message,true);}return;}
    if(field==='cameraText'){draw();return;}
    if(field==='scene'){resetProjection('');selectScene(e.target.value);return;}
    if(field==='assembly'){s.assembly=e.target.value;s.epoch++;s.cameraText=null;s.cameraPreview=null;s.renderText='';s.templateIssues=[];draw();return;}
    if(field==='job'){resetProjection('');s.job=e.target.value;s.jobInfo=null;s.jobError=null;s.epoch++;draw();return;}
    if(field==='band'){s.band=Number(e.target.value);draw();return;}
    if(field==='environment'||e.target.dataset.sceneInstance!==undefined||e.target.dataset.scenePosition!==undefined){
      try{
        const draft=parse(s.assetText),p=draft.payload;
        if(field==='environment')p.environment_light_id=e.target.value||null;
        else if(e.target.dataset.sceneInstance!==undefined)p.instances[Number(e.target.dataset.sceneInstance)][e.target.dataset.key]=e.target.value;
        else{const v=Number(e.target.value);if(!e.target.value.trim()||!Number.isFinite(v))throw new Error('유한한 위치 값을 입력하세요.');p.instances[Number(e.target.dataset.scenePosition)].to_world[Number(e.target.dataset.axis)][3]=v;}
        s.assetText=json(draft);draw();
      }catch(error){toast(error.message,true);}
    }
  });
  document.addEventListener('click',e=>{
    const button=e.target.closest('[data-scene-action],[data-scene-kind],[data-scene-edit],[data-scene-remove]');if(!button||s.pending)return;
    if(button.dataset.sceneAction?.startsWith('projection-')){projectionAction(button);return;}
    if(button.dataset.sceneKind){s.kind=button.dataset.sceneKind;s.assetText=json(s.templates?.assets[s.kind]||{});draw();return;}
    if(button.dataset.sceneEdit){const a=find('scene_assets',button.dataset.sceneEdit);if(a.payload.kind==='scene')selectScene(a.id);else{s.kind=a.payload.kind;s.assetText=json(edited(a));draw();}return;}
    busy(button,async()=>{
      const action=button.dataset.sceneAction;
      if(action==='retry-templates'){s.attempted=false;s.templates=null;await ensure();return;}
      if(action==='retry-job'){s.jobError=null;await loadJob();return;}
      if(action==='retry-poll'){s.pollError='';poll();draw();return;}
      if(action==='new'){s.assetText=json(s.templates.assets[s.kind]);draw();return;}
      if(action==='add-instance'||button.dataset.sceneRemove!==undefined){const d=parse(s.assetText);if(action==='add-instance'){const g=assets('geometry')[0],m=assets('material')[0];if(!g||!m)throw new Error('형상과 재질을 먼저 등록하세요.');d.payload.instances.push({geometry_id:g.id,material_id:m.id,area_light_id:null,to_world:[[.015,0,0,0],[0,.015,0,0],[0,0,.015,3],[0,0,0,1]]});}else d.payload.instances.splice(Number(button.dataset.sceneRemove),1);s.assetText=json(d);draw();return;}
      const epoch=s.epoch;s.pending=true;draw();
      try{
        if(action==='camera-template'){const d=await api('/scene_authoring/camera-template?scene_id='+encodeURIComponent(s.scene)+'&assembly_id='+encodeURIComponent(s.assembly));if(epoch===s.epoch){s.cameraText=json(d);s.cameraPreview=null;}}
        if(action==='camera-preview'){const d=parse(s.cameraText);if(d.scene_asset_id!==s.scene||d.source_assembly_id!==s.assembly)throw new Error('선택한 장면·기준 구성과 준비 요청이 다릅니다.');const p=await api('/scene_authoring/camera-preview',d);if(epoch===s.epoch)s.cameraPreview=p;}
        if(action==='camera-save'){if(!s.cameraPreview||canonical(s.cameraPreview.draft)!==canonical(parse(s.cameraText)))throw new Error('현재 준비 조건을 먼저 검사하세요.');const result=await api('/scene_authoring/cameras',{draft:s.cameraPreview.draft,preview_sha256:s.cameraPreview.preview_sha256});await refresh({renderPage:false});if(epoch===s.epoch&&state.view==='scenes'){s.assembly=result.assembly.id;s.epoch++;s.cameraText=null;s.cameraPreview=null;s.renderText=json(result.render.request);s.templateIssues=result.render.issues;}toast('원본 모델과 조건을 보존하고 새 카메라 구성을 저장했습니다.');}
        if(action==='render-template'){const result=await api('/scene_authoring/render-template?scene_id='+encodeURIComponent(s.scene)+'&assembly_id='+encodeURIComponent(s.assembly));if(epoch===s.epoch){s.renderText=json(result.request);s.templateIssues=result.issues;}}
        if(action==='cancel'){const job=await api('/scene_renders/'+encodeURIComponent(s.job)+'/cancel',{});if(s.job===job.id)s.jobInfo=job;await refresh({renderPage:false});}
        if(action==='camera'){
          const job=s.jobInfo;if(job?.id!==s.job||job.status!=='succeeded')throw new Error('완료된 장면 계산을 선택하세요.');
          let image=records('spectral_images').find(i=>i.render_job_snapshot?.id===job.id);
          if(!image)image=await api('/scene_render_publications',{job_id:job.id,name:job.name+' · Camera 입력',revision:'1'});
          const assembly=job.request.assembly_id;
          const draft=await api('/templates/spatial-test?assembly_id='+encodeURIComponent(assembly)+'&scene_id='+encodeURIComponent(image.id));
          let test=records('tests').find(t=>Object.keys(draft).every(k=>canonical(t[k])===canonical(draft[k])));
          if(!test)test=await api('/tests',draft);
          const run=await api('/runs',{assembly_id:assembly,test_id:test.id,plugin_id:'spatial_scene_v1',scene_id:image.id,parameters:{frames:2,seed:42}});
          await refresh({renderPage:false});if(epoch===s.epoch&&state.view==='scenes'&&location.hash==='#scenes'){chooseRun(run.id);state.experimentMode='camera';location.hash='experiments';render();}toast('고정된 3D 장면의 Camera 계산을 제출했습니다.');
        }
      }finally{s.pending=false;draw();}
    });
  });
  function projectionAction(button){
    if(p.pending)return;
    busy(button,async()=>{
      const action=button.dataset.sceneAction;
      if(action==='projection-retry'){p.error=null;await loadProjection();return;}
      if(action==='projection-retry-poll'){p.pollError='';pollProjection();draw();return;}
      const epoch=p.epoch,source=p.source;p.pending=true;draw();
      try{
        if(action==='projection-template'){
          const result=await api('/scene_authoring/projection-template?source_render_id='+encodeURIComponent(source)+'&assembly_id='+encodeURIComponent(p.assembly));
          if(epoch===p.epoch&&s.job===source){p.text=json(result.request);p.preview=null;p.issues=arr(result.issues);}
        }
        if(action==='projection-preview'||action==='projection-submit'){
          const draft=parse(p.text);
          if(draft.source_render_id!==source||draft.assembly_id!==p.assembly)throw new Error('원본 실행·적용 카메라와 재사용 조건이 다릅니다. 조건을 다시 불러오세요.');
          if(action==='projection-preview'){
            p.preview=null;
            const result=await api('/scene_projections/preview',draft);
            if(epoch===p.epoch&&s.job===source)p.preview=result;
          }else{
            if(p.preview?.status!=='ready'||canonical(p.preview.draft)!==canonical(draft))throw new Error('현재 재사용 조건을 먼저 검사하세요.');
            const job=await api('/scene_projections',{draft,preview_sha256:p.preview.preview_sha256});
            await refresh({renderPage:false});
            if(epoch===p.epoch&&s.job===source){p.job=job.id;p.info=projectionRecord(job);p.error=null;rememberProjection(job.id);}
            toast('원본 장면을 보존하고 새 센서면 광량 계산을 제출했습니다.');
          }
        }
        if(action==='projection-camera'){
          const job=p.info;
          if(job?.id!==p.job||job.status!=='succeeded'||projectionSource(job)!==source)throw new Error('완료된 광량 계산을 선택하세요.');
          let image=records('spectral_images').find(i=>i.projection_job_snapshot?.id===job.id);
          if(!image)image=await api('/scene_projection_publications',{job_id:job.id,name:job.name+' · Camera 입력',revision:'1'});
          const assembly=job.request.assembly_id;
          const draft=await api('/templates/spatial-test?assembly_id='+encodeURIComponent(assembly)+'&scene_id='+encodeURIComponent(image.id));
          let test=records('tests').find(t=>Object.keys(draft).every(k=>canonical(t[k])===canonical(draft[k])));
          if(!test)test=await api('/tests',draft);
          const run=await api('/runs',{assembly_id:assembly,test_id:test.id,plugin_id:'spatial_scene_v1',scene_id:image.id,parameters:{frames:2,seed:42}});
          await refresh({renderPage:false});
          if(epoch===p.epoch&&p.source===source&&s.job===source&&state.view==='scenes'&&location.hash==='#scenes'){chooseRun(run.id);state.experimentMode='camera';location.hash='experiments';render();}
          toast('저장된 광량과 적용 카메라로 RAW · RGB 계산을 제출했습니다.');
        }
      }finally{if(epoch===p.epoch)p.pending=false;draw();}
    });
  }
  document.addEventListener('submit',e=>{
    const form=e.target.closest('[data-scene-form]');if(!form)return;e.preventDefault();if(s.pending)return;
    busy(form.querySelector('button[type="submit"]'),async()=>{
      const epoch=s.epoch;s.pending=true;draw();
      try{
        if(form.dataset.sceneForm==='asset'){
          const draft=parse(s.assetText);const saved=await api('/scene_assets',draft);
          await refresh({renderPage:false});s.templates=await api('/scene_authoring/templates');
          if(epoch===s.epoch){s.kind=saved.payload.kind;if(s.kind==='scene')selectScene(saved.id);else s.assetText=json(edited(saved));}
          toast('원본을 보존하고 새 자산 버전을 저장했습니다.');
        }else{
          const request=parse(s.renderText);
          if(request.scene_asset_id!==s.scene||request.assembly_id!==s.assembly)throw new Error('선택한 장면·카메라와 렌더링 JSON의 ID가 다릅니다. 조건을 다시 불러오세요.');
          const job=await api('/scene_renders',request);await refresh({renderPage:false});
          if(epoch===s.epoch){s.job=job.id;s.jobInfo=job;s.band=0;}
          toast('원본을 고정하고 분광 렌더링을 제출했습니다.');
        }
      }finally{s.pending=false;draw();}
    });
  });
  return {view};
};

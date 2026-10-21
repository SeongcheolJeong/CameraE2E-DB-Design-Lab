'use strict';

window.CameraReadoutFittingUI=function(ctx){
  const {api,esc,records,render,refresh,toast,busy,state,recordButton,details}=ctx;
  const s={capture:'',metadata:'',metadataFile:null,metadataOriginal:'',evidence:null,epoch:0,pending:false,uncertain:null,base:'',name:'응답·읽기 잡음 추정',training:[],validation:[],slopeLow:'0.5',slopeHigh:'2',sigmaLow:'0.00000001',sigmaHigh:'0.1',iterations:'100',plan:null,job:null,publication:null,publishName:'추정 응답 보정',revision:'1',saved:null,open:{import:true,fit:true,history:false}};
  const draw=()=>{if(state.view==='validation')render();};
  const json=x=>JSON.stringify(x,null,2);
  const lock=()=>s.pending||Boolean(s.uncertain);
  function parse(text){
    if(new TextEncoder().encode(text).length>1048576)throw Error('관측 메타데이터는 UTF-8 1 MiB 이하여야 합니다.');
    const value=JSON.parse(text,(_k,v)=>{if(typeof v==='number'&&(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))))throw Error('정확히 표현 가능한 유한 수를 입력하세요.');return v;});
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error('관측 메타데이터는 JSON 객체여야 합니다.');
    return value;
  }
  const select=(field,label,entries)=>`<div class="form-field"><label for="readoutfit-${field}">${esc(label)}</label><select id="readoutfit-${field}" data-readoutfit-field="${field}"><option value="">선택하세요</option>${entries.map(r=>`<option value="${esc(r.id)}" ${s[field]===r.id?'selected':''}>${esc(r.name)} · ${esc(r.id.slice(-8))}</option>`).join('')}</select></div>`;
  const input=(field,label,type='text',extra='')=>`<div class="form-field"><label for="readoutfit-${field}">${esc(label)}</label><input id="readoutfit-${field}" data-readoutfit-field="${field}" type="${type}" value="${esc(s[field])}" ${extra}></div>`;
  function observations(role,label){
    const rows=records('readout_observations').filter(r=>r.dataset_role===(role==='training'?'development':'holdout'));
    return `<div class="form-field"><strong>${label}</strong>${rows.length?rows.map(r=>`<label class="check-option"><input type="checkbox" data-readoutfit-observation="${role}" value="${esc(r.id)}" ${s[role].includes(r.id)?'checked':''}><span>${esc(r.name)}<small>${esc(r.capture_id)} · ${esc(r.source_kind)}</small></span></label>`).join(''):'<small>해당 역할의 교정 관측을 먼저 등록하세요.</small>'}</div>`;
  }
  function resultView(){
    if(!s.job)return '';
    const job=s.job,r=job.result;
    return `<div class="callout neutral"><strong>${esc(job.name||job.specification?.name||'응답 추정')} · ${esc(job.status)}</strong><p>${esc(job.error||'')}</p><button class="button quiet small" data-readoutfit-action="load" data-id="${esc(job.id)}" ${lock()?'disabled':''}>실행 상태·진단 새로 읽기</button>${recordButton('readout_fits',job,'실행 근거 ↗')}</div>${r?`${details('추정 후보·학습 및 별도 평가 진단',r)}<p class="list-note">평가 데이터는 후보 선택에 사용된 근거로 보존됩니다. 이후 ISP 독립 검증에는 새 취득 데이터가 필요합니다. 절대 전하 기준의 불확실도와 실제 카메라 검증 상태는 별도로 유지합니다.</p>`:''}${job.status==='succeeded'&&s.publication?`<fieldset ${lock()?'disabled':''}>${input('publishName','새 보정 모델 이름')}${input('revision','보정 revision')}<button class="button primary" data-readoutfit-action="publish" ${s.saved?'disabled':''}>검토한 후보를 응답 보정 버전으로 등록</button></fieldset>`:''}${s.saved?`<div class="callout">보정 버전을 등록했습니다. ISP 재처리에서 이 버전을 선택할 수 있습니다. ${recordButton('readout_calibrations',s.saved,'등록된 보정 ↗')}</div>`:''}`;
  }
  function view(){
    const caps=records('captures').filter(r=>r.schema==='camera_nonlinear_capture_v1');
    return `<section class="panel" data-readoutfit-panel><div class="panel-header"><div><span class="eyebrow">ANCHORED RESPONSE FITTING</span><h2>교정 관측으로 응답·읽기 잡음 추정</h2><p>독립적으로 교정된 입력 전하와 원본 RAW를 연결해 응답 후보를 만들고 별도 취득에서 평가합니다.</p></div></div><div class="panel-content"><fieldset ${lock()?'disabled':''}><details class="data-detail" data-readoutfit-open="import" ${s.open.import?'open':''}><summary>1. 교정 관측 등록</summary>${select('capture','연결할 RAW 취득',caps)}<button class="button quiet" data-readoutfit-action="template" ${!s.capture?'disabled':''}>빈 관측 형식으로 교체</button><p class="list-note">노출 시간이나 RAW 평균만으로 절대 전하를 정할 수 없습니다. 프레임별 R·Gr·Gb·B의 교정된 총 입력 전하 평균(암전하 포함)과 그 근거를 입력하세요. 예제의 비어 있는 전하 값은 직접 채워야 합니다.</p>${['metadata','evidence'].map(key=>`<div class="form-field"><label for="readoutfit-file-${key}">${key==='metadata'?'관측 메타데이터 JSON':'절대 전하 기준 근거 JSON'}</label><input id="readoutfit-file-${key}" type="file" accept=".json,application/json" data-readoutfit-file="${key}"><small>${esc((key==='metadata'?s.metadataFile:s.evidence)?.name||'원본 파일 선택')} · ${key==='metadata'?'1':'2'} MiB 이하</small></div>`).join('')}<div class="form-field"><label for="readoutfit-metadata">관측 메타데이터 · JSON</label><textarea id="readoutfit-metadata" data-readoutfit-metadata spellcheck="false">${esc(s.metadata)}</textarea></div><button class="button secondary" data-readoutfit-action="import" ${!s.metadata||!s.evidence?'disabled':''}>교정 관측 원본 보존</button></details><details class="data-detail" data-readoutfit-open="fit" ${s.open.fit?'open':''}><summary>2. 추정 범위와 별도 평가 데이터 선택</summary>${select('base','기준 응답 보정',records('readout_calibrations'))}${input('name','추정 작업 이름')}${observations('training','학습 · development 관측')}${observations('validation','별도 평가 · holdout 관측')}<div class="form-row">${input('slopeLow','응답 기울기 배율 하한','number','min="0.01" step="any"')}${input('slopeHigh','응답 기울기 배율 상한','number','max="100" step="any"')}</div><div class="form-row">${input('sigmaLow','읽기 잡음 σ 하한 (V)','number','min="0" step="any"')}${input('sigmaHigh','읽기 잡음 σ 상한 (V)','number','max="1" step="any"')}</div>${input('iterations','최대 최적화 반복','number','min="10" max="200" step="1"')}<p class="list-note">기준 모델의 ADC·gain·전하 knot·암전류 맵은 고정합니다. DCG는 두 경로의 주변 분포를 맞추며 경로 간 잡음 상관은 기준 모델에서 유지합니다.</p><button class="button secondary" data-readoutfit-action="plan" ${!s.base||!s.training.length||!s.validation.length?'disabled':''}>입력 독립성·추정 범위 검사</button>${s.plan?`${details('고정할 추정 계획과 계산량',s.plan)}<button class="button primary" data-readoutfit-action="submit">이 계획으로 Worker 추정 실행</button>`:''}</details></fieldset>${s.uncertain?`<div class="callout">${esc(s.uncertain.kind==='import'?'관측 등록':s.uncertain.kind==='submit'?'추정 제출':'보정 등록')} 응답을 확인하지 못했습니다. 저장 결과를 확인한 뒤 진행하세요.<button class="button secondary small" data-readoutfit-action="reconcile" ${s.pending?'disabled':''}>저장 결과 확인</button></div>`:''}${resultView()}<details class="data-detail" data-readoutfit-open="history" ${s.open.history?'open':''}><summary>관측 ${records('readout_observations').length}개 · 추정 실행 ${records('readout_fits').length}개</summary>${records('readout_observations').map(r=>`<div class="locked-reference"><div><strong>${esc(r.name)}</strong><small>${esc(r.dataset_role)} · ${esc(r.source_kind)}</small></div>${recordButton('readout_observations',r,'관측 원본 ↗')}</div>`).join('')}${records('readout_fits').map(r=>`<div class="locked-reference"><div><strong>${esc(r.name)}</strong><small>${esc(r.status)}</small></div><button class="link-button" data-readoutfit-action="load" data-id="${esc(r.id)}" ${lock()?'disabled':''}>추정 결과 열기 →</button></div>`).join('')}</details></div></section>`;
  }
  function specification(){return {schema:'camera_readout_fit_request_v1',name:s.name,base_calibration_id:s.base,training_observation_ids:[...s.training],validation_observation_ids:[...s.validation],slope_scale_bounds:[Number(s.slopeLow),Number(s.slopeHigh)],sigma_v_bounds:[Number(s.sigmaLow),Number(s.sigmaHigh)],max_iterations:Number(s.iterations)};}
  const sha=async file=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');
  async function mutate(path,body,uncertain){
    s.uncertain=uncertain;
    try{return await api(path,body);}catch(error){if(error.status>=400&&error.status<500)s.uncertain=null;throw error;}
  }
  function imported(record){s.uncertain=null;s.evidence=null;s.metadataFile=null;s.metadataOriginal='';s.open.history=true;s.plan=null;toast('교정 관측 원본을 보존했습니다.');}
  async function load(id){
    const job=await api('/readout-fits/'+encodeURIComponent(id));
    s.job=job;s.publication=null;s.saved=null;
    if(job.status==='succeeded'){const manifest=await api('/readout-fits/'+encodeURIComponent(id)+'/export');s.publication=manifest.publication_binding;}
  }
  document.addEventListener('toggle',e=>{const key=e.target.dataset?.readoutfitOpen;if(key)s.open[key]=e.target.open;},true);
  document.addEventListener('input',e=>{
    if(lock())return;
    if(e.target.dataset?.readoutfitMetadata!==undefined){s.metadata=e.target.value;s.epoch++;const button=document.querySelector('[data-readoutfit-action="import"]');if(button)button.disabled=!s.metadata||!s.evidence;return;}
    const key=e.target.dataset?.readoutfitField;
    if(key&&key!=='capture'&&key!=='base'){s[key]=e.target.value;s.epoch++;if(!['publishName','revision'].includes(key))s.plan=null;}
  });
  document.addEventListener('change',async e=>{
    if(lock())return;
    const field=e.target.dataset?.readoutfitField,role=e.target.dataset?.readoutfitObservation;
    if(field){s[field]=e.target.value;s.epoch++;if(!['publishName','revision','capture'].includes(field))s.plan=null;draw();return;}
    if(role){s[role]=e.target.checked?[...new Set([...s[role],e.target.value])]:s[role].filter(id=>id!==e.target.value);s.plan=null;s.epoch++;draw();return;}
    const key=e.target.dataset?.readoutfitFile;if(!key)return;
    const f=e.target.files?.[0]||null,epoch=key==='evidence'?s.epoch:++s.epoch;
    try{
      if(key==='evidence'){s.evidence=null;if(f?.size>2097152)throw Error('근거 JSON은 2 MiB 이하여야 합니다.');s.evidence=f;draw();return;}
      s.metadataFile=null;s.metadataOriginal='';s.metadata='';draw();if(!f)return;
      if(f.size>1048576)throw Error('관측 JSON은 1 MiB 이하여야 합니다.');
      const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await f.arrayBuffer()),data=parse(text);
      if(epoch!==s.epoch)return;s.metadata=text;s.metadataOriginal=text;s.metadataFile=f;s.capture=data.capture_id||s.capture;draw();
    }catch(error){if(epoch===s.epoch){draw();toast(error.message,true);}}
  });
  document.addEventListener('click',e=>{
    const button=e.target.closest('[data-readoutfit-action]');if(!button||s.pending)return;
    const action=button.dataset.readoutfitAction;
    if(s.uncertain&&action!=='reconcile')return;
    busy(button,async()=>{
      s.pending=true;draw();
      try{
        if(action==='template'){
          const epoch=++s.epoch,value=await api('/templates/readout-observation?capture_id='+encodeURIComponent(s.capture));
          if(epoch===s.epoch){s.metadata=json(value);s.metadataFile=null;s.metadataOriginal='';}
        }else if(action==='import'){
          parse(s.metadata);if(!s.evidence)throw Error('전하 기준 근거 JSON을 선택하세요.');
          const original=s.metadataFile&&s.metadataOriginal===s.metadata,metadata=original?s.metadataFile:new Blob([new TextEncoder().encode(s.metadata)],{type:'application/json'});
          const body=new FormData();body.append('metadata_file',metadata,original?s.metadataFile.name:'readout-fit-observation-metadata.json');body.append('evidence',s.evidence);
          const hashes=await Promise.all([[metadata,'readout-fit-observation-metadata.json'],[s.evidence,'readout-fit-observation-evidence.json']].map(async([f,name])=>({name,sha256:await sha(f)})));
          imported(await mutate('/readout-observations/import',body,{kind:'import',hashes}));await refresh({renderPage:false});
        }else if(action==='plan'){
          s.plan=null;s.plan=await api('/readout-fits/plan',specification());
        }else if(action==='submit'){
          if(!s.plan)throw Error('입력을 바꾼 뒤에는 계획을 다시 검사하세요.');
          const body={specification:s.plan.specification,plan_hash:s.plan.plan_hash};
          const job=await mutate('/readout-fits',body,{kind:'submit',plan_hash:body.plan_hash});
          s.uncertain=null;s.plan=null;s.job=job;s.publication=null;s.saved=null;s.open.history=true;await refresh({renderPage:false});toast('추정 작업을 제출했습니다. 상태를 새로 읽어 완료 진단을 확인하세요.');
        }else if(action==='load'){await load(button.dataset.id);
        }else if(action==='publish'){
          if(s.saved||!s.publication||s.job?.status!=='succeeded')throw Error('완료된 후보의 진단을 먼저 읽으세요.');
          const body={name:s.publishName,revision:s.revision,...s.publication};
          const saved=await mutate('/readout-fits/'+encodeURIComponent(s.job.id)+'/publish',body,{kind:'publish',job_id:s.job.id,body});
          s.saved=saved;s.uncertain=null;await refresh({renderPage:false});toast('검토한 후보를 새 응답 보정 버전으로 등록했습니다.');
        }else if(action==='reconcile'){
          const u=s.uncertain;if(!u)return;
          const rows=await api(u.kind==='import'?'/readout-observations':u.kind==='submit'?'/readout-fits':'/readout-calibrations');
          const matches=rows.filter(r=>u.kind==='import'?u.hashes.every(h=>r.artifacts?.some(a=>a.name===h.name&&a.sha256===h.sha256)):u.kind==='submit'?r.plan_hash===u.plan_hash:r.fit_job_id===u.job_id&&r.request_sha256===u.body.request_sha256&&r.result_sha256===u.body.result_sha256&&r.name===u.body.name&&r.revision===u.body.revision);
          if(matches.length!==1)throw Error(matches.length?'일치하는 결과가 여러 개입니다. 실행 근거를 확인하세요.':'일치하는 저장 결과를 아직 찾지 못했습니다. 연결을 확인하고 다시 조회하세요.');
          if(u.kind==='import')imported(matches[0]);else if(u.kind==='submit'){s.uncertain=null;s.plan=null;s.job=matches[0];s.publication=null;s.saved=null;await load(matches[0].id);}else{s.saved=matches[0];s.uncertain=null;}
          await refresh({renderPage:false});toast('저장된 결과를 확인했습니다.');
        }
      }finally{s.pending=false;draw();}
    });
  });
  return {view};
};

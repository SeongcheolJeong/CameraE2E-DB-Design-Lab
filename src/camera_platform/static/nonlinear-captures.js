'use strict';

window.CameraNonlinearCaptureUI=function(ctx){
  const {api,esc,records,render,refresh,toast,busy,state,recordButton,onReplay}=ctx;
  const make=()=>({metadata:'',metadataFile:null,metadataOriginal:'',raw:null,dark_rate:null,evidence:null,epoch:0,uncertain:null,saved:null});
  const s={capture:make(),calibration:make(),assembly:'',test:'',sensor:'',mode:'sequential_frames',pending:false,open:{capture:true,calibration:false,history:false}};
  const draw=()=>{if(state.view==='validation')render();};
  const uncertain=()=>Boolean(s.capture.uncertain||s.calibration.uncertain);
  const nonlinear=()=>records('captures').filter(x=>x.schema==='camera_nonlinear_capture_v1');
  function parse(text){
    if(new TextEncoder().encode(text).length>1048576)throw Error('메타데이터 JSON은 UTF-8 1 MiB 이하여야 합니다.');
    const value=JSON.parse(text,(_key,v)=>{
      if(typeof v==='number'&&(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))))throw Error('브라우저에서 정확히 표현 가능한 수를 입력하세요. 큰 정수 시계는 상대 원점 또는 Python SDK를 사용하세요.');
      return v;
    });
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error('메타데이터는 JSON 객체여야 합니다.');
    return value;
  }
  const select=(key,label,entries,value)=>`<div class="form-field"><label for="nonlinear-${key}">${esc(label)}</label><select id="nonlinear-${key}" data-nonlinear-field="${key}"><option value="">선택하세요</option>${entries.map(x=>`<option value="${esc(x.id)}" ${value===x.id?'selected':''}>${esc(x.name)} · ${esc(x.id.slice(-8))}</option>`).join('')}</select></div>`;
  function file(role,key,label,help){
    const f=key==='metadata'?s[role].metadataFile:s[role][key],id=`nonlinear-${role}-${key}`;
    return `<div class="form-field"><label for="${id}">${esc(label)}</label><input id="${id}" type="file" accept="${key==='metadata'||key==='evidence'?'.json,application/json':'.npy'}" data-nonlinear-file="${key}" data-role="${role}"><small>${f?esc(f.name)+' · '+Math.ceil(f.size/1024)+' KiB 선택됨':esc(help)}</small></div>`;
  }
  function summary(role){
    try{
      const d=parse(s[role].metadata);
      const original=s[role].metadataFile&&s[role].metadataOriginal===s[role].metadata;
      return `출처: ${d.source_kind||'미입력'} · ${role==='capture'?'취득 '+(d.frames||[]).length+'개 · 적용 설정 미확인 '+(d.frames||[]).filter(f=>f.settings_evidence?.status!=='recorded_applied').length+'개':'절대 전하 기준: '+(d.charge_reference?.kind||'미입력')} · ${original?'선택 JSON의 원본 바이트 보존':'편집한 JSON을 새 원문으로 보존'}`;
    }catch{return '원본 JSON을 선택하거나 형식을 불러와 실제 기록을 입력하세요.';}
  }
  function form(role){
    const capture=role==='capture',d=s[role];
    return `<form data-nonlinear-form="${role}">${capture?file(role,'raw','원본 RAW NPY','정수 [F,H,W] 또는 DCG [A,2,H,W], HCG→LCG 순서 · 최대 64 MiB'):file(role,'dark_rate','기대 Dark rate NPY','electron/pixel/s · 기준 온도의 [H,W] 실수 배열. Dark RAW나 한 번의 노이즈 표본이 아닙니다.')+file(role,'evidence','절대 전하 기준·응답 근거 JSON','원본 보정 근거 · 최대 2 MiB. 외부 교정 근거는 실제 자료를 사용하세요.')}${file(role,'metadata','메타데이터 JSON 원본','UTF-8 JSON · 최대 1 MiB')}<div class="form-field"><label for="nonlinear-${role}-metadata-text">${capture?'취득 메타데이터':'응답 보정 선언'} · JSON</label><textarea id="nonlinear-${role}-metadata-text" data-nonlinear-metadata="${role}" spellcheck="false" required>${esc(d.metadata)}</textarea></div><p class="list-note" data-nonlinear-summary="${role}">${esc(summary(role))}</p><button type="submit" class="button primary full" ${s.pending||!d.metadata||(capture?!d.raw:!d.dark_rate||!d.evidence)?'disabled':''}>${capture?'비선형 RAW 원본 보존':'응답 보정 버전 등록'}</button></form>`;
  }
  function view(){
    const captures=nonlinear(),calibrations=records('readout_calibrations');
    return `<section class="panel" data-nonlinear-panel><div class="panel-header"><div><span class="eyebrow">NONLINEAR RAW · RESPONSE CALIBRATION</span><h2>비선형 RAW와 응답 보정 연결</h2><p>원본 취득과 응답 보정을 별도 버전으로 보존합니다.</p></div></div><div class="panel-content"><fieldset class="analysis-fields" ${s.pending||uncertain()?'disabled':''}><div class="form-field"><label for="nonlinear-mode">RAW 읽기 구성</label><select id="nonlinear-mode" data-nonlinear-field="mode"><option value="sequential_frames" ${s.mode==='sequential_frames'?'selected':''}>단일 경로 · 순차 프레임</option><option value="shared_charge_paths" ${s.mode==='shared_charge_paths'?'selected':''}>DCG · 동일 전하 HCG/LCG</option></select><small>형식을 불러올 때만 사용합니다. 이미 작성한 원본 JSON의 모드를 바꾸지 않습니다.</small></div><details class="data-detail" data-nonlinear-open="capture" ${s.open.capture?'open':''}><summary>1. 원본 취득 등록</summary>${select('assembly','원본 카메라 구성',records('assemblies'),s.assembly)}${select('test','원본 시험',records('tests'),s.test)}<button class="button quiet" type="button" data-nonlinear-action="template" data-role="capture" ${!s.assembly||!s.test?'disabled':''}>빈 취득 형식으로 교체</button><p class="list-note">실제 노출·온도·각 경로의 analog/digital gain을 기록하세요. 모르는 값은 null로 보존하며, 적용 설정을 모르면 전하 단위 재처리는 사용할 수 없습니다.</p>${form('capture')}</details><details class="data-detail" data-nonlinear-open="calibration" ${s.open.calibration?'open':''}><summary>2. 응답 보정 버전 등록</summary>${select('sensor','보정 대상 센서 모델',records('models').filter(x=>x.kind==='sensor'),s.sensor)}<button class="button quiet" type="button" data-nonlinear-action="template" data-role="calibration" ${!s.sensor?'disabled':''}>응답 보정 예제 형식으로 교체</button><p class="list-note">예제는 합성 기준입니다. 실측에는 동일 실물의 외부 전하·방사계측·공급사 교정 기준과 불확실도가 필요합니다. 제공된 근거의 진위나 실측 정확도를 자동 인증하지 않습니다.</p>${form('calibration')}</details></fieldset>${['capture','calibration'].filter(role=>s[role].uncertain).map(role=>`<div class="callout">${role==='capture'?'취득':'보정'} 등록 응답을 확인하지 못했습니다. 같은 원본의 저장 결과를 먼저 확인하세요.<button type="button" class="button secondary small" data-nonlinear-action="reconcile" data-role="${role}" ${s.pending?'disabled':''}>원본 일치 기록 확인</button></div>`).join('')}<div class="callout neutral">RAW만 먼저 등록할 수 있습니다. ISP 재처리에서 원본 RAW·HCG/LCG 경로·응답 보정 버전을 명시적으로 선택하세요. 보정 모델을 바꿀 때 같은 RAW를 다시 업로드하지 않습니다.</div><details class="data-detail" data-nonlinear-open="history" ${s.open.history?'open':''}><summary>보존된 취득 ${captures.length}개 · 응답 보정 ${calibrations.length}개</summary>${captures.map(x=>`<div class="locked-reference"><div><strong>${esc(x.name)}</strong><small>${esc(x.source_kind)} · ${esc(x.raw_shape.join(' × '))} · ${esc(x.id.slice(-8))}</small></div><div class="compact-actions">${recordButton('captures',x,'취득 원본 ↗')}<button class="link-button" data-nonlinear-action="replay" data-id="${esc(x.id)}">응답 선택 · ISP 재처리 →</button></div></div>`).join('')}${calibrations.map(x=>`<div class="locked-reference"><div><strong>${esc(x.name)}</strong><small>응답 r${esc(x.revision)} · ${esc(x.charge_reference?.kind||'기준 확인')} · ${esc(x.id.slice(-8))}</small></div>${recordButton('readout_calibrations',x,'보정 원본 ↗')}</div>`).join('')}</details></div></section>`;
  }
  document.addEventListener('toggle',e=>{const key=e.target.dataset?.nonlinearOpen;if(key)s.open[key]=e.target.open;},true);
  document.addEventListener('input',e=>{
    const role=e.target.dataset?.nonlinearMetadata;if(!role||uncertain())return;
    s[role].metadata=e.target.value;s[role].epoch++;
    const out=document.querySelector(`[data-nonlinear-summary="${role}"]`);if(out)out.textContent=summary(role);
    const button=e.target.closest('form')?.querySelector('button[type="submit"]');
    if(button)button.disabled=s.pending||!s[role].metadata||(role==='capture'?!s[role].raw:!s[role].dark_rate||!s[role].evidence);
  });
  document.addEventListener('change',async e=>{
    if(uncertain())return;
    const field=e.target.dataset?.nonlinearField;
    if(field){s[field]=e.target.value;s.capture.epoch++;s.calibration.epoch++;draw();return;}
    const key=e.target.dataset?.nonlinearFile,role=e.target.dataset?.role;if(!key||!role)return;
    const d=s[role],epoch=++d.epoch,f=e.target.files?.[0]||null;
    try{
      if(key==='metadata'){d.metadataFile=null;d.metadataOriginal='';if(!f){draw();return;}
        d.metadata='';draw();
        if(f.size>1048576)throw Error('메타데이터는 1 MiB 이하여야 합니다.');
        const value=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await f.arrayBuffer()),metadata=parse(value);
        if(epoch!==d.epoch)return;d.metadata=value;d.metadataOriginal=value;d.metadataFile=f;
        if(role==='capture'){s.assembly=metadata.assembly_id||'';s.test=metadata.test_id||'';}else{s.sensor=metadata.sensor_model_id||'';}
        if(['sequential_frames','shared_charge_paths'].includes(metadata.readout_mode))s.mode=metadata.readout_mode;
      }else{
        d[key]=null;if(f&&f.size>(key==='evidence'?2097152:67108864))throw Error('파일이 업로드 크기 제한을 초과했습니다.');d[key]=f;
      }
      draw();
    }catch(error){if(epoch===d.epoch){draw();toast(error.message,true);}}
  });
  document.addEventListener('click',e=>{
    const button=e.target.closest('[data-nonlinear-action]');if(!button||s.pending)return;
    busy(button,async()=>{
      if(button.dataset.nonlinearAction==='replay'){await onReplay('capture',button.dataset.id);return;}
      const role=button.dataset.role,d=s[role],epoch=d.epoch;
      if(button.dataset.nonlinearAction==='reconcile'){
        if(!d.uncertain)return;s.pending=true;draw();
        try{
          const entries=await api(role==='capture'?'/captures':'/readout-calibrations');
          const matches=entries.filter(record=>d.uncertain.every(h=>record.artifacts?.some(a=>a.name===h.name&&a.sha256===h.sha256)));
          if(matches.length!==1)throw Error(matches.length?'일치하는 기록이 여러 개입니다. 등록 원본을 확인하세요.':'아직 원본과 일치하는 기록이 없습니다. 연결을 확인하고 다시 조회하세요.');
          saved(role,matches[0]);await refresh({renderPage:false});toast('원본 바이트가 일치하는 저장 기록을 확인했습니다.');
        }finally{s.pending=false;draw();}return;
      }
      if(uncertain())throw Error('먼저 이전 등록 결과를 확인하세요.');
      s.pending=true;draw();
      try{
        const query=role==='capture'?'/templates/nonlinear-capture?assembly_id='+encodeURIComponent(s.assembly)+'&test_id='+encodeURIComponent(s.test)+'&readout_mode='+encodeURIComponent(s.mode):'/templates/readout-calibration?sensor_model_id='+encodeURIComponent(s.sensor)+'&mode='+encodeURIComponent(s.mode);
        const result=await api(query);if(epoch!==d.epoch)return;
        d.metadata=JSON.stringify(result,null,2);d.metadataFile=null;d.metadataOriginal='';
      }finally{s.pending=false;draw();}
    });
  });
  function saved(role,record){
    const d=s[role];d.saved=record;d.uncertain=null;s.open.history=true;
    d.raw=null;d.dark_rate=null;d.evidence=null;d.metadataFile=null;d.metadataOriginal='';
  }
  document.addEventListener('submit',e=>{
    const form=e.target.closest('[data-nonlinear-form]');if(!form)return;e.preventDefault();if(s.pending||uncertain())return;
    busy(form.querySelector('button[type="submit"]'),async()=>{
      const role=form.dataset.nonlinearForm,d=s[role],snapshot={...d};s.pending=true;draw();
      try{
        parse(snapshot.metadata);const body=new FormData();
        for(const key of role==='capture'?['raw']:['dark_rate','evidence']){if(!snapshot[key])throw Error('필수 원본 파일을 선택하세요.');body.append(key,snapshot[key]);}
        const original=snapshot.metadataFile&&snapshot.metadataOriginal===snapshot.metadata;
        const metadataFile=original?snapshot.metadataFile:new Blob([new TextEncoder().encode(snapshot.metadata)],{type:'application/json'});
        body.append('metadata_file',metadataFile,original?snapshot.metadataFile.name:'nonlinear-'+role+'-metadata.json');
        const parts=role==='capture'?[[metadataFile,'acquisition-metadata.json'],[snapshot.raw,'raw-capture.npy']]:[[metadataFile,'readout-calibration-metadata.json'],[snapshot.dark_rate,'expected-dark-rate.npy'],[snapshot.evidence,'readout-evidence.json']];
        const hashes=await Promise.all(parts.map(async([file,name])=>({name,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('')})));
        d.uncertain=hashes;
        let record;
        try{record=await api(role==='capture'?'/captures/nonlinear/import':'/readout-calibrations/import',body);}
        catch(error){if(error.status>=400&&error.status<500)d.uncertain=null;throw error;}
        saved(role,record);
        await refresh({renderPage:false});
        toast(role==='capture'?'비선형 RAW 원본을 보존했습니다. 응답 보정은 재처리에서 선택하세요.':'응답 보정 버전을 보존했습니다. 같은 RAW에 적용할 버전을 선택하세요.');
      }finally{s.pending=false;draw();}
    });
  });
  return {view};
};

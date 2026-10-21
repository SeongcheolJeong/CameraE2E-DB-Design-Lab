'use strict';

window.CameraResponseImportUI=function(ctx){
  const {api,esc,records,render,refresh,toast,busy,state,onImport}=ctx;
  const s={program:'',test:'',observable:'green_mean_normalized',testDraft:'',metadata:'',raw:null,file:null,fileText:'',pending:false,epoch:0,authorOpen:false};
  const find=(kind,id)=>records(kind).find(r=>r.id===id),draw=()=>{if(state.view==='validation')render();};
  const json=v=>JSON.stringify(v,null,2);
  const tests=()=>records('tests').filter(t=>t.observation_method?.id==='control_response_observation_v1'&&(!s.program||t.stimulus?.control_program_id===s.program));
  function parse(text){if(new TextEncoder().encode(text).length>1048576)throw new Error('JSON은 UTF-8 1 MiB 이하여야 합니다.');return JSON.parse(text,(_k,v)=>{if(typeof v==='number'&&(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))))throw new Error('시각은 브라우저에서 정확히 표현 가능한 정수 ns여야 합니다. 큰 시계는 상대 원점 또는 Python SDK를 사용하세요.');return v;});}
  function summary(){try{const d=parse(s.metadata),f=d.acquisition?.frames||[];return `선언 출처: ${d.source_kind||'미입력'} · 계획 프레임 ${f.length}개 · RAW 대응 선언 ${f.filter(x=>x.raw_index!==null&&x.raw_index!==undefined).length}개 · 적용 설정 미확인 ${f.filter(x=>x.applied==null).length}개\n시험: ${d.test_id||'미입력'}\n${s.file&&s.fileText===s.metadata?'선택한 JSON 파일의 원본 바이트를 업로드합니다.':'편집한 UTF-8 JSON을 새 원문으로 업로드합니다.'}`;}catch{return 'UTF-8 JSON을 선택하거나 입력하세요. 배열·로그의 정합성은 서버에서 검사합니다.';}}
  function select(field,label,items,value){return `<div class="form-field"><label for="response-${field}">${esc(label)}</label><select id="response-${field}" data-response-field="${field}"><option value="">선택하세요</option>${items.map(([id,name])=>`<option value="${esc(id)}" ${id===value?'selected':''}>${esc(name)}</option>`).join('')}</select></div>`;}
  const observables=['green_mean_normalized','wb_adjusted_rg_ratio','wb_adjusted_bg_ratio','exposure_ns','gain','wb_red_gain','wb_blue_gain','ae_command_latency_ns','awb_command_latency_ns'];
  function view(){
    return `<section class="panel" data-response-import-panel><div class="panel-header"><div><span class="eyebrow">RAW + ACQUISITION LOG</span><h2>폐루프 RAW·제어 로그 가져오기</h2><p>실제 적용 설정·시계·누락을 원본 RAW와 함께 보존합니다.</p></div></div><div class="panel-content"><fieldset class="analysis-fields" ${s.pending?'disabled':''}>${select('program','촬영 프로그램',records('control_programs').map(p=>[p.id,p.name]),s.program)}<details class="data-detail" data-response-author ${s.authorOpen?'open':''}><summary>첫 관측 시험 만들기</summary>${select('observable','프레임별 관측 지표',observables.map(v=>[v,v]),s.observable)}<button type="button" class="button secondary small" data-response-action="test-template" ${!s.program?'disabled':''}>공통 시험 형식 불러오기</button><form data-response-form="test"><div class="form-field"><label for="response-testDraft">시험 이름·허용 오차·검증 종류와 고정 조건 · JSON</label><textarea id="response-testDraft" data-response-field="testDraft" spellcheck="false">${esc(s.testDraft)}</textarea></div><button type="submit" class="button secondary full" ${!s.testDraft?'disabled':''}>공통 관측 시험 등록</button></form></details>${select('test','취득을 연결할 공통 시험',tests().map(t=>[t.id,t.name+' · '+t.id.slice(-8)]),s.test)}<button type="button" class="button quiet" data-response-action="metadata-template" ${!s.test?'disabled':''}>빈 취득 형식으로 교체</button><p class="list-note">형식의 null은 미확인입니다. 실제 RAW 순서·적용 설정·시계 대응·명령 로그를 기록에서 입력하세요. 빈 commands는 발행 명령이 없다는 선언이며, 로그를 확보하지 못한 상태와 구분해야 합니다.</p>
      <form data-response-form="import"><div class="form-field"><label for="response-raw-file">RAW NPY · 필수</label><input id="response-raw-file" type="file" accept=".npy" data-response-file="raw"><small>${s.raw?esc(s.raw.name)+' · '+Math.ceil(s.raw.size/1024)+' KiB 선택됨':'선형 RGGB 정수 [N,H,W] · 최대 64 MiB'}</small></div><div class="form-field"><label for="response-metadata-file">취득 로그 JSON 원본 · 선택 또는 아래에 입력</label><input id="response-metadata-file" type="file" accept=".json,application/json" data-response-file="metadata"><small>${s.file?esc(s.file.name)+' 선택됨':'UTF-8 JSON · 최대 1 MiB. 편집하지 않으면 원본 바이트를 그대로 보존합니다.'}</small></div><div class="form-field"><label for="response-metadata">취득 메타데이터와 프레임·제어 로그 · JSON</label><textarea id="response-metadata" data-response-field="metadata" spellcheck="false" required>${esc(s.metadata)}</textarea></div><p class="list-note response-import-summary" data-response-import-summary>${esc(summary())}</p><div class="callout">실측은 장비·Serial·교정·정책 실행 근거가 필요합니다. 출처 이름만 바꾼 합성 RAW는 독립 실측이 되지 않습니다. 알 수 없는 시각·설정·불확실도는 그대로 남기세요.</div><div class="inline-spacer"></div><button type="submit" class="button primary full" ${!s.raw||!s.test?'disabled':''}>원본 보존 · 첫 분석 생성</button></form></fieldset></div></section>`;
  }
  document.addEventListener('toggle',e=>{if(e.target.matches?.('[data-response-author]'))s.authorOpen=e.target.open;},true);
  document.addEventListener('input',e=>{const field=e.target.dataset?.responseField;if(!field)return;s[field]=e.target.value;s.epoch++;if(field==='testDraft'){const submit=e.target.closest('form')?.querySelector('button[type="submit"]');if(submit)submit.disabled=!s.testDraft||s.pending;}if(field==='metadata'){const out=document.querySelector('[data-response-import-summary]');if(out)out.textContent=summary();}});
  document.addEventListener('change',async e=>{
    const field=e.target.dataset?.responseField;
    if(field==='program'){s.test='';draw();}else if(field==='test'){draw();}
    const kind=e.target.dataset?.responseFile;if(!kind)return;
    const file=e.target.files?.[0],epoch=++s.epoch;
    try{
      if(kind==='raw'){if(file&&file.size>67108864)throw new Error('RAW는 64 MiB 이하여야 합니다.');s.raw=file||null;draw();return;}
      s.file=null;s.fileText='';if(!file){draw();return;}
      if(file.size>1048576)throw new Error('JSON은 1 MiB 이하여야 합니다.');
      const bytes=await file.arrayBuffer(),text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);
      const data=parse(text);if(epoch!==s.epoch)return;
      s.metadata=text;s.file=file;s.fileText=text;
      const test=find('tests',data.test_id);if(test){s.test=test.id;s.program=test.stimulus?.control_program_id||'';}
      draw();
    }catch(error){if(epoch===s.epoch){if(kind==='raw')s.raw=null;draw();toast(error.message,true);}}
  });
  document.addEventListener('click',e=>{
    const button=e.target.closest('[data-response-action]');if(!button||s.pending)return;
    busy(button,async()=>{const epoch=s.epoch,program=s.program,test=s.test;s.pending=true;try{
      if(button.dataset.responseAction==='test-template'){const draft=await api('/control_response_tests/template',{control_program_id:program,observable:s.observable});if(epoch===s.epoch){s.testDraft=json(draft);s.authorOpen=true;}}
      else{const draft=await api('/control_response_tests/'+encodeURIComponent(test)+'/acquisition-template');if(epoch===s.epoch){s.metadata=json(draft);s.file=null;s.fileText='';}}
    }finally{s.pending=false;draw();}});
  });
  document.addEventListener('submit',e=>{
    const form=e.target.closest('[data-response-form]');if(!form)return;e.preventDefault();if(s.pending)return;
    busy(form.querySelector('button[type="submit"]'),async()=>{const epoch=s.epoch,snapshot={...s};s.pending=true;try{
      if(form.dataset.responseForm==='test'){const test=await api('/tests',parse(snapshot.testDraft));await refresh({renderPage:false});if(epoch===s.epoch){s.test=test.id;s.program=test.stimulus.control_program_id;}toast('공통 관측 시험을 등록했습니다.');}
      else{
        const data=parse(snapshot.metadata);if(data.test_id!==snapshot.test)throw new Error('선택한 시험과 메타데이터의 test_id가 다릅니다. 원본에 대응하는 시험을 선택하세요.');
        if(!snapshot.raw)throw new Error('RAW 원본을 선택하세요.');
        const body=new FormData();body.append('raw',snapshot.raw);
        // Blob/File parts preserve LF/CRLF exactly; multipart string fields may normalize newlines.
        const original=snapshot.file&&snapshot.fileText===snapshot.metadata?snapshot.file:new Blob([new TextEncoder().encode(snapshot.metadata)],{type:'application/json'});
        body.append('metadata_file',original,snapshot.file&&snapshot.fileText===snapshot.metadata?snapshot.file.name:'acquisition-metadata.json');
        const result=await api('/captures/control-response/import',body);await refresh({renderPage:false});
        if(epoch===s.epoch){onImport(result);s.raw=null;s.file=null;s.fileText='';}toast('취득 원본과 첫 분석 버전을 보존했습니다.');
      }
    }finally{s.pending=false;draw();}});
  });
  return {view};
};

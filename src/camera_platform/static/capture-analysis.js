'use strict';

// Observations have their own revisions. The acquisition never acquires a new identity here.
window.CameraCaptureAnalysisUI = function(ctx) {
  const {api,esc,records,badge,details,recordButton,render,refresh,toast,busy,state,chooseCapture}=ctx;
  const s={capture:'',test:'',parent:'',name:'RAW 응답 재분석',revision:'1',uncertainty:'',observable:'green_mean_normalized',reduction:'trajectory',target:'',band:'',start:'',end:'',dwell:'2',testName:'공통 RAW 응답 시험',testRevision:'1',pending:false,epoch:0,testOpen:false};
  const find=(kind,id)=>records(kind).find(r=>r.id===id);
  const captures=()=>records('captures').filter(c=>c.capture_kind==='control_response_observation_v1');
  const origin=c=>find('tests',c?.acquisition_test_id||c?.test_id);
  const analyses=c=>records('capture_analyses').filter(a=>a.capture_id===c?.id);
  const legacy=c=>records('measurements').filter(m=>m.raw_capture_id===c?.id&&!m.capture_analysis_id);
  const candidates=c=>records('tests').filter(t=>t.observation_method?.id==='control_response_observation_v1'&&t.stimulus?.control_program_id===origin(c)?.stimulus?.control_program_id);
  const draw=()=>{if(state.view==='validation')render();};
  function resetMetric(){const test=find('tests',s.test);s.uncertainty=JSON.stringify({kind:'unknown',unit:test?.unit||'1',reason:'선택한 관측 지표의 총 불확실도는 아직 확립되지 않았습니다.'},null,2);}
  function selectCapture(id){s.capture=id;s.parent='';s.test=candidates(find('captures',id))[0]?.id||'';const frames=origin(find('captures',id))?.stimulus?.planned_frames||[];s.start=frames[0]?.frame_id||'';s.end=frames.at(-1)?.frame_id||'';resetMetric();}
  function select(field,label,items,value){return `<div class="form-field"><label for="analysis-${field}">${esc(label)}</label><select id="analysis-${field}" data-analysis-field="${field}"><option value="">선택하세요</option>${items.map(([id,name])=>`<option value="${esc(id)}" ${id===value?'selected':''}>${esc(name)}</option>`).join('')}</select></div>`;}
  function input(field,label,type='text'){return `<div class="form-field"><label for="analysis-${field}">${esc(label)}</label><input id="analysis-${field}" data-analysis-field="${field}" type="${type}" ${type==='number'?'step="any"':''} value="${esc(s[field])}"></div>`;}
  const observables=[['green_mean_normalized','정규화 Green 평균'],['wb_adjusted_rg_ratio','적용 WB 후 R/G'],['wb_adjusted_bg_ratio','적용 WB 후 B/G'],['exposure_ns','실제 노출 · ns'],['gain','실제 Gain'],['wb_red_gain','실제 WB Red Gain'],['wb_blue_gain','실제 WB Blue Gain'],['ae_command_latency_ns','AE 명령 지연 · ns'],['awb_command_latency_ns','AWB 명령 지연 · ns']];
  function view(){
    if(!captures().length)return '';
    if(!find('captures',s.capture))selectCapture(captures()[0].id);
    const capture=find('captures',s.capture),test=find('tests',s.test),frames=(origin(capture)?.stimulus?.planned_frames||[]).map(f=>[f.frame_id,f.frame_id]);
    return `<section class="panel" data-capture-analysis-panel><div class="panel-header"><div><span class="eyebrow">ONE ACQUISITION · MANY OBSERVATIONS</span><h2>보존 RAW를 다른 지표로 분석</h2><p>원본은 한 번 취득하고, 시험·관측 결과는 각각의 버전으로 보존합니다.</p></div></div><div class="panel-content"><fieldset class="analysis-fields" ${s.pending?'disabled':''}>${select('capture','취득 원본',captures().map(c=>[c.id,c.name+' · '+c.source_kind]),s.capture)}<p data-analysis-identity>${badge(capture.source_kind)} 취득 1건 · 분석 ${analyses(capture).length+legacy(capture).length}개<br><small>${esc(capture.id)}</small></p><p class="list-note">같은 원본의 분석 결과들은 독립된 실측 표본이 아닙니다. RAW·장비·시계·실제 적용 설정·데이터 식별자를 공유합니다.</p>
      <details class="data-detail" data-analysis-authoring ${s.testOpen?'open':''}><summary>다른 지표의 공통 시험 만들기</summary><form data-analysis-form="test">${input('testName','시험 이름')}${input('testRevision','시험 버전')}${select('observable','관측 지표',observables,s.observable)}${select('reduction','결과 형태',[['trajectory','프레임별 궤적'],['peak_absolute_target_error','구간 최대 목표 오차'],['sampled_settling_time','표본 정착 시간']],s.reduction)}${s.reduction!=='trajectory'?`${input('target','목표값','number')}${select('start','구간 시작 프레임',frames,s.start)}${select('end','구간 끝 프레임',frames,s.end)}${s.reduction==='sampled_settling_time'?input('band','정착 허용 폭','number')+input('dwell','최소 연속 표본 수','number'):''}`:''}<p class="list-note">정착 시간은 지정한 유한 구간의 표본 기준입니다. 연속 시간의 안정성을 보증하지 않습니다. 새 시험의 허용 오차는 무차원 0.01 또는 1 ns이며 구현 검증용으로 생성됩니다.</p><button type="submit" class="button secondary full">관측 시험 새 버전 생성</button></form></details>
      <form data-analysis-form="observe">${select('test','분석할 시험',candidates(capture).map(t=>[t.id,t.name+' · '+t.unit+' · '+t.id.slice(-8)]),s.test)}${input('name','분석 이름')}${input('revision','분석 버전')}${select('parent','이전 분석 · 선택 사항',analyses(capture).map(a=>[a.id,a.name+' · r'+a.revision]),s.parent)}<div class="form-field"><label for="analysis-uncertainty">이 지표의 불확실도 · ${esc(test?.unit||'')} · JSON</label><textarea id="analysis-uncertainty" data-analysis-field="uncertainty" spellcheck="false">${esc(s.uncertainty)}</textarea></div><p class="list-note">다른 지표나 단위의 오차 한계를 복사하지 마세요. unknown은 판정 근거가 부족하다는 뜻입니다.</p><button type="submit" class="button primary full" ${!s.test?'disabled':''}>원본 유지 · 분석 버전 저장</button></form></fieldset>
      <div class="capture-history" data-analysis-history>${legacy(capture).map(m=>`<article class="locked-reference"><div><strong>${esc(m.name)}</strong><small>기존 취득 기록에 보존된 분석 · ${esc(m.unit)}</small></div><button class="link-button" data-analysis-legacy="${esc(m.id)}">이 분석으로 비교</button></article>`).join('')}${analyses(capture).map(a=>`<article class="locked-reference"><div><strong>${esc(a.name)} · r${esc(a.revision)}</strong><small>${esc(a.observation.metric_id)} · ${esc(a.observation.unit)}</small><small>동일 취득 원본의 파생 분석</small></div><button class="link-button" data-analysis-select="${esc(a.id)}">이 분석으로 비교</button></article>${recordButton('capture_analyses',a,'분석 버전·시험·원본 연결 ↗')}`).join('')}</div></div></section>`;
  }
  document.addEventListener('toggle',event=>{if(event.target.matches?.('[data-analysis-authoring]'))s.testOpen=event.target.open;},true);
  document.addEventListener('input',event=>{const field=event.target.dataset?.analysisField;if(field){s[field]=event.target.value;s.epoch++;}});
  document.addEventListener('change',event=>{const field=event.target.dataset?.analysisField;if(!field)return;if(field==='capture')selectCapture(s.capture);if(field==='test')resetMetric();if(['capture','test','reduction','observable'].includes(field))draw();});
  document.addEventListener('click',event=>{const old=event.target.closest('[data-analysis-legacy]');if(old){const measurement=find('measurements',old.dataset.analysisLegacy);if(measurement){chooseCapture({capture:find('captures',measurement.raw_capture_id),test:find('tests',measurement.test_id),measurement});draw();}return;}const button=event.target.closest('[data-analysis-select]');if(!button)return;const analysis=find('capture_analyses',button.dataset.analysisSelect),measurement=find('measurements',analysis?.measurement_id);if(!measurement)return;chooseCapture({capture:find('captures',analysis.capture_id),test:find('tests',analysis.test_id),measurement});draw();toast('선택한 분석 버전의 시험과 Dataset을 비교에 연결했습니다.');});
  document.addEventListener('submit',event=>{
    const form=event.target.closest('[data-analysis-form]');if(!form)return;event.preventDefault();if(s.pending)return;
    busy(form.querySelector('button[type="submit"]'),async()=>{
      const epoch=s.epoch, snapshot={...s},capture=find('captures',s.capture);s.pending=true;
      try {
        if(form.dataset.analysisForm==='test'){
          const options={control_program_id:origin(capture).stimulus.control_program_id,observable:snapshot.observable,reduction:snapshot.reduction};
          if(snapshot.reduction!=='trajectory'){
            if(snapshot.target.trim()===''||!Number.isFinite(Number(snapshot.target)))throw new Error('유한한 목표값을 입력하세요.');
            Object.assign(options,{target:Number(snapshot.target),window_start_frame_id:snapshot.start,window_end_frame_id:snapshot.end});
            if(snapshot.reduction==='sampled_settling_time'){
              if(snapshot.band.trim()===''||!Number.isFinite(Number(snapshot.band))||!Number.isSafeInteger(Number(snapshot.dwell)))throw new Error('정착 허용 폭과 정수 표본 수를 입력하세요.');
              Object.assign(options,{settling_band:Number(snapshot.band),minimum_dwell_frames:Number(snapshot.dwell)});
            }
          }
          const draft=await api('/control_response_tests/template',options);
          const saved=await api('/tests',{...draft,name:snapshot.testName,revision:snapshot.testRevision});
          await refresh({renderPage:false});if(epoch===s.epoch){s.test=saved.id;resetMetric();}toast('취득 조건을 유지한 공통 관측 시험을 등록했습니다.');
        }else{
          if(new TextEncoder().encode(snapshot.uncertainty).length>1000000)throw new Error('불확실도 JSON은 1 MB 이하여야 합니다.');
          const uncertainty=JSON.parse(snapshot.uncertainty,(_k,v)=>{if(typeof v==='number'&&(!Number.isFinite(v)||(Number.isInteger(v)&&!Number.isSafeInteger(v))))throw new Error('정확히 표현 가능한 유한 수를 입력하세요.');return v;});
          const saved=await api('/capture_analyses',{capture_id:capture.id,test_id:snapshot.test,name:snapshot.name,revision:snapshot.revision,uncertainty,parent_analysis_id:snapshot.parent||null});
          await refresh({renderPage:false});if(epoch===s.epoch){chooseCapture(saved);s.parent=saved.analysis.id;}toast('같은 RAW 원본의 새 분석 버전을 저장했습니다.');
        }
      }finally{s.pending=false;draw();}
    });
  });
  return {view,selectCapture};
};

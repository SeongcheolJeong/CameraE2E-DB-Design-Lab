'use strict';

// The study workflow is separate from single camera runs and RAW replay.
window.CameraStudyUI = function(ctx) {
  const {api,esc,records,badge,details,number,render,refresh,toast,busy,state,chooseRun,downloadObject}=ctx;
  const s={catalog:null,name:'노출 · Read noise 설계 검토',question:'노출 시간을 줄이면서 필요한 SNR과 포화 여유를 확보할 수 있는가?',assemblies:[],test:'',design:'one_at_a_time',runtime:'response_db_v1',axes:[{parameter:'exposure_s',values:'0.002, 0.004, 0.008'},{parameter:'read_noise_e',values:'1, 2, 5'}],objectives:[{metric:'exposure_ms',direction:'minimize'},{metric:'min_theoretical_snr_db',direction:'maximize'}],minSNR:'',plan:null,study:'',analysis:null,request:0,loading:false,timer:null,error:''};
  const field=(name,value,extra='')=>`data-study-field="${name}" value="${esc(value)}" ${extra}`;
  const option=(id,label,selected)=>`<option value="${esc(id)}" ${selected?'selected':''}>${esc(label)}</option>`;
  const metricOptions=value=>Object.entries(s.catalog?.metrics || {}).map(([k,v])=>option(k,`${v.label} · ${v.unit}`,k===value)).join('');
  const parameterOptions=value=>Object.entries(s.catalog?.parameters || {}).map(([k,v])=>option(k,`${v.label} · ${v.unit}`,k===value)).join('');
  const defaults={exposure_s:'0.002, 0.004, 0.008',irradiance_scale:'0.5, 1, 1.5',qe_scale:'0.8, 1',read_noise_e:'1, 2, 5',dark_current_e_per_s:'0, 10, 50',full_well_e:'8000, 10000, 12000',gain:'1, 2'};
  function initialize(){
    if(!s.assemblies.length)s.assemblies=[state.data?.demo?.assembly_ids?.[0]].filter(Boolean);
    if(!s.test)s.test=state.data?.demo?.test_id || '';
  }
  async function activate(){
    initialize();
    if(!s.catalog){try{s.catalog=await api('/studies/catalog');s.error='';}catch(e){s.error=e.message;}}
    if(!s.study && records('studies').length)s.study=records('studies')[0].id;
    if(s.study)await loadAnalysis();
    if(state.view==='experiments'&&state.experimentMode==='study')render();
  }
  function schedule(){
    clearTimeout(s.timer);
    if(s.analysis?.status==='running')s.timer=setTimeout(()=>{
      if(state.view==='experiments'&&state.experimentMode==='study')loadAnalysis();
    },1500);
  }
  async function loadAnalysis(){
    const id=s.study,token=++s.request;
    if(!id)return;
    try{
      const result=await api('/studies/'+encodeURIComponent(id)+'/analysis');
      if(token!==s.request||s.study!==id)return;
      s.analysis=result;s.error='';
      if(state.view==='experiments'&&state.experimentMode==='study')render();
      schedule();
    }catch(e){if(token===s.request){s.error=e.message;render();}}
  }
  function specification(){
    const axes=s.axes.map(axis=>{
      const values=axis.values.trim().split(/[\s,]+/).map(x=>Number(x));
      if(!axis.values.trim()||values.some(v=>!Number.isFinite(v)))throw new Error('변수 값은 쉼표로 구분한 유한한 숫자로 입력하세요.');
      return {parameter:axis.parameter,unit:s.catalog.parameters[axis.parameter].unit,values};
    });
    const constraints=[{metric:'min_saturation_margin_e',operator:'>=',value:0,unit:'electron/pixel'}];
    if(s.minSNR.trim()){
      const value=Number(s.minSNR);if(!Number.isFinite(value))throw new Error('최소 SNR을 숫자로 입력하세요.');
      constraints.push({metric:'min_theoretical_snr_db',operator:'>=',value,unit:'dB'});
    }
    return {name:s.name,question:s.question,assembly_ids:s.assemblies,test_id:s.test,plugin_id:s.runtime,design:s.design,axes,objectives:s.objectives,constraints,seed:42,frames:4,output_size:[32,32]};
  }
  function planPanel(){
    if(!s.plan)return '';
    return `<section class="panel" data-study-plan="${esc(s.plan.plan_hash)}"><div class="panel-header"><div><h2>검사된 실행 계획</h2><p>${s.plan.run_count}개 계산 · ${s.plan.new_revision_count}개 새 버전 · 기준점 포함</p></div>${badge('draft','검토한 조건')}</div><div class="panel-content"><p>모든 후보가 현재 모델의 적용 범위와 Runtime 검사를 통과했습니다. 변경한 센서 특성은 추정한 설계 버전으로 남습니다.</p><div class="table-wrap"><table><thead><tr><th>후보</th><th>변경 조건</th></tr></thead><tbody>${s.plan.candidates.map(c=>`<tr><td>${c.index+1}${c.is_baseline?' · 기준':''}</td><td>${coordinates(c)}</td></tr>`).join('')}</tbody></table></div>${details('원본 모델·시험과 계획 근거',s.plan)}<div class="inline-spacer"></div><button class="button primary full" data-study-action="submit">검사한 ${s.plan.run_count}개 계산 제출 →</button></div></section>`;
  }
  function coordinates(row){return Object.entries(row.coordinates).map(([key,value])=>`${esc(s.catalog?.parameters[key]?.label || key)} ${number(value,6)} ${esc(s.catalog?.parameters[key]?.unit || '')}`).join('<br>');}
  function chart(analysis){
    const [x,y]=analysis.specification.objectives;
    if(!x||!y||x.metric===y.metric)return '';
    const points=analysis.rows.filter(r=>Number.isFinite(r.metrics[x.metric])&&Number.isFinite(r.metrics[y.metric]));
    if(!points.length)return '<p class="muted">두 목적값을 계산한 후보가 생기면 상충 관계를 표시합니다.</p>';
    const range=key=>{const a=points.map(r=>r.metrics[key]);let min=Math.min(...a),max=Math.max(...a);const pad=(max-min||Math.max(Math.abs(min),1))*.12;return [min-pad,max+pad];};
    const [xmin,xmax]=range(x.metric),[ymin,ymax]=range(y.metric);
    const px=v=>70+(v-xmin)/(xmax-xmin)*470,py=v=>250-(v-ymin)/(ymax-ymin)*200;
    const labels=[0,.25,.5,.75,1].map(t=>{
      const xv=xmin+t*(xmax-xmin),yv=ymin+t*(ymax-ymin);
      return `<path d="M${px(xv)} 50V250 M70 ${py(yv)}H540" stroke="#e4e8e5"/><text x="${px(xv)}" y="271" text-anchor="middle">${number(xv,2)}</text><text x="60" y="${py(yv)+4}" text-anchor="end">${number(yv,2)}</text>`;
    }).join('');
    const occupied=[];
    const markers=points.map(r=>{
      const x=px(r.metrics[analysis.specification.objectives[0].metric]),y=py(r.metrics[analysis.specification.objectives[1].metric]);
      let label={x:x+9,y:y-9};
      for(const shift of [-10,-26,-42,16,32,48]){
        const candidate={x:Math.min(538,x+9),y:Math.max(44,Math.min(250,y+shift))};
        if(!occupied.some(p=>Math.abs(p.x-candidate.x)<22&&Math.abs(p.y-candidate.y)<14)){label=candidate;break;}
      }
      occupied.push(label);
      return `<g><path d="M${x} ${y}L${label.x} ${label.y-3}" stroke="#bbc6bf" stroke-width=".7"/><circle cx="${x}" cy="${y}" r="6" fill="${r.feasibility!=='feasible'?'#bc7242':r.pareto_status==='non_dominated'?'#2b6651':'#a5b2aa'}"><title>후보 ${r.index+1}</title></circle><text x="${label.x}" y="${label.y}">${r.index+1}</text></g>`;
    }).join('');
    const name=o=>`${analysis.metric_definitions[o.metric].label} · ${analysis.metric_definitions[o.metric].unit} (${o.direction==='minimize'?'작을수록':'클수록'})`;
    return `<div class="study-chart"><svg viewBox="0 0 590 330" role="img" aria-label="${esc(name(x)+'와 '+name(y)+'의 후보 비교')}" style="width:100%;font-size:11px;fill:#536057"><rect x="0" y="0" width="590" height="330" fill="#fff"/>${labels}${markers}<text x="300" y="306" text-anchor="middle">${esc(name(x))}</text><text x="72" y="26">${esc(name(y))}</text></svg><p class="muted">녹색: 현재 샘플에서 비지배 · 회색: 다른 후보가 우세 · 주황색: 제약 미충족/미정. 실물 검증이나 전체 공간의 최적해를 뜻하지 않습니다.</p></div>`;
  }
  function results(){
    const a=s.analysis;
    if(!a)return `<section class="panel"><div class="panel-content"><h2>설계 질문에서 비교 근거까지</h2><p>변수와 목표를 선택하고 계획을 검사하세요. 제출한 계산은 원본 모델·시험·새 버전과 함께 보존됩니다.</p></div></section>`;
    const metric=k=>a.metric_definitions[k].label;
    const feasibility={feasible:'수치 제약 충족',infeasible:'제약 미충족',unknown:'판정 미정'};
    const pareto={non_dominated:'샘플 내 비지배',dominated:'다른 후보 우세',excluded:'비교 제외'};
    return `<section class="panel" data-study-analysis="${esc(a.study_id)}"><div class="panel-header"><div><h2>후보별 조건과 상충 관계</h2><p>${a.coverage.succeeded}개 완료 · ${a.coverage.failed}개 실패 · ${a.coverage.cancelled}개 취소 · ${a.coverage.queued+a.coverage.running}개 진행</p></div>${badge('unknown','실물 정확도 미확립')}</div><div class="panel-content"><p>${esc(a.specification.question)}</p><p class="list-note study-applied-constraints">적용된 제약: ${a.specification.constraints.length?a.specification.constraints.map(c=>`${esc(a.metric_definitions[c.metric].label)} ${esc(c.operator)} ${number(c.value,6)} ${esc(c.unit)}`).join(' · '):'별도 수치 제약 없음'}</p>${chart(a)}<div class="table-wrap"><table class="study-results"><thead><tr><th>후보 · 실제 계산</th><th>변수</th><th>최저 이론 SNR · dB</th><th>최소 포화 여유 · e−</th><th>상대 색 변화 · 1</th><th>제약 · 상충 관계</th></tr></thead><tbody>${a.rows.map(r=>`<tr data-study-row="${r.index}"><td><button class="link-button" data-study-action="run" data-id="${esc(r.run_id)}">${r.index+1}${r.is_baseline?' · 기준':''} ↗</button><small>${esc(r.run_status)}</small><small>${esc(records('assemblies').find(a=>a.id===r.base_assembly_id)?.name || r.base_assembly_id)}</small></td><td>${coordinates(r)}</td><td data-study-metric="min_theoretical_snr_db">${number(r.metrics.min_theoretical_snr_db,5)}</td><td data-study-metric="min_saturation_margin_e">${number(r.metrics.min_saturation_margin_e,3)}</td><td data-study-metric="max_relative_color_shading">${number(r.metrics.max_relative_color_shading,5)}</td><td>${esc(feasibility[r.feasibility])}<small>${esc(pareto[r.pareto_status])}</small>${r.error?details('실행 실패 원인',r.error):''}</td></tr>`).join('')}</tbody></table></div><div class="inline-spacer"></div><h3>변수별 구간 변화율</h3><p class="muted">다른 변수를 기준값에 고정한 두 계산 사이의 변화율입니다. 미분 수렴 오차와 전체 불확실도는 미확인입니다.</p>${a.sensitivities.length?`<div class="table-wrap"><table class="study-sensitivity"><thead><tr><th>변수 · 구간</th>${a.specification.objectives.map(o=>`<th>${esc(metric(o.metric))} 변화율</th>`).join('')}</tr></thead><tbody>${a.sensitivities.map(r=>`<tr><td>${esc(s.catalog?.parameters[r.parameter]?.label || r.parameter)}<small>${number(r.lower,6)} → ${number(r.upper,6)}</small></td>${a.specification.objectives.map(o=>`<td>${number(r.metrics[o.metric].slope,6)}<small>${esc(r.metrics[o.metric].unit)}</small></td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p>다른 변수가 같은 비교점이 아직 없습니다.</p>'}${details('수치 제약·원본 Run·미확인 항목',a)}<div class="inline-spacer"></div><button class="button secondary" data-study-action="export">현재 비교 근거 JSON ↓</button></div></section>`;
  }
  function view(){
    initialize();
    if(!s.catalog)return `<section class="panel"><div class="panel-content"><p>${esc(s.error || '실험 변수와 Metric 계약을 불러오고 있습니다.')}</p><button class="button secondary" data-study-action="load">다시 불러오기</button></div></section>`;
    return `<div class="content-grid"><div class="stack">${results()}<section class="panel"><div class="panel-header"><h2>보존된 설계 실험</h2></div><div class="panel-content">${records('studies').map(r=>`<div class="locked-reference study-history"><div><strong>${esc(r.name)}</strong><small>${esc(r.question)}</small></div><button class="button secondary small" data-study-action="select" data-id="${esc(r.id)}">결과 보기</button></div>`).join('')||'<p class="muted">제출한 실험이 이곳에 남습니다.</p>'}</div></section></div><div class="stack"><section class="panel"><div class="panel-header"><div><span class="eyebrow">PARAMETER STUDY</span><h2>새 실험 계획</h2><p>아래 입력은 새로 제출할 실험의 조건입니다.</p></div></div><div class="panel-content study-form">
      <div class="form-field"><label for="study-name">실험 이름</label><input id="study-name" ${field('name',s.name)} maxlength="160"></div>
      <div class="form-field"><label for="study-question">해결할 설계 질문</label><textarea id="study-question" data-study-field="question" maxlength="2000" rows="3">${esc(s.question)}</textarea></div>
      <div class="form-field"><label for="study-assemblies">기준 구성 · 여러 개 선택 가능</label><select id="study-assemblies" data-study-field="assemblies" multiple size="3">${records('assemblies').map(a=>option(a.id,`${a.name} · r${a.revision}`,s.assemblies.includes(a.id))).join('')}</select></div>
      <div class="form-field"><label for="study-test">기준 시험</label><select id="study-test" data-study-field="test">${records('tests').filter(t=>!t.observation_method && t.metric_id==='raw.channel_ratio').map(t=>option(t.id,t.name,t.id===s.test)).join('')}</select></div>
      <div class="form-field"><label for="study-runtime">계산 방식</label><select id="study-runtime" data-study-field="runtime">${option('response_db_v1','평탄장 · 응답 DB',s.runtime==='response_db_v1')}${option('quick_estimate_v1','빠른 광량 추정 · 해당 시험 필요',s.runtime==='quick_estimate_v1')}</select></div>
      <div class="form-field"><label for="study-design">변수 조합 방식</label><select id="study-design" data-study-field="design">${option('one_at_a_time','한 번에 한 변수 · 기준점 포함',s.design==='one_at_a_time')}${option('cartesian','입력 값의 모든 조합 · 기준점 포함',s.design==='cartesian')}</select></div>
      ${s.axes.map((a,i)=>`<div class="study-axis"><div class="form-field"><label for="study-axis-${i}">설계 변수 ${i+1}</label><select id="study-axis-${i}" data-study-field="axis" data-index="${i}">${parameterOptions(a.parameter)}</select></div><div class="form-field"><label for="study-values-${i}">값 · ${esc(s.catalog.parameters[a.parameter].unit)}</label><input id="study-values-${i}" ${field('values',a.values,`data-index="${i}"`)}></div><button class="button quiet" data-study-action="remove" data-index="${i}" ${s.axes.length===1?'disabled':''}>변수 삭제</button></div>`).join('')}
      <button class="button secondary full" data-study-action="add" ${s.axes.length>=6?'disabled':''}>설계 변수 추가</button><div class="inline-spacer"></div>
      ${s.objectives.map((o,i)=>`<div class="form-field"><label for="study-objective-${i}">비교 목표 ${i+1}</label><select id="study-objective-${i}" data-study-field="objective" data-index="${i}">${metricOptions(o.metric)}</select><select aria-label="목표 ${i+1} 방향" data-study-field="direction" data-index="${i}">${option('minimize','작을수록 좋음',o.direction==='minimize')}${option('maximize','클수록 좋음',o.direction==='maximize')}</select></div>`).join('')}
      <div class="form-field"><label for="study-snr">최저 이론 SNR 제약 · dB · 선택</label><input id="study-snr" ${field('minSNR',s.minSNR)} placeholder="예: 25"></div><p class="list-note">최소 포화 여유 ≥ 0을 함께 검사합니다. 최대 24개 계산, 고정 Seed 42, 후보당 4개 RAW 프레임. 선언된 범위를 벗어나는 Gain과 QE는 거부됩니다.</p>
      ${s.error?`<div class="callout" role="alert">${esc(s.error)}</div>`:''}<button class="button primary full" data-study-action="preview">전체 계획 검사 →</button></div></section>${planPanel()}</div></div>`;
  }
  document.addEventListener('input',event=>{
    const input=event.target.closest('[data-study-field]');if(!input)return;
    const key=input.dataset.studyField,index=Number(input.dataset.index);
    if(key==='assemblies')s.assemblies=Array.from(input.selectedOptions).map(o=>o.value);
    else if(key==='axis'){s.axes[index]={parameter:input.value,values:defaults[input.value]};}
    else if(key==='values')s.axes[index].values=input.value;
    else if(key==='objective')s.objectives[index].metric=input.value;
    else if(key==='direction')s.objectives[index].direction=input.value;
    else s[key]=input.value;
    s.plan=null;s.error='';
    // Clear stale submission controls immediately without replacing the focused input.
    document.querySelector('[data-study-plan]')?.remove();
    if(key==='axis')render();
  });
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-study-action]');if(!button)return;
    busy(button,async()=>{
      const action=button.dataset.studyAction;
      if(action==='load'){await activate();return;}
      if(action==='add'){const next=Object.keys(s.catalog.parameters).find(k=>!s.axes.some(a=>a.parameter===k));if(next)s.axes.push({parameter:next,values:defaults[next]});s.plan=null;render();return;}
      if(action==='remove'){s.axes.splice(Number(button.dataset.index),1);s.plan=null;render();return;}
      if(action==='preview'){
        const spec=specification(),snapshot=JSON.stringify(spec);
        try{const plan=await api('/studies/plan',spec);if(snapshot!==JSON.stringify(specification())){toast('조건이 변경되어 이전 계획을 적용하지 않았습니다.');return;}s.plan=plan;s.error='';render();}
        catch(e){s.error=e.message;render();throw e;}return;
      }
      if(action==='submit'){
        const plan=s.plan;if(!plan)return;
        const study=await api('/studies',{specification:plan.specification,plan_hash:plan.plan_hash});
        s.study=study.id;s.plan=null;s.analysis=null;await refresh({renderPage:false});await loadAnalysis();toast(`${study.run_count}개 계산을 제출했습니다.`);return;
      }
      if(action==='select'){s.study=button.dataset.id;s.analysis=null;await loadAnalysis();return;}
      if(action==='run'){await refresh({renderPage:false});chooseRun(button.dataset.id);state.experimentMode='camera';render();return;}
      if(action==='export'){if(s.analysis)downloadObject(s.analysis,'camera-study-'+s.analysis.study_id+'.json');}
    });
  });
  return {activate,view};
};

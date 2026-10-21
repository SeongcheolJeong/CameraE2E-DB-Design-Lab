'use strict';

// Independent module: reference selection -> frozen plan -> training -> metric inference.
window.CameraSurrogateUI=function(ctx){
  const {api,esc,records,details,number,render,refresh,toast,busy,state,downloadObject}=ctx;
  const s={catalog:null,name:'노출 · 노이즈 응답 근사',purpose:'검증한 수치 모델 범위 안에서 반복 설계 계산을 줄입니다.',metric:'min_theoretical_snr_db',tolerance:'0.3',sources:{training:{id:'',analysis:null,selected:[]},validation:{id:'',analysis:null,selected:[]}},plan:null,model:null,prediction:null,values:{},error:'',timer:null,sequence:0};
  const active=()=>state.view==='experiments'&&state.experimentMode==='surrogate';
  const show=()=>{if(active())render();};
  const button=(action,label,extra='')=>'<button class="button secondary" data-surrogate-action="'+action+'" '+extra+'>'+label+'</button>';
  const field=(key,value,extra='')=>'data-surrogate-field="'+key+'" value="'+esc(value)+'" '+extra;
  const option=(id,label,value)=>'<option value="'+esc(id)+'" '+(id===value?'selected':'')+'>'+esc(label)+'</option>';
  const key=row=>JSON.stringify(Object.keys(row.coordinates).sort().map(k=>[k,row.coordinates[k]]));
  const trainingKeys=()=>new Set((s.sources.training.analysis?.rows||[]).filter(r=>s.sources.training.selected.includes(r.run_id)).map(key));
  const overlap=row=>trainingKeys().has(key(row));
  const invalidate=()=>{s.plan=null;s.error='';document.querySelector('[data-surrogate-plan]')?.remove();};
  function reconcile(){
    const validation=s.sources.validation;
    validation.selected=validation.selected.filter(id=>!overlap(validation.analysis.rows.find(r=>r.run_id===id)));
  }
  async function activate(){
    try{s.catalog=s.catalog||await api('/surrogates/catalog');s.error='';if(!s.model&&records('surrogates').length)await selectModel(records('surrogates')[0].id);show();}
    catch(e){s.error=e.message;show();}
  }
  async function loadSource(split,id){
    const source=s.sources[split];source.id=id;source.analysis=null;source.selected=[];invalidate();show();
    if(!id)return;
    const analysis=await api('/studies/'+id+'/analysis');
    if(source.id!==id)return;
    source.analysis=analysis;
    source.selected=analysis.rows.filter(r=>r.run_status==='succeeded'&&(split!=='validation'||!overlap(r))).map(r=>r.run_id);
    if(s.sources.validation.analysis)reconcile();
    show();
  }
  function specification(){
    return {name:s.name,purpose:s.purpose,training:[{study_id:s.sources.training.id,run_ids:s.sources.training.selected}],validation:[{study_id:s.sources.validation.id,run_ids:s.sources.validation.selected}],metrics:[{metric:s.metric,unit:s.catalog.metrics[s.metric].unit,max_absolute_error:Number(s.tolerance)}]};
  }
  async function selectModel(id){
    const sequence=++s.sequence;clearTimeout(s.timer);
    const previousStatus=s.model?.id===id?s.model.status:null;
    const job=await api('/surrogates/'+id);
    if(sequence!==s.sequence)return;
    if(s.model?.id!==id){s.prediction=null;s.values={};}
    s.model=job;
    if(previousStatus&&previousStatus!==job.status&&['succeeded','failed','cancelled'].includes(job.status))await refresh({renderPage:false});
    if(job.result?.coefficients){
      job.result.axes.forEach((a,i)=>{
        if(s.values[a.parameter]===undefined)s.values[a.parameter]=String((job.result.coefficients.domain.lower[i]+job.result.coefficients.domain.upper[i])/2);
      });
    }
    show();
    if(['queued','running'].includes(job.status))s.timer=setTimeout(()=>{if(active())selectModel(id).catch(e=>{s.error=e.message;show();});},1500);
  }
  function sourcePanel(split,label){
    const source=s.sources[split],rows=source.analysis?.rows||[];
    return '<section class="surrogate-source"><h3>'+label+'</h3><label class="form-field">설계 실험<select data-surrogate-field="source" data-split="'+split+'">'+option('','실험 선택',source.id)+records('studies').map(r=>option(r.id,r.name,source.id)).join('')+'</select></label>'+
      (source.id&&!source.analysis?'<p>실제 Run을 불러오고 있습니다.</p>':'')+
      (source.analysis?'<p class="list-note">'+source.selected.length+'개 선택 / '+rows.length+'개 Run · '+esc(source.analysis.specification.plugin_id)+'</p><div class="surrogate-source-list">'+rows.map(r=>{
        const duplicate=split==='validation'&&overlap(r),disabled=r.run_status!=='succeeded'||duplicate;
        return '<label class="surrogate-row"><input type="checkbox" data-surrogate-field="row" data-split="'+split+'" data-id="'+esc(r.run_id)+'" '+(source.selected.includes(r.run_id)?'checked':'')+' '+(disabled?'disabled':'')+'><span>'+Object.entries(r.coordinates).map(([k,v])=>esc(s.catalog.parameters[k]?.label||k)+' '+number(v,6)).join(' · ')+'<small>'+esc(duplicate?'학습과 같은 조건 · 검증에서 제외':r.run_status==='succeeded'?'완료된 기준 계산':r.run_status)+'</small></span></label>';
      }).join('')+'</div>':'')+'</section>';
  }
  function planPanel(){
    const p=s.plan;if(!p)return '';
    return '<section class="panel" data-surrogate-plan><div class="panel-header"><h2>제출할 학습 계획</h2></div><div class="panel-content"><p>학습 '+p.datasets.training.length+'개 · 별도 조건 검증 '+p.datasets.validation.length+'개</p><p>고정 허용 오차: '+number(p.specification.metrics[0].max_absolute_error,8)+' '+esc(p.specification.metrics[0].unit)+'</p>'+domainTable(p.context.axes,p.domain)+details('동결할 원본·입력·계산 환경',p)+button('submit','이 계획으로 학습 · 수치 검증')+'</div></section>';
  }
  function domainTable(axes,domain){
    return '<div class="table-wrap"><table><thead><tr><th>입력 변수</th><th>허용 범위</th><th>검증한 조건 범위</th></tr></thead><tbody>'+axes.map((a,i)=>'<tr><td>'+esc(s.catalog.parameters[a.parameter]?.label||a.parameter)+'<small>'+esc(a.unit)+'</small></td><td>'+number(domain.lower[i],8)+' – '+number(domain.upper[i],8)+'</td><td>'+number(domain.validation_lower[i],8)+' – '+number(domain.validation_upper[i],8)+'</td></tr>').join('')+'</tbody></table></div><p class="list-note">학습 범위의 모든 모서리 조건을 포함합니다. 범위 전체의 연속 오차 상한은 아직 알 수 없습니다.</p>';
  }
  function chart(model){
    const rows=model.numerical_validation.rows,vals=rows.flatMap(r=>[r.outputs[0],r.predicted[0]]);
    let lo=Math.min(...vals),hi=Math.max(...vals);const pad=Math.max((hi-lo)*.1,Math.abs(hi)*.001,1e-9);lo-=pad;hi+=pad;
    const x=v=>60+(v-lo)/(hi-lo)*460,y=v=>250-(v-lo)/(hi-lo)*210;
    return '<svg class="surrogate-chart" viewBox="0 0 580 310" role="img" aria-label="별도 조건의 기준 계산과 Surrogate 예측 비교"><rect x="60" y="40" width="460" height="210" fill="#f7f9f3"/><line x1="60" y1="250" x2="520" y2="40" stroke="#acb4a1" stroke-dasharray="5 5"/>'+
      rows.map((r,i)=>'<circle cx="'+x(r.outputs[0])+'" cy="'+y(r.predicted[0])+'" r="4" fill="#728c58"><title>검증 '+(i+1)+' · 기준 '+r.outputs[0]+' · 예측 '+r.predicted[0]+'</title></circle>').join('')+
      '<text x="60" y="270">'+number(lo,4)+'</text><text x="520" y="270" text-anchor="end">'+number(hi,4)+'</text><text x="290" y="299" text-anchor="middle">기준 계산 · '+esc(model.metrics[0].unit)+'</text><text x="60" y="24">예측 · '+esc(model.metrics[0].unit)+' · 점선 = 동일 값</text></svg>';
  }
  function modelPanel(){
    const job=s.model;if(!job)return '<section class="panel"><div class="panel-content"><h2>계산 결과를 재사용하는 예측 모델</h2><p>같은 카메라·시험의 완료된 설계 실험에서 학습과 검증 조건을 선택하세요. 이 화면은 영상 생성 없이 수치 지표를 예측합니다.</p><p class="list-note">2차원 이상에서는 모든 모서리 조합이 있는 학습 데이터가 필요합니다. 설계 변수 탐색의 모든 조합 방식으로 만들 수 있습니다.</p></div></section>';
    if(!job.result)return '<section class="panel"><div class="panel-content"><h2>'+esc(job.name)+'</h2><p>'+esc(job.status)+'</p>'+ (job.error?'<p role="alert">'+esc(job.error)+'</p>':'<p>별도 Worker가 학습과 검증을 수행합니다.</p>')+'</div></section>';
    const m=job.result;
    return '<section class="panel" data-surrogate-model="'+esc(job.id)+'"><div class="panel-header"><div><span class="eyebrow">SURROGATE PREDICTION</span><h2>'+esc(job.name)+'</h2><p>'+ (m.prediction_eligible?'기준 계산 비교 통과 · 범위 내 예측 가능':'기준 계산 비교 실패 · 예측 사용 불가')+'</p></div></div><div class="panel-content"><p class="surrogate-boundary">실물 정확도 미확립 · 전체 불확실도 미확인 · 승인 전 모델 후보</p><p>RBF 근사 · 학습 '+m.datasets.training.length+'개 · 별도 조건 검증 '+m.datasets.validation.length+'개</p>'+chart(m)+
      '<div class="table-wrap"><table><thead><tr><th>출력 지표</th><th>허용 오차</th><th>관측 최대 오차</th><th>RMSE</th><th>수치 비교</th></tr></thead><tbody>'+m.numerical_validation.checks.map(c=>'<tr><td>'+esc(s.catalog.metrics[c.metric].label)+'<small>'+esc(c.unit)+'</small></td><td>'+number(c.max_absolute_error,8)+'</td><td data-surrogate-error="'+esc(c.metric)+'">'+number(c.max_observed_absolute_error,8)+'</td><td>'+number(c.rmse,8)+'</td><td>'+esc(c.status)+'</td></tr>').join('')+'</tbody></table></div>'+
      '<p class="list-note">검증에 관측된 오차이며 모든 입력이나 실제 카메라의 오차 보장이 아닙니다. 검증 자료를 재사용하며 조정했다면 새 조건의 검증이 필요합니다.</p>'+domainTable(m.axes,m.coefficients.domain)+
      details('검증 조건별 예측·잔차·원본 Run',m.numerical_validation)+details('학습된 계수·출처·고정 카메라',m)+button('export-model','모델 · 검증 근거 JSON ↓')+'</div></section>';
  }
  function predictionPanel(){
    const m=s.model?.result;if(!m?.prediction_eligible)return '';
    const p=s.prediction;
    return '<section class="panel"><div class="panel-header"><h2>범위 내 일괄 예측</h2></div><div class="panel-content"><p>각 변수에 쉼표로 값을 입력하면 모든 조합을 예측합니다. 최대 4,096개 조건입니다.</p>'+
      m.axes.map(a=>'<div class="form-field"><label for="surrogate-value-'+esc(a.parameter)+'">'+esc(s.catalog.parameters[a.parameter].label)+' · '+esc(a.unit)+'</label><input id="surrogate-value-'+esc(a.parameter)+'" '+field('value',s.values[a.parameter]||'','data-parameter="'+esc(a.parameter)+'"')+'></div>').join('')+
      button('predict','입력 조건 예측 · 근거 저장')+(p?'<div data-surrogate-prediction="'+esc(p.id)+'"><p>'+p.rows.length+'개 조건 예측 완료 · 근거 저장됨</p><div class="table-wrap"><table><thead><tr><th>입력 조건</th>'+m.metrics.map(c=>'<th>'+esc(s.catalog.metrics[c.metric].label)+' · '+esc(c.unit)+'</th>').join('')+'</tr></thead><tbody>'+p.rows.slice(0,100).map(r=>'<tr><td>'+Object.entries(r.coordinates).map(([k,v])=>esc(s.catalog.parameters[k].label)+' '+number(v,8)).join('<br>')+'</td>'+m.metrics.map(c=>'<td data-surrogate-output="'+esc(c.metric)+'">'+number(r.outputs[c.metric],8)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div><p class="list-note">화면은 최대 100개 조건을 표시합니다. 모든 예측과 원본 참조는 JSON에 보존됩니다.</p>'+button('export-prediction','전체 예측 근거 JSON ↓')+'</div>':'')+'</div></section>';
  }
  function view(){
    if(!s.catalog)return '<section class="panel"><div class="panel-content"><p>'+esc(s.error||'Surrogate 계약을 불러오고 있습니다.')+'</p>'+button('load','불러오기')+'</div></section>';
    const used=new Set(records('surrogates').flatMap(j=>j.result?.datasets?.validation?.map(r=>r.run_id)||j.display_summary?.validation_run_ids||[]));
    const reused=s.sources.validation.selected.filter(id=>used.has(id)).length;
    return (s.error?'<div class="surrogate-error" role="alert">'+esc(s.error)+'</div>':'')+'<div class="content-grid"><div class="stack">'+modelPanel()+predictionPanel()+
      '<section class="panel"><div class="panel-header"><h2>보존된 예측 모델</h2></div><div class="panel-content">'+records('surrogates').map(j=>'<div class="locked-reference study-history"><div><strong>'+esc(j.name)+'</strong><small>'+esc((j.result?.numerical_validation?.status||j.display_summary?.numerical_status)?'수치 비교 '+(j.result?.numerical_validation?.status||j.display_summary?.numerical_status):j.status)+' · '+esc(j.created_at)+'</small></div>'+button('select','모델 보기','data-id="'+esc(j.id)+'"')+'</div>').join('')+
      '<h3>보존된 예측</h3>'+records('surrogate_predictions').map(p=>'<div class="locked-reference study-history"><span>'+(p.rows?.length??p.display_summary?.point_count)+'개 조건 · '+esc(p.created_at)+'</span>'+button('saved-prediction','예측 보기','data-id="'+esc(p.id)+'"')+'</div>').join('')+'</div></section></div>'+
      '<div class="stack"><section class="panel"><div class="panel-header"><h2>새 모델 학습 계획</h2></div><div class="panel-content"><div class="form-field"><label for="surrogate-name">모델 이름</label><input id="surrogate-name" '+field('name',s.name)+' maxlength="160"></div><div class="form-field"><label for="surrogate-purpose">사용 목적</label><textarea id="surrogate-purpose" data-surrogate-field="purpose" rows="3" maxlength="2000">'+esc(s.purpose)+'</textarea></div>'+
      sourcePanel('training','1. 학습에 사용할 계산')+sourcePanel('validation','2. 별도 조건에서 검증')+(reused?'<p class="surrogate-boundary">선택한 검증 Run '+reused+'개가 이전 모델 검증에 쓰였습니다. 반복 조정 뒤에는 새 조건으로 확인하세요.</p>':'')+
      '<div class="form-field"><label for="surrogate-metric">예측할 지표</label><select id="surrogate-metric" data-surrogate-field="metric">'+Object.entries(s.catalog.metrics).map(([id,m])=>option(id,m.label+' · '+m.unit,s.metric)).join('')+'</select></div><div class="form-field"><label for="surrogate-tolerance">사전에 고정할 최대 절대 오차 · '+esc(s.catalog.metrics[s.metric].unit)+'</label><input id="surrogate-tolerance" '+field('tolerance',s.tolerance)+'></div><p class="list-note">학습·검증 조건은 중복할 수 없습니다. 같은 카메라 구성·시험·계산 환경의 완료된 Run만 연결됩니다.</p>'+button('preview','학습 계획 검사')+'</div></section>'+planPanel()+'</div></div>';
  }
  document.addEventListener('input',event=>{
    const el=event.target.closest('[data-surrogate-field]');if(!el)return;
    const key=el.dataset.surrogateField;
    if(['source','row'].includes(key))return;
    if(key==='value'){s.values[el.dataset.parameter]=el.value;return;}
    s[key]=el.value;invalidate();if(key==='metric')show();
  });
  document.addEventListener('change',event=>{
    const el=event.target.closest('[data-surrogate-field]');if(!el)return;
    if(el.dataset.surrogateField==='source'){loadSource(el.dataset.split,el.value).catch(e=>{s.error=e.message;show();});}
    if(el.dataset.surrogateField==='row'){
      const source=s.sources[el.dataset.split],id=el.dataset.id;
      source.selected=el.checked?[...source.selected,id]:source.selected.filter(r=>r!==id);
      if(s.sources.validation.analysis)reconcile();invalidate();show();
    }
  });
  document.addEventListener('click',event=>{
    const el=event.target.closest('[data-surrogate-action]');if(!el)return;
    busy(el,async()=>{try{
      const action=el.dataset.surrogateAction;
      if(action==='load')return await activate();
      if(action==='preview'){
        const spec=specification(),snapshot=JSON.stringify(spec),plan=await api('/surrogates/plan',spec);
        if(snapshot!==JSON.stringify(specification()))return toast('입력이 변경되어 이전 계획을 적용하지 않았습니다.');
        s.plan=plan;s.error='';show();
      }
      if(action==='submit'){
        if(!s.plan)return;const p=s.plan,job=await api('/surrogates',{specification:p.specification,plan_hash:p.plan_hash});
        s.plan=null;await refresh({renderPage:false});await selectModel(job.id);toast('학습과 별도 조건 검증을 제출했습니다.');
      }
      if(action==='select')await selectModel(el.dataset.id);
      if(action==='predict'){
        const m=s.model.result;let points=[{}];
        for(const a of m.axes){
          const tokens=s.values[a.parameter].split(',').map(v=>v.trim());
          if(tokens.some(v=>!v||!Number.isFinite(Number(v))))throw Error('변수 값을 유한한 숫자로 입력하세요.');
          const values=tokens.map(Number);if(points.length*values.length>4096)throw Error('최대 4,096개 조건까지 한 번에 예측할 수 있습니다.');
          points=points.flatMap(p=>values.map(v=>({...p,[a.parameter]:v})));
        }
        const identity=s.model.id;
        const prediction=await api('/surrogates/'+identity+'/predict',{points,units:Object.fromEntries(m.axes.map(a=>[a.parameter,a.unit]))});
        if(s.model.id===identity)s.prediction=prediction;
        s.error='';await refresh({renderPage:false});show();toast(prediction.rows.length+'개 조건의 예측 근거를 저장했습니다.');
      }
      if(action==='saved-prediction'){
        const p=await api('/surrogate_predictions/'+el.dataset.id);await selectModel(p.model_id);s.prediction=p;show();
      }
      if(action==='export-model')downloadObject(s.model,'camera-surrogate-'+s.model.id+'.json');
      if(action==='export-prediction')downloadObject(s.prediction,'camera-prediction-'+s.prediction.id+'.json');
    }catch(e){s.error=e.message;show();throw e;}});
  });
  return {activate,view};
};

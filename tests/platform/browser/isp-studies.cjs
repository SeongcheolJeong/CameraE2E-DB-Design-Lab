'use strict';

// Actual UI state module, controlled API responses. Not browser or numeric proof.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const copy=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};};
const source=(id='A')=>({source_kind:'run',source_id:id,name:'RAW <'+id+'>',raw_sha256:id.repeat(64),raw_shape:[4,32,32],geometry:{kind:'native_sensor'},raw_format:{hdr_streams:1},provenance:{source_kind:'simulated'}});
function specification(id='A') {
  return {schema:'camera_isp_study_v1',name:'Study '+id,question:'어떤 강도에서 반복 잡음과 세부 표현을 유지하는가?',phase:'tuning',calibration_id:'CAL',baseline_isp_model_id:'ISP',selection_id:null,parent_study_id:null,attempt:1,
    axes:[{parameter:'raw_denoise.strength',values:[0,.5]},{parameter:'linear_sharpen.amount',values:[0,.25]}],
    datasets:[{key:'flat',source_kind:'run',source_id:id,purpose:'tuning',acquisition_group_id:'acquisition-'+id,stationarity:{kind:'declared_static',source_kind:'simulated',description:'fixed optics and scene'},metric_spec:{kind:'flat',stationary:true,roi:{row_start:8,row_end:24,column_start:8,column_end:24},texture:null,edge:null},reference_id:null}],
    objectives:[{metric:'temporal_residual_std_dn',direction:'minimize',dataset_keys:['flat'],reducer:'max'},{metric:'mean_abs_bias_dn',direction:'minimize',dataset_keys:['flat'],reducer:'max'}],constraints:[{metric:'mean_abs_bias_dn',operator:'<=',value:2,unit:'DN_equivalent',dataset_keys:['flat'],reducer:'max'}]};
}
function plan(spec=specification()) {
  return {schema:'camera_isp_study_plan_v1',specification:copy(spec),plan_hash:'f'.repeat(64),job_count:2,resources:{estimated_output_bytes:1048576,estimated_working_bytes_max:2097152},
    candidates:[{candidate_id:'baseline',is_baseline:true,profile_sha256:'b'.repeat(64),coordinates:{'raw_denoise.strength':.5}},{candidate_id:'candidate_1',is_baseline:false,profile_sha256:'c'.repeat(64),coordinates:{'raw_denoise.strength':0}}],
    datasets:spec.datasets.map(d=>({key:d.key,specification:copy(d),raw_sha256:source(d.source_id).raw_sha256,assembly_id:'assembly',test_id:'test'}))};
}
const metric=(value,reason=null)=>({value,unit:'DN_equivalent',status:value===null?'unknown':'ok',reason});
function result(id='STUDY',raw='A') {
  const record={...plan(specification(raw)),id,name:'saved <'+id+'>',cells:[]};
  const rows=record.candidates.map(c=>({candidate_id:c.candidate_id,dataset_key:'flat',status:'succeeded',metrics:{temporal_residual_std_dn:metric(2),mean_abs_bias_dn:metric(null,'No independent reference <truth>')},metric_result:{coverage:{frame_count:4,complete:true}},artifacts:[{name:'sharpened_rgb_frames.npy',url:'/api/v1/artifacts/arr',sha256:'abc'}],summary:{raw_sha256:source(raw).raw_sha256,stage_previews:[0,1].flatMap(frame_index=>['baseline_rgb_frames','rgb_frames'].map(array=>({array,frame_index,preview_url:`/api/v1/artifacts/${id}-${c.candidate_id}-${array}-${frame_index}`,display_range:[0,1],display_transfer:'clamp_normalized_values_no_autoscale'})))}}));
  const analysis={study_id:id,plan_hash:record.plan_hash,status:'complete',phase:'tuning',analysis_sha256:'d'.repeat(64),coverage:{succeeded:2,failed:0,cancelled:0,running:0,queued:0},rows,
    reduction_definitions:{noise:{metric:'temporal_residual_std_dn',dataset_keys:['flat'],reducer:'max'},bias:{metric:'mean_abs_bias_dn',dataset_keys:['flat'],reducer:'max'}},
    aggregate:{rows:record.candidates.map(c=>({candidate_id:c.candidate_id,status:'succeeded',metrics:{noise:metric(2),bias:metric(null,'No reference')},constraints:[{metric:'bias',operator:'<=',value:2,unit:'DN_equivalent',actual:null,status:'unknown'}],feasibility:'unknown',pareto_status:'excluded',exclusion_reason:'Reference not available'})),objectives:[{metric:'noise',direction:'minimize'},{metric:'bias',direction:'minimize'}],metric_units:{noise:'DN_equivalent',bias:'DN_equivalent'},pareto_provisional:true}};
  return {record,analysis};
}
function context(handler,options={}) {
  const timers=new Map(),sandbox={window:{},TextEncoder,setTimeout:fn=>{const id=timers.size+1;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/camera_platform/static/isp-studies.js','utf8'),sandbox);
  const calls=[],chosen=[],validations=[],notices=[];let renders=0;
  const defaults=async(path,body)=>{
    if(path==='/isp-studies/catalog')return {metrics:{}};
    if(path==='/replay-sources')return [source('A'),source('B'),{...source('legacy'),geometry:{kind:'representative_raster'}},{...source('DCG'),raw_shape:[4,2,32,32],raw_format:{hdr_streams:2}}];
    if(path==='/isp-calibrations')return [{id:'CAL',name:'calibration',revision:'1'}];
    if(path==='/models')return [{id:'ISP',name:'ISP v2',kind:'isp',revision:'1',payload:{schema:'camera_isp_profile_v2'}}];
    if(path==='/isp-references')return [{id:'REF',name:'Independent <reference>',source_kind:'simulated',acquisition_group_id:'truth'}];
    if(path==='/isp-studies'&&body===undefined)return [];
    if(path==='/isp-selections')return [];
    if(path.startsWith('/templates/isp-study?'))return specification(new URL('http://t'+path).searchParams.get('source_id'));
    if(path==='/isp-studies/plan')return plan(body);
    if(path==='/isp-studies'&&body)return {...plan(body.specification),id:'STUDY'};
    const match=path.match(/^\/isp-studies\/([^/]+)(\/analysis)?$/);
    if(match){const item=result(match[1]);return match[2]?item.analysis:item.record;}
    if(path.endsWith('/selection'))return {...body,id:'SELECT',study_id:path.split('/')[2],calibration_id:'CAL',baseline_isp_model_id:'ISP'};
    if(path.endsWith('/cancel'))return {...result(path.split('/')[2]).analysis};
    throw Error('Unhandled '+path);
  };
  const ui=sandbox.window.CameraISPStudiesUI({api:async(path,body)=>{calls.push({path,body:body===undefined?undefined:copy(body)});const value=handler?await handler(path,body,defaults):await defaults(path,body);return copy(value);},esc,render:()=>renders++,toast:m=>notices.push(m),refresh:options.refresh||(async()=>{}),onStudy:id=>chosen.push(id),onValidationStudy:v=>validations.push(copy(v)),isActive:()=>true,records:()=>[]});
  return {ui,calls,chosen,validations,notices,timers,renders:()=>renders};
}
const change=(ui,field,value,extra={})=>ui.change({dataset:{ispstudyField:field,...extra},value});
const click=(ui,action,extra={})=>ui.click({dataset:{ispstudyAction:action,...extra}});
const submit=(ui,kind)=>ui.submit({dataset:{ispstudyForm:kind}});
const input=(ui,path,value,type='text')=>ui.input({dataset:{ispstudyField:'edit',path},value:String(value),type});
async function initialize(c) {await c.ui.activate();await change(c.ui,'source','run:A');await change(c.ui,'calibration','CAL');await change(c.ui,'baseline','ISP');await click(c.ui,'template');}

test('general fields use eligible native RAW, explicit datasets and declared units',async()=>{
  const c=context();await initialize(c);const html=c.ui.view();
  assert.match(html,/Study A/);assert.match(html,/RAW &lt;A&gt;/);assert.doesNotMatch(html,/>RAW &lt;legacy&gt;|>RAW &lt;DCG&gt;/);
  for(const field of ['datasets.0.acquisition_group_id','datasets.0.stationarity.description','datasets.0.metric_spec.roi.row_start','axes.0.values','constraints.0.value'])assert.ok(html.includes(`data-path="${field}"`));
  assert.match(html,/DN_equivalent/);assert.match(html,/전체 계획 JSON/);assert.match(html,/실물 검증과 통계적 우월성은 아직 확립하지/);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(x=>x[1]);assert.equal(ids.length,new Set(ids).size);
});

test('axis controls use the fixed template stages and restore only those stages after removal',async()=>{
  for(const allowed of [['raw_denoise.strength'],['linear_sharpen.amount'],['raw_denoise.strength','linear_sharpen.amount']]) {
    const c=context((path,body,next)=>{
      if(path.startsWith('/templates/isp-study?')){const spec=specification();spec.axes=spec.axes.filter(a=>allowed.includes(a.parameter));return spec;}
      return next(path,body);
    });
    await initialize(c);
    const selectors=[...c.ui.view().matchAll(/<select[^>]+data-path="axes\.\d+\.parameter"[^>]*>(.*?)<\/select>/gs)];
    assert.equal(selectors.length,allowed.length);
    for(const selector of selectors)assert.deepEqual([...selector[1].matchAll(/<option value="([^"]+)"/g)].map(m=>m[1]),allowed);
    assert.match(c.ui.view(),/data-ispstudy-action="add-axis"[^>]*disabled/);
    await click(c.ui,'add-axis');await submit(c.ui,'plan');
    assert.deepEqual(c.calls.findLast(x=>x.path==='/isp-studies/plan').body.axes.map(a=>a.parameter),allowed);
    await click(c.ui,'remove-axis',{index:'0'});await c.ui.activate();await click(c.ui,'add-axis');await submit(c.ui,'plan');
    const body=c.calls.findLast(x=>x.path==='/isp-studies/plan').body;
    assert.deepEqual(body.axes.map(a=>a.parameter).sort(),[...allowed].sort());
    assert.equal(body.baseline_isp_model_id,'ISP');assert.equal(body.calibration_id,'CAL');assert.equal(body.datasets[0].source_id,'A');
  }
});

test('both bypass template refusal is visible without creating a draft or altering profiles',async()=>{
  const message='Both raw denoise and linear sharpen are bypassed; configure an enabled stage in the baseline ISP before creating a tuning template';
  const c=context((path,body,next)=>{if(path.startsWith('/templates/isp-study?'))throw Error(message);return next(path,body);});
  await initialize(c);assert.match(c.ui.view(),/configure an enabled stage in the baseline ISP/);assert.doesNotMatch(c.ui.view(),/data-path="axes\./);
  assert.equal(c.calls.filter(x=>x.body!==undefined).length,0);
});

test('advanced JSON bypass axes remain subject to the server and cannot widen ordinary choices',async()=>{
  const c=context((path,body,next)=>{
    if(path.startsWith('/templates/isp-study?')){const spec=specification();spec.axes=spec.axes.slice(0,1);return spec;}
    if(path==='/isp-studies/plan'&&body.axes.some(a=>a.parameter==='linear_sharpen.amount'))throw Error('A tuning axis cannot be hidden by a bypassed stage');
    return next(path,body);
  });
  await initialize(c);c.ui.input({dataset:{ispstudyField:'json'},value:JSON.stringify(specification())});await submit(c.ui,'plan');
  assert.match(c.ui.view(),/bypassed stage/);assert.doesNotMatch(c.ui.view(),/data-ispstudy-plan/);
  const selectors=[...c.ui.view().matchAll(/<select[^>]+data-path="axes\.\d+\.parameter"[^>]*>(.*?)<\/select>/gs)];
  for(const selector of selectors)assert.doesNotMatch(selector[1],/linear_sharpen/);
  assert.equal(c.calls.filter(x=>x.path==='/isp-studies'&&x.body!==undefined).length,0);
});

test('new dataset ROI uses the declared server halo and remains nonempty on 16 by 20 RAW',async()=>{
  const c=context((path,body,next)=>path==='/isp-studies/catalog'?{metrics:{},evaluation_halo_native_pixels:4}:path==='/replay-sources'?[source(),{...source('B'),raw_shape:[4,16,20]}]:next(path,body));
  await initialize(c);await change(c.ui,'dataset-source','run:B',{index:'0'});input(c.ui,'datasets.0.acquisition_group_id','fresh');input(c.ui,'datasets.0.stationarity.description','fixed');await submit(c.ui,'plan');
  assert.deepEqual(c.calls.findLast(x=>x.path==='/isp-studies/plan').body.datasets[0].metric_spec.roi,{row_start:4,row_end:12,column_start:4,column_end:16});
});

test('template requests bind source calibration and baseline, and late source responses are discarded',async()=>{
  const wait=deferred(),c=context((path,body,next)=>path.includes('/templates/')?wait.promise:next(path,body));
  await c.ui.activate();await change(c.ui,'source','run:A');await change(c.ui,'calibration','CAL');await change(c.ui,'baseline','ISP');
  const pending=click(c.ui,'template');await change(c.ui,'source','run:B');wait.resolve(specification('A'));await pending;
  assert.doesNotMatch(c.ui.view(),/data-path="name"/);
  assert.equal(c.calls.find(x=>x.path.includes('/templates/')).path,'/templates/isp-study?source_kind=run&source_id=A&calibration_id=CAL&baseline_isp_model_id=ISP');
});

test('editing a frozen plan invalidates submit immediately and retains the draft through refresh',async()=>{
  const c=context();await initialize(c);await submit(c.ui,'plan');assert.match(c.ui.view(),/data-ispstudy-plan/);
  input(c.ui,'name','retained draft');assert.doesNotMatch(c.ui.view(),/data-ispstudy-plan/);await click(c.ui,'submit');assert.equal(c.calls.filter(x=>x.path==='/isp-studies'&&x.body).length,0);
  await c.ui.activate();assert.match(c.ui.view(),/value="retained draft"/);
});

test('late plan and late plan rejection cannot replace the newer edited state',async()=>{
  for(const reject of [false,true]) {
    const wait=deferred(),c=context((path,body,next)=>path==='/isp-studies/plan'?wait.promise:next(path,body));await initialize(c);
    const pending=submit(c.ui,'plan');input(c.ui,'question','newer question');if(reject)wait.reject(Error('stale failure'));else wait.resolve(plan());await pending;
    assert.doesNotMatch(c.ui.view(),/data-ispstudy-plan|stale failure/);assert.match(c.ui.view(),/newer question/);
  }
});

test('empty numeric fields, malformed JSON and invalid axis values never submit an earlier value',async()=>{
  const c=context();await initialize(c);input(c.ui,'constraints.0.value','','number');await submit(c.ui,'plan');assert.equal(c.calls.filter(x=>x.path==='/isp-studies/plan').length,0);
  assert.match(c.ui.view(),/이전 값으로 제출하지 않았습니다/);
  c.ui.input({dataset:{ispstudyField:'json'},value:'{"schema":"camera_isp_study_v1","datasets":null}'});await submit(c.ui,'plan');assert.equal(c.calls.filter(x=>x.path==='/isp-studies/plan').length,0);assert.match(c.ui.view(),/&quot;datasets&quot;:null/);
  c.ui.input({dataset:{ispstudyField:'json'},value:JSON.stringify(specification())});input(c.ui,'axes.0.values','0, nope');await submit(c.ui,'plan');assert.equal(c.calls.filter(x=>x.path==='/isp-studies/plan').length,0);
});

test('ordinary source and metric form edits submit exact ROI, reference, axes and reducer selection',async()=>{
  const c=context();await initialize(c);await change(c.ui,'dataset-source','run:B',{index:'0'});
  input(c.ui,'datasets.0.acquisition_group_id','new-acquisition');input(c.ui,'datasets.0.stationarity.description','declared unchanged scene');
  await change(c.ui,'edit','texture',{path:'datasets.0.metric_spec.kind'});input(c.ui,'datasets.0.metric_spec.texture.period_px',12,'number');
  await change(c.ui,'edit','REF',{path:'datasets.0.reference_id'});input(c.ui,'axes.0.values','0, 0.25, 1');await submit(c.ui,'plan');
  const body=c.calls.find(x=>x.path==='/isp-studies/plan').body;assert.deepEqual(body.axes[0].values,[0,.25,1]);assert.equal(body.datasets[0].source_id,'B');assert.equal(body.datasets[0].reference_id,'REF');assert.deepEqual(body.datasets[0].metric_spec.texture,{period_px:12,direction:'column'});assert.equal(body.datasets[0].metric_spec.edge,null);assert.equal(body.datasets[0].acquisition_group_id,'new-acquisition');
});

test('unknown committed submit preserves exact payload across later 403 and blocks new mutation',async()=>{
  let count=0;const c=context((path,body,next)=>{if(path==='/isp-studies'&&body){count++;if(count===1)throw Error('response lost');if(count===2){const error=Error('session rotated');error.status=403;throw error;}}return next(path,body);});
  await initialize(c);await submit(c.ui,'plan');await click(c.ui,'submit');assert.match(c.ui.view(),/응답이 불명확/);
  input(c.ui,'name','should not change');await click(c.ui,'template');await submit(c.ui,'plan');await click(c.ui,'submit');assert.equal(count,1);
  await click(c.ui,'retry');assert.equal(count,2);assert.match(c.ui.view(),/응답이 불명확/);await click(c.ui,'retry');assert.equal(count,3);
  const bodies=c.calls.filter(x=>x.path==='/isp-studies'&&x.body).map(x=>x.body);assert.deepEqual(bodies[0],bodies[1]);assert.deepEqual(bodies[1],bodies[2]);assert.doesNotMatch(c.ui.view(),/応答|should not change|응답이 불명확/);
});

test('initial definitive rejection unfreezes the preserved draft while completion plus refresh error does not resubmit',async()=>{
  const c=context((path,body,next)=>{if(path==='/isp-studies'&&body){const e=Error('plan binding changed');e.status=409;throw e;}return next(path,body);});
  await initialize(c);await submit(c.ui,'plan');await click(c.ui,'submit');assert.match(c.ui.view(),/plan binding changed/);assert.doesNotMatch(c.ui.view(),/응답이 불명확/);input(c.ui,'name','corrected');assert.match(c.ui.view(),/value="corrected"/);
  const ok=context(null,{refresh:async()=>{throw Error('refresh unavailable');}});await initialize(ok);await submit(ok.ui,'plan');await click(ok.ui,'submit');await click(ok.ui,'submit');assert.equal(ok.calls.filter(x=>x.path==='/isp-studies'&&x.body).length,1);assert.doesNotMatch(ok.ui.view(),/응답이 불명확/);
});

test('late selected-study analysis is scoped by identity and plan hash',async()=>{
  const wait=deferred(),c=context((path,body,next)=>path==='/isp-studies/old/analysis'?wait.promise:next(path,body));
  const pending=c.ui.selectStudy('old');await c.ui.selectStudy('new');wait.resolve(result('old').analysis);await pending;assert.match(c.ui.view(),/data-ispstudy-analysis="new"/);assert.doesNotMatch(c.ui.view(),/data-ispstudy-analysis="old"/);
  const bad=context((path,body,next)=>path==='/isp-studies/bad/analysis'?{...result('bad').analysis,plan_hash:'mismatch'}:next(path,body));await bad.ui.selectStudy('bad');assert.match(bad.ui.view(),/고정 계획이 다릅니다/);assert.doesNotMatch(bad.ui.view(),/data-ispstudy-analysis/);
});

test('unknown metric and constraint reasons stay unknown and never become zero or a Pareto winner',async()=>{
  const c=context();await c.ui.selectStudy('S');const html=c.ui.view();
  assert.match(html,/No independent reference &lt;truth&gt;/);assert.match(html,/제약 미확인/);assert.match(html,/잠정 Pareto/);assert.match(html,/비교 제외/);assert.doesNotMatch(html,/<svg/);assert.match(html,/전역 최적해, 자동 우승 후보/);
});

test('valid multiobjective plot displays supplied Pareto states without calculating new rankings',async()=>{
  const data=result('S');data.analysis.aggregate.rows.forEach((row,i)=>{row.metrics.bias=metric(i+1);row.feasibility='feasible';row.pareto_status=i?'dominated':'non_dominated';});data.analysis.aggregate.pareto_provisional=false;
  const longId='candidate-'+ 'a'.repeat(64);data.analysis.aggregate.rows[0].candidate_id=longId;
  const c=context((path,body,next)=>path==='/isp-studies/S/analysis'?data.analysis:next(path,body));await c.ui.selectStudy('S');const html=c.ui.view();assert.match(html,/<svg/);assert.match(html,/샘플 내 비지배/);assert.match(html,/샘플 내 지배됨/);assert.match(html,/반복 프레임 잔차 RMS · max\(flat\)/);
  const svg=html.match(/<svg.*?<\/svg>/s)[0];assert.ok(svg.includes('<title>'+longId));assert.match(svg,/>C1<\/text>/);assert.ok(!svg.match(/<text[^>]*>candidate-[a-f0-9]{64}<\/text>/));assert.ok(html.includes('title="'+longId+'">C1'));
  data.analysis.phase='independent_validation';data.analysis.aggregate.rows.forEach(row=>{row.pareto_status='not_ranked_validation';});data.analysis.aggregate.pareto_provisional=true;
  await c.ui.selectStudy('S');const validation=c.ui.view();assert.match(validation,/고정 기준·선택 후보 검증 · 순위 없음/);assert.match(validation,/이 데이터로 후보를 다시 순위화하지 않습니다/);assert.doesNotMatch(validation,/잠정 Pareto · 미확인\/미완료 포함|녹색: 서버가 판정한/);
});

test('previews require the same RAW hash, same frame and identical display scale',async()=>{
  const c=context();await c.ui.selectStudy('S');await change(c.ui,'candidate','candidate_1');await change(c.ui,'frame','1');let html=c.ui.view();
  assert.match(html,/<img[^>]*S-candidate_1-baseline_rgb_frames-1/);assert.match(html,/<img[^>]*S-candidate_1-rgb_frames-1/);assert.doesNotMatch(html,/<img[^>]*frames-0/);assert.match(html,/등록된 기준 후보와 다를 수/);
  for(const corrupt of [row=>row.summary.raw_sha256='wrong',row=>row.summary.stage_previews[1].display_range=[0,2],row=>row.summary.stage_previews[1].preview_url='javascript:unsafe']) {
    const data=result('S');corrupt(data.analysis.rows[1]);const bad=context((path,body,next)=>path==='/isp-studies/S/analysis'?data.analysis:next(path,body));await bad.ui.selectStudy('S');await change(bad.ui,'candidate','candidate_1');html=bad.ui.view();assert.doesNotMatch(html,/<img/);
  }
});

test('fresh evidence failure clears cached metrics and previews while retaining the editable draft',async()=>{
  let damaged=false;const c=context((path,body,next)=>{if(damaged&&path==='/isp-studies/S/analysis')throw Object.assign(Error('Original RAW SHA mismatch'),{status:409});return next(path,body);});
  await initialize(c);input(c.ui,'name','Preserved during failure');await c.ui.selectStudy('S');await change(c.ui,'candidate','candidate_1');assert.match(c.ui.view(),/<img/);
  damaged=true;await click(c.ui,'refresh-analysis');let html=c.ui.view();assert.match(html,/Original RAW SHA mismatch/);assert.match(html,/value="Preserved during failure"/);assert.doesNotMatch(html,/<img|data-ispstudy-analysis|raw hash.*winner/);assert.match(html,/이전 결과를 표시하지 않습니다/);
  damaged=false;await click(c.ui,'refresh-analysis');html=c.ui.view();assert.match(html,/<img/);assert.match(html,/data-ispstudy-analysis="S"/);
});

test('cancellation with unavailable evidence reports owned job states without restoring old successful results',async()=>{
  const c=context((path,body,next)=>path==='/isp-studies/S/cancel'?{schema:'camera_isp_study_cancellation_v1',study_id:'S',status:'cancellation_recorded_evidence_unavailable',rows:[{job_id:'owned-cell',candidate_id:'candidate_1',dataset_key:'flat',status:'cancelled'}],evidence_error:'Missing source <RAW>',physical_validation:'not_established'}:next(path,body));
  await c.ui.selectStudy('S');await change(c.ui,'candidate','candidate_1');assert.match(c.ui.view(),/<img/);await click(c.ui,'cancel');const html=c.ui.view();
  assert.match(html,/취소 상태 · 계산 근거 확인 불가/);assert.match(html,/Missing source &lt;RAW&gt;/);assert.match(html,/flat · cancelled/);assert.doesNotMatch(html,/<img|data-ispstudy-analysis|응답이 불명확/);assert.match(html,/계산 성공이나 근거 검증을 뜻하지 않습니다/);
});

test('selection pins candidate plus current analysis hash and starts separate validation without reusing tuning RAW',async()=>{
  const c=context();await c.ui.selectStudy('S');await change(c.ui,'candidate','candidate_1');c.ui.input({dataset:{ispstudyField:'rationale'},value:'保留 edge detail · check fresh acquisition'});await submit(c.ui,'selection');
  const body=c.calls.find(x=>x.path.endsWith('/selection')).body;assert.deepEqual(body,{candidate_id:'candidate_1',rationale:'保留 edge detail · check fresh acquisition',analysis_sha256:'d'.repeat(64)});
  await click(c.ui,'validation');assert.equal(c.validations.length,1);assert.match(c.ui.view(),/독립 검증/);assert.match(c.ui.view(),/SELECT/);assert.doesNotMatch(c.ui.view(),/data-path="datasets.0/);await submit(c.ui,'plan');assert.equal(c.calls.filter(x=>x.path==='/isp-studies/plan').length,0);
  await click(c.ui,'add-dataset');assert.match(c.ui.view(),/independent_validation/);assert.doesNotMatch(c.ui.view(),/data-path="axes.0/);
});

test('typing a selection rationale renders and enables save without requiring blur',async()=>{
  const c=context();await c.ui.selectStudy('S');await change(c.ui,'candidate','candidate_1');
  const selectionButton=()=>c.ui.view().match(/<form data-ispstudy-form="selection">.*?<button[^>]*type="submit"[^>]*>/s)[0].split('<button').at(-1);
  assert.match(selectionButton(),/disabled/);const before=c.renders();
  c.ui.input({dataset:{ispstudyField:'rationale'},value:'Preserve the measured edge response in the next acquisition'});
  assert.equal(c.renders(),before+1);assert.doesNotMatch(selectionButton(),/disabled/);
  c.ui.input({dataset:{ispstudyField:'rationale'},value:'  '});assert.match(selectionButton(),/disabled/);
});

test('late selection cannot replace a newer study or implicitly send it to a case',async()=>{
  const wait=deferred(),c=context((path,body,next)=>path.endsWith('/selection')?wait.promise:next(path,body));await c.ui.selectStudy('A');await change(c.ui,'candidate','candidate_1');c.ui.input({dataset:{ispstudyField:'rationale'},value:'selected explicitly'});const pending=submit(c.ui,'selection');await c.ui.selectStudy('B');wait.resolve({id:'SELECT-A',study_id:'A',candidate_id:'candidate_1',rationale:'selected explicitly'});await pending;assert.doesNotMatch(c.ui.view(),/선택 SELECT-A/);assert.equal(c.chosen.length,0);await click(c.ui,'use-study');assert.deepEqual(c.chosen,['B']);
});

test('a late accepted submit adds history without replacing a more recently selected study',async()=>{
  const wait=deferred(),c=context((path,body,next)=>path==='/isp-studies'&&body?wait.promise:next(path,body));await initialize(c);await submit(c.ui,'plan');
  const pending=click(c.ui,'submit');await c.ui.selectStudy('OTHER');wait.resolve({...plan(),id:'NEW'});await pending;
  assert.match(c.ui.view(),/data-ispstudy-analysis="OTHER"/);assert.doesNotMatch(c.ui.view(),/data-ispstudy-analysis="NEW"/);assert.match(c.ui.view(),/value="NEW"/);
});

test('saved selections resume validation and new dataset scopes cannot inherit hidden tuning keys',async()=>{
  const selection={id:'SAVED',study_id:'S',candidate_id:'candidate_1',rationale:'detail retained'};
  const c=context((path,body,next)=>path==='/isp-selections'?[selection]:path==='/isp-selections/SAVED'?selection:next(path,body));await initialize(c);await c.ui.selectStudy('S');
  assert.match(c.ui.view(),/保留|보존된 설계 선택/);await change(c.ui,'saved-selection','SAVED');await click(c.ui,'validation');await click(c.ui,'add-dataset');await change(c.ui,'dataset-source','run:B',{index:'0'});
  input(c.ui,'datasets.0.key','fresh');input(c.ui,'datasets.0.acquisition_group_id','independent');input(c.ui,'datasets.0.stationarity.description','independent static acquisition');
  for(const [kind,index] of [['objectives','0'],['objectives','1'],['constraints','0']])await c.ui.change({dataset:{ispstudyField:'reduction-dataset',kind,index,key:'fresh'},checked:true});
  await submit(c.ui,'plan');const body=c.calls.findLast(x=>x.path==='/isp-studies/plan').body;assert.equal(body.phase,'independent_validation');assert.equal(body.selection_id,'SAVED');assert.equal(body.parent_study_id,'S');assert.deepEqual(body.axes,[]);assert.ok([...body.objectives,...body.constraints].every(x=>JSON.stringify(x.dataset_keys)==='["fresh"]'));assert.equal(body.datasets[0].source_id,'B');
});

test('renaming and removing a dataset updates only its explicit reduction references',async()=>{
  const c=context();await initialize(c);input(c.ui,'datasets.0.key','renamed');await submit(c.ui,'plan');let body=c.calls.findLast(x=>x.path==='/isp-studies/plan').body;assert.deepEqual(body.objectives[0].dataset_keys,['renamed']);
  await click(c.ui,'remove-dataset',{index:'0'});await click(c.ui,'add-dataset');await change(c.ui,'dataset-source','run:B',{index:'0'});input(c.ui,'datasets.0.acquisition_group_id','B');input(c.ui,'datasets.0.stationarity.description','static');await submit(c.ui,'plan');body=c.calls.findLast(x=>x.path==='/isp-studies/plan').body;assert.deepEqual(body.objectives[0].dataset_keys,[]);assert.deepEqual(body.constraints[0].dataset_keys,[]);
});

test('cancel targets the viewed study, and polling is disabled when module deactivates',async()=>{
  const c=context((path,body,next)=>path==='/isp-studies/S/analysis'?{...result('S').analysis,status:'incomplete',coverage:{queued:2,running:0}}:next(path,body));await c.ui.activate();await c.ui.selectStudy('S');assert.equal(c.timers.size,1);await click(c.ui,'cancel');const call=c.calls.find(x=>x.path.endsWith('/cancel'));assert.equal(call.path,'/isp-studies/S/cancel');assert.deepEqual(call.body,{});c.ui.deactivate();assert.equal(c.timers.size,0);
});

test('unrelated host events are untouched and require no API call',async()=>{
  const c=context();assert.equal(c.ui.input({dataset:{}}),false);assert.equal(await c.ui.change({dataset:{}}),false);assert.equal(await c.ui.click({dataset:{}}),false);assert.equal(await c.ui.submit({dataset:{}}),false);assert.equal(c.calls.length,0);assert.equal(c.renders(),0);
});

const chargeSource=(path='hcg')=>({...source('DCG'),selector:{schema:'camera_nonlinear_raw_selection_v1',mode:'shared_charge_path',readout_id:path},raw_format:{hdr_streams:2}});
const chargeKey=path=>'run:DCG:'+JSON.stringify(['camera_nonlinear_raw_selection_v1','shared_charge_path',path]);
function chargeSpec(){const spec=specification('DCG');spec.schema='camera_isp_study_v2';spec.calibration_id=null;spec.datasets[0].selector=chargeSource().selector;spec.datasets[0].metric_spec.schema='camera_isp_metric_spec_v2';for(const item of [...spec.objectives,...spec.constraints]){item.metric=item.metric.replace(/_dn$/,'_e');if(item.unit)item.unit='electron_equivalent';}return spec;}
function chargeHandler(path,body,next){
  if(path==='/replay-sources')return [chargeSource('hcg'),chargeSource('lcg'),source()];
  if(path==='/models')return [{id:'ISP',name:'Charge ISP',kind:'isp',payload:{schema:'camera_isp_profile_v3'}}];
  if(path.startsWith('/templates/isp-study?'))return chargeSpec();
  return next(path,body);
}
test('charge Study template retains explicit path without optional LSC and uses electron metrics',async()=>{
  const c=context(chargeHandler);await c.ui.activate();await change(c.ui,'source',chargeKey('hcg'));await change(c.ui,'baseline','ISP');await click(c.ui,'template');
  const request=c.calls.find(x=>x.path.startsWith('/templates/isp-study?'));assert.ok(request);const query=new URL('http://t'+request.path).searchParams;
  assert.equal(query.get('version'),'2');assert.equal(query.get('calibration_id'),null);assert.deepEqual(JSON.parse(query.get('selector')),chargeSource().selector);
  const html=c.ui.view();assert.match(html,/electron_equivalent/);assert.doesNotMatch(html,/value="temporal_residual_std_dn"/);await submit(c.ui,'plan');
  const body=c.calls.find(x=>x.path==='/isp-studies/plan').body;assert.equal(body.schema,'camera_isp_study_v2');assert.equal(body.datasets[0].selector.readout_id,'hcg');assert.equal(body.datasets[0].metric_spec.schema,'camera_isp_metric_spec_v2');
});
test('changing a Study dataset DCG path changes its selector and invalidates inspected plan',async()=>{
  const c=context(chargeHandler);await c.ui.activate();await change(c.ui,'source',chargeKey('hcg'));await change(c.ui,'baseline','ISP');await click(c.ui,'template');await submit(c.ui,'plan');
  await change(c.ui,'dataset-source',chargeKey('lcg'),{index:'0'});assert.doesNotMatch(c.ui.view(),/data-ispstudy-plan=/);
  input(c.ui,'datasets.0.acquisition_group_id','same-acquisition');input(c.ui,'datasets.0.stationarity.description','same fixed scene');await submit(c.ui,'plan');
  assert.equal(c.calls.filter(x=>x.path==='/isp-studies/plan').at(-1).body.datasets[0].selector.readout_id,'lcg');
});

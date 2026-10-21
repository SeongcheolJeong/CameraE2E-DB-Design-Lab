'use strict';

// Actual UI module with deterministic API/timer stubs. This is not browser or
// physical ISP evidence; Python workflows exercise the corresponding server.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const script=fs.readFileSync('src/camera_platform/static/retained-isp.js','utf8');
const clone=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const base='/retained-isp/comparisons';
const workbench='/retained-isp/workbench';
const source=(id='A',path='single')=>({source_kind:'run',source_id:id,name:'native <'+id+'>',readout_id:path,selector:{schema:'camera_retained_raw_selection_v1',readout_id:path},raw_sha256:id.repeat(64),raw_shape:[2,8,10],raw_format:{response_type:'nonlinear'}});
const sourceKey=s=>JSON.stringify([s.source_id,s.selector.readout_id,s.raw_sha256]);
const model=(id='M')=>({id,kind:'isp',name:'ISP <'+id+'>',revision:'1',source_kind:'measured',physical_instance:'old-device',parents:['parent'],payload:{schema:'camera_retained_code_isp_profile_v1',spatial_filter:{mode:'same_site_bilateral_code_v1',strength:.5,range_sigma_dn:16},temporal_filter:{mode:'causal_difference_gate_v1',previous_weight:.5,threshold_dn:32,max_inter_transfer_interval_ns:100000000},detail:{mode:'bounded_code_detail_v1',amount:.25,threshold:.01,max_delta:.1}}});
const projectHistory=r=>({id:r.id,name:r.name,status:r.status,job_kind:r.job_kind,plugin_id:r.plugin_id,full_record_sha256:'a'.repeat(64),detail_path:(r.job_kind==='isp_replay'?'/replays':base)+'/'+r.id});
const catalogue=()=>({schema:'camera_retained_isp_workbench_v1',sources:[source(),source('B','LCG')],models:[model()],replays:[],comparisons:[],source_discovery:{scope:'metadata_only',raw_integrity:'verified_on_selection',physical_validation:'not_established'}});
const preview=(id,index)=>({id,name:id+'.png',url:'/api/v1/artifacts/'+id,frame_id:'frame-'+index,frame_index:index});
const record=(id='R',comparison=false,status='succeeded')=>{
  const groups=(comparison?['baseline','candidate-000']:['replay']).map(group=>({id:group,label:group+' <label>',frames:[0,1].map(index=>{const a=preview(group+index,index);return {frame_id:a.frame_id,frame_index:index,preview_artifact:a.name,preview_artifact_id:a.id,preview_url:a.url};})}));
  const artifacts=groups.flatMap(g=>g.frames.map(f=>({id:f.preview_artifact_id,name:f.preview_artifact,url:f.preview_url})));
  const metrics={per_frame:[0,1].map(index=>({frame_index:index,metrics:{display_clipping_fraction:{value:index/8,unit:'1'},temporal_change_distortion:{value:index?0.01:null,unit:'normalized_code_rgb',reason:index?null:'No preceding selected frame'}}}))};
  return {id,name:'result <'+id+'>',status,job_kind:comparison?'retained_isp_comparison':'isp_replay',plugin_id:comparison?'retained_isp_comparison_v1':'retained_isp_replay_v1',artifacts,result:{summary:{source_id:'A',selector:source().selector,raw_sha256:'a'.repeat(64),...(comparison?{preview_groups:groups,baseline:{candidate_id:'baseline',metrics},candidates:[{candidate_id:'candidate-000',coordinates:{'spatial_filter.strength':.5},metrics}],ranking:{candidates:[{candidate_id:'candidate-000',feasible:true,pareto:true}]}}:{frames:groups[0].frames})}}};
};
function context(handler) {
  const calls=[],timers=new Map();let timerId=0,renders=0,active=true;
  const box={window:{},document:{querySelector:()=>({remove(){}})},setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(box);vm.runInContext(script,box);
  const ui=box.window.CameraRetainedISPUI({esc,render:()=>renders++,isActive:()=>active,api:async(path,body)=>{
    calls.push({path,body:body===undefined?undefined:clone(body)});
    const handled=handler?await handler(path,body):undefined;if(handled!==undefined)return handled;
    if(path===workbench&&!body)return catalogue();
    if(path==='/models'&&body)return {...clone(body),id:'NEW'};
    if(path==='/replays'&&body)return record('R',false,'queued');
    if(path===base&&body)return record('C',true,'queued');
    if(path.startsWith('/templates/retained-isp-model?'))return model('template');
    if(path===base+'/plan')return {plan_hash:'pinned',frozen_request:{specification:clone(body)},cost:{candidate_count:6}};
    if(path.startsWith('/replays/'))return record(path.split('/')[2]);
    if(path.startsWith(base+'/'))return record(path.split('/')[3],true);
    throw Error('Unexpected API '+path);
  }});
  return {ui,calls,timers,renders:()=>renders,setActive:x=>{active=x;},tick:async()=>{const entry=timers.entries().next().value;if(entry){timers.delete(entry[0]);entry[1]();}for(let i=0;i<12;i++)await Promise.resolve();}};
}
const click=(c,action)=>c.ui.click({dataset:{retainedispAction:action}});
const change=(c,field,value)=>c.ui.change({dataset:{retainedispField:field},value});
const text=(c,field,value)=>c.ui.input({dataset:{retainedispField:field},value});
const edit=(c,path,value,type='number')=>c.ui.input({dataset:{retainedispField:'model-edit',path},type,value:String(value)});
const setup=async c=>{await c.ui.activate();await change(c,'source',sourceKey(source()));await change(c,'model','M');};
const plan=async c=>{await setup(c);await click(c,'comparison-template');await click(c,'plan');};

test('v2 comparison draft binds an authored temporal Test and changing it invalidates the plan',async()=>{
  const c=context();await setup(c);await click(c,'comparison-template');await change(c,'temporal-test-id','test-reference-1');
  await click(c,'comparison-temporal-template');await click(c,'plan');
  const request=c.calls.find(x=>x.path===base+'/plan'&&x.body).body;
  assert.equal(request.schema,'camera_retained_code_comparison_v2');
  assert.equal(request.temporal_test_id,'test-reference-1');
  assert.deepEqual(request.temporal_objectives,[{metric:'signal_change_rms_error',direction:'minimize'}]);
  assert.deepEqual(request.temporal_constraints.map(x=>x.metric),['truth_code_mae','signal_amplitude_error','signal_change_rms_error']);
  assert.match(c.ui.view(),/data-retainedisp-plan="pinned"/);
  await change(c,'temporal-test-id','different-test');
  assert.doesNotMatch(c.ui.view(),/data-retainedisp-plan=/);
  await click(c,'plan');
  assert.match(c.ui.view(),/시간 참조 Test ID와 비교 초안이 다릅니다/);
  assert.equal(c.calls.filter(x=>x.path===base+'/plan'&&x.body).length,1);
  await click(c,'submit');assert.equal(c.calls.filter(x=>x.path===base&&x.body).length,0);
});

test('v2 saved comparison shows signal truth support and excludes unknown candidates without claiming validation',async()=>{
  const saved=record('truth',true),summary=saved.result.summary;
  summary.temporal_validation={test_id:'test-1',reference_id:'reference-1',reference_sha256:'b'.repeat(64),truth_stage:'ccm_code_rgb_frames',candidate_stage:'pre_tone_code_rgb_frames'};
  const temporal_metrics={schema:'camera_temporal_signal_metrics_v1',support_status:'unknown',support_reason:'insufficient <step> support',metrics:{truth_code_mae:{value:null,unit:'normalized_code_rgb',status:'unknown',reason:'clipped <pixels>'},added_sample_age_ns:{value:10,unit:'ns',status:'ok',reason:null}}};
  summary.candidates[0].temporal_metrics=temporal_metrics;
  summary.ranking={pareto_provisional:true,excluded_candidate_ids:['candidate-000'],candidates:[{candidate_id:'candidate-000',feasible:false,feasibility:'unknown',pareto:false,temporal_constraints:[{metric:'truth_code_mae',status:'unknown'}]}]};
  const c=context(path=>path===base+'/truth'?saved:undefined);await change(c,'history',base+'/truth');await change(c,'group','candidate-000');
  const html=c.ui.view();assert.match(html,/시간 참조에 대한 후보 평가/);assert.match(html,/pre_tone_code_rgb_frames/);
  assert.match(html,/작성된 합성 기준/);assert.match(html,/비선정 후보 1개/);assert.match(html,/일부 지표는 미확인/);
  assert.match(html,/작성 참조 대비 절대 코드 오차/);assert.match(html,/unknown/);
  assert.match(html,/insufficient &lt;step&gt; support/);assert.match(html,/clipped &lt;pixels&gt;/);
  assert.doesNotMatch(html,/<step>|<pixels>/);
});

test('workbench accepts verified v2 comparison history and still rejects unknown plugin identities',async()=>{
  const saved=record('v2',true);saved.plugin_id='retained_isp_comparison_v2';
  const catalogueV2={...catalogue(),comparisons:[projectHistory(saved)]};
  const c=context(path=>path===workbench?catalogueV2:undefined);
  await c.ui.activate();assert.match(c.ui.view(),/result &lt;v2&gt;/);
  catalogueV2.comparisons[0].plugin_id='retained_isp_comparison_v3';
  await c.ui.activate();assert.match(c.ui.view(),/목록의 형식 또는 검증 범위/);
});

test('explicit readout and immutable model IDs are sent without charge calibration fields',async()=>{
  const c=context();await setup(c);await change(c,'source',sourceKey(source('B','LCG')));await change(c,'model','M');await click(c,'template');
  assert.ok(c.calls.some(x=>x.path==='/templates/retained-isp-model?source_id=B&readout_id=LCG'));
  await click(c,'replay');const body=c.calls.find(x=>x.path==='/replays'&&x.body).body;
  assert.deepEqual(body,{schema:'camera_retained_code_isp_replay_v1',name:'시간 이력 ISP · LCG',source_kind:'run',source_id:'B',selector:source('B','LCG').selector,isp_model_id:'M',force:false});
  assert.match(c.ui.view(),/native &lt;B&gt;/);assert.match(c.ui.view(),/광전하 복원/);
});

test('source-specific incomplete drafts survive switches and stale template responses',async()=>{
  const wait=deferred(),c=context(path=>path.startsWith('/templates/')?wait.promise:undefined);await setup(c);const pending=click(c,'template');
  await change(c,'source',sourceKey(source('B','LCG')));text(c,'model-json','{unfinished B');wait.resolve(model('late-A'));await pending;
  assert.match(c.ui.view(),/\{unfinished B/);assert.doesNotMatch(c.ui.view(),/late-A/);
  await change(c,'source',sourceKey(source()));assert.doesNotMatch(c.ui.view(),/\{unfinished B/);text(c,'model-json','{unfinished A');
  await change(c,'source',sourceKey(source('B','LCG')));assert.match(c.ui.view(),/\{unfinished B/);
});

test('newest template wins and a stale failure never overwrites another source error',async()=>{
  const a=deferred(),b=deferred();let requests=0;const c=context(path=>path.startsWith('/templates/')?(++requests===1?a.promise:b.promise):undefined);await setup(c);
  const first=click(c,'template'),second=click(c,'template');b.resolve(model('newest'));await second;a.reject(Error('stale template failure'));await first;
  assert.match(c.ui.view(),/newest/);assert.doesNotMatch(c.ui.view(),/stale template failure/);
});

test('blank, unsafe ns and broken JSON never register the preceding valid value',async()=>{
  const c=context();await setup(c);await click(c,'template');edit(c,'payload.spatial_filter.strength','');await click(c,'register');
  assert.equal(c.calls.filter(x=>x.path==='/models'&&x.body).length,0);assert.match(c.ui.view(),/이전 값으로 등록하지 않았습니다/);assert.match(c.ui.view(),/data-path="payload.spatial_filter.strength" type="number" value=""/);
  await click(c,'template');edit(c,'payload.temporal_filter.max_inter_transfer_interval_ns','9007199254740993');await click(c,'register');
  assert.match(c.ui.view(),/value="9007199254740993"/);assert.match(c.ui.view(),/max="9007199254740991"/);
  text(c,'model-json',JSON.stringify(model()).replace('100000000','9007199254740993'));await click(c,'register');assert.match(c.ui.view(),/정확히 보존/);
  text(c,'model-json','{"broken":');await click(c,'register');assert.match(c.ui.view(),/\{&quot;broken&quot;:/);
  assert.equal(c.calls.filter(x=>x.path==='/models'&&x.body).length,0);
});

test('friendly edits demote authored provenance and preserve parent IDs',async()=>{
  const c=context();await setup(c);await click(c,'template');edit(c,'payload.temporal_filter.previous_weight',.7);await click(c,'register');
  const body=c.calls.find(x=>x.path==='/models'&&x.body).body;assert.equal(body.payload.temporal_filter.previous_weight,.7);assert.equal(body.source_kind,'estimated');assert.equal(body.physical_instance,null);assert.deepEqual(body.parents,['parent']);
});

test('edited or cross-source plans cannot submit and submission uses frozen specification',async()=>{
  let planned;const c=context((path,body)=>{if(path===base+'/plan'){planned={plan_hash:'server-plan',frozen_request:{specification:clone(body)}};return planned;}});await plan(c);
  planned.frozen_request.specification.name='external mutation';await click(c,'submit');
  const sent=c.calls.find(x=>x.path===base&&x.body).body;assert.equal(sent.plan_hash,'server-plan');assert.notEqual(sent.specification.name,'external mutation');
  await click(c,'comparison-template');await click(c,'plan');text(c,'comparison-json','{unfinished plan');await click(c,'submit');
  assert.equal(c.calls.filter(x=>x.path===base&&x.body).length,1);assert.doesNotMatch(c.ui.view(),/data-retainedisp-plan=/);
});

test('a late plan after edit is discarded; failed replanning clears earlier approval',async()=>{
  const wait=deferred();let phase=0;const c=context((path,body)=>path===base+'/plan'?(phase===1?wait.promise:phase===2?Promise.reject(Error('plan refused')):{plan_hash:'ok',frozen_request:{specification:clone(body)}}):undefined);
  await plan(c);assert.match(c.ui.view(),/data-retainedisp-plan=/);phase=1;const pending=click(c,'plan');text(c,'comparison-json','{editing');wait.resolve({plan_hash:'late',frozen_request:{specification:{}}});await pending;assert.doesNotMatch(c.ui.view(),/data-retainedisp-plan=/);
  await click(c,'comparison-template');phase=0;await click(c,'plan');phase=2;await click(c,'plan');assert.match(c.ui.view(),/plan refused/);assert.doesNotMatch(c.ui.view(),/data-retainedisp-plan=/);
});

test('explicit forced comparison survives normalized frozen specification without force',async()=>{
  const c=context((path,body)=>{if(path===base+'/plan'){const specification=clone(body);delete specification.force;return {plan_hash:'force-plan',frozen_request:{specification}};}});
  await setup(c);await click(c,'comparison-template');
  const html=c.ui.view(),match=html.match(/<textarea id="retainedisp-comparison-json"[^>]*>([\s\S]*?)<\/textarea>/);
  const document=JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
  document.force=true;text(c,'comparison-json',JSON.stringify(document));await click(c,'plan');await click(c,'submit');
  assert.equal(c.calls.find(x=>x.path===base&&x.body).body.specification.force,true);
});

test('concurrent plan requests retain only the latest accepted response',async()=>{
  const a=deferred(),b=deferred();let request=0;let specification;
  const c=context((path,body)=>{if(path===base+'/plan'){specification=clone(body);return ++request===1?a.promise:b.promise;}});
  await setup(c);await click(c,'comparison-template');const first=click(c,'plan'),second=click(c,'plan');
  b.resolve({plan_hash:'newest',frozen_request:{specification}});await second;a.reject(Error('old plan failure'));await first;
  assert.match(c.ui.view(),/data-retainedisp-plan="newest"/);assert.doesNotMatch(c.ui.view(),/old plan failure/);
});

test('preview matching uses recorded frame ID and index, with real comparison metric names',async()=>{
  const r=record('C',true);r.result.summary.preview_groups[0].frames.reverse();const c=context(path=>path===base+'/C'?r:undefined);
  await change(c,'history',base+'/C');let html=c.ui.view();assert.match(html,/src="\/api\/v1\/artifacts\/baseline0"/);assert.match(html,/src="\/api\/v1\/artifacts\/candidate-0000"/);assert.match(html,/No preceding selected frame/);assert.match(html,/표시 코드 범위 초과 비율/);
  await change(c,'frame','1');html=c.ui.view();assert.match(html,/src="\/api\/v1\/artifacts\/baseline1"/);assert.match(html,/0\.125 · 1/);assert.doesNotMatch(html,/<label>/);
  await change(c,'group','baseline');assert.equal((c.ui.view().match(/<img class="preview-image"/g)||[]).length,1);
});

test('replay frame preview IDs use imaging fields; missing pair or unsafe URLs never renders an image',async()=>{
  const r=record(),c=context(path=>path==='/replays/R'?r:undefined);await change(c,'history','/replays/R');assert.match(c.ui.view(),/src="\/api\/v1\/artifacts\/replay0"/);
  for(const url of ['javascript:alert(1)','https://remote.test/x','/api/v1/artifacts/../session','/api/v1/artifacts/a/b']){
    r.artifacts[0].url=url;r.result.summary.frames[0].preview_url=url;await change(c,'history','/replays/R');assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);
  }
  r.artifacts[0].url='/api/v1/artifacts/replay0';r.result.summary.frames[0].preview_url='/api/v1/artifacts/other';await change(c,'history','/replays/R');assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);
});

test('failed record and collection GETs clear previous previews but preserve drafts',async()=>{
  let failure='';const c=context(path=>failure===path?Promise.reject(Error('readback failed')):undefined);await setup(c);text(c,'model-json','{draft');await change(c,'history','/replays/R');assert.match(c.ui.view(),/<img class="preview-image"/);
  failure='/replays/R';await click(c,'record-refresh');assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);assert.match(c.ui.view(),/\{draft/);
  failure='';await change(c,'history','/replays/R');failure=workbench;await click(c,'load');assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);assert.match(c.ui.view(),/\{draft/);
});

test('stale record failure cannot erase a newer result and unsafe history is refused',async()=>{
  const wait=deferred(),c=context(path=>path==='/replays/old'?wait.promise:undefined);const pending=change(c,'history','/replays/old');await change(c,'history','/replays/new');wait.reject(Error('old failed'));await pending;
  assert.match(c.ui.view(),/result &lt;new&gt;/);assert.doesNotMatch(c.ui.view(),/old failed/);const count=c.calls.length;await assert.rejects(change(c,'history','/replays/../session'));assert.equal(c.calls.length,count);
});

test('busy submission blocks duplicate writes and history/source changes',async()=>{
  const wait=deferred(),c=context((path,body)=>path==='/replays'&&body?wait.promise:undefined);await setup(c);const pending=click(c,'replay');
  await click(c,'replay');await change(c,'history','/replays/other');await change(c,'source',sourceKey(source('B','LCG')));assert.equal(c.calls.filter(x=>x.path==='/replays'&&x.body).length,1);assert.ok(!c.calls.some(x=>x.path==='/replays/other'));assert.match(c.ui.view(),/data-retainedisp-field="history" disabled/);
  wait.resolve(record('R',false,'queued'));await pending;assert.match(c.ui.view(),/native &lt;A&gt;/);assert.doesNotMatch(c.ui.view(),/data-retainedisp-field="history" disabled/);
});

test('polling runs only while active and stops on a terminal record',async()=>{
  let state='queued';const c=context(path=>path==='/replays/R'?record('R',false,state):undefined);await change(c,'history','/replays/R');assert.equal(c.timers.size,1);
  c.setActive(false);await c.tick();assert.equal(c.calls.filter(x=>x.path==='/replays/R').length,1);assert.equal(c.timers.size,0);
  c.setActive(true);await click(c,'record-refresh');state='succeeded';await c.tick();assert.equal(c.timers.size,0);assert.match(c.ui.view(),/succeeded/);
});

for(const comparison of [false,true])test('verified polling refreshes saved '+(comparison?'comparison':'replay')+' history status',async()=>{
  let state='queued';const collection=comparison?base:'/replays',path=collection+'/R';
  const c=context((request,body)=>{
    if(request===workbench&&!body)return {...catalogue(),[comparison?'comparisons':'replays']:[projectHistory(record('R',comparison,'queued'))]};
    if(request===path)return record('R',comparison,state);
  });
  await c.ui.activate();await change(c,'history',path);
  const history=()=>c.ui.view().match(/<select id="retainedisp-history"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.match(history(),/result &lt;R&gt; · queued<\/option>/);
  state='succeeded';await c.tick();
  assert.match(history(),/result &lt;R&gt; · succeeded<\/option>/);
  assert.doesNotMatch(history(),/result &lt;R&gt; · queued<\/option>/);
  assert.equal(c.timers.size,0);
});

test('activation uses one compact workbench GET and selected results still use full detail',async()=>{
  const saved={...catalogue(),replays:[projectHistory(record('R'))],comparisons:[projectHistory(record('C',true))]};
  const c=context(path=>path===workbench?saved:undefined);
  await c.ui.activate();assert.deepEqual(c.calls,[{path:workbench,body:undefined}]);
  assert.match(c.ui.view(),/result &lt;R&gt; · succeeded/);assert.match(c.ui.view(),/result &lt;C&gt; · succeeded/);
  assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);
  await change(c,'source',sourceKey(source()));assert.match(c.ui.view(),/원본 RAW는 초안 생성·실행 준비 시 확인/);
  await change(c,'history',base+'/C');assert.equal(c.calls.at(-1).path,base+'/C');
  assert.match(c.ui.view(),/src="\/api\/v1\/artifacts\/candidate-0000"/);
  assert.match(c.ui.view(),/href="\/api\/v1\/retained-isp\/comparisons\/C\/bundle"/);
});

test('catalogue loading is visible, blocks writes and keeps edited drafts',async()=>{
  const pending=deferred();let wait=false;
  const c=context(path=>path===workbench&&wait?pending.promise:undefined);
  await setup(c);text(c,'model-json','{my unfinished draft');wait=true;
  const load=c.ui.activate();assert.match(c.ui.view(),/aria-busy="true"/);
  assert.match(c.ui.view(),/role="status">저장 작업과 ISP 목록을 확인 중/);
  await click(c,'replay');assert.equal(c.calls.filter(x=>x.body!==undefined).length,0);
  assert.match(c.ui.view(),/\{my unfinished draft/);
  pending.resolve(catalogue());await load;
  assert.match(c.ui.view(),/aria-busy="false"/);assert.doesNotMatch(c.ui.view(),/role="status"/);
  assert.match(c.ui.view(),/\{my unfinished draft/);
});

test('a superseded catalogue failure cannot clear the latest catalogue or show its error',async()=>{
  const old=deferred(),fresh=deferred();let count=0;
  const c=context(path=>path===workbench?(++count===1?old.promise:fresh.promise):undefined);
  const first=c.ui.activate(),second=c.ui.activate();
  fresh.resolve({...catalogue(),comparisons:[projectHistory(record('latest',true))]});await second;
  old.reject(Error('obsolete catalogue error'));await first;
  assert.match(c.ui.view(),/result &lt;latest&gt;/);assert.doesNotMatch(c.ui.view(),/obsolete catalogue error/);
  assert.match(c.ui.view(),/aria-busy="false"/);
});

test('a superseded catalogue success cannot replace the latest catalogue',async()=>{
  const old=deferred();let count=0;
  const c=context(path=>path===workbench?(++count===1?old.promise:{...catalogue(),comparisons:[projectHistory(record('new',true))]}):undefined);
  const first=c.ui.activate();await c.ui.activate();
  old.resolve({...catalogue(),comparisons:[projectHistory(record('old',true))]});await first;
  assert.match(c.ui.view(),/result &lt;new&gt;/);assert.doesNotMatch(c.ui.view(),/result &lt;old&gt;/);
});

for(const [label,corrupt] of [
  ['schema',x=>{x.schema='old';}],
  ['discovery scope',x=>{x.source_discovery.raw_integrity='physically_validated';}],
  ['missing list',x=>{delete x.comparisons;}],
  ['history detail path',x=>{x.replays=[{...projectHistory(record()),detail_path:'/replays/../session'}];}],
  ['history hash',x=>{x.replays=[{...projectHistory(record()),full_record_sha256:'unverified'}];}],
  ['unsupported profile',x=>{x.models[0].payload.schema='different-profile';}],
])test('invalid composite '+label+' fails closed without legacy fallback and preserves drafts',async()=>{
  let fail=false;const c=context(path=>{if(path===workbench&&fail){const result=catalogue();corrupt(result);return result;}});
  await setup(c);text(c,'model-json','{local draft');await change(c,'history','/replays/R');
  fail=true;await click(c,'load');assert.match(c.ui.view(),/목록의 형식 또는 검증 범위/);
  assert.doesNotMatch(c.ui.view(),/<img class="preview-image"/);assert.match(c.ui.view(),/\{local draft/);
  assert.equal(c.calls.filter(x=>['/retained-isp/sources','/models','/replays',base].includes(x.path)&&x.body===undefined).length,0);
});

test('accepted catalogue does not retain mutable response aliases',async()=>{
  const saved={...catalogue(),replays:[projectHistory(record())]},c=context(path=>path===workbench?saved:undefined);
  await setup(c);saved.replays[0].name='external changed name';saved.models[0].payload.spatial_filter.mode='bypass';
  saved.models[0].payload.temporal_filter.mode='bypass';saved.models[0].payload.detail.mode='bypass';
  assert.doesNotMatch(c.ui.view(),/external changed name/);await click(c,'comparison-template');
  assert.match(c.ui.view(),/temporal_filter.previous_weight/);assert.match(c.ui.view(),/spatial_filter.strength/);
});

for(const comparison of [false,true])for(const terminal of ['succeeded','failed','cancelled'])test('late queued catalogue preserves newer '+terminal+' '+(comparison?'comparison':'replay')+' detail status',async()=>{
  const pending=deferred();let delay=false,status='queued';const collection=comparison?'comparisons':'replays',path=(comparison?base:'/replays')+'/R';
  const initial={...catalogue(),[collection]:[projectHistory(record('R',comparison,'queued'))]};
  const c=context(request=>request===workbench?(delay?pending.promise:initial):request===path?record('R',comparison,status):undefined);
  await c.ui.activate();await change(c,'history',path);delay=true;const load=c.ui.activate();
  status=terminal;await change(c,'history',path);pending.resolve(initial);await load;
  const options=c.ui.view().match(/<select id="retainedisp-history"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.match(options,new RegExp('result &lt;R&gt; · '+terminal+'<\\/option>'));
  assert.doesNotMatch(options,/· queued<\/option>/);assert.equal(c.timers.size,0);
});

test('fresh completed catalogue replaces an older queued history row',async()=>{
  let status='queued';const c=context(request=>request===workbench?{...catalogue(),replays:[projectHistory(record('R',false,status))]}:undefined);
  await c.ui.activate();status='succeeded';await c.ui.activate();
  const options=c.ui.view().match(/<select id="retainedisp-history"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.match(options,/result &lt;R&gt; · succeeded<\/option>/);
  assert.doesNotMatch(options,/· queued<\/option>/);
});

const aeSource=()=>({...source(),source_plugin_id:'electrical_control_v1',acquisition_mode:'closed_loop',actual_frame_conditions:[{frame_id:'frame-0',frame_start_ns:-1000000,exposure_ns:2000000},{frame_id:'frame-1',frame_start_ns:3000000,exposure_ns:1000000}]});
const aeRecord=(comparison=false)=>{
  const r=record(comparison?'C':'R',comparison),summary=r.result.summary;
  Object.assign(summary,{source_plugin_id:'electrical_control_v1',acquisition_mode:'closed_loop',control_trace_role:'evidence_only_not_processing_inputs',planning_contract:{initial_exposure_ns:2000000},realized_acquisition:{schema:'actual-acquisition'},control_trace:{policy:{enabled:true},initial_settings:{exposure_ns:2000000},frames:[0,1].map(i=>({frame_id:'frame-'+i,index:i,latch_at_ns:i*4000000-1000000,applied:{exposure_ns:i?1000000:2000000,gain:1},applied_command_id:i?'command-0':null,proposal:{status:'commanded',commanded:{exposure_ns:i?500000:1000000}}})),commands:[{command_id:'command-0',status:'applied'}]}});
  return r;
};

test('AE source discovery explicitly displays saved actual exposures and keeps the same selected-RAW request',async()=>{
  const c=context(path=>path===workbench?{...catalogue(),sources:[aeSource()]}:undefined);await c.ui.selectSource('A');
  assert.match(c.ui.view(),/전기 노출 제어 획득/);assert.match(c.ui.view(),/frame-0 2000000 ns → frame-1 1000000 ns/);assert.match(c.ui.view(),/선택 시 원본 RAW·제어 이력을 검증/);
  await change(c,'model','M');await click(c,'replay');const sent=c.calls.find(x=>x.path==='/replays'&&x.body).body;
  assert.equal(sent.source_id,'A');assert.deepEqual(sent.selector,source().selector);assert.equal(Object.hasOwn(sent,'control_trace'),false);assert.equal(Object.hasOwn(sent,'exposure_normalization'),false);
});

test('AE selected-frame actual exposure differs from its new command and original plan',async()=>{
  const c=context(path=>path==='/replays/R'?aeRecord():undefined);await change(c,'history','/replays/R');await change(c,'frame','1');const html=c.ui.view();
  assert.match(html,/실제 적용 노출 <strong>1000000 ns<\/strong>/);assert.match(html,/제안한 노출: 500000 ns/);assert.match(html,/적용 명령 command-0/);assert.match(html,/적용 latch 3000000 ns/);
  assert.match(html,/제어 명령과 계획은 별도 근거/);assert.match(html,/노출 정규화는 수행하지 않습니다/);assert.match(html,/설정이 바뀌면 시간 필터 상태를 재설정/);assert.match(html,/2000000/);
});

test('AE comparison uses selected original frame identity for actual command evidence',async()=>{
  const c=context(path=>path===base+'/C'?aeRecord(true):undefined);await change(c,'history',base+'/C');await change(c,'frame','1');
  assert.match(c.ui.view(),/실제 적용 노출 <strong>1000000 ns<\/strong>/);assert.match(c.ui.view(),/artifacts\/candidate-0001/);
});

test('incomplete or paired AE metadata refuses the catalogue without a legacy fallback',async()=>{
  for(const mutate of [x=>delete x.actual_frame_conditions,x=>x.actual_frame_conditions[0].exposure_ns=0,x=>x.selector.readout_id='HCG',x=>delete x.source_plugin_id]){
    const a=aeSource();mutate(a);const c=context(path=>path===workbench?{...catalogue(),sources:[a]}:undefined);await c.ui.activate();
    assert.match(c.ui.view(),/목록의 형식 또는 검증 범위/);assert.equal(c.calls.length,1);assert.doesNotMatch(c.ui.view(),/저장된 실제 적용 노출:/);
  }
});

test('AE trace is bound to selected frame index and evidence-only role; text remains escaped',async()=>{
  const r=aeRecord(),c=context(path=>path==='/replays/R'?r:undefined);r.result.summary.control_trace.frames[0].applied_command_id='<script>unsafe</script>';
  await change(c,'history','/replays/R');assert.match(c.ui.view(),/적용 명령 &lt;script&gt;unsafe&lt;\/script&gt;/);assert.doesNotMatch(c.ui.view(),/<script>unsafe/);
  r.result.summary.control_trace.frames[0].index=1;await change(c,'history','/replays/R');assert.match(c.ui.view(),/실제 AE 적용 이력을 확인할 수 없습니다/);assert.doesNotMatch(c.ui.view(),/data-retainedisp-acquisition/);
  r.result.summary.control_trace.frames[0].index=0;r.result.summary.control_trace_role='processing_input';await change(c,'history','/replays/R');assert.doesNotMatch(c.ui.view(),/data-retainedisp-acquisition/);
});

test('historical retained replay has no invented electrical acquisition trace',async()=>{
  const c=context();await setup(c);await change(c,'history','/replays/R');assert.doesNotMatch(c.ui.view(),/data-retainedisp-acquisition/);assert.match(c.ui.view(),/src="\/api\/v1\/artifacts\/replay0"/);
});


test('disabled electrical policy is labelled fixed settings rather than active AE',async()=>{
  const r=aeRecord();r.result.summary.control_trace.policy.enabled=false;
  for(const f of r.result.summary.control_trace.frames){f.applied.exposure_ns=2000000;f.applied_command_id=null;f.proposal={status:'disabled',commanded:null};}
  const c=context(path=>path==='/replays/R'?r:undefined);await change(c,'history','/replays/R');
  assert.match(c.ui.view(),/AE 정책: 비활성 · 초기 고정 설정/);assert.doesNotMatch(c.ui.view(),/AE 정책: 활성/);
  assert.match(c.ui.view(),/실제 적용 노출 <strong>2000000 ns<\/strong>/);assert.match(c.ui.view(),/새 노출 명령 없음/);
});

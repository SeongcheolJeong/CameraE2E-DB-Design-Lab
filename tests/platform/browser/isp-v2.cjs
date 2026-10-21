'use strict';

// UI contract tests exercise the actual module with local API stubs only.
// They are neither browser observations nor ISP numerical/physical tests.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const sandbox={window:{},TextEncoder};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('src/camera_platform/static/isp-v2.js','utf8'),sandbox);
const clone=value=>JSON.parse(JSON.stringify(value));
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const source=(id='A')=>({source_kind:'run',source_id:id,raw_sha256:id.repeat(64),name:'native <'+id+'>',raw_shape:[2,8,8],raw_format:{hdr_streams:1},geometry:{kind:'native_sensor',full_sensor_shape:[8,8],crop_origin:[0,0]}});
function calibration(id='A') {
  return {name:'cal <'+id+'>',revision:'1',source_kind:'estimated',source_uri:'fixture://declared',physical_instance:null,method_and_version:'fixture:1',parents:[],reference_source:{kind:'run',id},binding:{sensor:'S'},domain:{conditions:{}},instrument:null,dataset_ids:[],dataset_role:'development',uncertainty:{kind:'unknown',reason:'declared'},evidence_refs:[],payload:{schema:'camera_isp_calibration_v1',sites:['R','Gr','Gb','B'],lsc:{schema:'native_four_site_grid_v1',row_nodes:[0,7],column_nodes:[0,7],gains:[[[1,1,1,1],[1,1,1,1]],[[1,1,1,1],[1,1,1,1]]],valid_mask:Array.from({length:2},()=>Array.from({length:2},()=>[true,true,true,true]))},noise:{slope_dn:[1,1,1,1],intercept_dn2:[4,4,4,4]}}};
}
function model(calibrationId=null) {
  return {name:'ISP <v2>',revision:'1',kind:'isp',source_kind:'estimated',source_uri:'fixture://profile',physical_instance:null,method_and_version:'fixture:1',payload:{schema:'camera_isp_profile_v2',graph:['black_level','spatial_lsc','raw_denoise','demosaic','white_balance','ccm','linear_sharpen','tone'],spatial_lsc:{mode:'bypass'},raw_denoise:{mode:'bypass',strength:.5,range_floor_dn2:1},linear_sharpen:{mode:'bypass',amount:.25,threshold:.01,max_delta:.1},calibration_id:calibrationId}};
}
function context(handler,refresh=async()=>{}) {
  let chosen=source(),renderCount=0;const calls=[],models=[],notices=[];
  const ui=sandbox.window.CameraAdvancedISPUI({api:async(path,body)=>{calls.push({path,body:body===undefined?undefined:clone(body)});return handler?handler(path,body):path.includes('isp-calibration?')?calibration(chosen.source_id):path.includes('isp-profile?')?model(new URL('http://test'+path).searchParams.get('calibration_id')):[];},esc,render:()=>renderCount++,refresh,toast:m=>notices.push(m),source:()=>chosen,onModel:m=>models.push(clone(m))});
  return {ui,calls,models,notices,setSource:v=>{chosen=v;},renders:()=>renderCount};
}
const click=(ui,action)=>ui.click({dataset:{ispv2Action:action}});
const text=(ui,kind,value)=>ui.input({dataset:{ispv2Text:kind},value});
const edit=(ui,kind,path,value,type='number')=>ui.input({dataset:{ispv2Edit:kind,path},value:String(value),type});
const submit=(ui,kind)=>ui.submit({dataset:{ispv2Form:kind}});
const select=(ui,field,value)=>ui.change({dataset:{ispv2Field:field},value});
const mode=(ui,name,checked)=>ui.change({dataset:{ispv2Mode:name},checked});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

test('v2 is available only for native single-stream RAW and leaves v1 execution outside its editor',async()=>{
  const c=context();assert.match(c.ui.editor(),/RAW SHA-256/);assert.match(c.ui.editor(),/native &lt;A&gt;/);
  c.setSource({...source(),geometry:{kind:'representative_raster'}});assert.doesNotMatch(c.ui.editor(),/data-ispv2-form/);assert.match(c.ui.editor(),/기존 v1/);
  await click(c.ui,'profile-template');assert.equal(c.calls.length,0);
  c.setSource({...source(),raw_shape:[2,2,8,8],raw_format:{hdr_streams:2}});assert.doesNotMatch(c.ui.editor(),/data-ispv2-form/);
  assert.equal(c.ui.result({profile:{schema:'camera_isp_profile_v1'}}),'');
});

test('calibration template and profile requests pin selected RAW identities, never global assembly',async()=>{
  const c=context();await click(c.ui,'calibration-template');await select(c.ui,'calibrationId','C /&');await click(c.ui,'profile-template');
  assert.equal(c.calls[0].path,'/templates/isp-calibration?source_kind=run&source_id=A');
  assert.equal(c.calls[1].path,'/templates/isp-profile?version=2&source_kind=run&source_id=A&calibration_id=C%20%2F%26');
  const html=c.ui.editor();assert.match(html,/수치 편집은 추정 가정/);assert.match(html,/payload\.lsc\.gains\.0\.0\.1/);assert.match(html,/payload\.lsc\.gains\.0\.0\.2/);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size);
});

test('source and RAW digest switch retain separate unfinished drafts and ignore late template',async()=>{
  const wait=deferred(),c=context(()=>wait.promise),request=click(c.ui,'calibration-template');
  c.setSource(source('B'));text(c.ui,'calibration','{unfinished B');wait.resolve(calibration('A'));await request;
  assert.match(c.ui.editor(),/\{unfinished B/);assert.doesNotMatch(c.ui.editor(),/cal &lt;A&gt;/);
  c.setSource(source('A'));assert.doesNotMatch(c.ui.editor(),/value="cal &lt;A&gt;"/);
  text(c.ui,'calibration','{unfinished A');c.setSource({...source('A'),raw_sha256:'new'});assert.doesNotMatch(c.ui.editor(),/\{unfinished A/);
  c.setSource(source('A'));assert.match(c.ui.editor(),/\{unfinished A/);
});

test('calibration numeric edits preserve distinct Gr/Gb but invalidate measured provenance',async()=>{
  const original=calibration();Object.assign(original,{source_kind:'measured',source_uri:'bench://real',physical_instance:'camera-serial',parents:['parent-cal']});
  const c=context(async(path,body)=>body?{...body,id:'C'}:[]);text(c.ui,'calibration',JSON.stringify(original));
  edit(c.ui,'calibration','payload.lsc.gains.0.0.1',1.2);edit(c.ui,'calibration','payload.lsc.gains.0.0.2',1.7);
  await submit(c.ui,'calibration');const body=c.calls.find(x=>x.body).body;
  assert.deepEqual(body.payload.lsc.gains[0][0],[1,1.2,1.7,1]);assert.equal(body.source_kind,'estimated');assert.equal(body.physical_instance,null);assert.equal(body.instrument,null);assert.notEqual(body.source_uri,'bench://real');assert.equal(body.uncertainty.kind,'unknown');assert.deepEqual(body.parents,['parent-cal']);
  assert.equal(original.payload.lsc.gains[0][0][1],1);assert.match(c.ui.editor(),/value="C" selected/);
});

test('invalid JSON and empty numeric input cannot silently submit prior calibration values',async()=>{
  const c=context();await click(c.ui,'calibration-template');edit(c.ui,'calibration','payload.noise.slope_dn.0','');await submit(c.ui,'calibration');
  assert.equal(c.calls.filter(x=>x.body).length,0);assert.match(c.ui.editor(),/이전 수치로 등록하지 않았습니다/);
  assert.match(c.ui.editor(),/data-path="payload\.noise\.slope_dn\.0" type="number" value=""/);
  text(c.ui,'calibration','{"broken":');await submit(c.ui,'calibration');assert.equal(c.calls.filter(x=>x.body).length,0);assert.match(c.ui.editor(),/\{&quot;broken&quot;:/);
});

test('wrong reference source and altered selected calibration are rejected before POST',async()=>{
  const c=context();text(c.ui,'calibration',JSON.stringify(calibration('B')));await submit(c.ui,'calibration');assert.equal(c.calls.length,0);
  text(c.ui,'profile',JSON.stringify(model('wrong')));await submit(c.ui,'profile');assert.equal(c.calls.length,0);assert.match(c.ui.editor(),/보정 버전이 다릅니다/);
});

test('stage toggles require calibration for LSC and denoise, with per-calibration profile drafts',async()=>{
  const c=context();await click(c.ui,'profile-template');await mode(c.ui,'spatial_lsc',true);assert.match(c.ui.editor(),/보정 버전을 먼저 고정/);
  await mode(c.ui,'linear_sharpen',true);assert.match(c.ui.editor(),/payload\.linear_sharpen\.amount/);
  edit(c.ui,'profile','payload.linear_sharpen.amount',.7);
  await select(c.ui,'calibrationId','C');assert.doesNotMatch(c.ui.editor(),/data-path="payload\.linear_sharpen\.amount"/);
  await click(c.ui,'profile-template');await mode(c.ui,'raw_denoise',true);assert.match(c.ui.editor(),/payload\.raw_denoise\.strength/);
  await select(c.ui,'calibrationId','');assert.match(c.ui.editor(),/data-path="payload\.linear_sharpen\.amount" type="number" value="0.7"/);
});

test('a stored ISP is selected once, while failed refresh retains completion and blocks duplicate UI submit',async()=>{
  const c=context(async(path,body)=>body?{...body,id:'ISP'}:model(),async()=>{throw Error('offline refresh');});
  await click(c.ui,'profile-template');await submit(c.ui,'profile');
  assert.equal(c.models.length,1);assert.equal(c.models[0].id,'ISP');assert.match(c.ui.editor(),/등록은 완료되었습니다/);
  assert.match(c.ui.editor(),/<button type="submit" class="button primary full" disabled/);
  await submit(c.ui,'profile');assert.equal(c.calls.filter(x=>x.body).length,1);
  assert.equal(c.calls.filter(x=>x.path==='/replays').length,0);
});

test('late registered model never changes the selected RAW source profile',async()=>{
  const wait=deferred(),c=context(async(path,body)=>body?wait.promise:model());await click(c.ui,'profile-template');
  const request=submit(c.ui,'profile');c.setSource(source('B'));wait.resolve({...model(),id:'ISP-A'});await request;
  assert.equal(c.models.length,0);assert.doesNotMatch(c.ui.editor(),/ISP-A/);c.setSource(source('A'));assert.match(c.ui.editor(),/불변 버전을 등록했습니다/);
});

test('unknown POST result is reconciled by exact saved content without a second mutation',async()=>{
  let saved;const c=context(async(path,body)=>{
    if(path.includes('/templates/'))return model();
    if(body){saved={...clone(body),id:'ISP-committed'};throw Error('lost response');}
    return [saved];
  });
  await click(c.ui,'profile-template');await submit(c.ui,'profile');assert.match(c.ui.editor(),/중복 등록을 멈췄습니다/);
  await submit(c.ui,'profile');assert.equal(c.calls.filter(x=>x.body).length,1);
  await click(c.ui,'reconcile');assert.equal(c.models.length,1);assert.equal(c.models[0].id,'ISP-committed');assert.equal(c.calls.filter(x=>x.body).length,1);
});

test('initial definitive server rejection keeps editable draft and visible error',async()=>{
  const c=context(async(path,body)=>{if(body){const error=Error('outside calibration domain');error.status=422;throw error;}return model();});
  await click(c.ui,'profile-template');await submit(c.ui,'profile');assert.match(c.ui.editor(),/outside calibration domain/);assert.doesNotMatch(c.ui.editor(),/중복 등록을 멈췄습니다/);
  edit(c.ui,'profile','name','corrected','text');assert.match(c.ui.editor(),/value="corrected"/);assert.equal(c.models.length,0);
});

test('file import cannot replace a newer source draft after asynchronous read',async()=>{
  const wait=deferred(),c=context();
  const request=c.ui.change({dataset:{ispv2File:'calibration'},files:[{size:500,text:()=>wait.promise}]});
  c.setSource(source('B'));text(c.ui,'calibration','{new B');wait.resolve(JSON.stringify(calibration()));await request;
  assert.match(c.ui.editor(),/\{new B/);assert.doesNotMatch(c.ui.editor(),/cal &lt;A&gt;/);
});

function job(id='J') {
  return {id,name:'replay <unsafe>',status:'succeeded',profile:model().payload,artifacts:[{name:'lsc_normalized_frames.npy',url:'/api/v1/artifacts/abc',sha256:'abc'},{name:'lsc_bad.npy',url:'javascript:alert(1)',sha256:'bad'}],result:{summary:{
    stage_outputs:{spatial_lsc:{array:'lsc_normalized_frames',validity:'lsc_valid_mask_frames',unit:'1',applied:false}},
    calibration_snapshot:{id:'C',source_kind:'estimated',uncertainty:{kind:'unknown'}},
    stage_previews:[0,1].flatMap(frame_index=>['baseline_rgb_frames','rgb_frames'].map(array=>({array,frame_index,preview_url:`/api/v1/artifacts/${id}-${array}-${frame_index}`,display_range:[0,1],display_transfer:'clamp_normalized_values_no_autoscale'})))
  }}};
}
test('result compares the same stored frame with fixed display scale and only actual stage artifacts',async()=>{
  const c=context();let html=c.ui.result(job());assert.match(html,/replay &lt;unsafe&gt;/);assert.match(html,/J-baseline_rgb_frames-0/);assert.match(html,/J-rgb_frames-0/);assert.match(html,/>bypass</);assert.match(html,/lsc_valid_mask_frames/);assert.doesNotMatch(html,/javascript:/);
  await c.ui.change({dataset:{ispv2Result:'J'},value:'1'});html=c.ui.result(job());assert.match(html,/J-baseline_rgb_frames-1/);assert.doesNotMatch(html,/<img[^>]*-0/);
  assert.match(c.ui.result(job('other')),/other-baseline_rgb_frames-0/);assert.match(html,/총 불확실도/);assert.match(html,/estimated/);
  const inconsistent=job();inconsistent.result.summary.stage_previews[3].display_range=[0,2];assert.doesNotMatch(c.ui.result(inconsistent),/<img/);
  assert.doesNotMatch(c.ui.result({...job(),status:'running'}),/<img|download/);
});

test('unrelated host input and events remain unhandled',async()=>{
  const c=context();assert.equal(c.ui.input({dataset:{}}),false);assert.equal(await c.ui.change({dataset:{}}),false);assert.equal(await c.ui.click({dataset:{}}),false);assert.equal(await c.ui.submit({dataset:{}}),false);assert.equal(c.renders(),0);assert.equal(c.calls.length,0);
});

test('stage table follows frozen processing graph despite canonical object key order',()=>{
  const c=context(),run=job(),graph=clone(run.profile.graph);
  run.result.summary.stage_outputs=Object.fromEntries([...graph].sort().map(name=>[name,{array:name+'_frames',validity:name+'_valid',unit:'1',applied:true}]));
  run.result.summary.stage_outputs.unrecognized_stage={array:'extra',applied:true};
  const rows=html=>[...html.match(/<tbody>(.*?)<\/tbody>/s)[1].matchAll(/<tr><td>(.*?)<\/td>/g)].map(match=>match[1]);
  assert.deepEqual(rows(c.ui.result(run)),[...graph,'unrecognized_stage · 처리 순서 미확인']);
  delete run.profile;run.result.summary.profile={schema:'camera_isp_profile_v2'};run.result.summary.graph=graph;
  assert.deepEqual(rows(c.ui.result(run)),[...graph,'unrecognized_stage · 처리 순서 미확인']);
});

const nonlinearSource=(path='hcg')=>({...source('DCG'),selector:{schema:'camera_nonlinear_raw_selection_v1',mode:'shared_charge_path',readout_id:path},raw_format:{hdr_streams:2},raw_container_shape:[2,2,8,8]});
const nonlinearModel=()=>{const m=model();m.payload.schema='camera_isp_profile_v3';m.payload.graph[0]='charge_decode';m.payload.raw_denoise={mode:'bypass',strength:.5,range_floor_e2:4};return m;};
test('nonlinear profile binds explicit DCG path and permits decoded-noise denoise without LSC calibration',async()=>{
  const c=context(async(path,body)=>body?{...body,id:'V3'}:nonlinearModel());c.setSource(nonlinearSource());
  await click(c.ui,'profile-template');const query=new URL('http://t'+c.calls[0].path).searchParams;
  assert.equal(query.get('version'),'3');assert.deepEqual(JSON.parse(query.get('selector')),nonlinearSource().selector);
  await mode(c.ui,'raw_denoise',true);assert.match(c.ui.editor(),/range_floor_e2/);assert.match(c.ui.editor(),/전자²/);assert.doesNotMatch(c.ui.editor(),/range_floor_dn2/);
  await submit(c.ui,'profile');assert.equal(c.models[0].payload.raw_denoise.mode,'local_charge_variance_filter_v1');assert.equal(c.models[0].payload.calibration_id,null);
});
test('DCG path drafts are distinct, stable under selector object order, and reject opposite-path calibration',async()=>{
  const c=context();c.setSource(nonlinearSource('hcg'));text(c.ui,'calibration','{HCG unfinished');
  c.setSource(nonlinearSource('lcg'));assert.doesNotMatch(c.ui.editor(),/HCG unfinished/);text(c.ui,'calibration','{LCG unfinished');
  c.setSource({...nonlinearSource('hcg'),selector:{readout_id:'hcg',mode:'shared_charge_path',schema:'camera_nonlinear_raw_selection_v1'}});assert.match(c.ui.editor(),/HCG unfinished/);
  const doc=calibration('DCG');doc.schema='camera_charge_isp_calibration_v1';doc.reference_source.selector=nonlinearSource('lcg').selector;text(c.ui,'calibration',JSON.stringify(doc));await submit(c.ui,'calibration');assert.equal(c.calls.filter(x=>x.body).length,0);assert.match(c.ui.editor(),/경로·RAW 선택/);
});
test('charge LSC requires its own calibration and legacy profile cannot be posted for nonlinear source',async()=>{
  const c=context(async()=>nonlinearModel());c.setSource(nonlinearSource());await click(c.ui,'profile-template');await mode(c.ui,'spatial_lsc',true);assert.match(c.ui.editor(),/보정 버전을 먼저/);
  text(c.ui,'profile',JSON.stringify(model()));await submit(c.ui,'profile');assert.equal(c.calls.filter(x=>x.body).length,0);assert.match(c.ui.editor(),/RAW에 맞는 ISP 버전/);
});

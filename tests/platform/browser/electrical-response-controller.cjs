'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const click=action=>({dataset:{electricalresponseAction:action}});
const field=(name,value)=>({dataset:{electricalresponseField:name},value});
function setup(api){
  const timers=[],buttons=[{disabled:false}],box={URLSearchParams,setTimeout:fn=>{timers.push(fn);return timers.length;},document:{querySelectorAll:()=>buttons}};
  vm.createContext(box);
  for(const file of ['electrical-response.js','electrical-response-controller.js'])vm.runInContext(fs.readFileSync('src/camera_platform/static/'+file,'utf8'),box);
  const data={assemblies:[{id:'a',name:'Camera'}],scene_sequences:[{id:'s',assembly_id:'a',name:'Scene'}],control_sequences:[{id:'c',assembly_id:'a',scene_sequence_id:'s',name:'Start'}],capture_programs:[{id:'p',assembly_id:'a',name:'Policy',payload:{schema:'camera_electrical_control_capture_program_v1'}}]};
  const controller=box.CameraElectricalResponseController({api,esc,records:kind=>data[kind]||[],render:()=>{},isActive:()=>true});
  return {controller,timers,buttons};
}
const completed=(id,name)=>({id,name,status:'succeeded',artifacts:[],result:{summary:{cells:[]}}});

test('late selected-record response never replaces the newer selection and its fetch is retained',async()=>{
  const pending=deferred();
  const {controller,timers}=setup(async path=>{
    if(path==='/electrical-response/studies')return [{id:'a-run',status:'running',name:'Old'}];
    if(path.endsWith('/a-run'))return pending.promise;
    if(path.endsWith('/b-run'))return completed('b-run','New result');
    throw Error(path);
  });
  const first=controller.activate();await Promise.resolve();await Promise.resolve();
  await controller.change(field('history','b-run'));
  pending.resolve(completed('a-run','Stale result'));await first;
  assert.doesNotMatch(controller.view(),/<h3>Stale result<\/h3>/);
  assert.ok(timers.length);
  await timers.shift()();
  assert.match(controller.view(),/<h3>New result<\/h3>/);
});

test('failed current detail verification removes cached completed results and keeps retry error',async()=>{
  let deny=false;
  const {controller}=setup(async path=>{
    if(path==='/electrical-response/studies')return [{id:'a-run',status:'succeeded'}];
    if(deny)throw Error('RAW checksum denied');
    return completed('a-run','Verified result');
  });
  await controller.activate();assert.match(controller.view(),/<h3>Verified result<\/h3>/);
  deny=true;await controller.click(click('record-refresh'));
  assert.doesNotMatch(controller.view(),/<h3>Verified result<\/h3>/);
  assert.match(controller.view(),/RAW checksum denied/);
  assert.match(controller.view(),/아직 확인하지 못한 결과/);
});

test('late template failure cannot overwrite a newer authored draft',async()=>{
  const pending=deferred();
  const {controller}=setup(async path=>path==='/electrical-response/studies'?[]:pending.promise);
  await controller.activate();const load=controller.click(click('template'));
  controller.input(field('json','{new incomplete author draft'));
  pending.reject(Error('old template failed'));await load;
  assert.match(controller.view(),/new incomplete author draft/);
  assert.doesNotMatch(controller.view(),/old template failed/);
});

test('editing input while a plan is pending immediately disables submission and drops stale plan',async()=>{
  const pending=deferred(),spec={schema:'camera_electrical_response_study_v1',assembly_id:'a',scene_sequence_id:'s',control_sequence_id:'c',capture_program_id:'p',name:'My study',axes:[],seeds:[1],noise_enabled:false,evaluation:{}};
  const {controller,buttons}=setup(async path=>path==='/electrical-response/studies'?[]:pending.promise);
  await controller.activate();controller.input(field('json',JSON.stringify(spec)));
  const plan=controller.click(click('plan'));controller.input(field('json','{changed'));
  assert.equal(buttons[0].disabled,true);
  pending.resolve({plan_hash:'a'.repeat(64),frozen_request:{specification:spec}});await plan;
  assert.match(controller.view(),/\{changed/);
  assert.doesNotMatch(controller.view(),/data-electricalresponse-plan=/);
});

test('older history response cannot replace a freshly verified record status',async()=>{
  const pending=deferred();let loads=0;
  const {controller}=setup(async path=>{
    if(path==='/electrical-response/studies')return ++loads===1?[{id:'a-run',status:'running',name:'Old listing'}]:pending.promise;
    return completed('a-run','Fresh verified record');
  });
  await controller.activate();const refresh=controller.activate();
  await controller.click(click('record-refresh'));
  pending.resolve([{id:'a-run',status:'running',name:'Stale listing'}]);await refresh;
  assert.doesNotMatch(controller.view(),/Stale listing/);
  assert.match(controller.view(),/Fresh verified record/);
});

'use strict';

// Pure rendering/handler contract checks. No browser, API or mutable registry.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const test=require('node:test');
const sandbox={window:{},structuredClone};
vm.createContext(sandbox);
const app=fs.readFileSync('src/camera_platform/static/app.js','utf8');
vm.runInContext(app.slice(0,app.indexOf('(() => {')),sandbox);
vm.runInContext(fs.readFileSync('src/camera_platform/static/dcg.js','utf8'),sandbox);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const profile=()=>({schema:'camera_dcg_merge_v1',method:'nonnegative_read_covariance_raw',saturation_margin_dn:1,read_noise_guard_sigma:3,gain_path_agreement_sigma:5,tie_break_stream:'HCG'});
function document(id='A') {
  return {name:'DCG <source>',revision:'1',assembly_id:id,payload:{schema:'camera_dcg_capture_program_v1',merge_profile:profile(),electrical_model:{
    shared_charge_capacity_e:1000,dark_current_e_per_s:2,reference_temperature_c:25,dark_current_doubling_c:null,
    paths:['HCG','LCG'].map((stream_id,i)=>({stream_id,conversion_gain_v_per_e:4/(i+1),analog_gain:1,input_linear_ceiling_e:200*(i+1),digital_gain:1,adc:{bit_depth:12,full_scale_v:4095,black_level_dn:64+i}})),
    fixed_pattern:{prnu_sigma:0,dsnu_e_per_s:0,seed:1},
    read_noise:{covariance_e2:[[4,1],[1,9]],temperature_range_c:[25,25]}
  }}};
}
function measuredDocument(id='A') {
  const doc=document(id);
  Object.assign(doc,{source_kind:'measured',physical_instance:'bench-camera-1',source_uri:'measurement://original',method_and_version:'paired-measurement:1',parents:['original-program']});
  doc.payload.electrical_model.source={kind:'measured',description:'Original measured electrical settings'};
  doc.payload.electrical_model.read_noise.source={kind:'measured',description:'Measured zero cross covariance; original path gains'};
  doc.payload.electrical_model.read_noise.covariance_e2=[[4,0],[0,16]];
  return doc;
}
function context() {
  const state={assembly:'A',captureProgram:'',captureError:'',captureDrafts:sandbox.CameraCaptureDrafts()};
  let renders=0;
  const ui=sandbox.window.CameraDCGUI({state,esc,array:v=>Array.isArray(v)?v:[],json:JSON.stringify,number:v=>Number.isFinite(Number(v))?String(v):'—',badge:esc,details:(title,value)=>`<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(value))}</pre></details>`,options:()=>'',matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r?.result?.summary||{},empty:(a,b)=>`<p>${esc(a)} ${esc(b)}</p>`,render:()=>renders++});
  return {state,ui,renders:()=>renders};
}
function run(id='R',merge=true) {
  return {id,status:'succeeded',result:{summary:{acquisition_count:2,merge_applied:merge,
    acquisitions:[0,1].map(i=>({acquisition_id:`acq${i}`,exposure_s:.002*(i+1),merged_preview_url:`/${id}-${i}-merged.png`,readouts:['HCG','LCG'].map(stream_id=>({stream_id,preview_url:`/${id}-${i}-${stream_id}.png`}))})),
    prediction:{axes:[{name:'acquisition',values:['acq0','acq1']},{name:'readout',values:['HCG','LCG']},{name:'channel',values:['R','Gr','Gb','B']}],values:[11,12,13,14,21,22,23,24,31,32,33,34,41,42,43,44]},
    dcg_merge:merge?[{valid_pixels:10},{valid_pixels:20}]:null
  }}};
}
const preview=html=>html.match(/<img src="([^"]+)"/)?.[1];
const table=html=>html.match(/<table>(.*?)<\/table>/s)?.[1];

test('DCG authoring keeps two path fields, native sources escaped, and no sequential HDR controls',()=>{
  const {ui}=context(),doc=document(),html=ui.editor(doc,JSON.stringify(doc),'');
  assert.match(html,/payload\.electrical_model\.paths\.0\.analog_gain/);
  assert.match(html,/payload\.electrical_model\.paths\.1\.analog_gain/);
  assert.match(html,/value="DCG &lt;source&gt;"/);
  assert.match(html,/capture-dcg-merge/);
  assert.doesNotMatch(html,/id="capture-hdr-enabled"|id="capture-pwm-enabled"/);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(ids.length,new Set(ids).size);
});

test('acquisition and readout navigation preserve four site table values and per-run state',()=>{
  const {ui}=context();
  assert.equal(preview(ui.panel(run())),'/R-0-merged.png');
  assert.equal(ui.change({id:'dcg-acquisition',dataset:{dcgRun:'R'},value:'1'}),true);
  assert.equal(ui.change({id:'dcg-readout',dataset:{dcgRun:'R'},value:'LCG'}),true);
  const html=ui.panel(run());
  assert.equal(preview(html),'/R-1-LCG.png');
  assert.match(table(html),/<th>Gr · DN<\/th><th>Gb · DN<\/th>/);
  assert.match(table(html),/<td>31<\/td><td>32<\/td><td>33<\/td><td>34<\/td>/);
  assert.match(table(html),/<td>41<\/td><td>42<\/td><td>43<\/td><td>44<\/td>/);
  assert.doesNotMatch(table(html),/<td>11<\/td>/);
  assert.equal(preview(ui.panel(run('other'))),'/other-0-merged.png');
  assert.match(html,/실제 포화 검출을 뜻하지 않습니다/);
});

test('RAW-only result defaults to HCG and cannot imply that an absent merged image exists',()=>{
  const {ui}=context(),html=ui.panel(run('raw-only',false));
  assert.equal(preview(html),'/raw-only-0-HCG.png');
  assert.doesNotMatch(html,/<option value="merged"/);
  assert.match(html,/RAW 합성 사용 안 함/);
  assert.equal(preview(ui.panel({id:'waiting',status:'queued'})),undefined);
  assert.match(ui.panel({id:'failed',status:'failed',error:'<bad>'}),/&lt;bad&gt;/);
});

test('merge toggle restores edited profile, isolates assemblies, and retains malformed source',()=>{
  const {ui,state,renders}=context(),doc=document();
  state.captureDrafts.set('A',JSON.stringify(doc));ui.editor(doc,JSON.stringify(doc),'');
  state.captureDrafts.update('A','payload.merge_profile.gain_path_agreement_sigma',7);
  assert.equal(ui.change({id:'capture-dcg-merge',checked:false}),true);
  assert.equal(state.captureDrafts.parse('A').payload.merge_profile,null);
  ui.change({id:'capture-dcg-merge',checked:true});
  assert.equal(state.captureDrafts.parse('A').payload.merge_profile.gain_path_agreement_sigma,7);
  state.assembly='B';const second=document('B');second.payload.merge_profile.read_noise_guard_sigma=9;
  state.captureDrafts.set('B',JSON.stringify(second));ui.editor(second,JSON.stringify(second),'');
  ui.change({id:'capture-dcg-merge',checked:false});ui.change({id:'capture-dcg-merge',checked:true});
  assert.equal(state.captureDrafts.parse('B').payload.merge_profile.read_noise_guard_sigma,9);
  assert.equal(state.captureDrafts.parse('A').payload.merge_profile.read_noise_guard_sigma,3);
  state.captureDrafts.set('B','{unfinished');ui.change({id:'capture-dcg-merge',checked:true});
  assert.equal(state.captureDrafts.get('B'),'{unfinished');assert.ok(state.captureError);
  const count=renders();assert.equal(ui.change({id:'unrelated'}),false);assert.equal(renders(),count);
});

test('copying a saved RAW-only DCG program can enable merge without replacing its electrical model',async()=>{
  // Exercise the actual app action against local stubs, including its mode URL
  // and draft-epoch gate, rather than copying its dispatch logic into this test.
  const branch=app.slice(app.indexOf("        case 'capture-copy': {"),app.indexOf("        case 'capture-use':"));
  const copy=vm.runInContext(`(async function(ctx){const {state,selectedCaptureProgram,workbenchData,api,dcgUI,render}=ctx;switch('capture-copy'){${branch}}})`,sandbox);
  const {ui,state}=context(),full={id:'P',...document()};
  full.payload.merge_profile=null;full.payload.electrical_model.shared_charge_capacity_e=12345;
  state.captureProgram='P';let calls=0;
  const defaults=profile();
  await copy({state,selectedCaptureProgram:()=>full,workbenchData:{load:async()=>full},dcgUI:ui,render:()=>{},api:async url=>{
    calls++;assert.match(url,/assembly_id=A&mode=shared_charge$/);return {payload:{merge_profile:defaults}};
  }});
  assert.equal(calls,1);assert.equal(state.captureDrafts.parse('A').payload.merge_profile,null);
  defaults.read_noise_guard_sigma=99; // Remembered profile must be detached.
  ui.change({id:'capture-dcg-merge',checked:true});
  assert.equal(state.captureError,'');
  assert.equal(state.captureDrafts.parse('A').payload.merge_profile.read_noise_guard_sigma,3);
  assert.equal(state.captureDrafts.parse('A').payload.electrical_model.shared_charge_capacity_e,12345);
  assert.equal(state.captureDrafts.parse('A').revision,'1-edit');
  assert.equal(state.captureDrafts.parse('A').parents[0],'P');
  await copy({state,selectedCaptureProgram:()=>full,workbenchData:{load:async()=>full},dcgUI:ui,render:()=>{},api:async()=>{
    state.captureDrafts.set('A','new in-progress input');return {payload:{merge_profile:profile()}};
  }});
  assert.equal(state.captureDrafts.get('A'),'new in-progress input');
});

test('malformed covariance JSON renders an editable error instead of crashing Factory',()=>{
  const {ui}=context();
  for(const covariance of ['12',{length:2},null,[[1],[1]],[[1,0],null]]) {
    const doc=document();doc.payload.electrical_model.read_noise.covariance_e2=covariance;
    const source=JSON.stringify(doc);let html;
    assert.doesNotThrow(()=>{html=ui.editor(doc,source,'');});
    assert.match(html,/입력 원문은 유지됩니다/);
    assert.ok(html.includes(esc(source)));
    assert.match(html,/<button type="submit"[^>]*disabled/);
  }
});

test('actual numeric field handler preserves edited covariance but replaces stale measured provenance',()=>{
  const branch=app.slice(app.indexOf('    if(event.target.dataset.captureField) {'),app.indexOf("    if(event.target.id==='assembly-contract')"));
  const input=vm.runInContext(`(function(ctx,event){const {state,dcgUI,$,toast}=ctx;const json=JSON.stringify;${branch}})`,sandbox);
  const {ui,state}=context(),original=measuredDocument(),other=measuredDocument('B');
  const originalText=JSON.stringify(original),otherText=JSON.stringify(other);
  state.captureDrafts.set('A',originalText);state.captureDrafts.set('B',otherText);
  const source={value:''};
  input({state,dcgUI:ui,$:()=>source,toast:message=>assert.fail(message)},
    {target:{dataset:{captureField:'payload.electrical_model.read_noise.covariance_e2.0.1'},type:'number',value:'-1'}});
  const edited=state.captureDrafts.parse('A');
  assert.equal(state.captureError,'');
  assert.equal(JSON.stringify(edited.payload.electrical_model.read_noise.covariance_e2),'[[4,-1],[-1,16]]');
  assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);
  assert.notEqual(edited.source_uri,'measurement://original');
  assert.equal(edited.payload.electrical_model.source.kind,'estimated');
  assert.equal(edited.payload.electrical_model.read_noise.source.kind,'estimated');
  assert.doesNotMatch(edited.payload.electrical_model.read_noise.source.description,/Measured zero cross covariance/);
  assert.match(edited.payload.electrical_model.read_noise.source.description,/not measured/);
  assert.equal(edited.parents[0],'original-program');
  assert.equal(source.value,state.captureDrafts.get('A'));
  assert.equal(state.captureDrafts.get('B'),otherText);
  assert.equal(JSON.stringify(original),originalText);
});

test('name edits leave physics provenance intact and ordinary v1 drafts remain unchanged',()=>{
  const {ui,state}=context(),doc=measuredDocument();
  state.captureDrafts.set('A',JSON.stringify(doc));
  state.captureDrafts.update('A','name','Renamed program');
  const renamed=state.captureDrafts.get('A');
  ui.markParameterEdit('name');assert.equal(state.captureDrafts.get('A'),renamed);
  assert.equal(state.captureDrafts.parse('A').source_kind,'measured');
  const legacy={name:'legacy',assembly_id:'A',payload:{schema:'camera_capture_program_v1',electrical_model:{digital_gain:2}}};
  state.captureDrafts.set('A',JSON.stringify(legacy));const before=state.captureDrafts.get('A');
  assert.doesNotThrow(()=>ui.markParameterEdit('payload.electrical_model.digital_gain'));
  assert.equal(state.captureDrafts.get('A'),before);
  state.captureDrafts.set('A','{unfinished');
  assert.doesNotThrow(()=>ui.markParameterEdit('name'));
  assert.throws(()=>ui.markParameterEdit('payload.electrical_model.digital_gain'));
  assert.equal(state.captureDrafts.get('A'),'{unfinished');
});

test('dark-temperature checkbox also updates DCG provenance without changing covariance',()=>{
  const begin=app.indexOf("    if(['capture-dark-temperature','capture-hdr-enabled','capture-pwm-enabled'].includes(event.target.id)) {");
  const branch=app.slice(begin,app.indexOf('    const select=event.target.dataset.select;',begin));
  const change=vm.runInContext(`(function(ctx,event){const {state,dcgUI,json,render,selectedSceneSequence}=ctx;${branch}})`,sandbox);
  const {ui,state}=context();state.captureDrafts.set('A',JSON.stringify(measuredDocument()));
  change({state,dcgUI:ui,json:JSON.stringify,render:()=>{},selectedSceneSequence:()=>null},{target:{id:'capture-dark-temperature',checked:true}});
  const edited=state.captureDrafts.parse('A');
  assert.equal(state.captureError,'');assert.equal(edited.source_kind,'estimated');
  assert.equal(edited.physical_instance,null);
  assert.equal(edited.payload.electrical_model.dark_current_doubling_c,6);
  assert.equal(JSON.stringify(edited.payload.electrical_model.read_noise.covariance_e2),'[[4,0],[0,16]]');
});

test('app scopes bind DCG program, scene, settings and test while rejecting HDR topology',()=>{
  const state={runtime:'shared_charge_dcg_v1',assembly:'A',captureProgram:'P',sceneSequence:'S',controlSequence:'C',test:'T'};
  const spec={stimulus:{schema:'shared_charge_dcg_test_v1',capture_program_id:'P',scene_sequence_id:'S',control_sequence_id:'C'}};
  assert.equal(sandbox.CameraCaptureTestMatches(spec,state),true);
  assert.equal(sandbox.CameraCaptureTestMatches({...spec,stimulus:{...spec.stimulus,schema:'electrical_hdr_test_v1'}},state),false);
  for(const key of ['runtime','assembly','captureProgram','sceneSequence','controlSequence','test'])assert.notEqual(sandbox.CameraCaptureScope(state),sandbox.CameraCaptureScope({...state,[key]:'changed'}));
  assert.equal(sandbox.CameraCaptureMatches({capture_program_id:'another'},state),false);
  const {state:draftState}=context(),token=draftState.captureDrafts.snapshot('A');
  draftState.captureDrafts.set('A','user typing');
  assert.equal(draftState.captureDrafts.accept(token,document(),'A'),false);
  assert.equal(draftState.captureDrafts.get('A'),'user typing');
});

test('nonlinear saved DCG means retain dark response and path preview uses fixed color settings',()=>{
  const {ui}=context(),value=run();
  value.result.summary.prediction.method_and_version='sampled_nonlinear_shared_charge_raw_black_relative_v1';
  const html=ui.panel(value);
  assert.match(html,/실제 RAW−black DN 평균/);
  assert.match(html,/비선형 암전류 응답을 빼지 않습니다/);
  assert.match(html,/HCG · 보정된 전하·고정 색 미리보기/);
  assert.match(html,/LCG · 보정된 전하·고정 색 미리보기/);
  assert.doesNotMatch(html,/black·평균 암전하 보정 후/);
  assert.match(ui.panel(run()),/black·평균 암전하 보정 후/);
});

'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const box={structuredClone,TextEncoder};vm.createContext(box);
vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
const schemas=['camera_capture_program_v2','camera_dcg_capture_program_v2','camera_reference_hdr_capture_program_v2'];
const runtimes=['hdr_bracket_v1','shared_charge_dcg_v1','reference_hdr_v1'];
const modes=['independent_bracket_nonlinear','shared_charge_nonlinear','reference_guarded_nonlinear'];
const stimuli=['electrical_hdr_test_v2','shared_charge_dcg_test_v2','reference_hdr_test_v2'];
const curve=()=>({charge_e:[0,1000,2000],signal_voltage_v:[0,.1,.15],valid_domain:{temperature_c:[25,25],analog_gain:[1,1]},signed_inverse:{minimum_voltage_v:-.1,method:'origin_tangent'},source:{kind:'measured',description:'old source'}});
const doc=(schema=schemas[0])=>({name:'Curve <img onerror=x>',revision:'2',assembly_id:'A',source_kind:'measured',physical_instance:'sample-1',parents:['original'],payload:{schema,electrical_model:{charge_voltage_response:curve(),source:{kind:'measured'},read_noise:{source:{kind:'measured'}}}}});

test('all nonlinear families route, copy and select their own fixed Test without widening topology',()=>{
  schemas.forEach((schema,i)=>{
    assert.equal(box.CameraCaptureRuntime(schema),runtimes[i]);
    assert.equal(box.CameraCaptureTemplateMode(schema),'&mode='+modes[i]);
    const state={runtime:runtimes[i],captureProgram:'P',sceneSequence:'S',controlSequence:'C'};
    const stimulus={schema:stimuli[i],capture_program_id:'P',scene_sequence_id:'S',control_sequence_id:'C'};
    assert.equal(box.CameraCaptureTestMatches({stimulus},state),true);
    assert.equal(box.CameraCaptureTestMatches({stimulus:{...stimulus,capture_program_id:'other'}},state),false);
    assert.equal(box.CameraCaptureTestMatches({stimulus:{...stimulus,schema:stimuli[(i+1)%3]}},state),false);
  });
});
test('nonlinear draft edits exact knot path and keeps source input immutable',()=>{
  const drafts=box.CameraCaptureDrafts(),original=doc();
  drafts.set('A',JSON.stringify(original));drafts.update('A','payload.electrical_model.charge_voltage_response.signal_voltage_v.2',.16);
  assert.equal(drafts.parse('A').payload.electrical_model.charge_voltage_response.signal_voltage_v[2],.16);
  assert.equal(original.payload.electrical_model.charge_voltage_response.signal_voltage_v[2],.15);
  assert.throws(()=>drafts.update('A','payload.electrical_model.charge_voltage_response.signal_voltage_v.9',.2));
  drafts.set('A','{unfinished');assert.throws(()=>drafts.parse('A'));assert.equal(drafts.get('A'),'{unfinished');
});
test('physics controls cannot preserve measured authority after authored curve edits',()=>{
  const original=doc(),edited=box.CameraNonlinearParameterEdit(original,'payload.electrical_model.charge_voltage_response.signal_voltage_v.2');
  assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);
  assert.equal(edited.payload.electrical_model.charge_voltage_response.source.kind,'estimated');
  assert.equal(edited.payload.electrical_model.read_noise.source.kind,'estimated');
  assert.equal(original.source_kind,'measured');assert.equal(edited.parents[0],'original');
  assert.equal(box.CameraNonlinearParameterEdit(original,'name'),original);
});
test('paired curve edits mark both declared paths and covariance as authored',()=>{
  const original=doc(schemas[1]);original.payload.electrical_model.paths=[{stream_id:'HCG',charge_voltage_response:curve()},{stream_id:'LCG',charge_voltage_response:curve()}];
  delete original.payload.electrical_model.charge_voltage_response;
  const edited=box.CameraNonlinearParameterEdit(original,'payload.electrical_model.paths.0.charge_voltage_response.charge_e.1');
  for(const path of edited.payload.electrical_model.paths)assert.equal(path.charge_voltage_response.source.kind,'estimated');
  assert.equal(edited.payload.electrical_model.read_noise.source.kind,'estimated');
});
function editorContext(){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  Object.assign(box,{state:{assembly:'A',captureLoading:false,captureSaving:false,captureError:''},esc,json:JSON.stringify,array:v=>Array.isArray(v)?v:[],details:(t,v)=>'<details>'+esc(t)+' '+esc(JSON.stringify(v))+'</details>',options:()=>'',matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null});
  const start=source.indexOf('  function nonlinearTemplateActions() {'),end=source.indexOf('  function captureProgramEditor() {',start);
  vm.runInContext(source.slice(start,end),box);return box;
}
test('actual editor shows physical knot units, calibrated color scope and escaped editable source',()=>{
  const ui=editorContext(),d=doc(),html=ui.nonlinearCaptureEditor(d,JSON.stringify(d),'');
  assert.match(html,/전하→신호 전압/);assert.match(html,/electron/);assert.match(html,/V²/);
  assert.match(html,/고정 WB·CCM·tone/);assert.match(html,/RAW−black DN/);
  assert.match(html,/payload.electrical_model.charge_voltage_response.signal_voltage_v.2/);
  assert.match(html,/&lt;img onerror=x&gt;/);assert.doesNotMatch(html,/<img onerror=x>/);
  assert.match(html,/id="capture-program-json"/);assert.match(html,/data-form="capture-program"/);
  assert.doesNotMatch(html,/data-capture-field="payload.electrical_model.conversion_gain_v_per_e"/);
});
test('template controls require a selected assembly and preserve all three v2 acquisition choices',()=>{
  const ui=editorContext();let html=ui.nonlinearTemplateActions();
  for(const name of ['capture-nonlinear-template','capture-nonlinear-dcg-template','capture-nonlinear-reference-template'])assert.match(html,new RegExp('data-action="'+name+'"'));
  ui.state.assembly='';html=ui.nonlinearTemplateActions();assert.equal((html.match(/disabled/g)||[]).length,3);
});

test('saved nonlinear RAW can open explicit charge-domain replay while unselected legacy DCG remains unsupported',()=>{
  const summary={raw_format:{response_type:'nonlinear_charge_voltage'}};
  assert.equal(box.CameraRunReplayUnsupportedReason(summary,'hdr_bracket_v1'),'');
  assert.equal(box.CameraRunReplayUnsupportedReason(summary,'reference_hdr_v1'),'');
  assert.equal(box.CameraRunReplayUnsupportedReason({raw_formats:[{response_type:'nonlinear_charge_voltage'}]},'shared_charge_dcg_v1'),'');
  assert.match(box.CameraRunReplayUnsupportedReason(summary,'shared_charge_dcg_v1'),/경로별/);
  assert.equal(box.CameraRunReplayUnsupportedReason({raw_format:{response_type:'linear'}},'hdr_bracket_v1'),'');
  const begin=source.indexOf('  function experimentsView() {'),end=source.indexOf('\n  }',begin)+4;
  Object.assign(box,{selectedRun:()=>({id:'run-nonlinear',status:'succeeded',plugin_id:'hdr_bracket_v1',artifacts:[{name:'raw_frames.npy'}]}),runSummary:()=>summary,runtimeId:v=>v,state:{experimentMode:'camera'},badge:()=>'',details:()=>'',isSceneRuntime:()=>true});
  for(const key of ['caseContextBanner','heading','experimentModes','cameraPlot','nativeSitePanel','chargeCollectionPanel','uncertaintyPanel','recentRunTable','runControls','artifactLinks'])box[key]=()=>'';
  vm.runInContext(source.slice(begin,end),box);
  const html=box.experimentsView();
  assert.doesNotMatch(html,/<button[^>]*data-action="replay-from-run"[^>]*disabled/);
  assert.match(html,/이 RAW를 다른 ISP로 재처리/);
});
test('native pixel table identifies actual nonlinear RAW black-relative means without dark correction',()=>{
  const ui=editorContext(),begin=source.indexOf('  function nativeSitePanel(run) {'),end=source.indexOf('  function chargeCollectionPanel(run) {',begin);
  ui.runSummary=()=>({native_pixel_response:{source:{kind:'estimated'}},prediction:{unit:'DN',method_and_version:'sampled_nonlinear_raw_black_relative_v1',axes:[{name:'frame',values:['F0']},{name:'channel',values:['R','Gr','Gb','B']}],values:[1,2,3,4]}});
  ui.number=v=>String(v);ui.badge=()=>'';
  vm.runInContext(source.slice(begin,end),ui);
  const html=ui.nativeSitePanel({status:'succeeded'});
  assert.match(html,/RAW−black 평균 · 암전류 응답 포함/);
  assert.match(html,/비선형 암전류 응답을 빼거나 전자 수로 해석하지 않습니다/);
  assert.doesNotMatch(html,/잡음·양자화 전의 기대 평균/);
});

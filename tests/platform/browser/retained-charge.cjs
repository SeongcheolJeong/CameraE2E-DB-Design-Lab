'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const box={structuredClone};vm.createContext(box);
vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
const schema='camera_retained_charge_capture_program_v1';
const state=()=>({runtime:'retained_charge_v1',assembly:'A',captureProgram:'P',sceneSequence:'S',controlSequence:'C'});
const stimulus=()=>({schema:'retained_charge_test_v1',capture_program_id:'P',scene_sequence_id:'S',control_sequence_id:'C'});
const doc=()=>({assembly_id:'A',name:'Lag <img onerror=x>',revision:'1',source_kind:'measured',physical_instance:'old-sample',parents:['parent'],payload:{schema,retained_charge_model:{capacity_e:1000,transfer_efficiency:[.8,.9,.9,.7]},electrical_model:{source:{kind:'measured'}}}});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function ui(s=state()) {
  return box.CameraRetainedChargeUI({state:s,esc,array:v=>Array.isArray(v)?v:[],number:(v,n)=>Number(v).toFixed(n),badge:esc,details:(title,value)=>'<details>'+esc(title)+' '+esc(JSON.stringify(value))+'</details>',options:()=>'',records:()=>[{id:'C',assembly_id:'A',name:'Recorded <controls>'}],matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r?.result?.summary||{},empty:(a,b)=>esc(a+' '+b),frameBrowser:()=>'<div>saved raw preview</div>'});
}
test('retained programs require exact program, scene, controls and a supported test schema',()=>{
  assert.equal(box.CameraCaptureRuntime(schema),'retained_charge_v1');
  assert.equal(box.CameraCaptureSchema('retained_charge_v1'),schema);
  assert.equal(box.CameraCaptureTemplateMode(schema),'&mode=retained_charge');
  assert.equal(box.CameraCaptureTestMatches({stimulus:stimulus()},state()),true);
  for(const key of ['schema','capture_program_id','scene_sequence_id','control_sequence_id']) {
    const s=stimulus();delete s[key];assert.equal(box.CameraCaptureTestMatches({stimulus:s},state()),false);
    s[key]='other';assert.equal(box.CameraCaptureTestMatches({stimulus:s},state()),false);
  }
  assert.notEqual(box.CameraCaptureScope(state()),box.CameraCaptureScope({...state(),captureProgram:'other'}));
  assert.equal(box.CameraCaptureMatches({capture_program_id:'other'},state()),false);
});
test('a retained draft preserves invalid text and rejects cross-assembly late responses',()=>{
  const drafts=box.CameraCaptureDrafts();drafts.set('A',JSON.stringify(doc()));
  drafts.update('A','payload.retained_charge_model.transfer_efficiency.1',.4);
  assert.equal(drafts.parse('A').payload.retained_charge_model.transfer_efficiency[1],.4);
  const token=drafts.snapshot('A');drafts.set('A','{unfinished');
  assert.equal(drafts.accept(token,doc(),'A'),false);assert.equal(drafts.get('A'),'{unfinished');
  assert.equal(drafts.accept(drafts.snapshot('A'),doc(),'B'),false);
});
test('editing efficiency demotes program authority without replacing unchanged readout provenance',()=>{
  const original=doc(),edited=box.CameraRetainedParameterEdit(original,'payload.retained_charge_model.transfer_efficiency.0');
  assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);
  assert.equal(edited.payload.electrical_model.source.kind,'measured');
  assert.equal(original.source_kind,'measured');assert.deepEqual(edited.parents,original.parents);
  const readout=box.CameraRetainedParameterEdit(original,'payload.electrical_model.digital_gain');
  assert.equal(readout.payload.electrical_model.source.kind,'estimated');
});
test('program authoring binds controls, escapes source and exposes four independent native sites',()=>{
  const view=ui(),html=view.editor(doc(),JSON.stringify(doc()),'');
  assert.match(html,/&lt;img onerror=x&gt;/);assert.doesNotMatch(html,/<img onerror=x>/);
  for(let i=0;i<4;i++)assert.match(html,new RegExp('data-capture-field="payload.retained_charge_model.transfer_efficiency.'+i+'"'));
  assert.match(html,/PD clear/);assert.match(html,/id="capture-program-json"/);
  assert.match(view.actions(),/id="retained-control-sequence"/);
  assert.doesNotMatch(view.actions(),/disabled/);
  assert.match(ui({...state(),controlSequence:'unknown'}).actions(),/disabled/);
});
test('results distinguish simulated truth from measured charge and do not offer legacy replay',()=>{
  const html=ui().panel({status:'succeeded',result:{summary:{retained_charge:{frame_ledger:[{frame_id:'dark',totals_e:{delivered_e:200,residual_e:200,clear_loss_e:0,gap_overflow_e:0,exposure_overflow_e:0}}]}}}});
  assert.match(html,/200\.000/);assert.match(html,/RAW로 측정한 전하가 아닙니다/);
  assert.match(html,/RAW 코드의 표시/);assert.match(html,/암전류/);
  assert.match(box.CameraRunReplayUnsupportedReason({},'retained_charge_v1'),/이전 프레임/);
});

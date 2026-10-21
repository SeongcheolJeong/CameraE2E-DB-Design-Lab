'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const box={structuredClone};vm.createContext(box);vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
const schema='camera_electrical_control_capture_program_v1';
const state={runtime:'electrical_control_v1',assembly:'A',captureProgram:'P',sceneSequence:'S',controlSequence:'C',test:'T'};
test('electrical template becomes editable persisted draft without changing immutable parent',()=>{
  const doc={assembly_id:'A',source_kind:'measured',physical_instance:'old-sample',payload:{schema,electrical_control_policy:{target_black_relative_dn:{minimum:100,maximum:200},exposure_grid_ns:{minimum:100,maximum:1000,step:100}}}};
  const drafts=box.CameraCaptureDrafts();assert.equal(drafts.accept(drafts.snapshot('A'),doc,'A'),true);
  drafts.update('A','payload.electrical_control_policy.target_black_relative_dn.minimum',150);
  drafts.update('A','payload.electrical_control_policy.exposure_grid_ns.maximum',900);
  const edited=box.CameraRetainedParameterEdit(drafts.parse('A'),'payload.electrical_control_policy.target_black_relative_dn.minimum');
  assert.equal(edited.payload.electrical_control_policy.target_black_relative_dn.minimum,150);
  assert.equal(edited.payload.electrical_control_policy.exposure_grid_ns.maximum,900);
  assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);
  assert.equal(doc.payload.electrical_control_policy.target_black_relative_dn.minimum,100);
  const token=drafts.snapshot('A');drafts.set('A','{unfinished');assert.equal(drafts.accept(token,doc,'A'),false);assert.equal(drafts.get('A'),'{unfinished');
});
test('AE dispatch pins its own program and Test plus scenario schedule identity',()=>{
  assert.equal(box.CameraCaptureRuntime(schema),'electrical_control_v1');
  assert.equal(box.CameraCaptureSchema(state.runtime),schema);
  const stimulus={schema:'electrical_control_test_v1',capture_program_id:'P',scene_sequence_id:'S',control_sequence_id:'C'};
  assert.equal(box.CameraCaptureTestMatches({stimulus},state),true);
  for(const key of Object.keys(stimulus))assert.equal(box.CameraCaptureTestMatches({stimulus:{...stimulus,[key]:'other'}},state),false);
  assert.equal(box.CameraCaptureMatches({capture_program_id:'other'},state),false);
  assert.notEqual(box.CameraCaptureScope(state),box.CameraCaptureScope({...state,captureProgram:'other'}));
  assert.match(box.CameraRunReplayUnsupportedReason({},state.runtime),/이전 프레임/);
});

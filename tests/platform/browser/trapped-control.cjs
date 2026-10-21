'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8'),box={structuredClone};
vm.createContext(box);vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
vm.runInContext(fs.readFileSync('src/camera_platform/static/electrical-control.js','utf8'),box);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const doc=()=>({assembly_id:'A',name:'Trap AE',revision:'3',source_kind:'measured',physical_instance:'sample',payload:{schema:'camera_electrical_control_capture_program_v3',observation_readout_id:'single',electrical_model:{schema:'camera_electrical_readout_v1'},retained_charge_model:{schema:'camera_trapped_photodiode_v1',capacity_e:1000,transfer_efficiency:[1,1,1,1],trap_capacity_e:[100,100,100,100],capture_rate_s:[10,20,30,40],release_time_s:[.01,.02,.03,.04],integration:{max_step_ns:10000}},electrical_control_policy:{enabled:true,target_black_relative_dn:{minimum:20,maximum:40},exposure_grid_ns:{minimum:1000,maximum:4000,step:1000}}}});
function context(){return {state:{assembly:'A',controlSequence:'C'},esc,array:v=>Array.isArray(v)?v:[],number:(v,n=3)=>Number(v).toFixed(n),badge:esc,details:(title,v)=>esc(title)+esc(JSON.stringify(v)),options:()=>'',matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r.result?.summary||{},empty:esc,frameBrowser:()=>'<div>saved raw preview</div>',records:()=>[{id:'C',assembly_id:'A',name:'clock'}]};}
for(const mode of ['electrical_control_trapped','electrical_control_trapped_pwm_period','electrical_control_trapped_nonlinear','electrical_control_trapped_hcg','electrical_control_trapped_lcg','electrical_control_trapped_hcg_nonlinear','electrical_control_trapped_lcg_nonlinear'])test(mode+' query and authoring entry',()=>{
  assert.equal(box.CameraElectricalControlTemplateQuery(mode,'C&bad=1'),'&mode='+mode+'&control_sequence_id=C%26bad%3D1');
  assert.ok(box.CameraRetainedChargeUI(context()).actions().includes('data-electrical-mode="'+mode+'"'));
});
test('period policy editor and result state target feasibility explicitly',()=>{
  const document=doc();document.payload.electrical_control_policy.schema='camera_electrical_exposure_policy_v2';document.payload.electrical_control_policy.period_lattice={kind:'whole_pwm_period_v1',period_ns:8000000};document.payload.waveform={kind:'pwm',period_ns:8000000,on_time_ns:4000000};
  const ui=box.CameraElectricalControlUI(context()),html=ui.editor(document,JSON.stringify(document),'');
  assert.match(html,/data-capture-field="payload.electrical_control_policy.period_lattice.period_ns"/);
  assert.match(html,/data-capture-field="payload.waveform.on_time_ns"/);
  const run={status:'succeeded',result:{summary:{electrical_control:{policy:document.payload.electrical_control_policy,readout_id:'single',target_outcome:{in_target_frame_count:0,observed_frame_count:1,period_lattice:{period_ns:8000000,point_count:1},exposure_space_diagnosis:{status:'only_permitted_exposure_missed_in_observed_frames'}},frames:[{frame_id:'f0',applied:{exposure_ns:8000000},statistics:{value_dn:571},proposal:{status:'period_lattice_hold_outside_target',commanded:null}}],commands:[]}}}};
  const panel=ui.panel(run);
  assert.match(panel,/허용 노출 1개/);assert.match(panel,/목표 구간에 들어온 프레임 0\/1/);
  assert.match(panel,/period_lattice_hold_outside_target/);
  assert.match(panel,/발행된 명령 없음/);
  assert.match(panel,/이 정책에는 다른 노출 설정이 없습니다/);
  assert.match(panel,/Gain 또는 HDR 경로를 별도 비교해야 합니다/);
});
test('v3 draft routing preserves parent and demotes edited trap parameter authority',()=>{
  const original=doc(),before=JSON.stringify(original),drafts=box.CameraCaptureDrafts();drafts.set('A',before);
  assert.equal(box.CameraCaptureRuntime(drafts.parse('A').payload.schema),'electrical_control_v1');
  drafts.update('A','payload.retained_charge_model.release_time_s.0',.05);
  const edited=box.CameraRetainedParameterEdit(drafts.parse('A'),'payload.retained_charge_model.release_time_s.0');
  assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);assert.equal(edited.payload.retained_charge_model.release_time_s[0],.05);assert.equal(JSON.stringify(original),before);
});
test('trap AE editor exposes physical parameters and explicit single selector refusal',()=>{
  const ui=box.CameraElectricalControlUI(context()),document=doc(),before=JSON.stringify(document);
  const html=ui.editor(document,before,'');
  for(const key of ['trap_capacity_e','capture_rate_s','release_time_s','transfer_efficiency'])for(let i=0;i<4;i++)assert.ok(html.includes('data-capture-field="payload.retained_charge_model.'+key+'.'+i+'"'));
  assert.match(html,/PD clear는 자유 전하만/);assert.match(html,/완료된 RAW만 관측/);assert.equal(JSON.stringify(document),before);
  delete document.payload.observation_readout_id;
  const invalid=ui.editor(document,JSON.stringify(document),'');assert.match(invalid,/관측 경로 single을 명시/);assert.match(invalid,/type="submit" disabled/);
});
test('trap paired editor keeps fixed LCG and its ADC instead of preview selection',()=>{
  const document=doc();document.payload.observation_readout_id='LCG';document.payload.electrical_model={schema:'camera_shared_charge_readout_v2',paths:[{stream_id:'HCG',adc:{bit_depth:12,black_level_dn:10}},{stream_id:'LCG',adc:{bit_depth:12,black_level_dn:90}}]};
  const html=box.CameraElectricalControlUI(context()).editor(document,JSON.stringify(document),'');
  assert.match(html,/<option value="LCG" selected>/);assert.match(html,/black 90\.000 DN/);assert.match(html,/Trap 용량/);
});
test('actual AE timing and trap ledger remain separate from measured code observation',()=>{
  const run={status:'succeeded',result:{summary:{retained_charge:{model:{schema:'camera_trapped_photodiode_v1'},frame_ledger:[{frame_id:'f<1>',totals_e:{delivered_e:40,residual_e:2,trapped_e:30,captured_e:35,released_e:5,clear_loss_e:0,gap_overflow_e:1,exposure_overflow_e:2}}]},electrical_control:{readout_id:'single',frames:[{frame_id:'f<1>',applied:{exposure_ns:1000},statistics:{value_dn:20},proposal:{commanded:{exposure_ns:2000}}}],commands:[]}}}};
  const html=box.CameraElectricalControlUI(context()).panel(run);
  assert.match(html,/data-electrical-applied-exposure>1000 ns/);assert.match(html,/data-electrical-commanded-exposure>2000 ns/);
  assert.match(html,/Trap AE 전하 수지/);assert.match(html,/30\.000/);assert.match(html,/35\.000/);assert.match(html,/RAW로 측정한 전하가 아닙니다/);assert.match(html,/f&lt;1&gt;/);assert.doesNotMatch(html,/<f<1>/);
});

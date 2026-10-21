'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const app=fs.readFileSync('src/camera_platform/static/app.js','utf8'),box={structuredClone};
vm.createContext(box);vm.runInContext(app.slice(0,app.indexOf('(() => {')),box);
vm.runInContext(fs.readFileSync('src/camera_platform/static/electrical-control.js','utf8'),box);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const doc=(readout='LCG')=>({assembly_id:'A',name:'paired',revision:'2',source_kind:'measured',physical_instance:'old',payload:{schema:'camera_electrical_control_capture_program_v2',observation_readout_id:readout,electrical_model:{paths:[{stream_id:'HCG',adc:{bit_depth:12,black_level_dn:17.25}},{stream_id:'LCG',adc:{bit_depth:12,black_level_dn:91.5}}]},electrical_control_policy:{enabled:true,target_black_relative_dn:{minimum:1,maximum:3},exposure_grid_ns:{minimum:10,maximum:100,step:10}}}});
function context(state={}){return {state:{assembly:'A',controlSequence:'C',...state},esc,array:v=>Array.isArray(v)?v:[],number:(v,d=3)=>Number(v).toFixed(d),badge:esc,details:(title,value)=>esc(title)+esc(JSON.stringify(value)),options:()=>'',matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:run=>run.result?.summary||{},empty:esc,frameBrowser:()=>'<div data-preview>actual frames</div>',records:kind=>kind==='control_sequences'?[{id:'C',assembly_id:'A',name:'control'}]:[]};}
for(const [mode,readout] of [['electrical_control_hcg','HCG'],['electrical_control_lcg','LCG'],['electrical_control_hcg_nonlinear','HCG'],['electrical_control_lcg_nonlinear','LCG']])test(mode+' maps to an explicit bounded API template',()=>{
  const query=box.CameraElectricalControlTemplateQuery(mode,'C&attack=1');
  assert.equal(query,'&mode='+mode+'&control_sequence_id=C%26attack%3D1');
  const html=box.CameraRetainedChargeUI(context()).actions();
  assert.match(html,new RegExp('data-electrical-mode="'+mode+'"'));
  assert.match(html,new RegExp(readout+' 고정 관측'));
});
test('template query keeps original single default and rejects missing control or forged mode',()=>{
  assert.equal(box.CameraElectricalControlTemplateQuery(undefined,'C'),'&mode=electrical_control&control_sequence_id=C');
  assert.throws(()=>box.CameraElectricalControlTemplateQuery('auto','C'));
  assert.throws(()=>box.CameraElectricalControlTemplateQuery('electrical_control_hcg',''));
  assert.match(box.CameraRetainedChargeUI(context({captureLoading:true})).actions(),/data-electrical-mode="electrical_control_hcg" disabled/);
});
test('paired program draft dispatches to same runtime while immutable parent and invalid text survive',()=>{
  const original=doc(),before=JSON.stringify(original),drafts=box.CameraCaptureDrafts();
  drafts.accept(drafts.snapshot('A'),original,'A');
  assert.equal(box.CameraCaptureRuntime(drafts.parse('A').payload.schema),'electrical_control_v1');
  drafts.update('A','payload.observation_readout_id','HCG');
  const next=box.CameraRetainedParameterEdit(drafts.parse('A'),'payload.observation_readout_id');
  assert.equal(next.payload.observation_readout_id,'HCG');assert.equal(next.source_kind,'estimated');assert.equal(next.physical_instance,null);
  assert.equal(JSON.stringify(original),before);
  const token=drafts.snapshot('A');drafts.set('A','{unfinished');assert.equal(drafts.accept(token,original,'A'),false);assert.equal(drafts.get('A'),'{unfinished');
  assert.equal(box.CameraCaptureSchema('electrical_control_v1'),'camera_electrical_control_capture_program_v1');
});
test('paired editor exposes fixed path and exact selected ADC without mutating document',()=>{
  const original=doc(),before=JSON.stringify(original),html=box.CameraElectricalControlUI(context()).editor(original,JSON.stringify(original),'');
  assert.match(html,/data-capture-field="payload.observation_readout_id"/);assert.match(html,/<option value="LCG" selected>/);
  assert.match(html,/black 91\.500 DN/);assert.doesNotMatch(html,/black 17\.250 DN/);
  assert.match(html,/새 프로그램 버전/);assert.match(html,/자동 경로 전환이나 HDR 합성은 수행하지 않습니다/);
  assert.equal(JSON.stringify(original),before);
});
test('invalid paired path is visible escaped and blocks save rather than choosing a default',()=>{
  const invalid=doc('<script>auto</script>'),html=box.CameraElectricalControlUI(context()).editor(invalid,'{original}','');
  assert.match(html,/지원하지 않는 경로/);assert.match(html,/&lt;script&gt;auto&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
  assert.match(html,/type="submit" disabled/);assert.doesNotMatch(html,/<option value="HCG" selected>|<option value="LCG" selected>/);
  assert.match(html,/\{original\}/);
});
test('single editor retains implicit single path and does not author v2 selection',()=>{
  const single=doc();single.payload.schema='camera_electrical_control_capture_program_v1';delete single.payload.observation_readout_id;
  const html=box.CameraElectricalControlUI(context()).editor(single,JSON.stringify(single),'');
  assert.match(html,/관측 읽기 경로: single/);assert.doesNotMatch(html,/data-capture-field="payload.observation_readout_id"/);
});
test('result feedback path remains original trace even if a different preview path is selected',()=>{
  const ctx=context({readoutSelection:'HCG'}),run={status:'succeeded',result:{summary:{electrical_control:{readout_id:'LCG',policy:doc().payload.electrical_control_policy,raw_format:{black_level_dn:91.5},frames:[{frame_id:'f0',applied:{exposure_ns:10},statistics:{value_dn:-2},proposal:{status:'increase',commanded:{exposure_ns:20}}}],commands:[]}}}};
  const html=box.CameraElectricalControlUI(ctx).panel(run);
  assert.match(html,/관측 경로: LCG/);assert.doesNotMatch(html,/관측 경로: HCG/);
  assert.match(html,/data-electrical-applied-exposure>10 ns/);assert.match(html,/data-electrical-commanded-exposure>20 ns/);
  assert.match(html,/data-electrical-observation>-2\.000/);
});

test('paired result distinguishes LCG feedback from saved HCG preview on the same population',()=>{
  const run={status:'succeeded',result:{summary:{readout_ids:['HCG','LCG'],preview_readout_id:'HCG',electrical_control:{readout_id:'LCG',policy:doc().payload.electrical_control_policy,frames:[{frame_id:'f0',applied:{exposure_ns:10},statistics:{value_dn:2}}],commands:[]}}}};
  const before=JSON.stringify(run),html=box.CameraElectricalControlUI(context()).panel(run);
  assert.match(html,/고정 AE 관측 경로: LCG · 표시 RAW 경로: HCG · 동일 전달 전하의 두 읽기/);
  assert.doesNotMatch(html,/표시 RAW 경로: LCG/);assert.equal(JSON.stringify(run),before);
});
test('paired preview identity missing or invalid is unknown, never inferred from feedback selection',()=>{
  for(const preview of [undefined,'<script>bad</script>']) {
    const run={status:'succeeded',result:{summary:{readout_ids:['HCG','LCG'],preview_readout_id:preview,electrical_control:{readout_id:'LCG',frames:[{frame_id:'f0'}]}}}};
    const html=box.CameraElectricalControlUI(context()).panel(run);
    assert.match(html,/고정 AE 관측 경로: LCG · 표시 RAW 경로: 미확인/);
    assert.doesNotMatch(html,/<script>|표시 RAW 경로: LCG/);
  }
});

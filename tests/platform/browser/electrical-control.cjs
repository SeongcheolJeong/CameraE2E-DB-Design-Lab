'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const box={};vm.createContext(box);
vm.runInContext(fs.readFileSync('src/camera_platform/static/electrical-control.js','utf8'),box);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const document=()=>({name:'AE <img onerror=x>',revision:'new"version',payload:{schema:'camera_electrical_control_capture_program_v1',electrical_control_policy:{schema:'camera_electrical_exposure_policy_v1',enabled:true,target_black_relative_dn:{minimum:2,maximum:30},exposure_grid_ns:{minimum:1000,maximum:9000,step:1000},statistics_roi:{reference:'native_sensor',origin:[0,0],shape:[8,8]},meter:{sites:['Gr','Gb']}}}});
const successful=()=>({id:'run-A',status:'succeeded',capture_program_snapshot:{name:'Saved <script>alert(1)</script>'},request:{control_sequence:{frames:[{applied:{exposure_ns:1234}}]}},result:{summary:{electrical_control:{policy:document().payload.electrical_control_policy,frames:[{frame_id:'frame-0',index:0,latch_at_ns:9007199254000000,applied:{exposure_ns:3000},applied_command_id:null,statistics:{value_dn:-1.25,sample_count:32},proposal:{status:'increase',commanded:{exposure_ns:6000}},events:{statistics_ready_ns:9007199254009000},issued_command_ids:['command-0']}],commands:[{command_id:'command-0',source_frame_id:'frame-0',actuator_ready_ns:9007199254010000,status:'pending_after_sequence'}]},raw_formats:{single:{response_type:'nonlinear_charge_voltage',bit_depth:12,black_level_dn:63.5}}}}});
function create(extra={}) {
  let previews=[];
  const state={assembly:'A',captureProgram:'P',...extra.state};
  const context={state,esc,array:value=>Array.isArray(value)?value:[],number:(value,digits)=>Number(value).toFixed(digits),badge:value=>'<span>'+esc(value)+'</span>',details:(title,value)=>'<details><summary>'+esc(title)+'</summary>'+esc(JSON.stringify(value))+'</details>',options:()=>'<option value="A">camera</option>',matchingCapturePrograms:()=>[{id:'P"/><script>x</script>',name:'program <svg onload=x>',revision:'1'}],selectedCaptureProgram:()=>({id:'P'}),runSummary:run=>run?.result?.summary||{},empty:(title,message)=>'<div class="empty">'+esc(title)+' '+esc(typeof message==='string'?message:JSON.stringify(message))+'</div>',frameBrowser:run=>{previews.push(run);return '<div data-actual-preview="'+esc(run.id)+'">saved preview</div>';},...extra};
  return {ui:box.CameraElectricalControlUI(context),previews};
}
test('editor uses shared capture controls, exact policy fields and integer exposure steps',()=>{
  const {ui}=create(),html=ui.editor(document(),JSON.stringify(document()),'');
  for(const key of ['minimum','maximum'])assert.match(html,new RegExp('data-capture-field="payload.electrical_control_policy.target_black_relative_dn.'+key+'"'));
  for(const key of ['minimum','maximum','step'])assert.match(html,new RegExp('data-capture-field="payload.electrical_control_policy.exposure_grid_ns.'+key+'"[^>]*step="1"'));
  for(const item of ['id="capture-program-assembly"','id="factory-capture-program"','data-action="capture-use"','data-action="capture-copy"','data-form="capture-program"','id="capture-program-json"'])assert.ok(html.includes(item),item);
  assert.match(html,/value="2"/);assert.doesNotMatch(html,/type="checkbox"/);assert.doesNotMatch(html,/data-capture-field="[^"]*enabled/);
  assert.match(html,/enabled에 true 또는 false/);assert.match(html,/ROI/);assert.match(html,/지연/);
});
test('editor escapes names, option attributes, errors and full JSON without modifying input',()=>{
  const {ui}=create(),doc=document(),before=JSON.stringify(doc),text='</textarea><script>bad()</script>';
  const html=ui.editor(doc,text,'<img src=x onerror=bad()>');
  assert.equal(JSON.stringify(doc),before);
  assert.match(html,/&lt;img onerror=x&gt;/);assert.match(html,/&lt;\/textarea&gt;&lt;script&gt;/);assert.match(html,/P&quot;\/&gt;&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|<img src=x|<svg onload/);assert.match(html,/입력 원문은 유지됩니다/);
});
test('unselected programs disable copy/use and missing policy disables submit gracefully',()=>{
  const {ui}=create({selectedCaptureProgram:()=>null});
  const html=ui.editor(null,'{unfinished','parse error');
  assert.match(html,/data-action="capture-use" disabled/);assert.match(html,/data-action="capture-copy" disabled/);assert.match(html,/type="submit" disabled/);
  assert.match(html,/\{unfinished/);assert.match(html,/정책이 없습니다/);
});
test('saving preserves draft and disables only submission',()=>{
  const {ui}=create({state:{assembly:'A',captureSaving:true}}),html=ui.editor(document(),'saved draft','');
  assert.match(html,/type="submit" disabled>등록 중/);assert.match(html,/saved draft/);
});
test('actual applied exposure differs from the proposal and never inherits planned exposure',()=>{
  const {ui,previews}=create(),run=successful(),before=JSON.stringify(run),html=ui.panel(run);
  assert.match(html,/data-electrical-applied-exposure>3000 ns/);assert.match(html,/data-electrical-commanded-exposure>6000 ns/);
  assert.doesNotMatch(html,/1234/);assert.match(html,/data-electrical-observation>-1\.250/);
  assert.match(html,/9007199254000000 ns/);assert.match(html,/9007199254010000 ns/);
  assert.deepEqual(previews,[run]);assert.equal(JSON.stringify(run),before);
});
test('format details preserve nonlinear response and fractional black level without inverse claims',()=>{
  const html=create().ui.panel(successful());
  assert.match(html,/nonlinear_charge_voltage/);assert.match(html,/63\.5/);assert.match(html,/광전하 복구 결과가 아닙니다/);
  assert.doesNotMatch(html,/data-action="replay-from-run"|data-action="retained-isp-from-run"|보정된 전하/);
});
test('record labels and command identities remain escaped in result tables',()=>{
  const run=successful(),control=run.result.summary.electrical_control;
  control.frames[0].frame_id='<img onerror=x>';control.frames[0].applied_command_id='"<svg onload=x>';
  control.commands[0].source_frame_id='<script>source</script>';control.commands[0].status='<svg onload=x>';
  const html=create().ui.panel(run);
  assert.match(html,/&lt;img onerror=x&gt;/);assert.match(html,/&lt;script&gt;source&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<img onerror|<svg|<script>/);
});
test('absent observations and unapplied commands display unknowns instead of invented zeros',()=>{
  const run=successful(),frame=run.result.summary.electrical_control.frames[0];
  frame.statistics={};frame.proposal={status:'hold',commanded:null};frame.latch_at_ns=undefined;
  const html=create().ui.panel(run);
  assert.match(html,/data-electrical-observation>—/);assert.match(html,/data-electrical-commanded-exposure>—/);
  assert.match(html,/pending_after_sequence/);assert.doesNotMatch(html,/data-electrical-observation>0\.000/);
});
test('succeeded run without actual control evidence does not render a misleading preview',()=>{
  const {ui,previews}=create(),html=ui.panel({status:'succeeded',result:{summary:{}}});
  assert.match(html,/전기 AE 결과 근거 없음/);assert.equal(previews.length,0);assert.doesNotMatch(html,/data-electrical-applied-exposure/);
});
test('waiting and failed runs show status and escaped error without stale result frames',()=>{
  for(const status of ['queued','running','failed']) {
    const {ui,previews}=create(),run=successful();run.status=status;run.error='<script>failure</script>';
    const html=ui.panel(run);assert.equal(previews.length,0);assert.doesNotMatch(html,/data-electrical-applied-exposure|<script>/);
    assert.match(html,/&lt;script&gt;failure&lt;\/script&gt;/);
  }
});
test('invalid timestamps and nonfinite statistics are not presented as numerical evidence',()=>{
  const run=successful(),frame=run.result.summary.electrical_control.frames[0];frame.latch_at_ns=9007199254740992;frame.statistics.value_dn=NaN;
  const html=create().ui.panel(run);assert.match(html,/data-electrical-observation>—/);assert.doesNotMatch(html,/9007199254740992 ns/);
});
test('scope explicitly distinguishes RAW observations from sensor truth and physical qualification',()=>{
  const {ui}=create();assert.match(ui.scopeNote,/다음 노출/);assert.match(ui.scopeNote,/잔류 PD 전하/);
  assert.match(ui.scopeNote,/LFM 성능은 미확립/);assert.match(ui.scopeNote,/실제 AE 안정성/);
});

test('disabled policy and selected source format remain explicit without success claims',()=>{
  const run=successful(),control=run.result.summary.electrical_control;control.policy.enabled=false;control.readout_id='single';control.raw_format={response_type:'nonlinear_charge_voltage',black_level_dn:7.25};
  const html=create().ui.panel(run);assert.match(html,/AE 비활성 · 초기 노출 유지/);assert.match(html,/관측 경로: single/);assert.match(html,/목표 구간: 2\.000–30\.000 DN/);assert.match(html,/7\.25/);
});

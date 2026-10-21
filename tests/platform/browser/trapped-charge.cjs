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
test('trap program routing and parameter edits preserve draft and demote authority',()=>{
 const document=doc();document.payload.schema='camera_retained_charge_capture_program_v2';
 document.payload.retained_charge_model={schema:'camera_trapped_photodiode_v1',capacity_e:1000,transfer_efficiency:[1,1,1,1],trap_capacity_e:[100,101,102,103],capture_rate_s:[10,20,30,40],release_time_s:[.01,.02,.03,.04],integration:{max_step_ns:10000}};
 assert.equal(box.CameraCaptureRuntime(document.payload.schema),'retained_charge_v1');
 assert.equal(box.CameraCaptureTemplateMode(document.payload.schema),'&mode=trapped_charge');
 const drafts=box.CameraCaptureDrafts();drafts.set('A',JSON.stringify(document));
 drafts.update('A','payload.retained_charge_model.release_time_s.2',.05);
 const edited=box.CameraRetainedParameterEdit(drafts.parse('A'),'payload.retained_charge_model.release_time_s.2');
 assert.equal(edited.payload.retained_charge_model.release_time_s[2],.05);
 assert.equal(edited.source_kind,'estimated');assert.equal(edited.physical_instance,null);
 const html=ui().editor(edited,JSON.stringify(edited),'');
 for(const key of ['trap_capacity_e','capture_rate_s','release_time_s'])for(let i=0;i<4;i++)assert.ok(html.includes('data-capture-field="payload.retained_charge_model.'+key+'.'+i+'"'));
 assert.match(html,/최대 적분 간격/);assert.match(html,/PD clear는 자유 전하만/);
 assert.match(ui().actions(),/capture-trapped-template/);
});
test('trap ledger identifies internal transfers and remaining trap population',()=>{
 const html=ui().panel({status:'succeeded',result:{summary:{retained_charge:{model:{schema:'camera_trapped_photodiode_v1'},frame_ledger:[{frame_id:'lag',totals_e:{delivered_e:10,residual_e:0,trapped_e:30,captured_e:40,released_e:10,clear_loss_e:0,gap_overflow_e:0,exposure_overflow_e:0}}]}}}});
 assert.match(html,/Trap 잔류/);assert.match(html,/30\.000/);assert.match(html,/40\.000/);
 assert.match(html,/유한 시간 간격의 근사/);assert.match(html,/RAW로 측정한 전하가 아닙니다/);
});

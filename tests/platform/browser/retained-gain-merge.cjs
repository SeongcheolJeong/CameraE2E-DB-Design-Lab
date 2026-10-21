'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const box={structuredClone};vm.createContext(box);
vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function ui(){return box.CameraRetainedChargeUI({state:{assembly:'A',controlSequence:'C',captureSaving:false,captureLoading:false},esc,array:v=>Array.isArray(v)?v:[],number:(v,n)=>Number(v).toFixed(n),badge:esc,details:()=>'',options:()=>'',records:()=>[{id:'C',assembly_id:'A',name:'control'}],matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r.result?.summary||{},empty:()=>'',frameBrowser:()=>'<div>saved RAW</div>'});}
for(const [schema,mode,action] of [
  ['camera_retained_charge_capture_program_v3','retained_gain_merge','capture-retained-gain-template'],
  ['camera_retained_charge_capture_program_v4','trapped_gain_merge','capture-trapped-gain-template']
])test(schema+' is opt-in and routes to retained runtime',()=>{
  const drafts=box.CameraCaptureDrafts();
  const document={assembly_id:'A',source_kind:'estimated',payload:{schema,gain_merge_profile:{schema:'camera_dcg_merge_v1'},retained_charge_model:{capacity_e:1000,transfer_efficiency:[1,1,1,1]}}};
  drafts.set('A',JSON.stringify(document));
  assert.equal(drafts.parse('A').payload.schema,schema);
  assert.equal(box.CameraCaptureRuntime(schema),'retained_charge_v1');
  assert.equal(box.CameraCaptureTemplateMode(schema),'&mode='+mode);
  assert.ok(ui().actions().includes('data-action="'+action+'"'));
  const editor=ui().editor(document,JSON.stringify(document),'');
  assert.match(editor,/같은 프레임의 전달 총전하 추정/);
  assert.match(editor,/다중 노출 HDR 합성/);
});
test('gain-path result labels total delivered charge and escapes frame IDs',()=>{
  const html=ui().panel({status:'succeeded',result:{summary:{retained_charge:{frame_ledger:[]},gain_path_merge:{frames:[{frame_id:'<frame>',valid_pixels:10,invalid_pixels:6,agreement_pixels:8}]}}}});
  assert.match(html,/같은 프레임의 HCG\/LCG 전달 전하 결합/);
  assert.match(html,/잔류·trap·암전류는 포함/);
  assert.match(html,/&lt;frame&gt;/);
  assert.doesNotMatch(html,/<frame>/);
});

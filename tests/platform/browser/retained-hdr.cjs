'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const box={structuredClone};vm.createContext(box);
vm.runInContext(source.slice(0,source.indexOf('(() => {')),box);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const array=v=>Array.isArray(v)?v:[];
function ui(){return box.CameraRetainedChargeUI({state:{assembly:'A',controlSequence:'C',captureSaving:false,captureLoading:false},esc,array,number:(v,n)=>Number(v).toFixed(n),badge:esc,details:()=>'',options:()=>'',records:()=>[{id:'C',assembly_id:'A',name:'control'}],matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r.result?.summary||{},empty:()=>'',frameBrowser:()=>'<div>saved RAW</div>'});}

test('opt-in retained HDR routes to explicit program template',()=>{
  const schema='camera_retained_charge_capture_program_v5';
  const drafts=box.CameraCaptureDrafts();
  const document={assembly_id:'A',payload:{schema,photo_hdr_profile:{schema:'camera_retained_photo_hdr_profile_v1'},retained_charge_model:{capacity_e:1000,transfer_efficiency:[.8,.8,.8,.8]}}};
  drafts.set('A',JSON.stringify(document));
  assert.equal(drafts.parse('A').payload.schema,schema);
  assert.equal(box.CameraCaptureRuntime(schema),'retained_charge_v1');
  assert.equal(box.CameraCaptureTemplateMode(schema),'&mode=retained_stateful_hdr');
  assert.ok(ui().actions().includes('data-action="capture-retained-hdr-template"'));
  const editor=ui().editor(document,JSON.stringify(document),'');
  assert.match(editor,/현재 노출 광전자율을 추정/);
  assert.match(editor,/Trap·PWM·실측 보정/);
});

test('result displays bounded rate meaning and escaped counts',()=>{
  const summary={retained_charge:{frame_ledger:[]},photo_hdr:{valid_pixels:12,invalid_pixels:4,selected_frame_counts:[3,9]}};
  const html=ui().panel({status:'succeeded',result:{summary}});
  assert.match(html,/잔류 전하를 고려한 다중 노출 광전자율/);
  assert.match(html,/PRNU가 포함된 광전자율/);
  assert.match(html,/실측 교정, trap 역산/);
  assert.match(html,/3 \/ 9/);
});

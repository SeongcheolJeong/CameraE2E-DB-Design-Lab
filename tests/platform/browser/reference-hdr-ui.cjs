'use strict';

const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const sandbox={structuredClone,TextEncoder};
vm.createContext(sandbox);
vm.runInContext(source.slice(0,source.indexOf('(() => {')),sandbox);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const array=value=>Array.isArray(value)?value:[];
const document=(assembly_id='A')=>({name:'Reference <HDR>',revision:'1',assembly_id,source_kind:'estimated',source_uri:'fixture://reference',license:'MIT',method_and_version:'fixture:1',physical_instance:null,parents:[],payload:{schema:'camera_reference_hdr_capture_program_v1',electrical_model:{digital_gain:1},waveform:null,hdr_merge:{schema:'camera_reference_hdr_merge_v1',reference_frame_index:0,base_profile:{schema:'camera_hdr_merge_v1',consistency_sigma:5}}}});
const translationDocument=()=>{const result=document();result.payload.hdr_merge={schema:'camera_reference_hdr_merge_global_translation_v1',reference_frame_index:0,base_profile:result.payload.hdr_merge.base_profile,registration:{max_shift_px:4,minimum_common_pixels:64,maximum_mean_clipped_chi2:4,minimum_score_gap:3}};return result;};
function context() {
  const state={assembly:'A',captureProgram:'P',captureError:'',captureDrafts:sandbox.CameraCaptureDrafts()};
  const previews=[];
  const ui=sandbox.CameraReferenceHDRUI({state,esc,array,number:v=>v===null||v===undefined?'—':String(v),badge:esc,details:(title,value)=>`<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(value))}</pre></details>`,options:()=>'',matchingCapturePrograms:()=>[],selectedCaptureProgram:()=>null,runSummary:r=>r?.result?.summary||{},empty:(title,text)=>`<p>${esc(title)} ${esc(text)}</p>`,imagePreviews:(artifacts,scope)=>{previews.push({artifacts,scope});return '<img data-test-preview>';},frameBrowser:()=>'<div data-test-frames></div>'});
  return {state,ui,previews};
}
const state=()=>({runtime:'reference_hdr_v1',assembly:'A',sceneSequence:'S',controlSequence:'C',captureProgram:'P',test:'T'});
const testRecord=()=>({id:'T',stimulus:{schema:'reference_hdr_test_v1',capture_program_id:'P',scene_sequence_id:'S',control_sequence_id:'C'}});

test('reference runtime, fixed program and test are separate from sequential HDR and DCG',()=>{
  assert.equal(sandbox.CameraCaptureRuntime(document().payload.schema),'reference_hdr_v1');
  assert.equal(sandbox.CameraCaptureSchema('reference_hdr_v1'),document().payload.schema);
  assert.equal(sandbox.CameraCaptureTemplateMode(document().payload.schema),'&mode=reference_guarded');
  assert.equal(sandbox.CameraCaptureTestMatches(testRecord(),state()),true);
  for(const schema of ['electrical_hdr_test_v1','shared_charge_dcg_test_v1','temporal_scene_test_v1'])assert.equal(sandbox.CameraCaptureTestMatches({stimulus:{...testRecord().stimulus,schema}},state()),false);
  for(const key of ['capture_program_id','scene_sequence_id','control_sequence_id'])assert.equal(sandbox.CameraCaptureTestMatches({stimulus:{...testRecord().stimulus,[key]:'other'}},state()),false);
  for(const key of ['runtime','assembly','sceneSequence','controlSequence','captureProgram','test'])assert.notEqual(sandbox.CameraCaptureScope(state()),sandbox.CameraCaptureScope({...state(),[key]:'changed'}));
  assert.equal(sandbox.CameraCaptureMatches({capture_program_id:'P'},state()),true);
  assert.equal(sandbox.CameraCaptureMatches({capture_program_id:'Q'},state()),false);
  assert.equal(sandbox.CameraCaptureTestMatches(testRecord(),{...state(),runtime:'hdr_bracket_v1'}),false);
});

test('reference drafts update only the index, preserve nested profile and reject late responses',()=>{
  const {state:current}=context(),doc=document();
  current.captureDrafts.accept(current.captureDrafts.snapshot('A'),doc,'A');
  current.captureDrafts.update('A','payload.hdr_merge.reference_frame_index',1);
  assert.equal(current.captureDrafts.parse('A').payload.hdr_merge.reference_frame_index,1);
  assert.equal(JSON.stringify(current.captureDrafts.parse('A').payload.hdr_merge.base_profile),JSON.stringify(doc.payload.hdr_merge.base_profile));
  const token=current.captureDrafts.snapshot('A');
  current.captureDrafts.set('A','{unfinished reference');
  assert.equal(current.captureDrafts.accept(token,doc,'A'),false);
  assert.equal(current.captureDrafts.get('A'),'{unfinished reference');
  assert.equal(current.captureDrafts.accept(current.captureDrafts.snapshot('A'),doc,'B'),false);
});

test('reference editor exposes bounded index and full JSON without incompatible HDR or PWM controls',()=>{
  const {ui}=context(),doc=document(),html=ui.editor(doc,JSON.stringify(doc),'');
  assert.match(html,/value="Reference &lt;HDR&gt;"/);
  assert.match(html,/data-capture-field="payload.hdr_merge.reference_frame_index" type="number" min="0" max="15" step="1"/);
  assert.match(html,/<details class="data-detail" open>/);
  assert.match(html,/PWM 합성은 지원하지 않습니다/);
  assert.match(html,/포화된 기준 노출 복구는 지원하지 않습니다/);
  assert.match(html,/data-action="capture-reference-template"/);
  assert.match(html,/data-action="capture-reference-translation-template"/);
  assert.doesNotMatch(html,/id="capture-hdr-enabled"|id="capture-pwm-enabled"|data-capture-field="payload.hdr_merge.consistency_sigma"/);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(item=>item[1]);
  assert.equal(ids.length,new Set(ids).size);
  assert.doesNotMatch(html,/<button type="submit"[^>]*disabled/);
});

test('global RAW translation profile is visible and malformed registration is not submittable',()=>{
  const {ui}=context(),doc=translationDocument();
  assert.equal(sandbox.CameraReferenceHDRValid(doc),true);
  const html=ui.editor(doc,JSON.stringify(doc),'');
  assert.match(html,/새 RAW 전역 평행이동 HDR 프로그램/);
  assert.match(html,/global shutter 적용 설정이 필요합니다/);
  assert.doesNotMatch(html,/<button type="submit"[^>]*disabled/);
  doc.payload.hdr_merge.registration.max_shift_px=3;
  assert.equal(sandbox.CameraReferenceHDRValid(doc),false);
  assert.match(ui.editor(doc,JSON.stringify(doc),''),/<button type="submit"[^>]*disabled/);
});

test('malformed reference settings remain editable and cannot submit through the form',()=>{
  const {ui}=context();
  const broken=[doc=>doc.payload.hdr_merge=null,doc=>doc.payload.hdr_merge.base_profile=null,doc=>doc.payload.hdr_merge.reference_frame_index=16,doc=>doc.payload.hdr_merge.reference_frame_index=0.5,doc=>doc.payload.waveform={kind:'pwm'}];
  for(const change of broken){const doc=document();change(doc);const text=JSON.stringify(doc),html=ui.editor(doc,text,'');assert.match(html,/입력 원문은 유지됩니다/);assert.match(html,/<button type="submit"[^>]*disabled/);assert.ok(html.includes(esc(text)));}
  const constant=document();constant.payload.waveform={kind:'constant',scale:1};
  assert.doesNotMatch(ui.editor(constant,JSON.stringify(constant),''),/<button type="submit"[^>]*disabled/);
});

test('saved result separates fusion, reference fallback, unknown consistency and invalid reference',()=>{
  const {ui,previews}=context();
  const run={id:'R',status:'succeeded',capture_program_id:'P',artifacts:[{name:'sensor-rgb.png',url:'/preview'},{name:'raw_frames.npy'}],result:{summary:{hdr_merge_applied:true,frames:[{frame_id:'f0'},{frame_id:'f1'}],hdr_merge:{reference_frame_index:1,reference_frame_id:'f1 <source>',fused_pixels:20,reference_fallback_pixels:10,reference_only_unknown_pixels:3,reference_invalid_pixels:4}}}};
  const html=ui.panel(run);
  assert.match(html,/1 · f1 &lt;source&gt;/);
  for(const value of ['일치 판정 · 합성 픽셀</dt><dd>20','기준 노출로 돌아간 픽셀</dt><dd>10','기준 노출만 유효 · 일관성 미확인</dt><dd>3','기준 노출 무효 · unknown</dt><dd>4'])assert.ok(html.includes(value));
  assert.match(html,/data-test-frames/);assert.equal(previews.length,1);assert.equal(previews[0].artifacts.length,1);assert.equal(previews[0].scope.kind,'hdr');
  assert.match(html,/감지되지 않는 움직임은 남을 수 있습니다/);
  assert.match(html,/실물 검증은 미확립/);
  assert.doesNotMatch(ui.panel({id:'pending',status:'queued'}),/data-test-preview/);
  assert.match(ui.panel({id:'failed',status:'failed',error:'<failure>'}),/&lt;failure&gt;/);
  assert.doesNotMatch(ui.panel({id:'missing',status:'succeeded',result:{summary:{}}}),/data-test-preview|합성 픽셀<\/dt>/);
});

test('actual template action requests reference mode and keeps a newer draft',async()=>{
  const branch=source.slice(source.indexOf("        case 'capture-template':"),source.indexOf("        case 'capture-copy':"));
  const action=vm.runInContext(`(async function(ctx){const {state,api,toast,render}=ctx;const action='capture-reference-template';switch(action){${branch}}})`,sandbox);
  const {state:current}=context(),notices=[];
  await action({state:current,api:async url=>{assert.match(url,/assembly_id=A&mode=reference_guarded$/);return document();},toast:message=>notices.push(message),render:()=>{}});
  assert.equal(current.captureDrafts.parse('A').payload.schema,document().payload.schema);
  await action({state:current,api:async()=>{current.captureDrafts.set('A','typing a newer draft');return document();},toast:message=>notices.push(message),render:()=>{}});
  assert.equal(current.captureDrafts.get('A'),'typing a newer draft');assert.ok(notices.length);assert.equal(current.captureLoading,false);
});

test('actual translation template action requests the server-owned profile',async()=>{
  const branch=source.slice(source.indexOf("        case 'capture-template':"),source.indexOf("        case 'capture-copy':"));
  const action=vm.runInContext(`(async function(ctx){const {state,api,toast,render}=ctx;const action='capture-reference-translation-template';switch(action){${branch}}})`,sandbox);
  const {state:current}=context();
  await action({state:current,api:async url=>{assert.match(url,/assembly_id=A&mode=reference_guarded_translation$/);return translationDocument();},toast:message=>assert.fail(message),render:()=>{}});
  assert.equal(current.captureDrafts.parse('A').payload.hdr_merge.schema,'camera_reference_hdr_merge_global_translation_v1');
  assert.equal(current.captureLoading,false);
});

test('copy action preserves nested reference settings and program use selects its runtime',async()=>{
  const branch=source.slice(source.indexOf("        case 'capture-copy': {"),source.indexOf("        case 'capture-use':"));
  const copy=vm.runInContext(`(async function(ctx){const {state,selectedCaptureProgram,workbenchData,api,dcgUI,render}=ctx;switch('capture-copy'){${branch}}})`,sandbox);
  const {state:current}=context(),full={id:'P',...document()};full.payload.hdr_merge.reference_frame_index=1;
  await copy({state:current,selectedCaptureProgram:()=>full,workbenchData:{load:async()=>full},api:async url=>{assert.match(url,/mode=reference_guarded$/);return document();},dcgUI:{rememberProfile:()=>assert.fail('Wrong DCG editor')},render:()=>{}});
  assert.equal(current.captureDrafts.parse('A').payload.hdr_merge.reference_frame_index,1);
  assert.equal(current.captureDrafts.parse('A').parents[0],'P');assert.equal(current.captureDrafts.parse('A').revision,'1-edit');
  const useBranch=source.slice(source.indexOf("        case 'capture-use':"),source.indexOf("        case 'copy-record':"));
  const use=vm.runInContext(`(function(ctx){const {state,selectedCaptureProgram,clearSceneTest,location,render}=ctx;switch('capture-use'){${useBranch}}})`,sandbox);
  const location={hash:''};let cleared=0;
  use({state:current,selectedCaptureProgram:()=>full,clearSceneTest:()=>cleared++,location,render:()=>{}});
  assert.equal(current.runtime,'reference_hdr_v1');assert.equal(location.hash,'experiments');assert.equal(cleared,1);
});

test('actual run form submits fixed reference program and sequences without a second exposure field',async()=>{
  const branch=source.slice(source.indexOf("        case 'run':"),source.indexOf("        case 'replay':"));
  const submit=vm.runInContext(`(async function(ctx){const {state,data,isElectrical,findRecord,runOne}=ctx;switch('run'){${branch}}})`,sandbox);
  const data={assembly_id:'A',test_id:'T',plugin_id:'reference_hdr_v1',scene_sequence_id:'S',control_sequence_id:'C',capture_program_id:'P',seed:'73',exposure_ms:'999'};
  let body;
  await submit({state:state(),data,isElectrical:()=>true,findRecord:()=>testRecord(),runOne:async value=>{body=value;}});
  assert.equal(body.plugin_id,'reference_hdr_v1');assert.equal(body.capture_program_id,'P');assert.equal(body.scene_sequence_id,'S');assert.equal(body.control_sequence_id,'C');assert.equal(body.parameters.seed,73);assert.equal(Object.hasOwn(body.parameters,'exposure_ms'),false);
  await assert.rejects(submit({state:state(),data,isElectrical:()=>true,findRecord:()=>({stimulus:{...testRecord().stimulus,schema:'electrical_hdr_test_v1'}}),runOne:()=>assert.fail('Wrong topology submitted')}),/고정된 시험/);
});

test('actual asynchronous run action does not replace a newly selected reference input',async()=>{
  const begin=source.indexOf('  async function runOne(body) {'),end=source.indexOf("  document.addEventListener('submit'",begin);
  const run=vm.runInContext(`(async function(ctx,body){const {state,api,refresh,toast,runtimeId}=ctx;${source.slice(begin,end)}return runOne(body);})`,sandbox);
  const current=state();current.run='newer';let refreshed=0;
  const result=await run({state:current,api:async()=>{current.captureProgram='newly-selected';return {id:'submitted',status:'queued'};},refresh:async()=>refreshed++,toast:()=>{},runtimeId:v=>v},{plugin_id:'reference_hdr_v1',assembly_id:'A',test_id:'T',scene_sequence_id:'S',control_sequence_id:'C',capture_program_id:'P'});
  assert.equal(result.id,'submitted');assert.equal(current.run,'newer');assert.equal(current.captureProgram,'newly-selected');assert.equal(refreshed,1);
});

test('actual program selector filters topology and the editor dispatches the nested profile separately',()=>{
  const begin=source.indexOf('  function captureProgramSelector() {'),end=source.indexOf('  function captureProgramEditor() {',begin);
  const selector=vm.runInContext(`(function(ctx){const {state,selectedCaptureProgram,matchingCapturePrograms,referenceHDRUI,isDCG,esc,details,go}=ctx;${source.slice(begin,end)}return captureProgramSelector();})`,sandbox);
  const reference={id:'P',...document()},legacy={id:'legacy',...document(),payload:{schema:'camera_capture_program_v1'}},dcg={id:'dcg',...document(),payload:{schema:'camera_dcg_capture_program_v1'}};
  const html=selector({state:state(),selectedCaptureProgram:()=>reference,matchingCapturePrograms:()=>[reference,legacy,dcg],referenceHDRUI:{scopeNote:'REFERENCE SCOPE'},isDCG:()=>false,esc,details:()=>'',go:()=>''});
  assert.match(html,/<option value="P" selected>/);assert.doesNotMatch(html,/<option value="legacy"|<option value="dcg"/);assert.match(html,/REFERENCE SCOPE/);
  const editorBegin=end,editorEnd=source.indexOf('  function frameBrowser(',editorBegin);
  const editor=vm.runInContext(`(function(ctx){const {state,referenceHDRUI,dcgUI}=ctx;${source.slice(editorBegin,editorEnd)}return captureProgramEditor();})`,sandbox);
  const {state:current}=context();current.captureDrafts.set('A',JSON.stringify(document()));
  let selected=0;
  assert.equal(editor({state:current,referenceHDRUI:{editor:doc=>{selected++;assert.equal(doc.payload.hdr_merge.schema,'camera_reference_hdr_merge_v1');return 'reference editor';}},dcgUI:{editor:()=>assert.fail('Wrong editor')}}),'reference editor');
  assert.equal(selected,1);
});

test('actual saved-result routing uses reference HDR panel before generic temporal rendering',()=>{
  const begin=source.indexOf('  function cameraPlot(run,compare=false) {'),end=source.indexOf('  function temporalCameraPanel(',begin);
  assert.ok(end>begin);
  const route=vm.runInContext(`(function(ctx,run){const {state,runtimeId,referenceHDRUI,dcgUI}=ctx;${source.slice(begin,end)}return cameraPlot(run);})`,sandbox);
  let selected=0;
  assert.equal(route({state:state(),runtimeId:v=>v,referenceHDRUI:{panel:run=>{selected++;assert.equal(run.id,'saved');return 'reference result';}},dcgUI:{panel:()=>assert.fail('Wrong panel')}},{id:'saved',plugin_id:'reference_hdr_v1'}),'reference result');
  assert.equal(selected,1);
});

test('actual index input immediately disables invalid submission without depending on blur or change',()=>{
  const begin=source.indexOf('    if(event.target.dataset.captureField) {');
  const branch=source.slice(begin,source.indexOf("    if(event.target.id==='assembly-contract')",begin));
  const input=vm.runInContext(`(function(ctx,event){const {state,dcgUI,$,toast}=ctx;const json=JSON.stringify;${branch}})`,sandbox);
  const {state:current}=context();current.captureDrafts.set('A',JSON.stringify(document()));
  const submit={disabled:false},original={value:''};
  const form={querySelector:selector=>{assert.equal(selector,'button[type="submit"]');return submit;}};
  const target={dataset:{captureField:'payload.hdr_merge.reference_frame_index'},type:'number',form,value:'16'};
  const ctx={state:current,dcgUI:{markParameterEdit:()=>{}},$:()=>original,toast:message=>assert.fail(message)};
  input(ctx,{target});
  assert.equal(submit.disabled,true);assert.equal(current.captureDrafts.parse('A').payload.hdr_merge.reference_frame_index,16);assert.equal(original.value,current.captureDrafts.get('A'));
  for(const value of ['','0.5','-1']){target.value=value;input(ctx,{target});assert.equal(submit.disabled,true);}
  target.value='1';input(ctx,{target});assert.equal(submit.disabled,false);
  current.captureSaving=true;target.value='0';input(ctx,{target});assert.equal(submit.disabled,true);
  assert.equal(form.querySelector('button[type="submit"]'),submit);
  assert.match(context().ui.scopeNote,/같은 위치의 RAW가 일치한다고 판정한 영역만 합성/);
});

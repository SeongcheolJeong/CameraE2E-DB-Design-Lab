"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const root=path.resolve(__dirname,"../../../src/camera_platform/static"),source=fs.readFileSync(path.join(root,"reference-workshop.js"),"utf8"),html=fs.readFileSync(path.join(root,"reference-workshop.html"),"utf8");
const nodes=new Map();
function node(value=""){return {value,checked:false,disabled:false,textContent:"",innerHTML:"",classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[],reportValidity:()=>true,setAttribute(){},addEventListener(){}};}
for(const match of html.matchAll(/<(?:input|select|textarea|div|p|span|strong|output|button|fieldset|form|section|progress)[^>]*\bid="([^"]+)"[^>]*>/g)){const value=match[0].match(/\bvalue="([^"]*)"/);nodes.set(match[1],node(value?.[1]||""));}
// Include all remaining IDs such as labels and tables used by existing editor code.
for(const match of html.matchAll(/\bid="([^"]+)"/g))if(!nodes.has(match[1]))nodes.set(match[1],node());
const requests=[],context={console,URL,document:{getElementById:id=>nodes.get(id),querySelectorAll:()=>[]},location:{href:"http://127.0.0.1:8780/reference-workshop.html"},setTimeout:()=>1,clearTimeout(){},fetch:async(url,options)=>{requests.push({url,payload:JSON.parse(options.body)});return {ok:true,json:async()=>({id:"submitted",status:"queued"})};}};
vm.createContext(context);
vm.runInContext(source.replace("  init();\n})();",`const updateURLForTest=updateURL;buttons=()=>{};schedulePoll=()=>{};refreshJobs=async()=>{};renderResults=()=>{};renderSweep=()=>{};updateURL=()=>{};notice=()=>{};
globalThis.ui={state,chooseEditor,updateURLForTest,readSpec,applySpec,readParameterAxes,parameterPlan,readTuningConstraints,studyRow,reasonText,submit,sourceHasChirp,submitIspStudy,submitIspPreview,renderPreviewScope,renderTuningDiagnostics,selectView,tuningButtons,ispSource,viewFrom,equivalent};\n})();`),context);
const ui=context.ui,plain=value=>JSON.parse(JSON.stringify(value));
const spec={name:"Test source",schema:"camera_reference_workshop_v1",reference_id:"reference",lens:{curvature_scale:1,sensor_shift_um:0,optical_profile:"geometric_retrace"},pixel:{cfa_thickness_scale:1},chart_suite:"isp_lab_v1",isp:{schema:"camera_reference_isp_v2",lsc_enabled:true,raw_denoise:{strength:0,range_floor_dn2:1},linear_sharpen:{amount:0,threshold:.01,max_delta:.05},demosaic_method:"adaptive_laplacian",black_offset_dn:0,color_calibration_id:null,wb:[1,1,1],ccm:[[1,0,0],[0,1,0],[0,0,1]],tone_gamma:2.2,display_white_level:1},exposure_ms:4,gain:1,luminance_cd_m2:1000,frames:8,seed:42};
const field={id:"outer-ne",status:"succeeded",images:{texture:{raw:"texture/raw.npz",display:"texture/display.png"}},tests:{isp_lab:{uniform_mean_luma:.4,noise_power_spectrum:{status:"ok",units:"linear luma²",frequency_cycles_per_pixel:[0,.25,.5],radial_power:[1e-4,5e-5,1e-5]}}}};
const job={id:"full-source",status:"succeeded",result:{summary:{configuration:spec,fields:[field]}}};
ui.state.ready=true;ui.state.token="test-token";ui.state.current=ui.viewFrom(job);ui.state.field="outer-ne";ui.state.chart="texture";ui.applySpec(spec);
let editorHidden=true,updatedURL=null;nodes.get("isp-editor").classList.toggle=(name,value)=>{if(name==="hidden")editorHidden=value;};context.history={replaceState(_state,_title,url){updatedURL=String(url);}};context.location.href="http://127.0.0.1:8780/reference-workshop.html?run=full-source&editor=isp";ui.chooseEditor(new URLSearchParams("editor=isp"));assert.equal(ui.state.editor,"isp");assert.equal(editorHidden,false,"Explicit editor deep link opens the ISP controls");ui.updateURLForTest();assert.equal(new URL(updatedURL).searchParams.get("editor"),"isp","Candidate/field URL refresh retains the selected editor");
assert.equal(ui.equivalent(ui.readSpec(),spec),true,"Old v2 profile without LSC tuning keeps unchanged identity");
nodes.get("lsc-strength").value="0.85";nodes.get("lsc-gain-cap").value="2.5";
assert.deepEqual(plain(ui.readSpec().isp.lsc_tuning),{strength:.85,gain_cap:2.5});
ui.applySpec({...spec,isp:{...spec.isp,lsc_tuning:{strength:.75,gain_cap:3}}});
assert.deepEqual(plain(ui.readSpec().isp.lsc_tuning),{strength:.75,gain_cap:3},"New LSC tuning survives profile round trip");
ui.applySpec(spec);
const axes=ui.readParameterAxes();assert.equal(axes.combinations,12);assert.equal(ui.parameterPlan(ui.readSpec().isp,axes.axes).count,12,"Explicit full-LSC NR0/sharpen0 baseline deduplicates into default 12 candidates");
nodes.get("axis-nr").value="0.1, 0.2, 0.4";assert.equal(ui.parameterPlan(ui.readSpec().isp,ui.readParameterAxes().axes).count,13,"Baseline is counted when absent from full Cartesian grid");nodes.get("axis-nr").value="0, 0.2, 0.4";
for(const invalid of ["", "0,", "NaN", "Infinity", "-1", "1.1"]){nodes.get("axis-nr").value=invalid;assert.throws(()=>ui.readParameterAxes());}nodes.get("axis-nr").value="0, 0.2, 0.4";
nodes.get("axis-floor").value="1, 2";assert.throws(()=>ui.readParameterAxes(),/24/);nodes.get("axis-floor").value="1";
assert.deepEqual(plain(ui.readTuningConstraints()),{texture_retention_min:.95,halo_increase_max:.05,delta_e_increase_max:1,noise_improvement_min:.01,brightness_relative_min:.95,brightness_relative_max:1.05,uniformity_increase_max:.05});
nodes.get("constraint-brightness-min").value="1.2";assert.throws(()=>ui.readTuningConstraints(),/밝기/);nodes.get("constraint-brightness-min").value=".95";
assert.match(ui.reasonText("brightness_normalized_noise_gain_below_limit"),/밝기 대비 잡음/);
assert.match(ui.studyRow({candidate_id:"candidate-02",noise:.006,brightness_normalized_noise_improvement:.0123,eligible:false,reasons:["brightness_normalized_noise_gain_below_limit"]}),/1.23%/);
assert.match(ui.studyRow({candidate_id:"legacy",eligible:false}),/—%/,"Missing brightness-normalized evidence is not fabricated");
async function checks(){
  await ui.submitIspPreview();assert.equal(requests[0].url,"/api/v1/reference-workshop/isp-previews");assert.equal(requests[0].payload.source_run_id,"full-source");assert.equal(requests[0].payload.chart,"texture");assert.equal(requests[0].payload.field_id,"outer-ne");assert.equal(ui.state.active.id,"submitted");
  await ui.submitIspPreview();assert.equal(requests.length,1,"Preview cannot double submit while active");ui.state.active=null;
  const previewJob={id:"preview",status:"succeeded",source:{run_id:"full-source",candidate_id:"candidate-03"},result:{summary:{configuration:spec,fields:[field],preview_scope:{field_id:"outer-ne",chart:"texture",frames:1,frame_index:0}}}};
  ui.state.sweep={id:"old-study"};ui.selectView(ui.viewFrom(previewJob));assert.equal(ui.state.sweep,null,"A one-frame preview must clear stale full-study selection evidence");ui.renderPreviewScope();assert.match(nodes.get("isp-preview-scope").textContent,/첫 RAW 1장/);assert.match(nodes.get("isp-preview-scope").textContent,/평가하지 않았/);
  await ui.submitIspStudy("parameter_tuning");const request=requests.at(-1);assert.equal(request.url,"/api/v1/reference-workshop/isp-studies");assert.equal(request.payload.source_run_id,"full-source","Full study after preview must replay full preserved source");assert.equal(request.payload.source_candidate_id,"candidate-03");assert.equal(request.payload.recipe,"parameter_tuning");assert.equal(Object.keys(request.payload.parameter_axes).length,7);assert.equal(request.payload.constraints.brightness_relative_min,.95);assert.equal(request.payload.isp.lsc_enabled,true);
  ui.state.active=null;nodes.get("axis-nr").value="0.1, 0.2, 0.4";await assert.rejects(ui.submitIspStudy("parameter_tuning"),/13/);assert.equal(requests.length,2,"Overflow never submits");nodes.get("axis-nr").value="0, 0.2, 0.4";
  await ui.submit("replay");assert.equal(requests.at(-1).url,"/api/v1/reference-workshop/replays");assert.equal(requests.at(-1).payload.source_run_id,"full-source");assert.equal(requests.at(-1).payload.source_candidate_id,"candidate-03","Legacy replay from preview must also use complete source");ui.state.active=null;
  ui.state.preparedJobs.clear();assert.equal(ui.sourceHasChirp(ui.state.current),null,"A deep-linked preview does not claim the source lacks chirp");await ui.submitIspStudy("demosaic");assert.equal(requests.at(-1).payload.source_run_id,"full-source");assert.equal(requests.at(-1).payload.recipe,"demosaic","Missing preview chart is not missing source chart; backend validates full source");ui.state.active=null;
  ui.state.baseline=ui.viewFrom(job);ui.state.current=ui.viewFrom(job);ui.renderTuningDiagnostics();assert.match(nodes.get("isp-nps-chart").innerHTML,/<polyline/);assert.match(nodes.get("isp-brightness-diagnostics").innerHTML,/1\.000 ×/);
  const sparse=plain(previewJob);sparse.result.summary.fields[0].tests={};ui.state.current=ui.viewFrom(sparse);ui.state.baseline=null;ui.renderTuningDiagnostics();assert.match(nodes.get("isp-nps-chart").innerHTML,/NPS 없음/);assert.doesNotMatch(nodes.get("isp-nps-chart").innerHTML,/<polyline/,"Missing new NPS must clear old curves");assert.match(nodes.get("isp-brightness-diagnostics").innerHTML,/— ×/);
  console.log("Reference ISP tuning UI: exact source rebinding, preview scope, finite axes and candidate limit, LSC round trip, brightness gates, and stale diagnostic clearing passed");
}
checks().catch(error=>{console.error(error);process.exitCode=1;});

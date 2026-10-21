'use strict';
const assert=require('node:assert/strict'),{test}=require('node:test'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
require('../../src/camera_platform/static/esfr-measured.js');
const UI=global.CameraESFRMeasured;
const rois=Array.from({length:112},(_,i)=>({id:'edge-'+i,kind:'edge',field_id:'field-'+i,box:[Math.floor(i/14)*40,(i%14)*40,32,32]}));
const hash='a'.repeat(64),contract={id:'contract-1',content_hash:'b'.repeat(64),candidate_sha256:hash,created_at:'2026-10-18T01:00:00Z'};
const template={capture_metadata:{source_kind:'measured',role:'development',camera_id:null,captured_at:null,pixel_pitch_um:null,raw_format:{width:null,black_level_dn:null},sampling:{binning:[null,null]},isp:{transfer:null,comparison_mode:'diagnostic'}},validation_additions:{role:'validation',validation_contract:{id:null,content_hash:null},acquisition_attestation:{operator_id:null,captured_after_contract:null,original_raw_isp_pair:null,camera_and_chart_unchanged:null}},chart_profile:{physical_chart_id:null}};
function capture(id,role='development'){return{id,metadata:{source_kind:'measured',role,session_id:role+'-session',validation_contract:role==='validation'?{id:contract.id,content_hash:contract.content_hash}:undefined}};}
function state(){const captures=['D1','D2','D3'].map(id=>capture(id));return{source:{run_id:'run-1',candidate_id:'candidate-03',condition_id:'condition-006'},captures,development:new Set(captures.map(c=>c.id)),validation:new Set(),reviewed:new Set(captures.map(c=>c.id)),rois,operator:'operator-1',physicalChart:'chart-serial',workflow:null};}
function metadata(){return{raw_processing:{linear_response:true,demosaiced:false,denoised:false,sharpened:false,companded:false,geometric_transform:false},source_kind:'measured',role:'development',camera_id:'camera-serial',session_id:'dev-session',acquisition_id:'acq-1',captured_at:'2026-10-18T08:00:00+09:00',pixel_pitch_um:1.4,conditions:{gain:1,exposure_s:.01,temperature_c:25,aperture_f_number:4,focus_distance_m:1,distance_m:1,mode_id:'native',light_id:'light-serial',focus_setting:'manual-position'},raw_format:{width:640,height:480,stride_bytes:1280,bit_depth:12,packing:'unpacked16',byte_order:'little',cfa:'RGGB',header_bytes:0,bit_shift:0,black_level_dn:64,white_level_dn:4095},frame_pair:{pair_id:'pair-1',raw_frame_id:'frame-1',isp_frame_id:'frame-1',same_exposure:true},settings_provenance:{mode:'operator_recorded',reference:'camera-settings-log'},sampling:{binning:[1,1],subsampling:[1,1],resized:false,rotation_deg:0,mirrored:false},isp:{transfer:'srgb',coordinate_mapping:'same_native_raster',comparison_mode:'diagnostic'}};}

test('source tuple survives DB, editor, workflow recovery links without inventing a source',()=>{
 const source=UI.sourceFrom(new URLSearchParams('source_run=run-1&candidate=candidate-03&condition=condition-006'));
 assert.deepEqual(UI.sourceValue(source),state().source);
 assert.equal(UI.sourceURL(source,'/db-design.html'),'/db-design.html?run=run-1&candidate-b=candidate-03&condition=condition-006');
 assert.match(UI.sourceURL(source,'/esfr-measured.html','measured-1'),/workflow=measured-1$/);
 assert.throws(()=>UI.sourceValue({run_id:'run-1'}),/실행 ID와 후보/);
});
test('blank metadata preserves unknown numeric fields and never supplies attestation',()=>{
 const before=structuredClone(template),dev=UI.metadataForRole(template,'development');
 assert.equal(dev.raw_format.width,null);assert.equal(dev.raw_format.black_level_dn,null);assert.equal(dev.pixel_pitch_um,null);assert.equal(dev.source_kind,'measured');
 const val=UI.metadataForRole(template,'validation',{contract:{...contract,capture_context:{raw_format:{width:1234},camera_id:'actual'}}});
 assert.equal(val.raw_format.width,1234);assert.equal(val.camera_id,'actual');assert.equal(val.role,'validation');assert.deepEqual(val.validation_contract,{id:contract.id,content_hash:contract.content_hash});assert.equal(val.acquisition_attestation.captured_after_contract,null);
 assert.deepEqual(template,before);assert.ok(UI.metadataIssues(dev,'development').length>5);
});
test('complete metadata is accepted but unsafe format, time, pairing and sampling are caught',()=>{
 const good=metadata();assert.deepEqual(UI.metadataIssues(good,'development'),[]);
 for(const altered of [{...good,captured_at:'2026-10-18T08:00:00'},{...good,source_kind:'synthetic'},{...good,raw_processing:null},{...good,raw_processing:{...good.raw_processing,companded:true}},{...good,isp:{...good.isp,transfer:'unknown'}},{...good,raw_format:{...good.raw_format,cfa:'BGGR'}},{...good,sampling:{...good.sampling,binning:[2,2]}},{...good,frame_pair:{...good.frame_pair,isp_frame_id:'other'}}])assert.ok(UI.metadataIssues(altered,'development').length>0);
 const val={...good,role:'validation',validation_contract:{id:contract.id,content_hash:contract.content_hash},acquisition_attestation:{operator_id:'op',captured_after_contract:true,original_raw_isp_pair:true,camera_and_chart_unchanged:true}};
 assert.deepEqual(UI.metadataIssues(val,'validation'),[]);delete val.validation_contract;assert.ok(UI.metadataIssues(val,'validation').some(x=>x.includes('취득 계약')));
});
test('112 unique native ROIs are mandatory and reviewed data cannot be silently resized',()=>{
 const result=UI.normalizeRois(rois,[400,600]);assert.deepEqual(result,rois);assert.notEqual(result,rois);
 assert.throws(()=>UI.normalizeRois(rois.slice(0,111)),/112개/);
 assert.throws(()=>UI.normalizeRois([...rois.slice(0,111),rois[0]]),/중복/);
 assert.throws(()=>UI.normalizeRois([{...rois[0],box:[0,0,31,32]},...rois.slice(1)]),/32 native/);
 assert.throws(()=>UI.normalizeRois(rois,[32,32]),/범위/);
});
test('candidate contract requires both immutable hashes before validation can open',()=>{
 assert.equal(UI.candidateFrozen({contract}),true);assert.equal(UI.roleFor({contract}),'validation');
 for(const altered of [{...contract,candidate_sha256:null},{...contract,content_hash:'short'},{...contract,id:'../bad'}])assert.equal(UI.candidateFrozen({contract:altered}),false);
 assert.equal(UI.roleFor(null),'development');
});
test('create review binds only three selected measured development captures after each review',()=>{
 const s=state();assert.deepEqual(UI.readiness(s,'development'),[]);
 const body=UI.reviewPayload(s,'development');assert.deepEqual(body.capture_ids,['D1','D2','D3']);assert.equal(body.all_112_edges_reviewed,true);assert.equal(body.registration_suitable,true);assert.equal(body.fixed_camera_chart,true);assert.equal(body.no_clipping_or_occlusion,true);
 s.reviewed.delete('D2');assert.throws(()=>UI.reviewPayload(s,'development'),/모든 촬영/);s.reviewed.add('D2');s.captures[1].metadata.source_kind='synthetic';assert.ok(UI.readiness(s,'development').some(x=>x.includes('measured 역할')));
});
test('validation requires new contract-bound session and cannot reuse a submitted holdout',()=>{
 const s=state();s.workflow={id:'workflow-1',status:'awaiting_validation',contract};s.captures.push(...['V1','V2','V3'].map(id=>capture(id,'validation')));s.validation=new Set(['V1','V2','V3']);s.validation.forEach(id=>s.reviewed.add(id));
 assert.deepEqual(UI.readiness(s,'validation'),[]);assert.deepEqual(UI.reviewPayload(s,'validation').capture_ids,['V1','V2','V3']);
 s.captures.at(-1).metadata.validation_contract={id:'old',content_hash:contract.content_hash};assert.ok(UI.readiness(s,'validation').some(x=>x.includes('현재 후보')));
 s.captures.at(-1).metadata.validation_contract={id:contract.id,content_hash:contract.content_hash};s.captures.at(-1).metadata.session_id='development-session';assert.ok(UI.readiness(s,'validation').some(x=>x.includes('session_id')));
 s.workflow.validation_ids=['V1','V2','V3'];assert.ok(UI.readiness(s,'validation').some(x=>x.includes('이미 검증')));
});
test('report links match backend schema and exclude external or script destinations',()=>{
 const links=UI.resultLinks({links:{report:'/api/v1/calibration-lab/completion-reports/report-1/files/report.html',bundle:'/api/v1/calibration-lab/completion-reports/report-1/files/evidence.zip',lab:'/calibration-lab.html?campaign=C',image_report:'/api/v1/calibration-lab/studies/V/files/esfr-images/report.html',db_url:'https://evil.example'}});
 assert.equal(links.length,4);assert.equal(links[0].label,'통합 보정·검증 보고서');
 for(const unsafe of ['javascript:alert(1)','//evil.example/path','https://evil.example','/secret','/api/v1/camera-validation/esfr/%5csecret'])assert.equal(UI.safeURL(unsafe),null);
});
test('local API session token protects mutations and preserves multipart boundaries',async()=>{
 const calls=[],api=UI.createAPI(async(url,options={})=>{calls.push([url,options]);return{ok:true,status:200,json:async()=>url==='/api/v1/session'?{token:'session-token'}:{id:'ok'}};});
 await api('/measured/template');assert.equal(calls.length,1);assert.equal(calls[0][1].method,'GET');
 await api('/measured/workflows',{source:state().source});assert.equal(calls[1][0],'/api/v1/session');assert.equal(calls[2][1].headers['X-Platform-Token'],'session-token');assert.match(calls[2][1].body,/run-1/);
 const form=new FormData();form.append('metadata','{}');await api('/captures',form);assert.equal(calls.at(-1)[1].body,form);assert.equal(calls.at(-1)[1].headers['Content-Type'],undefined);
});
test('API failure retains structured backend error rather than claiming success',async()=>{
 const api=UI.createAPI(async()=>({ok:false,status:409,json:async()=>({detail:'source fingerprint changed'})}));await assert.rejects(api('/measured/workflows/w'),/source fingerprint changed/);
 const denied=UI.createAPI(async()=>({ok:false,status:403,json:async()=>({error:'no session'})}));await assert.rejects(denied('/measured/workflows',{}),/로컬 세션/);
});

test('page boot and stored workflow recovery bind real DOM IDs and frozen validation template',async()=>{
 const base=path.resolve(__dirname,'../../src/camera_platform/static'),html=fs.readFileSync(path.join(base,'esfr-measured.html'),'utf8'),nodes=new Map();
 for(const match of html.matchAll(/\bid="([^"]+)"/g)){const listeners=new Map();nodes.set(match[1],{value:'',checked:false,disabled:false,hidden:false,innerHTML:'',textContent:'',className:'',files:[],classList:{toggle(){},add(){},remove(){}},addEventListener(name,callback){listeners.set(name,callback);},listeners,setAttribute(){},insertAdjacentHTML(position,value){this.innerHTML=position==='afterbegin'?value+this.innerHTML:this.innerHTML+value;},querySelector(){return null;},scrollIntoView(){}});}
 const recorded=[],workflow={id:'measured-1',source:state().source,development_ids:['D1','D2','D3'],status:'awaiting_validation',stage:'candidate_frozen',rois,chart_profile:{physical_chart_id:'chart-serial'},contract:{...contract,capture_context:{camera_id:'actual-camera',raw_format:{width:1600}}},measured_review:{operator_id:'op',physical_chart_id:'chart-serial',capture_reviews:[]}};
 const document={getElementById:id=>nodes.get(id)||null,createElement:()=>({click(){}}),addEventListener(){}};
 const window={document,location:{href:'http://127.0.0.1:8780/esfr-measured.html?workflow=measured-1'},history:{replaceState(...args){recorded.push(args[2]);}},addEventListener(){},FormData,fetch:async(url)=>({ok:true,status:200,json:async()=>url.endsWith('/template')?template:url.endsWith('/captures')?[]:workflow})};
 const context=vm.createContext({window,URL,URLSearchParams,FormData,Blob,Intl,JSON,Number,Set,Map,Error,Date,Promise,setTimeout,clearTimeout,console});
 vm.runInContext(fs.readFileSync(path.join(base,'esfr-measured.js'),'utf8'),context);
 await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));
 assert.equal(nodes.get('measured-notice').className,'');assert.equal(nodes.get('measured-source-run').value,'run-1');assert.equal(nodes.get('measured-source-run').disabled,true);assert.equal(nodes.get('measured-upload-fields').disabled,false);assert.equal(nodes.get('measured-validate').disabled,true);
 const form=JSON.parse(nodes.get('measured-metadata').value);assert.equal(form.role,'validation');assert.equal(form.camera_id,'actual-camera');assert.equal(form.validation_contract.id,'contract-1');assert.match(nodes.get('measured-contract').innerHTML,/후보 고정 완료/);assert.match(recorded.at(-1),/workflow=measured-1/);
});

test('validation merges six reviewed captures while preserving frozen development rows and original operator',()=>{
 const row=id=>({capture_id:id,raw_sha256:id+'-sha',roi_sha256:hash,all_112_edges_reviewed:true,registration_suitable:true,fixed_camera_chart:true,no_clipping_or_occlusion:true});
 const development={operator_id:'original-reviewer',physical_chart_id:'chart-1',chart_model:'imatest_extended_esfr_2023_16_9',roi_sha256:hash,capture_reviews:['D1','D2','D3'].map(row),evidence_scope:'operator_declared_not_authenticated'};
 const validation={operator_id:'new-reviewer',physical_chart_id:'chart-1',chart_model:development.chart_model,roi_sha256:hash,capture_reviews:['V1','V2','V3'].map(row)};
 const workflow={development_ids:['D1','D2','D3'],measured_review:development},before=structuredClone({workflow,validation});
 const combined=UI.mergeValidationReview(workflow,validation,['V1','V2','V3']);
 assert.equal(combined.capture_reviews.length,6);assert.equal(combined.operator_id,'new-reviewer');assert.equal(combined.development_review.operator_id,'original-reviewer');
 assert.deepEqual(combined.capture_reviews.slice(0,3),development.capture_reviews);assert.deepEqual(combined.capture_reviews.slice(3),validation.capture_reviews);assert.deepEqual(combined.development_review,development);assert.deepEqual({workflow,validation},before);
 combined.capture_reviews[0].raw_sha256='mutated-local-copy';assert.equal(development.capture_reviews[0].raw_sha256,'D1-sha');
 assert.throws(()=>UI.mergeValidationReview(workflow,{...validation,roi_sha256:'different'},['V1','V2','V3']),/고정 ROI/);
 assert.throws(()=>UI.mergeValidationReview(workflow,validation,['V1','V2']),/3개 확인/);
 assert.throws(()=>UI.mergeValidationReview(workflow,{...validation,capture_reviews:['D1','V2','V3'].map(row)},['D1','V2','V3']),/겹칩니다/);
});

function iqState(){const s=state();s.rois=[...rois,...Array.from({length:18},(_,i)=>({id:'color-'+i,kind:'color',box:[0,i*8,8,8]})),...Array.from({length:20},(_,i)=>({id:'gray-'+i,kind:'gray',box:[16,i*8,8,8]}))];s.iq={enabled:true,reference_id:UI.IQ_REFERENCE,actual_isp_color_space:'unknown',nominal_confirmed:true,srgb_confirmed:false};s.iqReviewed=new Set(['D1','D2','D3']);s.noiseText='';return s;}
test('IQ default is recommended in markup while legacy workflow recovery disables absent IQ',()=>{
 const html=fs.readFileSync(path.resolve(__dirname,'../../src/camera_platform/static/esfr-measured.html'),'utf8');assert.match(html,/id="measured-iq-enabled" type="checkbox" checked/);
 const s=state();assert.deepEqual(UI.iqRequest(s,'development'),{});
});
test('IQ requires nominal acknowledgment, all 38 patches and every capture review without invented criteria',()=>{
 const s=iqState();assert.deepEqual(UI.readiness(s,'development'),[]);
 assert.deepEqual(UI.iqRequest(s,'development'),{iq:{enabled:true,reference_id:UI.IQ_REFERENCE,actual_isp_color_space:'unknown',patch_roi_reviewed_capture_ids:['D1','D2','D3'],noise_repeat_ids:[]}});
 s.iq.nominal_confirmed=false;assert.throws(()=>UI.iqRequest(s,'development'),/명목 기준/);s.iq.nominal_confirmed=true;
 s.iqReviewed.delete('D2');assert.throws(()=>UI.iqRequest(s,'development'),/촬영마다/);s.iqReviewed.add('D2');s.rois.pop();assert.throws(()=>UI.iqRequest(s,'development'),/20개/);
});
test('confirmed sRGB colorimetry is separate from unknown and no fake ΔE is requested',()=>{
 const s=iqState();s.iq.actual_isp_color_space='srgb';assert.throws(()=>UI.iqRequest(s,'development'),/sRGB 색 공간 근거/);s.iq.srgb_confirmed=true;assert.equal(UI.iqRequest(s,'development').iq.actual_isp_color_space,'srgb');
});
test('repeat protocol accepts zero to five extra captures, rejects core overlap and preserves frozen extra count',()=>{
 assert.deepEqual(UI.parseCaptureIDs('E1'),['E1']);assert.deepEqual(UI.parseCaptureIDs(''),[]);assert.throws(()=>UI.parseCaptureIDs('1 2 3 4 5 6'),/0–5/);assert.throws(()=>UI.parseCaptureIDs('E1 E1'),/중복/);
 const s=iqState();s.noiseText='D1';assert.throws(()=>UI.iqRequest(s,'development'),/겹칠/);s.noiseText='E1';s.captures.push(capture('E1'));assert.throws(()=>UI.iqRequest(s,'development'),/추가 반복/);s.iqReviewed.add('E1');assert.deepEqual(UI.iqRequest(s,'development').iq.patch_roi_reviewed_capture_ids,['D1','D2','D3','E1']);
 s.workflow={contract,iq:{noise_repeat_ids:['E1']}};s.validation=new Set(['V1','V2','V3']);s.captures.push(...['V1','V2','V3','VE1'].map(id=>capture(id,'validation')));for(const id of ['V1','V2','V3','VE1'])s.iqReviewed.add(id);s.noiseText='';assert.throws(()=>UI.iqRequest(s,'validation'),/고정한 수/);s.noiseText='VE1';assert.deepEqual(UI.iqRequest(s,'validation'),{iq_patch_roi_reviewed_capture_ids:['V1','V2','V3','VE1'],iq_noise_repeat_ids:['VE1']});
});
test('mode comparison uses two completed workflow identities and IQ artifact links stay local',()=>{
 assert.deepEqual(UI.comparisonRequest('measured-esfr-A','measured-esfr-B'),{workflow_a_id:'measured-esfr-A',workflow_b_id:'measured-esfr-B'});assert.throws(()=>UI.comparisonRequest('measured-esfr-A','measured-esfr-A'),/서로 다른/);assert.throws(()=>UI.comparisonRequest('../bad','measured-esfr-B'),/workflow/);
 const links=UI.resultLinks({links:{iq_report:'/api/v1/camera-validation/esfr/measured/workflows/measured-esfr-A/iq/files/report.html',iq_bundle:'/api/v1/camera-validation/esfr/measured/workflows/measured-esfr-A/iq/files/bundle.zip'}});assert.equal(links.length,2);assert.match(links[0].label,/IQ/);
});

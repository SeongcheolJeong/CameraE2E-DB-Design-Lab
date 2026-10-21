'use strict';
const assert=require('node:assert/strict'),{test}=require('node:test');
require('../../src/camera_platform/static/calibration-lab.js');
require('../../src/camera_platform/static/camera-validation-esfr.js');
const UI=global.CameraCalibrationLab,ESFR=global.CameraESFRValidation;
const measurements=Array.from({length:112},(_,i)=>['training','final_validation'].map(role=>({id:role+'-'+i,chart:'mtf',role,source_kind:'synthetic',provenance:{esfr_run_id:'esfr-1',roi_id:'edge-'+i},observations:{field_xy:[i/112,0]}}))).flat();
function controller(extra={}){
 const calls=[];
 const c=UI.createController({api:async(path,body)=>{calls.push({path,body});if(path.startsWith('/template'))return {campaign_template:{name:'all'},charts:['mtf']};if(path==='/campaigns')return [{id:'C'}];if(path==='/campaigns/C')return {campaign:{id:'C'},measurements,studies:[],profiles:[]};if(path==='/studies'&&body)return {id:'S',status:'queued'};if(path==='/studies/S')return {id:'S',status:'succeeded',job_kind:'calibration_lab_study',result:{summary:{comparisons:measurements.filter(m=>m.role==='training').map(m=>({measurement_id:m.id,chart:'mtf'}))}}};if(path==='/studies/V')return {id:'V',status:'succeeded',job_kind:'calibration_lab_validation',parent_study_id:'S',result:{summary:{comparisons:measurements.filter(m=>m.role==='final_validation').map(m=>({measurement_id:m.id,chart:'mtf'}))}}};throw Error(path);},campaign:'C',...extra});return {c,calls};
}
test('112 training and112 heldout measurements remain selected and sent intact',async()=>{
 const {c,calls}=controller();await c.init();assert.equal(c.state.training.size,112);assert.equal(c.state.validation.size,112);
 let html=UI.view(c.state);assert.equal((html.match(/data-select="training"/g)||[]).length,112);assert.equal((html.match(/data-select="validation"/g)||[]).length,112);
 await c.perform('select-none','training');assert.equal(c.state.training.size,0);await c.perform('select-all','training');assert.equal(c.state.training.size,112);
 await c.perform('study');assert.equal(calls.find(r=>r.path==='/studies'&&r.body).body.measurement_ids.length,112);
 assert.equal(UI.edgeCoverage(c.state).trainingCompared,112);
});
test('exact validation deep link restores heldout coverage without refitting',async()=>{
 const {c,calls}=controller({study:'S',validation:'V'});await c.init();assert.equal(c.state.validationJob.id,'V');assert.equal(c.state.completionValidation,'V');assert.equal(UI.edgeCoverage(c.state).validationCompared,112);assert.ok(calls.every(x=>x.body===undefined));
});
test('image validation displays actual metrics and requires exact frozen report URL',()=>{
 const job={id:'V',result:{summary:{esfr_image_validation:{report_url:'/api/v1/calibration-lab/studies/V/files/esfr-images/report.html',field_count:112,comparison_count:336,metrics:{raw_sfr_rmse:{before:.22,after:.04},isp_sfr_rmse:{before:.15,after:.03}},validation:{status:'fail'}}}}};
 const html=UI.esfrImageValidationCard(job);assert.match(html,/112개 위치/);assert.match(html,/0.04/);assert.match(html,/수치 기준 미충족/);
 job.result.summary.esfr_image_validation.report_url='/other';assert.match(UI.esfrImageValidationCard(job),/일치하지 않습니다/);
});
test('export panel exposes all112 roles and blockers without declaring camera validation',()=>{
 const html=ESFR.calibrationExportView({lab_url:'/calibration-lab.html?campaign=C',training_ids:Array(112).fill('T'),validation_ids:Array(112).fill('V'),study_blockers:[]});
 assert.match(html,/보정 112건 \/ 독립 검증 112건/);assert.doesNotMatch(html,/카메라 합격/);
 assert.match(ESFR.calibrationExportView({lab_url:'/calibration-lab.html?campaign=C',study_blockers:['actual <block>']}),/actual &lt;block&gt;/);
 assert.match(ESFR.calibrationExportView({lab_url:'https://bad.example'}),/올바르지 않습니다/);
});

function progressController(){
 const calls=[],timers=[];let progress={id:'S',request_hash:'request-1',job_kind:'calibration_lab_study',campaign_id:'C',parent_study_id:null,status:'running',progress:{fraction:.4},scope:'operational_state_only_not_evidence_verification'},full={id:'S',content_hash:'request-1',job_kind:'calibration_lab_study',specification:{campaign_id:'C',measurement_ids:['frozen-input']},status:'running',progress:{fraction:.1}};
 let failure=null;
 const c=UI.createController({api:async(path,body)=>{calls.push({path,body});if(path.startsWith('/template'))return {campaign_template:{name:'all'},charts:['mtf']};if(path==='/campaigns')return [{id:'C'}];if(path==='/campaigns/C')return {campaign:{id:'C'},measurements,studies:[],profiles:[]};if(path==='/studies/S')return structuredClone(full);if(path.startsWith('/studies/S/progress?')){if(failure)throw failure;return structuredClone(progress);}throw Error(path);},campaign:'C',study:'S',setTimer:fn=>{timers.push(fn);return timers.length;},clearTimer:()=>{}});
 return {c,calls,timers,setProgress:value=>{progress={...progress,...value};},setFull:value=>{full={...full,...value};},fail:value=>{failure=value;}};
}
test('running progress uses request-bound compact reads and only terminal loads full evidence',async()=>{
 const h=progressController();await h.c.init();assert.equal(h.calls.filter(x=>x.path==='/studies/S').length,1);const immutable=h.c.state.job.specification;
 await h.timers.shift()();assert.equal(h.calls.filter(x=>x.path==='/studies/S').length,1);assert.equal(h.c.state.job.specification,immutable);assert.equal(h.c.state.job.progress.fraction,.4);assert.equal(h.calls.at(-1).path,'/studies/S/progress?expected_request_hash=request-1');
 h.setProgress({status:'succeeded',result:{summary:{unverified:true}}});h.setFull({status:'succeeded',result:{summary:{verified_result:true}}});await h.timers.shift()();
 assert.equal(h.calls.filter(x=>x.path==='/studies/S').length,2);assert.deepEqual(h.c.state.job.result,{summary:{verified_result:true}});assert.equal(h.c.state.job.result.summary.unverified,undefined);
});
test('compact mismatch or network error preserves known request and never fetches or displays untrusted terminal result',async()=>{
 const h=progressController();await h.c.init();const known=h.c.state.job;
 h.setProgress({request_hash:'wrong',status:'succeeded'});await h.timers.shift()();assert.equal(h.c.state.job,known);assert.match(h.c.state.error,/요청 해시/);assert.equal(h.calls.filter(x=>x.path==='/studies/S').length,1);
 h.setProgress({request_hash:'request-1'});h.fail(Error('network unavailable'));await h.c.perform('refresh-study');assert.equal(h.c.state.job,known);assert.match(h.c.state.error,/network unavailable/);
});
test('compact parent and campaign binding are enforced',async()=>{
 const h=progressController();await h.c.init();const known=h.c.state.job;h.setProgress({campaign_id:'other'});await h.timers.shift()();assert.equal(h.c.state.job,known);assert.match(h.c.state.error,/캠페인/);
 h.setProgress({campaign_id:'C',parent_study_id:'unrelated'});await h.c.perform('refresh-study');assert.equal(h.c.state.job,known);assert.match(h.c.state.error,/상위 보정/);
});
test('terminal full response cannot swap immutable request hash',async()=>{
 const h=progressController();await h.c.init();const known=h.c.state.job;h.setProgress({status:'succeeded'});h.setFull({status:'succeeded',content_hash:'changed',result:{summary:{wrong:true}}});await h.timers.shift()();assert.equal(h.c.state.job,known);assert.match(h.c.state.error,/고정 요청 해시/);
});

test('E-SFR training-only selection defaults to native forward observer and explicit analytic choice is preserved',async()=>{
 const {c,calls}=controller();await c.init();assert.equal(UI.studyFitMethod(c.state),'esfr_forward_observer_v1');
 await c.perform('study');assert.equal(calls.find(x=>x.path==='/studies'&&x.body).body.fit_method,'esfr_forward_observer_v1');
 await c.change('fit-method','analytic_curve');assert.equal(UI.studyFitMethod(c.state),'analytic_curve');
 c.state.fitMethod='auto';c.state.development.add('unknown-development');assert.equal(UI.studyFitMethod(c.state),'analytic_curve');
});
test('forward provenance separates old failures and new independent data and validates frozen candidate identity',()=>{
 const job={result:{summary:{fit:{forward_observer:{method:'development_native_bayer_same_observer_gaussian_v1',fit_data_role:'development_only',field_count:112,development_metrics:{raw_sfr_rmse:{before:.04,after:.005,count:336,worst_after:.01}}}}}}};
 const image={candidate_sha256:'hash',candidate_frozen_before_new_acquisition:true,validation_acquisition_status:'new_independent_synthetic_acquisition',acquisition_contract:{id:'acquisition-1',created_at:'time',candidate_sha256:'hash',new_validation_capture_ids:['N1','N2','N3'],thresholds:{raw_sfr_rmse:.03,isp_sfr_rmse:.05}},comparison_history:{focus_roi_id:'corner-edge',thresholds_unchanged:true,prior_report_url:'/api/v1/calibration-lab/completion-reports/old/files/report.html',rows:[{dataset_role:'previous_holdout',fit_method:'analytic_curve',capture_id:'OLD1',roi_id:'corner-edge',raw_rmse:.041,raw_threshold:.03,isp_rmse:.01,isp_threshold:.05,status:'fail'},{dataset_role:'new_holdout',fit_method:'forward_observer',capture_id:'NEW1',roi_id:'corner-edge',raw_rmse:.006,raw_threshold:.03,isp_rmse:.01,isp_threshold:.05,status:'pass'}]}};
 const validation={result:{summary:{esfr_image_validation:image}}};let html=UI.forwardObserverCard(job,validation);
 for(const text of ['개발 촬영만 사용','이전 검증 촬영 · 이미 확인한 이력','새 독립 검증 촬영','OLD1','0.041','NEW1','0.006','기존 허용 기준 유지 확인','data-frozen-before-acquisition="true"'])assert.ok(html.includes(text),text);
 image.acquisition_contract.candidate_sha256='other';html=UI.forwardObserverCard(job,validation);assert.match(html,/data-frozen-before-acquisition="false"/);
 assert.equal(UI.historyReportURL('https://other/report.html'),null);
});

test('fresh validation deep link binds exact112 measurements while retaining old112 in the library',async()=>{
 const fresh=measurements.filter(m=>m.role==='final_validation').map(m=>({...m,id:'fresh-'+m.id})),all=[...measurements,...fresh],ids=fresh.map(m=>m.id);
 const c=UI.createController({campaign:'C',study:'S',validation:'V-new',api:async(path)=>{
  if(path.startsWith('/template'))return {campaign_template:{name:'all'},charts:['mtf']};
  if(path==='/campaigns')return [{id:'C'}];
  if(path==='/campaigns/C')return {campaign:{id:'C'},measurements:all,studies:[],profiles:[]};
  if(path==='/studies/S')return {id:'S',status:'succeeded',job_kind:'calibration_lab_study'};
  if(path==='/studies/V-new')return {id:'V-new',status:'succeeded',job_kind:'calibration_lab_validation',parent_study_id:'S',specification:{campaign_id:'C',measurement_ids:ids},result:{summary:{comparisons:ids.map(id=>({measurement_id:id,chart:'mtf'}))}}};
  throw Error(path);
 }});
 await c.init();assert.equal(c.state.error,'');assert.deepEqual([...c.state.validation].sort(),[...ids].sort());
 assert.equal(c.state.detail.measurements.length,336);assert.equal(UI.edgeCoverage(c.state).validationAvailable.length,224);assert.equal(UI.edgeCoverage(c.state).validation.length,112);assert.equal(UI.edgeCoverage(c.state).validationCompared,112);
 const html=UI.view(c.state);assert.equal((html.match(/data-select="validation"/g)||[]).length,224);assert.match(html,/전체 검증 측정 224건/);assert.match(html,/현재 검증 실행은 112개 측정/);
});

test('development metrics name analytical initializer and retain increasing curve diagnostic and acquisitionIDs',()=>{
 const forward={method:'development_native_bayer_same_observer_gaussian_v1',fit_data_role:'development_only',field_count:112,development_metrics:{raw_sfr_rmse:{before:.0074,after:.0026,count:336}},fields:[{roi_id:'corner',analytic_curve_rmse:{before:.00456,after:.03727},raw_sfr_rmse:{before:.03549,after:.01},isp_sfr_rmse:{before:.01584,after:.0073}}]};
 const image={acquisition_contract:{new_validation_capture_ids:['fresh-acq-1','fresh-acq-2','fresh-acq-3']},comparison_history:{focus_roi_id:'corner'}};
 const html=UI.forwardObserverCard({result:{summary:{fit:{forward_observer:forward}}}},{result:{summary:{esfr_image_validation:image}}});
 for(const text of ['기존 해석식 후보 평균 RMSE','Forward 후보 평균 RMSE','해석식 곡선 RMSE · 목적함수 아님','별도 진단으로 증가할 수 있습니다','0.00456','0.03727','fresh-acq-1','fresh-acq-2','fresh-acq-3'])assert.ok(html.includes(text),text);
 assert.doesNotMatch(html,/초기 평균 RMSE/);
});

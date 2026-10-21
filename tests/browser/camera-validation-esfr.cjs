'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
require('../../src/camera_platform/static/camera-validation-esfr.js');
const UI=global.CameraESFRValidation;
test('source and DB link preserve the exact tuple',()=>{
 const source=UI.sourceFrom(new URLSearchParams('source_run=run-1&candidate=candidate-03&condition=condition-006'));
 assert.deepEqual(UI.sourceValue(source),{run_id:'run-1',candidate_id:'candidate-03',condition_id:'condition-006'});
 assert.equal(UI.dbURL(source),'/db-design.html?run=run-1&candidate-b=candidate-03&condition=condition-006');
 assert.equal(UI.labURL(source,'study-1'),'/camera-validation-esfr.html?source_run=run-1&candidate=candidate-03&condition=condition-006&study=study-1');
 assert.equal(UI.sourceValue({}),null);
 assert.throws(()=>UI.sourceValue({run_id:'partial'}),/모두/);
});
test('ROI boxes preserve native raster axes and reject out-of-bounds edits',()=>{
 assert.deepEqual(UI.normalizeRois([{id:'edge-1',kind:'edge',box:[13,21,50,40]}],[100,200]),[{id:'edge-1',kind:'edge',box:[13,21,50,40]}]);
 assert.deepEqual(UI.normalizeRois([{id:'x',x:21,y:13,width:40,height:50}],[100,200]),[{id:'x',kind:'edge',box:[13,21,50,40]}]);
 assert.throws(()=>UI.normalizeRois([{id:'x',box:[90,1,11,10]}],[100,200]),/범위/);
 assert.throws(()=>UI.normalizeRois([{id:'x',box:[1.2,1,8,8]}],[100,200]),/정수/);
 assert.throws(()=>UI.normalizeRois([{id:'x',box:[1,1,8,8]},{id:'x',box:[1,1,8,8]}],[100,200]),/중복/);
});
test('source image dimensions use explicit RAW format and role selection cannot overlap',()=>{
 assert.deepEqual(UI.shapeOf({metadata:{raw_format:{height:1080,width:1920}}}),[1080,1920]);
 assert.equal(UI.shapeOf({}),null);
 assert.deepEqual(UI.splitSets([{id:'a'},{id:'b'},{id:'c'}],{a:'development',b:'validation'}),{development_ids:['a'],validation_ids:['b']});
});
test('report and preview links reject unrelated hosts and path traversal',()=>{
 assert.equal(UI.localURL('https://unrelated.example/evil'),null);
 assert.equal(UI.localURL('//unrelated.example/evil'),null);
 assert.equal(UI.localURL('/api/v1/camera-validation/esfr/../secret'),null);
 assert.equal(UI.fileURL('../bad','report.html'),null);
 assert.equal(UI.fileURL('study-1','../bad'),null);
 assert.equal(UI.fileURL('study-1','report.html'),'/api/v1/camera-validation/esfr/studies/study-1/files/report.html');
});
test('captured labels are escaped and synthetic data are explicitly marked',()=>{
 const html=UI.captureView([{id:'c1',source_kind:'synthetic',metadata:{name:'<script>bad</script>',raw_format:{height:10,width:20}}}],{});
 assert.ok(!html.includes('<script>'));
 assert.ok(html.includes('&lt;script&gt;'));
 assert.ok(html.includes('합성 취득'));
});
test('completed report does not invent camera pass and prevents unbound model apply',()=>{
 const html=UI.resultView({id:'study-1',status:'succeeded',result:{summary:{source_kind:'synthetic',roi_count:9}}},null,false);
 assert.ok(html.includes('합성 시험'));
 assert.ok(html.includes('report.html'));
 assert.ok(html.includes('evidence.zip'));
 assert.match(html,/id="esfr-apply"[^>]*disabled/);
 assert.ok(!html.includes('카메라 합격'));
});
test('API adds local token only on mutations and preserves multipart boundary',async()=>{
 const calls=[];const request=async(url,opts={})=>{calls.push([url,opts]);return {ok:true,status:200,json:async()=>url==='/api/v1/session'?{token:'test-token'}:{id:'ok'}};};
 const api=UI.createAPI(request);
 await api('/captures');assert.equal(calls.length,1);assert.equal(calls[0][1].method,'GET');
 calls.length=0;await api('/studies',{name:'a'});assert.equal(calls[0][0],'/api/v1/session');assert.equal(calls[1][1].headers['X-Platform-Token'],'test-token');assert.equal(calls[1][1].headers['Content-Type'],'application/json');
 calls.length=0;const form=new FormData();form.append('metadata','{}');await api('/captures',form);assert.equal(calls[1][1].body,form);assert.equal(calls[1][1].headers['Content-Type'],undefined);
});
test('polling waits for response and does not deliver stale job after another selection',async()=>{
 let resolveOld;const seen=[],timers=[];const poller=UI.createPoller({api:path=>path.endsWith('old')?new Promise(r=>{resolveOld=r;}):Promise.resolve({id:'new',status:'running'}),onJob:j=>seen.push(j.id),onError:e=>{throw e;},setTimer:fn=>{timers.push(fn);return timers.length;},clearTimer:()=>{}});
 const pending=poller.select('old');await poller.select('new');resolveOld({id:'old',status:'succeeded'});await pending;
 assert.deepEqual(seen,['new']);assert.equal(timers.length,1);poller.stop();await timers[0]();assert.deepEqual(seen,['new']);
});
test('mismatched response identity is rejected before display',async()=>{
 let caught;const poller=UI.createPoller({api:async()=>({id:'wrong',status:'succeeded'}),onJob:()=>assert.fail('wrong job shown'),onError:e=>caught=e});await poller.select('requested');assert.match(caught.message,/ID/);
});
test('measured captures without physical qualification stay measured',()=>{
 const html=UI.resultView({id:'s-1',status:'succeeded',result:{summary:{source_kind:'measured',physical_validation:'not_established',roi_count:3,metrics:{raw_sfr_rmse:{before:.2,after:.03},isp_sfr_rmse:{before:.15,after:.02}}}}},null,false);
 assert.ok(html.includes('업로드 실측 자료 분석'));
 assert.ok(!html.includes('합성 시험'));
 assert.ok(html.includes('RAW SFR RMSE'));
 assert.ok(html.includes('ISP SFR RMSE'));
 assert.ok(html.includes('0.2 → 0.03'));
 assert.ok(html.includes('0.15 → 0.02'));
 assert.ok(html.includes('/result.json'));
 assert.ok(html.includes('id="esfr-export"'));
});
test('DB application requires passed validation and fitted nonempty fields and permits nonconditional source',()=>{
 const source={run_id:'run-1',candidate_id:'c-1'};
 assert.deepEqual(UI.sourceValue(source),source);
 const job={id:'s-1',status:'succeeded',source,result:{fit:{status:'fitted'},candidate_profile:{camera_id:'camera-a',fields:[{id:'f-1',sigma_native_px:1}]},summary:{validation:{status:'pass'}}}};
 assert.ok(UI.canApply(job));
 assert.doesNotMatch(UI.resultView(job,{loaded:true,id:null},false),/id="esfr-apply"[^>]*disabled/);
 job.result.candidate_profile.fields=[];
 assert.ok(!UI.canApply(job));
 assert.match(UI.resultView(job,{loaded:true,id:null},false),/id="esfr-apply"[^>]*disabled/);
 job.result.candidate_profile.fields=[{id:'f-1'}];job.result.fit.status='failed';
 assert.ok(!UI.canApply(job));
});
test('active CAS uses profile identity rather than activation event and handles null',()=>{
 assert.equal(UI.activeID(null),null);
 assert.equal(UI.activeID({id:'activation-1',profile_id:'profile-1',profile:{id:'profile-1'}}),'profile-1');
});
test('capture role remains immutable in selection controls',()=>{
 const html=UI.captureView([{id:'v1',metadata:{role:'validation',source_kind:'measured',session_id:'heldout-session'}}],{});
 assert.ok(html.includes('value="validation"'));
 assert.ok(!html.includes('value="development"'));
 assert.ok(html.includes('heldout-session'));
});
test('fixed profile replay is marked as same-data operation and can never be refitted/applied',()=>{
 const job={id:'s-1',status:'succeeded',source:{run_id:'run-1',candidate_id:'c-1'},result:{summary:{fit:{status:'frozen_replay'},source_kind:'measured'},candidate_profile:{camera_id:'cam-a',fields:[{id:'f-1'}]}}};
 const html=UI.resultView(job,{loaded:true,id:'profile-1'},false);
 assert.ok(html.includes('id="esfr-replay"'));
 assert.ok(html.includes('고정 프로파일 동작 확인 · 새로운 독립 실측 아님'));
 assert.match(html,/id="esfr-apply"[^>]*disabled/);
 assert.ok(!UI.canApply(job));
});
test('active profile query source preserves requested optional-condition identity',()=>{
 const requested={run_id:'run-1',candidate_id:'c-1'};
 assert.deepEqual(UI.sourceOf({specification:{source:requested},source_context:{...requested,condition_id:'resolved-default'}}),requested);
});
test('chart manifest keeps actual feature counts and never substitutes expected values',()=>{
 const model=UI.chartModel({counts:{stars:13,side_squares:2,gray_patches:20,color_patches:18,wedge_groups:16,fiducials:4}});
 assert.ok(model.counts.every(item=>item.match));
 assert.deepEqual(model.counts.map(item=>item.count),[13,2,20,18,16,4]);
 const missing=UI.chartModel({counts:{stars:12,side_squares:-1}});
 assert.equal(missing.counts[0].count,12);assert.equal(missing.counts[0].match,false);
 assert.equal(missing.counts[1].count,null);assert.equal(missing.counts[2].count,null);
 assert.equal(UI.chartModel({counts:{wedge_pairs:12}}).counts[4].count,null);
});
test('chart previews are restricted to exact local chart assets',()=>{
 const root=UI.ROOT+'/chart-assets/';
 for(const name of ['generated.svg','generated-rois.svg','reference.png','manifest.json'])assert.equal(UI.chartAssetURL(root+name),root+name);
 for(const value of ['https://example.com/reference.png',root+'../secret.svg',root+'%2e%2e.svg',root+'generated.svg?x=1',root+'generated.svg#x',UI.ROOT+'/studies/s/files/report.html'])assert.equal(UI.chartAssetURL(value),null);
});
test('chart sources link authoritative publishers and preserve source labels as plain data',()=>{
 const model=UI.chartModel({title:'<img src=x onerror=alert(1)>',sources:[{title:'<b>Imatest</b>',url:'https://www.imatest.com/imaging/iso-12233/'},{title:'ISO',url:'https://www.iso.org/standard/71696.html'},{title:'bad',url:'javascript:alert(1)'},{title:'lookalike',url:'https://imatest.com.evil.example/'},'https://docs.imatest.com/docs/esfriso_instructions']});
 assert.equal(model.sources.length,3);
 assert.equal(model.sources[0].label,'<b>Imatest</b>');
 assert.equal(model.version,'<img src=x onerror=alert(1)>');
 assert.equal(UI.sourceURL('data:text/html,test'),null);
 assert.equal(UI.sourceURL('http://www.imatest.com/'),null);
});
test('chart manifest qualifications are preserved without declaring ISO conformance',()=>{
 const qualification={algorithm_conformance:'not_established',print_mtf:'unmeasured'};
 const model=UI.chartModel({qualification,geometry:{units:'normalized_chart_coordinates'},preview_url:'https://evil.example/chart.svg'});
 assert.deepEqual(model.qualification,qualification);
 assert.deepEqual(model.geometry,{units:'normalized_chart_coordinates'});
 assert.equal(model.preview,null);
});
test('four fiducial clicks preserve native x/y order and reject partial or off-image points',()=>{
 const points=[[207,118],[433,118],[207,254],[433,254]];
 assert.deepEqual(UI.fiducialPayload('capture-1',points,[360,640]),{capture_id:'capture-1',fiducials_native_xy:points});
 const value=UI.fiducialPayload('capture-1',points,[360,640]);value.fiducials_native_xy[0][0]=0;assert.equal(points[0][0],207);
 assert.throws(()=>UI.fiducialPayload('capture-1',points.slice(0,3),[360,640]),/네 개/);
 assert.throws(()=>UI.fiducialPayload('capture-1',[[640,1],...points.slice(1)],[360,640]),/범위/);
 assert.throws(()=>UI.fiducialPayload('capture-1',[points[0],points[0],...points.slice(2)],[360,640]),/달라야/);
 assert.throws(()=>UI.fiducialPayload('capture-1',[[NaN,1],...points.slice(1)],[360,640]),/범위/);
});

test('dense ROI preview uses compact indices and keeps full IDs accessible',()=>{
 const id='star-top-left-diagonal-edge-very-long-id';
 const markup=UI.roiView([{id,kind:'edge',box:[10,20,30,40]}],2560,id);
 assert.match(markup, /class="roi-item selected"/);
 assert.match(markup, /role="button" tabindex="0"/);
 assert.match(markup, /aria-pressed="true"/);
 assert.ok(markup.includes('<title>ROI 1: '+id));
 assert.match(markup, /class="roi-label"[^>]*>1<\/text>/);
 assert.ok(!markup.includes('>'+id+'</text>'));
 const escaped=UI.roiView([{id:'<bad>',kind:'edge',box:[10,20,30,40]}],2560);
 assert.ok(!escaped.includes('<bad>'));assert.ok(escaped.includes('&lt;bad&gt;'));
});

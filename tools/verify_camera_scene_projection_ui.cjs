'use strict';
// Functional browser proof. Writes are allowed only in the exact separate scene QA state.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {isDeepStrictEqual:same}=require('node:util');
async function stateRegression(){
 const vm=require('node:vm');
 const sourcePath=path.resolve(__dirname,'../src/camera_platform/static/scenes.js'),source=fs.readFileSync(sourcePath,'utf8');
 const seam='return {view};';assert(source.includes(seam));
 const result={schema:'camera_scene_projection_state_regression_v1',status:'passed',checks:[],source_sha256:crypto.createHash('sha256').update(source).digest('hex')};
 const check=name=>result.checks.push({name,status:'passed'});
 const artifact={name:'spectral-image.npy',url:'/api/v1/artifacts/new-irradiance',sha256:'abc'};
 const job=(id,sourceId,status)=>({id,name:id,status,source_render_id:sourceId,assembly_id:'camera',request:{source_render_id:sourceId,assembly_id:'camera'},result:{artifacts:[artifact],summary:{radiance_reused:true}}});
 function harness(){
  const timers=[],requests=[],running=[];
  const data={state_dir:'/isolated/vm-state',scene_projections:[job('A','source-A','queued'),job('B','source-B','queued')],assemblies:[{id:'camera',name:'Camera'}]};
  const state={view:'scenes',data};
  const api=async(endpoint,payload)=>{requests.push({endpoint,payload});if(endpoint==='/scene_projections')return {...job('cached','source-A','succeeded'),cache_hit:true};return job(endpoint.split('/').at(-1),'source-'+endpoint.split('/').at(-1),'succeeded');};
  const sandbox={window:{},document:{activeElement:null,addEventListener:()=>{}},sessionStorage:{setItem:()=>{},getItem:()=>null},location:{hash:'#scenes'},TextEncoder,structuredClone,setTimeout:fn=>{timers.push(fn);}};
  vm.runInNewContext(source.replace(seam,'return {view,__test:{s,p,pollProjection,resetProjection,loadProjection,projectionView,projectionAction,acceptProjection}};'),sandbox,{filename:sourcePath});
  const ui=sandbox.window.CameraSceneUI({api,esc:String,records:key=>data[key]||[],badge:status=>status,details:()=>'',heading:()=>'',recordButton:()=>'',render:()=>{},refresh:async()=>{},toast:()=>{},busy:(_button,fn)=>{const promise=fn();running.push(promise);return promise;},state,chooseRun:()=>{}});
  return {test:ui.__test,timers,requests,running,data};
 }
 {
  const h=harness(),t=h.test;
  t.resetProjection('source-A','camera');t.p.info=h.data.scene_projections[0];t.pollProjection();
  t.resetProjection('source-B','camera');t.p.info=h.data.scene_projections[1];
  await h.timers.shift()();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].endpoint,'/scene_projections/A');assert.equal(t.p.info.id,'B');assert.equal(h.timers.length,1);
  check('Timer captures A identities even when B is selected before it fires');
  await h.timers.shift()();assert.equal(t.p.info.id,'B');assert.equal(t.p.info.status,'succeeded');
  const html=t.projectionView({id:'source-B',status:'succeeded',request:{assembly_id:'camera'}});
  assert(html.includes('data-scene-action="projection-camera" >'));assert(html.includes(artifact.url));
  check('B terminal detail and Camera action recover without a page reload');
 }
 {
  const h=harness(),t=h.test;
  t.resetProjection('source-A','camera');t.p.info=h.data.scene_projections[0];t.pollProjection();
  t.resetProjection('source-B','camera');t.resetProjection('source-A','camera');t.p.info=h.data.scene_projections[0];
  await h.timers.shift()();assert.equal(t.p.info.status,'succeeded');
  check('A to B to A selection accepts the exact same terminal job despite a newer view epoch');
  t.acceptProjection(job('A','source-A','queued'));assert.equal(t.p.info.status,'succeeded');
  check('Delayed queued responses cannot replace a terminal observation');
  t.p.info=job('A','source-A','queued');await t.loadProjection();assert.equal(t.p.info.status,'succeeded');
  check('A terminal collection refresh reloads a stale selected detail');
 }
 {
  const h=harness(),t=h.test;
  t.resetProjection('source-A','camera');t.s.job='source-A';
  const draft={name:'cached',source_render_id:'source-A',assembly_id:'camera',projection:{on_axis_transmittance:{wavelength_nm:[500,600],values:[.4,.4]},source_uri:'fixture://explicit',method_and_version:'declared:1'},force:false};
  t.p.text=JSON.stringify(draft);t.p.preview={status:'ready',draft,preview_sha256:'proof'};
  t.projectionAction({dataset:{sceneAction:'projection-submit'}});await Promise.all(h.running);
  assert.equal(t.p.info.id,'cached');assert.equal(t.p.info.artifacts.length,1);assert.equal(t.p.info.artifacts[0].url,artifact.url);
  const html=t.projectionView({id:'source-A',status:'succeeded',request:{assembly_id:'camera'}});
  assert(html.includes('href="'+artifact.url+'"'));assert(html.includes('새 센서면 광량'));
  check('Completed cache submit normalizes nested artifacts into visible original links');
 }
 if(process.argv[3]){const output=path.resolve(process.argv[3]);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');}
 console.log(JSON.stringify(result));
}
if(process.argv[2]==='--state-regression'){
 stateRegression().catch(error=>{console.error(error);process.exitCode=1;});
}else{
const {chromium}=require('playwright');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_scene_projection_ui_v1',base_url:base,checks:[],errors:[],posts:[],records:{},physical_validation:'not_established',user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const save=()=>fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');
function floats(bytes){
  assert.equal(bytes.subarray(0,6).toString('latin1'),'\x93NUMPY');
  const version=bytes[6],start=version===1?10:12,length=version===1?bytes.readUInt16LE(8):bytes.readUInt32LE(8);
  const header=bytes.subarray(start,start+length).toString('utf8');
  assert(header.includes("'descr': '<f8'")&&header.includes("'fortran_order': False"),header);
  const values=[];for(let offset=start+length;offset<bytes.length;offset+=8)values.push(bytes.readDoubleLE(offset));
  return values;
}
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 const page=await browser.newPage({viewport:{width:1480,height:1080}});
 page.on('pageerror',e=>report.errors.push(String(e)));
 page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
 const get=async endpoint=>{const r=await page.request.get(base+'/api/v1/'+endpoint);assert(r.ok(),await r.text());return r.json();};
 const bytes=async artifact=>{const r=await page.request.get(base+artifact.url);assert(r.ok());const value=await r.body();assert.equal(hash(value),artifact.sha256);return value;};
 const postClick=async(selector,endpoint)=>{
  const waiting=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/'+endpoint);
  await page.locator(selector).click();const r=await waiting;assert(r.ok(),await r.text());return r.json();
 };
 const waitJob=async(kind,id)=>{
  const deadline=Date.now()+120000;
  while(Date.now()<deadline){const j=await get(kind+'/'+id);if(['succeeded','failed','cancelled'].includes(j.status)){assert.equal(j.status,'succeeded',JSON.stringify(j.error));return j;}await new Promise(r=>setTimeout(r,500));}
  throw new Error('Observation timed out; inspect the existing '+kind+' job '+id);
 };
 try{
  const before=await get('bootstrap');
  check('Exact separate QA state before any write',before.state_dir===input.state_dir&&path.basename(before.state_dir).startsWith('camera-platform-scene-qa'));
  report.state_dir=before.state_dir;report.static_sha256={};
  for(const name of ['scenes.js','style.css']){const r=await page.request.get(base+'/'+name);assert(r.ok());report.static_sha256[name]=hash(await r.body());}
  const pair=input.records.pairs[0],target=input.target_assembly_id||input.records.assembly;
  const source=await get('scene_renders/'+pair.render),assembly=await get('assemblies/'+target);
  const counts=(await get('workbench')).counts;
  const selectSource=async()=>{
   await page.goto(base+'/#scenes');await page.locator('[data-scene-action="new"]:enabled').waitFor();
   await page.locator('#scene-selected').selectOption(pair.scene);await page.locator('#scene-job').selectOption(pair.render);
   await page.locator('[data-scene-projection]').waitFor();await page.locator('#scene-projection-assembly').selectOption(target);
  };
  const load=async()=>{await page.locator('[data-scene-action="projection-template"]').click();await page.locator('#scene-projection-throughput-0').waitFor();};
  const fill=async()=>{
   const d=JSON.parse(await page.locator('#scene-projection-json').inputValue());
   for(let i=0;i<d.projection.on_axis_transmittance.values.length;i++)await page.locator('#scene-projection-throughput-'+i).fill('.4');
   await page.locator('#scene-projection-source').fill('fixture://projection-ui/declared-throughput-0.4');
   await page.locator('#scene-projection-method').fill('synthetic-throughput-assumption:1');
   await page.locator('#scene-projection-name').fill('UI saved radiance reuse · '+new Date().toISOString());
  };
  const preview=()=>postClick('[data-scene-action="projection-preview"]','scene_projections/preview');
  await selectSource();
  let attempts=0;const templateRoute='**/api/v1/scene_authoring/projection-template?*';
  await page.route(templateRoute,async route=>{attempts++;await route.fulfill({status:503,contentType:'application/json',body:'{"detail":"Intentional verifier transport failure"}'});});
  await page.locator('[data-scene-action="projection-template"]').click();
  await page.locator('[data-scene-action="projection-template"]:enabled').waitFor();
  await page.waitForTimeout(250);
  check('Template transport failure does not auto-retry or erase source identity',attempts===1&&await page.locator('#scene-job').inputValue()===pair.render);
  await page.unroute(templateRoute);await load();
  const empty=JSON.parse(await page.locator('#scene-projection-json').inputValue());
  check('Unknown throughput remains empty and blocks preview and submission',empty.projection.on_axis_transmittance.values.every(v=>v===null)&&await page.locator('[data-scene-action="projection-preview"]').isDisabled()&&await page.locator('[data-scene-action="projection-submit"]').isDisabled());
  check('Template loading performs no POST and creates no record',report.posts.length===0&&same((await get('workbench')).counts,counts));
  await fill();
  const valid=await page.locator('#scene-projection-json').inputValue();
  await page.locator('#scene-projection-json').evaluate(el=>el.closest('details').open=true);
  const changed=JSON.parse(valid);changed.projection.focal_length_mm*=2;
  await page.locator('#scene-projection-json').fill(JSON.stringify(changed));await page.locator('#scene-projection-json').dispatchEvent('change');
  const denied=await preview();
  check('Changed focal geometry requires rerender before a job can be submitted',denied.status==='rerender_required'&&denied.issues.length>0&&await page.locator('[data-scene-action="projection-submit"]').isDisabled());
  for(const invalid of ['{}','null','[]','{','']){
   await page.locator('#scene-projection-json').evaluate(el=>el.closest('details').open=true);
   await page.locator('#scene-projection-json').fill(invalid);await page.locator('#scene-projection-json').dispatchEvent('change');
   assert(await page.locator('[data-scene-action="projection-preview"]').isDisabled());
   assert(await page.locator('[data-scene-action="projection-submit"]').isDisabled());
   assert.equal(await page.locator('#scene-projection-json').inputValue(),invalid);
  }
  await page.locator('#scene-projection-json').fill(valid);await page.locator('#scene-projection-json').dispatchEvent('change');
  check('Malformed projection drafts remain editable and require a fresh preview',report.errors.length===0&&await page.locator('[data-scene-action="projection-submit"]').isDisabled());
  let plan=await preview();check('Explicit throughput and unchanged geometry are ready',plan.status==='ready'&&plan.physical_validation==='not_established');
  await page.locator('#scene-projection-name').fill('UI saved radiance · updated '+new Date().toISOString());
  check('Every edit invalidates the prior approval',await page.locator('[data-scene-action="projection-submit"]').isDisabled()&&await page.locator('[data-scene-projection-preview]').count()===0);
  plan=await preview();
  check('Preview leaves the source camera and renderer job untouched',same(await get('assemblies/'+target),assembly)&&same(await get('scene_renders/'+pair.render),source)&&same((await get('workbench')).counts,counts));
  for(const [label,width,height] of [['desktop',1480,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.locator('[data-scene-projection]').scrollIntoViewIfNeeded();
   await page.locator('#toast').waitFor({state:'hidden',timeout:15000});
   check(label+' projection form has no horizontal page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('[data-scene-projection]').screenshot({path:path.join(out,'projection-'+label+'.png')});
  }
  await page.setViewportSize({width:1480,height:1080});
  const queued=await postClick('[data-scene-action="projection-submit"]','scene_projections');report.records.projection=queued.id;save();
  const projected=await waitJob('scene_projections',queued.id);
  report.environment=projected.environment;report.records.source_render=pair.render;report.records.assembly=target;
  check('Projection finishes without creating another ray-render job',(await get('workbench')).counts.scene_renders===counts.scene_renders&&projected.request.source_render_id===pair.render&&projected.request.assembly_id===target);
  const originalRadiance=source.artifacts.find(a=>a.name==='spectral-radiance.npy'),retainedRadiance=projected.artifacts.find(a=>a.name==='spectral-radiance.npy');
  check('Original radiance evidence hash is retained exactly',retainedRadiance.sha256===originalRadiance.sha256&&hash(await bytes(retainedRadiance))===originalRadiance.sha256);
  const irradiance=projected.artifacts.find(a=>a.name==='spectral-image.npy');
  const L=floats(await bytes(originalRadiance)),E=floats(await bytes(irradiance)),N=projected.request.projection.f_number;
  check('Downloaded new irradiance equals the declared aperture and throughput conversion',E.length===L.length&&E.every((v,i)=>Math.abs(v-L[i]*Math.PI*.4/(4*N*N))<=1e-12*Math.max(1,Math.abs(v))));
  check('Throughput change produces new actual irradiance bytes',irradiance.sha256!==source.artifacts.find(a=>a.name==='spectral-image.npy').sha256);
  await page.locator('[data-scene-action="projection-camera"]:enabled').waitFor({timeout:30000});
  check('Projected result offers immutable original artifact links',await page.locator('[data-scene-projection-result] a[href="'+irradiance.url+'"]').count()>0);
  const beforeCached=(await get('workbench')).counts;
  const cached=await postClick('[data-scene-action="projection-submit"]','scene_projections');
  await page.locator('[data-scene-action="projection-camera"]:enabled').waitFor();
  check('Completed projection cache reuse preserves visible artifact links',cached.id===projected.id&&cached.cache_hit===true&&await page.locator('[data-scene-projection-result] a[href="'+irradiance.url+'"]').count()>0);
  check('Repeated completed projection submission creates no new records',same((await get('workbench')).counts,beforeCached));
  let release,received;const held=new Promise(resolve=>received=resolve),gate=new Promise(resolve=>release=resolve);
  await page.route('**/api/v1/runs',async route=>{const response=await route.fetch();received();await gate;await route.fulfill({response});});
  let cameraError;
  const cameraPromise=postClick('[data-scene-action="projection-camera"]','runs').catch(error=>{cameraError=error;received();return null;});await held;
  if(cameraError)throw cameraError;
  await page.locator('.nav-link[href="#factory"]').click();await page.locator('#model-json').waitFor();
  release();const camera=await cameraPromise;if(cameraError)throw cameraError;await page.unroute('**/api/v1/runs');report.records.run=camera.id;save();
  await page.waitForTimeout(250);
  check('Late Camera response preserves the user-selected page',new URL(page.url()).hash==='#factory'&&await page.locator('#model-json').count()===1);
  const run=await waitJob('runs',camera.id);
  check('Camera consumes the target assembly and projected input',run.assembly_id===target&&run.scene_snapshot.projection_job_snapshot.id===projected.id&&same(run.environment,projected.environment));
  for(const name of ['raw_frames.npy','rgb_frames.npy']){await bytes(run.artifacts.find(a=>a.name===name));}
  check('Actual RAW and RGB are downloadable with matching immutable hashes',true);
  const afterCamera=(await get('workbench')).counts;
  await page.goto(base+'/#scenes');await page.reload();await page.locator('[data-scene-projection-result="'+projected.id+'"] [data-scene-action="projection-camera"]:enabled').waitFor();
  check('The saved projection, source and target recover after a browser reload',await page.locator('#scene-projection-job').inputValue()===projected.id&&await page.locator('#scene-job').inputValue()===pair.render&&await page.locator('#scene-projection-assembly').inputValue()===target);
  const repeat=await postClick('[data-scene-action="projection-camera"]','runs');
  check('Repeated Camera action reuses the exact Test contract and completed run',repeat.id===run.id&&(await get('workbench')).counts.tests===afterCamera.tests);
  check('Original renderer record and assembly remain unchanged',same(await get('scene_renders/'+pair.render),source)&&same(await get('assemblies/'+target),assembly));
  check('No unintended original render or model writes',report.posts.every(p=>['/api/v1/scene_projections/preview','/api/v1/scene_projections','/api/v1/scene_projection_publications','/api/v1/tests','/api/v1/runs'].includes(p)));
  check('No uncaught browser exception',report.errors.length===0);
  report.artifact_sha256={radiance:retainedRadiance.sha256,irradiance:irradiance.sha256,raw:run.artifacts.find(a=>a.name==='raw_frames.npy').sha256,rgb:run.artifacts.find(a=>a.name==='rgb_frames.npy').sha256};
  report.status='passed';save();
 }catch(error){report.status='failed';report.failure=String(error.stack||error);save();await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw error;}
 finally{await browser.close();}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,records:report.records}));
})().catch(e=>{console.error(e);process.exitCode=1;});
}

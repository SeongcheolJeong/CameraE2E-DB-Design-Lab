'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_scene_preparation_ui_v1',base_url:base,checks:[],errors:[],records:{},physical_validation:'not_established',user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
const save=()=>fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 const page=await browser.newPage({viewport:{width:1480,height:1080}});
 page.on('pageerror',e=>report.errors.push(String(e)));
 try{
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),await r.text());return r.json();};
  const before=await get('bootstrap');
  check('Exact separate QA state; main cannot be mutated',before.state_dir===input.state_dir&&path.basename(before.state_dir).startsWith('camera-platform-scene-qa'));
  report.state_dir=before.state_dir;report.static_sha256={};
  for(const name of ['scenes.js','style.css']){const r=await page.request.get(base+'/'+name);assert(r.ok());report.static_sha256[name]=hash(await r.body());}

  const assembly=before.assemblies.find(a=>a.id===input.records.assembly);
  const lens=assembly.model_ids.find(id=>before.models.find(m=>m.id===id).kind==='lens');
  const source=before.assemblies.find(a=>!a.conditions.f_number);
  const scene=input.records.pairs[0].scene;
  const counts=(await get('workbench')).counts;
  const postClick=async(selector,endpoint)=>{
   const waiting=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/'+endpoint);
   await page.locator(selector).click();const r=await waiting;assert(r.ok(),await r.text());return r.json();
  };
  const preview=()=>postClick('[data-scene-action="camera-preview"]','scene_authoring/camera-preview');
  const waitJob=async(kind,id)=>{
   const deadline=Date.now()+120000;
   while(Date.now()<deadline){const j=await get(kind+'/'+id);if(['succeeded','failed','cancelled'].includes(j.status)){assert.equal(j.status,'succeeded',JSON.stringify(j.error));return j;}await new Promise(r=>setTimeout(r,500));}
   throw new Error('Observation timed out; inspect existing job '+id);
  };
  await page.goto(base+'/#scenes');
  await page.locator('[data-scene-action="new"]:enabled').waitFor();
  await page.locator('#scene-selected').selectOption(scene);
  await page.locator('#scene-render-assembly').selectOption(source.id);
  await page.locator('[data-scene-action="camera-template"]').click();
  await page.locator('#scene-prep-name').waitFor();
  let p=await preview();
  check('Unknown optics and absent PSF are visible and cannot be saved',p.status==='needs_input'&&p.optics.focal_length===null&&await page.locator('[data-scene-action="camera-save"]').isDisabled());
  check('Preparation template and preview create no records',JSON.stringify((await get('workbench')).counts)===JSON.stringify(counts));
  await page.locator('#scene-prep-lens').selectOption(lens);
  await page.locator('#scene-prep-condition-f_number').fill('8');
  await page.locator('#scene-prep-condition-temperature_c').fill('100');
  p=await preview();
  check('Out-of-domain temperature blocks preparation',p.status==='needs_input'&&p.issues.length>0&&await page.locator('[data-scene-action="camera-save"]').isDisabled());
  await page.locator('#scene-prep-condition-temperature_c').fill('25');
  p=await preview();
  check('Explicit model, scene light and aperture form a ready plan',p.status==='ready'&&p.draft.assembly.model_ids.includes(lens)&&p.draft.assembly.conditions.light_id.value===before.scene_assets.find(a=>a.id===scene).illumination_id);
  const goodDraft=await page.locator('#scene-camera-json').inputValue();
  for(const kind of ['lens','sensor','isp']){
   const id=await page.locator('#scene-prep-'+kind).inputValue();
   await page.locator('#scene-prep-'+kind).selectOption('');
   assert(await page.locator('[data-scene-action="camera-save"]').isDisabled());
   await page.locator('#scene-prep-'+kind).selectOption(id);
   assert.deepEqual(JSON.parse(await page.locator('#scene-camera-json').inputValue()),JSON.parse(goodDraft));
  }
  check('Cleared Lens Sensor and ISP selections recover without lost model IDs',true);
  for(const invalid of ['{}','null','[]','{"assembly":null}','{"assembly":{"model_ids":null}}','{','']){
   await page.locator('.scene-camera-preparation details').evaluate(el=>el.open=true);
   await page.locator('#scene-camera-json').fill(invalid);
   await page.locator('#scene-camera-json').dispatchEvent('change');
   await page.locator('[data-scene-camera-format-error]').waitFor();
   assert.equal(await page.locator('#scene-camera-json').inputValue(),invalid);
   assert(await page.locator('[data-scene-action="camera-save"]').isDisabled());
   assert(await page.locator('[data-scene-action="camera-preview"]').isDisabled());
   await page.locator('#scene-camera-json').fill(goodDraft);
   await page.locator('#scene-camera-json').dispatchEvent('change');
   await page.locator('#scene-prep-lens').waitFor();
   assert.equal(await page.locator('[data-scene-camera-format-error]').count(),0);
  }
  check('Malformed and structurally invalid JSON remains editable and recovers',report.errors.length===0);
  check('Recovered JSON still requires a fresh compatibility preview',await page.locator('[data-scene-action="camera-save"]').isDisabled());
  p=await preview();assert.equal(p.status,'ready');
  await page.locator('#scene-prep-name').fill('UI prepared camera · '+new Date().toISOString());
  check('Editing after preview invalidates save and stale result',await page.locator('[data-scene-action="camera-save"]').isDisabled()&&await page.locator('[data-scene-camera-preview]').count()===0);
  p=await preview();
  check('Unknown throughput is explicitly deferred',p.required_render_input.includes('transmittance'));
  await page.locator('.scene-camera-preparation').screenshot({path:path.join(out,'preparation-desktop.png')});
  await page.setViewportSize({width:390,height:844});
  await page.locator('.scene-camera-preparation').screenshot({path:path.join(out,'preparation-mobile.png')});
  check('Mobile preparation has no horizontal page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.setViewportSize({width:1480,height:1080});
  const saved=await postClick('[data-scene-action="camera-save"]','scene_authoring/cameras');
  report.records.assembly=saved.assembly.id;report.records.source=source.id;save();
  await page.waitForFunction(id=>document.querySelector('#scene-render-assembly')?.value===id,saved.assembly.id);
  check('Saved preparation is selected in render workflow',await page.locator('#scene-render-assembly').inputValue()===saved.assembly.id);
  check('Assembly preserves exact source and preview evidence',saved.assembly.scene_preparation.preview_sha256===p.preview_sha256&&saved.assembly.scene_preparation.source_assembly_sha256===source.content_hash);
  assert.deepEqual(await get('assemblies/'+source.id),source);
  for(const id of assembly.model_ids)assert.deepEqual(await get('models/'+id),before.models.find(m=>m.id===id));
  check('Source camera and every selected model remain byte-equivalent',true);
  const renderDraft=JSON.parse(await page.locator('#scene-render-json').inputValue());
  check('Save fills only verified optics, leaves every transmittance unknown',renderDraft.projection.focal_length_mm===4&&renderDraft.projection.f_number===8&&renderDraft.projection.on_axis_transmittance.values.every(v=>v===null));
  for(let i=0;i<renderDraft.projection.on_axis_transmittance.values.length;i++)await page.locator('#scene-throughput-'+i).fill('.8');
  await page.locator('#scene-throughput-source').fill('fixture://scene-camera-preparation-ui/throughput');
  await page.locator('#scene-throughput-method').fill('explicit-synthetic-preparation-test:1');
  await page.locator('#scene-height').fill('8');await page.locator('#scene-width').fill('8');
  await page.locator('#scene-spp').fill('2');
  const queued=await postClick('[data-scene-form="render"] button[type="submit"]','scene_renders');
  report.records.render=queued.id;save();
  const rendered=await waitJob('scene_renders',queued.id);
  report.environment=rendered.environment;report.backend=rendered.backend;
  check('Prepared camera executes actual Mitsuba in separate Worker',rendered.request.assembly_id===saved.assembly.id&&rendered.result.physical_validation==='not_established');
  await page.locator('[data-scene-action="camera"]:enabled').waitFor({timeout:30000});
  const camera=await postClick('[data-scene-action="camera"]','runs');report.records.run=camera.id;save();
  const result=await waitJob('runs',camera.id);
  check('Prepared render publishes into actual Camera RAW/RGB',result.assembly_id===saved.assembly.id&&result.artifacts.some(a=>a.name==='raw_frames.npy')&&result.artifacts.some(a=>a.name==='rgb_frames.npy'));
  for(const name of ['raw_frames.npy','rgb_frames.npy']){
   const a=result.artifacts.find(a=>a.name===name),r=await page.request.get(base+a.url);assert(r.ok());assert.equal(hash(await r.body()),a.sha256);
  }
  check('Actual downloaded RAW/RGB match immutable evidence hashes',true);
  await page.goto(base+'/#scenes');await page.locator('#scene-render-assembly').waitFor();
  await page.locator('#scene-render-assembly').selectOption(saved.assembly.id);
  check('Prepared camera survives browser reload',await page.locator('#scene-render-assembly').inputValue()===saved.assembly.id);
  check('No uncaught browser exception',report.errors.length===0);
  report.status='passed';save();
 }catch(e){report.status='failed';report.failure=String(e.stack||e);save();await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw e;}
 finally{await browser.close();}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,records:report.records}));
})().catch(e=>{console.error(e);process.exitCode=1;});

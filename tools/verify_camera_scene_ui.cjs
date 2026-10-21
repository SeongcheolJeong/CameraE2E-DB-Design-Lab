'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_scene_authoring_ui_v1',base_url:base,checks:[],errors:[],posts:[],records:{},physical_validation:'not_established',user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const save=()=>fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');
const canon=v=>JSON.stringify(v,(_k,x)=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.keys(x).sort().map(k=>[k,x[k]])):x);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 let page;
 try{
  page=await browser.newPage({viewport:{width:1480,height:1080}});
  page.on('pageerror',e=>report.errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p+': '+r.status());return r.json();};
  const before=await get('workbench');
  check('Explicit QA state; main workspace cannot be mutated',before.state_dir===input.state_dir&&path.basename(before.state_dir).startsWith('camera-platform-scene-qa'));
  report.state_dir=before.state_dir;
  const sourceId=input.records.pairs[0].scene,assembly=input.records.assembly;
  const original=await get('scene_assets/'+sourceId),material=await get('scene_assets/'+input.records.materials[0]);
  const waitJob=async(kind,id)=>{
   const deadline=Date.now()+120000;
   while(Date.now()<deadline){const job=await get(kind+'/'+id);if(['succeeded','failed','cancelled'].includes(job.status)){assert.equal(job.status,'succeeded',JSON.stringify(job.error));return job;}await new Promise(r=>setTimeout(r,500));}
   throw new Error('Observation timed out; job remains authoritative: '+id);
  };
  const clickPost=async(selector,endpoint)=>{
   const response=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/'+endpoint);
   await page.locator(selector).click();const r=await response;assert(r.ok(),await r.text());return r.json();
  };
  // Intentional transport failures must stop retrying and retain explicit retry controls.
  let templateAttempts=0;
  const rejectTemplate=async route=>{templateAttempts++;await route.fulfill({status:503,contentType:'application/json',body:'{"error":"QA template connection unavailable"}'});};
  await page.route('**/api/v1/scene_authoring/templates',rejectTemplate);
  await page.goto(base+'/#scenes');await page.locator('[data-scene-action="retry-templates"]').waitFor();
  await page.waitForTimeout(1800);check('Template transport error does not trigger an unbounded retry',templateAttempts===1);
  await page.unroute('**/api/v1/scene_authoring/templates',rejectTemplate);
  await page.locator('[data-scene-action="retry-templates"]').click();
  await page.locator('[data-scene-action="new"]:enabled').waitFor();
  await page.locator('#scene-selected').selectOption(sourceId);
  await page.locator('#scene-render-assembly').selectOption(assembly);
  const render=async(label)=>{
   await page.locator('[data-scene-action="render-template"]').click();
   await page.waitForFunction(()=>document.querySelector('#scene-render-json')?.value.includes('on_axis_transmittance'));
   let request=JSON.parse(await page.locator('#scene-render-json').inputValue());
   check(label+' requires explicit throughput instead of guessing',request.projection.on_axis_transmittance.values.every(v=>v===null));
   request.name='UI '+label+' · '+new Date().toISOString();
   await page.locator('#scene-render-name').fill(request.name);
   for(let i=0;i<request.projection.on_axis_transmittance.values.length;i++)await page.locator('#scene-throughput-'+i).fill('.8');
   await page.locator('#scene-throughput-source').fill('fixture://scene-ui-declared-throughput');
   await page.locator('#scene-throughput-method').fill('scene-ui-synthetic-test:1');
   request=JSON.parse(await page.locator('#scene-render-json').inputValue());
   check(label+' structured spectral inputs update the exact contract',request.projection.on_axis_transmittance.values.every(v=>v===.8)&&request.projection.source_uri==='fixture://scene-ui-declared-throughput');
   await page.locator('[data-scene-form="render"] details summary').click();
   if(label==='A'){
    const invalid=structuredClone(request);invalid.projection.f_number=9;
    await page.locator('#scene-render-json').fill(JSON.stringify(invalid,null,2));
    const rejected=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/scene_renders');
    await page.locator('[data-scene-form="render"] button[type="submit"]').click();
    check('Different aperture rejected before rendering',(await rejected).status()===422);
    await page.locator('[data-scene-form="render"] button[type="submit"]:enabled').waitFor();
   }
   if(!await page.locator('#scene-render-json').isVisible())await page.locator('[data-scene-form="render"] details summary').click();
   await page.locator('#scene-render-json').fill(JSON.stringify(request,null,2));
   const queued=await clickPost('[data-scene-form="render"] button[type="submit"]','scene_renders');
   report.records[label]={scene:request.scene_asset_id,render:queued.id};save();
   const job=await waitJob('scene_renders',queued.id);
   await page.locator('[data-scene-action="camera"]:enabled').waitFor({timeout:30000});
   check(label+' immutable job is selected',await page.locator('#scene-job').inputValue()===job.id);
   const previews=page.locator('.scene-preview');
   for(let i=0;i<await previews.count();i++){await previews.nth(i).scrollIntoViewIfNeeded();await previews.nth(i).evaluate(i=>i.decode());}
   check(label+' original radiance and depth previews render',await previews.count()===2&&await previews.evaluateAll(images=>images.every(i=>i.naturalWidth===32&&i.naturalHeight===32)));
   await page.locator('#scene-band').selectOption('4');
   for(let i=0;i<await previews.count();i++){await previews.nth(i).scrollIntoViewIfNeeded();await previews.nth(i).evaluate(i=>i.decode());}
   check(label+' spectral band preserves physical units',(await page.locator('.scene-preview-grid').innerText()).includes('650 nm')&&(await page.locator('.scene-preview-grid').innerText()).includes('W/m^2/sr/nm'));
   await page.locator('[data-scene-result]').screenshot({path:path.join(out,label+'-render.png')});
   const run=await clickPost('[data-scene-action="camera"]','runs');
   report.records[label].run=run.id;save();
   const completed=await waitJob('runs',run.id);
   await page.waitForURL('**/#experiments');
   check(label+' Camera consumes the rendered scene and fixed lens',completed.scene_snapshot.render_job_snapshot.id===job.id&&completed.assembly_id===assembly);
   const byName=(record,name)=>record.artifacts.find(a=>a.name===name);
   for(const [record,name] of [[job,'spectral-radiance.npy'],[job,'first-hit-depth-m.npy'],[completed,'raw_frames.npy'],[completed,'rgb.npy']]){
    const artifact=byName(record,name);assert(artifact,name);
    const response=await page.request.get(base+artifact.url);check(label+' '+name+' HTTP bytes match evidence',response.ok()&&sha(await response.body())===artifact.sha256);
    report.records[label][name]=artifact.sha256;
   }
   report.environment=job.environment;report.backend=job.backend;save();
   return {job,run:completed};
  };
  const a=await render('A');
  await page.locator('.nav-link[href="#scenes"]').click();await page.locator('#scene-selected').selectOption(sourceId);
  await page.locator('[data-scene-kind="material"]').click();
  await page.locator('[data-scene-edit="'+material.id+'"]').click();
  await page.locator('#scene-name').fill('UI spectral material B');
  for(const [i,value] of [.85,.7,.35,.15,.08].entries())await page.locator('#scene-spectrum-spectrum-'+i).fill(String(value));
  check('Structured material spectrum updates the exact asset draft',canon(JSON.parse(await page.locator('#scene-asset-json').inputValue()).payload.spectrum.values)===canon([.85,.7,.35,.15,.08]));
  const editedMaterial=await clickPost('[data-scene-form="asset"] button[type="submit"]','scene_assets');
  report.records.material=editedMaterial.id;save();
  await page.locator('[data-scene-kind="scene"]:enabled').waitFor();await page.locator('[data-scene-kind="scene"]').click();
  await page.locator('[data-scene-edit="'+sourceId+'"]').click();
  await page.locator('#scene-name').fill('UI same scene · material B');
  await page.locator('#scene-material-1').selectOption(editedMaterial.id);
  const editedScene=await clickPost('[data-scene-form="asset"] button[type="submit"]','scene_assets');
  report.records.edited_scene=editedScene.id;save();
  await page.locator('[data-scene-action="render-template"]:enabled').waitFor();
  check('Scene revision keeps original geometry, pose and lighting',editedScene.parents.includes(sourceId)&&editedScene.illumination_id===original.illumination_id&&canon(editedScene.payload.instances.map(i=>[i.geometry_id,i.to_world]))===canon(original.payload.instances.map(i=>[i.geometry_id,i.to_world])));
  check('Edited material has explicit derived provenance',editedMaterial.source_kind==='estimated'&&editedMaterial.parents.includes(material.id));
  await page.locator('#scene-render-assembly').selectOption(assembly);
  const b=await render('B');
  check('Material changes spectral radiance and actual RAW/RGB', ['spectral-radiance.npy','raw_frames.npy','rgb.npy'].every(k=>report.records.A[k]!==report.records.B[k]));
  check('Identical geometry preserves exact first-hit depth',report.records.A['first-hit-depth-m.npy']===report.records.B['first-hit-depth-m.npy']);
  check('Original scene and material records are unchanged',canon(await get('scene_assets/'+sourceId))===canon(original)&&canon(await get('scene_assets/'+material.id))===canon(material));
  const bundle=await page.request.get(base+'/api/v1/runs/'+b.run.id+'/bundle');check('Camera evidence bundle exports',bundle.ok()&&(await bundle.body()).length>1000);
  fs.writeFileSync(path.join(out,'camera-evidence.zip'),await bundle.body());report.bundle_sha256=sha(await bundle.body());
  await page.locator('.nav-link[href="#scenes"]').click();await page.reload();
  await page.locator('#scene-selected').selectOption(editedScene.id);await page.locator('#scene-job').selectOption(b.job.id);
  await page.locator('[data-scene-action="camera"]:enabled').waitFor();
  check('Page reload restores stored scene and render evidence',await page.locator('#scene-job').inputValue()===b.job.id);
  // Job-detail failure is different from job failure. No new job may be submitted.
  let detailAttempts=0;
  const detailPattern='**/api/v1/scene_renders/'+b.job.id;
  const rejectDetail=async route=>{detailAttempts++;await route.fulfill({status:503,contentType:'application/json',body:'{"error":"QA detail unavailable"}'});};
  await page.route(detailPattern,rejectDetail);await page.locator('#scene-job').selectOption('');await page.locator('#scene-job').selectOption(b.job.id);
  await page.locator('[data-scene-action="retry-job"]').waitFor();await page.waitForTimeout(1800);
  check('Failed detail lookup stops without replacing job',detailAttempts===1&&await page.locator('#scene-job').inputValue()===b.job.id);
  await page.unroute(detailPattern,rejectDetail);await page.locator('[data-scene-action="retry-job"]').click();await page.locator('[data-scene-action="camera"]:enabled').waitFor();
  const text='Draft retained across global refresh';await page.locator('#scene-name').fill(text);
  await page.locator('[data-action="refresh"]').click();await page.waitForTimeout(700);
  check('Global refresh preserves unsaved authoring content',await page.locator('#scene-name').inputValue()===text);
  for(const [label,width,height] of [['desktop',1480,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));
   check(label+' layout has no document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  check('No browser runtime errors',report.errors.length===0);
  report.status='passed';report.verified_at=new Date().toISOString();report.browser_version=browser.version();
 }catch(e){report.status='failed';report.failure=String(e);if(page)await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw e;}
 finally{save();await browser.close();}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,records:report.records}));
})().catch(e=>{console.error(e);process.exitCode=1;});

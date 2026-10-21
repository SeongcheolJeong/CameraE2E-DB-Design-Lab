'use strict';
// Read-only review of an already completed projection and Camera proof.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {isDeepStrictEqual:same}=require('node:util');
const {chromium}=require('playwright');
assert.equal(process.argv.length,5,'Expected base URL, saved HTTP proof, output directory');
const base=new URL(process.argv[2]).origin,input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
assert.equal(input.schema,'camera_scene_projection_verification_v1');assert.equal(input.status,'passed');
const ids=input.records;
for(const key of ['source_render','projection','assembly','image','test','run'])assert(typeof ids[key]==='string'&&ids[key]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_scene_projection_saved_ui_v1',base_url:base,state_dir:input.state_dir,checks:[],errors:[],posts:[],blocked_writes:[],read_only:true,user_browser_profile_accessed:false,physical_validation:'not_established'};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const save=()=>fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 const context=await browser.newContext({viewport:{width:1480,height:1080}});
 await context.route('**/*',async route=>{
  if(!['GET','HEAD','OPTIONS'].includes(route.request().method())){report.blocked_writes.push(route.request().url());await route.abort();}
  else await route.continue();
 });
 const page=await context.newPage();
 page.on('pageerror',error=>report.errors.push(String(error)));
 page.on('request',request=>{if(request.method()==='POST')report.posts.push(new URL(request.url()).pathname);});
 const get=async endpoint=>{const response=await page.request.get(base+'/api/v1/'+endpoint);assert(response.ok(),await response.text());return response.json();};
 const bytes=async url=>{const target=new URL(url,base);assert.equal(target.origin,base);const response=await page.request.get(target.href);assert(response.ok(),target.href);return response.body();};
 try{
  const before=await get('workbench');report.version=before.version;report.counts_before=before.counts;
  check('Exact state and installed environment version',before.state_dir===input.state_dir&&before.version===input.environment.platform_version&&before.version===input.running_version);
  report.static_sha256={};
  for(const name of ['index.html','app.js','scenes.js','workbench.js','style.css']){
   const value=hash(await bytes('/'+name));assert.equal(value,input.static_sha256[name],name+' served package hash');report.static_sha256[name]=value;
  }
  check('Served product files match the completed installed HTTP proof',true);
  const endpoints=['scene_renders/'+ids.source_render,'scene_projections/'+ids.projection,'assemblies/'+ids.assembly,'spectral_images/'+ids.image,'tests/'+ids.test,'runs/'+ids.run];
  const snapshots=await Promise.all(endpoints.map(get));
  const [source,projection,assembly,image,test,run]=snapshots;
  check('Source render and completed projection keep their exact parent relationship',source.status==='succeeded'&&projection.status==='succeeded'&&projection.source_render_id===source.id&&same(projection.source_render_snapshot,source));
  check('Target camera and Worker environment match the verified installed execution',same(projection.assembly_snapshot,assembly)&&same(projection.environment,input.environment)&&same(run.environment,input.environment));
  check('Original renderer environment remains separately preserved',same(source.environment,input.source_environment));
  check('Published irradiance retains both original and projected evidence',same(image.source_render_snapshot,source)&&same(image.projection_job_snapshot,projection)&&!image.render_job_snapshot&&image.physical_validation==='not_established');
  check('Saved Camera run pins the target Test and projected input',run.status==='succeeded'&&run.assembly_id===assembly.id&&run.test_id===test.id&&run.scene_id===image.id&&same(run.assembly_snapshot,assembly)&&same(run.scene_snapshot,image));
  check('Unknown total uncertainty is preserved',input.physical_validation==='not_established'&&same(input.total_uncertainty,{kind:'unknown'})&&same(run.result.prediction.uncertainty,{kind:'unknown'}));
  const requirements={
   original_radiance:[source,'spectral-radiance.npy'],original_irradiance:[source,'spectral-image.npy'],
   original_mc_standard_error:[source,'radiance-mc-standard-error.npy'],new_irradiance:[projection,'spectral-image.npy'],
   new_mc_standard_error:[projection,'irradiance-mc-standard-error.npy'],raw:[run,'raw_frames.npy'],rgb:[run,'rgb_frames.npy'],
  };
  report.artifact_sha256={};
  for(const [key,[record,name]] of Object.entries(requirements)){
   const checksum=input.artifact_sha256[key];assert(checksum,'Missing proof artifact '+key);
   const artifact=record.artifacts.find(a=>a.name===name&&a.sha256===checksum);assert(artifact,'Missing exact '+key+' descriptor');
   const original=await bytes(artifact.url);assert.equal(hash(original),checksum);assert.equal(original.length,artifact.bytes);
   report.artifact_sha256[key]=checksum;
   check('Downloaded original evidence '+key+' matches its proof checksum',true);
  }
  await page.goto(base+'/#scenes');await page.locator('[data-scene-action="new"]:enabled').waitFor();
  await page.locator('#scene-selected').selectOption(source.scene_asset_id);
  await page.locator('#scene-job').selectOption(source.id);
  await page.locator('#scene-projection-job').selectOption(projection.id);
  await page.locator('[data-scene-projection-result="'+projection.id+'"] [data-scene-action="projection-camera"]:enabled').waitFor();
  const newE=projection.artifacts.find(a=>a.name==='spectral-image.npy'&&a.sha256===input.artifact_sha256.new_irradiance);
  const oldE=source.artifacts.find(a=>a.name==='spectral-image.npy');
  check('New and original irradiance have separate visible evidence links',await page.locator('[data-scene-projection-result] a[href="'+newE.url+'"]').count()>0&&await page.locator('[data-scene-projection-result] a[href="'+oldE.url+'"]').count()>0&&(await page.locator('[data-scene-projection-result]').innerText()).includes('새 센서면 광량'));
  const images=page.locator('.scene-preview');assert.equal(await images.count(),2);
  for(const img of await images.all()){await img.scrollIntoViewIfNeeded();await img.evaluate(el=>el.decode());}
  check('Preserved radiance and depth previews load at the native crop size',await images.evaluateAll((items,shape)=>items.every(i=>i.naturalWidth===shape[1]&&i.naturalHeight===shape[0]),source.request.shape));
  check('The stored result keeps physical accuracy explicitly unknown',(await page.locator('[data-scene-projection-result]').innerText()).includes('미확립'));
  await page.reload();await page.locator('[data-scene-projection-result="'+projection.id+'"] [data-scene-action="projection-camera"]:enabled').waitFor();
  check('Source projection and target selection persist through page reload',await page.locator('#scene-job').inputValue()===source.id&&await page.locator('#scene-projection-job').inputValue()===projection.id&&await page.locator('#scene-projection-assembly').inputValue()===assembly.id);
  const waiting=page.waitForResponse(response=>response.request().method()==='GET'&&new URL(response.url()).pathname==='/api/v1/scene_authoring/projection-template');
  await page.locator('[data-scene-action="projection-template"]').click();const response=await waiting;assert(response.ok());const template=await response.json();
  await page.locator('#scene-projection-throughput-0').waitFor();
  check('Reviewing saved optics never assumes unknown throughput',template.request.projection.on_axis_transmittance.values.every(v=>v===null)&&await page.locator('[data-scene-action="projection-preview"]').isDisabled()&&await page.locator('[data-scene-action="projection-submit"]').isDisabled());
  for(const [label,width,height] of [['desktop',1480,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.locator('#toast').waitFor({state:'hidden'});
   // A reload/template redraw creates new lazy images below the initial viewport.
   // Decode those actual nodes before a full-page screenshot captures placeholders.
   for(const image of await page.locator('.scene-preview').all()){
    await image.scrollIntoViewIfNeeded();await image.evaluate(el=>el.decode());
   }
   check(label+' saved projection has no horizontal document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.evaluate(()=>{document.activeElement?.blur();scrollTo(0,0);});
   await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  await page.setViewportSize({width:1480,height:1080});
  await page.goto(base+'/#experiments');await page.locator('[data-action="select-run"][data-id="'+run.id+'"]').click();
  for(const name of ['raw_frames.npy','rgb_frames.npy']){
   const a=run.artifacts.find(item=>item.name===name),url=new URL(a.url,base).href;
   await page.locator('#main a[href="'+url+'"]').first().waitFor();
  }
  check('The saved Camera experiment exposes its original RAW and RGB files',true);
  check('Read-only review keeps every source and derived record unchanged',same(await Promise.all(endpoints.map(get)),snapshots));
  const after=await get('workbench');report.counts_after=after.counts;
  check('Every registry count remains unchanged',same(before.counts,after.counts));
  check('No POST or other mutation was attempted',report.posts.length===0&&report.blocked_writes.length===0);
  check('No uncaught browser error',report.errors.length===0);
  report.records=ids;report.environment=input.environment;report.status='passed';report.verified_at=new Date().toISOString();save();
 }catch(error){report.status='failed';report.failure=String(error.stack||error);save();await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw error;}
 finally{await browser.close();}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,posts:report.posts.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});

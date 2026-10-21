'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const {isDeepStrictEqual}=require('node:util');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_scene_saved_ui_v1',base_url:base,state_dir:input.state_dir,checks:[],errors:[],posts:[],read_only:true,user_browser_profile_accessed:false};
const check=(name,ok)=>{assert(ok,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1480,height:1080}});
  page.on('pageerror',e=>report.errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),await r.text());return r.json();};
  const before=await get('workbench');
  const expectedVersion=process.env.CAMERA_EXPECTED_PLATFORM_VERSION||input.environment.platform_version;
  check('Expected installed primary version and exact state',before.version===expectedVersion&&before.state_dir===input.state_dir);
  report.version=before.version;report.saved_run_version=input.environment.platform_version;
  const catalog=await get('scene_renders/catalog');check('Actual renderer remains connected',catalog.available&&isDeepStrictEqual(catalog.backend,input.backend));
  await page.goto(base+'/#scenes');await page.locator('#scene-selected').waitFor();
  for(const [index,pair] of input.records.pairs.entries()){
   await page.locator('#scene-selected').selectOption(pair.scene);await page.locator('#scene-job').selectOption(pair.render);await page.locator('[data-scene-action="camera"]:enabled').waitFor();
   const job=await get('scene_renders/'+pair.render);
   check('Saved render '+index+' keeps installed code identity',isDeepStrictEqual(job.environment,input.environment));
   for(const band of [0,4]){
    await page.locator('#scene-band').selectOption(String(band));
    const images=page.locator('.scene-preview');
    for(let i=0;i<await images.count();i++){await images.nth(i).scrollIntoViewIfNeeded();await images.nth(i).evaluate(i=>i.decode());}
    check('Original spectral/depth images '+index+'/'+band+' load',await images.count()===2&&await images.evaluateAll(list=>list.every(i=>i.naturalWidth===32)));
   }
   check('Physical uncertainty remains visible '+index,(await page.locator('.scene-trust').innerText()).includes('미확립'));
   const artifact=job.artifacts.find(a=>a.name==='spectral-radiance.npy');const bytes=await (await page.request.get(base+artifact.url)).body();
   check('Radiance evidence bytes preserved '+index,crypto.createHash('sha256').update(bytes).digest('hex')===pair.radiance_sha256);
   await page.locator('[data-scene-result]').screenshot({path:path.join(out,'render-'+index+'.png')});
  }
  await page.locator('#scene-render-assembly').selectOption(input.records.assembly);await page.locator('[data-scene-action="render-template"]').click();await page.locator('#scene-throughput-0').waitFor();
  check('Saved camera optics load without guessing throughput',await page.locator('#scene-throughput-0').inputValue()===''&&(await page.locator('.scene-optics').innerText()).includes('4 mm'));
  for(const [label,width,height] of [['desktop',1480,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));check(label+' no document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  check('Read-only review preserves all counts',JSON.stringify((await get('workbench')).counts)===JSON.stringify(before.counts));
  check('No writes or browser errors',report.posts.length===0&&report.errors.length===0);
  report.status='passed';report.version=before.version;report.records=input.records;report.environment=input.environment;report.verified_at=new Date().toISOString();
 }catch(e){report.status='failed';report.failure=String(e);throw e;}
 finally{await browser.close();fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,posts:report.posts.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});

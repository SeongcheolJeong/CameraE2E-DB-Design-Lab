'use strict';
// A real QA-only conflicting Test must not be reused just because its metric matches.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),output=process.argv[4];
const report={schema:'camera_scene_test_reuse_v1',status:'running',checks:[],state_dir:input.state_dir,base_url:base,errors:[]};
const check=(name,ok)=>{assert(ok,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage();page.on('pageerror',e=>report.errors.push(String(e)));
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),await r.text());return r.json();};
  const boot=await get('workbench');assert(boot.state_dir===input.state_dir&&path.basename(boot.state_dir).startsWith('camera-platform-scene-qa'));
  const run=await get('runs/'+input.records.B.run),test=await get('tests/'+run.test_id);
  const altered=structuredClone(test);for(const key of ['id','created_at','content_hash'])delete altered[key];
  altered.absolute_tolerance.value=test.absolute_tolerance.value/100;
  const posted=await page.request.post(base+'/api/v1/tests',{data:altered,headers:{'X-Platform-Token':boot.token}});assert(posted.ok(),await posted.text());
  const conflict=await posted.json();report.conflicting_test=conflict.id;report.original_test=test.id;
  await page.goto(base+'/#scenes');await page.locator('#scene-selected').selectOption(input.records.B.scene);await page.locator('#scene-job').selectOption(input.records.B.render);await page.locator('[data-scene-action="camera"]:enabled').waitFor();
  const submitted=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/runs');
  await page.locator('[data-scene-action="camera"]').click();const response=await submitted;assert(response.ok(),await response.text());
  const result=await response.json();report.run=result.id;
  check('Different tolerance cannot replace the selected observation Test',result.test_id===test.id&&result.test_id!==conflict.id);
  check('Exact inputs reuse the original completed Camera job',result.id===run.id&&result.status==='succeeded');
  await page.waitForURL('**/#experiments');
  await page.locator('.nav-link[href="#scenes"]').click();await page.locator('#scene-selected').selectOption(input.records.B.scene);await page.locator('#scene-job').selectOption(input.records.B.render);await page.locator('[data-scene-action="camera"]:enabled').waitFor();
  let release;const gate=new Promise(r=>{release=r;});let seen;const seenRequest=new Promise(r=>{seen=r;});
  await page.route('**/api/v1/runs',async route=>{
   if(route.request().method()!=='POST'){await route.continue();return;}
   const actual=await route.fetch();seen();await gate;await route.fulfill({response:actual});
  });
  await page.locator('[data-scene-action="camera"]').click();await seenRequest;
  await page.locator('.nav-link[href="#assets"]').click();release();await page.waitForTimeout(1000);
  check('Late Camera submission does not override navigation',page.url().endsWith('#assets'));
  check('No browser runtime errors',report.errors.length===0);
  report.status='passed';report.verified_at=new Date().toISOString();report.environment=input.environment;
 }catch(e){report.status='failed';report.failure=String(e);throw e;}
 finally{await browser.close();fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,run:report.run}));
})().catch(e=>{console.error(e);process.exitCode=1;});

/* Isolated browser: real saved evidence, lazy transport, failures and delayed replies. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2]||'http://127.0.0.1:8771',out=path.resolve(process.argv[3]||'reports/camera_platform/workbench-ui');
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_workbench_ui_v1',base_url:base,checks:[],errors:[],requests:[],posts:[],user_browser_profile_accessed:false};
function check(name,value){assert(value,name);report.checks.push({name,status:'passed'});}
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p+': '+r.status());return r.json();};
  const navigate=async view=>{await page.locator('.nav-link[href="#'+view+'"]').click();await page.locator('.nav-link[href="#'+view+'"][aria-current="page"]').waitFor();await page.locator('h1').waitFor();};
  const before=await get('bootstrap');report.version=before.version;
  check('Expected local workspace',before.workspace.endsWith('/CameraE2E'));
  page.on('pageerror',e=>report.errors.push(String(e)));
  page.on('request',r=>{report.requests.push(new URL(r.url()).pathname);if(r.method()==='POST')report.posts.push(r.url());});
  await page.goto(base+'/#builder');await page.locator('h1').waitFor();
  check('Builder opens using workbench projection',report.requests.includes('/api/v1/workbench')&&!report.requests.includes('/api/v1/bootstrap'));
  check('RAW replay sources not fetched on initial load',!report.requests.includes('/api/v1/replay-sources'));
  check('Initial details limited to chosen models',report.requests.filter(p=>p.startsWith('/api/v1/workbench/')).length<=3);
  for(const view of ['overview','assets','validation','factory','releases']){
   await navigate(view);
   check(view+' renders without a detail error',!(await page.locator('#main').innerText()).includes('기록 원본을 불러오지 못했습니다'));
  }
  await navigate('factory');await page.locator('#factory-job-select').waitFor();
  const model=before.models.at(-1),button=page.locator('[data-action="record"][data-collection="models"][data-id="'+model.id+'"]');
  await button.click();await page.locator('[data-action="copy-record"]:enabled').waitFor();
  const original=JSON.parse(await page.locator('#record-content').innerText());check('Record modal contains exact full model',JSON.stringify(original)===JSON.stringify(model)&&!!original.payload&&!original._projection);
  await page.locator('#record-dialog [data-action="close-dialog"]').first().click();
  await navigate('experiments');await page.locator('[data-mode="replay"]').click();await page.locator('#replay-job').waitFor();
  check('Replay sources fetched when replay selected',report.requests.includes('/api/v1/replay-sources'));
  const replay=before.replays.find(r=>r.result?.summary?.frames?.length>1)||before.replays[0];
  if(replay){await page.locator('#replay-job').selectOption(replay.id);await page.locator('#replay-job').waitFor();const evidence=(await page.locator('#main pre').allTextContents()).map(s=>{try{return JSON.parse(s);}catch{return null;}}).find(r=>r?.id===replay.id);check('Replay opens exact original acquisition evidence',JSON.stringify(evidence)===JSON.stringify(replay));}
  await page.locator('[data-mode="camera"]').click();
  const temporal=before.runs.find(r=>r.plugin_id==='temporal_scene_v1'&&r.status==='succeeded');
  if(temporal){
   await page.locator('[data-action="select-run"][data-id="'+temporal.id+'"]').click();
   const selector=page.locator('#frame-'+temporal.id);await selector.waitFor();
   const frames=temporal.result.summary.frames;check('Temporal frame count preserved',await selector.locator('option').count()===frames.length);
   await selector.selectOption(String(frames.at(-1).frame_id));await page.locator('#frame-'+temporal.id).waitFor();
   check('Last frame retains applied exposure',(await page.locator('.frame-facts').innerText()).includes(String(frames.at(-1).applied.exposure_ns)));
   const img=page.locator('.frame-browser img').first();await img.scrollIntoViewIfNeeded();await img.evaluate(async i=>{if(!i.complete||!i.naturalWidth)await i.decode();});check('Stored temporal preview decodes',await img.evaluate(i=>i.naturalWidth>0));
  }
  if(before.surrogates.length){
   await page.locator('[data-mode="surrogate"]').click();
   const model=before.surrogates[0];await page.locator('[data-surrogate-action="select"][data-id="'+model.id+'"]').click();await page.locator('[data-surrogate-model="'+model.id+'"]').waitFor();
   for(const metric of model.result.numerical_validation.checks){
    const text=await page.locator('[data-surrogate-error="'+metric.metric+'"]').innerText(),actual=Number(text.replaceAll(',','')),expected=metric.max_observed_absolute_error;
    check('Residual retains magnitude: '+metric.metric,expected===0?actual===0:actual!==0&&Math.abs(actual-expected)<=Math.max(Math.abs(expected)*5e-5,Math.abs(expected)>=1e-8?5e-9:0));
    if(expected>0&&expected<1e-8)check('Tiny nonzero residual uses scientific notation',text.includes('e-'));
   }
   const prediction=before.surrogate_predictions[0];
   if(prediction){await page.locator('[data-surrogate-action="saved-prediction"][data-id="'+prediction.id+'"]').click();await page.locator('[data-surrogate-prediction="'+prediction.id+'"]').waitFor();check('Full saved batch reopens from summary',(await page.locator('[data-surrogate-prediction]').innerText()).includes(prediction.rows.length+'개 조건'));
    const downloading=page.waitForEvent('download');await page.locator('[data-surrogate-action="export-prediction"]').click();const download=await downloading;const destination=path.join(out,'prediction.json');await download.saveAs(destination);check('Export is the full prediction, not the history summary',JSON.stringify(JSON.parse(fs.readFileSync(destination,'utf8')))===JSON.stringify(prediction));}
  }
  if(temporal){
   const shortcut=await browser.newPage();await shortcut.goto(base+'/#experiments');
   await shortcut.locator('[data-action="select-run"][data-id="'+temporal.id+'"]').click();
   const action=shortcut.locator('[data-action="replay-from-run"][data-id="'+temporal.id+'"]');await action.waitFor();
   check('Saved RAW shortcut works before replay sources are loaded',await action.isEnabled());
   await action.click();await shortcut.locator('#replay-source').waitFor();
   check('RAW shortcut selects exact acquisition',await shortcut.locator('#replay-source').inputValue()==='run:'+temporal.id);
   await shortcut.close();
  }
  // New page, uncached record: fail once, then retry the exact same selection.
  const failurePage=await browser.newPage();failurePage.on('pageerror',e=>report.errors.push(String(e)));
  let intercepted=0;
  await failurePage.route('**/api/v1/workbench/runs/*',async route=>{if(intercepted++===0)await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'deliberate UI transport test'})});else await route.continue();});
  await failurePage.goto(base+'/#experiments');await failurePage.locator('[data-action="retry-details"]').waitFor();
  check('Detail failure is explicit, without a successful result',(await failurePage.locator('#main').innerText()).includes('deliberate UI transport test')&&await failurePage.locator('[data-action="select-run"]').count()===0);
  await failurePage.locator('[data-action="retry-details"]').click();await failurePage.locator('[data-mode="camera"]').waitFor();check('Retry recovers full selected record',intercepted===2);
  await failurePage.close();
  // Hold original run response while navigating to another view. The late reply
  // must not render the old view or overwrite a draft typed in the new view.
  const race=await browser.newPage();let release;const held=new Promise(r=>{release=r;});let reached;const hit=new Promise(r=>{reached=r;});
  await race.route('**/api/v1/workbench/runs/*',async route=>{reached();await held;await route.continue();});
  await race.goto(base+'/#experiments');await hit;
  await race.locator('.nav-link[href="#factory"]').click();await race.locator('#model-json').waitFor();await race.locator('#model-json').fill('{"draft":"keep across old reply"}');release();
  await race.waitForResponse(r=>r.url().includes('/workbench/runs/'));await race.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  check('Late reply does not change view or erase draft',new URL(race.url()).hash==='#factory'&&await race.locator('#model-json').inputValue()==='{"draft":"keep across old reply"}');
  await race.close();
  await navigate('builder');await page.locator('h1').waitFor();
  for(const [label,width,height] of [['desktop',1440,1000],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));check(label+' has no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,label+'.png')});
  }
  check('No browser runtime errors',report.errors.length===0);check('Read-only verification preserves all counts',report.posts.length===0&&JSON.stringify((await get('bootstrap')).counts)===JSON.stringify(before.counts));
  report.status='passed';report.verified_at=new Date().toISOString();report.browser_version=browser.version();
 }finally{await browser.close();fs.writeFileSync(path.join(out,'headless-qa.json'),JSON.stringify(report,null,2)+'\n');}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,errors:report.errors}));
})().catch(e=>{console.error(e);process.exitCode=1;});

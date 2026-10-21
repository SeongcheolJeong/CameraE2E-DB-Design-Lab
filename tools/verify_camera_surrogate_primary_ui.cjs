const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2]||'http://127.0.0.1:8770';
const input=JSON.parse(fs.readFileSync(process.argv[3]||'reports/camera_platform/primary-surrogate-verification.json','utf8'));
const out=path.resolve(process.argv[4]||'reports/camera_platform/surrogate-ui/primary');
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_surrogate_primary_ui_v1',read_only:true,checks:[],posts:[],errors:[],user_browser_profile_accessed:false};
const numericMatch=(text,value)=>{const actual=Number(text.replaceAll(',',''));return value===0?actual===0:actual!==0&&Math.abs(actual-value)<=Math.max(Math.abs(value)*5e-5,Math.abs(value)>=1e-8?5e-9:0);};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  page.on('request',r=>{if(r.method()==='POST')report.posts.push(r.url());});
  page.on('pageerror',e=>report.errors.push(String(e)));
  const get=async route=>{const r=await page.request.get(base+'/api/v1/'+route);assert(r.ok());return r.json();};
  const initial=await get('bootstrap');report.version=initial.version;
  const expected=process.env.CAMERA_EXPECTED_STATE_DIR;report.expected_state_dir=expected||null;
  check('Expected workspace and worker healthy',(expected?initial.state_dir===path.resolve(expected):initial.state_dir.endsWith('/camera-platform'))&&(await get('health')).worker==='running');
  await page.goto(base+'/#experiments');
  await page.locator('[data-mode="surrogate"][data-action="experiment-mode"]').click();
  await page.locator('[data-surrogate-action="select"][data-id="'+input.model_id+'"]').click();
  const panel=page.locator('[data-surrogate-model="'+input.model_id+'"]');await panel.waitFor();
  const model=(await get('surrogates/'+input.model_id)).result;
  check('Installed model displays successful numerical comparison',(await panel.innerText()).includes('기준 계산 비교 통과'));
  for(const metric of model.numerical_validation.checks)
   check('Saved '+metric.metric+' error matches UI',numericMatch(await page.locator('[data-surrogate-error="'+metric.metric+'"]').innerText(),metric.max_observed_absolute_error));
  check('Physical accuracy is still unestablished',(await panel.innerText()).includes('실물 정확도 미확립'));
  await page.locator('[data-surrogate-action="saved-prediction"][data-id="'+input.prediction_id+'"]').click();
  await page.locator('[data-surrogate-prediction="'+input.prediction_id+'"]').waitFor();
  check('1024 saved predictions reopen with bounded 100-row display',
   (await page.locator('[data-surrogate-prediction]').innerText()).includes('1024개 조건')&&await page.locator('[data-surrogate-output="min_theoretical_snr_db"]').count()===100);
  const dl=page.waitForEvent('download');await page.locator('[data-surrogate-action="export-model"]').click();await(await dl).saveAs(path.join(out,'model.json'));
  check('Model export preserves coefficients',JSON.stringify(JSON.parse(fs.readFileSync(path.join(out,'model.json'),'utf8')).result.coefficients)===JSON.stringify(model.coefficients));
  for(const [label,width,height] of [['desktop',1440,1100],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));
   check(label+' layout fits viewport',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(out,label+'.png'),fullPage:false});
  }
  check('Read-only inspection creates no records',report.posts.length===0&&JSON.stringify((await get('bootstrap')).counts)===JSON.stringify(initial.counts));
  check('No JavaScript runtime errors',report.errors.length===0);
  report.status='passed';report.browser_version=browser.version();report.verified_at=new Date().toISOString();
 }finally{await browser.close();fs.writeFileSync(path.join(out,'headless-qa.json'),JSON.stringify(report,null,2)+'\n');}
 process.stdout.write(JSON.stringify({status:report.status,checks:report.checks.length})+'\n');
})().catch(e=>{process.stderr.write(String(e.stack)+'\n');process.exitCode=1;});


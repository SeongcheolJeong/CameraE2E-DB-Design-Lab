const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2]||'http://127.0.0.1:8771';
const input=JSON.parse(fs.readFileSync(process.argv[3]||'reports/camera_platform/surrogate-http-verification.json','utf8'));
const out=path.resolve(process.argv[4]||'reports/camera_platform/surrogate-ui');
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_surrogate_ui_verification_v1',checks:[],errors:[],posts:[],user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  page.on('pageerror',e=>report.errors.push(String(e)));
  page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok());return r.json();};
  const initial=await get('bootstrap');
  check('Isolated source QA workspace',initial.state_dir.endsWith('/camera-platform-surrogate-qa'));
  report.version=initial.version;
  await page.goto(base+'/#experiments');
  await page.locator('[data-action="experiment-mode"][data-mode="surrogate"]').click();
  await page.locator('#surrogate-name').fill('UI · 별도 조건 검증 응답 모델');
  await page.locator('[data-surrogate-field="source"][data-split="training"]').selectOption(input.training_study_id);
  await page.locator('[data-surrogate-field="row"][data-split="training"]').first().waitFor();
  await page.locator('[data-surrogate-field="source"][data-split="validation"]').selectOption(input.validation_study_id);
  await page.locator('[data-surrogate-field="row"][data-split="validation"]').first().waitFor();
  check('16 completed training references selected',await page.locator('[data-surrogate-field="row"][data-split="training"]:checked').count()===16);
  check('9 distinct holdout coordinates selected and duplicate baseline visibly disabled',
   await page.locator('[data-surrogate-field="row"][data-split="validation"]:checked').count()===9&&await page.locator('[data-surrogate-field="row"][data-split="validation"]:disabled').count()===1);
  const before=(await get('bootstrap')).counts;
  await page.locator('[data-surrogate-action="preview"]').click();
  await page.locator('[data-surrogate-plan]').waitFor();
  check('Planning has no database side effects',JSON.stringify((await get('bootstrap')).counts)===JSON.stringify(before));
  await page.locator('#surrogate-tolerance').fill('0.2');
  check('Editing invalidates the frozen submission plan',await page.locator('[data-surrogate-plan]').count()===0);
  await page.locator('#surrogate-tolerance').fill('0.3');
  await page.locator('[data-surrogate-action="preview"]').click();
  await page.locator('[data-surrogate-action="submit"]').waitFor();
  const submitted=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/surrogates'&&r.request().method()==='POST');
  await page.locator('[data-surrogate-action="submit"]').click();
  const response=await submitted;assert(response.ok(),await response.text());const job=await response.json();report.model_id=job.id;
  await page.locator('[data-surrogate-model="'+job.id+'"]').waitFor({timeout:30000});
  const model=(await get('surrogates/'+job.id)).result;
  check('Actual worker completes UI-selected training and passes holdout checks',model.prediction_eligible&&model.datasets.validation.length===9);
  check('Displayed residual equals saved numerical result',
   (await page.locator('[data-surrogate-error="min_theoretical_snr_db"]').innerText())===model.numerical_validation.checks[0].max_observed_absolute_error.toLocaleString('en-US',{maximumFractionDigits:8}));
  check('Graph and physical uncertainty boundaries are visible',await page.locator('.surrogate-chart').count()===1&&(await page.locator('#main').innerText()).includes('실물 정확도 미확립'));
  await page.locator('#surrogate-value-exposure_s').fill('0.0035, 0.0055, 0.0075');
  await page.locator('#surrogate-value-read_noise_e').fill('1.5, 3.5');
  await page.locator('[data-surrogate-action="predict"]').click();
  await page.locator('[data-surrogate-prediction]').waitFor();
  let pid=await page.locator('[data-surrogate-prediction]').getAttribute('data-surrogate-prediction');
  let prediction=await get('surrogate_predictions/'+pid);report.prediction_id=pid;
  check('Cartesian UI inputs produce six persisted metric predictions',prediction.rows.length===6);
  const displayed=await page.locator('[data-surrogate-output="min_theoretical_snr_db"]').allInnerTexts();
  check('All visible predictions match actual outputs',displayed.every((text,i)=>text===prediction.rows[i].outputs.min_theoretical_snr_db.toLocaleString('en-US',{maximumFractionDigits:8})));
  const dl=page.waitForEvent('download');
  await page.locator('[data-surrogate-action="export-model"]').click();
  const download=await dl;await download.saveAs(path.join(out,'model.json'));
  check('Portable coefficients and original split are exported',JSON.parse(fs.readFileSync(path.join(out,'model.json'),'utf8')).result.training_plan_hash===model.training_plan_hash);
  const pdl=page.waitForEvent('download');await page.locator('[data-surrogate-action="export-prediction"]').click();await (await pdl).saveAs(path.join(out,'prediction.json'));
  check('Full prediction evidence exports exact rows',JSON.stringify(JSON.parse(fs.readFileSync(path.join(out,'prediction.json'),'utf8')).rows)===JSON.stringify(prediction.rows));
  const count=(await get('bootstrap')).counts.surrogate_predictions;
  await page.locator('#surrogate-value-exposure_s').fill('0.009');
  await page.locator('[data-surrogate-action="predict"]').click();
  await page.locator('.surrogate-error').waitFor();
  check('Out-of-domain UI shows refusal and creates no result',(await page.locator('.surrogate-error').innerText()).includes('OUT_OF_DOMAIN')&&(await get('bootstrap')).counts.surrogate_predictions===count);
  await page.locator('#surrogate-value-exposure_s').fill('0.0055');
  await page.locator('[data-surrogate-action="predict"]').click();
  await page.waitForFunction(old=>document.querySelector('[data-surrogate-prediction]')?.getAttribute('data-surrogate-prediction')!==old,pid);
  check('Corrected input can predict without retraining',!(await page.locator('.surrogate-error').count()));
  await page.locator('#toast').waitFor({state:'hidden',timeout:10000});
  for(const [label,width,height] of [['desktop',1440,1100],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));
   check(label+' layout has no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  await page.reload();
  await page.locator('[data-action="experiment-mode"][data-mode="surrogate"]').click();
  await page.locator('[data-surrogate-model]').waitFor();
  check('Model and prediction history reopen after refresh',await page.locator('[data-surrogate-action="saved-prediction"]').count()>=3);
  check('No JavaScript runtime errors',report.errors.length===0);
  report.status='passed';report.browser_version=browser.version();report.verified_at=new Date().toISOString();
 }finally{await browser.close();fs.writeFileSync(path.join(out,'headless-qa.json'),JSON.stringify(report,null,2)+'\n');}
 process.stdout.write(JSON.stringify({status:report.status,checks:report.checks.length,report:path.join(out,'headless-qa.json')})+'\n');
})().catch(e=>{process.stderr.write(String(e.stack)+'\n');process.exitCode=1;});


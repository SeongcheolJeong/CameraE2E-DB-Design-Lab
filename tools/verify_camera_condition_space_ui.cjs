/* Read-only browser verification for Builder model applicability diagnostics. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');

const base=process.argv[2];
const runId=process.argv[3];
const output=path.resolve(process.argv[4]);
if(!base||!runId||!output)throw new Error('base URL, saved run ID and output directory required');
fs.mkdirSync(output,{recursive:true});
const evidence={schema:'camera_condition_space_ui_qa_v1',base_url:base,run_id:runId,
  checks:[],page_errors:[],request_errors:[],posts:[]};
function check(name,ok){assert(ok,name);evidence.checks.push(name);}

(async()=>{
  const browser=await chromium.launch({headless:true,
    executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
  try{
    const page=await browser.newPage({viewport:{width:1360,height:980}});
    page.on('pageerror',error=>evidence.page_errors.push(String(error)));
    page.on('requestfailed',request=>evidence.request_errors.push(request.url()));
    page.on('request',request=>{if(request.method()==='POST')evidence.posts.push(new URL(request.url()).pathname);});
    const beforeResponse=await page.request.get(base+'/api/v1/bootstrap');
    check('bootstrap HTTP 200',beforeResponse.status()===200);
    const before=await beforeResponse.json();
    const runResponse=await page.request.get(base+'/api/v1/runs/'+encodeURIComponent(runId));
    check('saved run HTTP 200',runResponse.status()===200);
    const run=await runResponse.json();
    await page.goto(base+'/#builder');
    await page.locator('#builder-assembly').waitFor();
    await page.locator('#builder-assembly').selectOption(run.assembly_id);
    await page.locator('#condition-values').fill('0.4, 0.5, 1');
    await page.locator('[data-action="condition-space"]').click();
    await page.locator('#condition-space-panel [data-condition-value="0.5"]').waitFor();
    await page.locator('#lfm-plan-run').selectOption(runId);
    await page.locator('#lfm-plan-draft').fill('4, 1\n8, 0.5\n8, 1');
    await page.locator('[data-action="lfm-candidate-plan"]').click();
    await page.locator('#lfm-candidate-plan-panel [data-lfm-candidate="8000000:0.5"]').waitFor();
    check('PWM and model blockers differ',await page.locator('#lfm-candidate-plan-panel [data-lfm-candidate="4000000:1"]').getAttribute('data-lfm-status')==='blocked'
      && await page.locator('#lfm-candidate-plan-panel [data-lfm-candidate="8000000:0.5"]').getAttribute('data-lfm-status')==='blocked'
      && await page.locator('#lfm-candidate-plan-panel [data-lfm-candidate="8000000:1"]').getAttribute('data-lfm-status')==='eligible_for_new_frozen_run_preflight');
    check('new-run and physical limits visible',(await page.locator('#lfm-candidate-plan-panel').innerText()).includes('새 Control/Test')
      && (await page.locator('#lfm-candidate-plan-panel').innerText()).includes('물리 LFM 성능'));
    check('analog and fixed digital gain roles visible',(await page.locator('#lfm-candidate-plan-panel').innerText()).includes('analog gain')
      && (await page.locator('#lfm-candidate-plan-panel').innerText()).includes('digital gain 1 고정'));
    check('candidate blockers readable in Korean',(await page.locator('#lfm-candidate-plan-panel').innerText()).includes('PWM 주기·노출 격자 밖')
      && (await page.locator('#lfm-candidate-plan-panel').innerText()).includes('게인 모델 적용 범위 밖'));
    const postsBeforeBadPlan=evidence.posts.length;
    await page.locator('#lfm-plan-draft').fill('invalid');
    await page.locator('[data-action="lfm-candidate-plan"]').click();
    check('invalid plan rejected before request',await page.locator('#lfm-candidate-plan-panel [role="alert"]').count()===1
      && evidence.posts.length===postsBeforeBadPlan);
    await page.locator('#lfm-plan-draft').fill('4, 1\n8, 0.5\n8, 1');
    await page.locator('[data-action="lfm-candidate-plan"]').click();
    await page.locator('#lfm-candidate-plan-panel [data-lfm-candidate="8000000:0.5"]').waitFor();
    check('low gain blocked',await page.locator('#condition-space-panel [data-condition-value="0.4"]').getAttribute('data-condition-status')==='blocked'
      && await page.locator('#condition-space-panel [data-condition-value="0.5"]').getAttribute('data-condition-status')==='blocked');
    check('fixed gain Composer compatible',await page.locator('#condition-space-panel [data-condition-value="1"]').getAttribute('data-condition-status')==='compatible');
    const panelText=await page.locator('#condition-space-panel').innerText();
    check('three model owners shown',panelText.includes('Lens')&&panelText.includes('Sensor')&&panelText.includes('ISP'));
    check('physical and runtime boundary visible',panelText.includes('PWM 정책')&&panelText.includes('물리 정확도'));
    check('only read-only diagnostic POSTs used',evidence.posts.filter(item=>item==='/api/v1/assemblies/condition-space').length===1
      && evidence.posts.filter(item=>item==='/api/v1/lfm/candidate-plans').length===2&&evidence.posts.length===3);
    await page.locator('#condition-axis').selectOption('exposure_s');
    await page.locator('#condition-values').fill('0.004, 0.008');
    await page.locator('[data-action="condition-space"]').click();
    await page.locator('#condition-space-panel [data-condition-value="0.008"]').waitFor();
    check('declared exposure model range shown',await page.locator('#condition-space-panel [data-condition-value="0.004"]').getAttribute('data-condition-status')==='compatible'
      && await page.locator('#condition-space-panel [data-condition-value="0.008"]').getAttribute('data-condition-status')==='compatible');
    await page.locator('#condition-values').fill('NaN');
    await page.locator('[data-action="condition-space"]').click();
    check('invalid input rejected before request',await page.locator('#condition-space-panel [role="alert"]').count()===1
      && evidence.posts.filter(item=>item==='/api/v1/assemblies/condition-space').length===2);
    await page.locator('#condition-axis').selectOption('gain');
    await page.locator('#condition-values').fill('0.4, 0.5, 1');
    await page.locator('[data-action="condition-space"]').click();
    await page.locator('#condition-space-panel [data-condition-value="0.5"]').waitFor();
    await page.screenshot({path:path.join(output,'builder-condition-space.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    check('mobile has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(output,'builder-condition-space-mobile.png'),fullPage:true});
    const afterResponse=await page.request.get(base+'/api/v1/bootstrap');
    check('bootstrap still available',afterResponse.status()===200);
    const after=await afterResponse.json();
    check('registry counts unchanged',JSON.stringify(before.counts)===JSON.stringify(after.counts));
    check('no browser errors',evidence.page_errors.length===0&&evidence.request_errors.length===0);
    evidence.status='passed';
    evidence.assembly_id=run.assembly_id;
    evidence.version=before.version;
    evidence.browser_version=browser.version();
  }finally{
    await browser.close();
    fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(evidence,null,2)+'\n');
  }
  process.stdout.write(JSON.stringify({status:evidence.status,checks:evidence.checks.length,version:evidence.version})+'\n');
})().catch(error=>{process.stderr.write(String(error.stack||error)+'\n');process.exitCode=1;});

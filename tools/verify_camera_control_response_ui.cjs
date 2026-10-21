'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const input=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),out=path.resolve(process.argv[3]);
const base=input.base_url,ids=input.records;
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_response_ui_v1',base_url:base,state_dir:input.state_dir,checks:[],errors:[],posts:[],user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  page.on('pageerror',e=>report.errors.push(String(e)));
  page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p);return r.json();};
  const before=await get('workbench');
  check('Explicit isolated QA state',before.state_dir===input.state_dir&&!input.state_dir.endsWith('/camera-platform'));
  await page.goto(base+'/#validation');
  await page.locator('details.data-detail').filter({has:page.locator('[data-action="select-capture"][data-id="'+ids.capture+'"]')}).locator('summary').first().click();
  await page.locator('[data-action="select-capture"][data-id="'+ids.capture+'"]').click();
  check('Capture selection retains its matching runtime',Boolean(await page.locator('#validation-run').inputValue()));
  await page.locator('#validation-run').selectOption(ids.run);
  const section=page.locator('[data-response-observation]');await section.waitFor();
  check('Exact common Test selected',await page.locator('#validation-test').inputValue()===ids.test);
  check('Exact observed dataset selected',await page.locator('#validation-measurement').inputValue()===ids.measurement);
  check('Exact prediction selected',await page.locator('#validation-run').inputValue()===ids.run);
  check('All planned frames shown including missing RAW',await section.locator('[data-response-sample]').count()===32);
  const missing=section.locator('[data-response-sample="controlled-5"]');
  check('Missing RAW retains explicit dash and reason',(await missing.innerText()).includes('RAW 누락')&&(await missing.innerText()).includes('—'));
  check('Actual RAW coverage is visible',(await section.locator('[data-response-coverage]').innerText()).includes('31/32'));
  check('WB ratio and unit remain explicit',(await section.innerText()).includes('wb_adjusted_rg_ratio')&&(await section.innerText()).includes('결과 단위 1'));
  check('Comparison stays inconclusive',(await page.locator('#validation-result').innerText()).includes('판정 보류'));
  await section.evaluate(el=>el.scrollIntoView({block:'start'}));
  await page.screenshot({path:path.join(out,'desktop.png')});
  await section.screenshot({path:path.join(out,'response-table.png')});
  check('Read-only inspection makes no POST',report.posts.length===0);
  const afterRead=await get('workbench');check('Read-only inspection preserves counts',JSON.stringify(before.counts)===JSON.stringify(afterRead.counts));
  await page.setViewportSize({width:390,height:844});
  check('Mobile page does not overflow horizontally',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await section.evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:path.join(out,'mobile.png')});
  await page.setViewportSize({width:1440,height:1080});
  const submitted=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/runs'&&r.request().method()==='POST');
  await page.locator('[data-action="run-capture"][data-id="'+ids.capture+'"]').click();
  const response=await submitted;check('Observed Test rerun submits successfully',response.ok());
  const queued=await response.json();let run;
  const deadline=Date.now()+30000;
  while(Date.now()<deadline){run=await get('runs/'+queued.id);if(['succeeded','failed','cancelled'].includes(run.status))break;await new Promise(r=>setTimeout(r,100));}
  check('Observed Test rerun uses closed-loop program',run.plugin_id==='closed_loop_scene_v1'&&run.test_id===ids.test&&run.control_program_id===ids.program);
  check('Observed Test rerun computes actual response',run.status==='succeeded'&&run.result.prediction.observation_method.id==='control_response_observation_v1');
  await page.locator('[data-control-result="'+queued.id+'"]').waitFor();
  check('New exact run displayed in control view',await page.locator('#control-frame option').count()===32);
  check('No JavaScript errors',report.errors.length===0);
  report.rerun_id=queued.id;report.status='passed';
 }catch(error){report.status='failed';report.error=String(error);throw error;}
 finally{fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

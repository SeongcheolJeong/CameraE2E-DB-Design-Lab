'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const input=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),out=path.resolve(process.argv[3]),base=input.base_url,ids=input.records;
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_response_saved_ui_v1',base_url:base,state_dir:input.state_dir,checks:[],errors:[],posts:[],read_only:true,user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()}),page=await browser.newPage({viewport:{width:1440,height:1080}});
 const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p);return r.json();};
 page.on('pageerror',e=>report.errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
 try{
  const before=await get('bootstrap'),capture=await get('captures/'+ids.capture);
  check('Expected installed primary state and version',before.state_dir===input.state_dir&&before.version===(process.env.CAMERA_EXPECTED_PLATFORM_VERSION||'0.10.0'));
  check('Saved acquisition retains its delivery hash and synthetic source',capture.content_hash===input.capture_hash&&capture.source_kind==='simulated');
  await page.goto(base+'/#validation');await page.locator('[data-analysis-select="'+ids.analysis+'"]').click();
  await page.locator('#validation-run').selectOption(ids.run);
  const table=page.locator('[data-response-observation]');await table.waitFor();
  check('Exact Test and observed Measurement selected',await page.locator('#validation-test').inputValue()===ids.test&&await page.locator('#validation-measurement').inputValue()===ids.measurement);
  check('All thirty-two actual RAW observations shown',await table.locator('[data-response-sample]').count()===32&&(await table.innerText()).includes('32/32'));
  check('Unknown physical prediction accuracy remains inconclusive',(await page.locator('#validation-result').innerText()).includes('판정 보류'));
  check('Shared acquisition meaning is visible',(await page.locator('[data-capture-analysis-panel]').innerText()).includes('독립된 실측 표본이 아닙니다'));
  check('New file upload controls are available',await page.locator('#response-raw-file').count()===1&&await page.locator('#response-metadata-file').count()===1);
  await page.locator('#toast').waitFor({state:'hidden',timeout:15000});await table.screenshot({path:path.join(out,'response-comparison.png')});await page.locator('[data-capture-analysis-panel]').screenshot({path:path.join(out,'analysis-history.png')});
  await page.locator('[data-analysis-select="'+ids.alternate_analysis+'"]').click();
  check('Alternate metric selects its own Test and Measurement',await page.locator('#validation-test').inputValue()===ids.alternate_test&&await page.locator('#validation-measurement').inputValue()===ids.alternate_measurement);
  check('Changing metric clears the previous comparison',await page.locator('[data-response-observation]').count()===0);
  await page.locator('[data-analysis-select="'+ids.analysis+'"]').click();await page.locator('#validation-run').selectOption(ids.run);await table.waitFor();
  await page.locator('#toast').waitFor({state:'hidden',timeout:15000});
  await page.setViewportSize({width:390,height:844});await table.evaluate(e=>e.scrollIntoView({block:'start'}));
  check('Mobile saved evidence has no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(out,'mobile.png')});
  for(const a of capture.artifacts){const bytes=await (await page.request.get(base+a.url)).body();check('Original artifact hash '+a.name,crypto.createHash('sha256').update(bytes).digest('hex')===a.sha256);}
  const after=await get('bootstrap');
  check('Read-only review preserves all counts',JSON.stringify(before.counts)===JSON.stringify(after.counts));
  check('Review submits no requests',report.posts.length===0);check('No JavaScript errors',report.errors.length===0);
  report.status='passed';report.records=ids;
 }catch(error){report.status='failed';report.error=String(error);await page.screenshot({path:path.join(out,'debug.png')});throw error;}
 finally{fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

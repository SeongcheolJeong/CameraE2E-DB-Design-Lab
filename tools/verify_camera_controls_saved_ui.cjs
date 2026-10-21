'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const base=process.argv[2],input=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const ids=input.records,report={schema:'camera_saved_controls_ui_v1',base_url:base,checks:[],errors:[],posts:[],physical_validation:'not_established',records:ids,user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  page.on('pageerror',e=>report.errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p+': '+r.status());return r.json();};
  const before=await get('workbench'),run=await get('runs/'+ids.run),program=await get('control_programs/'+ids.program),policy=await get('control_policies/'+ids.policy),replay=await get('replays/'+ids.replay);
  check('Expected state and installed version',before.state_dir===input.state_dir&&before.version===(process.env.CAMERA_EXPECTED_PLATFORM_VERSION||input.environment.platform_version));
  const summary=run.result.summary,frames=summary.frames;
  check('Saved run pins program and policy',run.control_program_snapshot.content_hash===program.content_hash&&run.control_policy_snapshot.content_hash===policy.content_hash&&summary.control_program_sha256===program.expanded_program_sha256);
  check('Saved inputs and Worker share the verified environment',JSON.stringify(run.environment)===JSON.stringify(input.environment));
  await page.goto(base+'/#controls');await page.locator('#control-run').waitFor();await page.locator('#control-run').selectOption(ids.run);await page.locator('[data-control-result="'+ids.run+'"]').waitFor();
  check('Recorded frame count shown',await page.locator('#control-frame option').count()===frames.length);
  check('Closed-loop observation and physical limitations are visible',(await page.locator('[data-control-result]').innerText()).includes('실물 예측 정확도는 미확립'));
  check('Target residual remains visible',(await page.locator('[data-control-final]').innerText()).includes('목표 대비 잔차'));
  for(const index of [0,frames.length-1]){
   await page.locator('#control-frame').selectOption(String(index));
   const actual=(await page.locator('[data-control-applied="exposure"]').innerText()).replaceAll(',','');
   check('Exact applied exposure at frame '+index,actual===frames[index].applied.exposure_ns+' ns');
   const image=page.locator('.control-preview');await image.scrollIntoViewIfNeeded();await image.evaluate(async i=>{if(!i.complete||!i.naturalWidth)await i.decode();});
   check('Stored RGB preview at frame '+index,await image.evaluate(i=>i.naturalWidth>0));
   const detail=JSON.parse(await page.locator('[data-control-result] details pre').first().textContent());
   check('Selected frame retains complete original control evidence '+index,JSON.stringify(detail)===JSON.stringify(frames[index]));
  }
  const downloading=page.waitForEvent('download');await page.locator('[data-control-action="export-trace"]').click();const downloaded=await downloading;const tracePath=path.join(out,'trace.json');await downloaded.saveAs(tracePath);
  check('Trace export equals saved original',JSON.stringify(JSON.parse(fs.readFileSync(tracePath,'utf8')))===JSON.stringify(summary.control_trace));
  for(const name of ['raw_frames.npy','rgb_frames.npy']){
   const entry=run.artifacts.find(a=>a.name===name),response=await page.request.get(base+entry.url),bytes=await response.body();
   check(name+' HTTP checksum matches immutable descriptor',response.ok()&&crypto.createHash('sha256').update(bytes).digest('hex')===entry.sha256);
  }
  await page.locator('#control-policy').selectOption(ids.policy);await page.locator('[data-control-action="copy-policy"]').click();
  check('Policy source can be copied into a separate draft',JSON.parse(await page.locator('#control-policyDraft').inputValue()).parents[0]===policy.id);
  check('Policy form preserves admitted maximum gain',Number(await page.locator('[data-path="payload.ae.readout_gain.maximum"]').inputValue())===policy.payload.ae.readout_gain.maximum);
  await page.locator('[data-action="replay-from-run"][data-id="'+ids.run+'"]').click();await page.locator('#replay-source').waitFor();
  check('RAW replay shortcut preserves selected acquisition',await page.locator('#replay-source').inputValue()==='run:'+ids.run);
  await page.locator('#replay-job').selectOption(ids.replay);await page.locator('#frame-'+ids.replay).waitFor();
  await page.locator('#frame-'+ids.replay).selectOption(frames.at(-1).frame_id);
  check('Replay shows original applied exposure',(await page.locator('.frame-facts').innerText()).includes(String(frames.at(-1).applied.exposure_ns)));
  check('Replay preserves identical RAW and complete control trace',replay.source.raw_sha256===input.raw_sha256&&JSON.stringify(replay.result.summary.control_trace)===JSON.stringify(summary.control_trace));
  check('Replay uses the saved alternate ISP',replay.isp_model_id===ids.isp&&(await page.locator('.frame-isp').innerText()).includes('전체 프레임 동일'));
  await page.locator('.nav-link[href="#controls"]').click();await page.locator('[data-control-result="'+ids.run+'"]').waitFor();await page.reload();await page.locator('#control-run').waitFor();await page.locator('#control-run').selectOption(ids.run);await page.locator('[data-control-result="'+ids.run+'"]').waitFor();
  check('Reload restores selected immutable acquisition',await page.locator('#control-frame option').count()===frames.length);
  // Capture the evidence area without changing any saved record.
  await page.locator('#control-frame').selectOption(String(frames.length-1));await page.locator('.control-preview').scrollIntoViewIfNeeded();await page.locator('.control-preview').evaluate(async i=>{if(!i.complete||!i.naturalWidth)await i.decode();});await page.locator('[data-control-final]').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'evidence.png')});
  for(const [label,width,height] of [['desktop',1440,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));check(label+' has no document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  check('No browser runtime errors',report.errors.length===0);
  check('Read-only review creates no writes and preserves counts',report.posts.length===0&&JSON.stringify((await get('workbench')).counts)===JSON.stringify(before.counts));
  report.status='passed';report.verified_at=new Date().toISOString();report.browser_version=browser.version();
 }finally{await browser.close();fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,posts:report.posts.length,errors:report.errors}));
})().catch(e=>{console.error(e);process.exitCode=1;});

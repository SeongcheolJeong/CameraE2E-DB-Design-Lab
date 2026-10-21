'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2],inputs=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),out=path.resolve(process.argv[4]);
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_controls_ui_verification_v1',base_url:base,checks:[],errors:[],physical_validation:'not_established',input_kind:inputs.input_kind,user_browser_profile_accessed:false};
function check(name,value){assert(value,name);report.checks.push({name,status:'passed'});}
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});page.on('pageerror',e=>report.errors.push(String(e)));
  const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),p+': '+r.status());return r.json();};
  const boot=await get('workbench');check('Isolated expected state directory',boot.state_dir===inputs.state_dir);
  const click=async action=>{const el=page.locator('[data-control-action="'+action+'"]');await el.waitFor();await el.click();};
  const draft=()=>page.locator('#control-policyDraft'),programDraft=()=>page.locator('#control-programDraft');
  const editPolicy=async mutate=>{const d=JSON.parse(await draft().inputValue());mutate(d);await draft().fill(JSON.stringify(d,null,2));await draft().blur();};
  await page.goto(base+'/#controls');await page.locator('#control-assembly').waitFor();
  await page.locator('#control-assembly').selectOption(inputs.assembly_id);await page.locator('#control-scene').selectOption(inputs.scene_sequence_id);
  await click('policy-template');await page.waitForFunction(()=>document.querySelector('#control-policyDraft')?.value.includes('camera_control_policy_v1'));
  check('AE AWB numeric controls render',await page.locator('[data-path="payload.ae.target_normalized"]').count()===1);
  await editPolicy(d=>{d.name='Out of domain UI policy';d.revision='ui-invalid-domain-1';d.payload.ae.readout_gain.maximum=16;});
  await click('register-policy');await page.waitForFunction(()=>document.querySelector('#control-policy')?.value);
  await page.locator('#control-count').fill(String(inputs.frame_count));await click('program-template');
  await page.waitForFunction(()=>document.querySelector('#control-programDraft')?.value.includes('camera_control_schedule_v1'));
  const original=await programDraft().inputValue();const countBefore=(await get('control_programs')).length;
  await click('check');await page.locator('[data-control-error]').waitFor();
  check('Entire gain range refusal is visible',(await page.locator('[data-control-error]').innerText()).includes('gain')||await page.locator('[data-control-error] pre').count()>0);
  const refused=JSON.parse(await page.locator('[data-control-error] pre').textContent());
  check('Structured preflight retains full admitted gain interval',refused.control_bounds.gain.max===16&&refused.issues.length>0);
  check('Failed preflight creates no program',(await get('control_programs')).length===countBefore);
  check('Failed preflight has no registration button',await page.locator('[data-control-action="register-program"]').count()===0);
  await editPolicy(d=>{d.name='UI feedback policy';d.revision='ui-valid-1';d.payload.ae.readout_gain.maximum=8;});
  const oldPolicy=await page.locator('#control-policy').inputValue();await click('register-policy');
  await page.waitForFunction(id=>document.querySelector('#control-policy')?.value!==id,oldPolicy);
  await click('program-template');await page.waitForFunction(()=>document.querySelector('#control-programDraft')?.value.includes('camera_control_schedule_v1'));
  const validDraft=await programDraft().inputValue();await click('check');await page.locator('[data-control-check]').waitFor();
  check('Full domain check passes',(await page.locator('[data-control-check]').innerText()).includes('통과'));
  // A prior successful check is never valid for a changed draft.
  await programDraft().fill(validDraft+' ');check('Editing immediately removes previous approval',await page.locator('[data-control-check]').count()===0);
  // Hold a check response while editing. The late success must remain invisible.
  let release,reached;const held=new Promise(r=>release=r),hit=new Promise(r=>reached=r);
  await page.route('**/api/v1/control_programs/check',async route=>{const response=await route.fetch();reached();await held;await route.fulfill({response});});
  await click('check');await hit;await programDraft().fill(validDraft+'  ');release();
  await page.waitForFunction(()=>!document.querySelector('[data-control-action="check"]')?.disabled);
  check('Late successful check cannot approve an edited draft',await page.locator('[data-control-check]').count()===0);
  await page.unroute('**/api/v1/control_programs/check');await click('check');await page.locator('[data-control-check]').waitFor();
  await click('register-program');await page.waitForFunction(()=>document.querySelector('#control-program')?.value);
  report.program_id=await page.locator('#control-program').inputValue();
  const registered=await get('control_programs/'+report.program_id);
  check('Registered program pins policy and native scene',registered.scene_sequence_id===inputs.scene_sequence_id&&registered.assembly_id===inputs.assembly_id&&registered.payload.frames.length===32);
  const originalArtifact=registered.artifacts.find(a=>a.name==='control-program-source.json');
  check('Browser preserves original JSON bytes',(await (await page.request.get(base+originalArtifact.url)).text())===validDraft+'  ');
  await page.locator('#control-seed').fill('83');const submitting=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/runs'&&r.request().method()==='POST');await click('run');const queued=await (await submitting).json();assert(queued.id);report.run_id=queued.id;await page.locator('[data-control-result="'+report.run_id+'"]').waitFor({timeout:60000});
  const run=await get('runs/'+report.run_id),summary=run.result.summary,frames=summary.frames;
  check('Actual worker produced 32 frames',run.status==='succeeded'&&frames.length===32);
  check('Raw feedback changes actual acquisition exposure',frames[0].applied.exposure_ns===inputs.initial_exposure_ns&&frames.some(f=>f.applied.exposure_ns!==inputs.initial_exposure_ns));
  check('Commands use later application frames',summary.control_trace.commands.some(c=>c.status==='applied')&&summary.control_trace.commands.filter(c=>c.status==='applied').every(c=>c.applied_frame_index>c.source_frame_index));
  check('Run retains pinned policy and program hashes',run.control_program_snapshot.content_hash===registered.content_hash&&summary.control_program_sha256===registered.expanded_program_sha256);
  check('Final target residual and actuator hold remain explicit',(await page.locator('[data-control-final]').innerText()).includes('목표 대비 잔차')&&(await page.locator('[data-control-final]').innerText()).includes('목표 도달 판정 아님'));
  check('Only generated frame conditions appear after capture',run.test_snapshot.frame_conditions===null&&summary.frame_conditions.length===32);
  await page.locator('#control-frame').selectOption('31');
  check('Last-frame applied exposure matches source',(await page.locator('[data-control-applied="exposure"]').innerText()).replaceAll(',','')===frames[31].applied.exposure_ns+' ns');
  const preview=page.locator('.control-preview');await preview.scrollIntoViewIfNeeded();await preview.evaluate(async i=>{if(!i.complete||!i.naturalWidth)await i.decode();});
  check('Actual stored RGB preview decodes',await preview.evaluate(i=>i.naturalWidth>0));
  const downloading=page.waitForEvent('download');await click('export-trace');const download=await downloading;const dest=path.join(out,'control-trace.json');await download.saveAs(dest);
  check('Downloaded trace equals API original',JSON.stringify(JSON.parse(fs.readFileSync(dest,'utf8')))===JSON.stringify(summary.control_trace));
  const raw=run.artifacts.find(a=>a.name==='raw_frames.npy'),rawResponse=await page.request.get(base+raw.url);
  check('RAW array artifact is served',rawResponse.ok()&&(await rawResponse.body()).subarray(0,6).toString('latin1')==='\x93NUMPY');
  check('Physical accuracy is explicitly unestablished',(await page.locator('[data-control-result]').innerText()).includes('실물 예측 정확도는 미확립'));
  // Reload retrieves original result, without a new acquisition or test creation.
  const before=await get('workbench');await page.reload();await page.locator('[data-control-result="'+report.run_id+'"]').waitFor();
  check('Reload reopens saved acquisition',(await get('workbench')).counts.runs===before.counts.runs);
  const frame0=await page.locator('[data-control-applied="exposure"]').innerText();check('Reload starts from actual first-frame setting',frame0.replaceAll(',','')===inputs.initial_exposure_ns+' ns');
  await page.locator('#control-program').selectOption(report.program_id);await page.locator('#control-seed').fill('83');
  const beforeReuse=await get('workbench');await click('run');await page.waitForFunction(()=>!document.querySelector('[data-control-action="run"]')?.disabled);
  const afterReuse=await get('workbench');check('Identical submission reuses pinned test and cached run',afterReuse.counts.tests===beforeReuse.counts.tests&&afterReuse.counts.runs===beforeReuse.counts.runs);
  await page.locator('[data-action="replay-from-run"][data-id="'+report.run_id+'"]').click();await page.locator('#replay-source').waitFor();
  check('Feedback RAW shortcut selects the exact acquisition',await page.locator('#replay-source').inputValue()==='run:'+report.run_id);
  await page.locator('#replay-isp').selectOption(inputs.models.isp);await page.locator('#replay-name').fill('Control acquisition replay · fixed original ISP');
  const replaying=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/replays'&&r.request().method()==='POST');await page.locator('form[data-form="replay"] button[type="submit"]').click();const queuedReplay=await (await replaying).json();assert(queuedReplay.id);report.replay_id=queuedReplay.id;await page.locator('#frame-'+report.replay_id).waitFor({timeout:60000});
  const replay=await get('replays/'+report.replay_id);
  check('Actual replay preserves generated controls and trace',JSON.stringify(replay.result.summary.control_trace)===JSON.stringify(summary.control_trace)&&JSON.stringify(replay.result.summary.frame_conditions)===JSON.stringify(summary.frame_conditions));
  check('Replay keeps identical RAW and changes WB-processed RGB',replay.source.raw_sha256===raw.sha256&&replay.artifacts.find(a=>a.name==='rgb_frames.npy').sha256!==run.artifacts.find(a=>a.name==='rgb_frames.npy').sha256);
  await page.locator('.nav-link[href="#controls"]').click();await page.locator('[data-control-result]').waitFor();
  // Navigation into the general experiment history routes this run back to control evidence.
  await page.locator('.nav-link[href="#experiments"]').click();await page.locator('[data-mode="camera"]').click();await page.locator('[data-action="select-run"][data-id="'+report.run_id+'"]').click();await page.locator('[data-control-result="'+report.run_id+'"]').waitFor();
  check('General history opens feedback evidence, not a fixed-setting form',new URL(page.url()).hash==='#controls');
  for(const [label,width,height] of [['desktop',1440,1080],['mobile',390,844]]){
   await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));check(label+' has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
  }
  check('No browser runtime errors',report.errors.length===0);report.status='passed';report.verified_at=new Date().toISOString();report.browser_version=browser.version();
 }finally{await browser.close();fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2)+'\n');}
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,errors:report.errors,run_id:report.run_id}));
})().catch(e=>{console.error(e);process.exitCode=1;});

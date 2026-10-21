/* Real isolated Chromium verification; never opens a user's browser profile. */
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const crypto = require('crypto');
const {chromium} = require('playwright');

const base = process.argv[2] || 'http://127.0.0.1:8771';
const inputPath = process.argv[3] || 'reports/camera_platform/temporal-http-verification.json';
const outdir = process.argv[4] || 'reports/camera_platform/temporal-ui';
const readonly = process.argv.includes('--read-only');
const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
const report = {schema: 'camera_temporal_ui_verification_v1', base_url: base,
  created_at: new Date().toISOString(), mode: readonly ? 'read_only' : 'functional',
  browser: {headless: true, user_browser_profile_accessed: false}, checks: [], errors: [],
  post_paths: [], screenshots: []};
fs.mkdirSync(outdir, {recursive: true});
const check = (name, value=true) => {assert(value, name); report.checks.push({name, status:'PASS'});};
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

(async () => {
  const browser = await chromium.launch({headless:true,
    executablePath:process.env.CAMERA_UI_BROWSER || chromium.executablePath()});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  report.browser.version = browser.version();
  page.on('pageerror', e => report.errors.push(String(e)));
  page.on('console', e => {if(e.type()==='error') report.errors.push(e.text());});
  page.on('request', r => {if(r.method()==='POST') report.post_paths.push(new URL(r.url()).pathname);});
  const get = async route => {
    const r = await page.request.get(base+'/api/v1/'+route);
    assert(r.ok(), await r.text()); return await r.json();
  };
  const postClick = async (locator, route) => {
    const [r] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname==='/api/v1/'+route && r.request().method()==='POST', {timeout:120000}),
      locator.click()
    ]);
    assert(r.ok(), await r.text()); return await r.json();
  };
  const screenshot = async name => {
    const preview = page.locator('.frame-browser img');
    if(await preview.count()) {
      await preview.scrollIntoViewIfNeeded();
      await page.waitForFunction(() => [...document.querySelectorAll('.frame-browser img')].every(i=>i.complete&&i.naturalWidth>0));
      check(name+' rendered preview occupies visible space',await preview.evaluate(i=>i.clientHeight>=200));
    }
    check(name+' no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const filename = path.resolve(outdir, name+'.png');
    await page.screenshot({path:filename,fullPage:true});
    report.screenshots.push(filename);
  };
  const waitRecord = async (collection,id) => {
    const deadline = Date.now()+120000;
    while(Date.now()<deadline) {
      const record = await get(collection+'/'+id);
      if(['succeeded','failed','cancelled'].includes(record.status)) {
        assert.equal(record.status,'succeeded',record.error); return record;
      }
      await new Promise(resolve=>setTimeout(resolve,400));
    }
    throw Error('Specific job remains nonterminal: '+id);
  };
  const openExperiments = async () => {
    await page.goto(base+'/#experiments');
    await page.locator('#run-runtime').waitFor();
  };
  const frameChecks = async (run, label) => {
    const frames = run.result.summary.frames;
    await page.locator(`[data-frame-job="${run.id}"]`).waitFor({timeout:120000});
    check(label+' explicit frame selector has every saved frame',
      await page.locator(`[data-frame-job="${run.id}"] option`).count()===frames.length);
    await page.locator(`[data-frame-job="${run.id}"]`).selectOption(frames[1].frame_id);
    const facts = await page.locator('.frame-facts').innerText();
    check(label+' applied frame ID and exposure visible', facts.includes(frames[1].frame_id) && facts.includes(String(frames[1].applied.exposure_ns)));
    const img=page.locator('.frame-browser img');
    await img.waitFor();
    assert.equal(new URL(await img.getAttribute('src'),base).pathname,frames[1].preview_url);
    report.lazy_preview_observations=report.lazy_preview_observations||[];
    report.lazy_preview_observations.push(await img.evaluate(i=>({loading:i.loading,complete:i.complete,naturalWidth:i.naturalWidth,top:i.getBoundingClientRect().top,viewportHeight:innerHeight})));
    // Native selectOption does not scroll. A lazy image outside the viewport is
    // intentionally not fetched until visible, especially with a long run history.
    await img.scrollIntoViewIfNeeded();
    await page.waitForFunction(() => [...document.querySelectorAll('.frame-browser img')].every(i=>i.complete&&i.naturalWidth>0));
    check(label+' actual native preview decoded', await img.evaluate(i=>i.naturalWidth===64&&i.naturalHeight===64));
    const content = await (await page.request.get(base+frames[1].preview_url)).body();
    check(label+' preview content hash matches frame record',hash(content)===frames[1].preview_artifact_id);
    await page.locator('[data-action="frame-step"][data-step="1"]').click();
    check(label+' next button selects next declared frame',await page.locator(`[data-frame-job="${run.id}"]`).inputValue()===frames[2].frame_id);
    await page.locator('.frame-browser').scrollIntoViewIfNeeded();
    await screenshot(label+'-desktop');
    await page.setViewportSize({width:390,height:844});
    await page.locator('.frame-browser').scrollIntoViewIfNeeded();
    await screenshot(label+'-mobile');
    await page.setViewportSize({width:1440,height:1000});
  };
  try {
    const boot=await get('bootstrap');
    report.server_version=boot.version;
    check('server provides temporal API contract',boot.schema==='camera_platform_v1' && boot.plugins.some(p=>p.plugin_id==='temporal_scene_v1'));
    check('managed worker is live',(await get('health')).worker==='running');
    let run, replay;
    if(!readonly) {
      await page.goto(base+'/#factory');
      await page.locator('#temporal-assembly').selectOption(input.records.assembly);
      await page.locator('[data-action="sequence-template"][data-kind="scene"]').click();
      await page.waitForFunction(()=>document.querySelector('#temporal-scene-json').value.includes('assembly_id'));
      const fixture=JSON.parse(await page.locator('#temporal-scene-json').inputValue());
      fixture.name='화면 검증 · 이동광점'; fixture.revision='ui-'+Date.now();
      await page.locator('#temporal-scene-json').fill(JSON.stringify(fixture,null,2));
      const scene=await postClick(page.locator('[data-form="temporal-fixture"] button[type="submit"]'),'scene_sequences/fixture');
      await page.waitForFunction(id=>document.querySelector('#temporal-scene-sequence').value===id,scene.id);
      check('UI authored timeline registered and selected',scene.source_kind==='estimated'&&scene.image_snapshots.length===65);
      await page.locator('[data-action="sequence-template"][data-kind="control"]').click();
      await page.waitForFunction(id=>document.querySelector('#control-sequence-json').value.includes(id),scene.id);
      const controls=JSON.parse(await page.locator('#control-sequence-json').inputValue());
      controls.name='화면 검증 · 실제 적용 설정 예제'; controls.revision=fixture.revision;
      controls.payload.frames[1].applied.exposure_ns=8000000;
      const original=' \r\n'+JSON.stringify(controls,null,2)+'\r\n';
      await page.locator('#sequence-file-control').setInputFiles({name:'recorded-settings.json',mimeType:'application/json',buffer:Buffer.from(original)});
      await page.waitForFunction(()=>document.querySelector('#control-sequence-json').value.includes('8000000'));
      const control=await postClick(page.locator('[data-form="control-sequence"] button[type="submit"]'),'control_sequences/import');
      const source=control.artifacts.find(a=>a.name==='control-sequence-source.json');
      check('Uploaded control CRLF source bytes preserved',Buffer.compare(await (await page.request.get(base+source.url)).body(),Buffer.from(original))===0);
      // Navigate without reload so the exact new pair remains selected.
      await page.locator('a[href="#experiments"]').last().click();
      await page.locator('#run-runtime').waitFor();
      check('Temporal runtime selected from registered sequence',await page.locator('#run-runtime').inputValue()==='temporal_scene_v1');
      check('Exact scene/control selection survives refresh',await page.locator('#run-scene-sequence').inputValue()===scene.id&&await page.locator('#run-control-sequence').inputValue()===control.id);
      check('Temporal capture has no scalar exposure editor',await page.locator('#run-exposure').count()===0);
      await page.locator('[data-action="temporal-test-template"]').click();
      await page.locator('#temporal-test-json').waitFor();
      const test=await postClick(page.locator('[data-form="temporal-test"] button[type="submit"]'),'tests');
      await page.waitForFunction(id=>document.querySelector('#run-test').value===id,test.id);
      const queued=await postClick(page.locator('[data-form="run"] button[type="submit"]'),'runs');
      check('UI submitted only explicit temporal IDs and seed',queued.scene_sequence_id===scene.id&&queued.control_sequence_id===control.id&&Object.keys(queued.parameters).join(',')==='seed');
      run=await waitRecord('runs',queued.id);
      report.records={assembly:input.records.assembly,scene:scene.id,control:control.id,test:test.id,run:run.id};
    } else {
      run=await get('runs/'+input.records.run);
      await openExperiments();
      await page.locator(`[data-action="select-run"][data-id="${run.id}"]`).click();
    }
    await frameChecks(run,'temporal-camera');
    const files=page.locator('.artifact-browser').first();
    check('Large source file list is collapsed by default',await files.evaluate(e=>!e.open));
    check('RAW RGB summary are immediately downloadable',await page.locator('.artifact-summary>.artifact-links a').count()>=3);
    await files.locator('summary').click();
    check('Expanded original file list retains named hashes',await files.locator('.artifact-file-row code').count()>100);
    await files.locator('summary').click();
    await page.locator(`[data-action="replay-from-run"][data-id="${run.id}"]`).click();
    await page.locator('#replay-source').waitFor();
    check('Replay source shows per-frame acquisition',await page.locator('[data-form="replay"]').innerText().then(t=>t.includes('프레임별 원본 촬영 조건')));
    if(!readonly) {
      await page.locator('#replay-isp').selectOption(input.records.isp);
      await page.locator('#replay-name').fill('화면 검증 · 같은 RAW 새 ISP');
      const queued=await postClick(page.locator('[data-form="replay"] button[type="submit"]'),'replays');
      replay=await waitRecord('replays',queued.id);
      report.records.replay=replay.id;
    } else {
      replay=await get('replays/'+input.records.replay);
      await page.locator('#replay-job').selectOption(replay.id);
    }
    await frameChecks(replay,'temporal-replay');
    check('Replay original acquisition and new ISP both visible',await page.locator('.frame-isp').innerText().then(t=>t.includes('원본 적용 ISP')&&t.includes('이 재처리의 ISP')));
    assert.deepEqual(replay.result.summary.frame_conditions,run.result.summary.frame_conditions);
    const raw=run.artifacts.find(a=>a.name==='raw_frames.npy');
    const rawReplay=replay.artifacts.find(a=>a.name==='raw_frames.npy');
    check('UI produced replay preserves original RAW hash',raw.sha256===rawReplay.sha256);
    const before=run.artifacts.find(a=>a.name==='rgb_frames.npy');
    const after=replay.artifacts.find(a=>a.name==='rgb_frames.npy');
    check('UI produced replay changes actual RGB arrays',before.sha256!==after.sha256);
    const download = await (await page.request.get(base+`/api/v1/replays/${replay.id}/bundle`)).body();
    check('UI evidence link downloads ZIP',download[0]===0x50&&download[1]===0x4b);
    const final=await get('bootstrap');
    check('No capture or physical measurement fixtures created',final.counts.captures===boot.counts.captures&&final.counts.measurements===boot.counts.measurements);
    if(readonly) {assert.deepEqual(final.counts,boot.counts);check('Read-only pass performs zero POST',report.post_paths.length===0);}
    check('No browser JavaScript or console errors',report.errors.length===0);
    report.status='PASS';
  } catch(error) {
    report.status='FAIL'; report.failure=String(error.stack||error);
    await page.screenshot({path:path.resolve(outdir,'failure.png')}).catch(()=>{});
    throw error;
  } finally {
    fs.writeFileSync(path.join(outdir,readonly?'primary-readonly-qa.json':'headless-qa.json'),JSON.stringify(report,null,2)+'\n');
    await browser.close();
  }
  console.log(JSON.stringify({status:report.status,checks:report.checks.length,posts:report.post_paths.length,outdir}));
})().catch(error=>{console.error(error);process.exitCode=1;});

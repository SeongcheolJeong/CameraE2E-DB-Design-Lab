/* One small user-driven experiment through the real browser/API/Worker path.
 * Uses installed Chromium against an explicitly chosen local project URL.
 * An existing output directory is refused so failed evidence is retained.
 */
'use strict';
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict'), crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.argv[2] || '');
if (!process.argv[2] || !output.startsWith(root + path.sep) || fs.existsSync(output)) {
  throw new Error('Supply a NEW project-local evidence directory');
}
const base = (process.env.CAMERA_UI_BASE_URL || 'http://127.0.0.1:8798').replace(/\/$/, '');
const readOnly = process.env.CAMERA_UI_READ_ONLY === '1';
const state = process.env.CAMERA_UI_EXPECTED_STATE || (base === 'http://127.0.0.1:8798'
  ? path.join(root, 'outputs/camera-platform-design-experiments-smoke-20260927-execution') : null);
const executable = process.env.CAMERA_UI_BROWSER || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const result = {started_at_utc: new Date().toISOString(), base_url: base,
  expected_state: state, checks: [], browser_errors: [], writes: [],
  execution_kind: readOnly ? 'browser_legacy_saved_result_read_only' :
    'browser_submission_to_local_worker_not_in_process_test'};
const check = (name, value) => {assert(value, name); result.checks.push(name);};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const save = (name, value) => fs.writeFileSync(path.join(output, name),
  JSON.stringify(value, null, 2) + '\n', {flag: 'wx'});
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
async function get(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(20000)});
  if (!response.ok) throw new Error(`GET ${new URL(url).pathname}: ${response.status} ${await response.text()}`);
  return response;
}
fs.mkdirSync(output, {recursive: true});
let chrome, exited, ws, profile, sequence = 0;
const pending = new Map();
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence, timer = setTimeout(() => {
      pending.delete(id); reject(new Error('CDP timeout: ' + method));
    }, 15000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);}});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const answer = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}
async function until(expression) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    const error = await evaluate('document.getElementById("error")?.textContent');
    if (error) throw new Error(error);
    await pause(150);
  }
  throw new Error('UI timeout: ' + expression);
}
async function screenshot(name) {
  fs.writeFileSync(path.join(output, name), Buffer.from(
    (await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'), {flag: 'wx'});
}

(async () => {
  try {
    const health = await (await get(base + '/api/v1/health')).json();
    check(readOnly ? 'Selected legacy local project' : 'Selected local project and running Worker',
      (!state || health.state_dir === state) && (readOnly || health.worker === 'running'));
    const templateResponse = await fetch(base + '/api/v1/design-experiments/template');
    check(readOnly ? 'Legacy state has no new R1 submission template' : 'New R1 submission template is available',
      readOnly ? templateResponse.status === 422 : templateResponse.ok);
    const template = readOnly ? null : await templateResponse.json();
    const schema = template?.specification?.schema;
    if (!readOnly) check('Known design experiment contract', ['camera_design_experiment_v1','camera_design_experiment_v2'].includes(schema));
    const r1 = schema === 'camera_design_experiment_v2';
    if (r1) check('Template is bound to explicit R1 engineering project mode',
      template.project_mode?.mode === 'r1_engineering' && Boolean(template.project_mode?.project_id));
    const modelId = r1 ? template.catalog?.models?.[1]?.id : null;
    const sceneId = r1 ? template.catalog?.scenes?.[1]?.id : null;
    if (r1) check('Second registered model and scene are offered without code changes', Boolean(modelId && sceneId));
    result.contract_schema = schema;result.selected_model_id = modelId;result.selected_scene_id = sceneId;
    const before = await (await get(base + '/api/v1/design-experiments')).json();
    result.jobs_before = before.map(j => j.id);
    profile = fs.mkdtempSync(path.join(os.tmpdir(), 'camera-design-browser-'));
    chrome = spawn(executable, ['--headless', '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
      '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
    exited = new Promise(resolve => {
      chrome.once('exit', code => resolve(code));
      chrome.once('error', error => {result.browser_errors.push(String(error)); resolve(null);});
    });
    const portFile = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 100 && !fs.existsSync(portFile); i++) await pause(100);
    if (!fs.existsSync(portFile)) throw new Error('Installed Chromium did not expose its private debugger');
    const port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]);
    const debug = 'http://127.0.0.1:' + port;
    result.browser_version = (await (await get(debug + '/json/version')).json()).Browser;
    const target = await (await fetch(debug + '/json/new?about:blank', {method: 'PUT'})).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = reject;});
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const p = pending.get(message.id);
        if (p) {pending.delete(message.id); message.error ? p.reject(new Error(JSON.stringify(message.error))) : p.resolve(message.result);}
      } else if (message.method === 'Runtime.exceptionThrown') result.browser_errors.push(message.params.exceptionDetails);
      else if (message.method === 'Network.requestWillBeSent' &&
               !['GET', 'HEAD'].includes(message.params.request.method)) {
        const r = message.params.request;
        result.writes.push({method: r.method, path: new URL(r.url).pathname});
      }
    };
    await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false});
    if (readOnly) {
      await call('Page.navigate', {url: base + '/design-experiments.html'});
      await until('document.getElementById("connection")?.textContent.includes("조회 전용") && document.getElementById("saved")?.options.length>1');
      check('Legacy saved results load without a new template or write',
        result.writes.length === 0 && await evaluate('document.getElementById("experiment-form").hidden && document.getElementById("submit").disabled'));
      const id = await evaluate('document.getElementById("saved").options[1].value');
      await evaluate(`(() => {const e=document.getElementById('saved');e.value=${JSON.stringify(id)};e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await until('!document.getElementById("result").hidden && document.querySelectorAll("#rows tr").length>0');
      const job = await (await get(base + '/api/v1/design-experiments/' + id)).json();
      save('experiment.json', job);
      check('Original v1 saved choice remains readable without R1 model/scene fields',
        job.status === 'succeeded' && job.specification?.schema === 'camera_design_experiment_v1' &&
        job.result?.summary?.selection?.selected_candidate_id === 'clear-e2-g1' &&
        !Object.hasOwn(job.specification, 'model_ids'));
      await evaluate('document.querySelector("#rows [data-case]").click()');
      await until('!document.getElementById("preview").hidden && document.getElementById("raw-image").complete && document.getElementById("raw-image").naturalWidth>0');
      check('Legacy RAW preview opens while absent RGB stays hidden',
        await evaluate('document.getElementById("rgb-preview").hidden && document.getElementById("rgb-download").hidden'));
      check('Legacy review creates no POST or browser exception', result.writes.length === 0 && result.browser_errors.length === 0);
      result.experiment_id = id;
      result.status = 'passed';
      await screenshot('legacy-read-only.png');
      return;
    }
    await call('Page.navigate', {url: base + '/#experiments'});
    await until('document.getElementById("main")?.textContent.includes("저장된 설계 결정 비교")');
    check('Main workbench opens the design experiment on the same origin',
      await evaluate('!!document.querySelector("#main a[href=\'/design-experiments.html\']") && new URL(document.querySelector("#main a[href=\'/design-experiments.html\']").href).origin===location.origin'));
    await call('Page.navigate', {url: base + '/design-experiments.html'});
    await until('!!document.getElementById("connection") && document.getElementById("connection").textContent.includes("현재 작업공간:") && !!document.getElementById("name").value');
    if (r1) check('R1 browser states its spectral, capture and physical limits',
      await evaluate(`(() => {const scope=document.getElementById('scope-warning').textContent;
        return ['R1 설계 실험 범위','16×16','500/600 nm','최대 36개 후보·128회','고정 ISP','HDR/LFM','실물 예측 정확도 검증은 포함되지'].every(part=>scope.includes(part));})()`));
    if (r1) check('Registered model and scene selectors use the advertised catalog',
      await evaluate(`document.querySelector('input[name=model][value=${JSON.stringify(modelId)}]')!==null && [...document.getElementById('scene').options].some(o=>o.value===${JSON.stringify(sceneId)})`));
    check('Opening the workbench creates no experiment', result.writes.length === 0);
    check('Submission requires explicit research acknowledgement', await evaluate('document.getElementById("submit").disabled'));
    if (r1) {
      await evaluate(`(() => {
        document.getElementById('acknowledge').checked=true;
        document.querySelectorAll('input[name=model]').forEach(el=>{el.checked=false;});
        document.getElementById('experiment-form').dispatchEvent(new Event('input',{bubbles:true}));
      })()`);
      check('Missing model disables submission without a POST',
        await evaluate('document.getElementById("submit").disabled && document.getElementById("budget").textContent.includes("등록된 모델")') && result.writes.length === 0);
    }
    await evaluate(`(() => {
      const values = {name:${JSON.stringify('실제 Worker · 후보 비교 · '+result.started_at_utc)}, exposures:'1, 2', gains:'1',
        training:'1, 2', evaluation:'1.5', repeats:'0', seed:'2701',
        signal:${r1 ? "'0'" : "'120'"}, snr:${r1 ? "'0'" : "'10'"}, well:${r1 ? "'100'" : "'1'"}, white:${r1 ? "'100'" : "'1'"}, cap:'4'};
      for (const [id,value] of Object.entries(values)) document.getElementById(id).value=value;
      ${r1 ? `document.querySelectorAll('input[name=model]').forEach(el=>{el.checked=el.value===${JSON.stringify(modelId)};});document.getElementById('scene').value=${JSON.stringify(sceneId)};` : "document.querySelectorAll('input[name=stop]').forEach(el=>{el.checked=el.value==='clear';});"}
      document.getElementById('experiment-form').dispatchEvent(new Event('input',{bubbles:true}));
    })()`);
    check('Small grid shows maximum five acquisitions', await evaluate('document.getElementById("budget").textContent.includes("최대 5회")'));
    await evaluate(`(() => {
      const ack=document.getElementById('acknowledge'); ack.checked=true;
      ack.dispatchEvent(new Event('input',{bubbles:true}));
      if(!document.getElementById('experiment-form').checkValidity()) throw new Error('Invalid smoke form');
      document.getElementById('experiment-form').requestSubmit();
    })()`);
    await until('!!document.getElementById("status") && document.getElementById("status").textContent.includes("succeeded") && !document.getElementById("result").hidden');
    result.experiment_id = await evaluate('new URL(location.href).searchParams.get("experiment")');
    const job = await (await get(base + '/api/v1/design-experiments/' + result.experiment_id)).json();
    save('experiment.json', job);
    const summary = job.result.summary;
    check('Exactly one explicit new-experiment POST', result.writes.length === 1 && result.writes[0].path === '/api/v1/design-experiments');
    check('Real Worker completed four training and one evaluation capture',
      job.status === 'succeeded' && summary.new_captures === 5 &&
      summary.rows.filter(r => r.split === 'train').length === 4 && summary.evaluation.count === 1);
    if (r1) {
      check('R1 submission freezes the second registered model and scene',
        job.specification.schema === schema &&
        JSON.stringify(job.specification.model_ids) === JSON.stringify([modelId]) &&
        job.specification.scene_id === sceneId && job.specification.acknowledge_simulation_limits === true &&
        !Object.hasOwn(job.specification, 'acknowledge_research_only') &&
        summary.selection.candidates.every(c => c.model_id === modelId && c.scene_id === sceneId) &&
        summary.rows.every(r => r.model_id === modelId && r.scene_id === sceneId));
      check('A feasible model candidate is selected before separate evaluation',
        summary.selection.feasible_count > 0 &&
        summary.selection.candidates.some(c => c.candidate_id === summary.selection.selected_candidate_id && c.feasible) &&
        summary.selection_frozen_before_evaluation && summary.evaluation.reselection === false);
    } else check('Legacy clear 2 ms choice stays compatible',
      summary.selection.selected_candidate_id === 'clear-e2-g1' &&
      summary.selection_frozen_before_evaluation && summary.evaluation.reselection === false);
    check('Selected candidate passes declared evaluation constraints', summary.evaluation.passed === 1);
    const choiceEntry = job.artifacts.find(a => a.name === 'selection-before-evaluation.json');
    const choiceBytes = Buffer.from(await (await get(base + choiceEntry.url)).arrayBuffer());
    check('Frozen choice has matching artifact hash and evaluation_seen=false',
      hash(choiceBytes) === choiceEntry.sha256 && JSON.parse(choiceBytes).evaluation_seen === false);
    fs.writeFileSync(path.join(output, 'selection-before-evaluation.json'), choiceBytes, {flag: 'wx'});
    check('Five acquisition rows are visible', await evaluate('document.querySelectorAll("#rows tr").length===5'));
    await evaluate('document.querySelector("#rows [data-case]").click()');
    await until('!document.getElementById("preview").hidden && ["raw-image","rail-image"].every(id=>{const i=document.getElementById(id);return i.complete&&i.naturalWidth>0;})');
    check('Native original RAW and final-code rail previews loaded', true);
    if (r1) {
      await until('!document.getElementById("rgb-preview").hidden && !document.getElementById("rgb-download").hidden && document.getElementById("rgb-image").complete && document.getElementById("rgb-image").naturalWidth>0');
      const rgbURL = await evaluate('document.getElementById("rgb-download").href');
      const rgbBytes = Buffer.from(await (await get(rgbURL)).arrayBuffer());
      const pngURL = await evaluate('document.getElementById("rgb-image").src');
      const pngBytes = Buffer.from(pngURL.split(',')[1], 'base64');
      check('Fixed ISP RGB original and preview match saved artifact hashes',
        rgbBytes.subarray(0,6).equals(Buffer.from([0x93,0x4e,0x55,0x4d,0x50,0x59])) &&
        hash(rgbBytes) === summary.rows[0].rgb_artifact.sha256 &&
        hash(pngBytes) === summary.rows[0].rgb_preview_artifact.sha256);
      fs.writeFileSync(path.join(output, 'first-case-rgb.npy'), rgbBytes, {flag: 'wx'});
      fs.writeFileSync(path.join(output, 'first-case-rgb.png'), pngBytes, {flag: 'wx'});
    }
    const rawURL = await evaluate('document.getElementById("raw-download").href');
    const rawBytes = Buffer.from(await (await get(rawURL)).arrayBuffer());
    check('Original NPY download matches stored RAW artifact bytes',
      rawBytes.subarray(0,6).equals(Buffer.from([0x93,0x4e,0x55,0x4d,0x50,0x59])) &&
      hash(rawBytes) === summary.rows[0].raw_artifact.sha256);
    fs.writeFileSync(path.join(output, 'first-case-raw.npy'), rawBytes, {flag: 'wx'});
    let replayId = null, expectedWrites = 1;
    if (r1 && await evaluate('!!document.getElementById("isp-replay")')) {
      await until('!document.getElementById("isp-replay").hidden && !document.getElementById("replay-form").hidden');
      check('Saved R1 case exposes an explicit ISP profile editor',
        await evaluate('!!document.getElementById("wb-r") && !!document.getElementById("replay-ccm") && !!document.getElementById("replay-tone")'));
      await evaluate(`(() => {
        const wb=document.getElementById('wb-r');wb.value=String(Math.min(16,Number(wb.value)*1.25));
        const tone=document.getElementById('replay-tone');tone.value=tone.value==='srgb'?'linear':'srgb';
        document.getElementById('replay-name').value='설치본 RAW 재처리 검증';
        document.getElementById('replay-form').requestSubmit();
      })()`);
      await until('!document.getElementById("replay-preview").hidden && !document.getElementById("replay-comparison").hidden && document.getElementById("replay-rgb-image").complete && document.getElementById("replay-rgb-image").naturalWidth>0');
      replayId = await evaluate('document.getElementById("replay-saved").value');
      const replay = await (await get(base + '/api/v1/design-isp-replays/' + replayId)).json();
      const replayRGB = replay.artifacts.find(a => a.name === 'replay-rgb.npy');
      const replayBytes = Buffer.from(await (await get(base + replayRGB.url)).arrayBuffer());
      check('Browser submitted exactly one saved-RAW ISP replay',
        result.writes.length===2 && result.writes[1].path==='/api/v1/design-isp-replays' &&
        replay.status==='succeeded' && replay.result.summary.new_captures===0 &&
        replay.result.summary.input_unchanged===true &&
        replay.result.summary.source_raw_sha256===summary.rows[0].raw_artifact.sha256);
      check('Browser shows changed RGB with matching saved NPY bytes',
        replay.result.summary.comparison.mean_abs_rgb_delta>0 &&
        replayRGB.sha256===hash(replayBytes) &&
        replayRGB.sha256!==summary.rows[0].rgb_artifact.sha256 &&
        await evaluate('document.getElementById("replay-comparison").textContent.includes("새로운 촬영 0회")'));
      result.replay_id=replayId;
      result.replay_rgb_sha256=replayRGB.sha256;
      fs.writeFileSync(path.join(output, 'replay-rgb.npy'), replayBytes, {flag: 'wx'});
      expectedWrites=2;
    }
    await screenshot('desktop-result.png');
    await call('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    check('Mobile layout has no page-wide horizontal overflow', await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
    await screenshot('mobile.png');
    const previousDocumentOrigin = await evaluate('performance.timeOrigin');
    await call('Page.navigate', {url: base + '/design-experiments.html?experiment=' + result.experiment_id});
    await until(`performance.timeOrigin!==${JSON.stringify(previousDocumentOrigin)} && !!document.getElementById("result") && !document.getElementById("result").hidden && document.querySelectorAll("#rows tr").length===5`);
    const after = await (await get(base + '/api/v1/design-experiments')).json();
    check('Saved result reopens without a new job or POST', after.length === before.length + 1 && after.some(item=>item.id===result.experiment_id) && result.writes.length === expectedWrites);
    if (replayId) {
      await evaluate('document.querySelector("#rows [data-case]").click()');
      await until(`!![...document.getElementById("replay-saved").options].find(o=>o.value===${JSON.stringify(replayId)})`);
      await evaluate(`(() => {const picker=document.getElementById('replay-saved');picker.value=${JSON.stringify(replayId)};picker.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await until('!document.getElementById("replay-preview").hidden && !document.getElementById("replay-comparison").hidden');
      check('Saved ISP replay reopens in browser without another capture or POST',
        result.writes.length===expectedWrites &&
        await evaluate(`document.getElementById('replay-saved').value===${JSON.stringify(replayId)}`));
    }
    check('Reload does not pre-consent to a new experiment', await evaluate('!document.getElementById("acknowledge").checked'));
    check('No browser exceptions', result.browser_errors.length === 0);
    result.captures = summary.rows.map(r => ({candidate_id:r.candidate_id,model_id:r.model_id,scene_id:r.scene_id, split:r.split,
      radiance_scale:r.radiance_scale, green_dn:r.green_dn, green_snr_p10:r.green_snr_p10,
      well_fraction:r.well_fraction, white_fraction:r.white_fraction, violations:r.violations}));
    result.new_captures = summary.new_captures;
    result.selection = summary.selection.selected_candidate_id;
    result.status = 'passed';
  } catch (error) {
    result.status = 'failed'; result.error = String(error); process.exitCode = 1;
    if (ws?.readyState === 1) {try {await screenshot('failure.png');} catch {}}
  } finally {
    if (ws) ws.close();
    if (chrome) {
      chrome.kill('SIGTERM');
      if (await Promise.race([exited.then(()=>true),pause(3000).then(()=>false)]) === false) {
        chrome.kill('SIGKILL'); await exited;
      }
    }
    if (profile) fs.rmSync(profile, {recursive: true, force: true});
    result.finished_at_utc = new Date().toISOString();
    save('browser-qa.json', result);
    console.log(JSON.stringify(result, null, 2));
  }
})();

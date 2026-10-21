/* Fresh Chromium/CDP smoke for the installed R1 workbench's registered HDR path.
 * Usage: node tools/verify_camera_r1_modes_ui.cjs BASE_URL SEED_JSON NEW_OUTPUT_DIR
 * The installed server, Worker and registry seed must already exist.
 */
'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const [baseArgument, seedPath, outputArgument] = process.argv.slice(2);
if (!baseArgument || !seedPath || !outputArgument) {
  throw Error('Usage: node tools/verify_camera_r1_modes_ui.cjs BASE_URL SEED_JSON NEW_OUTPUT_DIR');
}
const parsedBase = new URL(baseArgument);
assert.equal(parsedBase.protocol, 'http:', 'Installed smoke requires local HTTP');
assert(['127.0.0.1', 'localhost'].includes(parsedBase.hostname), 'Base URL must be loopback');
assert(parsedBase.pathname === '/' && !parsedBase.search && !parsedBase.hash,
  'Base URL must contain only the origin');
const base = parsedBase.origin;
const seed = JSON.parse(fs.readFileSync(seedPath, 'utf8'));
assert.equal(seed.schema, 'r1-registry-pilot-v1', 'Unexpected registry seed schema');
const ids = {
  assembly: seed.assembly_id,
  sceneStatic: seed.scene_sequence_ids?.static,
  sceneChanging: seed.scene_sequence_ids?.changing,
  controlStatic: seed.control_sequence_ids?.static,
  controlChanging: seed.control_sequence_ids?.changing,
  programHDR: seed.capture_program_ids?.hdr,
  programDCG: seed.capture_program_ids?.dcg,
  testTemporal: seed.test_ids?.temporal,
  testHDR: seed.test_ids?.hdr,
  testDCG: seed.test_ids?.dcg,
};
for (const [key, value] of Object.entries(ids)) {
  assert(typeof value === 'string' && value.length > 0, `Seed lacks ${key}`);
}
const output = path.resolve(outputArgument);
assert(!fs.existsSync(output), 'Evidence output directory must be new');
fs.mkdirSync(output);
const executable = process.env.CAMERA_UI_BROWSER || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const proof = {
  schema: 'camera_r1_modes_installed_browser_smoke_v1',
  status: 'running', base_url: base, started_at_utc: new Date().toISOString(),
  seed_schema: seed.schema, seed_ids: ids,
  browser: {headless: true, fresh_context: true, user_profile_accessed: false},
  checks: [], post_paths: [], browser_errors: [],
};
const check = (name, condition) => {
  assert(condition, name);
  proof.checks.push(name);
};
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const get = async url => {
  const response = await fetch(url, {signal: AbortSignal.timeout(30000)});
  if (!response.ok) {
    throw new Error(`GET ${new URL(url).pathname}: ${response.status} ${await response.text()}`);
  }
  return response;
};
const api = async route => (await get(`${base}/api/v1/${route}`)).json();

function npyHeader(bytes) {
  assert(bytes.length >= 12 && bytes[0] === 0x93 && bytes.subarray(1, 6).toString() === 'NUMPY',
    'RAW artifact is not an NPY file');
  const major = bytes[6];
  assert([1, 2, 3].includes(major), 'Unsupported NPY header version');
  const start = major === 1 ? 10 : 12;
  const length = major === 1 ? bytes.readUInt16LE(8) : bytes.readUInt32LE(8);
  assert(start + length <= bytes.length && length > 0 && length <= 65536,
    'Incomplete or unbounded NPY header');
  const header = bytes.subarray(start, start + length).toString(major === 3 ? 'utf8' : 'latin1');
  const shapeText = header.match(/['"]shape['"]\s*:\s*\(([^)]*)\)/)?.[1];
  const dtype = header.match(/['"]descr['"]\s*:\s*['"]([^'"]+)['"]/)?.[1];
  assert(shapeText !== undefined && dtype, 'NPY header lacks shape or dtype');
  const shape = shapeText.split(',').map(value => value.trim()).filter(Boolean).map(Number);
  assert(shape.length && shape.every(value => Number.isSafeInteger(value) && value > 0),
    'NPY shape is invalid');
  const match = dtype.match(/^[<|=]([iu])([1248])$/);
  assert(match, 'RAW NPY dtype must be an integer code container');
  const itemBytes = Number(match[2]);
  const expected = shape.reduce((product, value) => product * value, 1) * itemBytes;
  assert(Number.isSafeInteger(expected) && start + length + expected === bytes.length,
    'RAW NPY payload length differs from declared shape');
  return {version: `${major}.${bytes[7]}`, shape, dtype, item_bytes: itemBytes};
}

let chrome, exited, ws, profile, sequence = 0;
const pending = new Map();
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id); reject(new Error('CDP timeout: ' + method));
    }, 20000);
    pending.set(id, {
      resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);},
    });
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const answer = await call('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  });
  if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}
async function until(expression, seconds = 90) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error('UI timeout: ' + expression);
}
async function select(id, value) {
  await until(`!!document.getElementById(${JSON.stringify(id)}) &&
    [...document.getElementById(${JSON.stringify(id)}).options].some(option=>option.value===${JSON.stringify(value)})`);
  const selected = await evaluate(`(() => {
    const el=document.getElementById(${JSON.stringify(id)});
    if(!el||![...el.options].some(option=>option.value===${JSON.stringify(value)}))return false;
    el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('change',{bubbles:true}));return true;
  })()`);
  check(`${id} offers ${value}`, selected);
  // Registry projections resolve their full records asynchronously. During that
  // fetch the main panel is replaced by a loading placeholder, so wait for the
  // form and selected value to return before touching the next control.
  await until(`!!document.querySelector('form[data-form="run"]') &&
    document.getElementById(${JSON.stringify(id)})?.value===${JSON.stringify(value)}`);
}
async function screenshot(name) {
  fs.writeFileSync(path.join(output, name), Buffer.from(
    (await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'), {flag: 'wx'});
}
const hdrPanel = `(() => [...document.querySelectorAll('section.panel')].find(section =>
  section.querySelector('h2')?.textContent==='전기 Readout과 HDR·주기광 결과'))()`;

async function main() {
  try {
    const before = await api('workbench');
    const health = await api('health');
    check('Installed workbench and Worker are available',
      before.schema === 'camera_platform_v1' && health.worker === 'running');
    const available = (name, id) => Array.isArray(before[name]) &&
      before[name].some(record => record.id === id);
    check('Seeded HDR inputs are registered',
      available('assemblies', ids.assembly) &&
      available('scene_sequences', ids.sceneStatic) &&
      available('control_sequences', ids.controlStatic) &&
      available('capture_programs', ids.programHDR) &&
      available('tests', ids.testHDR));
    const beforeRunIds = new Set((before.runs || []).map(run => run.id));

    profile = fs.mkdtempSync(path.join(os.tmpdir(), 'camera-r1-modes-browser-'));
    chrome = spawn(executable, ['--headless', '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
      '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
    exited = new Promise(resolve => {
      chrome.once('exit', code => resolve(code));
      chrome.once('error', error => {proof.browser_errors.push(String(error)); resolve(null);});
    });
    const portFile = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 200 && !fs.existsSync(portFile); i++) await delay(100);
    assert(fs.existsSync(portFile), 'Installed Chromium did not expose its private debugger');
    const port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]);
    const debug = 'http://127.0.0.1:' + port;
    proof.browser.version = (await (await get(debug + '/json/version')).json()).Browser;
    const target = await (await fetch(debug + '/json/new?about:blank', {method: 'PUT'})).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = reject;});
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const receiver = pending.get(message.id);
        if (receiver) {
          pending.delete(message.id);
          message.error ? receiver.reject(new Error(JSON.stringify(message.error))) :
            receiver.resolve(message.result);
        }
      } else if (message.method === 'Runtime.exceptionThrown') {
        proof.browser_errors.push(message.params.exceptionDetails);
      } else if (message.method === 'Network.requestWillBeSent' &&
                 message.params.request.method === 'POST') {
        proof.post_paths.push(new URL(message.params.request.url).pathname);
      }
    };
    await call('Page.enable');
    await call('Runtime.enable');
    await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride', {
      width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
    });
    await call('Page.navigate', {url: base + '/#experiments'});
    await until('!!document.getElementById("run-runtime") && !!document.getElementById("run-assembly")');
    await select('run-assembly', ids.assembly);

    // Selector-only checks; API proof runs the temporal and DCG Workers.
    await select('run-runtime', 'temporal_scene_v1');
    await select('run-scene-sequence', ids.sceneChanging);
    await select('run-control-sequence', ids.controlChanging);
    await select('run-test', ids.testTemporal);
    check('Temporal registered test enables submission', await evaluate(
      'document.querySelector(\'form[data-form="run"]\').checkValidity() && !document.querySelector(\'form[data-form="run"] button[type="submit"]\').disabled'));
    await select('run-runtime', 'shared_charge_dcg_v1');
    await select('run-capture-program', ids.programDCG);
    await select('run-scene-sequence', ids.sceneStatic);
    await select('run-control-sequence', ids.controlStatic);
    await select('run-test', ids.testDCG);
    check('DCG registered program and test enable submission', await evaluate(
      'document.querySelector(\'form[data-form="run"]\').checkValidity() && !document.querySelector(\'form[data-form="run"] button[type="submit"]\').disabled'));

    await select('run-runtime', 'hdr_bracket_v1');
    await select('run-capture-program', ids.programHDR);
    await select('run-scene-sequence', ids.sceneStatic);
    await select('run-control-sequence', ids.controlStatic);
    await select('run-test', ids.testHDR);
    const seedValue = 50_000_000 + Date.now() % 900_000_000;
    await evaluate(`(() => {const el=document.getElementById('run-seed');
      el.value=${JSON.stringify(String(seedValue))};
      el.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    const selected = await evaluate(`[
      'run-runtime','run-assembly','run-scene-sequence','run-control-sequence',
      'run-capture-program','run-test'].map(id=>document.getElementById(id)?.value)`);
    check('HDR form has the exact seeded registered inputs',
      JSON.stringify(selected) === JSON.stringify([
        'hdr_bracket_v1', ids.assembly, ids.sceneStatic, ids.controlStatic,
        ids.programHDR, ids.testHDR,
      ]));
    check('HDR form enables submission', await evaluate(
      'document.querySelector(\'form[data-form="run"]\').checkValidity() && !document.querySelector(\'form[data-form="run"] button[type="submit"]\').disabled'));
    await evaluate('document.querySelector(\'form[data-form="run"]\').requestSubmit()');
    const deadline = Date.now() + 180000;
    let completed;
    while (Date.now() < deadline) {
      const runs = await api('runs');
      const created = runs.find(run => !beforeRunIds.has(run.id) &&
        run.plugin_id === 'hdr_bracket_v1' && run.parameters?.seed === seedValue);
      if (created) {
        proof.run_id = created.id;
        const run = await api(`runs/${encodeURIComponent(created.id)}`);
        if (['succeeded', 'failed', 'cancelled'].includes(run.status)) {
          completed = run; break;
        }
      }
      await delay(400);
    }
    assert(completed, 'HDR Worker Run did not reach a terminal state');
    assert.equal(completed.status, 'succeeded', JSON.stringify(completed.error));
    check('Browser submitted one exact HDR request',
      proof.post_paths.length === 1 && proof.post_paths[0] === '/api/v1/runs' &&
      completed.plugin_id === 'hdr_bracket_v1' &&
      completed.assembly_id === ids.assembly &&
      completed.scene_sequence_id === ids.sceneStatic &&
      completed.control_sequence_id === ids.controlStatic &&
      completed.capture_program_id === ids.programHDR &&
      completed.test_id === ids.testHDR && completed.parameters?.seed === seedValue);
    proof.submitted_seed = seedValue;
    check('Worker saved four-frame HDR fusion',
      completed.result?.summary?.hdr_merge_applied === true &&
      completed.result.summary.frames?.length === 4 &&
      completed.result.summary.physical_validation === 'not_established');
    const raw = completed.artifacts.find(artifact => artifact.name === 'raw_frames.npy');
    assert(raw?.url && raw.sha256, 'Saved Run lacks raw_frames.npy');
    const rawURL = new URL(raw.url, base);
    assert.equal(rawURL.origin, base, 'RAW artifact must come from installed server');
    const rawBytes = Buffer.from(await (await get(rawURL.href)).arrayBuffer());
    const npy = npyHeader(rawBytes);
    check('Saved RAW artifact is integer 4x16x16 with matching bytes',
      JSON.stringify(npy.shape) === JSON.stringify([4, 16, 16]) &&
      sha256(rawBytes) === raw.sha256);
    proof.raw_artifact = {name: raw.name, sha256: raw.sha256,
      bytes: rawBytes.length, shape: npy.shape, dtype: npy.dtype};

    await until(`!!document.querySelector('[data-action="select-run"][data-id=${JSON.stringify(completed.id)}]')`);
    await evaluate(`document.querySelector('[data-action="select-run"][data-id=${JSON.stringify(completed.id)}]').click()`);
    await until(`${hdrPanel}?.textContent.includes('유효 RAW 합성 픽셀')`);
    const panel = await evaluate(`(() => {const panel=${hdrPanel};return {
      weights:[...panel.querySelectorAll('details > summary')].some(x=>x.textContent.includes('HDR 가중치')),
      frames:panel.querySelectorAll('select[data-frame-job=${JSON.stringify(completed.id)}] option').length,
      preview:panel.querySelector('img.preview-image')?.src,
      loaded:panel.querySelector('img.preview-image')?.complete && panel.querySelector('img.preview-image')?.naturalWidth>0,
    };})()`);
    const mergedArtifact = completed.artifacts.find(artifact => artifact.name === 'sensor-rgb.png');
    assert(mergedArtifact?.url, 'Saved merged HDR preview is missing');
    check('Saved HDR result panel and merge diagnostics are visible',
      panel.weights && panel.frames === 4);
    await until(`${hdrPanel}?.querySelector('img.preview-image')?.complete && ${hdrPanel}?.querySelector('img.preview-image')?.naturalWidth>0`);
    check('Merged HDR preview is rendered from saved artifact',
      new URL(panel.preview, base).pathname === new URL(mergedArtifact.url, base).pathname);

    const previousOrigin = await evaluate('performance.timeOrigin');
    await call('Page.reload');
    await until(`performance.timeOrigin!==${JSON.stringify(previousOrigin)} && !!document.getElementById('run-runtime')`);
    await until(`!!document.querySelector('[data-action="select-run"][data-id=${JSON.stringify(completed.id)}]')`);
    await evaluate(`document.querySelector('[data-action="select-run"][data-id=${JSON.stringify(completed.id)}]').click()`);
    await until(`${hdrPanel}?.textContent.includes('유효 RAW 합성 픽셀')`);
    check('Reload reopens the same saved HDR result without another submission',
      await evaluate(`${hdrPanel}?.querySelectorAll('select[data-frame-job=${JSON.stringify(completed.id)}] option').length===4`) &&
      proof.post_paths.length === 1);
    const reopened = await api(`runs/${encodeURIComponent(completed.id)}`);
    check('Reopened Run retains original RAW and merged HDR identity',
      reopened.status === 'succeeded' &&
      reopened.result?.summary?.hdr_merge_applied === true &&
      reopened.artifacts.find(artifact => artifact.name === 'raw_frames.npy')?.sha256 === raw.sha256 &&
      reopened.artifacts.find(artifact => artifact.name === 'sensor-rgb.png')?.sha256 ===
        mergedArtifact.sha256);
    await screenshot('hdr-reopened.png');
    proof.screenshot = path.join(output, 'hdr-reopened.png');
    check('No browser exceptions and exactly one UI POST',
      proof.browser_errors.length === 0 &&
      JSON.stringify(proof.post_paths) === JSON.stringify(['/api/v1/runs']));
    proof.status = 'passed';
  } catch (error) {
    proof.status = 'failed';
    proof.failure = String(error.stack || error);
    if (ws?.readyState === 1) {
      try {await screenshot('failure.png'); proof.failure_screenshot = path.join(output, 'failure.png');}
      catch {}
    }
    throw error;
  } finally {
    if (ws) ws.close();
    if (chrome) {
      chrome.kill('SIGTERM');
      if (await Promise.race([exited.then(() => true), delay(3000).then(() => false)]) === false) {
        chrome.kill('SIGKILL'); await exited;
      }
    }
    if (profile) fs.rmSync(profile, {recursive: true, force: true});
    proof.finished_at_utc = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'browser-smoke.json'),
      JSON.stringify(proof, null, 2) + '\n', {flag: 'wx'});
  }
}

if (require.main === module) {
  main().then(() => console.log(JSON.stringify({status: proof.status,
    checks: proof.checks.length, run_id: proof.run_id,
    evidence: path.join(output, 'browser-smoke.json')})))
    .catch(error => {console.error(error); process.exitCode = 1;});
}

module.exports = {npyHeader};

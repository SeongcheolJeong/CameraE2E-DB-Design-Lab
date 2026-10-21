/* Installed-wheel Chromium smoke: saved HDR chart and bounded optimizer jobs. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const base = (process.argv[2] || '').replace(/\/$/, '');
assert(/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base), 'Pass an isolated localhost origin');
const output = process.argv[3];
assert(output && path.isAbsolute(output), 'Pass an absolute evidence directory');
const executable = process.env.CAMERA_UI_BROWSER || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'camera-saved-browser-'));
let chrome, ws, nextId = 0;
const pending = new Map(), errors = [], writes = [], requests = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => {pending.delete(id); reject(Error('CDP timeout: ' + method));}, 20000);
    pending.set(id, {
      resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);},
    });
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const answer = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (answer.exceptionDetails) throw Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}
async function until(expression, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    const error = await evaluate('document.getElementById("error")?.textContent');
    if (error) throw Error('UI error: ' + error);
    const status = await evaluate('document.getElementById("status")?.textContent || ""');
    if (/^(failed|cancelled)\b/.test(status)) throw Error('Worker ended: ' + status);
    await pause(250);
  }
  throw Error('UI timeout: ' + expression);
}
async function get(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(30000)});
  assert(response.ok, `GET ${url}: ${response.status}`);
  return response;
}
async function screenshot(filename) {
  const target = path.join(output, filename);
  fs.writeFileSync(target, Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'), {flag: 'wx'});
  return target;
}

(async () => {
  try {
    const health = await (await get(base + '/api/v1/health')).json();
    assert.equal(health.worker, 'running', 'Real Worker must be running');
    assert.notEqual(health.workspace, process.cwd(), 'Use copied workspace, not source checkout');
    assert(fs.existsSync(executable), 'Local Chromium headless shell not installed');
    fs.mkdirSync(output, {recursive: true});
    chrome = spawn(executable, ['--headless', '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
      '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
    const portFile = path.join(profile, 'DevToolsActivePort');
    for (let attempt = 0; attempt < 100 && !fs.existsSync(portFile); attempt++) await pause(100);
    assert(fs.existsSync(portFile), 'Chromium debugger did not start');
    const port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]);
    const target = await (await fetch('http://127.0.0.1:' + port + '/json/new?about:blank', {method: 'PUT'})).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = reject;});
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const item = pending.get(message.id);
        if (item) {pending.delete(message.id); message.error ? item.reject(Error(JSON.stringify(message.error))) : item.resolve(message.result);}
      } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
      else if (message.method === 'Network.requestWillBeSent') {
        const request = message.params.request;
        requests.push({method: request.method, url: request.url});
        if (!['GET', 'HEAD'].includes(request.method)) writes.push({method: request.method, path: new URL(request.url).pathname});
      }
    };
    await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false});
    await call('Page.navigate', {url: base + '/saved-sensor-workbench.html'});
    await until('document.getElementById("connection")?.textContent.includes("연결했습니다") && [...document.getElementById("quality-sensor-a").options].some(o=>o.textContent.includes("DEF-1801-802")) && [...document.getElementById("search-sensor").options].some(o=>o.textContent.includes("DEP-2209-801"))');
    assert.equal(await evaluate('document.getElementById("quality-submit").disabled'), true, 'Acknowledgement gates quality submission');
    assert.equal(await evaluate('document.getElementById("search-submit").disabled'), true, 'Acknowledgement gates search submission');
    await evaluate(`(() => {
      const select=document.getElementById('quality-sensor-a');
      select.value=[...select.options].find(o=>o.textContent.includes('DEF-1801-802')).value;
      for(const box of document.querySelectorAll('input[name="quality-scene"]')) box.checked=box.value==='hdr_chart_d65';
      document.getElementById('quality-name').value='installed wheel browser HDR chart';
      document.getElementById('quality-acknowledge').checked=true;
      document.getElementById('quality-form').dispatchEvent(new Event('change',{bubbles:true}));
      if(document.getElementById('quality-submit').disabled) throw Error('Quality form remained disabled');
      document.getElementById('quality-form').requestSubmit();
    })()`);
    await until('document.getElementById("status")?.textContent.startsWith("succeeded") && new URL(location.href).searchParams.get("kind")==="quality"', 180000);
    const qualityId = await evaluate('new URL(location.href).searchParams.get("id")');
    const quality = await (await get(base + '/api/v1/saved-sensor-quality/' + encodeURIComponent(qualityId))).json();
    assert.equal(quality.job_kind, 'saved_sensor_quality');
    assert.equal(quality.status, 'succeeded');
    const qualityReport = quality.result.summary.sensor_reports[0];
    assert.equal(qualityReport.cases.length, 1);
    assert.equal(qualityReport.cases[0].scene, 'hdr_chart_d65');
    assert.equal(qualityReport.cases[0].metrics.hdr_chart_response.levels_bright_to_dark.length, 6);
    assert.equal(qualityReport.cases[0].metrics.hdr_chart_response.hdr_fusion_performed, false);
    assert.equal(qualityReport.cases[0].metrics.hdr_chart_response.lfm_evaluated, false);
    assert.equal(await evaluate('document.querySelectorAll("#result-summary .subresult tbody tr").length'), 6);
    assert.equal(await evaluate('document.querySelectorAll("#artifacts a[href^=" + JSON.stringify("/api/v1/artifacts/") + "]").length'), 3);
    const npz = Buffer.from(await (await get(base + qualityReport.artifacts.npz.url)).arrayBuffer());
    assert(npz.subarray(0, 2).equals(Buffer.from('PK')), 'Quality arrays are stored as NPZ');
    const qualityScreenshot = await screenshot('saved-sensor-quality-hdr-dev5.png');

    await evaluate(`(() => {
      const select=document.getElementById('search-sensor');
      select.value=[...select.options].find(o=>o.textContent.includes('DEP-2209-801')).value;
      document.getElementById('search-name').value='installed wheel browser exposure gain';
      document.getElementById('search-acknowledge').checked=true;
      document.getElementById('search-form').dispatchEvent(new Event('change',{bubbles:true}));
      if(document.getElementById('search-submit').disabled) throw Error('Search form remained disabled');
      document.getElementById('search-form').requestSubmit();
    })()`);
    await until('document.getElementById("status")?.textContent.startsWith("succeeded") && new URL(location.href).searchParams.get("kind")==="search"', 180000);
    const searchId = await evaluate('new URL(location.href).searchParams.get("id")');
    const search = await (await get(base + '/api/v1/saved-sensor-search/' + encodeURIComponent(searchId))).json();
    assert.equal(search.job_kind, 'saved_sensor_search');
    assert.equal(search.status, 'succeeded');
    assert.equal(search.result.summary.search.case_count, 4);
    assert.equal(search.result.summary.search.cases.length, 4);
    assert.equal(search.result.summary.search.raw_arrays_exported, false);
    assert.equal(await evaluate('document.querySelectorAll("#result-summary tbody tr").length'), 4);
    assert.equal(await evaluate('document.querySelectorAll("#artifacts a").length'), 1);
    const searchScreenshot = await screenshot('saved-sensor-search-dev5.png');
    await call('Page.navigate', {url: base + '/saved-sensor-workbench.html?kind=search&id=' + encodeURIComponent(searchId)});
    await until('document.getElementById("status")?.textContent.startsWith("succeeded") && document.querySelectorAll("#result-summary tbody tr").length===4');
    assert.equal(await evaluate('document.getElementById("provenance").textContent.includes("result_sha256")'), true);
    assert.deepEqual(writes, [
      {method: 'POST', path: '/api/v1/saved-sensor-quality'},
      {method: 'POST', path: '/api/v1/saved-sensor-search'},
    ]);
    assert.equal(errors.length, 0, 'No browser runtime exceptions');
    assert(requests.every(item => item.url.startsWith(base) || item.url.startsWith('about:') || item.url.startsWith('data:')),
      'No external browser requests');
    const evidence = {
      schema: 'camerae2e_saved_sensor_workbench_browser_smoke_v1',
      status: 'passed',
      runtime: 'installed_dev5_wheel',
      worker: health.worker,
      workspace: health.workspace,
      state_dir: health.state_dir,
      quality: {id: qualityId, saved_run_id: qualityReport.revision.run_id,
        scene: 'hdr_chart_d65', levels: 6, hdr_fusion_performed: false,
        npz_sha256: qualityReport.artifacts.npz.sha256, screenshot: qualityScreenshot},
      search: {id: searchId, saved_run_id: search.result.summary.search.revision.run_id,
        case_count: 4, feasible_count: search.result.summary.search.feasible_count,
        raw_arrays_exported: false, screenshot: searchScreenshot, reopened: true},
      writes,
      browser_exceptions: errors.length,
      external_requests: 0,
      validation_boundary: 'Local installed-wheel browser, API and Worker smoke; no physical validation.',
    };
    const evidencePath = path.join(output, 'saved-sensor-workbench-browser-dev5.json');
    fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + '\n', {flag: 'wx'});
    console.log(JSON.stringify({evidence: evidencePath, quality_id: qualityId, search_id: searchId}));
  } finally {
    if (ws) ws.close();
    if (chrome) {chrome.kill('SIGTERM'); await Promise.race([new Promise(resolve => chrome.once('exit', resolve)), pause(3000)]);}
    fs.rmSync(profile, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

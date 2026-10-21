/* Fresh local Chromium smoke: browser form -> Worker -> two separate DB tracks. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const base = (process.argv[2] || '').replace(/\/$/, '');
assert(/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base), 'Pass a local HTTP origin');
const screenshotPath = process.argv[3];
assert(screenshotPath && path.isAbsolute(screenshotPath), 'Pass an absolute screenshot path');
const executable = process.env.CAMERA_UI_BROWSER || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'camera-db-browser-'));
let chrome, ws, nextId = 0;
const pending = new Map(), errors = [], writes = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => {pending.delete(id); reject(Error('CDP timeout: ' + method));}, 20000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);},
      reject: error => {clearTimeout(timer); reject(error);}});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const answer = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (answer.exceptionDetails) throw Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}
async function until(expression, timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    const error = await evaluate('document.getElementById("error")?.textContent');
    if (error) throw Error(error);
    const status = await evaluate('document.getElementById("status")?.textContent || ""');
    if (/^(failed|cancelled)\b/.test(status)) throw Error('Worker ended: ' + status);
    await pause(180);
  }
  throw Error('UI timeout: ' + expression);
}
async function get(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(30000)});
  assert(response.ok, `GET ${url}: ${response.status}`);
  return response;
}

(async () => {
  try {
    const health = await (await get(base + '/api/v1/health')).json();
    assert.equal(health.worker, 'running', 'Use an isolated running Worker');
    chrome = spawn(executable, ['--headless', '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
      '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
    const portFile = path.join(profile, 'DevToolsActivePort');
    for (let attempt = 0; attempt < 100 && !fs.existsSync(portFile); attempt++) await pause(100);
    assert(fs.existsSync(portFile), 'Chromium debugger did not start');
    const port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]);
    const debug = 'http://127.0.0.1:' + port;
    const target = await (await fetch(debug + '/json/new?about:blank', {method: 'PUT'})).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = reject;});
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const item = pending.get(message.id);
        if (item) {pending.delete(message.id); message.error ? item.reject(Error(JSON.stringify(message.error))) : item.resolve(message.result);}
      } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
      else if (message.method === 'Network.requestWillBeSent' && !['GET', 'HEAD'].includes(message.params.request.method)) {
        writes.push({method: message.params.request.method, path: new URL(message.params.request.url).pathname});
      }
    };
    await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false});
    await call('Page.navigate', {url: base + '/db-comparisons.html'});
    await until('document.getElementById("connection")?.textContent.includes("연결했습니다") && document.getElementById("lens-a")?.value && document.getElementById("sensor-a")?.value');
    assert.equal(await evaluate('document.getElementById("submit").disabled'), true, 'Acknowledgement must gate submission');
    assert.equal(await evaluate('document.getElementById("lens-a").value !== document.getElementById("lens-b").value && document.getElementById("sensor-a").value !== document.getElementById("sensor-b").value'), true);
    await evaluate('(() => {const e=document.getElementById("acknowledge");e.checked=true;e.dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("comparison-form").requestSubmit();})()');
    await until('document.getElementById("status")?.textContent.includes("succeeded") && !document.getElementById("result").hidden', 120000);
    const id = await evaluate('new URL(location.href).searchParams.get("comparison")');
    const detail = await (await get(base + '/api/v1/db-comparisons/' + encodeURIComponent(id))).json();
    assert.equal(detail.status, 'succeeded');
    assert.equal(detail.result.summary.lens_geometry.lenses.length, 2);
    assert.equal(detail.result.summary.sensor_raw.captures.length, 2);
    assert.equal(detail.result.summary.lens_sensor_cross_product.status, 'not_executed');
    assert.equal(await evaluate('document.querySelectorAll("#lens-results table tbody tr").length'), 2);
    assert.equal(await evaluate('document.querySelectorAll("#sensor-results .card").length'), 2);
    assert.equal(await evaluate('document.querySelectorAll("#sensor-results a[href^=\'/api/v1/artifacts/\']").length'), 2);
    assert.equal(await evaluate('document.getElementById("sensor-results").textContent.includes("저장 readout과 공통 적용값")'), true);
    assert.equal(await evaluate('document.querySelectorAll("#sensor-results table")[0]?.querySelectorAll("tbody tr").length'), 6);
    assert.equal(await evaluate('document.getElementById("sensor-results").textContent.includes("실행값")'), true);
    assert.equal(await evaluate('document.getElementById("cross-product").textContent.includes("미실행")'), true);
    assert.deepEqual(writes, [{method: 'POST', path: '/api/v1/db-comparisons'}]);
    assert.equal(errors.length, 0, 'No browser runtime exceptions');
    for (const capture of detail.result.summary.sensor_raw.captures) {
      const bytes = Buffer.from(await (await get(base + capture.raw_artifact.url)).arrayBuffer());
      assert(bytes.subarray(0, 6).equals(Buffer.from([0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59])), 'RAW is an NPY file');
    }
    fs.writeFileSync(screenshotPath, Buffer.from((await call('Page.captureScreenshot', {format: 'png'})).data, 'base64'), {flag: 'wx'});
    console.log(JSON.stringify({status: 'passed', id, screenshot: screenshotPath, writes, lens_count: 2, sensor_count: 2, cross_product: 'not_executed'}));
  } finally {
    if (ws) ws.close();
    if (chrome) {chrome.kill('SIGTERM'); await Promise.race([new Promise(resolve => chrome.once('exit', resolve)), pause(3000)]);}
    fs.rmSync(profile, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

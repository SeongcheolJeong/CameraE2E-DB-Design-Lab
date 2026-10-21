/* Browser integration using installed Chromium and Node's built-in CDP WebSocket. */
'use strict';
const fs=require('node:fs'), path=require('node:path'), os=require('node:os');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'), output=path.resolve(process.argv[2]||'');
if (!process.argv[2] || !output.startsWith(root+path.sep)) throw new Error('Supply project-local output directory');
const base='http://127.0.0.1:8796';
const marker=JSON.parse(fs.readFileSync(path.join(root,'outputs/camera-platform-raw-temporal-dev-20260926/raw-temporal-dev.json')));
const executable=process.env.CAMERA_UI_BROWSER||path.join(os.homedir(),'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const result={source_run_id:marker.source_run_id,checks:[],errors:[],posts:[],base_url:base};
const check=(name,value)=>{assert(value,name);result.checks.push(name);};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'camera-raw-replay-browser-'));
fs.mkdirSync(output,{recursive:true});
const chrome=spawn(executable,['--headless','--remote-debugging-address=127.0.0.1','--remote-debugging-port=9336',
  '--no-first-run','--no-default-browser-check','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
const exited=new Promise(resolve=>{chrome.once('exit',code=>resolve(code));chrome.once('error',error=>{result.errors.push(String(error));resolve(null);});});
let ws, sequence=0;const pending=new Map();
function call(method,params={}){return new Promise((resolve,reject)=>{
  const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout '+method));},15000);
  pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
  ws.send(JSON.stringify({id,method,params}));
});}
async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function until(expression){const deadline=Date.now()+60000;while(Date.now()<deadline){
  if(await evaluate(expression))return;const error=await evaluate('document.getElementById("error")?.textContent');
  if(error)throw new Error(error);await sleep(200);}throw new Error('UI timeout: '+expression);}

(async()=>{
 try{
  let version;for(let i=0;i<50;i++){try{version=await (await fetch('http://127.0.0.1:9336/json/version')).json();break;}catch{await sleep(100);}}
  if(!version)throw new Error('Installed Chromium did not start');
  result.browser_version=version.Browser;
  const target=await(await fetch('http://127.0.0.1:9336/json/new?about:blank',{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{const message=JSON.parse(event.data);if(message.id){const p=pending.get(message.id);if(p){pending.delete(message.id);message.error?p.reject(new Error(JSON.stringify(message.error))):p.resolve(message.result);}}
   else if(message.method==='Runtime.exceptionThrown')result.errors.push(message.params.exceptionDetails);
   else if(message.method==='Network.requestWillBeSent'&&message.params.request.method==='POST')result.posts.push(message.params.request.url);};
  await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1360,height:1080,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:base+'/raw-temporal.html'});
  await until('document.querySelectorAll("#source option").length>1');
  check('No implicit submission on page load',result.posts.length===0);
  check('Submit disabled without explicit opt-in',await evaluate('document.getElementById("submit").disabled'));
  await evaluate(`(async()=>{const items=await(await fetch('/api/v1/raw-temporal/sources')).json();
    const i=items.findIndex(x=>x.source_id===${JSON.stringify(marker.source_run_id)}&&x.readout_id==='single');
    if(i<0)throw new Error('Registered demo source missing');const el=document.getElementById('source');el.value=String(i);el.dispatchEvent(new Event('change'));})()`);
  check('Selecting source alone does not enable filtering',await evaluate('document.getElementById("submit").disabled'));
  await evaluate(`document.getElementById('name').value='RAW replay · 실제 Worker와 화면 연결';
    document.getElementById('acknowledge').checked=true;document.getElementById('acknowledge').dispatchEvent(new Event('change'));
    if(!document.getElementById('replay-form').checkValidity())throw new Error('Default form values violate HTML constraints');
    document.getElementById('replay-form').requestSubmit();`);
  await until('!document.getElementById("result").hidden && !!document.getElementById("saved").value');
  result.replay_id=await evaluate('document.getElementById("saved").value');
  check('Exactly one user-started replay submission',result.posts.length===1);
  check('Sixteen actual acquisition rows shown',await evaluate('document.querySelectorAll("#frames-table tr").length===16'));
  await evaluate(`document.getElementById('frame').value='8';document.getElementById('frame').dispatchEvent(new Event('input'));`);
  await until('["original-image","processed-image","mask-image"].every(id=>{const i=document.getElementById(id);return i.complete&&i.naturalWidth>0;})');
  check('Original processed and mask images loaded',true);
  check('Actual sample age and reset visible',await evaluate('document.getElementById("frame-info").textContent.includes("10.000 ms")'));
  check('Known false-positive warning visible',await evaluate('document.body.textContent.includes("3.79117%")'));
  const job=await(await fetch(`${base}/api/v1/raw-temporal/replays/${result.replay_id}`)).json();
  check('Separate worker completed replay',job.status==='succeeded');
  check('Selected source identity retained',job.result.summary.acquisition.source_id===marker.source_run_id);
  check('Late frames actually blend',job.result.summary.frames[8].blend_fraction>0);
  result.frame8=job.result.summary.frames[8];
  fs.writeFileSync(path.join(output,'replay-result.json'),JSON.stringify(job,null,2));
  fs.writeFileSync(path.join(output,'desktop.png'),Buffer.from((await call('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  check('Mobile page has no horizontal overflow',await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
  fs.writeFileSync(path.join(output,'mobile.png'),Buffer.from((await call('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await call('Page.navigate',{url:base+'/raw-temporal.html?replay='+result.replay_id});
  await until('!!document.getElementById("result") && !document.getElementById("result").hidden');
  check('Saved replay reopens without new job',result.posts.length===1);
  check('Reopened page does not precheck risk consent',await evaluate('!document.getElementById("acknowledge").checked'));
  check('No browser script errors',result.errors.length===0);
  result.status='passed';
 }catch(error){result.status='failed';result.error=String(error);process.exitCode=1;}
 finally{
  if(ws)ws.close();chrome.kill('SIGTERM');
  const exit=await Promise.race([exited,sleep(3000).then(()=>undefined)]);
  if(exit===undefined){chrome.kill('SIGKILL');await exited;}
  fs.rmSync(profile,{recursive:true,force:true});
  fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
 }
})();

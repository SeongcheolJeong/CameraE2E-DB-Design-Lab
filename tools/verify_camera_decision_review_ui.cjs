/* Saved-decision browser checks. No model/RAW calculation or write API. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(!process.argv[2]||!output.startsWith(root+path.sep)||fs.existsSync(output))throw new Error('Supply a NEW project-local output directory');
const base='http://127.0.0.1:8797',port=9337;
const executable=process.env.CAMERA_UI_BROWSER||path.join(os.homedir(),'Library/Caches/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const result={checks:[],errors:[],writes:[],requests:[],base_url:base,new_acquisitions:0};
const check=(name,value)=>{assert(value,name);result.checks.push(name);};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'camera-decision-browser-'));
fs.mkdirSync(output,{recursive:false});
const chrome=spawn(executable,['--headless','--remote-debugging-address=127.0.0.1','--remote-debugging-port='+port,
 '--no-first-run','--no-default-browser-check','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
const exited=new Promise(resolve=>{chrome.once('exit',resolve);chrome.once('error',error=>{result.errors.push(String(error));resolve(null);});});
let ws,sequence=0;const pending=new Map();
function call(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence;
 const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout '+method));},15000);
 pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});
 ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
 if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function until(expression){const deadline=Date.now()+20000;while(Date.now()<deadline){
 if(await evaluate(expression))return;const error=await evaluate('document.getElementById("error")?.textContent');
 if(error)throw new Error(error);await sleep(100);}throw new Error('UI timeout '+expression);}
async function select(id,value){await evaluate(`{const el=document.getElementById(${JSON.stringify(id)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('change'));}`);}
async function screenshot(name){fs.writeFileSync(path.join(output,name),Buffer.from((await call('Page.captureScreenshot',{format:'png'})).data,'base64'));}

(async()=>{
 try{
  let version;for(let i=0;i<50;i++){try{version=await(await fetch(`http://127.0.0.1:${port}/json/version`)).json();break;}catch{await sleep(100);}}
  if(!version)throw new Error('Installed Chromium did not start');result.browser=version.Browser;
  const target=await(await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}}
   else if(m.method==='Runtime.exceptionThrown')result.errors.push(m.params.exceptionDetails);
   else if(m.method==='Network.requestWillBeSent'){const r=m.params.request;if(r.url.startsWith(base)){result.requests.push({method:r.method,url:r.url});if(!['GET','HEAD'].includes(r.method))result.writes.push(r.url);}}};
  await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1080,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:base+'/decision-review.html'});
  await until('document.querySelectorAll("#cards [data-decision]").length===2 && !document.getElementById("loading").hidden===false');
  check('Both frozen choices visible',await evaluate('document.getElementById("cards").textContent.includes("radial-e4-g1")&&document.getElementById("cards").textContent.includes("clear-e2-g1")'));
  check('No RAW opened for initial metadata view',!result.requests.some(r=>r.url.includes('/cases/')));
  check('Signal limits and noise groups visible',await evaluate('document.getElementById("limits").textContent.includes("120")&&document.getElementById("groups").textContent.includes("12/12")'));
  await screenshot('signal-desktop.png');
  await select('split','stress');
  check('Signal stress keeps selected and baseline failures',await evaluate('document.getElementById("rows").textContent.includes("radial-e4-g1")&&document.getElementById("rows").textContent.includes("clear-e4-g1")&&document.getElementById("rows").textContent.includes("Well 포화")'));
  await evaluate('document.querySelector("#rows [data-open]").click()');
  await until('document.querySelectorAll("#raw-cards img").length===4 && [...document.querySelectorAll("#raw-cards img")].every(i=>i.complete&&i.naturalWidth===16)');
  check('Matched original RAW and final-code rails loaded at native size',true);
  check('RAW download retains NPY signature',await evaluate(`(async()=>{const a=document.querySelector('#raw-cards a');const r=await fetch(a.href);const b=new Uint8Array(await r.arrayBuffer());return r.ok&&b[0]===147&&String.fromCharCode(...b.slice(1,6))==='NUMPY';})()`));
  await evaluate('document.getElementById("raw-panel").scrollIntoView()');await screenshot('signal-stress-raw.png');
  await evaluate('document.querySelector("[data-profile=motion]").click()');
  check('Motion and reference-color limitations visible',await evaluate('document.getElementById("scope-notes").textContent.includes("정의상 0")&&document.getElementById("scope-notes").textContent.includes("잡음 OFF")'));
  check('Contrast inversion sign remains visible',await evaluate('document.getElementById("rows").textContent.includes("-21.221%")'));
  check('Charge and integer RAW contrasts are distinct columns',await evaluate('document.querySelector("thead").textContent.includes("광전하 대비")&&document.querySelector("thead").textContent.includes("정수 RAW 대비")'));
  await select('split','development');
  await select('scenario','dev-motion-1000');
  check('Same-input winner comparison shows 4ms failure and 2ms pass',await evaluate('document.querySelectorAll("#rows tr").length===2&&document.getElementById("rows").textContent.includes("63.662%")&&document.getElementById("rows").textContent.includes("90.032%")'));
  await evaluate('document.querySelector("#rows [data-open]").click()');
  await until('document.querySelectorAll("#raw-cards img").length===4 && [...document.querySelectorAll("#raw-cards img")].every(i=>i.complete&&i.naturalWidth===16)');
  await evaluate('document.getElementById("raw-panel").scrollIntoView()');await screenshot('motion-raw.png');
  await select('split','holdout');
  await evaluate('document.getElementById("failures").checked=true;document.getElementById("failures").dispatchEvent(new Event("change"))');
  check('Failure filter retains paired passing comparison',await evaluate('document.querySelectorAll("#rows tr").length===4&&document.getElementById("rows").textContent.includes("clear-e2-g1")'));
  const reopen=await evaluate('location.href');
  await call('Page.navigate',{url:reopen});await until('document.querySelector("[data-decision=motion].active")&&document.getElementById("split").value==="holdout"');
  check('Deep link reopens the same saved decision without submitting',true);
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate('window.scrollTo(0,0)');await sleep(100);
  check('Mobile layout has no page-wide overflow',await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
  await screenshot('mobile.png');
  check('Original decision JSON downloadable',await evaluate(`(async()=>{const a=[...document.querySelectorAll('#documents a')].find(x=>x.href.endsWith('/choice'));const r=await fetch(a.href);const d=await r.json();return d.selected_candidate_id==='clear-e2-g1'&&d.holdout_seen===false;})()`));
  check('No page writes or browser exceptions',result.writes.length===0&&result.errors.length===0);
  result.status='passed';
 }catch(error){result.status='failed';result.error=String(error);process.exitCode=1;}
 finally{if(ws)ws.close();chrome.kill('SIGTERM');if(await Promise.race([exited,sleep(3000).then(()=>undefined)])===undefined){chrome.kill('SIGKILL');await exited;}
  fs.rmSync(profile,{recursive:true,force:true});fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify({...result,requests:result.requests.length},null,2));}
})();

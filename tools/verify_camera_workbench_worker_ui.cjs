/* Mutating integration check restricted to the copied workbench QA state. */
'use strict';
const fs=require('node:fs'),assert=require('node:assert/strict');const {chromium}=require('playwright');
const base='http://127.0.0.1:8771',file='reports/camera_platform/workbench-worker-ui.json';
const report={schema:'camera_workbench_worker_ui_v1',checks:[],errors:[],posts:[],user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',e=>report.errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
 const get=async p=>{const r=await page.request.get(base+'/api/v1/'+p);assert(r.ok(),await r.text());return r.json();};
 const before=await get('workbench');assert(before.state_dir.endsWith('/camera-platform-workbench-qa'),'Never submit in primary state');
 await page.goto(base+'/#experiments');await page.locator('#run-exposure').fill('4.12');await page.locator('#run-seed').fill('43');
 const response=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/v1/runs');
 await page.locator('form[data-form="run"] button[type="submit"]').click();
 const submitted=await response;check('UI submits a real camera job',submitted.status()===202);const queued=await submitted.json();report.run_id=queued.id;
 await page.locator('[data-action="select-run"][data-id="'+queued.id+'"]').waitFor();
 await page.waitForFunction(id=>Array.from(document.querySelectorAll('#main pre')).some(e=>{try{const r=JSON.parse(e.textContent);return r.id===id&&r.status==='succeeded'&&r.result&&r.artifacts.length>0;}catch{return false;}}),queued.id,{timeout:120000});
 const completed=await get('runs/'+queued.id);check('Worker transition exposes full RAW/RGB evidence',completed.status==='succeeded'&&completed.artifacts.some(a=>a.name.includes('raw'))&&completed.artifacts.some(a=>a.media_type==='image/png'));
 const current=(await page.locator('#main pre').allTextContents()).map(s=>{try{return JSON.parse(s);}catch{return null;}}).find(r=>r?.id===queued.id);
 check('UI completed detail equals authoritative run',JSON.stringify(current)===JSON.stringify(completed));
 await page.reload();await page.locator('[data-action="select-run"][data-id="'+queued.id+'"]').waitFor();await page.locator('[data-action="select-run"][data-id="'+queued.id+'"]').click();
 await page.waitForFunction(id=>Array.from(document.querySelectorAll('#main pre')).some(e=>{try{return JSON.parse(e.textContent).id===id;}catch{return false;}}),queued.id);
 check('Completed result reopens after reload',true);check('Exactly one new run', (await get('workbench')).counts.runs===before.counts.runs+1);check('No browser errors',!report.errors.length);
 report.status='passed';report.verified_at=new Date().toISOString();
 }finally{await browser.close();fs.writeFileSync(file,JSON.stringify(report,null,2)+'\n');}console.log(JSON.stringify({status:report.status,checks:report.checks.length,run:report.run_id}));})().catch(e=>{console.error(e);process.exitCode=1;});

'use strict';
let token;
const status=document.querySelector('#status');
async function api(path,body){
 if(!token){const r=await fetch('/api/v1/session');token=(await r.json()).token;}
 const r=await fetch('/api/v1/road-hdr-studies'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Platform-Token':token},body:body?JSON.stringify(body):undefined});
 const value=await r.json();if(!r.ok)throw Error(JSON.stringify(value));return value;
}
function specification(){const payload={schema:'camera_road_hdr_job_v1',name:'도로 HDR 설계평가',package_path:document.querySelector('#package').value,specification_sha256:document.querySelector('#sha').value,action:document.querySelector('#action').value};const profile=document.querySelector('#profiles').value;if(payload.action==='reprocess'&&profile!=='all')payload.profiles=[profile];return payload;}
function bind(button,fn){document.querySelector(button).onclick=async()=>{try{status.textContent=JSON.stringify(await fn(),null,2);}catch(e){status.textContent=e.message;}};}
bind('#freeze',async()=>{const b=await api('/freeze',{package_path:document.querySelector('#package').value});document.querySelector('#sha').value=b.specification_sha256;return b;});
bind('#preflight',()=>api('/preflight',specification()));
bind('#submit',()=>api('',specification()));
bind('#refresh',async()=>{const jobs=await api('');const root=document.querySelector('#jobs');root.replaceChildren();for(const job of jobs){const p=document.createElement('p');p.textContent=job.name+' · '+job.specification.action+' · '+job.status+' ';if(job.result?.summary?.viewer_path){const a=document.createElement('a');a.href=job.result.summary.viewer_path;a.textContent='비교 결과 열기';p.append(a);}if(['queued','running'].includes(job.status)){const b=document.createElement('button');b.textContent='취소';b.onclick=()=>api('/'+job.id+'/cancel',{}).then(()=>document.querySelector('#refresh').click());p.append(b);}root.append(p);if(job.specification.package_path===document.querySelector('#package').value)document.querySelector('#sha').value=job.specification.specification_sha256;}return {jobs:jobs.length};});

document.querySelector('#refresh').click();

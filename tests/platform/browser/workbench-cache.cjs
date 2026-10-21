'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
vm.runInThisContext(fs.readFileSync('src/camera_platform/static/workbench.js','utf8'));
const hash=n=>String(n).padStart(64,'0');
function projection(id='a',h=1,kind='models',status){return {id,content_hash:'frozen',status,updated_at:'2026-01-01T00:00:00',_projection:{schema:'camera_workbench_projection_v1',collection:kind,record_sha256:hash(h),detail_path:'/workbench/'+kind+'/'+id}};}
function response(item,{h=1,status=item.status,updated_at=item.updated_at}={}){return {schema:'camera_workbench_record_v1',collection:item._projection.collection,record_sha256:hash(h),record:{id:item.id,content_hash:item.content_hash,status,updated_at,payload:{value:1}}};}
(async()=>{
 let count=0,release;const item=projection();
 const data=CameraWorkbenchData({api:async()=>{count++;return await new Promise(resolve=>{release=resolve;});}});
 const a=data.load(item),b=data.load(item);assert.equal(count,1);assert.equal(data.resolve(item),item);
 release(response(item));const [x,y]=await Promise.all([a,b]);assert.equal(x,y);assert.equal(data.resolve(item),x);assert.equal((await data.load(item)),x);assert.equal(count,1);
 const other=projection('a',2);assert.equal(data.resolve(other),other);
 let calls=0;const retry=CameraWorkbenchData({api:async()=>{if(++calls===1)throw Error('temporary');return response(item);}});
 await assert.rejects(retry.load(item),/temporary/);assert.equal(retry.resolve(item),item);await retry.load(item);assert.equal(calls,2);
 const immutable=CameraWorkbenchData({api:async()=>response(item,{h:2})});await assert.rejects(immutable.load(item),/버전/);assert.equal(immutable.resolve(item),item);
 const queued=projection('job',1,'runs','queued'),finished=response(queued,{h:2,status:'succeeded',updated_at:'2026-01-01T00:00:01'});
 const jobs=CameraWorkbenchData({api:async()=>finished});assert.equal((await jobs.load(queued)).status,'succeeded');
 const latest=projection('job',2,'runs','succeeded');assert.equal(jobs.resolve(latest).status,'succeeded');
 const stale=projection('advancing',1,'runs','queued');
 // Distinct new index hash forces a fresh authoritative completed detail.
 const terminal=projection('advancing',3,'runs','succeeded');terminal.updated_at='2026-01-01T00:00:02';
 const ordered=CameraWorkbenchData({api:async()=>response(terminal,{h:3,status:'succeeded',updated_at:terminal.updated_at})});
 await ordered.load(terminal);assert.equal(ordered.resolve(stale).status,'succeeded');
 const regression=CameraWorkbenchData({api:async()=>response(latest,{h:1,status:'running'})});await assert.rejects(regression.load(latest),/버전/);
 // ISP Study cells also advance while an index/detail request is in flight.
 for(const initial of ['queued','running']){
  const cell=projection('isp-cell-'+initial,1,'isp_study_cells',initial);
  const final=response(cell,{h:2,status:'succeeded',updated_at:'2026-01-01T00:00:01'});
  const cells=CameraWorkbenchData({api:async()=>final});
  assert.equal((await cells.load(cell)).status,'succeeded');
  assert.equal(cells.resolve(cell).status,'succeeded');
  const complete=projection(cell.id,2,'isp_study_cells','succeeded');complete.updated_at=final.record.updated_at;
  const backwards=CameraWorkbenchData({api:async()=>response(complete,{h:1,status:'running',updated_at:'2026-01-01T00:00:00'})});
  await assert.rejects(backwards.load(complete),/버전/);
 }
 const mismatch=CameraWorkbenchData({api:async()=>({...response(item),collection:'runs'})});await assert.rejects(mismatch.load(item),/일치/);
 let routed=false;const unsafe=CameraWorkbenchData({api:async()=>{routed=true;}});await assert.rejects(unsafe.load({...item,_projection:{...item._projection,detail_path:'https://example.com'}}),/참조/);assert.equal(routed,false);
 let pending={};const parallel=CameraWorkbenchData({api:path=>new Promise(resolve=>{pending[path]=resolve;})});
 const first=projection('first'),second=projection('second');const one=parallel.load(first),two=parallel.load(second);
 pending[second._projection.detail_path](response(second));await two;pending[first._projection.detail_path](response(first));await one;
 assert.equal(parallel.resolve(first).id,'first');assert.equal(parallel.resolve(second).id,'second');
 const limited=CameraWorkbenchData({capacity:2,api:async path=>response(projection(path.split('/').at(-1)))});
 await limited.load(first);await limited.load(second);await limited.load(projection('third'));assert.equal(limited.resolve(first),first);assert.equal(limited.resolve(second).id,'second');
 console.log('PASS: cache coalescing, identity, retry, worker progress, regression refusal, path binding, reordered replies, capacity');
})().catch(error=>{console.error(error);process.exitCode=1;});

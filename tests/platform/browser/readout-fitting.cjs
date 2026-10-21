'use strict';

// Run the actual module's registered DOM events. This checks UI state and HTTP
// contracts; it does not substitute for browser, Worker or physical-camera QA.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const {File}=require('node:buffer'),{webcrypto,createHash}=require('node:crypto');
const clone=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const bytes=x=>new TextEncoder().encode(x).buffer;
const file=(value,name='original.json')=>new File([value],name,{type:'application/json'});
const digest=value=>createHash('sha256').update(value).digest('hex');
const binding={request_sha256:'a'.repeat(64),result_sha256:'b'.repeat(64)};
const complete={id:'FIT /+?',name:'completed fit',status:'succeeded',result:{candidate:{slope:1.25}},plan_hash:'plan-1',content_hash:'c'.repeat(64)};
function context(handler,options={}) {
  const handlers=new Map(),calls=[],errors=[],notices=[],tasks=[],elements=new Map();let renders=0,refreshes=0;
  const document={addEventListener:(name,fn)=>{if(!handlers.has(name))handlers.set(name,[]);handlers.get(name).push(fn);},querySelector:q=>{if(!elements.has(q))elements.set(q,{disabled:false});return elements.get(q);}};
  const sandbox={window:{},document,TextEncoder,TextDecoder,Blob,FormData,crypto:options.crypto||webcrypto};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/camera_platform/static/readout-fitting.js','utf8'),sandbox);
  const entries={captures:[],readout_calibrations:[],readout_observations:[],readout_fits:[],...options.records};
  const ui=sandbox.window.CameraReadoutFittingUI({api:async(path,body)=>{calls.push({path,body});if(handler)return handler(path,body);if(path==='/readout-fits/plan')return {specification:clone(body),plan_hash:'plan-1'};if(path.endsWith('/export'))return {publication_binding:clone(binding)};if(path.startsWith('/readout-fits/')&&body===undefined)return clone(complete);return {id:'saved',name:'saved',status:'queued'};},esc,records:key=>entries[key]||[],render:()=>renders++,refresh:async args=>{refreshes++;return options.refresh?.(args);},toast:(message,error)=>notices.push({message,error}),busy:(_button,fn)=>{const task=Promise.resolve(fn()).catch(e=>errors.push(e));tasks.push(task);return task;},state:{view:'validation'},recordButton:(kind,r,label)=>`<button data-record-kind="${esc(kind)}" data-record-id="${esc(r.id)}">${esc(label)}</button>`,details:(label,value)=>`<details><summary>${esc(label)}</summary><pre>${esc(JSON.stringify(value))}</pre></details>`});
  const dispatch=async(name,target)=>{for(const fn of handlers.get(name)||[])await fn({target,preventDefault(){}});};
  const drain=async()=>{while(tasks.length)await tasks.shift();};
  return {ui,calls,errors,notices,entries,dispatch,drain,elements,renders:()=>renders,refreshes:()=>refreshes,
    input:(field,value)=>dispatch('input',{dataset:field==='metadata'?{readoutfitMetadata:''}:{readoutfitField:field},value}),
    change:(field,value)=>dispatch('change',{dataset:{readoutfitField:field},value}),
    check:(role,value,checked=true)=>dispatch('change',{dataset:{readoutfitObservation:role},value,checked}),
    choose:(key,value)=>dispatch('change',{dataset:{readoutfitFile:key},files:value?[value]:[]}),
    click:(action,id)=>dispatch('click',{closest:()=>({dataset:{readoutfitAction:action,id}})})};
}
const posted=c=>c.calls.filter(x=>x.body!==undefined);
const mutations=c=>posted(c).filter(x=>x.path!=='/readout-fits/plan');
const textValue=c=>c.ui.view().match(/<textarea[^>]*>(.*?)<\/textarea>/s)[1];
async function click(c,action,id){await c.click(action,id);await c.drain();}
async function readyImport(c,metadata='{"capture_id":"C","name":"authored"}') {await c.input('metadata',metadata);await c.choose('evidence',file('{"source":"independent anchor"}','evidence.json'));}
async function readyPlan(c) {await c.change('base','BASE');await c.check('training','TRAIN');await c.check('validation','HOLDOUT');await click(c,'plan');}

test('roles, original RAW choices and names are distinct and escaped',async()=>{
  const evil='<img src=x onerror="bad()">',c=context(null,{records:{captures:[{id:'C"<&',name:evil,schema:'camera_nonlinear_capture_v1'},{id:'old',name:'legacy hidden',schema:'camera_capture_v1'}],readout_calibrations:[{id:'BASE"<&',name:evil}],readout_observations:[{id:'TRAIN',name:evil,capture_id:'<capture>',source_kind:'<source>',dataset_role:'development'},{id:'HOLDOUT',name:'holdout name',capture_id:'C2',source_kind:'measured',dataset_role:'holdout'},{id:'BAD',name:'unqualified role',dataset_role:'unknown'}],readout_fits:[{id:'FIT"',name:evil,status:'<status>'}]}});
  await c.choose('evidence',file('{}',evil+'.json'));await c.input('metadata','{"name":"'+evil.replaceAll('"','\\"')+'"}');const html=c.ui.view();
  assert.doesNotMatch(html,/<img|legacy hidden/);assert.match(html,/&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);assert.match(html,/&lt;capture&gt;/);assert.match(html,/&lt;source&gt;/);
  const selected=[...html.matchAll(/data-readoutfit-observation="([^"]+)" value="([^"]+)"/g)].map(m=>[m[1],m[2]]);
  assert.deepEqual(selected,[['training','TRAIN'],['validation','HOLDOUT']]);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size);
  assert.doesNotMatch(html,/data-readoutfit-action="publish"/);assert.match(html,/step="1"/);assert.match(html,/min="10" max="200"/);
});

test('original metadata and evidence CRLF bytes and filenames are preserved',async()=>{
  const c=context(),metadata='{\r\n  "capture_id":"C /+?", "name":"원본"\r\n}\r\n',evidence='{\r\n "anchor": "원본 근거"\r\n}\r\n';
  await c.choose('metadata',file(metadata,'original-metadata.json'));await c.choose('evidence',file(evidence,'original-evidence.json'));await click(c,'import');
  assert.equal(c.errors.length,0);assert.equal(mutations(c).length,1);const post=mutations(c)[0];assert.equal(post.path,'/readout-observations/import');
  assert.deepEqual([...post.body.keys()],['metadata_file','evidence']);
  for(const [key,name,value] of [['metadata_file','original-metadata.json',metadata],['evidence','original-evidence.json',evidence]]){assert.equal(post.body.get(key).name,name);assert.deepEqual(Buffer.from(await post.body.get(key).arrayBuffer()),Buffer.from(value));}
  assert.match(c.ui.view(),/data-readoutfit-action="import" disabled/);
});

test('edited metadata uses exactly the edited text and a new descriptor filename',async()=>{
  const c=context(),edited='{\n "capture_id":"C", "name":"edited", "n":1.25\n}\n';await readyImport(c);await c.choose('metadata',file('{"name":"old"}','old.json'));await c.input('metadata',edited);await click(c,'import');
  const part=mutations(c)[0].body.get('metadata_file');assert.equal(part.name,'readout-fit-observation-metadata.json');assert.equal(await part.text(),edited);
});

for(const invalid of ['{"n":1e400}','{"n":-1e400}','{"n":9007199254740993}','{"n":-9007199254740993}','{"n":NaN}','[]','null'])test(`rejects malformed or lossy metadata before POST: ${invalid}`,async()=>{
  const c=context();await readyImport(c,invalid);await click(c,'import');assert.equal(mutations(c).length,0);assert.equal(c.errors.length,1);assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="reconcile"/);
});

test('safe integer boundaries and finite fractions preserve their original serialization',async()=>{
  const c=context(),metadata='{"max":9007199254740991,"min":-9007199254740991,"tiny":3e-12}';await readyImport(c,metadata);await click(c,'import');assert.equal(await mutations(c)[0].body.get('metadata_file').text(),metadata);
});

test('pending or invalid replacement metadata cannot submit the old valid draft',async()=>{
  const c=context(),wait=deferred();await readyImport(c,'{"name":"old draft"}');const pending=c.choose('metadata',{name:'new.json',size:20,arrayBuffer:()=>wait.promise});
  await click(c,'import');assert.equal(mutations(c).length,0);wait.resolve(bytes('{broken'));await pending;await click(c,'import');assert.equal(mutations(c).length,0);assert.doesNotMatch(textValue(c),/old draft/);assert.ok(c.notices.some(n=>n.error));
});

test('late metadata reads cannot overwrite manual edits or newer files',async()=>{
  const c=context(),first=deferred();const pending=c.choose('metadata',{name:'old.json',size:20,arrayBuffer:()=>first.promise});await c.input('metadata','{"name":"manual"}');first.resolve(bytes('{"name":"old"}'));await pending;assert.match(textValue(c),/manual/);
  const second=deferred(),pending2=c.choose('metadata',{name:'old2.json',size:20,arrayBuffer:()=>second.promise});await c.choose('metadata',file('{"name":"new file"}'));second.resolve(bytes('{"name":"older file"}'));await pending2;assert.match(textValue(c),/new file/);assert.doesNotMatch(textValue(c),/older file/);
});

test('later template selection supersedes an earlier still-pending file read',async()=>{
  const c=context(()=>({name:'new template',capture_id:'C'})),wait=deferred();await c.change('capture','C');const pending=c.choose('metadata',{name:'stale.json',size:20,arrayBuffer:()=>wait.promise});await click(c,'template');
  wait.resolve(bytes('{"name":"stale file"}'));await pending;assert.match(textValue(c),/new template/);assert.doesNotMatch(textValue(c),/stale file/);
});

test('choosing independent evidence does not discard an already-pending metadata file',async()=>{
  const c=context(),wait=deferred(),original='{"capture_id":"C","name":"both selected originals"}';
  const pending=c.choose('metadata',{name:'metadata.json',size:original.length,arrayBuffer:()=>wait.promise});await c.choose('evidence',file('{"anchor":"evidence"}'));
  wait.resolve(bytes(original));await pending;assert.equal(textValue(c),esc(original));assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="import" disabled/);
});

test('invalid UTF-8 and BOM files leave no previous importable draft',async()=>{
  for(const data of [new Uint8Array([0xff,0xfe]),new Uint8Array([0xef,0xbb,0xbf,0x7b,0x7d])]){const c=context();await readyImport(c);await c.choose('metadata',file(data));await click(c,'import');assert.equal(mutations(c).length,0);assert.ok(c.notices.some(n=>n.error));}
});

test('metadata and evidence bounds reject before reads or submission',async()=>{
  const c=context();let reads=0;await readyImport(c);await c.choose('metadata',{name:'large.json',size:1048577,arrayBuffer:()=>{reads++;throw Error('must not read');}});await click(c,'import');assert.equal(reads,0);assert.equal(mutations(c).length,0);
  await readyImport(c);await c.choose('evidence',{name:'large.json',size:2097153,arrayBuffer:()=>{reads++;throw Error('must not read');}});await click(c,'import');assert.equal(reads,0);assert.equal(mutations(c).length,0);
  await readyImport(c,'{"n":"'+'é'.repeat(524289)+'"}');await click(c,'import');assert.equal(mutations(c).length,0);
});

test('successful file selection aligns capture control without rewriting original metadata',async()=>{
  const c=context(null,{records:{captures:[{id:'OLD',name:'old',schema:'camera_nonlinear_capture_v1'},{id:'NEW',name:'new',schema:'camera_nonlinear_capture_v1'}]}});await c.change('capture','OLD');const original='{\r\n"capture_id":"NEW"\r\n}';await c.choose('metadata',file(original));assert.match(c.ui.view(),/<option value="NEW" selected/);assert.equal(textValue(c),esc(original));
});

test('template encodes original capture ID and keeps empty charge placeholders',async()=>{
  const c=context(()=>({capture_id:'C /+?',frames:[{frame_id:'f',mean_total_charge_e:[null,null,null,null]}]}));await c.change('capture','C /+?');await click(c,'template');assert.equal(c.calls[0].path,'/templates/readout-observation?capture_id=C%20%2F%2B%3F');assert.match(textValue(c),/null/);assert.equal(mutations(c).length,0);
});

test('pending template request locks edits and additional actions until it resolves',async()=>{
  const wait=deferred(),c=context(()=>wait.promise);await c.change('capture','C');await c.click('template');await c.input('metadata','{"name":"blocked"}');await c.change('capture','OTHER');await c.click('template');assert.equal(c.calls.length,1);assert.match(c.ui.view(),/<fieldset disabled/);wait.resolve({name:'template'});await c.drain();assert.match(textValue(c),/template/);assert.doesNotMatch(textValue(c),/blocked/);
});

test('plan includes explicit bounds and unique role selections; submit pins server specification',async()=>{
  const c=context((path,body)=>path==='/readout-fits/plan'?{plan_hash:'server-plan',specification:{...clone(body),name:'server-normalized'}}:{id:'RUN',status:'queued'});await c.change('base','BASE');await c.check('training','TRAIN');await c.check('training','TRAIN');await c.check('validation','VAL');await c.check('validation','removed');await c.check('validation','removed',false);
  for(const [key,value] of [['slopeLow','0.01'],['slopeHigh','100'],['sigmaLow','1e-9'],['sigmaHigh','1'],['iterations','200']])await c.input(key,value);
  await click(c,'plan');const spec=posted(c)[0].body;assert.deepEqual(clone(spec),{schema:'camera_readout_fit_request_v1',name:'응답·읽기 잡음 추정',base_calibration_id:'BASE',training_observation_ids:['TRAIN'],validation_observation_ids:['VAL'],slope_scale_bounds:[.01,100],sigma_v_bounds:[1e-9,1],max_iterations:200});
  await click(c,'submit');assert.equal(mutations(c).length,1);assert.equal(mutations(c)[0].path,'/readout-fits');assert.equal(mutations(c)[0].body.plan_hash,'server-plan');assert.equal(mutations(c)[0].body.specification.name,'server-normalized');
});

for(const key of ['base','name','slopeLow','slopeHigh','sigmaLow','sigmaHigh','iterations','training','validation'])test(`changing ${key} invalidates a checked fit plan`,async()=>{
  const c=context();await readyPlan(c);assert.match(c.ui.view(),/data-readoutfit-action="submit"/);if(key==='base')await c.change(key,'NEW');else if(['training','validation'].includes(key))await c.check(key,'NEW');else await c.input(key,'1.1');
  assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="submit"/);await click(c,'submit');assert.equal(mutations(c).length,0);assert.match(c.errors.at(-1).message,/계획을 다시/);
});

test('a rejected replan never leaves the previous successful plan submittable',async()=>{
  let attempt=0;const c=context((path,body)=>{if(path==='/readout-fits/plan'){if(++attempt===2)throw Object.assign(Error('invalid bounds'),{status:422});return {specification:clone(body),plan_hash:'p'};}return {};});await readyPlan(c);await click(c,'plan');assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="submit"/);await click(c,'submit');assert.equal(mutations(c).length,0);
});

test('candidate publication is explicit and uses both server-exported identity hashes',async()=>{
  const c=context();await click(c,'load',complete.id);assert.equal(mutations(c).length,0);assert.deepEqual(c.calls.map(r=>r.path),['/readout-fits/FIT%20%2F%2B%3F','/readout-fits/FIT%20%2F%2B%3F/export']);assert.match(c.ui.view(),/data-readoutfit-action="publish"/);
  await c.input('publishName','approved new version');await c.input('revision','r2');await click(c,'publish');const post=mutations(c)[0];assert.equal(post.path,'/readout-fits/FIT%20%2F%2B%3F/publish');assert.deepEqual(clone(post.body),{name:'approved new version',revision:'r2',...binding});assert.notEqual(post.body.request_sha256,complete.content_hash);await click(c,'publish');assert.equal(mutations(c).length,1);
});

test('queued, failed or missing-export jobs cannot publish or reuse an earlier binding',async()=>{
  let mode='complete';const c=context(path=>path.endsWith('/export')?(mode==='missing'?Promise.reject(Error('manifest unavailable')):{publication_binding:binding}):({...complete,status:mode==='complete'||mode==='missing'?'succeeded':mode}));
  await click(c,'load',complete.id);assert.match(c.ui.view(),/data-readoutfit-action="publish"/);
  for(mode of ['queued','failed','missing']){await click(c,'load',complete.id);assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="publish"/);await click(c,'publish');assert.equal(mutations(c).length,0);}
});

for(const kind of ['import','submit','publish'])test(`${kind} records acknowledged success before refresh failure and prevents repeated POST`,async()=>{
  const c=context(null,{refresh:async()=>{throw Error('refresh unavailable');}});if(kind==='import')await readyImport(c);else if(kind==='submit')await readyPlan(c);else await click(c,'load',complete.id);
  await click(c,kind);await click(c,kind);assert.equal(mutations(c).length,1);assert.equal(c.refreshes(),1);assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="reconcile"/);assert.ok(c.errors.some(e=>e.message==='refresh unavailable'));
});

for(const kind of ['import','submit','publish'])test(`${kind} uncertain POST is reconciled by exact immutable identity without another POST`,async()=>{
  let saved,match='exact';const c=context(async(path,body)=>{
    if(path==='/readout-fits/plan')return {specification:clone(body),plan_hash:'checked-plan'};
    if(body){if(kind==='import')saved={id:'OBS',artifacts:await Promise.all([['metadata_file','readout-fit-observation-metadata.json'],['evidence','readout-fit-observation-evidence.json']].map(async([key,name])=>({name,sha256:digest(Buffer.from(await body.get(key).arrayBuffer()))})))};else if(kind==='submit')saved={...complete,id:'SUBMITTED',status:'queued',plan_hash:body.plan_hash};else saved={id:'PUBLISHED',fit_job_id:complete.id,...clone(body)};throw Error('response lost after commit');}
    if(path===({import:'/readout-observations',submit:'/readout-fits',publish:'/readout-calibrations'}[kind])){
      if(match==='none')return [];if(match==='many')return [saved,{...saved,id:'SECOND'}];if(match==='wrong')return [{...saved,...(kind==='import'?{artifacts:saved.artifacts.map((a,i)=>i?{...a,name:'unrelated.json'}:a)}:kind==='submit'?{plan_hash:'different-plan'}:{result_sha256:'f'.repeat(64)})}];return [saved];}
    if(path.endsWith('/export'))return {publication_binding:binding};return path.endsWith('/SUBMITTED')?saved:complete;
  });
  if(kind==='import')await readyImport(c);else if(kind==='submit')await readyPlan(c);else await click(c,'load',complete.id);await click(c,kind);assert.equal(mutations(c).length,1);assert.match(c.ui.view(),/data-readoutfit-action="reconcile"/);
  await c.input('name','blocked name');await click(c,kind);await click(c,'template');assert.equal(mutations(c).length,1);assert.doesNotMatch(c.ui.view(),/blocked name/);
  for(match of ['none','many','wrong']){await click(c,'reconcile');assert.match(c.ui.view(),/data-readoutfit-action="reconcile"/);await click(c,kind);assert.equal(mutations(c).length,1);}
  match='exact';await click(c,'reconcile');assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="reconcile"/);assert.equal(mutations(c).length,1);
});

test('definitive 4xx import leaves original draft editable for a corrected request',async()=>{
  const c=context((_path,body)=>{if(body&&mutations(c).length===1)throw Object.assign(Error('invalid observation'),{status:422});return {id:'OBS'};});await readyImport(c);await click(c,'import');assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="reconcile"/);await c.input('metadata','{"name":"corrected"}');await click(c,'import');assert.equal(mutations(c).length,2);assert.equal(await mutations(c)[1].body.get('metadata_file').text(),'{"name":"corrected"}');
});

test('confirmed submitted identity remains recoverable if subsequent job read fails',async()=>{
  const c=context((path,body)=>{if(path==='/readout-fits/plan')return {specification:clone(body),plan_hash:'checked-plan'};if(body)throw Error('lost response');if(path==='/readout-fits')return [{id:'CONFIRMED',name:'confirmed fit',status:'queued',plan_hash:'checked-plan'}];throw Error('job read unavailable');});
  await readyPlan(c);await click(c,'submit');await click(c,'reconcile');assert.equal(mutations(c).length,1);
  const html=c.ui.view();assert.ok(html.includes('data-readoutfit-action="reconcile"')||html.includes('data-id="CONFIRMED"'),'The confirmed job must retain a retry/read control after GET failure');
});

test('a hashing failure before POST does not create uncertain state',async()=>{
  const c=context(null,{crypto:{subtle:{digest:async()=>{throw Error('digest unavailable');}}}});await readyImport(c);await click(c,'import');assert.equal(mutations(c).length,0);assert.doesNotMatch(c.ui.view(),/data-readoutfit-action="reconcile"/);await c.input('metadata','{"name":"editable"}');assert.match(textValue(c),/editable/);
});

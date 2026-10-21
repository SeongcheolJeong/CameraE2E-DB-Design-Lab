'use strict';

// Execute the actual UI modules and registered event handlers with local stubs.
// These are event/state contracts, not rendered-browser or physical-camera proof.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const {File}=require('node:buffer'),{webcrypto,createHash}=require('node:crypto');
const clone=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const file=(value,name='metadata.json')=>new File([value],name,{type:name.endsWith('.json')?'application/json':'application/octet-stream'});
const bytes=text=>new TextEncoder().encode(text).buffer;
const digest=value=>createHash('sha256').update(value).digest('hex');
function context(handler,options={}) {
  const handlers=new Map(),calls=[],errors=[],notices=[],tasks=[],replays=[],summaries=new Map();let renders=0,refreshes=0;
  const forms=Object.fromEntries(['capture','calibration'].map(role=>[role,{dataset:{nonlinearForm:role},button:{disabled:false},querySelector(){return this.button;}}]));
  const document={addEventListener:(name,fn)=>{if(!handlers.has(name))handlers.set(name,[]);handlers.get(name).push(fn);},querySelector:query=>{if(!summaries.has(query))summaries.set(query,{textContent:''});return summaries.get(query);}};
  const sandbox={window:{},document,TextEncoder,TextDecoder,Blob,FormData,crypto:options.crypto||webcrypto};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/camera_platform/static/nonlinear-captures.js','utf8'),sandbox);
  const entries={assemblies:[],tests:[],models:[],captures:[],readout_calibrations:[],...options.records};
  const ui=sandbox.window.CameraNonlinearCaptureUI({api:async(path,body)=>{calls.push({path,body});return handler?handler(path,body):body?{id:'saved',name:'saved',artifacts:[]}:{};},esc,records:key=>entries[key]||[],render:()=>renders++,refresh:async args=>{refreshes++;return options.refresh?.(args);},toast:(message,error)=>notices.push({message,error}),busy:(_button,fn)=>{const work=Promise.resolve(fn()).catch(error=>{errors.push(error);});tasks.push(work);return work;},state:{view:'validation'},recordButton:(kind,record,label)=>`<button data-record-kind="${esc(kind)}" data-record-id="${esc(record.id)}">${esc(label)}</button>`,onReplay:async(kind,id)=>replays.push({kind,id})});
  const dispatch=async(name,target)=>{for(const fn of handlers.get(name)||[])await fn({target,preventDefault(){}});};
  const drain=async()=>{while(tasks.length)await tasks.shift();};
  return {ui,calls,errors,notices,replays,entries,forms,dispatch,drain,summaries,renders:()=>renders,refreshes:()=>refreshes,
    input:(role,value)=>dispatch('input',{dataset:{nonlinearMetadata:role},value,closest:()=>forms[role]}),
    change:(field,value)=>dispatch('change',{dataset:{nonlinearField:field},value}),
    choose:(role,key,value)=>dispatch('change',{dataset:{nonlinearFile:key,role},files:value?[value]:[]}),
    click:(action,role='capture',id)=>dispatch('click',{closest:()=>({dataset:{nonlinearAction:action,role,id}})}),
    submit:role=>dispatch('submit',{closest:()=>forms[role]})};
}
async function ready(c,role='capture',metadata='{"name":"authored","source_kind":"estimated","frames":[]}') {
  await c.input(role,metadata);
  for(const key of role==='capture'?['raw']:['dark_rate','evidence'])await c.choose(role,key,file(key+' original bytes',key+(key==='evidence'?'.json':'.npy')));
}
const posted=c=>c.calls.filter(x=>x.body!==undefined);
const formHtml=(c,role)=>c.ui.view().match(new RegExp(`<form data-nonlinear-form="${role}">(.*?)</form>`,'s'))[1];
const textValue=(c,role)=>formHtml(c,role).match(/<textarea[^>]*>(.*?)<\/textarea>/s)[1];
async function submit(c,role='capture'){await c.submit(role);await c.drain();}
async function click(c,action,role='capture',id){await c.click(action,role,id);await c.drain();}

test('acquisition and response forms have distinct files, labels, IDs and escaped record/file names',async()=>{
  const unsafe='<img src=x onerror="bad()">',c=context(null,{records:{assemblies:[{id:'A"<&',name:unsafe}],tests:[{id:'T',name:unsafe}],models:[{id:'S',name:unsafe,kind:'sensor'},{id:'ISP',name:'hidden ISP',kind:'isp'}],captures:[{id:'C"<&',name:unsafe,schema:'camera_nonlinear_capture_v1',source_kind:'estimated',raw_shape:[4,8,8]},{id:'old',name:'legacy hidden',schema:'camera_capture_v1'}],readout_calibrations:[{id:'D',name:unsafe,revision:'<1>',charge_reference:{kind:'<authored>'}}]}});
  await c.choose('capture','raw',file('RAW',unsafe+'.npy'));const html=c.ui.view(),capture=formHtml(c,'capture'),cal=formHtml(c,'calibration');
  assert.match(capture,/data-nonlinear-file="raw"/);assert.doesNotMatch(capture,/data-nonlinear-file="dark_rate"|data-nonlinear-file="evidence"|readout_calibration_id/);
  assert.match(cal,/data-nonlinear-file="dark_rate"/);assert.match(cal,/data-nonlinear-file="evidence"/);assert.doesNotMatch(cal,/data-nonlinear-file="raw"/);
  assert.match(html,/&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);assert.doesNotMatch(html,/<img|legacy hidden|hidden ISP/);
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(x=>x[1]);assert.equal(ids.length,new Set(ids).size);
});

for(const role of ['capture','calibration'])test(`${role} submits the exact imported JSON CRLF bytes without normalization`,async()=>{
  const c=context(),original='{\r\n  "name": "원본",\r\n  "source_kind": "estimated",\r\n  "frames": []\r\n}\r\n';
  await ready(c,role);await c.choose(role,'metadata',file(original,'original-'+role+'.json'));await submit(c,role);
  assert.equal(c.errors.length,0);assert.equal(posted(c).length,1);const request=posted(c)[0];
  assert.equal(request.path,role==='capture'?'/captures/nonlinear/import':'/readout-calibrations/import');
  assert.deepEqual([...request.body.keys()].sort(),(role==='capture'?['raw','metadata_file']:['dark_rate','evidence','metadata_file']).sort());
  const originalPart=request.body.get('metadata_file');assert.equal(originalPart.name,'original-'+role+'.json');
  assert.deepEqual(Buffer.from(await originalPart.arrayBuffer()),Buffer.from(original));assert.match(formHtml(c,role),/type="submit"[^>]*disabled/);
});

test('edited JSON is preserved verbatim as new metadata and never silently replaced by the original file',async()=>{
  const c=context(),original='{\r\n "name":"old"\r\n}',edited='{\n "name": "edited", "arbitrary_preserved": 1.25\n}\n';
  await ready(c);await c.choose('capture','metadata',file(original,'old.json'));await c.input('capture',edited);await submit(c);
  const part=posted(c)[0].body.get('metadata_file');assert.equal(part.name,'nonlinear-capture-metadata.json');assert.equal(await part.text(),edited);assert.notEqual(await part.text(),original);
});

for(const invalid of ['{"n":1e400}','{"n":-1e400}','{"n":9007199254740993}','{"n":-9007199254740993}','{"n":NaN}','[]','null'])test(`malformed or lossy metadata is rejected before any POST: ${invalid}`,async()=>{
  const c=context();await ready(c,'capture',invalid);await submit(c);assert.equal(posted(c).length,0);assert.equal(c.errors.length,1);
});

test('safe integer boundary and finite fractional numbers retain their original text',async()=>{
  const c=context(),original='{"n":9007199254740991,"f":0.000000003,"negative":-9007199254740991}';
  await ready(c,'capture',original);await submit(c);assert.equal(await posted(c)[0].body.get('metadata_file').text(),original);
});

test('a newly selected pending or invalid metadata file cannot submit the previous valid draft',async()=>{
  const wait=deferred(),c=context();await ready(c,'capture','{"name":"stale metadata"}');
  const request=c.choose('capture','metadata',{name:'new.json',size:10,arrayBuffer:()=>wait.promise});
  await submit(c);assert.equal(posted(c).length,0,'new metadata is pending, so the previous text must not be used');
  wait.resolve(bytes('{invalid'));await request;await submit(c);assert.equal(posted(c).length,0,'invalid replacement must not revive old metadata');
  assert.doesNotMatch(textValue(c,'capture'),/stale metadata/);assert.ok(c.notices.some(x=>x.error));
});

test('invalid UTF-8 and BOM-prefixed input are rejected without posting a prior draft',async()=>{
  for(const invalid of [new Uint8Array([0xff,0xfe,0x7b,0x7d]),new Uint8Array([0xef,0xbb,0xbf,0x7b,0x7d])]) {
    const c=context();await ready(c);await c.choose('capture','metadata',file(invalid));await submit(c);
    assert.equal(posted(c).length,0);assert.ok(c.notices.some(x=>x.error));
  }
});

test('metadata and binary file limits reject before reads or network submission',async()=>{
  const c=context();let reads=0;await ready(c);
  await c.choose('capture','metadata',{name:'large.json',size:1048577,arrayBuffer:()=>{reads++;throw Error('must not read');}});
  await submit(c);assert.equal(reads,0);assert.equal(posted(c).length,0);
  await ready(c);await c.choose('capture','raw',{name:'large.npy',size:67108865});await submit(c);assert.equal(posted(c).length,0);
  await ready(c,'calibration');await c.choose('calibration','evidence',{name:'large.json',size:2097153});await submit(c,'calibration');assert.equal(posted(c).length,0);
});

test('a late file read cannot overwrite manual edits or a newer metadata file',async()=>{
  const c=context(),wait=deferred();
  const first=c.choose('capture','metadata',{name:'old.json',size:20,arrayBuffer:()=>wait.promise});
  await c.input('capture','{"name":"manual newer"}');wait.resolve(bytes('{"name":"old file"}'));await first;
  assert.match(textValue(c,'capture'),/manual newer/);assert.doesNotMatch(textValue(c,'capture'),/old file/);
  const next=deferred(),second=c.choose('capture','metadata',{name:'older.json',size:20,arrayBuffer:()=>next.promise});
  await c.choose('capture','metadata',file('{"name":"new file"}'));next.resolve(bytes('{"name":"older file"}'));await second;
  assert.match(textValue(c,'capture'),/new file/);assert.doesNotMatch(textValue(c,'capture'),/older file/);
});

test('late templates cannot overwrite edits or changed acquisition/template bindings',async()=>{
  for(const replacement of ['edit','field']) {
    const wait=deferred(),c=context(()=>wait.promise);await ready(c,'capture','{"name":"before"}');
    await c.click('template','capture');
    if(replacement==='edit')await c.input('capture','{"name":"newer edit"}');else await c.change('assembly','different-assembly');
    wait.resolve({name:'stale template'});await c.drain();assert.doesNotMatch(textValue(c,'capture'),/stale template/);
    if(replacement==='edit')assert.match(textValue(c,'capture'),/newer edit/);
  }
});

test('template requests bind separate assembly/test versus sensor fields and do not rewrite other drafts',async()=>{
  const c=context(path=>({name:path.startsWith('/templates/nonlinear-capture')?'capture example':'calibration example'}));
  await c.change('assembly','A /&');await c.change('test','T +?');await c.change('sensor','S =#');await c.change('mode','shared_charge_paths');
  await c.input('calibration','{"name":"unsaved calibration"}');await click(c,'template');assert.match(textValue(c,'calibration'),/unsaved calibration/);
  await click(c,'template','calibration');assert.match(textValue(c,'capture'),/capture example/);
  assert.equal(c.calls[0].path,'/templates/nonlinear-capture?assembly_id=A%20%2F%26&test_id=T%20%2B%3F&readout_mode=shared_charge_paths');
  assert.equal(c.calls[1].path,'/templates/readout-calibration?sensor_model_id=S%20%3D%23&mode=shared_charge_paths');
  assert.equal(posted(c).length,0);
});

test('successful metadata files align visible template source controls while pending, invalid and typed drafts do not',async()=>{
  const c=context(null,{records:{assemblies:[{id:'A-old',name:'old camera'},{id:'A-file',name:'file camera'}],tests:[{id:'T-old',name:'old test'},{id:'T-file',name:'file test'}],models:[{id:'S-old',name:'old sensor',kind:'sensor'},{id:'S-file',name:'file sensor',kind:'sensor'}]}});
  const selected=field=>c.ui.view().match(new RegExp(`<select[^>]*data-nonlinear-field="${field}"[^>]*>(.*?)</select>`,'s'))[1].match(/<option value="([^"]*)" selected/)[1];
  await c.change('assembly','A-old');await c.change('test','T-old');await c.change('sensor','S-old');
  const original='{\r\n "assembly_id":"A-file", "test_id":"T-file", "readout_mode":"shared_charge_paths"\r\n}';
  const wait=deferred(),read=c.choose('capture','metadata',{name:'pending.json',size:original.length,arrayBuffer:()=>wait.promise});
  assert.equal(selected('assembly'),'A-old');assert.equal(selected('test'),'T-old');assert.equal(selected('mode'),'sequential_frames');
  wait.resolve(bytes(original));await read;
  assert.equal(selected('assembly'),'A-file');assert.equal(selected('test'),'T-file');assert.equal(selected('mode'),'shared_charge_paths');assert.equal(selected('sensor'),'S-old');
  assert.equal(textValue(c,'capture'),esc(original),'source-control synchronization must not normalize the imported metadata');
  await c.choose('calibration','metadata',file('{"sensor_model_id":"S-file","readout_mode":"sequential_frames"}'));
  assert.equal(selected('sensor'),'S-file');assert.equal(selected('mode'),'sequential_frames');assert.equal(selected('assembly'),'A-file');assert.equal(selected('test'),'T-file');
  await c.choose('capture','metadata',file('{"assembly_id":"A-old",'));await c.input('capture','{"assembly_id":"A-old","test_id":"T-old","readout_mode":"shared_charge_paths"}');
  await c.input('calibration','{"sensor_model_id":"S-old",');
  assert.equal(selected('assembly'),'A-file');assert.equal(selected('test'),'T-file');assert.equal(selected('sensor'),'S-file');assert.equal(selected('mode'),'sequential_frames');assert.equal(c.calls.length,0);
});

for(const role of ['capture','calibration'])test(`${role} success is recorded before a failed refresh and cannot be submitted twice`,async()=>{
  const c=context(null,{refresh:async()=>{throw Error('offline refresh');}});await ready(c,role);await submit(c,role);await submit(c,role);
  assert.equal(posted(c).length,1);assert.equal(c.refreshes(),1);assert.match(formHtml(c,role),/type="submit"[^>]*disabled/);
  assert.doesNotMatch(c.ui.view(),/data-nonlinear-action="reconcile"/);assert.equal(c.errors[0].message,'offline refresh');
});

test('uncertain capture POST locks both drafts and reconciles exact named source bytes without a second POST',async()=>{
  let saved;const c=context(async(path,body)=>{
    if(body){saved={id:'C',artifacts:await Promise.all([['metadata_file','acquisition-metadata.json'],['raw','raw-capture.npy']].map(async([key,name])=>({name,sha256:digest(Buffer.from(await body.get(key).arrayBuffer()))})))};throw Error('response lost after commit');}
    assert.equal(path,'/captures');return [saved];
  });
  await ready(c);await ready(c,'calibration','{"name":"keep calibration"}');await submit(c);assert.match(c.ui.view(),/data-nonlinear-action="reconcile"/);
  await c.input('capture','{"name":"blocked edit"}');await c.input('calibration','{"name":"blocked other edit"}');
  await submit(c);await submit(c,'calibration');await click(c,'template','calibration');assert.equal(posted(c).length,1);
  assert.doesNotMatch(c.ui.view(),/blocked edit|blocked other edit/);await click(c,'reconcile');
  assert.equal(posted(c).length,1);assert.doesNotMatch(c.ui.view(),/data-nonlinear-action="reconcile"/);
  assert.match(textValue(c,'calibration'),/keep calibration/);assert.doesNotMatch(formHtml(c,'calibration'),/type="submit"[^>]*disabled/);
});

test('uncertain calibration recovery checks all three original hashes and descriptor names',async()=>{
  let exact,mode='wrong name';const c=context(async(path,body)=>{
    if(body){exact={id:'CAL',artifacts:await Promise.all([['metadata_file','readout-calibration-metadata.json'],['dark_rate','expected-dark-rate.npy'],['evidence','readout-evidence.json']].map(async([key,name])=>({name,sha256:digest(Buffer.from(await body.get(key).arrayBuffer()))})))};throw Error('unknown POST outcome');}
    assert.equal(path,'/readout-calibrations');if(mode==='none')return [];
    if(mode==='ambiguous')return [exact,{...exact,id:'another'}];
    if(mode==='wrong name')return [{...exact,artifacts:exact.artifacts.map((a,i)=>i===2?{...a,name:'unrelated.json'}:a)}];
    if(mode==='wrong hash')return [{...exact,artifacts:exact.artifacts.map((a,i)=>i===1?{...a,sha256:'0'.repeat(64)}:a)}];return [exact];
  });
  await ready(c,'calibration');await submit(c,'calibration');
  for(mode of ['wrong name','wrong hash','none','ambiguous']) {await click(c,'reconcile','calibration');assert.match(c.ui.view(),/data-nonlinear-action="reconcile"/);await submit(c,'calibration');assert.equal(posted(c).length,1);}
  mode='exact';await click(c,'reconcile','calibration');assert.doesNotMatch(c.ui.view(),/data-nonlinear-action="reconcile"/);assert.equal(posted(c).length,1);
});

test('definitive 4xx rejection retains editable originals and permits a corrected request',async()=>{
  const c=context(async(_path,body)=>{if(body&&posted(c).length===1){const error=Error('invalid acquisition');error.status=422;throw error;}return {id:'C'};});
  await ready(c);await submit(c);assert.doesNotMatch(c.ui.view(),/data-nonlinear-action="reconcile"/);
  await c.input('capture','{"name":"corrected"}');await submit(c);assert.equal(posted(c).length,2);assert.equal(await posted(c)[1].body.get('metadata_file').text(),'{"name":"corrected"}');
});

test('SHA preparation failure is not treated as an uncertain mutation',async()=>{
  const c=context(null,{crypto:{subtle:{digest:async()=>{throw Error('hash failed');}}}});await ready(c);await submit(c);
  assert.equal(posted(c).length,0);assert.doesNotMatch(c.ui.view(),/data-nonlinear-action="reconcile"/);await c.input('capture','{"name":"still editable"}');assert.match(textValue(c,'capture'),/still editable/);
});

test('capture replay action delegates original capture identity without guessing a response calibration',async()=>{
  const c=context();await click(c,'replay','capture','C-original');assert.deepEqual(c.replays,[{kind:'capture',id:'C-original'}]);assert.equal(c.calls.length,0);
});

test('actual saved-frame selector explicitly labels absent timestamps and preserves zero nanoseconds',()=>{
  const source=fs.readFileSync('src/camera_platform/static/app.js','utf8'),begin=source.indexOf('  function frameBrowser(job,replay=false) {'),end=source.indexOf('  function replayAcquisition(source) {',begin);
  assert.ok(begin>=0&&end>begin);
  const frames=[{frame_id:'browser-dcg-0',frame_start_ns:null},{frame_id:'unknown-without-key'},{frame_id:'origin',frame_start_ns:0},{frame_id:'later',frame_start_ns:12345}],state={frameSelections:{}};
  const sandbox={state,resultFrames:()=>frames,runSummary:()=>({}),array:x=>Array.isArray(x)?x:[],safeArtifactUrl:()=>'',esc,empty:()=>'',number:String,findRecord:()=>null,imagePreviews:()=>'',details:()=>''};
  vm.createContext(sandbox);vm.runInContext(source.slice(begin,end),sandbox);
  const html=sandbox.frameBrowser({id:'captured-replay',artifacts:[]},true);
  const labels=[...html.matchAll(/<option[^>]*>(.*?)<\/option>/g)].map(x=>x[1]);
  assert.deepEqual(labels,['Frame browser-dcg-0 · 시각 미제공','Frame unknown-without-key · 시각 미제공','Frame origin · 0 ns','Frame later · 12345 ns']);
  assert.match(html,/<dt>시작 시각 · ns<\/dt><dd>미제공<\/dd>/);
  state.frameSelections['captured-replay']='origin';
  assert.match(sandbox.frameBrowser({id:'captured-replay',artifacts:[]},true),/<dt>시작 시각 · ns<\/dt><dd>0<\/dd>/);
});

const selector=(cal='RESPONSE-A',path='hcg')=>({schema:'camera_nonlinear_raw_selection_v2',mode:'shared_charge_path',readout_id:path,readout_calibration_id:cal});
const selectedSource=(cal='RESPONSE-A',path='hcg')=>({source_kind:'capture',source_id:'C-original',raw_sha256:'a'.repeat(64),name:'same retained RAW',raw_shape:[4,16,16],raw_container_shape:[4,2,16,16],raw_format:{hdr_streams:1},geometry:{kind:'native_sensor'},selector:selector(cal,path)});
const sourceKey=s=>s.source_kind+':'+s.source_id+':'+JSON.stringify([s.selector.schema,s.selector.mode,s.selector.readout_id,s.selector.readout_calibration_id]);
function ispContext(handler) {
  const sandbox={window:{},TextEncoder};vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/camera_platform/static/isp-v2.js','utf8'),sandbox);
  const calls=[];let source=selectedSource();const ui=sandbox.window.CameraAdvancedISPUI({api:async(path,body)=>{calls.push({path,body});return handler?handler(path,body):{name:'V3',kind:'isp',payload:{schema:'camera_isp_profile_v3',graph:['charge_decode'],calibration_id:null,spatial_lsc:{mode:'bypass'},raw_denoise:{mode:'bypass',strength:.5,range_floor_e2:4},linear_sharpen:{mode:'bypass'}}};},esc,render(){},refresh:async()=>{},toast(){},source:()=>source,onModel(){}});
  return {ui,calls,setSource:value=>{source=value;}};
}

test('the host replay key separates decoder version and path, preserving old keys and selector order invariance',()=>{
  const source=fs.readFileSync('src/camera_platform/static/app.js','utf8'),begin=source.indexOf('  function sourceKey(source) {'),end=source.indexOf('\n',begin);
  assert.ok(begin>=0&&end>begin);const key=vm.runInNewContext('('+source.slice(begin,end).trim()+')');
  assert.notEqual(key(selectedSource()),key(selectedSource('RESPONSE-B')));assert.notEqual(key(selectedSource()),key(selectedSource('RESPONSE-A','lcg')));
  const reordered={readout_calibration_id:'RESPONSE-A',readout_id:'hcg',mode:'shared_charge_path',schema:'camera_nonlinear_raw_selection_v2'};
  assert.equal(key(selectedSource()),key({...selectedSource(),selector:reordered}));assert.equal(key({source_kind:'run',source_id:'legacy'}),'run:legacy');
  assert.equal(key({source_kind:'run',source_id:'old-v1',selector:{schema:'camera_nonlinear_raw_selection_v1',mode:'sequential_frames'}}),'run:old-v1:["camera_nonlinear_raw_selection_v1","sequential_frames",""]');
});

test('captured nonlinear profile templates keep decoder identity separate from optional charge LSC',async()=>{
  const c=ispContext();await c.ui.change({dataset:{ispv2Field:'calibrationId'},value:'LSC-C'});await c.ui.click({dataset:{ispv2Action:'profile-template'}});
  const query=new URL('http://test'+c.calls[0].path).searchParams;assert.equal(query.get('version'),'3');assert.equal(query.get('calibration_id'),'LSC-C');assert.deepEqual(JSON.parse(query.get('selector')),selector());
  assert.match(c.ui.editor(),/ISP v3/);assert.doesNotMatch(c.ui.editor(),/RAW DN 기준/);
});

test('decoder revisions have separate ISP drafts and reject an opposite decoder charge calibration',async()=>{
  const c=ispContext();c.ui.input({dataset:{ispv2Text:'calibration'},value:'{unfinished A'});c.setSource(selectedSource('RESPONSE-B'));assert.doesNotMatch(c.ui.editor(),/unfinished A/);
  c.ui.input({dataset:{ispv2Text:'calibration'},value:'{unfinished B'});c.setSource(selectedSource());assert.match(c.ui.editor(),/unfinished A/);assert.doesNotMatch(c.ui.editor(),/unfinished B/);
  const wrong={schema:'camera_charge_isp_calibration_v1',name:'wrong decoder',reference_source:{kind:'capture',id:'C-original',selector:selector('RESPONSE-B')},payload:{}};
  c.ui.input({dataset:{ispv2Text:'calibration'},value:JSON.stringify(wrong)});await c.ui.submit({dataset:{ispv2Form:'calibration'}});
  assert.equal(c.calls.filter(x=>x.body).length,0);assert.match(c.ui.editor(),/경로·RAW 선택/);
});

test('Study source and dataset selectors preserve the chosen decoder calibration independently of LSC',async()=>{
  const calls=[],sandbox={window:{},TextEncoder,setTimeout:()=>1,clearTimeout(){}};vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/camera_platform/static/isp-studies.js','utf8'),sandbox);
  const spec={schema:'camera_isp_study_v2',name:'captured Study',question:'declared static repeat test',phase:'tuning',calibration_id:null,baseline_isp_model_id:'ISP3',axes:[{parameter:'raw_denoise.strength',values:[0,.5]}],datasets:[{key:'flat',source_kind:'capture',source_id:'C-original',selector:selector(),purpose:'tuning',acquisition_group_id:'independent-group',stationarity:{kind:'declared_static',description:'declared static capture',source_kind:'estimated'},metric_spec:{schema:'camera_isp_metric_spec_v2',kind:'flat',stationary:true,roi:{row_start:4,row_end:12,column_start:4,column_end:12},texture:null,edge:null},reference_id:null}],objectives:[{metric:'temporal_residual_std_e',direction:'minimize',dataset_keys:['flat'],reducer:'max'}],constraints:[]};
  const ui=sandbox.window.CameraISPStudiesUI({api:async(path,body)=>{calls.push({path,body:body===undefined?undefined:clone(body)});if(path==='/replay-sources')return [selectedSource(),selectedSource('RESPONSE-B')];if(path==='/models')return [{id:'ISP3',kind:'isp',name:'ISP3',payload:{schema:'camera_isp_profile_v3'}}];if(path==='/isp-studies/catalog')return {metrics:{},evaluation_halo_native_pixels:4};if(path.startsWith('/templates/isp-study?'))return clone(spec);if(path==='/isp-studies/plan')return {specification:body,job_count:2,plan_hash:'a'.repeat(64),resources:{},candidates:[],datasets:[]};return [];},esc,render(){},refresh:async()=>{},toast(){},onStudy(){},onValidationStudy(){},isActive:()=>true,records:()=>[]});
  await ui.activate();await ui.change({dataset:{ispstudyField:'source'},value:sourceKey(selectedSource())});await ui.change({dataset:{ispstudyField:'baseline'},value:'ISP3'});await ui.click({dataset:{ispstudyAction:'template'}});
  const request=calls.find(x=>x.path.startsWith('/templates/isp-study?'));assert.ok(request);const query=new URL('http://test'+request.path).searchParams;
  assert.equal(query.get('version'),'2');assert.equal(query.get('calibration_id'),null);assert.deepEqual(JSON.parse(query.get('selector')),selector());
  await ui.change({dataset:{ispstudyField:'dataset-source',index:'0'},value:sourceKey(selectedSource('RESPONSE-B'))});
  ui.input({dataset:{ispstudyField:'edit',path:'datasets.0.acquisition_group_id'},value:'another-group',type:'text'});ui.input({dataset:{ispstudyField:'edit',path:'datasets.0.stationarity.description'},value:'fixed capture',type:'text'});
  await ui.submit({dataset:{ispstudyForm:'plan'}});const plan=calls.find(x=>x.path==='/isp-studies/plan');assert.ok(plan);assert.deepEqual(plan.body.datasets[0].selector,selector('RESPONSE-B'));assert.equal(plan.body.calibration_id,null);
});

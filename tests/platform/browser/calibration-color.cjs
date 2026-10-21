'use strict';
const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm');
const scope={window:{},URL,URLSearchParams,Intl};
vm.createContext(scope);
vm.runInContext(fs.readFileSync((process.env.CAMERA_STATIC_DIR||'src/camera_platform/static')+'/calibration-color.js','utf8'),scope);
const UI=scope.window.CameraCalibrationColor;
const a='a'.repeat(64),b='b'.repeat(64);
function record(){return {id:'color-calibration-test',name:'Saved',source_kind:'synthetic',files:{'raw-training.npy':a,'raw-validation.npy':b},evidence:{raw_originals:{training:{sha256:a,frame_count:8,patch_count:24},validation:{sha256:b,frame_count:8,patch_count:24}}}};}
function html(r){return UI.view({items:[r],record:r,selected:r.id,campaign:'',draft:'',busy:false,reading:false,error:'',notice:'',template:null});}
function rawLinks(r){return [...html(r).matchAll(/href="([^"]+\.npy)"/g)].map(match=>match[1]);}
test('every RAW download uses an actual manifest key with matching original SHA256',()=>{
  const r=record(),links=rawLinks(r);
  assert.equal(links.length,2);
  for(const link of links){const name=decodeURIComponent(link.split('/files/')[1]);assert.ok(Object.hasOwn(r.files,name));const role=name==='raw-training.npy'?'training':'validation';assert.equal(r.files[name],r.evidence.raw_originals[role].sha256);}
  assert.ok(links[0].endsWith('/files/raw-training.npy'));
  assert.ok(links[1].endsWith('/files/raw-validation.npy'));
  assert.ok(!links.some(link=>link.includes('/files/originals/')));
});
test('missing or mismatched manifest originals never produce guessed links',()=>{
  const r=record();delete r.files['raw-training.npy'];r.files['raw-validation.npy']=a;
  assert.equal(UI.rawOriginalFile(r,'validation'),null);
  // The remaining file is byte-identical to training: choose the verified real key.
  assert.equal(UI.rawOriginalFile(r,'training'),'raw-validation.npy');
  r.files={};assert.equal(rawLinks(r).length,0);
});
test('legacy nested paths are accepted only when explicitly retained and hash bound',()=>{
  const r=record();r.files={'originals/training.npy':{sha256:a},'originals/validation.npy':{sha256:b}};
  assert.equal(UI.rawOriginalFile(r,'training'),'originals/training.npy');
  assert.equal(rawLinks(r).length,2);
  r.files['raw-training.npy']=a;assert.equal(UI.rawOriginalFile(r,'training'),'raw-training.npy');
});
test('unsafe or ambiguous filenames and unknown roles are not linked',()=>{
  const r=record();r.files={'../private.npy':a,'/absolute.npy':a,'https://host/file.npy':a,'x.npy':b,'y.npy':b};
  assert.equal(UI.rawOriginalFile(r,'training'),null);
  assert.equal(UI.rawOriginalFile(r,'validation'),null);
  assert.equal(UI.rawOriginalFile(r,'other'),null);
  assert.equal(rawLinks(r).length,0);
});
test('initial load and example import do not submit or inject the backlink campaign',async()=>{
  const writes=[],c=UI.createController({campaign:'existing-real-camera',api:async(path,body)=>{if(body!==undefined){writes.push(body);return {id:'new',...body};}return path==='/color-template'?{name:'Synthetic',qe_scan:{source_kind:'synthetic'}}:{items:[]};}});
  await c.init();assert.equal(c.state.draft,'');assert.equal(writes.length,0);
  await c.perform('template');assert.equal(UI.parse(c.state.draft).campaign_id,undefined);
  await c.perform('execute');assert.equal(writes.length,1);assert.equal(writes[0].campaign_id,undefined);
});

test('one calibration action retains DB condition and automatically watches its replay',async()=>{
  const writes=[],r=record(),model={id:'model-auto',color_calibration_id:r.id};
  const c=UI.createController({dbSource:{run_id:'run-source',candidate_id:'candidate-03',condition_id:'condition-006'},api:async(path,body)=>{
    if(body!==undefined){writes.push({path,body});return {status:'submitted',calibration:r,model,run:{id:'run-auto',status:'queued',model_snapshot:model}};}
    if(path==='/color-template')return {name:'Synthetic'};
    return {items:[r]};
  }});
  await c.init();c.input('{"name":"New calibration"}');await c.perform('execute');
  assert.equal(writes.length,1);assert.equal(writes[0].path,'/color-db-workflows');
  assert.equal(writes[0].body.model.source.condition_id,'condition-006');
  assert.equal(JSON.parse(writes[0].body.calibration_json).name,'New calibration');
  assert.equal(c.state.record.id,r.id);assert.equal(c.state.autoJob.id,'run-auto');
  c.display('field','outer-ne');c.display('stage','raw');assert.equal(writes.length,1);
});
test('loading a saved calibration discovers its existing DB report without writes',async()=>{
  const writes=[],r=record();
  const c=UI.createController({selected:r.id,autoResults:true,api:async(path,body)=>{
    if(body!==undefined){writes.push(body);throw Error('unexpected mutation');}
    if(path==='/color-template')return {};
    if(path==='/color-calibrations/'+r.id)return r;
    if(path.startsWith('/db-color-replays?'))return {items:[{id:'run-saved'}]};
    if(path==='/db-color-replays/run-saved')return {id:'run-saved',status:'succeeded',model_snapshot:{color_calibration_id:r.id}};
    return {items:[r]};
  }});
  await c.init();assert.equal(c.state.autoJob.id,'run-saved');assert.equal(writes.length,0);
});

test('browser template and editor transport preserve float lexemes and pinned JSON bytes',async()=>{
  const original='{"name":"Example","wavelength":[380.0,390.0],"black":64.0,"profile_sha256":"pinned"}',writes=[];
  const api=UI.createAPI(async(path,options)=>{
    if(path==='/api/v1/session')return {ok:true,json:async()=>({token:'local'})};
    if(path.endsWith('/color-template'))return {ok:true,text:async()=>original};
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return {ok:true,json:async()=>({id:'new'})};}
    return {ok:true,json:async()=>({items:[]})};
  });
  const c=UI.createController({api});await c.init();await c.perform('template');
  assert.equal(c.state.draft,original);await c.perform('execute');
  assert.equal(writes[0].input_json,original);
});

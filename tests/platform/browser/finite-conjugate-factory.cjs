'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
function body(name){const start=source.indexOf('  function '+name+'(');const end=source.indexOf('\n  function ',start+5);assert.ok(start>=0&&end>start);return source.slice(start,end);}
test('finite conjugate template action only appears on the two prescription solvers',()=>{
  const box={state:{factoryPlugins:[],factoryPlugin:'',factoryDrafts:{},factoryError:''},badge:()=>'',details:()=>'',icon:()=>'',esc:v=>String(v??'')};
  vm.createContext(box);vm.runInContext(body('factoryRequestPanel'),box);
  for(const id of ['rayoptics_lens_v1','prescription_psf_v1','prysm_psf_v1','meep_planar_stack_v1']){
    box.state.factoryPlugin=id;box.state.factoryPlugins=[{plugin_id:id,available:true}];
    assert.equal(box.factoryRequestPanel().includes('data-request-version="5"'),['rayoptics_lens_v1','prescription_psf_v1'].includes(id));
  }
});
test('publication visibly copies exact finite depth and magnification while preserving source',()=>{
  const box={};vm.createContext(box);vm.runInContext(body('factoryApplicationConditions'),box);
  const original={conditions:{exposure_s:{value:.004,unit:'s'},object_distance_mm:{value:99,unit:'mm'}}};
  const p={schema:'lens_prescription_v5',object_conjugate:{distance_mm:500,reference_magnification:-.04}};
  const job={plugin_id:'prescription_psf_v1',request:{prescription:p},result:{summary:{geometric_reference_f_number:19.8}}};
  const values=JSON.parse(JSON.stringify(box.factoryApplicationConditions(job,original)));
  assert.equal(values.object_distance_mm.value,500);assert.equal(values.object_reference_magnification.value,-.04);
  assert.equal(values.f_number.value,19.8);assert.equal(original.conditions.object_distance_mm.value,99);
  assert.equal(box.factoryApplicationConditions(null,null),null);
});
test('scene submission routes the explicit finite schema and explains finite source support',()=>{
  assert.match(source,/metadata\.schema==='camera_finite_plane_fixture_v1'\?'\/spectral_images\/finite_plane':'\/spectral_images\/fixture'/);
  assert.match(source,/입력 창 밖의 광휘를 0으로/);
});

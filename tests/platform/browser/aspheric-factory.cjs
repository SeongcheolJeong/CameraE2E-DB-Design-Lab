'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const source=fs.readFileSync('src/camera_platform/static/app.js','utf8');
const start=source.indexOf('  function factoryRequestPanel() {');
const end=source.indexOf('\n  function ',start+5);
test('prescription Factory v4 action is explicit and does not appear on unrelated solvers',()=>{
  assert.ok(start>=0 && end>start);
  const box={state:{factoryPlugins:[],factoryPlugin:'',factoryDrafts:{},factoryError:''},badge:()=>'',details:()=>'',icon:()=>'',esc:v=>String(v??'').replaceAll('<','&lt;')};
  vm.createContext(box);vm.runInContext(source.slice(start,end),box);
  for(const id of ['rayoptics_lens_v1','prescription_psf_v1','prysm_psf_v1','meep_planar_stack_v1']){
    box.state.factoryPlugin=id;box.state.factoryPlugins=[{plugin_id:id,available:true}];
    box.state.factoryDrafts[id]='{"schema":"existing-edited-input"}';
    const html=box.factoryRequestPanel();
    assert.equal(html.includes('data-request-version="4"'),['rayoptics_lens_v1','prescription_psf_v1'].includes(id));
    assert.match(html,/existing-edited-input/);
  }
});

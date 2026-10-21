'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');

function helpers(file){
  const source=fs.readFileSync('src/camera_platform/static/'+file,'utf8');
  vm.runInThisContext(source.slice(0,source.indexOf('(() => {')));
}
helpers('app.js');
helpers('design-experiments.js');

const catalog={models:[{id:'lens-a',name:'A',revision:'1'},{id:'lens-b',name:'B',revision:'2'}],scenes:[{id:'flat',name:'Flat',revision:'1'},{id:'checker',name:'Checker',revision:'1'}]};
const common={name:'Comparison',stops:['clear'],model_ids:['lens-a','lens-b'],scene_id:'checker',exposures_ms:[1,2],gains:[1],training_scales:[.5,2],evaluation_scales:[.85],evaluation_noise_repeats:1,seed:7,acknowledge:true,limits:{minimum_green_dn:120}};

test('R1 entry activates only for a supported local template, never a fixed external port',()=>{
  assert.equal(CameraDesignExperimentAvailability({specification:{schema:'camera_design_experiment_v2'},catalog,project_mode:{mode:'r1_engineering'}}).available,true);
  assert.equal(CameraDesignExperimentAvailability({specification:{schema:'camera_design_experiment_v2'},catalog}).available,false);
  assert.equal(CameraDesignExperimentAvailability({specification:{schema:'camera_design_experiment_v2'}}).available,false);
  assert.equal(CameraDesignExperimentAvailability({specification:{schema:'unknown'}}).available,false);
  assert.equal(CameraDesignExperimentAvailability(null).available,false);
  const main=fs.readFileSync('src/camera_platform/static/app.js','utf8');
  assert.match(main,/href="\/design-experiments\.html"/);
  assert.doesNotMatch(main,/127\.0\.0\.1:8798/);
});

test('v2 submission binds only registered model and scene IDs and does not send old stop labels',()=>{
  const template={schema:'camera_design_experiment_v2',name:'server',model_ids:['lens-a'],scene_id:'flat'};
  const value=CameraDesignExperimentSpecification(template,catalog,common);
  assert.deepEqual(value.model_ids,['lens-a','lens-b']);
  assert.equal(value.scene_id,'checker');
  assert.equal(value.schema,'camera_design_experiment_v2');
  assert.equal(Object.hasOwn(value,'stops'),false);
  assert.equal(value.acknowledge_simulation_limits,true);
  assert.equal(Object.hasOwn(value,'acknowledge_research_only'),false);
  for(const input of [
    {...common,model_ids:[]},
    {...common,model_ids:['lens-a','lens-a']},
    {...common,model_ids:['missing']},
    {...common,scene_id:'missing'},
  ])assert.throws(()=>CameraDesignExperimentSpecification(template,catalog,input),/등록된/);
});

test('legacy v1 can still build its original request and saved v1 results need no v2 catalog',()=>{
  const template={schema:'camera_design_experiment_v1',name:'legacy',stops:['clear']};
  const value=CameraDesignExperimentSpecification(template,undefined,common);
  assert.deepEqual(value.stops,['clear']);
  assert.equal(Object.hasOwn(value,'model_ids'),false);
  assert.equal(Object.hasOwn(value,'scene_id'),false);
  assert.equal(value.acknowledge_research_only,true);
  assert.equal(Object.hasOwn(value,'acknowledge_simulation_limits'),false);
  assert.throws(()=>CameraDesignExperimentSpecification(template,undefined,{...common,stops:['unknown']}),/투과/);
});

test('failed, cancelled and partial Worker records never appear as a completed choice',()=>{
  const partial={result:{summary:{status:'completed',selection:{selected_candidate_id:'lens-a-e2-g1'}}}};
  assert.equal(CameraDesignExperimentVisibleSummary({...partial,status:'failed'}),null);
  assert.equal(CameraDesignExperimentVisibleSummary({...partial,status:'cancelled'}),null);
  assert.equal(CameraDesignExperimentVisibleSummary({...partial,status:'succeeded',result:{summary:{status:'failed',selection:{selected_candidate_id:'lens-a-e2-g1'}}}}),null);
  assert.equal(CameraDesignExperimentVisibleSummary({...partial,status:'succeeded'}).selection.selected_candidate_id,'lens-a-e2-g1');
});

test('R1 case preview requires identity, RAW, and fixed ISP RGB while v1 remains readable',()=>{
  const detail={row:{case_id:'case-1',raw_artifact:{url:'/raw'},diagnostics_artifact:{url:'/diagnostics'},rgb_artifact:{url:'/rgb'}},images:{raw:'data:image/png;base64,a',final_code_rail:'data:image/png;base64,b',rgb:'data:image/png;base64,c'}};
  assert.deepEqual(CameraDesignExperimentCaseAssets(detail,'case-1','camera_design_experiment_v2'),{rgb:true});
  assert.throws(()=>CameraDesignExperimentCaseAssets(detail,'different','camera_design_experiment_v2'),/일치/);
  const legacy={...detail,row:{...detail.row,rgb_artifact:undefined},images:{...detail.images,rgb:undefined}};
  assert.deepEqual(CameraDesignExperimentCaseAssets(legacy,'case-1','camera_design_experiment_v1'),{rgb:false});
  assert.throws(()=>CameraDesignExperimentCaseAssets(legacy,'case-1','camera_design_experiment_v2'),/ISP RGB/);
});

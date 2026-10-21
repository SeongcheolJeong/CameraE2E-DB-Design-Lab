'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../../../src/camera_platform/static');
const html=fs.readFileSync(path.join(root,'reference-ghost.html'),'utf8'),source=fs.readFileSync(path.join(root,'reference-ghost.js'),'utf8');
function node(tag='div'){
  const attrs={},n={tag,value:'',textContent:'',children:[],dataset:{},disabled:false,checked:false,width:256,height:256,clearCount:0,paintCount:0,
    replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);},setAttribute(k,v){attrs[k]=v;},removeAttribute(k){delete attrs[k];},click(){},getBoundingClientRect(){return{left:10,top:20,width:500,height:500};},
    getContext(){return {clearRect:()=>n.clearCount++,drawImage:()=>n.paintCount++,beginPath(){},moveTo(){},lineTo(){},stroke(){},fillText(){},fillRect(){},getImageData(){return{data:new Uint8ClampedArray(256*256*4).fill(255)};},putImageData(){}};}};return n;
}
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size,'No duplicate control IDs');
const nodes=new Map(ids.map(id=>[id,node()]));for(const [id,value]of Object.entries({angle:'15',power:'100',coating:'r005',chart:'black',modeA:'source_off',modeB:'with_ghost',white:'.05',stage:'isp'}))nodes.get(id).value=value;
const context={console,structuredClone,URL,URLSearchParams,Float32Array,Uint8ClampedArray,document:{getElementById:id=>nodes.get(id),createElement:tag=>node(tag),createElementNS:(_ns,tag)=>node(tag)},location:{search:''},history:{replaceState(){}},setTimeout:()=>1};
vm.createContext(context);
const tail="  refresh(new URLSearchParams(location.search).get('run')).catch(e=>message(e.message));";
assert.ok(source.includes(tail));
vm.runInContext(source.replace(tail,`  update=async()=>nativeCase(); globalThis.ghostTest={state,selectPosition,selectedCase,positionFromMap,nativeCase,submit,renderChartMetrics,populateFields,sameXY};`),context);
const ui=context.ghostTest,plain=x=>JSON.parse(JSON.stringify(x));
const mode=id=>({id,image:id+'.png',valid:id+'-valid.png',metrics:{valid_fraction:1,temporal_noise_e:2,well_saturation_fraction:0,adc_saturation_fraction:0,fixed_roi_percentile_contrast:.2},chart_metrics:{black_level:{status:'ok',mean_excess_e:20,peak_excess_e:50,minimum_excess_e:-2,mean_e:21,temporal_variance_e:4},targets:[]}});
const savedCase={id:'case-01',angle_deg:15,coating:'r005',irradiance_w_m2:100,field_id:'probe-01',probe_xy_mm:[1,2],chart:'black',ghost_mean_e:20,ghost_peak_e:50,background_transmission_min_max:[.8,.9],raw_hashes:{source_off:'raw-off',direct_only:'raw-direct',with_ghost:'raw-ghost'},modes:['source_off','direct_only','with_ghost'].map(mode)};
ui.state.run='prepared';ui.state.result={fields:[{id:'probe-01',xy_mm:[1,2]}],cases:[savedCase],specification:{schema:'camera_reference_ghost_study_v2',isp:{display_white_level:.05,lsc_enabled:false},fields:[],probes:[{id:'probe-01',xy_mm:[1,2]}],charts:['black']}};
ui.populateFields();assert.deepEqual(plain(ui.state.selectedXY),[1,2]);assert.equal(ui.selectedCase(),savedCase);
for(const m of savedCase.modes)for(const key of ['image','valid'])ui.state.images.set('/api/v1/reference-workshop/ghost-studies/prepared/files/'+m[key],Promise.resolve({}));
assert.deepEqual(plain(ui.positionFromMap({clientX:260,clientY:270},nodes.get('ghostMap'))),[0,0]);
assert.deepEqual(plain(ui.positionFromMap({clientX:10,clientY:20},nodes.get('ghostMap'))),[-13.66,13.66]);
async function checks(){
 await ui.nativeCase();assert.equal(nodes.get('nativeA').paintCount,1);assert.equal(nodes.get('nativeB').paintCount,1);assert.match(nodes.get('rawBinding').textContent,/raw-off.*raw-ghost/);
 await ui.selectPosition(1.01,2);assert.equal(ui.selectedCase(),undefined,'A neighboring location must not reuse the previous RAW');assert.equal(nodes.get('rawBinding').textContent,'');assert.equal(nodes.get('metrics').children.length,0);assert.match(nodes.get('caseNote').textContent,/일치하는 RAW가 없습니다/);assert.ok(nodes.get('nativeA').clearCount>0);
 // A slow image fetch from a previously selected probe must never repaint the new position.
 const pending=[];for(const m of savedCase.modes){m.image=m.id+'-late.png';ui.state.images.set('/api/v1/reference-workshop/ghost-studies/prepared/files/'+m.image,new Promise(resolve=>pending.push(resolve)));}
 ui.state.selectedXY=[1,2];const paints=nodes.get('nativeA').paintCount,older=ui.nativeCase();await ui.selectPosition(1.01,2);pending.forEach(resolve=>resolve({}));await older;assert.equal(nodes.get('nativeA').paintCount,paints,'A late old image cannot repaint after probe selection');
 for(const m of savedCase.modes)ui.state.images.set('/api/v1/reference-workshop/ghost-studies/prepared/files/'+m.image,Promise.resolve({}));
 assert.throws(()=>ui.selectPosition(13.4,0),/센서면 안/,'Reject ROI footprint crossing sensor bounds');assert.deepEqual(plain(ui.state.selectedXY),[1.01,2]);
 await ui.selectPosition(1,2);assert.equal(ui.selectedCase(),savedCase);nodes.get('power').value='101';await ui.nativeCase();assert.equal(nodes.get('rawBinding').textContent,'','Changing source power invalidates the recorded native view');nodes.get('power').value='100';
 const legacy={...savedCase,modes:savedCase.modes.filter(m=>m.id!=='source_off')};ui.state.result.cases=[legacy];await ui.nativeCase();assert.match(nodes.get('caseNote').textContent,/취득 비교군이 없습니다/);assert.equal(nodes.get('rawBinding').textContent,'');nodes.get('modeA').value='direct_only';await ui.nativeCase();assert.match(nodes.get('rawBinding').textContent,/raw-direct/);nodes.get('stage').value='raw';await ui.nativeCase();assert.equal(nodes.get('rawBinding').textContent,'');assert.match(nodes.get('caseNote').textContent,/RAW 미리보기가 없습니다/);nodes.get('stage').value='isp';
 ui.state.result.cases=[savedCase];const request=[];context.fetch=async(url,options)=>{request.push({url,options});if(url==='/api/v1/session')return{ok:true,json:async()=>({token:'csrf'})};if(options?.method==='POST')return{ok:true,json:async()=>({id:'new-probe',status:'queued'})};return{ok:true,json:async()=>({id:'new-probe',status:'running'})};};
 await ui.submit(false);const post=request.find(r=>r.options?.method==='POST');const spec=JSON.parse(post.options.body);assert.equal(spec.schema,'camera_reference_ghost_study_v2');assert.deepEqual(spec.fields,[]);assert.deepEqual(spec.probes,[{id:'probe-01',xy_mm:[1,2]}]);assert.equal(spec.isp.lsc_enabled,false);assert.equal(spec.isp.display_white_level,.05);assert.equal(post.options.headers['X-Platform-Token'],'csrf');
 await nodes.get('cancel').onclick();const cancelled=request.find(r=>r.url.endsWith('/new-probe/cancel'));assert.ok(cancelled,'Cancel uses the currently active job');assert.equal(cancelled.options.method,'POST');assert.equal(cancelled.options.headers['Content-Type'],'application/json');assert.equal(cancelled.options.headers['X-Platform-Token'],'csrf');assert.equal(cancelled.options.body,'{}');
 console.log('Reference Ghost probes: physical map coordinates, complete ROI bounds, exact native identity, missing source-off handling, v2 capture payload and unchanged saved RAW references passed');
}
checks().catch(e=>{console.error(e);process.exitCode=1;});

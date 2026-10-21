'use strict';
// Actual retained ISP UI; synthetic API records exercise selection semantics.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test');
const script=fs.readFileSync('src/camera_platform/static/retained-isp.js','utf8');
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const selector=path=>({schema:'camera_retained_raw_selection_v1',readout_id:path});
const choice=path=>({source_kind:'run',source_id:'A',name:'Paired',selector:selector(path),readout_id:path,readout_index:['HCG','LCG'].indexOf(path),source_plugin_id:'electrical_control_v1',acquisition_mode:'closed_loop',raw_sha256:'a'.repeat(64),raw_container_shape:[2,2,8,10],raw_shape:[2,8,10],raw_format:{response_type:'nonlinear_charge_voltage'},actual_frame_conditions:[0,1].map(i=>({frame_id:'f'+i,frame_start_ns:i*10,exposure_ns:i?2:4})),observation_readout_id:'HCG',observation_readout_index:0});
const catalogue=()=>({schema:'camera_retained_isp_workbench_v1',sources:[choice('HCG'),choice('LCG')],models:[],replays:[],comparisons:[],source_discovery:{scope:'metadata_only',raw_integrity:'verified_on_selection',physical_validation:'not_established'}});
const record=()=>({id:'R',name:'Paired replay',status:'succeeded',job_kind:'isp_replay',artifacts:[],result:{summary:{source_id:'A',selector:selector('LCG'),readout_id:'LCG',raw_sha256:'a'.repeat(64),source_plugin_id:'electrical_control_v1',acquisition_mode:'closed_loop',control_trace_role:'evidence_only_not_processing_inputs',frames:[0,1].map(i=>({frame_id:'f'+i,frame_index:i})),control_trace:{readout_id:'HCG',observation_readout_index:0,policy:{enabled:true},frames:[0,1].map(i=>({frame_id:'f'+i,index:i,latch_at_ns:i*10,applied:{exposure_ns:i?2:4,gain:1},proposal:{status:'hold',commanded:null}}))}}}});
function context(data=catalogue(),saved=record()){
  const calls=[],box={window:{},document:{querySelector:()=>null},setTimeout:()=>1,clearTimeout:()=>{}};
  vm.createContext(box);vm.runInContext(script,box);
  const ui=box.window.CameraRetainedISPUI({esc,render:()=>{},api:async(path,body)=>{calls.push({path,body});if(path==='/retained-isp/workbench')return data;if(path==='/replays/R')return saved;throw Error(path);}});
  return {ui,calls};
}
const change=(c,field,value)=>c.ui.change({dataset:{retainedispField:field},value});

test('paired discovery separates selected LCG ISP RAW from fixed HCG feedback',async()=>{
  const c=context();await c.ui.activate();const x=choice('LCG');await change(c,'source',JSON.stringify([x.source_id,x.selector.readout_id,x.raw_sha256]));
  assert.match(c.ui.view(),/ISP RAW 경로 LCG · AE 관측 경로 HCG/);assert.match(c.ui.view(),/f0 4 ns → f1 2 ns/);assert.match(c.ui.view(),/목록 정보이며 선택 시/);
});

test('paired replay selected frame exposes display and meter independently',async()=>{
  const c=context();await change(c,'history','/replays/R');await change(c,'frame','1');
  assert.match(c.ui.view(),/ISP RAW 경로 <strong>LCG<\/strong> · AE 관측 경로 <strong>HCG<\/strong>/);
  assert.match(c.ui.view(),/실제 적용 노출 <strong>2 ns<\/strong>/);assert.match(c.ui.view(),/ISP 경로 선택은 원본 AE 관측 경로를 바꾸지 않습니다/);
  assert.match(c.ui.view(),/전하 역변환·노출 정규화는 수행하지 않습니다/);
});

test('paired discovery refuses missing or inconsistent metering and selected path indices',async()=>{
  for(const mutate of [x=>delete x.observation_readout_id,x=>x.observation_readout_index=1,x=>x.readout_index=0,x=>x.raw_container_shape[1]=1]){
    const data=catalogue();mutate(data.sources[1]);const c=context(data);await c.ui.activate();assert.match(c.ui.view(),/목록의 형식 또는 검증 범위/);assert.equal(c.calls.length,1);
  }
});

test('paired result refuses absent fixed-meter binding and escapes meter evidence',async()=>{
  const r=record();delete r.result.summary.control_trace.observation_readout_index;const c=context(catalogue(),r);await change(c,'history','/replays/R');
  assert.match(c.ui.view(),/공유 전하의 ISP 경로와 AE 관측 경로를 확인할 수 없습니다/);assert.doesNotMatch(c.ui.view(),/data-retainedisp-acquisition/);
  r.result.summary.control_trace.observation_readout_index=0;r.result.summary.control_trace.frames[0].applied_command_id='<unsafe>';
  await change(c,'history','/replays/R');assert.match(c.ui.view(),/적용 명령 &lt;unsafe&gt;/);assert.doesNotMatch(c.ui.view(),/<unsafe>/);
});

test('paired replay distinguishes acquisition gain from selected path analog and digital gains',async()=>{
  const r=record();r.result.summary.frame_conditions=[{frame_id:'f0',gain:1,readout_analog_gain:2,readout_digital_gain:1.5},{frame_id:'f1',gain:1,readout_analog_gain:3,readout_digital_gain:1.25}];
  const c=context(catalogue(),r);await change(c,'history','/replays/R');await change(c,'frame','1');
  assert.match(c.ui.view(),/획득 Gain 1/);assert.match(c.ui.view(),/ISP LCG 읽기 · Analog Gain 3 · Digital Gain 1.25/);assert.doesNotMatch(c.ui.view(),/ISP LCG 읽기 · Analog Gain 2/);
});

test('missing selected path gain evidence stays unknown instead of using acquisition or meter gain',async()=>{
  const c=context();await change(c,'history','/replays/R');
  assert.match(c.ui.view(),/획득 Gain 1/);assert.match(c.ui.view(),/ISP LCG 읽기 · Analog Gain 미확인 · Digital Gain 미확인/);
});

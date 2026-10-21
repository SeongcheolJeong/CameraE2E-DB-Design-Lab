'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),crypto=require('node:crypto');
const box={};vm.createContext(box);vm.runInContext(fs.readFileSync('src/camera_platform/static/electrical-response.js','utf8'),box);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ui=box.CameraElectricalResponseUI({esc}),clone=value=>JSON.parse(JSON.stringify(value));
const hash=id=>crypto.createHash('sha256').update(id).digest('hex');
const bindings={assembly_id:'assembly-1',scene_sequence_id:'scene-1',control_sequence_id:'control-1',capture_program_id:'program-1'};
const specification=()=>({schema:'camera_electrical_response_study_v1',name:'Step study',...bindings,axes:[{parameter:'waveform.phase_ns',values:[0,100]}],seeds:[41,42],noise_enabled:false,evaluation:{schema:'camera_electrical_response_evaluation_v1',roi:{reference:'native_sensor',origin:[0,0],shape:[4,4]},sites:['Gr','Gb'],target_black_relative_dn:{minimum:100,maximum:120},clock:{kind:'relative',unit:'ns'},window:{start_ns:1000,end_ns:4000,change_point_ns:1000},frame_ids:['frame-0','frame-1'],stimulus:{kind:'constant_step',declaration:'authored_simulation'},hold:{consecutive_observations:2,minimum_span_ns:1000},crossing_exposure_policy:'exclude'}});
const state=()=>({text:JSON.stringify(specification()),bindings:clone(bindings),options:{assemblies:[{id:'assembly-1',name:'Camera'}]},history:[]});
const response=spec=>({plan_hash:'a'.repeat(64),frozen_request:{specification:clone(spec)},cost:{captures:8}});
function planned(){const s=state(),ticket=ui.beginPlan(s);assert.equal(ui.acceptPlan(s,ticket,response(ticket.specification)),true);return s;}
function record(){
  const artifacts=[];
  const artifact=(name,type)=>{const id=hash(name),a={id,name,sha256:id,url:'/api/v1/artifacts/'+id,media_type:type};artifacts.push(a);return a;};
  const part=(id,exposure,value)=>({run_id:id,status:'succeeded',raw_artifact:artifact(id+'-raw','application/octet-stream'),frames:['frame-0','frame-1'].map((frame_id,index)=>({frame_id,preview_artifact_id:artifact(id+'-'+index,'image/png').id})),evaluation:{specification:specification().evaluation,raw_format:{response_type:'nonlinear_charge_voltage',black_level_dn:63.5},frames:['frame-0','frame-1'].map((frame_id,index)=>({frame_id,status:'observed',eligible:true,in_target:false,statistics:{value_dn:value+index,digital_low_rail_fraction:0,digital_high_rail_fraction:.25},timing:{applied_exposure_ns:exposure,statistics_ready_ns:2000+index*1000},exclusion_reasons:[]})),settling:{status:'not_reached',reason:'complete_declared_window',entry_latency_ns:null,confirmation_latency_ns:null,observed_hold_span_ns:null}}});
  const cell=(index,phase,seed)=>({cell_id:'cell-'+index,coordinates:{'waveform.phase_ns':phase},seed,baseline:part('base-'+index,2000,200+index*10),candidate:part('ae-'+index,1000,150+index*10),comparison:{frames:['frame-0','frame-1'].map(frame_id=>({frame_id,status:'paired',delta_value_dn:-50,delta_digital_low_rail_fraction:0,delta_digital_high_rail_fraction:-.1,delta_applied_exposure_ns:-1000,delta_statistics_ready_ns:-1000,missing_or_excluded_reasons:[]}))}});
  const cells=[cell(0,0,41),cell(1,0,42),cell(2,100,41)];
  return {id:'study-1',name:'Stored comparison',status:'succeeded',artifacts,result:{summary:{specification:specification(),cells}}};
}
test('author shows immutable selections, explicit target/window/hold, axes, seeds and noise',()=>{
  const html=ui.render(state());
  for(const key of Object.keys(bindings))assert.ok(html.includes('data-electricalresponse-field="'+key+'"'));
  for(const text of ['100–120 signed RAW−black DN','1000 ns → 4000 ns','연속 관측: 2개','최소 관측 span: 1000 ns','waveform.phase_ns','결정론 흐름','data-electricalresponse-action="plan"'])assert.ok(html.includes(text),text);
  assert.doesNotMatch(html,/data-electricalresponse-action="submit"/);
});
test('draft text, immutable option labels, names and request errors are escaped',()=>{
  const s=state();s.text='</textarea><script>bad()</script>';s.options.assemblies=[{id:'"<svg onload=x>',name:'<img onerror=x>'}];s.error='<script>error</script>';
  const html=ui.render(s);assert.doesNotMatch(html,/<script>|<svg|<img onerror/);assert.match(html,/&lt;\/textarea&gt;/);assert.match(html,/&quot;&lt;svg/);
});
test('missing and invalid JSON remains authored text and cannot become a plan',()=>{
  const s=state();ui.setText(s,'{invalid');assert.throws(()=>ui.beginPlan(s));assert.equal(s.text,'{invalid');assert.match(ui.render(s),/평가 명세 없음/);
  s.text='null';assert.throws(()=>ui.beginPlan(s),/JSON 객체/);
});
test('plans and submit payload are detached and preserve explicit force outside plan',()=>{
  const s=state(),spec=specification();spec.force=true;s.text=JSON.stringify(spec);const ticket=ui.beginPlan(s),r=response(spec);delete r.frozen_request.specification.force;
  assert.equal(ui.acceptPlan(s,ticket,r),true);r.cost.captures=999;assert.equal(s.plan.cost.captures,8);
  const payload=ui.submitPayload(s);assert.equal(payload.specification.force,true);payload.specification.evaluation.hold.minimum_span_ns=0;
  assert.equal(s.plan.frozen_request.specification.evaluation.hold.minimum_span_ns,1000);
});
test('format-independent canonical key ordering does not invalidate the same server specification',()=>{
  const s=state(),ticket=ui.beginPlan(s),r=response(ticket.specification);r.frozen_request.specification=Object.fromEntries(Object.entries(r.frozen_request.specification).reverse());
  assert.equal(ui.acceptPlan(s,ticket,r),true);
});
test('changing text invalidates the plan immediately, even if changed back',()=>{
  const s=planned(),original=s.text;ui.setText(s,original+' ');assert.equal(s.plan,null);ui.setText(s,original);assert.throws(()=>ui.submitPayload(s),/다시 검사/);
});
test('changing immutable input rejects a pending plan response and mismatched JSON',()=>{
  const s=state(),ticket=ui.beginPlan(s);ui.setBindings(s,{assembly_id:'assembly-2'});
  assert.equal(ui.acceptPlan(s,ticket,response(ticket.specification)),false);assert.equal(s.plan,null);assert.throws(()=>ui.beginPlan(s),/현재 고정 입력/);
});
test('later plan request dominates older success and failure without losing the newer plan',()=>{
  const s=state(),old=ui.beginPlan(s),fresh=ui.beginPlan(s);
  assert.equal(ui.acceptPlan(s,fresh,response(fresh.specification)),true);
  assert.equal(ui.acceptPlan(s,old,response(old.specification)),false);assert.equal(ui.rejectPlan(s,old,Error('late error')),false);
  assert.equal(s.plan.plan_hash,'a'.repeat(64));assert.equal(s.error,'');
});
for(const field of ['assembly_id','evaluation','axes'])test('server plan cannot replace requested '+field,()=>{
  const s=state(),ticket=ui.beginPlan(s),r=response(ticket.specification);
  if(field==='evaluation')r.frozen_request.specification.evaluation.target_black_relative_dn.maximum=121;else if(field==='axes')r.frozen_request.specification.axes=[];else r.frozen_request.specification[field]='wrong';
  assert.throws(()=>ui.acceptPlan(s,ticket,r),/계획이 요청한/);assert.equal(s.plan,null);
});
test('plan failure remains visible, removes submit authority and does not destroy text',()=>{
  const s=state(),text=s.text,ticket=ui.beginPlan(s);assert.equal(ui.rejectPlan(s,ticket,Error('<img> refused')),true);
  assert.equal(s.text,text);assert.match(ui.render(s),/&lt;img&gt; refused/);assert.throws(()=>ui.submitPayload(s));
});
test('submission locks author edits and rejects a second submission payload request',()=>{
  const s=planned();s.submitting=true;assert.equal(ui.setText(s,'replacement'),false);assert.equal(ui.setBindings(s,{assembly_id:'other'}),false);assert.throws(()=>ui.submitPayload(s));
  assert.match(ui.render(s),/data-electricalresponse-field="json"[^>]*disabled/);assert.match(ui.render(s),/data-electricalresponse-action="submit" disabled/);
});
test('planning and loading states are explicit and never offer an active submit',()=>{
  const s=state();ui.beginPlan(s);assert.match(ui.render(s),/aria-busy="true"/);assert.match(ui.render(s),/계획 검사 중/);
  s.loading=true;assert.throws(()=>ui.beginPlan(s),/진행 중인 요청/);assert.match(ui.render(s),/저장 결과를 확인 중/);
});
test('result preserves actual paired exposures, signed deltas and original nonlinear format',()=>{
  const s=state();s.record=record();const before=JSON.stringify(s.record),html=ui.render(s);
  for(const text of ['실제 노출: 2000 ns','실제 노출: 1000 ns','관측 signed DN: 200','관측 signed DN: 150','-50 DN','-1000 ns','nonlinear_charge_voltage','63.5','원본 RAW 전체 프레임','/studies/study-1/bundle'])assert.ok(html.includes(text),text);
  assert.equal(JSON.stringify(s.record),before);assert.equal((html.match(/<img class="preview-image"/g)||[]).length,2);
});
test('selected original command trace remains separate from the RAW evaluation',()=>{
  const s=state();s.record=record();s.record.result.summary.cells[0].candidate.control_trace={frames:[{frame_id:'frame-0',proposal:{status:'within_target',commanded:{exposure_ns:2200}},applied_command_id:'cmd-previous',latch_at_ns:1900}],commands:[{command_id:'cmd-previous',status:'applied'}]};
  const html=ui.render(s);for(const text of ['이 관측의 정책 판정: within_target','제안 노출: 2200 ns','적용 명령: cmd-previous','Latch: 1900 ns','관측 signed DN: 150'])assert.ok(html.includes(text),text);
});
test('historical result displays its own evaluation contract independently of the edited draft',()=>{
  const s=state();s.record=record();s.record.result.summary.specification.evaluation.target_black_relative_dn={minimum:300,maximum:350};
  const html=ui.render(s);assert.match(html,/100–120 signed RAW−black DN/);assert.match(html,/이 저장 결과의 평가 계약/);assert.match(html,/300–350 signed RAW−black DN/);
});
test('artifact ID, SHA and local URL must name the same content-addressed evidence',()=>{
  const s=state();s.record=record();for(const a of s.record.artifacts){a.url='/api/v1/artifacts/'+hash('unrelated');}
  const html=ui.render(s);assert.doesNotMatch(html,/<img |href="\/api\/v1\/artifacts\//);
});
test('condition, seed pair and frame selection preserve exact original frame pairing',()=>{
  const s=state();s.record=record();ui.select(s,{cell:'cell-0',pair:'cell-1',frame:'frame-1'});
  let html=ui.render(s);assert.match(html,/seed 42/);assert.match(html,/관측 signed DN: 211/);assert.ok(html.includes('<img class="preview-image" src="/api/v1/artifacts/'+hash('ae-1-1')));assert.ok(!html.includes('<img class="preview-image" src="/api/v1/artifacts/'+hash('ae-0-0')));
  ui.select(s,{cell:'cell-2'});assert.equal(s.pair,'');assert.equal(s.frame,'');html=ui.render(s);assert.match(html,/관측 signed DN: 220/);assert.ok(html.includes('<img class="preview-image" src="/api/v1/artifacts/'+hash('ae-2-0')));
});
test('invalid selections fall back to a real pair and never fabricate a frame',()=>{
  const s=state();s.record=record();s.cell='absent';s.pair='absent';s.frame='absent';const html=ui.render(s);
  assert.match(html,/선택 프레임 frame-0/);assert.doesNotMatch(html,/선택 프레임 absent/);
});
for(const [kind,label] of [['censored','관측 범위 부족'],['not_applicable','해당 없음'],['not_reached','미도달']])test('settling '+kind+' is explicit and absent latency is not zero',()=>{
  const s=state();s.record=record();s.record.result.summary.cells[0].candidate.evaluation.settling.status=kind;
  const html=ui.render(s);assert.ok(html.includes(label));assert.match(html,/진입 지연: 미관측/);assert.doesNotMatch(html,/진입 지연: 0 ns/);
});
test('missing observation and unavailable comparison retain reasons without invented zero or paired evidence',()=>{
  const s=state();s.record=record();const cell=s.record.result.summary.cells[0];cell.candidate.evaluation.frames[0]={frame_id:'frame-0',status:'missing',exclusion_reasons:['missing_child']};cell.candidate.frames=[];
  cell.comparison.frames[0]={frame_id:'frame-0',status:'unavailable',missing_or_excluded_reasons:['candidate_missing'],delta_value_dn:null};
  const html=ui.render(s);assert.match(html,/missing_child/);assert.match(html,/candidate_missing/);assert.match(html,/관측 signed DN: 미관측/);assert.ok(!html.includes('<img class="preview-image" src="/api/v1/artifacts/'+hash('ae-0-0')));
});
test('unsafe or unbound artifact links are refused even when frame claims a preview URL',()=>{
  const s=state();s.record=record();const cell=s.record.result.summary.cells[0];cell.baseline.frames[0].preview_url='https://external.test/image.png';cell.candidate.frames[0].preview_artifact_id='not-in-artifacts';
  cell.baseline.raw_artifact={...cell.baseline.raw_artifact,sha256:'c'.repeat(64)};s.record.artifacts.find(a=>a.name==='ae-0-raw').url='javascript:alert(1)';
  const html=ui.render(s);assert.doesNotMatch(html,/<img |href="javascript:|href="https:/);assert.doesNotMatch(html,/>원본 RAW 전체 프레임/);
});
test('failed aggregate exposes explicitly retained evidence without calling it a completed pair',()=>{
  const s=state();s.record=record();s.record.status='failed';const summary=s.record.result.summary;summary.status='no_complete_pairs';summary.complete_pair_count=0;summary.planned_pair_count=3;
  for(const cell of summary.cells){cell.candidate={run_id:'failed-child',status:'failed',error:'numerical refusal',evaluation:null,frames:[]};cell.comparison=null;}
  const html=ui.render(s);assert.match(html,/완료된 비교 쌍 없음/);assert.match(html,/0 \/ 3쌍/);assert.match(html,/numerical refusal/);assert.ok(html.includes('<img class="preview-image" src="/api/v1/artifacts/'+hash('base-0-0')));assert.equal((html.match(/<img class="preview-image"/g)||[]).length,1);
});
test('partial success reports exact completed and planned pair counts without hiding failed conditions',()=>{
  const s=state();s.record=record();Object.assign(s.record.result.summary,{status:'partial',complete_pair_count:2,planned_pair_count:3});
  const html=ui.render(s);assert.match(html,/일부 비교 쌍만 완료/);assert.match(html,/2 \/ 3쌍/);assert.match(html,/cell-2/);
});
test('succeeded without paired evidence reports missing result and shows no synthesized metrics',()=>{
  const s=state();s.record={id:'x',status:'succeeded',result:{summary:{}}};const html=ui.render(s);
  assert.match(html,/쌍별 결과 근거가 없습니다/);assert.doesNotMatch(html,/<img|관측 signed DN:/);
});
for(const status of ['queued','running','failed','cancelled'])test(status+' cannot display stale successful frames and exposes terminal errors',()=>{
  const s=state();s.record=record();s.record.status=status;s.record.error={reason:'<script>failure</script>'};const html=ui.render(s);
  assert.match(html,/&lt;script&gt;failure/);assert.doesNotMatch(html,/<script>|<img|관측 signed DN:/);
  assert.equal(html.includes('data-electricalresponse-action="cancel"'),['queued','running'].includes(status));
});
test('scope rejects physical, optimization and matched-noise overclaims',()=>{
  for(const word of ['프레임 ID','실제 관측 준비 시각','잡음 일치를 보장하지','물리 LFM 개선','자동 최적해'])assert.ok(ui.scopeNote.includes(word),word);
});

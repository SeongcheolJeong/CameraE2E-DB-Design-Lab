'use strict';

// Feedback policy authoring and acquisition evidence have their own workflow.
window.CameraControlUI = function(ctx) {
  const {api,esc,records,badge,details,number,heading,render,refresh,toast,busy,state,downloadObject}=ctx;
  const s={initialized:false,assembly:'',scene:'',policy:'',program:'',run:'',frame:0,count:'16',seed:'42',policyDraft:'',programDraft:'',check:null,error:'',preflight:null,revision:0,pending:false};
  const stable=v=>Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
  const equal=(a,b)=>JSON.stringify(stable(a))===JSON.stringify(stable(b));
  const json=v=>JSON.stringify(v,null,2), find=(kind,id)=>records(kind).find(r=>r.id===id);
  const draw=()=>{if(state.view==='controls')render();};
  const runs=()=>records('runs').filter(r=>r.plugin_id==='closed_loop_scene_v1');
  const summary=()=>find('runs',s.run)?.result?.summary;
  function needed() {
    if(!s.initialized){s.assembly=state.assembly||records('assemblies')[0]?.id||'';s.scene=records('scene_sequences')[0]?.id||'';s.run=runs()[0]?.id||'';s.initialized=true;}
    return [find('control_programs',s.program),find('runs',s.run)].filter(Boolean);
  }
  function invalidate(){s.revision++;s.check=null;s.error='';s.preflight=null;document.querySelector('[data-control-check]')?.remove();document.querySelector('[data-control-error]')?.remove();}
  function parse(text) {
    if(new TextEncoder().encode(text).length>1_000_000)throw new Error('JSON은 1 MB 이하여야 합니다.');
    return JSON.parse(text,(_key,value)=>{
      if(typeof value==='number'&&(!Number.isFinite(value)||(Number.isInteger(value)&&!Number.isSafeInteger(value))))throw new Error('브라우저에서 정확히 표현할 수 없는 수입니다. 시간은 원점에 상대적인 안전한 정수 ns를 사용하거나 Python SDK로 등록하세요.');
      return value;
    });
  }
  const button=(action,label,disabled=false,primary=false)=>`<button type="button" class="button ${primary?'primary':'secondary'} small" data-control-action="${action}" ${disabled||s.pending?'disabled':''}>${esc(label)}</button>`;
  function select(field,label,kind,value,empty='선택하세요') {
    return `<div class="form-field"><label for="control-${field}">${esc(label)}</label><select id="control-${field}" data-control-field="${field}"><option value="">${esc(empty)}</option>${records(kind).map(r=>`<option value="${esc(r.id)}" ${r.id===value?'selected':''}>${esc(r.name||r.id)} · ${esc(r.revision||'')}</option>`).join('')}</select></div>`;
  }
  const input=(field,label,value,type='text')=>`<div class="form-field"><label for="control-${field}">${esc(label)}</label><input id="control-${field}" data-control-field="${field}" type="${type}" value="${esc(value)}" ${type==='number'?'step="any"':''}></div>`;
  function parameters() {
    let draft;try{draft=parse(s.policyDraft);}catch{return '<p class="muted">정책 JSON을 불러오거나 올바르게 수정하면 편집 항목이 표시됩니다.</p>';}
    const read=path=>path.split('.').reduce((v,k)=>v?.[k],draft);
    const field=(path,label,bool=false)=>{const value=read(path);return `<div class="form-field"><label for="control-param-${path}">${esc(label)}</label>${bool?`<select id="control-param-${path}" data-control-field="parameter" data-path="${path}"><option value="true" ${value===true?'selected':''}>활성</option><option value="false" ${value===false?'selected':''}>고정</option></select>`:`<input id="control-param-${path}" data-control-field="parameter" data-path="${path}" type="number" step="any" value="${esc(value)}">`}</div>`;};
    return `<div class="control-fields">${field('payload.ae.enabled','AE',true)}${field('payload.awb.enabled','AWB',true)}${field('payload.ae.target_normalized','AE 목표 · 정규화 RAW G')}${field('payload.ae.minimum_delay_frames','AE 최소 지연 · 프레임')}${field('payload.ae.exposure_ns.minimum','최소 노출 · ns')}${field('payload.ae.exposure_ns.maximum','최대 노출 · ns')}${field('payload.ae.exposure_ns.step','노출 조절 간격 · ns')}${field('payload.ae.readout_gain.minimum','최소 Readout gain')}${field('payload.ae.readout_gain.maximum','최대 Readout gain')}${field('payload.ae.readout_gain.step','Gain 조절 간격')}${field('payload.awb.minimum_delay_frames','AWB 최소 지연 · 프레임')}${field('payload.latency.frame_transport_ns','영상 전달 지연 · ns')}${field('payload.latency.command_transport_ns','명령 전달 지연 · ns')}${field('payload.latency.actuator_ns','설정 반영 지연 · ns')}</div>`;
  }
  function editor(field,label) {return `<details class="data-detail control-editor" ${s[field]?'open':''}><summary>${esc(label)}</summary><label class="muted" for="control-${field}">이름·버전·출처 및 전체 설정</label><textarea id="control-${field}" data-control-field="${field}" spellcheck="false" rows="10">${esc(s[field])}</textarea></details>`;}
  function checks() {
    if(!s.check)return '';
    const c=s.check.compatibility;
    return `<section class="panel" data-control-check><div class="panel-content"><h2>실행 범위 검사 ${badge(c.status)}</h2><p>AE가 도달할 수 있는 전체 노출·Gain 구간과 각 모델의 적용 조건을 검사했습니다. 물리 정확도 검증 상태는 미확립입니다.</p>${details('모델별 적용 범위 · 자원 · 시간 범위',s.check)}${button('register-program','검사한 프로그램 등록',false,true)}</div></section>`;
  }
  function graph(frames,target) {
    const values=frames.map(f=>f.statistics.ae.value),finite=values.filter(Number.isFinite);
    if(!finite.length)return '';
    const max=Math.max(target,...finite,0.001)*1.12, x=i=>45+650*i/Math.max(1,frames.length-1),y=v=>150-120*v/max;
    const path=values.map((v,i)=>Number.isFinite(v)?`${i===0||!Number.isFinite(values[i-1])?'M':'L'}${x(i)},${y(v)}`:'').join(' ');
    return `<figure class="control-chart"><svg viewBox="0 0 730 180" role="img" aria-label="프레임별 RAW G 평균과 AE 목표"><path d="M45 25V150H695" fill="none" stroke="#cdd7d1"/><path d="M45 ${y(target)}H695" stroke="#ba6a3c" stroke-dasharray="5 4"/><text x="48" y="${y(target)-5}">목표 ${number(target,4)}</text><text x="8" y="30">${number(max,2)}</text><text x="25" y="153">0</text><path d="${path}" stroke="#326850" stroke-width="2" fill="none"/><circle cx="${x(s.frame)}" cy="${y(values[s.frame]||0)}" r="4" fill="#326850"/><text x="45" y="172">프레임 0</text><text x="640" y="172">${frames.length-1}</text></svg><figcaption>관측 RAW에서 black level을 뺀 정규화 G 평균 · 목표선은 정책 입력</figcaption></figure>`;
  }
  const ns=v=>Number.isSafeInteger(v)?`${v.toLocaleString('en-US')} ns`:v==null?'—':'정확한 정수는 원본 파일 확인';
  const gains=v=>Array.isArray(v)?v.map(n=>number(n,5)).join(' / '):'—';
  function traceView() {
    const run=find('runs',s.run),out=summary(),frames=out?.frames||[],trace=out?.control_trace;
    if(!run)return '<p class="muted">등록된 프로그램을 실행하면 프레임별 제어 이력이 여기에 표시됩니다.</p>';
    if(run.status!=='succeeded')return `<p data-control-run-status="${esc(run.status)}">${badge(run.status)} ${esc(typeof run.error==='string'?run.error:json(run.error||''))}</p>`;
    if(!frames.length||!trace)return '<p role="alert">이 실행에 폐루프 제어 이력이 없습니다. 기록 원본을 확인하세요.</p>';
    s.frame=Math.min(s.frame,frames.length-1);
    const last=frames.at(-1),ae=out.control_program.policy.ae;
    const residual=ae.enabled?last.statistics.ae.value-ae.target_normalized:null;
    const lastState=last.control_proposals.ae.status;
    const explanation=lastState==='actuator_grid_hold'?'범위·격자로 값 유지 · 목표 도달 판정 아님':lastState==='within_deadband'?'정책의 Deadband 안':lastState;
    const f=frames[s.frame], commands=trace.commands.filter(c=>c.source_frame_index===s.frame);
    const links=(run.artifacts||[]).filter(a=>['raw_frames.npy','rgb_frames.npy'].includes(a.name));
    return `<div data-control-result="${esc(run.id)}"><div class="control-result-title"><div><h3>${esc(run.name)}</h3><p>${frames.length} 프레임 · 명령 ${trace.commands.length}개 · ${esc(trace.mode.shutter)} shutter</p></div>${badge('succeeded','시뮬레이션 계산 완료')}</div><p class="callout">이력의 지연은 선언된 시뮬레이션 입력입니다. 실제 하드웨어 시간 정확도와 실물 예측 정확도는 미확립입니다. 목표 잔차나 조절 간격에 따른 유지 상태도 결과에 남습니다.</p><div class="control-final" data-control-final><strong>마지막 RAW G ${number(last.statistics.ae.value,6)}</strong><span>${ae.enabled?'목표 대비 잔차 '+number(residual,6):'AE 고정 · 목표 미사용'}</span><span>${esc(explanation)}</span></div>${graph(frames,out.control_program.policy.ae.target_normalized)}<div class="control-frame-layout"><div><div class="form-field"><label for="control-frame">프레임 선택</label><select id="control-frame" data-control-field="frame">${frames.map((row,i)=>`<option value="${i}" ${s.frame===i?'selected':''}>${i} · ${esc(row.frame_id)}</option>`).join('')}</select></div><img class="control-preview" src="${esc(f.preview_url)}" alt="선택 프레임 RGB 미리보기" loading="lazy"><p class="muted">RAW ${number(f.statistics.ae.value,6)} · 유효 AWB 셀 ${f.statistics.awb.valid_cells}</p></div><div class="table-wrap"><table class="control-frame-facts"><thead><tr><th>항목</th><th>이번 프레임 적용</th><th>이 프레임에서 발행한 명령</th></tr></thead><tbody><tr><td>노출</td><td data-control-applied="exposure">${ns(f.applied.exposure_ns)}</td><td>${ns(f.control_proposals.ae.commanded?.exposure_ns)}</td></tr><tr><td>Readout gain</td><td>${number(f.applied.gain,5)}</td><td>${number(f.control_proposals.ae.commanded?.gain,5)}</td></tr><tr><td>WB R / G / B</td><td>${gains(f.applied.wb_gains_rgb)}</td><td>${gains(f.control_proposals.awb.commanded?.wb_gains_rgb)}</td></tr><tr><td>적용 시각</td><td>${ns(f.latch_at_ns)}</td><td>아래 명령별 반영 시각 참조</td></tr><tr><td>적용 근거</td><td>AE: ${esc(f.applied_sources.ae.command_id||'초기값')}<br>AWB: ${esc(f.applied_sources.awb.command_id||'초기값')}</td><td>${esc(f.control_proposals.ae.status)} / ${esc(f.control_proposals.awb.status)}</td></tr></tbody></table></div></div><div class="table-wrap"><table data-control-commands><thead><tr><th>발행 명령</th><th>발행 시각</th><th>적용 프레임 / 시각</th><th>최종 상태</th></tr></thead><tbody>${commands.length?commands.map(c=>`<tr><td>${esc(c.command_id)}</td><td>${ns(c.issued_at_ns)}</td><td>${esc(c.applied_frame_id||'—')}<br>${ns(c.applied_at_ns)}</td><td>${esc(c.status)}</td></tr>`).join(''):'<tr><td colspan="4">이 프레임에서는 새 명령을 발행하지 않았습니다.</td></tr>'}</tbody></table></div>${details('선택 프레임 · 관측 통계 · 연속 요청값 · 양자화 · 적용 근거',f)}<div class="control-actions">${button('export-trace','전체 제어 이력 JSON')}<button class="button secondary small" data-action="replay-from-run" data-id="${esc(run.id)}">이 RAW를 다른 ISP로 재처리 →</button>${links.map(a=>`<a class="button secondary small" href="${esc(a.url)}" download>${esc(a.name)} ↓</a>`).join('')}<a class="button secondary small" href="/api/v1/runs/${encodeURIComponent(run.id)}/bundle" download>근거 ZIP ↓</a></div>${details('원본 실행 기록',run)}</div>`;
  }
  function view() {
    needed();
    const program=find('control_programs',s.program);
    return `${heading('CAMERA FEEDBACK CONTROL','관측에서 다음 촬영까지.','RAW 통계로 AE와 AWB를 조절하고, 요청·명령·실제 적용을 프레임별로 추적합니다.')}<div class="control-workspace"><p class="callout">기존 시간 장면을 선택해 정책과 촬영 프로그램을 등록하세요. 제어 가능한 구간은 Lens·Sensor·ISP의 명시된 유효 범위 안에 있어야 합니다. 기본 정책의 Gain 8이 모델 범위를 넘으면 정책을 수정하거나 근거가 있는 새 모델을 등록해야 합니다.</p>${s.error?`<section class="panel" data-control-error role="alert"><div class="panel-content"><h2>설정 확인이 필요합니다</h2><p>${esc(s.error)}</p>${s.preflight?details('전체 실행 전 검사 보고서',s.preflight):''}</div></section>`:''}<div class="two-columns"><section class="panel"><div class="panel-header"><div><span class="eyebrow">01 · POLICY</span><h2>제어 정책</h2><p>AE·AWB의 범위, 통계 영역과 반영 지연</p></div></div><div class="panel-content">${select('assembly','카메라 구성','assemblies',s.assembly)}${select('scene','시간 장면','scene_sequences',s.scene)}${button('policy-template','새 정책 초안',!s.scene)}${select('policy','등록된 정책','control_policies',s.policy)}${button('copy-policy','선택한 정책으로 새 버전 작성',!s.policy)}<p class="muted">편집 값은 정책 등록 후 새 프로그램에 사용할 수 있습니다. 기존 촬영 프로그램은 등록 당시 정책을 유지합니다.</p>${parameters()}${editor('policyDraft','정책 JSON · 출처 및 고급 설정')}<div class="control-actions">${button('register-policy','정책 등록',!s.policyDraft,true)}</div></div></section><section class="panel"><div class="panel-header"><div><span class="eyebrow">02 · PROGRAM</span><h2>촬영 프로그램</h2><p>장면·정책·카메라·촬영 시각을 버전으로 고정</p></div></div><div class="panel-content">${input('count','초안 프레임 수 · 1–64',s.count,'number')}${button('program-template','촬영 프로그램 초안',!s.assembly||!s.scene||!s.policy)}<p class="muted">초안은 장면 시간 범위 안에 Global shutter 프레임을 배치합니다. Rolling shutter와 초기 WB·촬영 시각은 JSON에서 지정할 수 있습니다.</p>${editor('programDraft','프로그램 JSON · 시각과 초기 설정')}<div class="control-actions">${button('check','전체 제어 범위 검사',!s.programDraft,true)}</div>${program?details('선택한 등록 프로그램',program):''}</div></section></div>${checks()}<section class="panel"><div class="panel-header"><div><span class="eyebrow">03 · ACQUISITION</span><h2>등록된 프로그램 실행</h2><p>프로그램에 고정된 시험 정의와 재현 가능한 noise seed 사용</p></div></div><div class="panel-content"><div class="control-fields">${select('program','촬영 프로그램','control_programs',s.program)}${input('seed','Noise seed',s.seed,'number')}</div>${program?`<p class="muted">${esc(find('assemblies',program.assembly_id)?.name||program.assembly_id)} · ${program.payload?.frames.length||program.display_summary?.frame_count} 프레임</p>`:''}${button('run','시험 등록 및 실행',!s.program,true)}</div></section><section class="panel"><div class="panel-header"><div><h2>촬영 결과 · 제어 이력</h2><p>실행 기록은 새로 열어도 동일한 원본 근거를 불러옵니다.</p></div></div><div class="panel-content"><div class="form-field"><label for="control-run">저장된 실행</label><select id="control-run" data-control-field="run"><option value="">실행 선택</option>${runs().map(r=>`<option value="${esc(r.id)}" ${r.id===s.run?'selected':''}>${esc(r.name||r.id)} · ${esc(r.status)} · ${esc(r.id.slice(-6))}</option>`).join('')}</select></div>${traceView()}</div></section></div>`;
  }
  document.addEventListener('input',event=>{
    const el=event.target.closest('[data-control-field]');if(!el)return;
    const field=el.dataset.controlField;
    if(['frame','run','program','policy','assembly','scene'].includes(field))return; // select change handles these once
    invalidate();
    if(field==='parameter'){
      try{const d=parse(s.policyDraft),parts=el.dataset.path.split('.'),key=parts.pop();let parent=d;for(const k of parts)parent=parent[k];parent[key]=el.tagName==='SELECT'?el.value==='true':el.value===''?null:Number(el.value);s.policyDraft=json(d);const editor=document.getElementById('control-policyDraft');if(editor)editor.value=s.policyDraft;}
      catch(e){s.error=e.message;toast(e.message,true);}return;
    }
    s[field]=el.value;
  });
  document.addEventListener('change',event=>{
    const el=event.target.closest('[data-control-field]');if(!el)return;
    const field=el.dataset.controlField;
    if(field==='frame'){s.frame=Number(el.value);draw();return;}
    if(!['run','program','policy','assembly','scene'].includes(field)){if(field==='policyDraft')draw();return;}
    invalidate();s[field]=el.value;
    if(field==='run'){s.frame=0;draw();return;}
    if(['assembly','scene','policy'].includes(field)){s.program='';s.programDraft='';}
    if(field==='scene')s.policyDraft='';
    if(field==='program'){const p=find('control_programs',s.program);if(p){s.assembly=p.assembly_id;s.scene=p.scene_sequence_id;s.policy=p.control_policy_id;}s.programDraft='';}
    draw();
  });
  async function reload(){await refresh({renderPage:false});}
  document.addEventListener('click',event=>{
    const el=event.target.closest('[data-control-action]');if(!el||s.pending)return;
    busy(el,async()=>{
      const action=el.dataset.controlAction,rev=s.revision;
      s.pending=true;s.error='';s.preflight=null;
      try{
        if(action==='policy-template'){
          const draft=await api('/templates/control-policy?'+new URLSearchParams({scene_sequence_id:s.scene}));
          if(rev===s.revision){invalidate();s.policyDraft=json(draft);}
        }else if(action==='copy-policy'){
          const record=find('control_policies',s.policy);if(!record)throw new Error('정책을 선택하세요.');
          const {id,content_hash,created_at,artifacts,approval_status,physical_validation,...draft}=record;
          draft.parents=[id];draft.revision=record.revision+'-next';invalidate();s.policyDraft=json(draft);
        }else if(action==='register-policy'){
          parse(s.policyDraft);const text=s.policyDraft;
          const record=await api('/control_policies/import',{document_text:text});await reload();
          if(rev===s.revision){s.policy=record.id;s.program='';s.programDraft='';invalidate();}toast('출처와 정책 원본을 새 버전으로 등록했습니다.');
        }else if(action==='program-template'){
          const n=Number(s.count);if(!Number.isInteger(n)||n<1||n>64)throw new Error('프레임 수는 1–64 사이 정수여야 합니다.');
          const draft=await api('/templates/control-program?'+new URLSearchParams({assembly_id:s.assembly,scene_sequence_id:s.scene,control_policy_id:s.policy,frame_count:n}));
          if(rev===s.revision){invalidate();s.programDraft=json(draft);}
        }else if(action==='check'){
          const text=s.programDraft,spec=parse(text);
          const checked=await api('/control_programs/check',spec);
          if(rev===s.revision&&text===s.programDraft)s.check=checked;else toast('초안이 바뀌어 이전 검사 결과를 적용하지 않았습니다.');
        }else if(action==='register-program'){
          if(!s.check)throw new Error('현재 프로그램의 범위 검사를 먼저 실행하세요.');
          const record=await api('/control_programs/import',{document_text:s.programDraft});await reload();
          if(rev===s.revision){s.program=record.id;invalidate();}toast('촬영 프로그램과 참조 모델을 고정했습니다.');
        }else if(action==='run'){
          const program=find('control_programs',s.program),seed=Number(s.seed);
          if(!program)throw new Error('등록된 촬영 프로그램을 선택하세요.');
          if(s.seed.trim()===''||!Number.isInteger(seed)||seed<0||seed>2147483647)throw new Error('Noise seed는 0–2147483647 정수여야 합니다.');
          const template=await api('/templates/closed-loop-test?'+new URLSearchParams({control_program_id:program.id}));
          const existing=records('tests').find(t=>t.stimulus?.control_program_id===program.id&&t.metric_id===template.metric_id&&t.unit===template.unit&&equal(t.observation_method??null,template.observation_method??null)&&equal(t.stimulus,template.stimulus));
          const test=existing||await api('/tests',template);
          const run=await api('/runs',{assembly_id:program.assembly_id,test_id:test.id,plugin_id:'closed_loop_scene_v1',control_program_id:program.id,parameters:{seed}});
          await reload();if(rev===s.revision){s.run=run.id;s.frame=0;}toast('폐루프 촬영을 제출했습니다.');
        }else if(action==='export-trace'){
          const out=summary();if(out)downloadObject(out.control_trace,'control-trace-'+s.run+'.json');
        }
      }catch(e){if(rev===s.revision){s.error=e.message;s.preflight=e.preflight||null;}throw e;}
      finally{s.pending=false;draw();}
    });
  });
  function selectRun(id){s.run=id;s.frame=0;s.initialized=true;draw();}
  return {view,needed,selectRun};
};

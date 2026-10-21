'use strict';
(function(global){
  const ROOT='/api/v1/calibration-lab',COLLECTION='/color-calibrations';
  const list=x=>Array.isArray(x)?x:[],finite=x=>typeof x==='number'&&Number.isFinite(x);
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const json=x=>JSON.stringify(x,null,2),fmt=x=>finite(x)?new Intl.NumberFormat('ko-KR',{maximumSignificantDigits:5}).format(x):x===null||x===undefined?'—':typeof x==='object'?esc(JSON.stringify(x)):esc(x);
  const labels={pass:'수치 기준 충족',fail:'수치 기준 미충족',inconclusive:'판정 보류',not_evaluated:'미평가',not_established:'물리 검증 미확립',synthetic:'합성 입력',measured:'실측 입력',fitted:'추정 완료',absolute_qe:'절대 QE',relative_spectral_response:'상대 분광 응답'};
  const label=x=>labels[x]||x||'미기록';
  function parse(text){const value=JSON.parse(text);if(!value||Array.isArray(value)||typeof value!=='object')throw Error('JSON 객체를 입력하세요.');const tokens=text.match(/"(?:\\.|[^"\\])*"|[{}\[\]:,]/g)||[],stack=[];for(let i=0;i<tokens.length;i++){const token=tokens[i];if(token==='{')stack.push(new Set());else if(token==='[')stack.push(null);else if(token==='}'||token===']')stack.pop();else if(token[0]==='"'&&tokens[i+1]===':'){const key=JSON.parse(token),keys=stack.at(-1);if(keys?.has(key))throw Error('중복된 JSON 키: '+key);keys?.add(key);}}function check(x){if(typeof x==='number'&&(!Number.isFinite(x)||(Number.isInteger(x)&&!Number.isSafeInteger(x))))throw Error('유한한 값과 안전한 정수 범위를 사용하세요.');if(x&&typeof x==='object')Object.values(x).forEach(check);}check(value);return value;}
  const badge=(value)=>`<span class="badge ${['pass','fail','inconclusive'].includes(value)?value:''}">${esc(label(value))}</span>`;
  const detail=(title,value)=>`<details><summary>${esc(title)}</summary><pre>${esc(json(value)||'미기록')}</pre></details>`;
  const empty=text=>`<p class="empty">${esc(text)}</p>`;
  function table(headers,rows){return rows.length?`<div class="table-scroll"><table><thead><tr>${headers.map(h=>'<th>'+esc(h)+'</th>').join('')}</tr></thead><tbody>${rows.map(r=>'<tr>'+r.map(v=>'<td>'+fmt(v)+'</td>').join('')+'</tr>').join('')}</tbody></table></div>`:empty('아직 계산 결과가 없습니다.');}
  function plot(x,series,xUnit,yUnit){
    if(x.length<2||!x.every(finite)||!series.length||series.some(s=>s.values.length!==x.length||!s.values.every(finite)))return '';
    const all=series.flatMap(s=>s.values),lo=Math.min(...all,0),hi=Math.max(...all),xmin=Math.min(...x),xmax=Math.max(...x);if(xmax===xmin)return '';
    const range=hi>lo?hi-lo:1,indices=[...new Set([...x.map((_,i)=>i).filter(i=>i%Math.max(1,Math.ceil(x.length/512))===0),x.length-1])];
    let svg='<svg class="plot" viewBox="0 0 640 260" role="img" aria-label="'+esc(yUnit)+' 곡선"><path d="M55 22 V211 H615" fill="none" stroke="#a9bbb2"/>';
    for(let i=0;i<5;i++){const y=22+i*189/4;svg+=`<text x="48" y="${y+4}" text-anchor="end">${esc(Number(hi-i*range/4).toPrecision(3))}</text>`;}
    for(const s of series){const points=indices.map(i=>`${55+(x[i]-xmin)/(xmax-xmin)*560},${211-(s.values[i]-lo)/range*189}`).join(' ');svg+=`<polyline points="${points}" fill="none" stroke="${s.color}" stroke-width="2"/>`;}
    svg+=`<text x="55" y="232">${esc(xmin)}</text><text x="615" y="232" text-anchor="end">${esc(xmax)}</text><text x="330" y="251" text-anchor="middle">${esc(xUnit)}</text><text x="60" y="15">${esc(yUnit)}</text></svg>`;
    return svg+`<div class="legend">${series.map(s=>`<span><i style="border-color:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>`;
  }
  function qeContent(record){const profile=record?.qe?.profile;if(!profile)return empty('파장별 교정광량과 전자 신호에서 R/G/B 응답을 추정합니다.');const wave=list(profile.wavelength_nm),values=list(profile.qe||profile.relative_spectral_response),channels=list(profile.channels).length?profile.channels:['R','G','B'],relative=profile.response_kind==='relative_spectral_response';const curves=channels.map((c,i)=>({name:c,color:['#c75040','#21845d','#386db2'][i%3],values:values.map(row=>row[i])}));return `${badge(profile.response_kind)}<p class="scope">${relative?'상대 분광 응답입니다. 정규화된 곡선으로 절대 광전자 수나 RAW DN을 예측할 수 없습니다.':'파장별 교정된 센서면 광량으로 얻은 절대 QE입니다. 색상 차트의 CCM으로 추정한 값이 아닙니다.'}</p>${plot(wave,curves,'파장 (nm)',relative?'상대 응답':'QE (e⁻ / photon)')}${table(['파장 (nm)',...channels],wave.map((w,i)=>[w,...list(values[i])]))}${detail('QE 조건·광량 기준·출처',{reference_plane:profile.reference_plane,conditions:profile.conditions,radiometry:profile.radiometry,normalization:profile.normalization,included_effects:profile.included_effects,profile_sha256:profile.profile_sha256})}`;}
  function metricRows(value,prefix=''){return Object.entries(value||{}).flatMap(([key,v])=>{const name=prefix?prefix+'.'+key:key;return v&&typeof v==='object'&&!Array.isArray(v)?metricRows(v,name):[[name,v]];});}
  function compareMetrics(before,after){const a=new Map(metricRows(before)),b=new Map(metricRows(after));return table(['지표','기준','후보'],[...new Set([...a.keys(),...b.keys()])].map(k=>[k,a.get(k),b.get(k)]));}
  function rawContent(record){const raw=record?.raw_validation,candidate=raw?.candidate||(raw?.status?raw:null);if(!candidate)return empty('독립 광량·차트로 CCM 이전 RAW를 검증합니다.');const channels=list(candidate.channels),baseline=raw.baseline;return `${badge(candidate.status)}<p class="scope">RAW는 CCM 이전의 센서 신호입니다. 뒤 단계의 CCM을 변경해도 여기의 RAW 오차는 없어지지 않습니다.</p>${candidate.reason?'<p class="notice warning">'+esc(candidate.reason)+'</p>':''}${candidate.metrics?`<h3>채널별 RAW 오차 · DN</h3>${table(['채널','기준 RMSE','후보 RMSE','후보 평균 편차'],channels.map((c,i)=>[c,baseline?.metrics?.channel_rmse_dn?.[i],candidate.metrics.channel_rmse_dn?.[i],candidate.metrics.channel_bias_dn?.[i]]))}${compareMetrics(baseline?.metrics,candidate.metrics)}${table(['패치','측정 RAW (DN)','기준 예측 (DN)','후보 예측 (DN)'],list(candidate.patch_ids).map((id,i)=>[id,candidate.measured_raw_rgb_dn?.[i],baseline?.predicted_raw_rgb_dn?.[i],candidate.predicted_raw_rgb_dn?.[i]]))}`:''}${plot(list(candidate.patch_ids).map((_,i)=>i+1),channels.map((c,i)=>({name:c+' 후보 잔차',color:['#c75040','#21845d','#386db2'][i%3],values:list(candidate.residual_dn).map(row=>row[i])})),'패치 번호','예측 − 측정 (DN)')}${detail('RAW 기준·후보 관측값과 채널별 근거',raw)}`;}
  function ccmContent(record){const ccm=record?.ccm,profile=ccm?.profile;if(!profile)return (ccm?.status||ccm?.validation?.status)?`${badge(ccm.status||ccm.validation.status)}<p class="notice warning">${esc(ccm.reason||ccm.validation?.reason||'이 입력으로 CCM을 계산하지 않았습니다.')}</p>`:empty('RAW 모델을 고정한 뒤, 별도 학습 차트로 ISP의 3 × 3 색 행렬을 구합니다.');const validation=ccm.validation,comparison=validation?.comparison,outputs=profile.target_space==='xyz'?['X','Y','Z']:['R','G','B'];return `${badge(validation?.status)}<p class="help">독립 색상 차트에서 기존 행렬과 새 행렬의 ISP 출력을 비교합니다. RAW 모델과 QE는 이 단계에서 갱신하지 않습니다.</p>${profile.target_space==='camera_linear_rgb'?'<p class="scope">camera_linear_rgb는 색도 기준 공간이 아니므로 ΔE를 계산하지 않습니다. 아래 값은 해당 선형 RGB 공간의 RMSE입니다.</p>':''}<h3>독립 검증 · 색 오차</h3>${compareMetrics(comparison?.metrics?.baseline,comparison?.metrics?.candidate)}<h3>새 CCM</h3>${table(['입력 → 출력',...outputs],list(profile.matrix).map((row,i)=>[['R','G','B'][i],...row]))}<p class="muted">${esc(profile.application||'행 벡터 × 행렬')} · 목표 공간 ${esc(profile.target_space||'미기록')}</p>${comparison?table(['패치','기준 출력','후보 출력','목표 출력'],list(comparison.target_rgb).map((r,i)=>[i+1,comparison.baseline?.[i],comparison.candidate?.[i],r])):''}${detail('CCM 학습·독립 검증과 고정 조건',ccm)}`;}
  function rawOriginalFile(record,role){
    if(!['training','validation'].includes(role))return null;
    const checksum=record.evidence?.raw_originals?.[role]?.sha256;
    if(typeof checksum!=='string'||!/^[a-f0-9]{64}$/.test(checksum))return null;
    const matching=Object.entries(record.files||{}).filter(([name,value])=>{
      const hash=typeof value==='string'?value:value?.sha256;
      return hash===checksum&&/\.npy$/i.test(name)&&name.split('/').every(part=>part!=='.'&&part!=='..'&&/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(part));
    }).map(([name])=>name);
    // Prefer the current flat key, then an explicitly retained legacy key.
    for(const name of ['raw-'+role+'.npy','originals/'+role+'.npy'])if(matching.includes(name))return name;
    return matching.length===1?matching[0]:null;
  }
  function rawOriginals(record){const originals=record.evidence?.raw_originals||{},base=ROOT+COLLECTION+'/'+encodeURIComponent(record.id);return `<h3>원본 RAW 취득 근거</h3>${table(['역할','패치 수','프레임 수','CFA / bit','원본 SHA256'],Object.entries(originals).map(([role,v])=>[role,v.patch_count,v.frame_count,[v.cfa,v.bit_depth],v.sha256]))}<div class="actions">${Object.keys(originals).map(role=>{const filename=rawOriginalFile(record,role);return filename?`<a class="button" href="${base}/files/${filename.split('/').map(encodeURIComponent).join('/')}" download>${role==='training'?'학습':'독립 검증'} RAW 원본 ↓</a>`:'';}).join('')}</div>`;}
  function resultView(record,db){if(!record)return '';const base=ROOT+COLLECTION+'/'+encodeURIComponent(record.id),limitations=[...list(record.limitations),...list(record.qe?.profile?.limitations),...list(record.raw_validation?.candidate?.limitations),...list(record.ccm?.limitations)];return `<section class="card"><p class="eyebrow">PRESERVED RESULT</p><h2>${esc(record.name||'색 응답 보정 결과')}</h2><p>${badge(record.source_kind)}${badge(record.physical_validation||'not_established')}</p><p class="identity">${esc(record.id)}<br>입력 SHA256 ${esc(record.input_sha256||'미기록')}</p>${record.source_kind==='synthetic'?'<p class="notice warning">합성 데이터로 계산 경로를 확인한 결과입니다. 실제 카메라 실측 검증이 아닙니다.</p>':''}<div class="actions"><a class="button" href="${base}/files/report.html" target="_blank" rel="noopener">표준 보고서 ↗</a><a class="button" href="${base}/files/report.json" download>결과 JSON ↓</a><a class="button" href="${base}/files/qe-profile.json" download>QE 프로파일 ↓</a><a class="button" href="${base}/files/ccm-profile.json" download>CCM 프로파일 ↓</a><a class="button" href="${base}/bundle" download>근거 ZIP ↓</a><a class="button" href="/calibration-db-color.html?calibration=${encodeURIComponent(record.id)}${db?.run_id?'&amp;source_run='+encodeURIComponent(db.run_id)+'&amp;candidate='+encodeURIComponent(db.candidate_id)+'&amp;condition='+encodeURIComponent(db.condition_id):''}">DB 모델 적용 · Macbeth 재실행 ↗</a></div>${rawOriginals(record)}${detail('적용 한계', [...new Set(limitations)])}</section>`;}
  function view(s){const items=s.items;return `${s.error?'<p class="notice error" role="alert">'+esc(s.error)+'</p>':''}${s.notice?'<p class="notice" role="status">'+esc(s.notice)+'</p>':''}<section class="card saved"><label>저장된 색 응답 보정<select data-field="selected"><option value="">결과 선택</option>${items.map(r=>`<option value="${esc(r.id)}" ${r.id===s.selected?'selected':''}>${esc(r.name||r.id)}</option>`).join('')}</select></label><button data-action="refresh" ${s.busy?'disabled':''}>목록 새로고침</button></section><div class="layout"><aside><section class="card"><p class="eyebrow">INPUT CONTRACT</p><h2>측정 입력과 기준 모델</h2><p class="help">교정광량·RAW 검증·CCM 학습·CCM 독립 검증을 입력합니다. 실제 측정값과 합성 예제는 출처로 구분합니다. 캠페인 연결은 입력 JSON의 campaign_id를 명시한 경우에만 적용합니다.</p>${s.campaign?'<p class="identity">돌아갈 캠페인 '+esc(s.campaign)+'</p>':''}<div class="actions"><button data-action="template" ${s.busy||!s.template?'disabled':''}>합성 예제 불러오기</button><button data-action="download-input" ${!s.draft?'disabled':''}>입력 JSON ↓</button></div><form data-form="execute"><fieldset ${s.busy?'disabled':''}><label>JSON 파일 가져오기<input type="file" data-file="input" accept=".json,application/json"></label><label>보정 입력 JSON<textarea data-field="draft" spellcheck="false">${esc(s.draft)}</textarea></label><p class="help">파일을 불러오는 동작은 계산을 제출하지 않습니다. 실행 버튼을 누르면 입력과 결과가 새 기록으로 보존됩니다.</p>${dbSettings(s)}<button class="primary full" type="submit" ${!s.draft||s.reading?'disabled':''}>${s.busy?'계산 및 기록 중…':'QE 추정 → RAW 검증 → CCM 보정'}</button></fieldset></form><p class="scope">ColorChecker 색상만으로 파장별 QE를 식별할 수 없습니다. 상대 QE만 있으면 절대 RAW 예측을 진행할 수 없습니다.</p></section></aside><div>${resultView(s.record,s.db)}${s.record&&s.db?.enabled?'<button class="button primary" data-action="db-existing">저장된 보정으로 DB 자동 비교</button>':''}${autoResultView(s)}<section class="card stage"><p class="eyebrow">01 / SPECTRAL SENSOR RESPONSE</p><h2>QE · 파장별 센서 응답</h2>${qeContent(s.record)}</section><section class="card stage raw"><p class="eyebrow">02 / BEFORE ISP CCM</p><h2>RAW · 센서 모델 검증</h2>${rawContent(s.record)}</section><section class="card stage"><p class="eyebrow">03 / ISP COLOR TRANSFORM</p><h2>CCM · 출력 색상 보정</h2>${ccmContent(s.record)}</section></div></div>`;}
  function createAPI(request){return async(path,body)=>{const headers={};if(body!==undefined){const r=await request('/api/v1/session');if(!r.ok)throw Error('로컬 세션을 확인하지 못했습니다.');const session=await r.json();if(!session.token)throw Error('로컬 세션 토큰이 없습니다.');headers['X-Platform-Token']=session.token;headers['Content-Type']='application/json';}const endpoint=path.startsWith('/db-color-replays')?'/api/v1/db-design/color-replays'+path.slice(17):ROOT+path;const response=await request(endpoint,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:json(body)});let value;try{if(path==='/color-template'&&response.ok){const text=await response.text();return {template:parse(text),input_json:text};}value=await response.json();}catch{throw Error('서버 응답을 읽지 못했습니다 ('+response.status+').');}if(!response.ok){const message=value.error||value.detail||value.message||value;throw Error(typeof message==='string'?message:json(message));}return value;};}
  function dbSettings(s) {
    const d=s.db||{enabled:false,run_id:"",candidate_id:"",condition_id:"",target_camera_id:"synthetic-db-calibration"};
    return `<div class="holdout"><h3>보정 후 자동 DB 비교</h3><label><input type="checkbox" data-db="enabled" ${d.enabled?'checked':''}> DB 적용·가상 실측 비교·오차 보고서 자동 생성</label><p class="help">원래 응답이 정의된 합성 표준 예제를 지원합니다. 일반 실측 입력에는 별도 관측값이 필요합니다. 새 비교 RAW는 피팅에 사용하지 않습니다.</p>${[['run_id','원본 DB 실행 ID'],['candidate_id','DB 후보 ID'],['condition_id','DB 조건 ID'],['target_camera_id','새 합성 카메라 ID']].map(([key,label])=>`<label>${label}<input data-db="${key}" value="${esc(d[key])}" ${d.enabled?'':'disabled'} ${d.enabled&&key!=='condition_id'?'required':''}></label>`).join('')}</div>`;
  }
  function autoResultView(s) {
    const job=s.autoJob;
    if(!job)return '';
    const link='/calibration-db-color.html?run='+encodeURIComponent(job.id);
    let out=`<section class="card"><p class="eyebrow">CALIBRATION → DB → AUTOMATIC HOLDOUT</p><h2>보정 후 자동 DB 검증</h2><p class="identity">${esc(job.id)}</p><a class="button" href="${link}">DB 모델·비교 결과 ↗</a>`;
    if(['queued','running'].includes(job.status))return out+`<p class="notice" role="status">자동 비교·보고서 생성 중 · ${esc(job.progress?.message || job.status)}</p></section>`;
    if(job.status!=='succeeded')return out+`<p class="notice error">${esc(json(job.error)||'자동 비교가 완료되지 않았습니다.')}</p></section>`;
    const ui=global.CameraDBColorCalibration;
    return out+(ui?ui.holdoutView(job.result?.summary,s):empty('DB 비교 결과 링크에서 보고서를 확인하세요.'))+'</section>';
  }
  function createController(deps) {
    const api=deps.api, source=deps.dbSource||{};
    const s={items:[],template:null,record:null,selected:'',campaign:deps.campaign||'',draft:'',busy:false,reading:false,error:'',notice:'',autoJob:null,field:'center',stage:'isp',db:{enabled:!!(source.run_id&&source.candidate_id),run_id:source.run_id||'',candidate_id:source.candidate_id||'',condition_id:source.condition_id||'',target_camera_id:'synthetic-db-calibration'}};
    let selectionEpoch=0,draftEpoch=0,listEpoch=0,autoEpoch=0,timer=null;
    const render=()=>deps.render?.(s);
    function cancelAuto(){autoEpoch++;if(timer!==null)deps.clearTimer?.(timer);timer=null;}
    async function refresh(){const token=++listEpoch,value=await api(COLLECTION);if(token!==listEpoch)return;s.items=list(value.items);render();}
    async function watch(job,id=s.selected){
      cancelAuto();const token=autoEpoch;
      async function load(value){
        try{
          const row=value || await api('/db-color-replays/'+encodeURIComponent(job.id));
          if(token!==autoEpoch || s.selected!==id)return;
          const calibration=row.model_snapshot?.color_calibration_id || row.result?.summary?.color_calibration_id;
          if(calibration&&calibration!==id)throw Error('자동 비교의 보정 ID가 선택한 기록과 다릅니다.');
          if(row.id!==job.id)throw Error('자동 비교의 실행 ID가 다릅니다.');
          s.autoJob=row;render();
          if(['queued','running'].includes(row.status)&&deps.setTimer)timer=deps.setTimer(()=>load(),3000);
        }catch(e){if(token===autoEpoch){s.error=e.message;render();}}
      }
      await load(job.status?job:null);
    }
    async function associated(id,token){
      if(!deps.autoResults)return;
      const value=await api('/db-color-replays?color_calibration_id='+encodeURIComponent(id));
      if(token!==selectionEpoch)return;
      const rows=list(value.items).filter(r=>!s.db.run_id || (r.source?.run_id===s.db.run_id&&r.source?.candidate_id===s.db.candidate_id&&(!s.db.condition_id||r.source?.condition_id===s.db.condition_id)));
      if(rows.length)await watch({id:rows[0].id},id);
    }
    async function select(id){
      cancelAuto();const token=++selectionEpoch;s.selected=id;s.record=null;s.autoJob=null;s.error='';render();
      if(!id){deps.onSelect?.('');return;}
      try{const record=await api(COLLECTION+'/'+encodeURIComponent(id));if(token!==selectionEpoch||id!==s.selected)return;if(record.id!==id)throw Error('결과 ID가 요청과 다릅니다.');s.record=record;deps.onSelect?.(id);await associated(id,token);}catch(e){if(token===selectionEpoch)s.error=e.message;}render();
    }
    async function init(){try{const [template]=await Promise.all([api('/color-template'),refresh()]);s.template=template.payload||template.template||template;s.templateJSON=template.input_json||json(s.template);if(deps.selected)await select(deps.selected);}catch(e){s.error=e.message;}render();}
    function modelConfig(){
      for(const key of ['run_id','candidate_id','target_camera_id'])if(!s.db[key].trim())throw Error('자동 비교에 사용할 DB 실행·후보와 새 합성 카메라 ID를 입력하세요.');
      if(!/^synthetic-db-[A-Za-z0-9_.-]+$/.test(s.db.target_camera_id.trim()))throw Error('카메라 ID는 synthetic-db-로 시작해야 합니다.');
      const source={run_id:s.db.run_id.trim(),candidate_id:s.db.candidate_id.trim()};if(s.db.condition_id.trim())source.condition_id=s.db.condition_id.trim();
      return {name:'Calibration Lab · 자동 Macbeth 비교',source,binding:{mode:'synthetic_model_transfer',angular_assumption:'angle_independent',green_sites:'shared_green',target_camera_id:s.db.target_camera_id.trim()}};
    }
    async function perform(action){
      if(s.busy)return;s.error='';s.notice='';
      if(action==='template'){if(!s.template)return;const text=s.templateJSON||json(s.template);parse(text);s.draft=text;draftEpoch++;s.notice='합성 예제를 불러왔습니다. 실행 전까지 계산하지 않습니다.';render();return;}
      if(action==='download-input'){try{parse(s.draft);deps.download?.(s.draft,'color-calibration-input.json');}catch(e){s.error=e.message;}render();return;}
      s.busy=true;render();
      try{
        if(action==='refresh'){await refresh();if(s.selected)await associated(s.selected,selectionEpoch);return;}
        if(!['execute','db-existing'].includes(action))return;
        if(s.reading)throw Error('입력 파일을 읽는 중입니다.');
        const token=selectionEpoch;
        let record,flow;
        if(s.db.enabled || action==='db-existing'){
          const body={model:modelConfig()};
          if(action==='db-existing'){if(!s.record)throw Error('저장된 보정을 먼저 선택하세요.');body.color_calibration_id=s.record.id;}else {parse(s.draft);body.calibration_json=s.draft;}
          flow=await api('/color-db-workflows',body);record=flow.calibration;
        }else {parse(s.draft);record=await api(COLLECTION,{input_json:s.draft});}
        if(!record?.id)throw Error('결과 ID가 없습니다.');
        await refresh();
        if(token===selectionEpoch){
          cancelAuto();s.selected=record.id;s.record=record;s.autoJob=null;selectionEpoch++;deps.onSelect?.(record.id);
          s.notice=flow?'보정 결과를 보존하고 DB 자동 비교를 제출했습니다. 완료 후 RAW·ISP·오차 보고서를 표시합니다.':'새 결과를 보존했습니다. QE, RAW, CCM의 판정을 각각 확인하세요.';
          if(flow?.run)await watch(flow.run,record.id);
          if(flow&&flow.status!=='submitted'){s.notice='보정 결과는 보존했습니다. DB 자동 비교는 진행하지 않았습니다.';s.error=flow.error || '보정 기준을 충족하지 않았습니다.';}
        }
      }catch(e){s.error=e.message;}finally{s.busy=false;render();}
    }
    async function importFile(file){const token=++draftEpoch;s.error='';s.reading=!!file;render();if(!file)return;try{if(file.size>32*1024*1024)throw Error('JSON 파일은 32 MiB 이하여야 합니다.');const text=await file.text();parse(text);if(token!==draftEpoch)return;s.draft=text;s.notice='파일을 불러왔습니다. 실행 버튼으로 새 계산을 제출할 수 있습니다.';}catch(e){if(token===draftEpoch)s.error=e.message;}finally{if(token===draftEpoch){s.reading=false;render();}}}
    return {state:s,init,refresh,select,perform,importFile,input(text){draftEpoch++;s.reading=false;s.draft=text;},dbInput(key,value){if(Object.hasOwn(s.db,key)){s.db[key]=value;if(key==='enabled')render();}},display(key,value){if(key==='field'||key==='stage'&&['raw','isp'].includes(value)){s[key]=value;render();}},destroy(){selectionEpoch++;draftEpoch++;listEpoch++;cancelAuto();}};
  }

  global.CameraCalibrationColor={parse,esc,plot,view,rawOriginalFile,createAPI,createController};
  const root=global.document?.getElementById('color-lab');if(!root)return;
  const query=new URL(global.location.href).searchParams,campaign=query.get('campaign_id')||query.get('campaign')||'',back=global.document.getElementById('calibration-back');if(back&&campaign)back.href='/calibration-lab.html?campaign='+encodeURIComponent(campaign);
  const dbSource={run_id:query.get('source_run')||'',candidate_id:query.get('candidate')||'',condition_id:query.get('condition')||''};
  if(dbSource.run_id&&dbSource.candidate_id){
    const q=new URLSearchParams();if(campaign)q.set('campaign',campaign);q.set('source_run',dbSource.run_id);q.set('candidate',dbSource.candidate_id);if(dbSource.condition_id)q.set('condition',dbSource.condition_id);
    if(back)back.href='/calibration-lab.html?'+q;
    const dbBack=global.document.getElementById('color-db-back');if(dbBack&&global.CameraDBColorCalibration)dbBack.href=global.CameraDBColorCalibration.sourceURL(dbSource);
  }
  const ui=createController({api:createAPI((...args)=>fetch(...args)),render:s=>{root.innerHTML=view(s);},campaign,dbSource:{run_id:query.get('source_run')||'',candidate_id:query.get('candidate')||'',condition_id:query.get('condition')||''},autoResults:true,setTimer:(fn,ms)=>setTimeout(fn,ms),clearTimer:id=>clearTimeout(id),selected:query.get('calibration')||'',onSelect:id=>{const url=new URL(global.location.href);if(id)url.searchParams.set('calibration',id);else url.searchParams.delete('calibration');history.replaceState({},'',url.pathname+url.search);},download:(text,name)=>{const url=URL.createObjectURL(new Blob([text],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}});
  root.addEventListener('click',e=>{const target=e.target.closest('[data-action]');if(target&&!target.disabled)ui.perform(target.dataset.action);});root.addEventListener('submit',e=>{if(e.target.matches('[data-form="execute"]')){e.preventDefault();ui.perform('execute');}});root.addEventListener('input',e=>{if(e.target.dataset.db&&e.target.dataset.db!=='enabled')ui.dbInput(e.target.dataset.db,e.target.value);if(e.target.dataset.field==='draft'){ui.input(e.target.value);const submit=root.querySelector('button[type=submit]'),download=root.querySelector('[data-action=download-input]');if(submit)submit.disabled=ui.state.busy||!ui.state.draft||ui.state.reading;if(download)download.disabled=!ui.state.draft;}});root.addEventListener('change',e=>{if(e.target.dataset.field==='selected')ui.select(e.target.value);if(e.target.dataset.db)ui.dbInput(e.target.dataset.db,e.target.dataset.db==='enabled'?e.target.checked:e.target.value);if(e.target.dataset.holdout)ui.display(e.target.dataset.holdout,e.target.value);if(e.target.dataset.file==='input')ui.importFile(e.target.files?.[0]);});global.addEventListener('beforeunload',()=>ui.destroy());ui.init();
})(typeof window==='undefined'?globalThis:window);

/* Saved result identity and optical preview are deliberately separate. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/api/v1/reference-workshop/ghost-studies';
  const state = {record:null, result:null, template:null, run:null, busy:null, token:0, imageToken:0, buffers:new Map(), images:new Map(), prescription:null, csrf:null, selectedXY:[0,0], selectedProbeId:null};
  const coats=['fresnel','r005','r020'];
  const HALF_MM=13.66, PROBE_HALF_MM=256*0.0029/2;
  const chartLabels={black:'검은 배경 + 밝은 광원',dark_contrast:'암부 단계 + 저대비 물체',color:'24패치 색 차트',resolution:'경사 에지 · SFR',uniform:'균일장',texture:'질감'};
  const modeLabels={source_off:'광원 OFF',direct_only:'직접광만',with_ghost:'직접광 + 고스트'};
  const chartDescriptions={black:'배경 광자는 0입니다. 광원 OFF / 직접광 / 고스트 포함을 동일 노출·표시로 비교해 고스트가 만드는 신호를 봅니다. 광원의 직접상은 선택한 탐침 밖에 있을 수 있습니다.',dark_contrast:'암부 회색 단계와 저대비 물체를 고정 좌표로 생성합니다. 물체 위치를 바꾸지 않고, 고스트에 따른 암부 들뜸·대비·시간 잡음의 변화를 비교합니다.'};
  function sameXY(a,b){return a&&b&&a.length===2&&b.length===2&&a.every((v,i)=>Math.abs(v-b[i])<1e-7);}
  function fields(){return state.result?.fields||[...(state.record?.ghost_study?.fields||[]).map(id=>({id})),...(state.record?.ghost_study?.probes||[])];}
  function populateFields(){
    const all=fields();options($('field'),[...all.map(f=>f.id),'selected'],id=>id==='selected'?'지도에서 선택한 위치':`${all.find(f=>f.id===id)?.label||id}${all.find(f=>f.id===id)?.xy_mm?' · '+all.find(f=>f.id===id).xy_mm.map(number).join(', ')+' mm':''}`);
    const first=all.find(f=>f.xy_mm)||{id:null,xy_mm:[0,0]};state.selectedXY=[...first.xy_mm];state.selectedProbeId=first.id;$('field').value=first.id||'selected';setProbeInputs();
  }
  function setProbeInputs(){
    $('probeX').value=state.selectedXY[0].toFixed(6);$('probeY').value=state.selectedXY[1].toFixed(6);
    const meta=fields().find(f=>sameXY(f.xy_mm,state.selectedXY));
    $('probeNote').textContent=`선택 위치 (${state.selectedXY.map(number).join(', ')}) mm · 256×256 native · 2.9 µm pitch · 폭 0.7424 mm. ${meta?'저장된 위치입니다.':'아직 저장되지 않은 위치입니다. 계산 버튼으로 RAW·ISP를 생성합니다.'} 임의 위치의 배경은 저장된 시야의 반경 보간·회전 근사이며 고스트는 선택 좌표에 직접 적분합니다. 새 탐침 취득은 LSC OFF로 비교합니다.`;
  }
  function clearNative(note){
    ++state.imageToken;for(const id of ['nativeA','nativeB','sfrPlot','colorPlot']){const canvas=$(id);canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
    $('metrics').replaceChildren();$('chartMetrics').replaceChildren();$('rawBinding').textContent='';for(const id of ['arraysA','arraysB'])$(id).removeAttribute('href');$('caseNote').textContent=note;
  }
  function selectPosition(x,y){
    if(!Number.isFinite(x)||!Number.isFinite(y)||Math.abs(x)>HALF_MM-PROBE_HALF_MM||Math.abs(y)>HALF_MM-PROBE_HALF_MM)throw new Error(`전체 native 탐침이 센서면 안에 있어야 합니다. X·Y 범위: ±${(HALF_MM-PROBE_HALF_MM).toFixed(4)} mm`);
    state.selectedXY=[Number(x.toFixed(6)),Number(y.toFixed(6))];const f=fields().find(f=>sameXY(f.xy_mm,state.selectedXY));state.selectedProbeId=f?.id||null;$('field').value=f?.id||'selected';setProbeInputs();clearNative('선택 위치의 저장 RAW를 확인 중…');return update();
  }
  function positionFromMap(event,canvas){const box=canvas.getBoundingClientRect();return[(event.clientX-box.left)/box.width*(2*HALF_MM)-HALF_MM,HALF_MM-(event.clientY-box.top)/box.height*(2*HALF_MM)];}
  function selectedCase(){
    return state.result?.cases.find(c=>{const f=state.result.fields.find(f=>f.id===c.field_id);return sameXY(c.probe_xy_mm||f?.xy_mm,state.selectedXY)&&c.angle_deg===+$('angle').value&&c.coating===$('coating').value&&c.chart===$('chart').value&&c.irradiance_w_m2===+$('power').value;});
  }

  const number = v => Number.isFinite(v) ? v.toLocaleString(undefined,{maximumFractionDigits:3}) : '—';
  async function request(url, options) { if(options && options.method && options.method!=='GET'){if(!state.csrf){const session=await fetch('/api/v1/session').then(r=>r.json());state.csrf=session.token;}options={...options,headers:{...(options.headers||{}),'X-Platform-Token':state.csrf}};}const r=await fetch(url,options); if(!r.ok) throw new Error(`${r.status}: ${await r.text()}`); return r.json(); }
  function message(s) {$('status').textContent=s;}
  function options(el,values,label=v=>v) {const old=el.value;el.replaceChildren(...values.map(v=>{const o=document.createElement('option');o.value=v;o.textContent=label(v);return o;})); if(values.includes(old))el.value=old;}
  function base() {return `${API}/${state.run}/files/`;}
  async function binary(path) {const url=base()+path;if(!state.buffers.has(url)){state.buffers.set(url,fetch(url).then(async r=>{if(!r.ok)throw new Error('Optical map download failed');return new Float32Array(await r.arrayBuffer());}));}return state.buffers.get(url);}
  async function refresh(preferred) {
    const data=await request(API);state.template=data.template;
    const rows=data.studies;
    options($('study'),rows.map(x=>x.id),v=>{const r=rows.find(x=>x.id===v);return `${r.name} · ${r.status} · ${v.slice(-8)}`;});
    if(preferred && rows.some(x=>x.id===preferred))$('study').value=preferred;
    if(rows.length)await load($('study').value);else{state.record={ghost_study:state.template};populateFields();options($('chart'),['black','dark_contrast',...state.template.charts.filter(c=>!['black','dark_contrast'].includes(c))],c=>chartLabels[c]||c);$('replay').disabled=true;message('준비된 결과가 없습니다. 현재 조건으로 첫 작업을 제출할 수 있습니다.');}
  }
  async function load(id) {
    const token=++state.token;clearNative('저장된 결과 확인 중…');state.result=null;state.busy=null;$('cancel').disabled=true;$('submit').disabled=$('replay').disabled=false;message('저장된 결과 확인 중…');
    const r=await request(`${API}/${id}`);if(token!==state.token)return;
    if(r.status!=='succeeded') {
      state.record=r;state.result=null;state.run=id;state.buffers.clear();
      const spec=r.ghost_study||state.template;populateFields();options($('chart'),spec.charts,c=>chartLabels[c]||c);
      for(const canvas of ['directMap','ghostMap','nativeA','nativeB','sfrPlot','colorPlot']){const el=$(canvas);el.getContext('2d').clearRect(0,0,el.width,el.height);}
      $('rayDiagram').replaceChildren();state.prescription=null;for(const id of ['pathNote','mapNote','diagnostics'])$(id).textContent='';$('previewBadge').textContent='계산 결과 대기';$('report').removeAttribute('href');$('bundle').removeAttribute('href');$('caseNote').textContent='완료된 RAW 결과가 아직 없습니다.';$('metrics').replaceChildren();$('rawBinding').textContent='';
      if(['queued','running'].includes(r.status)){state.busy=id;$('cancel').disabled=false;$('submit').disabled=$('replay').disabled=true;message(`${id} · ${r.status}\n${r.progress?.stage||''}`);poll();}
      else{state.busy=null;$('cancel').disabled=true;$('submit').disabled=$('replay').disabled=false;message(`${id} · ${r.status}\n${r.error||''}`);}
      return;
    }
    state.record=r;state.result=r.result.summary;state.run=id;state.buffers.clear();
    const s=state.result.specification;
    $('angle').value=s.angles_deg.includes(15)?15:s.angles_deg[0];$('power').value=s.irradiance_w_m2[0];$('coating').value=s.coatings.includes('r005')?'r005':s.coatings[0];$('white').value=s.isp.display_white_level;
    populateFields();options($('chart'),[...new Set([...s.charts,'black','dark_contrast'])],c=>chartLabels[c]||c);$('chart').value=s.charts.includes('black')?'black':s.charts[0];const modes=state.result.cases[0]?.modes.map(m=>m.id)||['direct_only','with_ghost'];options($('modeA'),modes,m=>modeLabels[m]||m);options($('modeB'),modes,m=>modeLabels[m]||m);$('modeA').value=modes.includes('source_off')?'source_off':'direct_only';$('modeB').value='with_ghost';
    const link=new URLSearchParams(location.search),linkedField=state.result.fields.find(f=>f.id===link.get('probe'));
    if(linkedField){state.selectedXY=[...linkedField.xy_mm];state.selectedProbeId=linkedField.id;$('field').value=linkedField.id;setProbeInputs();}
    if(link.has('chart')&&[...new Set([...s.charts,'black','dark_contrast'])].includes(link.get('chart')))$('chart').value=link.get('chart');
    if(link.has('angle')&&Number.isFinite(+link.get('angle'))&&+link.get('angle')>=0&&+link.get('angle')<=30)$('angle').value=link.get('angle');
    if(link.has('power')&&Number.isFinite(+link.get('power'))&&+link.get('power')>=0)$('power').value=link.get('power');

    $('report').href=state.result.report_url;$('bundle').href=state.result.bundle_url;
    $('limitations').replaceChildren(...state.result.limitations.map(v=>{const li=document.createElement('li');li.textContent=v;return li;}));
    state.prescription=await request(base()+'prescription.json');if(token!==state.token)return;
    message(`${id} · ${state.result.cases.length}개 native 비교 · ${number(state.result.elapsed_seconds)}초 · ${(state.result.package_bytes/1e6).toFixed(1)} MB\n광학 캐시는 재사용하며 RAW 잡음·포화는 새 취득에서 계산합니다.`);
    await update(true);
  }
  function drawMap(canvas,a,b,t,plane,ci,strength,single=false) {
    const ctx=canvas.getContext('2d'),im=ctx.createImageData(512,512),off=single?0:plane*512*512*3;
    for(let p=0;p<512*512;p++){const v=((1-t)*a[off+p*3+ci]+t*b[off+p*3+ci])*strength;
      const z=Math.max(0,Math.min(1,Math.log1p(Math.max(0,v)*100)/Math.log1p(1000)));
      const q=(511-Math.floor(p/512))*512+p%512;im.data[4*q]=255*Math.min(1,z*1.9);im.data[4*q+1]=255*Math.max(0,Math.min(1,(z-.22)*1.5));im.data[4*q+2]=255*Math.max(0,(z-.65)/.35);im.data[4*q+3]=255;
    }ctx.putImageData(im,0,0);
    ctx.strokeStyle='#b9ffe2';ctx.fillStyle='#b9ffe2';ctx.font='11px sans-serif';
    for(const f of state.result.fields){const x=(f.xy_mm[0]+13.66)/27.32*512,y=(13.66-f.xy_mm[1])/27.32*512;ctx.strokeRect(x-7,y-7,14,14);ctx.fillText(f.label||f.id,x+10,y+4);}
    const px=(state.selectedXY[0]+HALF_MM)/(2*HALF_MM)*512,py=(HALF_MM-state.selectedXY[1])/(2*HALF_MM)*512,half=PROBE_HALF_MM/(2*HALF_MM)*512;ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.strokeRect(px-half,py-half,2*half,2*half);ctx.strokeStyle='#ff9460';ctx.beginPath();ctx.moveTo(px-15,py);ctx.lineTo(px+15,py);ctx.moveTo(px,py-15);ctx.lineTo(px,py+15);ctx.stroke();ctx.lineWidth=1;
  }
  function diagram(row) {
    const svg=$('rayDiagram');svg.replaceChildren();if(!state.prescription)return;
    const ns='http://www.w3.org/2000/svg';function path(d,color,width=1){const p=document.createElementNS(ns,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',color);p.setAttribute('stroke-width',width);svg.append(p);}
    const surfaces=state.prescription.surfaces;let z=0;const length=surfaces.reduce((a,s)=>a+s.thickness_mm,0),x=v=>35+v/length*1030,y=v=>140-v/20*120;
    path(`M20 140 H1080`,'#c6d8d0');surfaces.forEach((s,i)=>{let d='';for(let k=0;k<=80;k++){const r=-s.semi_diameter_mm+2*s.semi_diameter_mm*k/80,c=s.radius_mm?1/s.radius_mm:0,as=s.asphere||{},q=1-(1+(as.conic_constant||0))*c*c*r*r;let sag=c*r*r/(1+Math.sqrt(Math.max(q,0)));(as.orders||[]).forEach((n,j)=>sag+=as.coefficients[j]*Math.pow(Math.abs(r),n));d+=(k?' L':'M')+x(z+sag)+' '+y(r);}path(d,row&&row.pair.includes(i)?'#df743f':s.is_stop?'#353d3a':'#698e84',row&&row.pair.includes(i)?3:1);z+=s.thickness_mm;});
    path(`M${x(length)} 15 V265`,'#087e70',3);
    if(row&&row.ray_path_zx_mm){path(row.ray_path_zx_mm.map((p,i)=>(i?'L':'M')+x(p[0])+' '+y(p[1])).join(' '),'#e26337',2);}
  }
  async function update(rebuild=false) {
    if(!state.result)return;const token=++state.token, angle=+$('angle').value,power=+$('power').value,ci=coats.indexOf($('coating').value);
    const params=new URLSearchParams({run:state.run,probe:state.selectedProbeId||'selected',chart:$('chart').value,angle:String(angle),power:String(power)});history.replaceState(null,'','?'+params);const nativeWork=nativeCase();$('previewBadge').textContent='선택 조건의 광학 미리보기 갱신 중';$('angleLabel').textContent=angle+'°';const atlas=[...state.result.atlas].sort((a,b)=>a.angle_deg-b.angle_deg);
    if(angle<atlas[0].angle_deg || angle>atlas.at(-1).angle_deg){$('previewBadge').textContent='캐시 범위 밖 — 외삽하지 않음';$('rayDiagram').replaceChildren();$('pathNote').textContent='현재 각도의 계산된 경로가 없습니다.';$('directMap').getContext('2d').clearRect(0,0,512,512);$('ghostMap').getContext('2d').clearRect(0,0,512,512);await nativeWork;return;}
    let lo=atlas.filter(a=>a.angle_deg<=angle).at(-1),hi=atlas.find(a=>a.angle_deg>=angle);let t=lo===hi?0:(angle-lo.angle_deg)/(hi.angle_deg-lo.angle_deg);
    if(rebuild || $('path').dataset.angle!==String(lo.angle_deg)){options($('path'),['all',...lo.paths.map((_,i)=>String(i))],v=>v==='all'?'전체 고스트':`면 ${lo.paths[+v].pair.map(x=>x+1).join(' → ')} · ${(lo.paths[+v].energy_m2[ci]*1e6).toPrecision(3)} mm²`);$('path').value='all';$('path').dataset.angle=String(lo.angle_deg);}
    const [a,b]=await Promise.all([binary(lo.binary),binary(hi.binary)]);if(token!==state.token)return;
    drawMap($('directMap'),a,b,t,0,ci,power);
    let row=null;
    if($('path').value==='all'){drawMap($('ghostMap'),a,b,t,1,ci,power);}else{row=lo.paths[+$('path').value];const map=await binary(row.binary);if(token!==state.token)return;drawMap($('ghostMap'),map,map,0,0,ci,power,true);}
    diagram(row||lo.paths[0]);
    $('previewBadge').textContent=t?'보간된 광학 미리보기 — RAW 계산 결과 아님':'저장 각도의 광학 응답 · 광량 재가중 미리보기';
    $('mapNote').textContent=`방위각 ${state.result.specification.azimuth_deg}° · 광원은 D65 450–650 nm 평행광 · 고정 표시 0–10 W/m²의 로그 열지도. 테두리는 native 탐침의 실제 위치입니다.`;
    $('pathNote').textContent=row?`반사면 ${row.pair.map(x=>x+1).join(' → ')} · 550 nm의 실제 대표 광선 · 선택 경로 지도는 ${lo.angle_deg}°에서 계산한 결과`:`전체 경로의 에너지를 합산합니다. 단면은 ${lo.angle_deg}°의 대표 경로입니다. 목록은 기여도가 큰 최대 12개이며 전체 지도는 모든 추적 경로를 포함합니다.`;
    $('diagnostics').textContent=`${lo.angle_deg}°: 수치 교차 실패 ${lo.diagnostics.numerical_failure}, 개구 차단 ${lo.diagnostics.aperture_blocked}, 전반사로 제외 ${lo.diagnostics.total_internal_reflection}. 실패 수는 광선-경로 상호작용 수이며 물리 고스트 예측의 정확도 인증이 아닙니다. 대표 동공 격자 refinement: ${state.result.refinement?.status||"pending"} · ${state.result.refinement?.checks?.map(c=>(100*c.relative_l1_ghost_rate).toFixed(1)+"%").join(" / ")||"—"} 변화 (데모 기준 5%). 정량 수렴과 다른 각도의 정확도는 확인하지 않았습니다.`;
    await nativeWork;
  }
  function image(url){if(!state.images.has(url))state.images.set(url,new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>{state.images.delete(url);reject(new Error('RAW preview image download failed'));};im.src=url;}));return state.images.get(url);}
  async function paintNative(canvas,mode,token){
    const raw=$('stage').value==='raw',asset=raw?mode.raw_image:mode.image,maskAsset=raw?mode.raw_valid:mode.valid;
    if(!asset)throw new Error('이 저장 결과에는 정수 RAW 미리보기가 없습니다. 원본 배열을 내려받거나 새 v2 취득을 실행하십시오.');
    const [im,mask]=await Promise.all([image(base()+asset),maskAsset?image(base()+maskAsset):Promise.resolve(null)]);if(token!==state.imageToken)return;
    const c=canvas.getContext('2d');c.drawImage(im,0,0);if($('valid').checked&&mask){const tmp=document.createElement('canvas');tmp.width=tmp.height=256;const q=tmp.getContext('2d');q.drawImage(mask,0,0);const m=q.getImageData(0,0,256,256).data,px=c.getImageData(0,0,256,256);for(let i=0;i<256*256;i++)if(m[4*i]<128){const v=(Math.floor(i/256/8)+Math.floor(i%256/8))%2;px.data.set(v?[184,111,178,255]:[105,117,126,255],4*i);}c.putImageData(px,0,0);}
  }
  function labPlots(c,pair) {
    const plot=$('sfrPlot').getContext('2d');plot.clearRect(0,0,600,210);plot.strokeStyle='#cfddd6';plot.beginPath();plot.moveTo(35,15);plot.lineTo(35,180);plot.lineTo(585,180);plot.stroke();plot.fillStyle='#657b75';plot.font='12px system-ui';plot.fillText('0',30,200);plot.fillText('0.5 cycles/native pixel',430,200);
    pair.forEach((m,i)=>{const v=m.metrics.sfr;if(!v || v.status!=='ok'){plot.fillText(i===0?'선택 차트에서 SFR 평가 없음':'고스트 포함 SFR 미판정',45,35+i*20);return;}plot.strokeStyle=i?'#c97a3f':'#087e70';plot.beginPath();v.frequency_cycles_per_native_pixel.forEach((f,j)=>{const x=35+f/.5*550,y=180-Math.min(1.1,v.mtf[j])/1.1*160;j?plot.lineTo(x,y):plot.moveTo(x,y);});plot.stroke();plot.fillStyle=i?'#c97a3f':'#087e70';plot.fillText(`${modeLabels[m.id]} · SFR50 ${number(v.mtf50_cycles_per_native_pixel)}`,230,25+i*18);});
    const q=$('colorPlot').getContext('2d');q.clearRect(0,0,600,210);q.font='12px system-ui';q.fillStyle='#657b75';const color=c.modes.find(m=>m.id==='with_ghost')?.metrics.color_change;
    if(!color){q.fillText('색 차트에서 고정 24개 패치를 비교합니다.',20,35);return;}
    q.fillText('동일 코팅의 직접광 결과 대비 · 고정 세로 범위 0–100',20,18);
    color.patches.forEach((p,i)=>{const x=20+i*23;if(p.delta_e00===null){q.fillStyle='#b86faa';q.fillText('×',x+4,170);}else{q.fillStyle='#087e70';const h=Math.min(100,p.delta_e00)/100*140;q.fillRect(x,175-h,17,h);}q.fillStyle='#657b75';q.fillText(String(i+1),x,195);});
  }
  function metricCard(label,value){const div=document.createElement('div');div.className='kpi';div.textContent=label;const strong=document.createElement('strong');strong.textContent=value;div.append(strong);return div;}
  function renderChartMetrics(c,pair){
    const container=$('chartMetrics');container.replaceChildren();
    if(!pair.some(m=>m.chart_metrics))return;
    const title=document.createElement('h2');title.textContent=c.chart==='black'?'검은 배경 · 광원 OFF 대비 신호':'암부 차트 · 고정 물체 / 배경 영역';container.append(title);
    const note=document.createElement('p');note.className='lede';note.textContent=c.chart==='black'?'검은 배경의 고정 측정 영역에서, 광원 OFF 대비 광전자 상승을 부호를 유지해 계산합니다. 최대값은 동일 픽셀의 8회 평균 차이입니다.':'4개 암부 레벨 × 5·10·20% 대비의 12개 고정 물체. CNR의 잡음은 픽셀별 8회 시간 분산입니다. 대비 보존율은 광원 OFF 대비이며 실제 검출 확률을 뜻하지 않습니다. 포화·무효 영역은 미판정입니다.';container.append(note);
    const table=document.createElement('table');table.className='table';
    function row(values,head=false){const tr=document.createElement('tr');for(const value of values){const td=document.createElement(head?'th':'td');td.textContent=value;tr.append(td);}table.append(tr);}
    const val=(v,scale=1,suffix='')=>Number.isFinite(v)?number(v*scale)+suffix:'미판정';
    if(c.chart==='black'){
      row(['고정 영역 지표',...pair.map(m=>modeLabels[m.id])],true);
      const entries=pair.map(m=>m.chart_metrics?.black_level);
      for(const [label,key] of [['평균 상승 · e','mean_excess_e'],['최대 상승 · e','peak_excess_e'],['최소 상승 · e','minimum_excess_e'],['평균 전자 · e','mean_e']])row([label,...entries.map(v=>v?.status==='ok'?val(v[key]):'미판정')]);
      row(['시간 잡음 · e RMS',...entries.map(v=>v?.status==='ok'&&Number.isFinite(v.temporal_variance_e)?val(Math.sqrt(v.temporal_variance_e)):'미판정')]);
      row(['상태',...entries.map(v=>v?.status==='ok'?'유효':v?.reason||'자료 없음')]);
    }else{
      row(['배경 레벨 / 입력 대비','A 관측 대비','B 관측 대비','A CNR','B CNR','B 대비 보존율'],true);
      const reference=pair.find(m=>m.chart_metrics?.targets?.length)?.chart_metrics.targets||[];
      for(const target of reference){const values=pair.map(m=>m.chart_metrics?.targets?.find(t=>t.id===target.id));const cell=(index,key,scale=1,suffix='')=>values[index]?.status==='ok'?val(values[index][key],scale,suffix):'미판정';row([`${number(target.background_level*100)}% / ${number(target.declared_weber_contrast*100)}%`,cell(0,'observed_contrast',100,'%'),cell(1,'observed_contrast',100,'%'),cell(0,'cnr'),cell(1,'cnr'),cell(1,'contrast_retention',100,'%')]);}
    }
    container.append(table);
  }
  async function nativeCase(){
    clearNative('선택 위치의 저장 RAW를 확인 중…');const token=state.imageToken;
    $('labPlots').hidden=['black','dark_contrast'].includes($('chart').value);$('chartDescription').textContent=chartDescriptions[$('chart').value]||'기존 Lab 입력에 동일 광원을 추가해 직접광과 고스트의 영향을 비교합니다.';
    const c=selectedCase();
    if(!c){$('caseNote').textContent=`(${state.selectedXY.map(number).join(', ')}) mm · 현재 위치·각도·광량·차트와 일치하는 RAW가 없습니다. ‘선택 위치 RAW 계산’을 누르면 실제 취득을 시작합니다.`;return;}
    const pair=[$('modeA').value,$('modeB').value].map(id=>c.modes.find(m=>m.id===id));if(pair.some(m=>!m)){$('caseNote').textContent='이 저장 결과에는 선택한 취득 비교군이 없습니다. 새로 계산하십시오.';return;}
    $('caseNote').textContent=`${c.id} · ${c.field_id} · (${state.selectedXY.map(number).join(', ')}) mm · ${chartLabels[c.chart]||c.chart} · ${c.angle_deg}° · ${c.irradiance_w_m2} W/m² · native 256×256 · 4 ms · gain 1 · 반복 취득 8회 · 저장 표시 white ${state.result.specification.isp.display_white_level}${$('stage').value==='raw'?' · RAW 표시는 black pedestal 포함, 0–'+(pair[0].raw_preview?.white_dn??'ADC white')+' DN 고정 범위 · gamma 1':''}`;
    for(const [i,id]of ['arraysA','arraysB'].entries())$(id).href=base()+pair[i].arrays;
    if($('stage').value==='raw'&&pair.some(m=>!m.raw_image)){$('caseNote').textContent+=' · 이 구형 취득에는 RAW 미리보기가 없습니다. 원본 배열을 내려받거나 새 취득을 실행하십시오.';return;}
    await Promise.all([paintNative($('nativeA'),pair[0],token),paintNative($('nativeB'),pair[1],token)]);if(token!==state.imageToken)return;
    labPlots(c,pair);renderChartMetrics(c,pair);
    const metrics=[['고스트 평균 광전자',number(c.ghost_mean_e)+' e'],['고스트 최대 광전자',number(c.ghost_peak_e)+' e'],[c.chart==='black'?'입력 배경 광량':'ROI 대비 · A / B',c.chart==='black'?'0 · 외부 광원만':pair.every(m=>m.metrics.valid_fraction===1)?pair.map(m=>number(m.metrics.fixed_roi_percentile_contrast)).join(' / '):'무효 영역 · 미판정'],['직접광 분광 투과 범위',(c.background_transmission_min_max||[]).map(number).join(' – ')],['시간 잡음 · A / B · e RMS',pair.map(m=>number(m.metrics.temporal_noise_e)).join(' / ')],['Well 포화 · A / B · %',pair.map(m=>number(100*m.metrics.well_saturation_fraction)).join(' / ')],['ADC 포화 · A / B · %',pair.map(m=>number(100*m.metrics.adc_saturation_fraction)).join(' / ')],c.chart==='color'?['직접광 대비 색 변화 · ΔE00',number(c.modes.find(m=>m.id==='with_ghost')?.metrics.color_change?.mean_delta_e00)]:['유효 픽셀 · A / B · %',pair.map(m=>number(100*m.metrics.valid_fraction)).join(' / ')]];
    $('metrics').replaceChildren(...metrics.map(([a,b])=>metricCard(a,b)));
    $('rawBinding').textContent=`${state.run} / ${c.id} · ${pair.map(m=>`${modeLabels[m.id]} RAW SHA-256 ${c.raw_hashes[m.id]}`).join(' | ')}`;
  }
  async function submit(replay=false){let s=structuredClone(state.result?state.result.specification:state.template);s.name='Reference Ghost Lab · '+(replay?'RAW ISP replay':'selected capture');
    if(replay){s.reprocess_of=state.run;}else{s.schema='camera_reference_ghost_study_v2';s.isp.lsc_enabled=false;s.reprocess_of=null;s.angles_deg=[+$('angle').value];s.coatings=[$('coating').value];s.fields=[];s.probes=[{id:'probe-01',xy_mm:[...state.selectedXY]}];s.charts=[$('chart').value||'black'];s.irradiance_w_m2=[+$('power').value];}
    if(replay&&!state.result)throw new Error('완료된 RAW 연구를 먼저 선택하십시오.');s.isp.display_white_level=+$('white').value;
    const r=await request(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)});state.busy=r.id;$('cancel').disabled=false;$('submit').disabled=$('replay').disabled=true;message(`${r.id} · ${r.status}`);poll();
  }
  async function poll(){if(!state.busy)return;const id=state.busy;try{const r=await request(`${API}/${id}`);if(id!==state.busy)return;message(`${r.id} · ${r.status}\n${r.progress?.stage||r.error||''}`);if(['succeeded','failed','cancelled','timed_out'].includes(r.status)){state.busy=null;$('cancel').disabled=true;$('submit').disabled=$('replay').disabled=false;if(r.status==='succeeded')await refresh(r.id);return;}setTimeout(poll,1500);}catch(e){message(e.message);}}
  function guarded(fn){return()=>Promise.resolve().then(fn).catch(e=>message(e.message));}
  $('refresh').onclick=guarded(()=>refresh(state.run));$('study').onchange=guarded(()=>load($('study').value));
  for(const id of ['angle','power','coating','path','chart','valid','modeA','modeB','stage'])$(id).oninput=guarded(()=>update(id==='coating'));
  $('field').onchange=guarded(()=>{const f=fields().find(f=>f.id===$('field').value);if(f?.xy_mm)return selectPosition(...f.xy_mm);});
  $('selectProbe').onclick=guarded(()=>selectPosition(+$('probeX').value,+$('probeY').value));
  for(const id of ['directMap','ghostMap'])$(id).onclick=event=>{const xy=positionFromMap(event,$(id));Promise.resolve().then(()=>selectPosition(...xy)).catch(e=>message(e.message));};
  $('submit').onclick=guarded(()=>submit(false));$('replay').onclick=guarded(()=>submit(true));$('cancel').onclick=guarded(()=>request(`/api/v1/reference-workshop/runs/${state.busy}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
  $('save').onclick=()=>{const selection={run_id:state.run,angle_deg:+$('angle').value,irradiance_w_m2:+$('power').value,coating:$('coating').value,field:state.selectedProbeId,probe_xy_mm:state.selectedXY,mode_a:$('modeA').value,mode_b:$('modeB').value,stage:$('stage').value,chart:$('chart').value,manifest_sha256:state.result?.manifest_sha256};const url=URL.createObjectURL(new Blob([JSON.stringify(selection,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ghost-selection.json';a.click();URL.revokeObjectURL(url);};
  refresh(new URLSearchParams(location.search).get('run')).catch(e=>message(e.message));
})();

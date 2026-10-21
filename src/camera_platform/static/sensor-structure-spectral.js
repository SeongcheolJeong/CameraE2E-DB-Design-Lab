/* Stored spectral OCL solver and camera results. No fabricated images or completed status. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const endpoint = '/api/v1/sensor-structure-spectral-studies';
  const query = new URLSearchParams(location.search);
  let run = query.get('run') || '';
  let summary = null;
  let timer = null;
  let selectedCase = null;
  const fieldNames = {center: '중심', 'right-edge': '오른쪽 가장자리', 'outer-ne': '오른쪽 가장자리'};
  const chartNames = {uniform: '균일장', color: '색 패치', texture: '질감', resolution: '해상력'};
  const profileNames = {spectral_response: 'R/G/B 파장별 응답'};
  const checkNames={energy:'에너지',passivity:'무원성',grid60:'60 cells/µm',grid80:'80 cells/µm',time_refinement:'계산 시간',pml_refinement:'PML 두께',half_cell_phase:'반 격자 위치'};
  const channelNames = {red:'R',green:'G',blue:'B',R:'R',G:'G',Gr:'Gr',Gb:'Gb',B:'B'};
  const classificationNames = {magnitude_stable:'영향 크기 안정', direction_stable:'방향 안정', indistinguishable:'구분 어려움', unresolved:'미결정', numerical_evidence:'수치 해석 근거', observed:'수치 해석 근거', measured:'취득·계산값', assumption:'적용 가정', assumed:'적용 가정', model_assumption:'모델 가정', limitation:'지원 범위 제한', computed_stable:'계산상 안정 확인', explicit_hypothesis:'명시적인 적용 가정', reused_model:'기존 모델 재사용', model_proxy:'모델 근사값', fixed_policy:'고정 처리 정책', not_validated:'검증되지 않은 범위', valid:'수치 유효', passed:'검사 통과', invalid:'수치 무효', failed:'검사 실패', not_checked:'확인 전', single_grid:'단일 격자 탐색', two_grid_stable:'두 격자 변화 경향 확인',time_not_checked:'시간 변화 확인 전',pml_not_checked:'PML 변화 확인 전',time_pml_not_checked:'시간·PML 변화 확인 전', exploratory:'탐색 결과', computed:'계산값', official_specification:'공식 센서 사양', optical_mounting_assumption:'광학 배치 가정'};
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const fmt = (x, digits=2) => finite(x) ? x.toLocaleString('en-US', {maximumFractionDigits:digits, minimumFractionDigits:digits}) : '—';
  const signed = (x,digits=2,suffix='') => finite(x) ? `${x>0?'+':''}${fmt(x,digits)}${suffix}` : '—';
  const pair = (a,b,digits=2) => `${fmt(a,digits)} → ${fmt(b,digits)}`;
  const asText = value => typeof value === 'string' ? value : value == null ? '—' : JSON.stringify(value);
  const make = (tag, text, className) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
  const appendCell = (row,text,className) => row.appendChild(make('td',text,className));
  const fileUrl = path => {
    if(typeof path !== 'string' || !path || path.startsWith('/') || path.split('/').some(x=>x==='..')) return null;
    return `${endpoint}/${encodeURIComponent(run)}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
  };
  async function json(url) {const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`${response.status} ${response.statusText} · ${url}`);return response.json();}
  function link(parent,label,url) {if(!url)return;const a=make('a',label);a.href=url;a.target='_blank';a.rel='noopener';parent.append(a);}
  function selectOptions(id,values,names,initial) {
    const select=$(id), previous=select.value || query.get(id) || initial;
    select.replaceChildren();
    values.forEach(value=>{const option=make('option',names?names[value]||String(value):String(value));option.value=String(value);select.append(option);});
    if(values.some(v=>String(v)===String(previous)))select.value=String(previous);
  }
  function setImage(id,missing,path) {
    const img=$(id),url=fileUrl(path);img.hidden=!url;$(missing).hidden=!!url;
    img.onload=()=>{$(missing).hidden=true;img.hidden=false;};
    img.onerror=()=>{img.hidden=true;$(missing).hidden=false;$(missing).textContent='저장 영상 로딩 실패 · 다운로드·실행 상태 확인';};
    if(url)img.src=url;else img.removeAttribute('src');
  }
  const svgNS='http://www.w3.org/2000/svg';
  function svgNode(tag,attributes={},text) {const node=document.createElementNS(svgNS,tag);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,String(value)));if(text!==undefined)node.textContent=text;return node;}
  const stateText=value=>typeof value==='string' ? classificationNames[value]||value : typeof value==='boolean' ? value?'수치 유효':'수치 무효' : asText(value);
  function geometryFields() {
    const g=summary.geometry||{};
    const fields=g.fields||g.probes||g.rois||g.roi_layout||[];
    const entries=Array.isArray(fields)?fields:Object.entries(fields).map(([id,v])=>({field_id:id,...v}));
    const byId=new Map(entries.map(item=>[item.field_id||item.id,item]));
    (summary.cases||[]).forEach(c=>{byId.set(c.field_id,{...(byId.get(c.field_id)||{}),...(c.source_geometry||{}),field_id:c.field_id});});
    byId.forEach((field,id)=>{const anchor=(summary.evidence||[]).find(e=>e.field_id===id&&e.wavelength_nm===550)||(summary.evidence||[]).find(e=>e.field_id===id);if(anchor)field.evidence_cra_deg=anchor.angle_deg;});
    return [...byId.values()];
  }
  function roiInfo(item) {
    const roi=item.roi||item;
    const origin=roi.roi_origin_active_row_col||roi.roi_origin_row_col||roi.origin_row_col||roi.origin_rc||roi.origin||[roi.row_start??roi.row0,roi.col_start??roi.col0];
    const shape=roi.roi_shape_row_col||roi.roi_size_row_col||roi.shape_row_col||roi.shape_rc||roi.shape||roi.size||[roi.rows,roi.cols];
    const cra=item.cra_deg??item.actual_cra_deg??item.chief_ray_angle_deg??item.host_cra_deg??item.evidence_cra_deg??roi.cra_deg;
    return {origin,shape,cra};
  }
  function renderGeometry() {
    const g=summary.geometry||{},size=g.active_rows_cols||g.active_rows_col||g.active_shape_row_col||g.active_shape||g.active_size_row_col||[g.active_rows,g.active_cols];
    const rows=Number(size?.[0]),cols=Number(size?.[1]),pitch=g.pixel_pitch_um??g.pitch_um,fields=geometryFields();
    const table=$('geometry-rows');table.replaceChildren();
    fields.forEach(item=>{const id=item.field_id||item.id,{origin,shape,cra}=roiInfo(item),tr=make('tr');appendCell(tr,fieldNames[id]||id);appendCell(tr,origin?.every(finite)?origin.map(v=>fmt(v,0)).join(', '):'—');appendCell(tr,shape?.every(finite)?shape.map(v=>fmt(v,0)).join(' × '):'—');appendCell(tr,`${fmt(cra,3)}°`);tr.dataset.field=id;tr.tabIndex=0;tr.setAttribute('aria-label',`${fieldNames[id]||id} 비교 선택`);const activate=()=>{if([...$('field').options].some(o=>o.value===id)){$('field').value=id;drawCase();}};tr.addEventListener('click',activate);tr.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();activate();}});table.append(tr);});
    const diagram=$('sensor-diagram');diagram.replaceChildren();
    if(!(rows>0&&cols>0)){diagram.append(make('p','센서 전체 영역의 좌표 메타데이터가 없습니다. 도식을 추정해 표시하지 않습니다.'));}
    else {
      const width=650,height=420,padX=45,padY=55,scale=Math.min((width-2*padX)/cols,(height-105)/rows),drawWidth=cols*scale,drawHeight=rows*scale,x0=(width-drawWidth)/2,y0=padY;
      const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`});
      svg.append(svgNode('text',{x:width/2,y:25,'text-anchor':'middle',fill:'#c6d9ed','font-size':16},`${fmt(cols,0)} cols × ${fmt(rows,0)} rows`));
      svg.append(svgNode('rect',{x:x0,y:y0,width:drawWidth,height:drawHeight,rx:4,fill:'#162c41',stroke:'#7193b4','stroke-width':1.5}));
      svg.append(svgNode('line',{x1:x0+drawWidth/2,x2:x0+drawWidth/2,y1:y0,y2:y0+drawHeight,stroke:'#47627a','stroke-dasharray':'5 5'}),svgNode('line',{x1:x0,x2:x0+drawWidth,y1:y0+drawHeight/2,y2:y0+drawHeight/2,stroke:'#47627a','stroke-dasharray':'5 5'}));
      fields.forEach(item=>{const id=item.field_id||item.id,{origin,shape}=roiInfo(item);if(!origin?.every(finite)||!shape?.every(finite))return;const inside=origin[0]>=0&&origin[1]>=0&&shape[0]>0&&shape[1]>0&&origin[0]+shape[0]<=rows&&origin[1]+shape[1]<=cols;const color=inside?'#78ddc6':'#ef9d89',x=x0+origin[1]*scale,y=y0+origin[0]*scale,w=shape[1]*scale,h=shape[0]*scale;const group=svgNode('g',{'data-field':id});group.append(svgNode('rect',{x,y,width:w,height:h,fill:color,'fill-opacity':.18,stroke:color,'stroke-width':2}),svgNode('text',{x:x+w/2,y:y-12,'text-anchor':'middle',fill:color,'font-size':14},fieldNames[id]||id));svg.append(group);});
      svg.append(svgNode('text',{x:width/2,y:y0+drawHeight+30,'text-anchor':'middle',fill:'#a8b9cc','font-size':14},finite(pitch)?`픽셀 ${fmt(pitch,2)} µm · ${fmt(cols*pitch/1000,3)} × ${fmt(rows*pitch/1000,3)} mm`:'센서 좌표에 비례한 탐침 크기'));diagram.append(svg);
    }
    $('geometry-note').textContent=g.description||'전체 active 영역과 탐침을 동일 비율로 표시합니다. 표시 배율을 변경해 탐침을 센서 중심으로 옮기지 않습니다.';
    $('geometry-evidence').textContent=JSON.stringify(g,null,2);
  }
  function renderSpectralPlot(items) {
    const holder=$('spectral-chart');holder.replaceChildren();
    const valid=items.filter(x=>finite(x.wavelength_nm)&&finite(x.response_gain));
    if(!valid.length){holder.append(make('p','이 위치에서 완료된 파장 응답이 없습니다.'));return;}
    const width=1100,height=290,left=75,right=40,top=30,bottom=55;
    let minW=Math.min(...valid.map(x=>x.wavelength_nm)),maxW=Math.max(...valid.map(x=>x.wavelength_nm));if(minW===maxW){minW-=20;maxW+=20;}
    let low=Math.min(1,...valid.map(x=>x.response_gain)),high=Math.max(1,...valid.map(x=>x.response_gain));const margin=Math.max(.005,(high-low)*.2);low-=margin;high+=margin;
    const x=w=>left+(w-minW)/(maxW-minW)*(width-left-right),y=g=>height-bottom-(g-low)/(high-low)*(height-top-bottom);
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`});
    for(let i=0;i<5;i++){const g=low+(high-low)*i/4;svg.append(svgNode('line',{x1:left,x2:width-right,y1:y(g),y2:y(g),stroke:'#273c51'}),svgNode('text',{x:left-12,y:y(g)+4,'text-anchor':'end',fill:'#a8b9cc','font-size':13},`${fmt(g,3)}×`));}
    svg.append(svgNode('line',{x1:left,x2:width-right,y1:y(1),y2:y(1),stroke:'#b6c9dc','stroke-dasharray':'6 5'}));
    [...new Set(valid.map(i=>i.wavelength_nm))].sort((a,b)=>a-b).forEach(w=>svg.append(svgNode('text',{x:x(w),y:height-bottom+25,'text-anchor':'middle',fill:'#a8b9cc','font-size':13},`${fmt(w,0)} nm`)));
    const colors={red:'#f19a9b',green:'#78ddc6',blue:'#90caff',R:'#f19a9b',G:'#78ddc6',Gr:'#78ddc6',Gb:'#9bd3a1',B:'#90caff'};
    [...new Set(valid.map(i=>i.cfa_channel))].forEach(channel=>{const points=valid.filter(i=>i.cfa_channel===channel).sort((a,b)=>a.wavelength_nm-b.wavelength_nm);const color=colors[channel]||'#d4b2ef';svg.append(svgNode('polyline',{points:points.map(i=>`${x(i.wavelength_nm)},${y(i.response_gain)}`).join(' '),fill:'none',stroke:color,'stroke-width':2}));points.forEach(i=>{const uncertain=['unresolved','indistinguishable'].includes(i.trend_status);const circle=svgNode('circle',{cx:x(i.wavelength_nm),cy:y(i.response_gain),r:uncertain?6:5,fill:uncertain?'#0c1725':color,stroke:color,'stroke-width':2});circle.append(svgNode('title',{},`${channelNames[channel]||channel} ${i.wavelength_nm} nm: ${fmt(i.response_gain,6)}× · ${stateText(i.trend_status)}${i.polarization_opposed?' · s/p 방향 반대':''}`));svg.append(circle);});});
    svg.append(svgNode('text',{x:left,y:18,fill:'#c6d9ed','font-size':13},'OCL 이동 / 기준 흡수'));holder.append(svg);
  }
  function renderEvidence() {
    const rows=$('evidence');rows.replaceChildren();const field=$('field').value;
    const items=(summary.evidence||[]).filter(item=>!field||item.field_id===field);
    $('spectral-field').textContent=fieldNames[field]||field||'전체 위치';renderSpectralPlot(items);
    items.forEach(item=>{
      const row=make('tr');appendCell(row,`${fieldNames[item.field_id]||item.field_id} · ${fmt(item.angle_deg,3)}°`);
      appendCell(row,`${channelNames[item.cfa_channel]||asText(item.cfa_channel)} · ${fmt(item.wavelength_nm,0)} nm`);
      appendCell(row,fmt(item.baseline_absorption,6));appendCell(row,fmt(item.shifted_absorption,6));
      appendCell(row,`${fmt(item.response_gain,5)}× (${signed((item.response_gain-1)*100,2,'%')})`,'neutral');
      appendCell(row,finite(item.mesh_gain_change)?fmt(item.mesh_gain_change,6):asText(item.mesh_gain_change));
      const validity=make('td',stateText(item.numerical_validity));validity.append(make('small',`에너지 잔차 최대 ${finite(item.energy_residual_max)?fmt(item.energy_residual_max*100,3)+'%':'—'}`));row.append(validity);
      const pol=make('td',item.polarization_opposed?'s/p 방향 반대':'s/p별 변화',item.polarization_opposed?'warning':'');
      (item.polarized||[]).forEach(p=>pol.append(make('small',`${p.polarization}: ${signed(p.absolute_delta*100,4,'%p')} 흡수`)));row.append(pol);
      const trend=make('td',stateText(item.trend_status),['magnitude_stable','direction_stable','two_grid_stable'].includes(item.trend_status)?'neutral':'warning');
      if(item.numerical_validity===true&&['unresolved','indistinguishable'].includes(item.trend_status))trend.append(make('small','유효 배열 · 미확정 효과를 탐색 영상에 적용'));
      if(item.completed_checks?.length)trend.append(make('small','완료: '+item.completed_checks.map(c=>checkNames[c]||c).join(' · ')));
      if(item.pending_checks?.length)trend.append(make('small','미완료: '+item.pending_checks.map(c=>checkNames[c]||c).join(' · '),'warning'));
      if(item.scope)trend.append(make('small',stateText(item.scope)));row.append(trend);rows.append(row);
    });
    $('spectral-note').textContent=summary.spectral_response?.interpolation_description||'빈 원은 미결정 또는 구분 어려운 차이입니다. 채운 점도 전체 수렴·제품 검증을 뜻하지 않습니다. 점은 실제 계산한 파장입니다. 연결선은 계산 표본을 잇는 안내이며, 사이 파장의 독립 해석 결과가 아닙니다. 흡수 비율의 격자 민감도는 수치 변화량이며 통계적 신뢰구간이 아닙니다. 두 격자 변화 경향 확인은 시간·PML을 포함한 전체 수렴 판정이 아닙니다.';
  }
  function renderAssumptions() {
    const container=$('assumptions');container.replaceChildren();
    (summary.assumptions||[]).forEach(item=>{
      const card=make('div',undefined,'assumption'),kind=String(item.classification||'assumption');
      card.append(make('span',classificationNames[kind]||kind,`classification${/evidence|observed|source|computed_stable/.test(kind)?' evidence':''}`),make('h3',item.label||item.id||'가정'),make('p',item.description||''));container.append(card);
    });
  }
  function renderMetrics(c) {
    const metrics=c.metrics||{},a=metrics.baseline||{},b=metrics.projected||{},changes=metrics.comparison||{};
    const table=$('site-metrics');table.replaceChildren();const bars=$('signal-bars');bars.replaceChildren();
    const allSignal=[...(a.site_mean_e||[]),...(b.site_mean_e||[])].filter(finite),maximum=Math.max(1,...allSignal);
    ['R','Gr','Gb','B'].forEach((site,i)=>{
      const row=make('tr');appendCell(row,site);appendCell(row,pair(a.site_mean_e?.[i],b.site_mean_e?.[i]));appendCell(row,signed(changes.signal_change_pct?.[i],2,'%'),'neutral');
      appendCell(row,pair(a.site_temporal_noise_e?.[i],b.site_temporal_noise_e?.[i]));appendCell(row,signed(changes.noise_change_pct?.[i],2,'%'));
      appendCell(row,pair(a.site_snr?.[i],b.site_snr?.[i]));appendCell(row,signed(changes.snr_change_pct?.[i],2,'%'));table.append(row);
      const group=make('div',undefined,'bar-group');group.append(make('h3',`${site} · 평균 신호 e⁻`));
      [a.site_mean_e?.[i],b.site_mean_e?.[i]].forEach(value=>{const bar=make('div',undefined,'bar-row'),track=make('div',undefined,'bar-track'),fill=make('div',undefined,'bar-fill');fill.style.width=`${finite(value)?Math.max(0,Math.min(100,value/maximum*100)):0}%`;track.append(fill);bar.append(track,make('span',fmt(value,1),'bar-number'));group.append(bar);});bars.append(group);
    });
    const rgb=$('rgb-metrics');rgb.replaceChildren();['R','G','B'].forEach((channel,i)=>{const row=make('tr');appendCell(row,channel);appendCell(row,fmt(a.linear_rgb_temporal_noise?.[i],5));appendCell(row,fmt(b.linear_rgb_temporal_noise?.[i],5));rgb.append(row);});
    const luma=make('tr');appendCell(luma,'휘도');appendCell(luma,fmt(a.luma_temporal_noise,5));appendCell(luma,fmt(b.luma_temporal_noise,5));rgb.append(luma);
    const other=$('other-metrics');other.replaceChildren();
    [['Well 포화',a.well_saturation_fraction,b.well_saturation_fraction,100,'%'],['ADC 포화',a.adc_saturation_fraction,b.adc_saturation_fraction,100,'%']].forEach(([name,x,y,scale,suffix])=>{const row=make('tr');appendCell(row,name);appendCell(row,finite(x)?fmt(x*scale,3)+suffix:'—');appendCell(row,finite(y)?fmt(y*scale,3)+suffix:'—');other.append(row);});
    const shift=make('tr');appendCell(shift,'기준 → 적용 색 이동 ΔE00');appendCell(shift,'—');appendCell(shift,fmt(changes.color_shift_delta_e00_mean,3));other.append(shift);
    const patches=changes.color_shift_delta_e00_patches;const hasPatches=Array.isArray(patches)&&patches.length>0;$('patch-panel').hidden=!hasPatches;$('patches').replaceChildren();
    if(hasPatches)patches.forEach((value,i)=>{const numeric=finite(value)?value:value?.delta_e00;const el=make('div',undefined,'patch');el.append(make('small',value?.label||`PATCH ${i+1}`),make('span',fmt(numeric,2)));$('patches').append(el);});
  }
  function drawCase() {
    const cases=summary?.cases||[];
    selectedCase=cases.find(c=>String(c.field_id)===$('field').value&&String(c.chart)===$('chart').value&&String(c.light_scale)===$('light').value&&String(c.profile_id)===$('profile').value);
    $('no-case').hidden=!!selectedCase;
    if(!selectedCase){$('no-case').textContent='이 조합의 완료 결과가 없습니다. 다른 저장 조건을 선택하세요.';$('image-pair').hidden=true;$('difference-panel').hidden=true;$('case-details').replaceChildren();$('case-downloads').replaceChildren();renderMetrics({});return;}
    const c=selectedCase,stage=$('stage').value,files=c.files||{},isDifference=stage==='difference';
    $('image-pair').hidden=isDifference;$('difference-panel').hidden=!isDifference;
    $('stage-a').textContent=$('stage-b').textContent=stage==='raw'?'NATIVE RAW':'FIXED ISP';
    setImage('image-a','missing-a',files[`baseline_${stage==='raw'?'raw':'isp'}`]);setImage('image-b','missing-b',files[`projected_${stage==='raw'?'raw':'isp'}`]);setImage('image-diff','missing-diff',files.difference);
    const params=new URLSearchParams(location.search);params.set('run',run);['field','chart','light','profile','stage'].forEach(id=>params.set(id,$(id).value));history.replaceState(null,'',`${location.pathname}?${params}`);
    $('case-details').replaceChildren();
    const geo=c.source_geometry||{},anchor=(summary.evidence||[]).find(e=>e.field_id===c.field_id&&e.wavelength_nm===550),cra=c.actual_cra_deg??c.host_cra_deg??geo.cra_deg??geo.actual_cra_deg??anchor?.angle_deg;
    [`CASE ${c.id}`,`실제 탐침 CRA ${fmt(cra,3)}° (550 nm)`, '파장별 전자율 적용 → 합산',`광량 ${fmt(c.light_scale,2)}×`].forEach(value=>$('case-details').append(make('span',value,'chip')));
    $('profile-explanation').textContent='각 위치의 R·G·B 필터별 계산 파장 응답을 450–650 nm 입력 격자로 보간하고, 기존 파장별 평균 전자율에 곱한 뒤 합산합니다. 2D 주기 단면·균일 CFA의 비율을 카메라 Bayer 응답에 연결하는 가정은 남아 있으며, 제품 QE나 3D 픽셀 간 전달을 검증한 결과가 아닙니다.';
    const evidence=(summary.evidence||[]).filter(e=>e.field_id===c.field_id),unresolved=evidence.filter(e=>e.trend_status==='unresolved').length,indistinguishable=evidence.filter(e=>e.trend_status==='indistinguishable').length,opposed=evidence.filter(e=>e.polarization_opposed).length,pending=[...new Set(evidence.flatMap(e=>e.pending_checks||[]))];
    const notices=['탐색용 RAW·ISP: 파장별 수치 유효성을 통과해도 OCL 효과가 모두 확정된 것은 아닙니다.'];
    if(unresolved||indistinguishable)notices.push(`선택 위치의 ${evidence.length}개 응답 중 미결정 ${unresolved}개 · 구분 어려움 ${indistinguishable}개.`);
    if(opposed)notices.push(`s/p 방향이 반대인 응답 ${opposed}개는 무편광 평균만으로 개선을 주장하지 않습니다.`);
    if(pending.length)notices.push(`추가 확인 전: ${pending.map(c=>checkNames[c]||c).join(' · ')}.`);
    $('assessment-note').textContent=notices.join(' ');
    document.querySelectorAll('#geometry-rows tr').forEach(row=>row.classList.toggle('geometry-selected',row.dataset.field===c.field_id));
    document.querySelectorAll('#sensor-diagram g[data-field] rect').forEach(rect=>rect.setAttribute('fill-opacity',rect.parentElement.dataset.field===c.field_id?'.38':'.12'));
    renderEvidence();
    $('case-downloads').replaceChildren();[['baseline_raw','기준 RAW 표시'],['projected_raw','적용 RAW 표시'],['arrays','원본 RAW · 배열'],['metrics','지표 JSON']].forEach(([key,label])=>link($('case-downloads'),label,fileUrl(files[key])));
    $('difference-note').textContent=c.difference_display||summary.conditions?.difference_preview||'고정 표시 범위의 차이 영상';renderMetrics(c);toggleSampling();
  }
  function toggleSampling() {document.querySelectorAll('.image-frame').forEach(el=>el.classList.toggle('pixelated',$('sampling').value==='pixelated'));}
  function render() {
    $('result').hidden=false;const source=summary.source||{},conditions=summary.conditions||{},cases=summary.cases||[];
    const evidence=summary.evidence||[],count=status=>evidence.filter(e=>e.trend_status===status).length;
    const stable=evidence.filter(e=>['magnitude_stable','direction_stable','two_grid_stable'].includes(e.trend_status)).length;
    $('overall-assessment').textContent=`수치 해석 ${conditions.solver_samples??'—'}조건 · RAW/ISP ${cases.length}조건 완료. 파장별 ${evidence.length}개 비교 중 두 격자 경향 확인 ${stable}개, 구분 어려움 ${count('indistinguishable')}개, 미결정 ${count('unresolved')}개. ${stable===0?'현재 모델과 정밀도에서는 OCL 이동의 유의한 개선을 확인하지 못했습니다.':'개별 격자·편광 판정과 미완료 확인 항목을 함께 보세요.'}`;
    if(summary.title)document.title=`${summary.title} | CameraE2E`;
    $('host-sensor').textContent=asText(source.host_sensor);
    $('capture').textContent=`${fmt(conditions.exposure_ms,1)} ms · ${fmt(conditions.gain,0)}×`;
    $('capture-note').textContent=`독립 취득 ${conditions.frames??'—'}회 · ${asText(conditions.illumination)}`;
    $('evidence-count').textContent=`${(summary.evidence||[]).length}개 조건`;$('case-count').textContent=`${cases.length}개 저장 결과`;
    if(source.trend_run_id)$('trend-link').href=`/sensor-structure.html?run=${encodeURIComponent(source.trend_run_id)}`;
    $('source-revision').textContent=`구조 revision: ${asText(source.structure_revision)} · 분광 campaign ${source.campaign_id||source.spectral_run_id||'—'} · 카메라 원본 ${source.camera_run_id||'—'} / ${source.camera_candidate_id||'—'}`;
    const unique=key=>[...new Set(cases.map(c=>c[key]))];
    selectOptions('field',unique('field_id'),fieldNames,'center');selectOptions('chart',unique('chart'),chartNames,'color');selectOptions('light',unique('light_scale').sort((a,b)=>a-b),null,1);selectOptions('profile',unique('profile_id'),profileNames,'spectral_response');$('profile-control').hidden=unique('profile_id').length<=1;
    if(query.has('stage')&&['isp','raw','difference'].includes(query.get('stage')))$('stage').value=query.get('stage');
    renderGeometry();renderEvidence();renderAssumptions();$('provenance').textContent=JSON.stringify({schema:summary.schema,status:summary.status,source,geometry:summary.geometry,spectral_response:summary.spectral_response,conditions,assumptions:summary.assumptions},null,2);
    $('summary-downloads').replaceChildren();link($('summary-downloads'),'전체 실행 · 결과 JSON',`${endpoint}/${encodeURIComponent(run)}`);
    drawCase();
  }
  async function load() {
    clearTimeout(timer);$('refresh').disabled=true;$('error').textContent='';
    try {
      if(!run){try{const latest=await json(`${endpoint}/latest`);run=latest.id||latest.run_id||'';}catch(_){/* A missing ready run is a neutral empty state. */}}
      if(!run){$('status').textContent='선택한 실행이 없습니다.';$('empty-help').hidden=false;return;}
      const job=await json(`${endpoint}/${encodeURIComponent(run)}`);const status=job.status||'unknown';
      $('run-info').textContent=`실행 ${job.id||run} · ${status}${job.progress?` · ${job.progress.completed}/${job.progress.total} · ${job.progress.stage}`:''}`;$('status-wrap').classList.toggle('ready',status==='succeeded');
      $('status').replaceChildren(make('span','', 'status-dot'),document.createTextNode(status==='succeeded'?'분광 응답·RAW·ISP 계산 완료 · 저장된 결과 탐색':status==='running'?'계산 진행 중 · 완료된 결과만 표시':status==='queued'?'실행 대기 중':`실행 상태: ${status}`));
      if(job.error)$('error').textContent=asText(job.error);
      summary=job.result?.summary||job.summary||null;
      if(summary&&Array.isArray(summary.cases))render();else $('result').hidden=true;
      if(['running','queued','cancelling','cancel_requested'].includes(status))timer=setTimeout(load,5000);
    } catch(error) {$('error').textContent=`결과를 읽지 못했습니다. ${error.message}`;$('status').textContent='조회 실패 · 저장 계산을 완료로 표시하지 않습니다.';}
    finally {$('refresh').disabled=false;}
  }
  ['field','chart','light','profile','stage'].forEach(id=>$(id).addEventListener('change',drawCase));$('sampling').addEventListener('change',toggleSampling);$('refresh').addEventListener('click',load);window.addEventListener('pagehide',()=>clearTimeout(timer));load();
})();

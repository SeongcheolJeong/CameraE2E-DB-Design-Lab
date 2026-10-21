"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const API = "/api/v1/reference-workshop";
  const state = {ready:false, catalog:{}, template:null, current:null, baseline:null, sweep:null, active:null, submitting:false, openToken:0, token:null, jobs:[], revisions:[], revision:null, draft:null, editor:"lens", field:"center", stage:"isp", chart:"resolution", zoom:1, pan:[0,0], poll:null, preparedJobs:new Map(), preparedToken:0, captureCache:new Map(), saturationToken:0, imageLoads:new Map(), imagesReady:Promise.resolve()};
  const pixelState={studies:[],template:null,study:null,a:null,b:null,baselineId:null,candidateId:null,wavelength:550,site:"R",mode:"pupil",colorMode:"munsell",story:"photon_budget",imageStage:"display",imageChart:"color",token:0,listToken:0,cache:new Map(),loading:false,active:null,poll:null,visible:false};
  const clone = value => JSON.parse(JSON.stringify(value));
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const fmt = (value, digits=2) => finite(value) ? value.toLocaleString("ko-KR", {maximumFractionDigits:digits,minimumFractionDigits:digits}) : "—";
  const esc = value => String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const get = (value,path) => path.split(".").reduce((node,key)=>node?.[key],value);
  const summary = job => job?.result?.summary || job?.result || null;
  const busy = job => ["queued","running","cancelling"].includes(job?.status);
  const finished = job => job?.status === "succeeded" && Boolean(summary(job));
  const configuration = view => view?.data?.configuration || view?.job?.specification || {};
  const hasAdoptedReadout = view => Boolean(view?.data?.asset_trial || view?.job?.asset_trial || view?.data?.pixel?.cell?.provenance?.design_asset_trial);
  const appliedReadout = view => view?.data?.pixel?.cell?.electrical_model || view?.data?.asset_trial?.applied_electrical_model || view?.data?.asset_trial?.applied_inputs || {};
  function readoutRevision(view){return view?.data?.design_asset_model_id||view?.job?.design_asset_model_id||view?.data?.pixel?.cell?.provenance?.design_asset_trial?.model_revision_id||null;}
  function lineageText(view){const trial=view?.data?.asset_trial,lineage=view?.data?.raw_lineage||view?.data?.lineage;if(trial)return `평균 광전자율 재사용 · 새 RAW ${trial.new_raw_frames??"—"}장 · 사용자 가정 readout revision ${readoutRevision(view)||"—"} · 취득 원본 RAW 보존`;return lineage?typeof lineage==="string"?lineage:JSON.stringify(lineage):view?.data?.source_run_id?`원본 RAW: ${view.data.source_run_id} · ISP 재처리`:"원본 취득과 처리 설정을 실행 결과에 함께 보존합니다.";}
  const fields = view => view?.data?.fields || [];
  const selectedField = view => fields(view).find(item=>item.id===state.field) || null;
  const successful = field => field && (!field.status || field.status === "succeeded" || field.status === "completed");
  const names = {queued:"대기",running:"계산 중",cancelling:"취소 중",cancelled:"취소됨",failed:"실패",succeeded:"완료"};
  const fallbackPoints = [
    {id:"center",label:"중심",x:0,y:0},
    {id:"mid-right",label:"중간 · 오른쪽",x:.5,y:0},{id:"mid-top",label:"중간 · 위",x:0,y:.5},
    {id:"mid-left",label:"중간 · 왼쪽",x:-.5,y:0},{id:"mid-bottom",label:"중간 · 아래",x:0,y:-.5},
    {id:"outer-ne",label:"주변 · 우상",x:.601,y:.601},{id:"outer-nw",label:"주변 · 좌상",x:-.601,y:.601},
    {id:"outer-sw",label:"주변 · 좌하",x:-.601,y:-.601},{id:"outer-se",label:"주변 · 우하",x:.601,y:-.601}
  ];
  const controls = {"lens.curvature_scale":["곡률 배율","×"],"lens.sensor_shift_um":["센서 Z 이동","µm"],"lens.optical_profile":["광학 계산 방식",""],"pixel.cfa_thickness_scale":["CFA 두께 배율","×"],"chart_suite":["Lab 입력 구성",""],"isp.schema":["ISP 파이프라인",""],"isp.demosaic_method":["Demosaic",""],"isp.raw_denoise.strength":["RAW denoise",""],"isp.linear_sharpen":["제한 Sharpen",""],"isp.raw_denoise.range_floor_dn2":["RAW range floor","DN²"],"isp.lsc_tuning":["LSC 강도 · gain 상한",""],"isp.color_calibration_id":["Color calibration",""],"isp.black_offset_dn":["추가 black offset","DN"],"isp.lsc_enabled":["고정 LSC",""],"isp.denoise":["Denoise",""],"isp.sharpen":["Sharpen",""],"isp.wb":["WB",""],"isp.ccm":["CCM",""],"isp.tone_gamma":["Tone gamma",""],"isp.display_white_level":["표시 white level",""],"exposure_ms":["노출","ms"],"gain":["Gain","×"],"luminance_cd_m2":["광량","cd/m²"],"frames":["반복 취득",""],"seed":["Seed",""]};
  function text(id,value){$(id).textContent=value;}
  function fileURL(view,path,field=null){
    if(path && typeof path === "object") path=path.path||path.file||path.url;
    if(typeof path!=="string" || !path) return null;
    if(path.startsWith("/") || /^https?:/.test(path)) return path;
    const base=field?.files_base || view?.data?.files_base || `${API}/runs/${encodeURIComponent(view.job.id)}/files/${view.candidateId?encodeURIComponent(view.candidateId)+"/":""}`;
    return base.replace(/\/$/,"")+"/"+path.split("/").map(encodeURIComponent).join("/");
  }
  function imagePath(view,stage=state.stage){
    const field=selectedField(view);
    if(!successful(field)) return null;
    if(["psf","opd","layout"].includes(stage)) return field.optics?.images?.[stage] || null;
    const images=field.images?.[state.chart] || field.image_sets?.[state.chart] || {};
    const keys={isp:["display","native_isp","isp"],linear:["linear","linear_rgb"],raw:["raw_preview","raw_png"]}[stage]||[stage];
    for(const key of keys)if(typeof images[key]==="string"&&!/\.(npy|npz)$/i.test(images[key]))return images[key];
    return null;
  }
  function viewFrom(job,candidateId=null){
    if(!finished(job))throw Error("완료된 실행만 결과로 열 수 있습니다.");
    const all=summary(job);
    if(all.candidates){
      const item=all.candidates.find(candidate=>candidate.id===candidateId);
      if(!item)throw Error("탐색 결과에서 정확한 후보를 선택하세요.");
      if(item.status && item.status!=="succeeded")throw Error("완료되지 않은 후보의 결과를 표시할 수 없습니다.");
      return {job,candidateId:item.id,data:item.summary||item.result?.summary||item};
    }
    if(candidateId)throw Error("이 실행에는 탐색 후보가 없습니다.");
    return {job,candidateId:null,data:all};
  }
  function canonicalValue(value){if(Array.isArray(value))return value.map(canonicalValue);if(value&&typeof value==="object")return Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonicalValue(value[key])]));return value;}
  function sameValue(a,b){return JSON.stringify(canonicalValue(a))===JSON.stringify(canonicalValue(b));}
  function equivalent(a,b,ignoreISP=false){const aa=clone(a),bb=clone(b);delete aa.name;delete bb.name;aa.chart_suite=aa.chart_suite||"classic_v1";bb.chart_suite=bb.chart_suite||"classic_v1";if(ignoreISP){delete aa.isp;delete bb.isp;}return sameValue(aa,bb);}
  function sampleCurve(x,y,at){if(!Array.isArray(x)||!Array.isArray(y)||x.length!==y.length||!x.length)return null;if(at<x[0]||at>x.at(-1))return null;for(let i=1;i<x.length;i++)if(x[i]>=at){if(!finite(y[i])||!finite(y[i-1]))return null;const t=(at-x[i-1])/(x[i]-x[i-1]);return y[i-1]*(1-t)+y[i]*t;}return finite(y.at(-1))?y.at(-1):null;}
  function opticalCurve(field){
    const mtf=field?.optics?.mtf;
    if(Array.isArray(mtf))return mtf.find(item=>item.wavelength_nm===550)||mtf[0]||null;
    return mtf||null;
  }
  function metric(field,kind){
    if(!successful(field))return null;
    const m=field.metrics||{},o=field.optics||{},t=field.tests||{};
    if(kind==="mtf"){const curve=opticalCurve(field);return m.mtf_at_0_125??o.mtf_at_0_125??sampleCurve(curve?.cycles_per_native_pixel||curve?.frequency_cycles_per_pixel,curve?.radial||curve?.mtf,.125);}
    if(kind==="cra")return o.cra_deg??o.chief_ray_angle_deg??m.cra_deg??field.geometry?.cra_deg??null;
    if(kind==="ri")return o.relative_illumination??m.relative_illumination??null;
    if(kind==="color")return m.delta_e00??t.color?.delta_e00_mean??null;
    if(kind==="noise")return m.temporal_noise_dn??t.noise?.uniform?.raw_green?.temporal_noise_dn??t.noise?.uniform?.raw_green?.mean_temporal_std_dn??null;
    if(kind==="electrons")return m.mean_electrons??t.signal?.mean_electrons??null;
    if(kind==="texture_retention")return t.isp_lab?.texture_retention??null;
    if(kind==="lab_noise")return t.isp_lab?.temporal_noise_luma??null;
    if(kind==="demosaic_chroma")return t.isp_lab?.demosaic?.chroma_residual_rms??null;
    if(kind==="texture")return m.texture_contrast_ratio??t.texture?.contrast_ratio_to_linear??null;
    if(kind==="processed_noise")return t.noise?.uniform?.processed?.temporal_std??null;
    if(kind==="processed_snr")return t.noise?.uniform?.processed?.snr_db??null;
    if(kind==="sfr50")return t.resolution?.sites?.center?.processed_system?.mtf50_cycles_per_pixel??null;
    if(kind==="overshoot"){const n=t.resolution?.sites?.center?.processed_system?.overshoot_fraction;return finite(n)?n*100:null;}
    return m[kind]??null;
  }
  async function api(path,payload){
    if(payload!==undefined&&!state.token){const result=await fetch("/api/v1/session");if(!result.ok)throw Error("세션을 확인할 수 없습니다. 새로고침하세요.");state.token=(await result.json()).token;}
    const result=await fetch(API+path,{method:payload===undefined?"GET":"POST",headers:payload===undefined?{}:{"Content-Type":"application/json","x-platform-token":state.token},...(payload===undefined?{}:{body:JSON.stringify(payload)})});
    let value;try{value=await result.json();}catch{throw Error(`서버 응답을 읽지 못했습니다 (${result.status}).`);}
    if(!result.ok){const message=value.detail??value.error??value.message??value;throw Error(typeof message==="string"?message:JSON.stringify(message));}return value;
  }
  function notice(message,type="ready"){text("status-message",message);$("status-bar").classList.toggle("busy",type==="busy");$("status-bar").classList.toggle("error",type==="error");}
  function error(err){notice(err.message||String(err),"error");buttons();}
  function toast(message){text("toast",message);$("toast").classList.remove("hidden");clearTimeout(state.toast);state.toast=setTimeout(()=>$("toast").classList.add("hidden"),4000);}
  function v2Profile(source={}){
    const base=state.catalog.isp_v2_profile||{};
    return {schema:"camera_reference_isp_v2",lsc_enabled:true,wb:clone(source.wb||base.wb||[1,1,1]),ccm:clone(source.ccm||base.ccm||[[1,0,0],[0,1,0],[0,0,1]]),tone_gamma:source.tone_gamma??base.tone_gamma??2.2,display_white_level:source.display_white_level??base.display_white_level??1,black_offset_dn:0,demosaic_method:"adaptive_laplacian",lsc_tuning:clone(source.lsc_tuning||{strength:1,gain_cap:4}),raw_denoise:clone(base.raw_denoise||{strength:.4,range_floor_dn2:1}),linear_sharpen:clone(base.linear_sharpen||{amount:.3,threshold:.01,max_delta:.05}),color_calibration_id:source.schema==="camera_reference_isp_v2"?source.color_calibration_id??null:null};
  }
  function readSpec(){
    if(!state.draft)throw Error("Reference를 아직 불러오지 못했습니다.");
    const spec=clone(state.draft);spec.name=$("run-name").value.trim();
    spec.lens={...spec.lens,curvature_scale:Number($("curvature").value),sensor_shift_um:Number($("sensor-z").value),optical_profile:$("optical-profile").value};
    spec.pixel={...spec.pixel,cfa_thickness_scale:Number($("cfa-scale").value)};
    for(const [key,id] of [["exposure_ms","exposure"],["gain","gain"],["luminance_cd_m2","luminance"],["frames","frames"],["seed","seed"]])spec[key]=Number($(id).value);
    spec.chart_suite=$("include-chirp")?.checked?"isp_lab_v1":"classic_v1";
    const ccm=$("ccm").value.trim().split(/\n/).map(row=>row.trim().split(/[\s,]+/).map(Number));
    if(ccm.length!==3||ccm.some(row=>row.length!==3||row.some(n=>!finite(n))))throw Error("CCM은 3 × 3 숫자 행렬이어야 합니다.");
    const common={wb:["r","g","b"].map(c=>Number($("wb-"+c).value)),ccm,tone_gamma:Number($("tone-gamma").value),display_white_level:Number($("display-white").value)};
    if($("isp-family")?.value==="v2")spec.isp={...v2Profile(),...common,lsc_enabled:$("lsc-enabled").checked,lsc_tuning:{strength:Number($("lsc-strength").value),gain_cap:Number($("lsc-gain-cap").value)},black_offset_dn:Number($("black-offset").value),demosaic_method:$("demosaic-method").value,raw_denoise:{strength:Number($("raw-denoise").value),range_floor_dn2:Number($("range-floor").value)},linear_sharpen:{amount:Number($("bounded-sharpen").value),threshold:Number($("sharpen-threshold").value),max_delta:Number($("sharpen-max-delta").value)},color_calibration_id:$("color-calibration-id").value.trim()||null};
    else spec.isp={...(spec.isp?.schema==="camera_reference_isp_v2"?{}:spec.isp),...common,lsc_enabled:$("isp-profile").value!=="baseline",lsc_strength:0,denoise:Number($("denoise").value),sharpen:Number($("sharpen").value)};
    if(spec.isp.schema==="camera_reference_isp_v2"&&!state.draft.isp?.lsc_tuning&&spec.isp.lsc_tuning.strength===1&&spec.isp.lsc_tuning.gain_cap===4)delete spec.isp.lsc_tuning;
    if(!spec.name)throw Error("파생 설계 이름을 입력하세요.");return spec;
  }
  function applySpec(spec){
    state.draft=clone(spec);$("run-name").value=spec.name||"Reference design";$("curvature").value=spec.lens.curvature_scale;$("sensor-z").value=spec.lens.sensor_shift_um;$("optical-profile").value=spec.lens.optical_profile||"geometric_retrace";$("cfa-scale").value=spec.pixel.cfa_thickness_scale;
    for(const[key,id]of[["exposure_ms","exposure"],["gain","gain"],["luminance_cd_m2","luminance"],["frames","frames"],["seed","seed"]])$(id).value=spec[key];
    $("include-chirp").checked=spec.chart_suite==="isp_lab_v1";
    const isp=spec.isp||{},v2=isp.schema==="camera_reference_isp_v2",advanced=v2?isp:v2Profile(isp);$("isp-family").value=v2?"v2":"legacy";$("isp-profile").value=!isp.lsc_enabled?"baseline":isp.denoise>=.5?"lsc_denoise":"lsc";
    for(const[key,id]of[["denoise","denoise"],["sharpen","sharpen"],["tone_gamma","tone-gamma"],["display_white_level","display-white"]])$(id).value=isp[key]??(key==="display_white_level"?10:0);
    $("lsc-enabled").checked=advanced.lsc_enabled;$("lsc-strength").value=advanced.lsc_tuning?.strength??1;$("lsc-gain-cap").value=advanced.lsc_tuning?.gain_cap??4;$("demosaic-method").value=advanced.demosaic_method;$("raw-denoise").value=advanced.raw_denoise.strength;$("range-floor").value=advanced.raw_denoise.range_floor_dn2;$("bounded-sharpen").value=advanced.linear_sharpen.amount;$("sharpen-threshold").value=advanced.linear_sharpen.threshold;$("sharpen-max-delta").value=advanced.linear_sharpen.max_delta;$("black-offset").value=advanced.black_offset_dn??0;$("color-calibration-id").value=advanced.color_calibration_id||"";
    ["r","g","b"].forEach((c,i)=>$("wb-"+c).value=(isp.wb||[1,1,1])[i]);$("ccm").value=(isp.ccm||[[1,0,0],[0,1,0],[0,0,1]]).map(row=>row.join(", ")).join("\n");refreshEditor();
  }
  function refreshEditor(){
    text("denoise-value",fmt(Number($("denoise").value)));text("sharpen-value",fmt(Number($("sharpen").value)));text("raw-denoise-value",fmt(Number($("raw-denoise").value)));text("bounded-sharpen-value",fmt(Number($("bounded-sharpen").value)));text("lsc-strength-value",fmt(Number($("lsc-strength").value)));
    const v2=$("isp-family").value==="v2";$("legacy-isp-controls").classList.toggle("hidden",v2);$("raw-isp-controls").classList.toggle("hidden",!v2);$("display-white").min=v2?1:.0001;$("display-white").max=v2?100:10000;$("tone-gamma").max=v2?4:3;
    // Hidden controls must not block the browser's form validation.
    for(const id of ["legacy-isp-controls","raw-isp-controls"])$(id).querySelectorAll("input,select,button").forEach(node=>node.disabled=(id==="legacy-isp-controls")===v2);
    text("optical-profile-note",$("optical-profile").value==="wavefront"?"회절 PSF의 지원 시야를 검사합니다. 지원되지 않는 주변부는 실패로 표시하며 기하 PSF로 자동 교체하지 않습니다.":"기하 광선의 착지 분포를 사용합니다. 회절을 포함한 PSF로 해석하지 않습니다.");buttons();
  }
  function buttons(){
    const active=Boolean(state.active)||state.submitting,adopted=hasAdoptedReadout(state.current);for(const id of["run-button","save-revision"])$(id).disabled=!state.ready||active||adopted;$("readout-trial-notice").classList.toggle("hidden",!adopted);
    $("design-controls").disabled=!state.ready;$("reset-config").disabled=!state.ready;
    for(const id of ["constraint-texture","constraint-halo","constraint-color","constraint-noise"])$(id).disabled=!state.ready;
    $("pin-baseline").disabled=!state.current;$("cancel-run").classList.toggle("hidden",!state.active);
    let same=false,changed=false;try{const spec=readSpec();same=Boolean(state.current&&equivalent(spec,configuration(state.current),true));changed=Boolean(state.current&&!equivalent(spec,configuration(state.current)));}catch{}
    $("replay-button").disabled=!state.ready||active||!same;$("configuration-notice").classList.toggle("hidden",!changed);
    text("replay-note",!state.current?"완료된 취득을 선택하면 활성화됩니다.":same?"원본 RAW를 유지하며 ISP만 다시 계산합니다.":"광학·픽셀·취득 조건이 달라졌습니다. 새 취득이 필요합니다.");
    document.querySelectorAll("[data-study]").forEach(button=>button.disabled=!state.ready||active||(adopted&&["optical","pixel"].includes(button.dataset.study))||(["lsc","denoise"].includes(button.dataset.study)&&!same));
    const chirp=sourceHasChirp(state.current);
    document.querySelectorAll("[data-isp-recipe],[data-isp-action]").forEach(button=>{const missing=button.dataset.ispRecipe==="demosaic"&&chirp===false;button.disabled=!state.ready||active||!same||missing;button.title=missing?"선택한 RAW에 chirp가 없습니다. 추가 취득하거나 준비된 demosaic 결과를 여세요.":!same?"선택 원본과 광학·픽셀·취득 조건을 같게 유지하세요.":"현재 선택 원본으로 새 비교 작업을 제출합니다.";});
    tuningButtons(same,active);
    text("isp-recipe-note",!state.current?"완료된 RAW를 선택해 시작하세요.":!same?"편집한 취득 조건이 원본과 다릅니다. 원본 설정을 다시 열거나 신규 취득하세요.":chirp===null?"미리 계산은 원본 RAW를 참조합니다. 추가 차트 지원 여부는 제출 시 원본에서 확인합니다.":chirp?"현재 원본으로 세 recipe를 실행할 수 있습니다. 설정을 바꾸어도 원본 RAW는 유지합니다.":"현재 RAW로 잡음·색 비교가 가능합니다. Demosaic 비교에는 chirp 추가 취득이 필요합니다.");
  }
  function setEditor(editor){if(!["lens","pixel","isp"].includes(editor))return;state.editor=editor;for(const name of["lens","pixel","isp"])$(name+"-editor").classList.toggle("hidden",name!==editor);document.querySelectorAll("[data-editor]").forEach(button=>{button.classList.toggle("active",button.dataset.editor===editor);button.setAttribute("aria-selected",String(button.dataset.editor===editor));});if(editor==="pixel")showPixelLab();updateURL();}
  function chooseEditor(query){const editor=query.get("editor");if(["lens","pixel","isp"].includes(editor))setEditor(editor);}
  function points(){const listed=state.catalog.fields;return Array.isArray(listed)&&listed.length?listed:fallbackPoints;}
  function pointXY(point){const fallback=fallbackPoints.find(item=>item.id===point.id)||{x:0,y:0};const xy=point.normalized_xy||point.normalized_position;return [point.normalized_x??point.x??xy?.[0]??fallback.x,point.normalized_y??point.y??xy?.[1]??fallback.y];}
  function renderMap(){
    const root=$("field-map"),kind=$("field-metric").value;root.innerHTML='<div class="field-ring"></div><div class="field-ring inner"></div>';
    for(const point of points()){
      const field=fields(state.current).find(item=>item.id===point.id),value=metric(field,kind),button=document.createElement("button"),[x,y]=pointXY(point);
      button.className="field-point"+(!field?" pending":!successful(field)?" failed":"")+(point.id===state.field?" active":"");button.style.left=(50+x*50)+"%";button.style.top=(50-y*43)+"%";button.setAttribute("aria-pressed",String(point.id===state.field));
      button.innerHTML=`<span>${esc(point.label||point.id)}</span><strong>${!field?"입력 위치":!successful(field)?"실패":fmt(value,kind==="mtf"?3:2)}</strong>`;
      button.title=!successful(field)&&field?(typeof field.error==="string"?field.error:JSON.stringify(field.error||field.issues||"해당 시야 계산 실패")):point.label||point.id;
      button.onclick=()=>{state.field=point.id;renderResults();renderSweep();updateURL();};root.append(button);
    }
    const point=points().find(item=>item.id===state.field),field=selectedField(state.current),[x,y]=point?pointXY(point):[0,0];text("field-title",field?.label||point?.label||state.field);text("field-coordinate",`정규화 위치 (${fmt(x,3)}, ${fmt(y,3)}) · ${state.field}`);
    if(field&&!successful(field))$("field-facts").innerHTML=`<p class="field-error">${esc(typeof field.error==="string"?field.error:JSON.stringify(field.error||field.issues||"해당 위치 계산 실패"))}</p>`;
    else $("field-facts").innerHTML=`<dl><dt>CRA</dt><dd>${fmt(metric(field,"cra"))}°</dd><dt>상대조도</dt><dd>${fmt(metric(field,"ri"),3)}</dd><dt>왜곡</dt><dd>${fmt(field?.optics?.distortion_percent??field?.metrics?.distortion_percent)}%</dd><dt>평균 광전자</dt><dd>${fmt(metric(field,"electrons"),1)} e</dd></dl>`;
  }
  function renderImage(prefix,view){
    const field=selectedField(view),path=imagePath(view),url=path?fileURL(view,path,field):null,image=$(prefix+"-image"),empty=$(prefix+"-empty");
    let ready=Promise.resolve();
    if(url){
      const target=new URL(url,location.href).href;
      if(image.currentSrc===target&&image.complete&&image.naturalWidth>0){state.imageLoads.get(prefix)?.finish();state.imageLoads.delete(prefix);image.hidden=false;empty.classList.add("hidden");}
      else{const prior=state.imageLoads.get(prefix);if(prior?.target===target&&!prior.settled)ready=prior.ready;else{prior?.finish();image.hidden=true;empty.classList.remove("hidden");empty.querySelector("strong").textContent="선택한 계산 영상을 불러오는 중";const load={target,settled:false};ready=load.ready=new Promise(resolve=>{load.finish=()=>{load.settled=true;resolve();};});state.imageLoads.set(prefix,load);image.onload=()=>{if(state.imageLoads.get(prefix)===load&&image.currentSrc===target){image.hidden=false;empty.classList.add("hidden");load.finish();}};image.onerror=()=>{if(state.imageLoads.get(prefix)===load){image.hidden=true;empty.classList.remove("hidden");empty.querySelector("strong").textContent="저장된 영상을 열 수 없습니다";load.finish();}};image.src=url;}}
    }else{state.imageLoads.get(prefix)?.finish();state.imageLoads.delete(prefix);image.hidden=true;empty.classList.remove("hidden");image.onload=image.onerror=null;image.removeAttribute("src");empty.querySelector("strong").textContent=field&&!successful(field)?"이 시야의 계산이 실패했습니다":view?"이 단계의 저장 영상이 없습니다":prefix==="baseline"?"기준안의 실제 결과":"변경한 설계의 실제 결과";}
    text(prefix+"-name",configuration(view).name||(prefix==="baseline"?"기준안":"파생 설계"));text(prefix+"-meta",view?`${view.job.id}${view.candidateId?" / "+view.candidateId:""}`:"완료된 실행을 선택하세요.");
    const shape=field?.geometry?.native_shape_row_col||field?.geometry?.output_shape_row_col||field?.geometry?.roi_shape_row_col;
    text(prefix+"-geometry",field?`${shape?shape[1]+" × "+shape[0]+" native":"native ROI"} · ${field.label||field.id} · ${configuration(view).lens?.optical_profile==="wavefront"?"회절 PSF":"기하 PSF · 회절 제외"}`:"계산 결과 없음");
    const link=$(prefix+"-native");link.classList.toggle("hidden",!url);if(url)link.href=url;
    return ready;
  }
  function renderViewer(){
    for(const button of document.querySelectorAll("[data-stage]")){const missing=Boolean(state.current)&&!imagePath(state.current,button.dataset.stage)&&!imagePath(state.baseline,button.dataset.stage);button.disabled=false;button.classList.toggle("unavailable",missing);button.title=missing?"이 단계의 저장 영상이 없습니다.":"저장된 첫 프레임의 처리 단계";button.classList.toggle("active",button.dataset.stage===state.stage);button.setAttribute("aria-selected",String(button.dataset.stage===state.stage));}
    state.imagesReady=Promise.all([renderImage("baseline",state.baseline),renderImage("candidate",state.current)]);
    const view=state.current,field=selectedField(view),images=successful(field)?field.images?.[state.chart]||{}:{};
    for(const[id,key]of[["raw-array","raw"],["mean-array","mean"]]){const path=images[key];$(id).classList.toggle("hidden",!path);if(path)$(id).href=fileURL(view,path,field);}
    $("candidate-bundle").classList.toggle("hidden",!view);if(view)$("candidate-bundle").href=`${API}/runs/${encodeURIComponent(view.job.id)}/bundle`;
    const report=view?.data?.report_url||view?.job?.result?.report_url||null;$("report-link").classList.toggle("disabled",!report);$("report-link").setAttribute("aria-disabled",String(!report));if(report)$("report-link").href=report;else $("report-link").removeAttribute("href");
    const a=configuration(state.baseline).isp?.display_white_level,b=configuration(view).isp?.display_white_level;text("display-note",finite(a)&&finite(b)?`표시 white · A ${fmt(a,1)} / B ${fmt(b,1)}${a!==b?" · 서로 다른 표시 범위":" · 공통 표시 범위"}`:"프레임별 밝기 정규화 없음");
    text("raw-lineage",lineageText(view));
    if(["raw_lsc","raw_denoise","demosaic","sharpen_delta","demosaic_residual","valid_mask"].includes(state.stage))text("display-note","저장된 첫 프레임 진단 · 단계별 단위·표시 범위는 보고서 참조");
    renderSaturation();
  }
  function renderMetrics(){
    const a=selectedField(state.baseline),b=selectedField(state.current),items=[["mtf","광학 MTF", "@ 0.125 cy/native pixel",3],["color","색 오차","평균 ΔE00 · 색 차트",2],["noise","RAW 시간 잡음","균일장 · 반복 취득 · DN",2],["electrons","평균 광전자","균일장 · e · 처리 이전",1],["processed_noise","처리 후 시간 잡음","균일장 · linear sRGB · 반복 취득",5],["processed_snr","처리 후 SNR","dB · 균일장",2],["texture","질감 대비 비율","노이즈 없는 처리 진단",3],["overshoot","처리 후 Overshoot","% · ROI 중심 edge",2],["texture_retention","고정 기준 질감 보존","ISP Lab · 공통 bypass 기준",3],["lab_noise","선형 휘도 시간 잡음","ISP Lab · 반복 취득",6],["sfr50","처리 후 SFR50","cy/native pixel · edge",3],["demosaic_chroma","재구성 색 잔차","모델 내부 4채널 응답 기준",5]];
    $("metric-cards").innerHTML=items.map(([key,label,unit,digits])=>`<article class="bench-metric"><h3>${label}</h3><div class="metric-pair"><span class="baseline-number">${fmt(metric(a,key),digits)}</span><strong>${fmt(metric(b,key),digits)}</strong></div><p>${unit}</p></article>`).join("");
    const curves=[];for(const[view,color]of[[state.baseline,"#b8823c"],[state.current,"#087f83"]]){const curve=opticalCurve(selectedField(view));if(curve)for(const[direction,dash]of[["radial",false],["tangential",true]]){const x=curve.cycles_per_native_pixel||curve.frequency_cycles_per_pixel,y=curve[direction];if(Array.isArray(x)&&Array.isArray(y))curves.push({x,y,color,dash});}}
    drawMTF(curves);text("mtf-note",`${configuration(state.current).lens?.optical_profile==="wavefront"?"회절 PSF":"기하 광선 PSF · 회절 제외"} · 수신 픽셀 면적 적분 포함 · 550 nm · 실선 방사 / 점선 접선 · ISP SFR과 구분`);
  }
  function drawMTF(curves){
    if(!curves.length){$("mtf-chart").innerHTML='<div class="chart-empty">선택 위치의 저장된 MTF 곡선이 없습니다.</div>';return;}
    const width=650,height=180,p={l:35,r:18,t:8,b:30},x=n=>p.l+n/.5*(width-p.l-p.r),y=n=>height-p.b-n*(height-p.t-p.b);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="A와 B 광학 MTF 곡선">`;
    for(const n of[0,.25,.5,.75,1])svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(n)}" y2="${y(n)}" stroke="#e4ece2"/><text x="${p.l-7}" y="${y(n)+3}" text-anchor="end" font-size="9" fill="#8ca18e">${n}</text>`;
    for(const n of[0,.1,.2,.3,.4,.5])svg+=`<text x="${x(n)}" y="${height-12}" text-anchor="middle" font-size="9" fill="#8ca18e">${n}</text>`;
    for(const curve of curves){const points=curve.x.map((n,i)=>finite(n)&&finite(curve.y[i])&&n>=0&&n<=.5?`${x(n)},${y(curve.y[i])}`:null).filter(Boolean).join(" ");svg+=`<polyline points="${points}" fill="none" stroke="${curve.color}" stroke-width="1.8"${curve.dash?' stroke-dasharray="5 4"':""}/>`;}
    $("mtf-chart").innerHTML=svg+'</svg>';
  }
  async function captureSaturation(view,kind){
    const field=selectedField(view),raw=successful(field)?field.images?.[kind]?.raw:null;
    if(typeof raw!=="string")return null;
    const path=raw.slice(0,raw.lastIndexOf("/")+1)+"capture.json",url=fileURL(view,path,field);
    if(!state.captureCache.has(url))state.captureCache.set(url,fetch(url).then(async result=>{if(!result.ok)throw Error(`capture ${result.status}`);const value=await result.json();return {source:url,fractions:value.saturation_fractions||null};}).catch(()=>null));
    return state.captureCache.get(url);
  }
  async function renderSaturation(){
    const token=++state.saturationToken,root=$("saturation-note");
    if(!state.current){root.className="saturation-note";root.textContent="장면별 원본 RAW의 포화율을 확인합니다.";return;}
    root.textContent="A/B 영상 확인 후 원본 RAW 포화율을 불러옵니다.";
    await state.imagesReady;
    if(token!==state.saturationToken)return;
    root.textContent="선택 시야의 원본 RAW 포화율을 불러오는 중입니다.";
    const labels={color:"색 패치 · ΔE",resolution:"Slanted edge · SFR",uniform:"균일장",texture:"질감"},kinds=[...new Set(["color","resolution",state.chart])];
    const rows=await Promise.all(kinds.map(async kind=>({kind,a:await captureSaturation(state.baseline,kind),b:await captureSaturation(state.current,kind)})));
    if(token!==state.saturationToken)return;
    let clipped=false;
    const values=record=>{const f=record?.fractions;if(!f)return "조회 불가";const well=f.well_saturated_mask,adc=f.adc_high_mask;if((well||0)>0||(adc||0)>0)clipped=true;return `Well ${fmt(finite(well)?well*100:null,2)}% · ADC ${fmt(finite(adc)?adc*100:null,2)}%`;};
    const body=rows.map(row=>`<tr><th>${labels[row.kind]||esc(row.kind)}</th><td>${values(row.a)}</td><td>${values(row.b)}</td></tr>`).join("");
    root.className="saturation-note"+(clipped?" clipped":"");root.innerHTML=`<strong>${clipped?"포화가 포함된 비교 · ΔE/SFR에 clipping의 영향이 함께 반영됩니다.":"원본 RAW 포화 진단 · 장면마다 구분합니다."}</strong><table><thead><tr><th>선택 위치 · 첫 RAW</th><th>A 기준</th><th>B 변경</th></tr></thead><tbody>${body}</tbody></table><small>독립 반복 중 첫 취득의 well / ADC 상단 포화율입니다. 광량·노출을 자동으로 낮추지 않습니다.</small>`;
  }
  function renderChanges(){
    if(!state.current||!state.baseline){text("changes","기준안 A와 변경안 B를 열면 변경값을 표시합니다.");return;}
    const a=configuration(state.baseline),b=configuration(state.current),changed=Object.entries(controls).filter(([path])=>!sameValue(get(a,path),get(b,path)));
    const rows=changed.map(([path,[label,unit]])=>`<tr><td>${label}</td><td>${esc(JSON.stringify(get(a,path)))} ${unit}</td><td>${esc(JSON.stringify(get(b,path)))} ${unit}</td></tr>`);
    const ra=appliedReadout(state.baseline),rb=appliedReadout(state.current),adopted=hasAdoptedReadout(state.current)||hasAdoptedReadout(state.baseline);
    for(const[key,label,unit]of[["read_noise_e","Read noise","e⁻ RMS"],["dark_current_e_per_s","Dark current","e⁻/s"]])if(adopted||!sameValue(ra[key],rb[key]))rows.push(`<tr><td>${label} · 실제 readout</td><td>${fmt(ra[key],3)} ${unit}</td><td>${fmt(rb[key],3)} ${unit}</td></tr>`);
    $("changes").innerHTML=(rows.length?'<table class="change-table"><thead><tr><th>설계 변수</th><th>A 기준</th><th>B 변경</th></tr></thead><tbody>'+rows.join("")+"</tbody></table>":"동일한 설정입니다. 새로운 변경을 적용해 비교할 수 있습니다.")+(adopted?`<p class="field-note">사용자 가정 모델 · A ${esc(readoutRevision(state.baseline)||"기준 공통 readout 가정")} → B ${esc(readoutRevision(state.current)||"기준 공통 readout 가정")}<br>${state.current?.data?.asset_trial||state.baseline?.data?.asset_trial?"광전자율을 유지하고 해당 readout으로 새 RAW를 취득했습니다.":"해당 readout을 적용한 보존 RAW와 ISP 결과를 비교합니다."} 제품의 측정 회로 사양이 아닙니다. <a href="/design-library.html?family=sensor">Readiness Lab에서 변경·시험 ↗</a></p>`:"");
  }
  function renderReference(){
    const reference=state.current?.data?.reference||state.catalog.reference||{},lens=reference.lens||{},pixel=reference.sensor||reference.pixel||{};
    text("lens-reference",lens.title||lens.name||lens.patent_id||reference.lens_name||"특허 렌즈 Reference");text("lens-origin",[lens.configuration||lens.configuration_name,lens.revision_id||lens.revision||reference.lens_revision_id].filter(Boolean).join(" · ")||"고정된 특허 처방 revision");
    text("sensor-reference",pixel.title||pixel.name||pixel.model||reference.sensor_name||(pixel.source_report?"IMX462 · "+pixel.source_report:"보고서 기반 픽셀"));text("sensor-origin",[pixel.report_code||pixel.source_report||reference.sensor_report_code,finite(pixel.pixel_pitch_um)?`${pixel.pixel_pitch_um} µm · 원래 pitch`:"원래 pitch 유지",pixel.source||"보고서 기반 구조 · 공통 readout 가정"].filter(Boolean).join(" · "));
    const prescription=state.current?.data?.prescription||reference.prescription||lens.prescription||{},surfaces=prescription.surfaces||lens.surfaces||[];text("prescription-title",state.current?.data?.prescription?"선택 실행의 실제 변경 처방":"기준 렌즈 처방 · 변경값은 비교표 참조");
    $("prescription-body").innerHTML=surfaces.length?surfaces.map((row,i)=>`<tr><td>${esc(row.id??row.name??i)}</td><td>${row.radius_mm===null?"∞":fmt(row.radius_mm??row.radius,4)}</td><td>${fmt(row.thickness_mm??row.thickness,4)}</td><td>${esc(row.material||row.material_name||(row.refractive_index_after?"선언된 분산 모델":"—"))}</td><td>${fmt(row.semi_diameter_mm??row.aperture_radius_mm,3)}</td></tr>`).join(""):'<tr><td colspan="5">선택한 Reference의 처방을 불러오면 표시합니다.</td></tr>';
    const cell=state.current?.data?.pixel?.cell||{},classification=cell.field_classification||{},sourceFacts=classification.source_derived||{},assumptions=classification.common_assumptions||{};
    const classes=`<div class="evidence-columns"><section><h4>보고서 기반 값</h4><pre>${esc(JSON.stringify(sourceFacts,null,2))}</pre></section><section><h4>공통 모델 가정</h4><pre>${esc(JSON.stringify(assumptions,null,2))}</pre></section><section><h4>이번 변경값</h4><pre>${esc(JSON.stringify(classification.user_overrides||{},null,2))}</pre></section></div>`;
    const limits=state.current?.data?.limitations||state.catalog.limitations||[];$("provenance").innerHTML=(Object.keys(classification).length?classes:"")+'<ul>'+limits.map(value=>`<li>${esc(typeof value==="string"?value:JSON.stringify(value))}</li>`).join("")+`</ul><pre>${esc(JSON.stringify({reference,calibration:state.current?.data?.calibration||null,configuration:state.current?configuration(state.current):state.template},null,2))}</pre>`;
  }
  function renderResults(){renderMap();renderViewer();renderMetrics();renderChanges();renderReference();renderIspDiagnostics();renderTuningDiagnostics();renderPreviewScope();buttons();}
  function updateURL(){if(!state.current)return;const url=new URL(location.href);url.search="";url.searchParams.set("run",state.current.job.id);if(state.current.candidateId)url.searchParams.set("candidate",state.current.candidateId);if(state.baseline){url.searchParams.set("baseline",state.baseline.job.id);if(state.baseline.candidateId)url.searchParams.set("baseline_candidate",state.baseline.candidateId);}url.searchParams.set("field",state.field);url.searchParams.set("chart",state.chart);url.searchParams.set("stage",state.stage);if(state.editor!=="lens"||["lens","pixel","isp"].includes(new URL(location.href).searchParams.get("editor")))url.searchParams.set("editor",state.editor);history.replaceState(null,"",url);}
  function selectView(view){if(view.data.preview_scope){state.sweep=null;state.field=view.data.preview_scope.field_id;state.chart=view.data.preview_scope.chart;$("chart").value=state.chart;}state.current=view;state.revision=null;$("revision-select").value="";applySpec(configuration(view));renderResults();renderSweep();updateURL();}
  function renderSweep(){
    const value=summary(state.sweep),root=$("sweep-result");root.replaceChildren();root.className=value?.candidates?.length?"candidate-grid":"sweep-empty";
    renderIspStudy();
    if(!value?.candidates?.length){root.textContent="계산된 후보를 선택하면 해당 설정·시야·원본을 함께 엽니다.";return;}
    const candidateSpec=item=>(item.summary||item.result?.summary||item).configuration||{},curvatures=[...new Set(value.candidates.map(item=>candidateSpec(item).lens?.curvature_scale))].sort((a,b)=>a-b),shifts=[...new Set(value.candidates.map(item=>candidateSpec(item).lens?.sensor_shift_um))].sort((a,b)=>a-b),pairs=new Set(value.candidates.map(item=>JSON.stringify([candidateSpec(item).lens?.curvature_scale,candidateSpec(item).lens?.sensor_shift_um]))),matrix=value.candidates.length===9&&curvatures.length===3&&shifts.length===3&&pairs.size===9&&curvatures.every(finite)&&shifts.every(finite);
    const candidates=matrix?[...value.candidates].sort((a,b)=>candidateSpec(a).lens.curvature_scale-candidateSpec(b).lens.curvature_scale||candidateSpec(a).lens.sensor_shift_um-candidateSpec(b).lens.sensor_shift_um):value.candidates;
    if(matrix){root.classList.add("reference-optical-grid");const caption=document.createElement("p");caption.className="optical-grid-caption";caption.textContent=`행: 곡률 ×${curvatures.map(x=>fmt(x,3)).join(" / ×")} · 열: 센서 Z ${shifts.join(" / ")} µm · 현재 선택 시야의 계산값`;root.append(caption);}
    for(const item of candidates){
      const button=document.createElement("button"),data=item.summary||item.result?.summary||item,spec=data.configuration||{},isp=spec.isp||{},study=value.study,row=study?.selection?.rows?.find(r=>r.candidate_id===item.id);
      button.className="candidate-card";button.classList.toggle("selected",state.current?.job.id===state.sweep.id&&state.current?.candidateId===item.id);
      const ok=!item.status||item.status==="succeeded",field=(data.fields||[]).find(f=>f.id===state.field);button.disabled=!ok;
      const configText=study?`${esc(isp.demosaic_method||"bilinear")} · RAW NR ${fmt(isp.raw_denoise?.strength,2)}<br>Sharpen ${fmt(isp.linear_sharpen?.amount,2)} · LSC ${fmt(isp.lsc_tuning?.strength??1,2)} / cap ${fmt(isp.lsc_tuning?.gain_cap??4,1)}<br>${isp.color_calibration_id?"고정 Munsell CCM":"원본 CCM"}`:`곡률 ×${fmt(spec.lens?.curvature_scale,3)} · Z ${fmt(spec.lens?.sensor_shift_um,0)}µm<br>CFA ×${fmt(spec.pixel?.cfa_thickness_scale,1)}`;
      const metrics=study?`시간 잡음 ${fmt(metric(field,"lab_noise"),6)}<br>질감 ${fmt(metric(field,"texture_retention"),3)} · ΔE ${fmt(metric(field,"color"),2)}`:`MTF ${fmt(metric(field,"mtf"),3)} · ΔE ${fmt(metric(field,"color"),2)}<br>광전자 ${fmt(metric(field,"electrons"),1)} e`;
      button.innerHTML=`<h4>${esc(item.id)}${study?.selection?.selected_candidate_id===item.id?'<span class="selected-profile-badge">선택</span>':""}</h4><p>${configText}</p><div class="candidate-numbers">${ok?`${(data.fields||[]).filter(successful).length} / ${(data.fields||[]).length} 시야 완료 · ${esc(field?.label||state.field)}<br>${metrics}${row?`<br>${row.eligible===true?"조건 통과":"조건 미충족"}`:""}`:esc(typeof item.error==="string"?item.error:names[item.status]||item.status)}</div>`;
      button.onclick=()=>{selectView(viewFrom(state.sweep,item.id));notice(`${item.id}의 실제 설정과 결과를 열었습니다.`);};root.append(button);
    }
  }
  const recipeNames={parameter_tuning:"ISP 파라미터 설계",noise_detail:"ISP 설정 선택",demosaic:"Demosaic 비교",color_recalibration:"센서 색 재보정"};
  const studyReasons={brightness_normalized_noise_gain_below_limit:"밝기 대비 잡음의 개선이 최소 조건보다 작습니다",missing_or_invalid_field_metrics:"시야별 지표가 누락되었거나 유효하지 않습니다",texture_below_limit:"질감 보존율이 하한보다 낮습니다",additional_halo_above_limit:"Halo 증가가 상한을 넘었습니다",color_regression_above_limit:"색 오차 증가가 상한을 넘었습니다",brightness_below_limit:"밝기가 기준 하한보다 낮습니다",brightness_above_limit:"밝기가 기준 상한보다 높습니다",brightness_outside_limits:"시야 밝기가 지정 범위를 벗어났습니다",brightness_outside_baseline_limits:"시야 밝기가 기준 대비 범위를 벗어났습니다",missing_or_invalid_brightness_metrics:"밝기 지표가 누락되었거나 유효하지 않습니다",field_uniformity_regression_above_limit:"시야 균일도 악화가 상한을 넘었습니다",uniformity_regression_above_limit:"시야 균일도 악화가 상한을 넘었습니다"};
  const selectionStatuses={baseline_retained:"기준 설정 유지",recommended_within_demo:"데모 조건 내 선택",inconclusive:"미결정",comparison_only:"고정 조건 비교"};
  function reasonText(value){const raw=typeof value==="string"?value:JSON.stringify(value);return studyReasons[raw]||raw;}
  function selectionText(selection){return {baseline_retained:"선택 조건을 만족한 후보의 잡음 이득이 최소 개선 조건에 못 미쳐 기준 설정을 유지합니다.",recommended_within_demo:"질감·추가 Halo·색 오차 조건을 만족하면서 최악 시야의 시간 잡음이 가장 작은 후보입니다.",inconclusive:"필요한 시야 지표와 선택 조건을 모두 만족하는 비교를 확보하지 못했습니다.",comparison_only:"동일 원본의 차이를 비교합니다. 알고리즘이나 센서의 보편적인 우열을 자동으로 정하지 않습니다."}[selection.status]||selection.reason||"각 후보의 저장 지표를 비교하세요.";}
  function chooseRecipeView(recipe,query=null){
    const charts=["resolution","color","uniform","texture","demosaic_chirp"],stages=["isp","linear","raw","raw_lsc","raw_denoise","demosaic","sharpen_delta","demosaic_residual","valid_mask","psf","opd","layout"];
    const requestedChart=query?.get("chart"),requestedStage=query?.get("stage"),defaultChart={demosaic:"demosaic_chirp",noise_detail:"texture",parameter_tuning:"texture",color_recalibration:"color"}[recipe];
    if(charts.includes(requestedChart))state.chart=requestedChart;else if(defaultChart)state.chart=defaultChart;
    if(stages.includes(requestedStage))state.stage=requestedStage;else if(defaultChart)state.stage="isp";
    $("chart").value=state.chart;
  }
  function studyBaseline(job){
    const value=summary(job);if(!value?.study)return null;
    const id=value.study.baseline_candidate_id||value.study.selection?.baseline_candidate_id||value.candidates?.find(c=>!c.status||c.status==="succeeded")?.id;
    return id?viewFrom(job,id):null;
  }
  function selectStudyCandidate(id){
    if(!state.sweep)return;
    selectView(viewFrom(state.sweep,id));setEditor("isp");notice(`${id}의 실제 RAW·ISP 설정·지표를 열었습니다.`);
  }
  function studyRow(row){
    const reasons=Array.isArray(row.reasons)?row.reasons:row.reasons?[row.reasons]:[];
    return `<td>${esc(row.candidate_id)}</td><td>${fmt(row.noise,6)}</td><td>${fmt(finite(row.brightness_normalized_noise_improvement)?row.brightness_normalized_noise_improvement*100:null,2)}%</td><td>${fmt(finite(row.texture_retention)?row.texture_retention*100:null,1)}%</td><td>${fmt(finite(row.halo_increase)?row.halo_increase*100:null,2)}%p</td><td>${fmt(row.delta_e_increase,2)}</td><td>${fmt(row.brightness_relative_min,3)} – ${fmt(row.brightness_relative_max,3)}</td><td>${fmt(row.uniformity_regression,3)}</td><td>${row.eligible===true?"조건 통과":row.eligible===false?"탈락":"판정 없음"}${reasons.length?`<small>${reasons.map(value=>`<span title="${esc(typeof value==="string"?value:JSON.stringify(value))}">${esc(reasonText(value))}</span>`).join(" · ")}</small>`:""}</td>`;
  }
  function renderIspStudy(){
    const result=summary(state.sweep),study=result?.study,root=$("isp-study-result");
    if(!study){root.innerHTML='<p class="scope-caption">준비된 ISP study를 열면 후보별 실제 지표와 선택 이유를 표시합니다.</p>';return;}
    const selection=study.selection||{},rows=selection.rows||[],selected=selection.selected_candidate_id;
    if(study.recipe==="parameter_tuning"&&!state.active)text("tuning-draft-status",`표시 중: 완료된 ${state.sweep.id}. 위 입력 범위는 다음 실행에 적용됩니다.`);
    root.innerHTML=`<div class="isp-study-heading"><div><span class="eyebrow">${esc(recipeNames[study.recipe]||study.recipe)}</span><h3>${selected?`선택: ${esc(selected)}`:esc(selectionStatuses[selection.status]||selection.status||"비교 결과")}</h3><p title="${esc(selection.reason||selection.status||"")}">${esc(selectionText(selection))}</p><small>${esc(state.sweep.id)} · ${rows.length||result.candidates?.length||0}개 후보</small></div><button type="button" id="save-selected-isp" class="button secondary"${selected?"":" disabled"}>선택 프로필 저장</button></div><div id="noise-texture-scatter" class="noise-texture-scatter"></div>${rows.length?`<div class="table-scroll"><table class="change-table isp-selection-table"><thead><tr><th>후보</th><th>최악 시간 잡음</th><th>밝기 대비 잡음 개선</th><th>최소 질감 보존</th><th>최대 Halo 증가</th><th>최대 ΔE 증가</th><th>시야 밝기 범위 · 기준 대비</th><th>균일도 악화</th><th>선택 조건</th></tr></thead><tbody>${rows.map(row=>`<tr data-isp-candidate="${esc(row.candidate_id)}" class="${state.current?.job.id===state.sweep.id&&state.current?.candidateId===row.candidate_id?"active":""}">${studyRow(row)}</tr>`).join("")}</tbody></table></div>`:'<p class="scope-caption">이 recipe는 후보 지표를 비교하며 자동 선택하지 않습니다.</p>'}<p class="scope-caption">클릭한 후보의 실제 설정과 영상이 B에 표시됩니다. 숫자가 없는 후보는 계산된 값으로 채우지 않습니다.</p>`;
    root.querySelectorAll("[data-isp-candidate]").forEach(row=>{const candidate=result.candidates.find(c=>c.id===row.dataset.ispCandidate);if(candidate&&(!candidate.status||candidate.status==="succeeded")){row.tabIndex=0;row.setAttribute("role","button");row.onclick=()=>selectStudyCandidate(candidate.id);row.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();row.click();}};}});
    root.querySelector("#save-selected-isp").onclick=async()=>{try{if(!selected)return;const view=viewFrom(state.sweep,selected),spec=clone(configuration(view));await api("/revisions",{name:spec.name+" · selected ISP",kind:"camera",specification:spec,parent_id:null});await refreshRevisions();toast("선택한 ISP의 고정 설정을 revision으로 저장했습니다.");}catch(err){error(err);}};
    drawNoiseTexture(rows);
  }
  function drawNoiseTexture(rows){
    const root=$("isp-study-result").querySelector("#noise-texture-scatter"),valid=rows.filter(row=>finite(row.noise)&&finite(row.texture_retention));
    if(!valid.length){root.innerHTML='<p class="scope-caption">잡음–질감 좌표가 없는 recipe입니다. 아래 후보 카드를 선택해 비교하세요.</p>';return;}
    const width=680,height=245,p={l:66,r:32,t:20,b:46},xmin=Math.min(...valid.map(r=>r.noise))*.95,xmax=Math.max(...valid.map(r=>r.noise))*1.05,ymin=Math.min(.9,...valid.map(r=>r.texture_retention))-.015,ymax=Math.max(1.02,...valid.map(r=>r.texture_retention))+.015,x=n=>p.l+(n-xmin)/Math.max(xmax-xmin,1e-9)*(width-p.l-p.r),y=n=>height-p.b-(n-ymin)/Math.max(ymax-ymin,1e-9)*(height-p.t-p.b);
    let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="후보별 최악 시야 시간 잡음과 최소 질감 보존율. 아래 후보 버튼으로 선택합니다.">`;
    for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(v)}" y2="${y(v)}" stroke="#e2ece5"/><text x="${p.l-8}" y="${y(v)+3}" text-anchor="end">${fmt(v*100,1)}%</text>`;const n=xmin+(xmax-xmin)*i/4;svg+=`<text x="${x(n)}" y="${height-p.b+18}" text-anchor="middle">${fmt(n,5)}</text>`;}
    const threshold=summary(state.sweep)?.study?.constraints?.texture_retention_min;
    if(finite(threshold)&&threshold>=ymin&&threshold<=ymax)svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(threshold)}" y2="${y(threshold)}" stroke="#bb9767" stroke-dasharray="5 4"/>`;
    for(const row of valid){const active=state.current?.job.id===state.sweep.id&&state.current?.candidateId===row.candidate_id;svg+=`<g class="scatter-candidate${active?" active":""}" data-point="${esc(row.candidate_id)}"><title>${esc(row.candidate_id)} · ${row.eligible===true?"조건 통과":"조건 미충족"}</title><circle cx="${x(row.noise)}" cy="${y(row.texture_retention)}" r="${active?8:6}" fill="${row.eligible===true?"#087f83":"#b8823c"}"/><text x="${x(row.noise)+10}" y="${y(row.texture_retention)-9}">${esc(row.candidate_id.replace("candidate-","#"))}</text></g>`;}
    svg+=`<text x="${width/2}" y="${height-7}" text-anchor="middle">시간 잡음 · 왼쪽이 낮음</text><text x="16" y="${height/2}" transform="rotate(-90 16 ${height/2})" text-anchor="middle">질감 보존율</text></svg>`;root.innerHTML=svg;
    root.querySelectorAll("[data-point]").forEach(node=>{node.onclick=()=>selectStudyCandidate(node.dataset.point);});
    // Close measurements can share SVG bounds. Real buttons provide distinct mouse and keyboard targets.
    const choices=document.createElement("div");choices.className="scatter-choice-list";choices.setAttribute("role","group");choices.setAttribute("aria-label","산점도 후보 선택");
    for(const row of valid){const button=document.createElement("button");button.type="button";button.className="scatter-choice";button.dataset.scatterCandidate=row.candidate_id;const active=state.current?.job.id===state.sweep.id&&state.current?.candidateId===row.candidate_id;button.setAttribute("aria-pressed",String(active));button.setAttribute("aria-label",`${row.candidate_id} · 잡음 ${fmt(row.noise,6)} · 질감 ${fmt(row.texture_retention*100,1)}%`);button.innerHTML=`<strong>${esc(row.candidate_id)}</strong><span>잡음 ${fmt(row.noise,6)} · 질감 ${fmt(row.texture_retention*100,1)}%</span>`;button.onclick=()=>selectStudyCandidate(row.candidate_id);choices.append(button);}root.append(choices);
  }
  function catalogData(value){return {...value,...(value.catalog||{})};}
  function preparedStudyInfo(job){
    if(!finished(job)||!summary(job)?.study)return null;
    const value=summary(job),source=value.study.source||job.source||{},candidates=(value.candidates||[]).filter(c=>!c.status||c.status==="succeeded"),spec=candidates[0]?.configuration||job.specification||{};
    return {id:job.id,created:job.created_at||job.created||null,recipe:value.study.recipe,sourceRunId:source.run_id||null,sourceCandidateId:source.candidate_id||null,cfa:spec.pixel?.cfa_thickness_scale,curvature:spec.lens?.curvature_scale,z:spec.lens?.sensor_shift_um,conditions:candidates.map(candidate=>({candidateId:candidate.id,calibrationId:candidate.configuration?.isp?.color_calibration_id||null,configuration:candidate.configuration}))};
  }
  function selectPreparedCondition(job,candidateId,side){
    ++state.openToken;
    const view=viewFrom(job,candidateId);chooseRecipeView(summary(job)?.study?.recipe);
    if(side==="A"){state.baseline=view;renderResults();updateURL();notice(`${candidateId}를 A 기준으로 고정했습니다. 다른 CFA의 B를 선택하면 센서 간 비교가 됩니다.`);}
    else {state.sweep=job;if(!state.baseline)state.baseline=studyBaseline(job);selectView(view);setEditor("isp");notice(`${candidateId}의 실제 결과를 B에 열었습니다. A 기준은 유지합니다.`);}
  }
  function preparedSourceKey(info){return info.sourceRunId?JSON.stringify([info.recipe,info.sourceRunId,info.sourceCandidateId]):JSON.stringify([info.recipe,info.id]);}
  function preparedTime(info){return typeof info.created==="number"?info.created:Date.parse(info.created)||0;}
  function renderPreparedStudies(){
    const root=$("prepared-isp-studies"),prepared=state.catalog.prepared_isp_studies||[],token=++state.preparedToken;root.replaceChildren();
    if(!prepared.length)return;
    const caption=document.createElement("p");caption.textContent="원본별 최신 결과 · 이전 처리는 전체 실행 기록에서 확인";root.append(caption);
    const pending=[],latest=new Map();
    for(const [rank,item] of prepared.entries()){
      const id=item.run_id||item.id;if(!id)continue;
      const card=document.createElement("article");card.className="prepared-color-study";card.dataset.runId=id;root.append(card);
      const paint=job=>{
        const info=preparedStudyInfo(job);if(!info)return;
        const key=preparedSourceKey(info),previous=latest.get(key),timestamp=preparedTime(info);
        if(previous&&(previous.timestamp>timestamp||(previous.timestamp===timestamp&&previous.rank<rank))){card.hidden=true;return;}
        if(previous)previous.card.hidden=true;latest.set(key,{card,timestamp,rank});card.hidden=false;
        const sourceText=`${info.sourceRunId||"확인 불가"}${info.sourceCandidateId?" / "+info.sourceCandidateId:""}`;
        card.innerHTML=`<div class="prepared-color-title"><strong>${esc(recipeNames[info.recipe]||info.recipe)} · CFA ×${fmt(info.cfa,1)}</strong><span>곡률 ×${fmt(info.curvature,3)} · Z ${fmt(info.z,0)} µm</span></div><p>원본 ${esc(sourceText)}</p><div class="prepared-color-conditions"></div><small>처리 실행 ${esc(id)}</small>`;
        const conditions=card.querySelector(".prepared-color-conditions");
        if(info.recipe!=="color_recalibration"){const button=document.createElement("button");button.type="button";button.className="icon-button";button.textContent="결과 열기";button.setAttribute("aria-label",`${recipeNames[info.recipe]||info.recipe} · 곡률 ${fmt(info.curvature,3)} · 원본 ${sourceText} · 처리 ${id}`);button.onclick=()=>openRun(id,item.candidate_id||null).catch(error);conditions.append(button);return;}
        for(const condition of info.conditions){const row=document.createElement("div");row.className="prepared-color-condition";const label=document.createElement("span");label.textContent=condition.calibrationId?"Munsell64 CCM":"기존 고정 CCM";label.title=condition.calibrationId||"원본 spectral CCM";row.append(label);for(const side of ["A","B"]){const button=document.createElement("button");button.type="button";button.className="icon-button";button.textContent=side==="A"?"A 기준":"B 비교";button.setAttribute("aria-label",`CFA ${fmt(info.cfa,1)} · ${label.textContent} · ${side} · 원본 ${sourceText} · 처리 ${id} / ${condition.candidateId}`);button.title=`${id} / ${condition.candidateId}`;button.onclick=()=>selectPreparedCondition(job,condition.candidateId,side);row.append(button);}conditions.append(row);}
      };
      const cached=state.preparedJobs.get(id);
      if(cached)paint(cached);else{card.innerHTML=`<strong>${esc(item.name||recipeNames[item.recipe]||"ISP 비교")}</strong><p>원본 카메라와 최신 처리 확인 중</p><small>${esc(id)}</small>`;pending.push({id,card,paint});}
    }
    const historyButton=document.createElement("button");historyButton.type="button";historyButton.className="icon-button prepared-history-link";historyButton.textContent="전체 보존 실행 기록 ↗";historyButton.onclick=()=>historyPanel(true);root.append(historyButton);
    // Exact source metadata arrives after primary images. Never merge different source candidates.
    (async()=>{await state.imagesReady;for(const item of pending){if(token!==state.preparedToken)return;try{const job=await api(`/runs/${encodeURIComponent(item.id)}`);if(token!==state.preparedToken)return;state.preparedJobs.set(item.id,job);item.paint(job);}catch(err){if(token!==state.preparedToken)return;item.card.querySelector("p").textContent="원본 정보 조회 실패 · "+err.message;const button=document.createElement("button");button.type="button";button.className="icon-button";button.textContent="실행 상태 열기";button.onclick=()=>openRun(item.id).catch(error);item.card.append(button);}}})().catch(error);
  }
  function renderIspDiagnostics(){
    const curves=[];
    for(const [view,color] of [[state.baseline,"#b8823c"],[state.current,"#087f83"]]){const curve=selectedField(view)?.tests?.resolution?.sites?.center?.processed_system;if(curve?.status==="ok"&&Array.isArray(curve.sfr)){const x=curve.frequency?.cycles_per_pixel;if(Array.isArray(x))curves.push({x,y:curve.sfr,color});}}
    drawResponse("sfr-chart",curves,"처리 후 System SFR",true);
    const noiseRows=[["균일장", "temporal_noise_rgb","temporal_noise_luma"],["색 패치","color_noise_rgb","color_noise_luma"]];
    const channelNoise=(view,rgbKey,lumaKey)=>{const lab=selectedField(view)?.tests?.isp_lab||{},rgb=lab[rgbKey]||{};return [rgb.r,rgb.g,rgb.b,lab[lumaKey]].map(value=>fmt(value,6)).join(" / ");};
    $("isp-channel-noise").innerHTML='<table class="change-table"><thead><tr><th>시간 잡음 · R / G / B / Y</th><th>A 기준</th><th>B 변경</th></tr></thead><tbody>'+noiseRows.map(([label,rgb,luma])=>`<tr><td>${label}</td><td>${channelNoise(state.baseline,rgb,luma)}</td><td>${channelNoise(state.current,rgb,luma)}</td></tr>`).join("")+'</tbody></table><p class="field-note">저장된 반복 취득의 픽셀별 시간 분산입니다. 공간 질감을 잡음으로 세지 않습니다.</p>';
    const aa=selectedField(state.baseline)?.tests?.color?.patches||[],bb=selectedField(state.current)?.tests?.color?.patches||[],patches=[...new Set([...aa,...bb].map(row=>row.patch))].sort((a,b)=>a-b),root=$("patch-chart");
    if(!patches.length){root.innerHTML='<div class="chart-empty">저장된 패치별 색 지표가 없습니다.</div>';return;}
    const peak=Math.max(1,...[...aa,...bb].map(row=>row.delta_e00).filter(finite));
    root.innerHTML=patches.map(id=>{const a=aa.find(row=>row.patch===id),b=bb.find(row=>row.patch===id);return `<div class="patch-comparison"><span>${esc(id)}</span><div><i class="patch-bar baseline" style="width:${finite(a?.delta_e00)?a.delta_e00/peak*100:0}%"></i><i class="patch-bar candidate" style="width:${finite(b?.delta_e00)?b.delta_e00/peak*100:0}%"></i></div><small><span>${fmt(a?.delta_e00,1)}</span> / ${fmt(b?.delta_e00,1)}</small></div>`;}).join("");
  }
  function drawResponse(id,curves,label,autoY=false){
    if(!curves.length){$(id).innerHTML='<div class="chart-empty">선택 위치의 저장된 곡선이 없습니다.</div>';return;}
    const width=620,height=190,p={l:36,r:14,t:12,b:30},ymax=autoY?Math.max(1,...curves.flatMap(c=>c.y.filter(finite)))*1.05:1,x=n=>p.l+n/.5*(width-p.l-p.r),y=n=>height-p.b-n/ymax*(height-p.t-p.b);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">`;
    for(let i=0;i<=4;i++){const n=i*ymax/4;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(n)}" y2="${y(n)}" stroke="#e4ece2"/><text x="${p.l-6}" y="${y(n)+3}" text-anchor="end" font-size="9" fill="#8ca18e">${fmt(n,2)}</text>`;}
    for(const n of [0,.1,.2,.3,.4,.5])svg+=`<text x="${x(n)}" y="${height-12}" text-anchor="middle" font-size="9" fill="#8ca18e">${n}</text>`;
    for(const c of curves)svg+=`<polyline points="${c.x.map((v,i)=>finite(v)&&finite(c.y[i])&&v>=0&&v<=.5?`${x(v)},${y(c.y[i])}`:null).filter(Boolean).join(" ")}" fill="none" stroke="${c.color}" stroke-width="1.8"/>`;
    $(id).innerHTML=svg+'</svg>';
  }
  function readConstraints(){
    const constraints={texture_retention_min:Number($("constraint-texture").value),halo_increase_max:Number($("constraint-halo").value)/100,delta_e_increase_max:Number($("constraint-color").value),noise_improvement_min:Number($("constraint-noise").value)/100};
    for(const id of ["constraint-texture","constraint-halo","constraint-color","constraint-noise"])if(!$(id).reportValidity())throw Error("ISP 선택 조건을 확인하세요.");
    if(Object.values(constraints).some(value=>!finite(value)))throw Error("ISP 선택 조건은 유한한 숫자여야 합니다.");return constraints;
  }
  async function submitIspStudy(recipe){
    if(!state.ready||state.active||state.submitting||!$("design-form").reportValidity())return;
    const spec=readSpec(),source=state.current;if(!source||!equivalent(spec,configuration(source),true))throw Error("ISP 비교는 선택 원본의 광학·픽셀·취득 조건을 유지해야 합니다.");
    if(recipe==="demosaic"&&sourceHasChirp(source)===false)throw Error("이 원본에는 demosaic chirp가 없습니다. chirp를 추가 취득하세요.");
    const isp=spec.isp.schema==="camera_reference_isp_v2"?spec.isp:v2Profile(configuration(source).isp);
    const binding=ispSource(source);
    const request={source_run_id:binding.run_id,...(binding.candidate_id?{source_candidate_id:binding.candidate_id}:{}),recipe,name:spec.name+" · "+recipeNames[recipe],isp,...(recipe==="noise_detail"?{constraints:readConstraints()}: {}),...(recipe==="parameter_tuning"?{parameter_axes:readParameterAxes().axes,constraints:readTuningConstraints()}: {})};
    if(recipe==="parameter_tuning"){const plan=parameterPlan(isp,request.parameter_axes);if(plan.count>12)throw Error(`기준안을 포함한 후보 ${plan.count}개입니다. 12개 이하로 범위를 줄이세요.`);request.isp.lsc_enabled=true;}
    state.submitting=true;buttons();try{const job=await api("/isp-studies",request);state.active=job;if(recipe==="parameter_tuning")text("tuning-draft-status",`전체 평가 제출 · ${job.id}. 아래는 완료된 보존 결과이며 새 결과는 작업 완료 후 표시합니다.`);state.baseline=source;renderResults();notice(`${recipeNames[recipe]} 작업을 제출했습니다. 같은 원본 RAW를 사용합니다.`,"busy");schedulePoll();refreshJobs().catch(error);}finally{state.submitting=false;buttons();}
  }
  const tuningAxes=[
    {id:"axis-nr",path:"raw_denoise.strength",label:"RAW NR strength",min:0,max:1,default:[0,.2,.4]},
    {id:"axis-floor",path:"raw_denoise.range_floor_dn2",label:"RAW range floor",min:1e-12,max:2**48,default:[1]},
    {id:"axis-sharpen",path:"linear_sharpen.amount",label:"Sharpen amount",min:0,max:1,default:[0,.3]},
    {id:"axis-threshold",path:"linear_sharpen.threshold",label:"Sharpen threshold",min:0,max:1,default:[.01]},
    {id:"axis-delta",path:"linear_sharpen.max_delta",label:"Sharpen max delta",min:0,max:1,default:[.05]},
    {id:"axis-lsc",path:"lsc_tuning.strength",label:"LSC strength",min:0,max:1,default:[.85,1]},
    {id:"axis-cap",path:"lsc_tuning.gain_cap",label:"LSC gain cap",min:1,max:4,default:[4]}
  ];
  function ispSource(view=state.current){
    const source=view?.data?.preview_scope?.source||view?.job?.source;
    return view?.data?.preview_scope&&source?.run_id?source:{run_id:view?.job?.id,candidate_id:view?.candidateId||null};
  }
  function sourceHasChirp(view){
    if(view?.data?.preview_scope){const binding=ispSource(view),job=state.preparedJobs.get(binding.run_id);if(!job)return null;try{view=viewFrom(job,binding.candidate_id);}catch{return null;}}
    return fields(view).some(field=>successful(field)&&field.images?.demosaic_chirp?.raw);
  }
  function readParameterAxes(){
    const axes={};let combinations=1;
    for(const axis of tuningAxes){const raw=$(axis.id).value.trim(),tokens=raw.split(",").map(value=>value.trim());if(!raw||tokens.some(value=>!value))throw Error(`${axis.label}: 쉼표로 구분한 숫자가 필요합니다.`);const values=[...new Set(tokens.map(Number))];if(values.some(value=>!finite(value)||value<axis.min||value>axis.max))throw Error(`${axis.label}: ${axis.min}–${axis.max} 범위의 숫자를 입력하세요.`);axes[axis.path]=values;combinations*=values.length;}
    if(combinations>12)throw Error(`조합 ${combinations}개입니다. 최대 12개 이하로 범위를 줄이세요.`);return {axes,combinations};
  }
  function parameterPlan(isp,axes){
    const baseline=clone(isp);baseline.lsc_enabled=true;baseline.lsc_tuning={strength:1,gain_cap:4};baseline.raw_denoise.strength=0;baseline.linear_sharpen.amount=0;
    let candidates=[{...clone(isp),lsc_enabled:true,lsc_tuning:clone(isp.lsc_tuning||{strength:1,gain_cap:4})}];
    for(const [path,values] of Object.entries(axes)){const [part,key]=path.split(".");candidates=candidates.flatMap(candidate=>values.map(value=>{const next=clone(candidate);next[part][key]=value;return next;}));}
    const keys=new Set([baseline,...candidates].map(value=>JSON.stringify(canonicalValue(value))));return {count:keys.size,baseline};
  }
  function readTuningConstraints(){
    const extra={brightness_relative_min:Number($("constraint-brightness-min").value),brightness_relative_max:Number($("constraint-brightness-max").value),uniformity_increase_max:Number($("constraint-uniformity").value)};
    for(const id of ["constraint-brightness-min","constraint-brightness-max","constraint-uniformity"])if(!$(id).reportValidity())throw Error("밝기·균일도 조건을 확인하세요.");
    if(Object.values(extra).some(value=>!finite(value))||extra.brightness_relative_min<0||extra.brightness_relative_min>1||extra.brightness_relative_max<1||extra.brightness_relative_max>2||extra.brightness_relative_min>extra.brightness_relative_max||extra.uniformity_increase_max<0||extra.uniformity_increase_max>1)throw Error("밝기 하한·상한과 균일도 범위를 확인하세요.");return {...readConstraints(),...extra};
  }
  function tuningEdited(){text("tuning-draft-status","범위가 편집되었습니다. 표시된 결과는 보존 실행이며 새 범위가 아직 계산되지 않았습니다.");buttons();}
  function tuningButtons(same,active){
    const source=state.current,binding=ispSource(source),preview=source?.data?.preview_scope;
    text("tuning-source",binding.run_id?`원본 ${binding.run_id}${binding.candidate_id?" / "+binding.candidate_id:""}`:"보존 원본 선택 필요");
    text("isp-preview-target",`${state.field} · ${state.chart} · 첫 RAW 1장`);
    let valid=true;try{const spec=readSpec(),isp=spec.isp.schema==="camera_reference_isp_v2"?spec.isp:v2Profile(configuration(source).isp),plan=parameterPlan(isp,readParameterAxes().axes);valid=plan.count<=12;text("tuning-candidate-count",`${plan.count}개 후보 · 기준안 포함`);if(!valid)text("tuning-candidate-count",`${plan.count}개 후보 · 최대 12개`);}catch(err){valid=false;text("tuning-candidate-count",err.message);}
    $("isp-preview-button").disabled=!state.ready||active||!same||(!preview&&!selectedField(source)?.images?.[state.chart]?.raw);
    $("isp-tuning-submit").disabled=!state.ready||active||!same||!valid;
    $("tuning-preset").disabled=!state.ready||active;
    for(const axis of tuningAxes)$(axis.id).disabled=!state.ready||active;
    for(const id of ["constraint-brightness-min","constraint-brightness-max","constraint-uniformity"])$(id).disabled=!state.ready||active;
  }
  async function submitIspPreview(){
    if(!state.ready||state.active||state.submitting||!$("design-form").reportValidity())return;
    const spec=readSpec(),source=state.current;if(!source||!equivalent(spec,configuration(source),true))throw Error("선택한 취득 조건을 유지해야 미리 계산할 수 있습니다.");
    if(!source.data.preview_scope&&!selectedField(source)?.images?.[state.chart]?.raw)throw Error("현재 위치·차트의 보존 RAW가 없습니다.");
    const isp=spec.isp.schema==="camera_reference_isp_v2"?spec.isp:v2Profile(configuration(source).isp),binding=ispSource(source);
    const request={source_run_id:binding.run_id,...(binding.candidate_id?{source_candidate_id:binding.candidate_id}:{}),name:spec.name+" · 첫 RAW 미리 계산",isp,field_id:state.field,chart:state.chart};
    if(!source.data.preview_scope)state.preparedJobs.set(source.job.id,source.job);
    state.submitting=true;buttons();try{const job=await api("/isp-previews",request);state.active=job;state.baseline=source;notice("선택 위치의 첫 RAW를 실제 재처리합니다. 기존 영상은 완료 시까지 보존 결과로 표시됩니다.","busy");schedulePoll();refreshJobs().catch(error);}finally{state.submitting=false;buttons();}
  }
  function renderPreviewScope(){
    const preview=state.current?.data?.preview_scope,root=$("isp-preview-scope");root.classList.toggle("hidden",!preview);root.textContent=preview?`미리 계산 · ${preview.field_id} · ${preview.chart} · 첫 RAW 1장. 시간 잡음·NPS·9개 시야 선택은 평가하지 않았습니다. 원본 ${ispSource().run_id}`:"";
  }
  function renderTuningDiagnostics(){
    const curves=[],npsNotes=[];
    for(const [view,color,label]of [[state.baseline,"#b8823c","A"],[state.current,"#087f83","B"]]){const nps=selectedField(view)?.tests?.isp_lab?.noise_power_spectrum;if(nps?.status==="ok"&&Array.isArray(nps.frequency_cycles_per_pixel)&&Array.isArray(nps.radial_power)){curves.push({x:nps.frequency_cycles_per_pixel,y:nps.radial_power,color});npsNotes.push(`${label}: ${nps.units||"linear luma²"}`);}}
    drawNps(curves);text("isp-nps-note",curves.length?`반복 취득의 시간 잔차 · ${npsNotes.join(" / ")} · cycles/native pixel. 공간 질감은 잡음에서 제외합니다.`:"해당 실행에 저장된 반복 취득 NPS가 없습니다. 한 장의 공간 변동으로 대체하지 않습니다.");
    const lab=view=>selectedField(view)?.tests?.isp_lab||{},la=lab(state.baseline),lb=lab(state.current),a=la.uniform_mean_luma,b=lb.uniform_mean_luma;
    const uniformity=view=>{const values=fields(view).filter(successful).map(field=>field.tests?.isp_lab?.uniform_mean_luma);return values.length===9&&values.every(value=>finite(value)&&value>0)?Math.min(...values)/Math.max(...values):null;};
    const aLsc=configuration(state.baseline).isp?.lsc_tuning||{strength:1,gain_cap:4},bLsc=configuration(state.current).isp?.lsc_tuning||{strength:1,gain_cap:4};
    $("isp-brightness-diagnostics").innerHTML=`<table class="change-table"><thead><tr><th>${esc(state.field)}</th><th>A 기준</th><th>B 변경</th></tr></thead><tbody><tr><td>균일장 평균 선형 Y</td><td>${fmt(a,6)}</td><td>${fmt(b,6)}</td></tr><tr><td>9개 시야 min / max</td><td>${fmt(uniformity(state.baseline),3)}</td><td>${fmt(uniformity(state.current),3)}</td></tr><tr><td>LSC 강도 / cap</td><td>${configuration(state.baseline).isp?.lsc_enabled?`${fmt(aLsc.strength)} / ${fmt(aLsc.gain_cap,1)}`:"OFF"}</td><td>${configuration(state.current).isp?.lsc_enabled?`${fmt(bLsc.strength)} / ${fmt(bLsc.gain_cap,1)}`:"OFF"}</td></tr><tr><td>실제 LSC gain · min–max</td><td>${fmt(la.effective_lsc?.effective_min,3)} – ${fmt(la.effective_lsc?.effective_max,3)}</td><td>${fmt(lb.effective_lsc?.effective_min,3)} – ${fmt(lb.effective_lsc?.effective_max,3)}</td></tr></tbody></table><p class="brightness-ratio">선택 시야 B / A 밝기 <strong>${fmt(finite(a)&&a>0&&finite(b)?b/a:null,3)} ×</strong></p>`;
  }
  function drawNps(curves){
    const root=$("isp-nps-chart"),valid=curves.filter(curve=>curve.x.length===curve.y.length&&curve.y.some(value=>finite(value)&&value>=0));if(!valid.length){root.innerHTML='<div class="chart-empty">저장된 시간 NPS 없음</div>';return;}
    const width=620,height=190,p={l:66,r:14,t:12,b:35},maximum=Math.max(1e-20,...valid.flatMap(curve=>curve.y.filter(finite)))*1.05,x=value=>p.l+value/.5*(width-p.l-p.r),y=value=>height-p.b-value/maximum*(height-p.t-p.b);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="반복 취득 시간 잡음의 radial NPS">`;
    for(let i=0;i<=4;i++){const value=maximum*i/4;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(value)}" y2="${y(value)}" stroke="#e4ece2"/><text x="${p.l-7}" y="${y(value)+3}" text-anchor="end">${value.toExponential(1)}</text>`;}
    for(const value of [0,.1,.2,.3,.4,.5])svg+=`<text x="${x(value)}" y="${height-12}" text-anchor="middle">${value}</text>`;
    for(const curve of valid)svg+=`<polyline fill="none" stroke="${curve.color}" stroke-width="1.8" points="${curve.x.map((value,i)=>finite(value)&&value>=0&&value<=.5&&finite(curve.y[i])?`${x(value)},${y(curve.y[i])}`:null).filter(Boolean).join(" ")}"/>`;root.innerHTML=svg+'</svg>';
  }
  async function refreshJobs(){const result=await api("/runs");state.jobs=Array.isArray(result)?result:result.runs||result.items||[];state.jobs.sort((a,b)=>String(b.created_at||b.created||"").localeCompare(String(a.created_at||a.created||"")));text("run-count",state.jobs.length);const root=$("history-list");root.replaceChildren();for(const job of state.jobs){const row=document.createElement("article"),button=document.createElement("button");row.className="history-item";row.innerHTML=`<h3>${esc(job.name||job.specification?.name||"Reference design")}</h3><p>${esc(job.id)}</p><span class="state">${esc(names[job.status]||job.status)}</span>`;button.className="icon-button";button.textContent="결과 · 상태 열기";button.onclick=()=>openRun(job.id).catch(error);row.append(button);root.append(row);}if(!state.jobs.length)root.innerHTML='<p class="history-empty">실행한 Reference 설계를 보존합니다.</p>';}
  async function refreshRevisions(){const result=await api("/revisions");state.revisions=result.revisions||result.items||[];$("revision-select").innerHTML='<option value="">저장된 revision</option>'+state.revisions.map(r=>`<option value="${esc(r.id)}">${esc(r.name||r.id)}</option>`).join("");}
  function historyPanel(open){$("history-panel").classList.toggle("hidden",!open);$("history-toggle").setAttribute("aria-expanded",String(open));if(open)refreshJobs().catch(error);}
  async function openRun(id,candidateId=null){
    const token=++state.openToken,job=await api(`/runs/${encodeURIComponent(id)}`);if(token!==state.openToken)return null;
    if(finished(job)){state.preparedJobs.set(job.id,job);chooseRecipeView(summary(job)?.study?.recipe);if(summary(job).candidates){state.sweep=job;state.baseline=studyBaseline(job)||state.baseline;const selected=candidateId||summary(job).study?.selection?.selected_candidate_id||summary(job).candidates.find(c=>!c.status||c.status==="succeeded")?.id;if(selected)selectView(viewFrom(job,selected));else renderSweep();notice("완료된 후보의 실제 설정·원본·지표를 열었습니다.");}else selectView(viewFrom(job));}
    else if(busy(job)){state.active=job;buttons();schedulePoll();}else notice(`${names[job.status]||job.status} · ${typeof job.error==="string"?job.error:JSON.stringify(job.error||{})}`,"error");historyPanel(false);return job;
  }
  async function submit(kind,axes=null){
    if(!state.ready||state.active||state.submitting||!$("design-form").reportValidity())return;
    if(kind!=="replay"&&hasAdoptedReadout(state.current))throw Error("채택한 readout 가정은 일반 촬영 설정으로 전달되지 않습니다. Readiness Lab에서 모델을 변경·시험하세요.");
    const spec=readSpec();let job;
    if(kind==="replay"&&(!state.current||!equivalent(spec,configuration(state.current),true)))throw Error("ISP 재처리는 원본의 광학·픽셀·취득 조건을 유지해야 합니다.");
    state.submitting=true;buttons();
    try{
      if(kind==="replay"){const binding=ispSource();job=await api("/replays",{source_run_id:binding.run_id,...(binding.candidate_id?{source_candidate_id:binding.candidate_id}:{}),name:spec.name+" · ISP",isp:spec.isp});}
      else if(kind==="sweep")job=await api("/sweeps",{name:spec.name+" · sweep",specification:spec,axes});else job=await api("/runs",spec);
      state.active=job;notice(kind==="replay"?"보존 RAW에서 ISP를 재처리합니다.":"Reference 기반 계산을 제출했습니다. 실제 결과가 도착하면 표시합니다.","busy");schedulePoll();refreshJobs().catch(error);
    }finally{state.submitting=false;buttons();}
  }
  function schedulePoll(){clearTimeout(state.poll);state.poll=setTimeout(poll,1800);}
  async function poll(){if(!state.active)return;try{const id=state.active.id,job=await api(`/runs/${encodeURIComponent(id)}`);if(state.active?.id!==id)return;state.active=job;if(busy(job)){notice(job.progress?.stage||job.progress?.message||"Reference 응답을 계산하고 있습니다.","busy");const f=job.progress?.fraction;$("job-progress").classList.toggle("hidden",!finite(f));if(finite(f))$("job-progress").value=f;schedulePoll();return;}state.active=null;$("job-progress").classList.add("hidden");if(finished(job)){state.preparedJobs.set(job.id,job);chooseRecipeView(summary(job)?.study?.recipe);if(summary(job).candidates){state.sweep=job;if(summary(job).study){state.catalog.prepared_isp_studies=[...(state.catalog.prepared_isp_studies||[]).filter(item=>(item.run_id||item.id)!==job.id),{recipe:summary(job).study.recipe,run_id:job.id,name:job.name}];state.preparedJobs.set(job.id,job);}state.baseline=studyBaseline(job)||state.baseline;const chosen=summary(job).study?.selection?.selected_candidate_id||summary(job).candidates.find(c=>!c.status||c.status==="succeeded")?.id;if(chosen)selectView(viewFrom(job,chosen));else renderSweep();notice("후보 계산 완료 · 성공·실패 시야와 각 후보 결과를 확인하세요.");}else{if(!state.baseline)state.baseline=viewFrom(job);selectView(viewFrom(job));notice(summary(job).preview_scope?"첫 RAW 미리 계산 완료 · 1개 시야 · 1개 차트. 반복 잡음과 설정 선택은 전체 평가에서 확인하세요.":"실제 계산 완료 · 원본 RAW와 시야별 결과를 보존했습니다.");}renderPreparedStudies();toast("Reference 계산이 완료되었습니다.");}else notice(`${names[job.status]||job.status} · ${typeof job.error==="string"?job.error:JSON.stringify(job.error||{})}`,"error");await Promise.all([refreshJobs(),refreshRevisions()]);buttons();}catch(err){notice(`상태 확인 재시도 · ${err.message}`,"error");schedulePoll();}}
  function transform(){document.querySelectorAll(".image-content").forEach(node=>node.style.transform=`translate(${state.pan[0]}px,${state.pan[1]}px) scale(${state.zoom})`);text("zoom-label",state.zoom===1?"맞춤":Math.round(state.zoom*100)+"%");}
  function pixelStatus(message,failed=false){text("pixel-status",message);$("pixel-status").classList.toggle("error",failed);}
  function pixelField(detail){return detail?.fields?.find(field=>field.id===state.field)||null;}
  function pixelSample(detail,table){const w=detail?.wavelength_nm?.indexOf(pixelState.wavelength),s=detail?.sites?.indexOf(pixelState.site);const value=table?.[w]?.[s];return finite(value)?value:null;}
  function pixelQE(detail,mode=pixelState.mode,field=pixelField(detail)){return field?.status==="unavailable"?null:field?.[mode+"_qe"]||null;}
  function pixelControls(){const ready=Boolean(pixelState.a&&pixelState.b&&!pixelState.loading);$("pixel-save-comparison").disabled=!ready||Boolean(pixelState.active);$("pixel-study-submit").disabled=!pixelState.template||Boolean(pixelState.active);for(const id of ["pixel-baseline","pixel-candidate"])$(id).disabled=!pixelState.study||Boolean(pixelState.active);}
  function pixelURL(){if(!pixelState.study)return;const url=new URL(location.href);for(const [key,value] of Object.entries({pixel_study:pixelState.study.id,pixel_candidate:pixelState.candidateId,pixel_baseline:pixelState.baselineId,pixel_wave:pixelState.wavelength,pixel_site:pixelState.site,pixel_mode:pixelState.mode,pixel_color:pixelState.colorMode,pixel_story:pixelState.story,field:state.field}))url.searchParams.set(key,value);history.replaceState(null,"",url);}
  function showPixelLab(){pixelState.visible=true;$("pixel-lab-panel").classList.remove("hidden");if(!pixelState.studies.length&&!pixelState.loading)refreshPixelStudies(true).catch(err=>pixelStatus(err.message,true));}
  async function refreshPixelStudies(selectLatest=false){
    const token=++pixelState.listToken,value=await api("/pixel-studies");if(token!==pixelState.listToken)return;
    pixelState.studies=value.studies||[];pixelState.template=value.template||null;
    $("pixel-study-select").innerHTML='<option value="">보존 study 선택</option>'+pixelState.studies.map(study=>`<option value="${esc(study.id)}">${esc(study.name||study.id)} · ${esc(names[study.status]||study.status)}</option>`).join("");
    if(pixelState.study)$("pixel-study-select").value=pixelState.study.id;
    pixelControls();if(!pixelState.studies.length)pixelStatus("준비된 Pixel study가 없습니다. 고정된 원본 명세로 진단을 실행하세요.");
    if(selectLatest&&!pixelState.study){const latest=pixelState.studies.find(study=>study.status==="succeeded");if(latest)await openPixelStudy(latest.id);}
    return value;
  }
  async function pixelDetail(studyId,candidateId){
    const key=JSON.stringify([studyId,candidateId]);if(pixelState.cache.has(key))return pixelState.cache.get(key);
    const detail=await api(`/pixel-studies/${encodeURIComponent(studyId)}/candidates/${encodeURIComponent(candidateId)}`);
    if(detail.id!==candidateId)throw Error("Pixel 후보 응답의 원본 식별자가 요청과 다릅니다.");
    pixelState.cache.set(key,detail);return detail;
  }
  async function openPixelStudy(id,candidateId=null,baselineId=null){
    const study=pixelState.studies.find(item=>item.id===id);if(!study)throw Error("보존 목록에서 해당 Pixel study를 찾지 못했습니다. 목록을 새로고침하세요.");
    if(study.status!=="succeeded"){pixelStatus(`${names[study.status]||study.status} · ${id}`);return;}
    const candidates=(study.candidates||[]).filter(item=>!item.status||item.status==="succeeded");
    baselineId=baselineId||candidates.find(item=>item.cfa_thickness_scale===1)?.id||candidates[0]?.id;candidateId=candidateId||candidates.find(item=>item.cfa_thickness_scale===.9)?.id||baselineId;
    if(!candidates.some(item=>item.id===candidateId)||!candidates.some(item=>item.id===baselineId))throw Error("선택한 Pixel 후보가 이 study의 완료 결과에 없습니다.");
    const token=++pixelState.token;pixelState.study=study;pixelState.baselineId=baselineId;pixelState.candidateId=candidateId;pixelState.a=null;pixelState.b=null;pixelState.loading=true;$("pixel-study-select").value=id;
    const options=candidates.map(item=>`<option value="${esc(item.id)}">CFA ×${fmt(item.cfa_thickness_scale,1)} · ${esc(item.id)}</option>`).join("");$("pixel-baseline").innerHTML=options;$("pixel-candidate").innerHTML=options;$("pixel-baseline").value=baselineId;$("pixel-candidate").value=candidateId;
    renderPixelLab();pixelStatus("선택한 두 후보의 저장 진단과 원본 연결을 읽고 있습니다.");pixelURL();
    try{const [a,b]=await Promise.all([pixelDetail(id,baselineId),baselineId===candidateId?Promise.resolve(null):pixelDetail(id,candidateId)]);if(token!==pixelState.token)return;pixelState.a=a;pixelState.b=b||a;pixelState.loading=false;renderPixelLab();pixelStatus("저장된 구조·분광·방향 응답을 열었습니다. 파장 선택은 D65 RAW를 바꾸지 않습니다.");}
    catch(err){if(token!==pixelState.token)return;pixelState.loading=false;renderPixelLab();pixelStatus(err.message,true);throw err;}
  }
  function pixelLineChart(id,curves,xValues,{labels=null,yTitle="",marker=null}={}){
    const valid=curves.filter(curve=>Array.isArray(curve.values)&&curve.values.some(finite));if(!valid.length){$(id).innerHTML='<p class="pixel-empty">이 조건의 저장된 응답이 없습니다. 다른 조건으로 대체하지 않습니다.</p>';return;}
    const width=680,height=230,p={l:51,r:16,t:23,b:43},xmin=Math.min(...xValues),xmax=Math.max(...xValues),maximum=Math.max(1e-9,...valid.flatMap(curve=>curve.values.filter(finite))),ymax=maximum*1.08,x=v=>p.l+(v-xmin)/Math.max(xmax-xmin,1)*(width-p.l-p.r),y=v=>height-p.b-v/ymax*(height-p.t-p.b);
    let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(yTitle)}"><text x="${p.l}" y="12">${esc(yTitle)}</text>`;
    for(let i=0;i<=4;i++){const v=ymax*i/4;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(v)}" y2="${y(v)}" stroke="#e1eadb"/><text x="${p.l-7}" y="${y(v)+3}" text-anchor="end">${fmt(v,maximum<.1?4:2)}</text>`;}
    for(let i=0;i<xValues.length;i++)if(labels||i%5===0||i===xValues.length-1)svg+=`<text x="${x(xValues[i])}" y="${height-17}" text-anchor="middle">${esc(labels?.[i]||xValues[i])}</text>`;
    if(finite(marker)&&marker>=xmin&&marker<=xmax)svg+=`<line x1="${x(marker)}" x2="${x(marker)}" y1="${p.t}" y2="${height-p.b}" stroke="#b49a67" stroke-dasharray="3 4"/>`;
    for(const curve of valid){let segment=[];const flush=()=>{if(segment.length){svg+=`<polyline points="${segment.join(" ")}" fill="none" stroke="${curve.color}" stroke-width="2"${curve.dashed?' stroke-dasharray="5 3"':""}/>`;segment=[];}};curve.values.forEach((v,i)=>{if(finite(v)&&finite(xValues[i]))segment.push(`${x(xValues[i])},${y(v)}`);else flush();});flush();}
    $(id).innerHTML=svg+'</svg><div class="pixel-legend">'+valid.map(curve=>`<span><i style="background:${curve.color}"></i>${esc(curve.label)}${curve.dashed?" · 점선":""}</span>`).join("")+'</div>';
  }
  function renderPixelStructure(){
    const a=pixelState.a,b=pixelState.b,section=b?.structure?.cross_section,layers=section?.layers||[];
    if(!layers.length){$("pixel-structure").innerHTML='<p class="pixel-empty">선택 후보의 저장 단면이 없습니다.</p>';return;}
    $("pixel-structure").innerHTML=layers.map(layer=>{const reference=a?.structure?.cross_section?.layers?.find(item=>item.name===layer.name);return `<div class="pixel-structure-layer${layer.name==="cfa"?" cfa":""}"><span>${esc({fixed_top_polymer:"상부 polymer",cfa:"CFA",passivation:"Passivation",active_silicon:"활성 Si"}[layer.name]||layer.name)}<small><br>${esc(layer.material)}</small></span><span>A ${fmt(reference?.thickness_um,3)}</span><strong>B ${fmt(layer.thickness_um,3)} µm</strong></div>`;}).join("")+`<p class="field-note">Pitch ${fmt(section.pixel_pitch_um,1)} µm · 상부 적층 ${fmt(section.top_stack_height_um,2)} µm<br>실측 구조와 재료·수집 가정은 출처 명세에서 구분합니다.</p>`;
  }
  function renderPixelBudget(){
    const detail=pixelState.b,field=pixelField(detail),budget=field?.photon_budget?.[pixelState.mode];
    text("pixel-budget-title",`A / B · ${state.field} · ${pixelState.wavelength} nm · ${pixelState.site} · ${{normal:"정면",chief:"주광선",pupil:"전체 동공"}[pixelState.mode]}`);
    const budgetA=pixelField(pixelState.a)?.photon_budget?.[pixelState.mode],rows=[["반사","reflection"],["상부층 흡수","layer_absorption.lens"],["CFA 흡수","layer_absorption.cfa"],["Passivation 흡수","layer_absorption.passivation"],["Si 흡수","silicon_absorption"],["깊은 투과","deep_transmission"],["수집 광전자","collected_electrons"],["수집 손실","collection_loss"]];
    $("pixel-photon-budget").innerHTML=budget?rows.map(([label,key])=>{const a=pixelSample(pixelState.a,get(budgetA,key)),b=pixelSample(detail,get(budget,key));return `<div class="pixel-budget-row"><span>${label}</span><span class="pixel-budget-pair">${[a,b].map((value,index)=>`<span class="pixel-budget-track"><i class="pixel-budget-fill${index?"":" baseline"}" style="width:${finite(value)?Math.min(100,Math.max(0,value*100)):0}%"></i></span>`).join("")}</span><strong><small>${finite(a)?fmt(a*100,2)+"%":"—"}</small><br>${finite(b)?fmt(b*100,2)+"%":"—"}</strong></div>`;}).join("")+'<p class="field-note">주황 A / 초록 B · 입사 광자 1 기준. 수집 광전자와 수집 손실은 Si 흡수의 분해이며 위 광학 수지에 다시 더하지 않습니다.</p>':'<p class="pixel-empty">이 시야·방향 모드의 광자 수지가 없습니다. 정면 값으로 대체하지 않습니다.</p>';
  }
  function renderPixelQE(){
    const curves=[];for(const [detail,label,color]of [[pixelState.a,"A","#b8823c"],[pixelState.b,"B","#087f83"]]){const table=pixelQE(detail),s=detail?.sites?.indexOf(pixelState.site);if(table)curves.push({label:`${label} · CFA ×${fmt(detail.cfa_thickness_scale,1)} · ${pixelState.site}`,color,values:table.map(row=>row[s])});}
    pixelLineChart("pixel-qe-chart",curves,pixelState.b?.wavelength_nm||[],{yTitle:"절대 QE · e⁻ / 입사 광자",marker:pixelState.wavelength});text("pixel-qe-legend",`${state.field} · ${pixelState.mode} · 파장축 nm`);
  }
  function renderPixelAngular(){
    const details=[pixelState.a,pixelState.b],fieldRows=pixelState.b?.fields||[],modes=["normal","chief","pupil"],colors=["#9caa8d","#78a2a2","#087f83"],curves=[];
    for(const [index,detail]of details.entries())for(const [j,mode]of modes.entries())curves.push({label:`${index?"B":"A"} · ${{normal:"정면",chief:"CRA",pupil:"전체 동공"}[mode]}`,color:colors[j],dashed:!index,values:fieldRows.map(row=>pixelSample(detail,pixelQE(detail,mode,detail?.fields?.find(item=>item.id===row.id))))});
    pixelLineChart("pixel-angle-chart",curves,fieldRows.map((_,i)=>i),{labels:fieldRows.map(row=>row.id==="center"?"중심":row.id.replace("mid-","M-").replace("outer-","O-")),yTitle:`${pixelState.wavelength} nm · ${pixelState.site} 절대 QE`});
    $("pixel-field-buttons").innerHTML=fieldRows.map(row=>`<button type="button" data-pixel-field="${esc(row.id)}" aria-pressed="${row.id===state.field}">${esc(fallbackPoints.find(point=>point.id===row.id)?.label||row.id)}</button>`).join("");
    $("pixel-field-buttons").querySelectorAll("[data-pixel-field]").forEach(button=>button.onclick=()=>{state.field=button.dataset.pixelField;renderPixelLab();pixelURL();});
    const field=pixelField(pixelState.b),wi=pixelState.b?.wavelength_nm?.indexOf(pixelState.wavelength);$("pixel-angular-facts").innerHTML=`<p class="field-note">선택 시야 ${esc(state.field)} · 주광선 입사각 ${fmt(field?.chief_theta_deg?.[wi],2)}°</p><table class="pixel-angle-table"><thead><tr><th>응답 방식</th><th>A QE</th><th>B QE</th><th>B − A</th></tr></thead><tbody>${modes.map(mode=>{const a=pixelSample(pixelState.a,pixelQE(pixelState.a,mode)),b=pixelSample(pixelState.b,pixelQE(pixelState.b,mode));return `<tr><td>${{normal:"정면 입사",chief:"주광선 CRA",pupil:"전체 동공 평균"}[mode]}</td><td>${fmt(a,5)}</td><td>${fmt(b,5)}</td><td>${fmt(finite(a)&&finite(b)?b-a:null,5)}</td></tr>`;}).join("")}</tbody></table>`;
    renderPixelPupilHistogram();
  }
  function pixelHistogram(detail){
    const field=pixelField(detail),histogram=field?.pupil_histogram,wi=detail?.wavelength_nm?.indexOf(pixelState.wavelength),edges=histogram?.bin_edges_deg,weights=histogram?.weights?.[wi];
    if(field?.status==="unavailable"||!Array.isArray(edges)||!Array.isArray(weights)||edges.length!==weights.length+1||!weights.length||!edges.every((v,i)=>finite(v)&&(!i||v>edges[i-1]))||!weights.every(v=>finite(v)&&v>=0))return null;
    const sum=weights.reduce((total,value)=>total+value,0);if(Math.abs(sum-1)>1e-6)return null;
    return {edges,weights,sum};
  }
  function renderPixelPupilHistogram(){
    const all=[{label:"A",color:"#b8823c",histogram:pixelHistogram(pixelState.a)},{label:"B",color:"#087f83",histogram:pixelHistogram(pixelState.b)}],valid=all.filter(item=>item.histogram),root=$("pixel-pupil-histogram");
    if(!valid.length){root.innerHTML='<p class="pixel-empty">선택 시야·파장의 유효한 동공 가중치 분포가 없습니다. 다른 시야나 정면 분포로 대체하지 않습니다.</p>';return;}
    const width=680,height=210,p={l:48,r:15,t:22,b:42},xmin=Math.min(...valid.map(item=>item.histogram.edges[0])),xmax=Math.max(...valid.map(item=>item.histogram.edges.at(-1))),ymax=Math.max(.05,...valid.flatMap(item=>item.histogram.weights))*1.1,x=v=>p.l+(v-xmin)/(xmax-xmin)*(width-p.l-p.r),y=v=>height-p.b-v/ymax*(height-p.t-p.b);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(state.field)} ${pixelState.wavelength} nm 조건부 동공 광자 가중치"><text x="${p.l}" y="12">조건부 광자 가중치 · ${pixelState.wavelength} nm · ${esc(state.field)}</text>`;
    for(let i=0;i<=3;i++){const value=ymax*i/3;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(value)}" y2="${y(value)}" stroke="#e1eadb"/><text x="${p.l-6}" y="${y(value)+3}" text-anchor="end">${fmt(value*100,0)}%</text>`;}
    for(const [side,item]of all.entries()){if(!item.histogram)continue;const {edges,weights}=item.histogram;for(let i=0;i<weights.length;i++){const left=x(edges[i]),span=x(edges[i+1])-left,barWidth=Math.max(1,span*.36),xx=left+span*(side?.54:.1);svg+=`<rect x="${xx}" y="${y(weights[i])}" width="${barWidth}" height="${height-p.b-y(weights[i])}" fill="${item.color}" opacity=".85"><title>${item.label} ${edges[i]}–${edges[i+1]}° · ${fmt(weights[i]*100,2)}%</title></rect>`;}}
    for(const value of valid[0].histogram.edges)svg+=`<text x="${x(value)}" y="${height-24}" text-anchor="middle">${fmt(value,0)}</text>`;
    svg+=`<text x="${width/2}" y="${height-5}" text-anchor="middle">센서면 입사각 · deg</text></svg>`;
    root.innerHTML=svg+'<div class="pixel-legend">'+all.map(item=>`<span><i style="background:${item.color}"></i>${item.label} ${item.histogram?"가중치 합 "+fmt(item.histogram.sum,6):"분포 없음"}</span>`).join("")+'</div><p class="field-note">저장된 조건부 광자 가중치를 그대로 표시합니다. 다시 정규화하거나 추가 cos·면적·투과율을 곱하지 않습니다. CFA site 선택으로 렌즈의 방향 분포가 바뀌지는 않습니다.</p>';
  }
  function pixelColorPair(){
    const same=pixelState.colorMode==="same_cfa",a=same?pixelState.b:pixelState.a,b=pixelState.b,modeA=same?"frozen":pixelState.colorMode,modeB=same?"munsell":pixelState.colorMode;
    return [{detail:a,mode:modeA,value:pixelField(a)?.color_comparison?.[modeA],label:"A"},{detail:b,mode:modeB,value:pixelField(b)?.color_comparison?.[modeB],label:"B"}];
  }
  function renderPixelColor(){
    const pair=pixelColorPair(),fields=pixelState.b?.fields||[],colors=["#b8823c","#087f83"];
    pixelLineChart("pixel-color-chart",pair.map((item,index)=>({label:`${item.label} · ${item.mode}`,color:colors[index],values:fields.map(field=>item.detail?.fields?.find(row=>row.id===field.id)?.color_comparison?.[item.mode]?.delta_e00_mean)})),fields.map((_,i)=>i),{labels:fields.map(field=>field.id==="center"?"중심":field.id.replace("mid-","M-").replace("outer-","O-")),yTitle:"Macbeth 평균 ΔE00 · 낮을수록 작음"});
    pixelLineChart("pixel-noise-chart",pair.map((item,index)=>({label:`${item.label} · ${item.mode}`,color:colors[index],values:[item.value?.color_noise_rgb?.r,item.value?.color_noise_rgb?.g,item.value?.color_noise_rgb?.b,item.value?.color_noise_luma]})),[0,1,2,3],{labels:["R","G","B","Y"],yTitle:`${state.field} · signed linear sRGB RMS`});
    $("pixel-color-facts").innerHTML=`<table class="pixel-color-table"><thead><tr><th>${pixelState.colorMode==="same_cfa"?"같은 B RAW · 보정 효과":"같은 보정 방법 · 센서 변경 효과"}</th><th>균일장 평균 광전자<br>e⁻ / native pixel</th><th>균일장 RAW Gr<br>시간 σ · DN</th><th>평균 ΔE00</th><th>최대 ΔE00</th><th>색 패치 ISP Y<br>시간 σ · linear sRGB</th></tr></thead><tbody>${pair.map(item=>{const input=pixelField(item.detail)?.source_uniform_metrics,raw=input?.raw_green_noise;return `<tr><td>${item.label} · CFA ×${fmt(item.detail?.cfa_thickness_scale,1)} · ${item.mode==="munsell"?"Munsell CCM":"기존 CCM"}</td><td>${fmt(input?.mean_electrons,2)}</td><td>${fmt(raw?.status==="ok"&&raw?.unit==="DN"?raw.temporal_std:null,4)}</td><td>${fmt(item.value?.delta_e00_mean,3)}</td><td>${fmt(item.value?.delta_e00_max,3)}</td><td>${fmt(item.value?.color_noise_luma,6)}</td></tr>`;}).join("")}</tbody></table><p class="field-note">전자·RAW는 균일장 원본, ISP 잡음은 색 패치의 반복 취득 지표입니다. 같은 CFA의 CCM만 바꾸면 원본 광전자와 RAW 잡음은 동일하게 유지됩니다.</p>`;
    renderPixelColorScatter(pair);renderPixelPatches(pair);
    $("pixel-ccm-norms").innerHTML=`<table class="pixel-color-table"><thead><tr><th>고정 CCM</th><th>R 열</th><th>G 열</th><th>B 열</th></tr></thead><tbody>${pair.map(item=>`<tr><td>${item.label} · ${item.mode}</td>${[0,1,2].map(i=>`<td>${fmt(item.value?.calibration?.column_l2_norm?.[i],7)}</td>`).join("")}</tr>`).join("")}</tbody></table>`+pair.map(item=>`<p class="field-note pixel-calibration-id">${item.label} · ${esc(item.value?.calibration?.id||"보존 spectral CCM")}<br>${esc(item.value?.calibration?.column_norm_unit||"norm 자료 없음")}${item.value?.calibration?.matrix_sha256?"<br>matrix "+esc(item.value.calibration.matrix_sha256):""}</p>`).join("");
  }
  function renderPixelColorScatter(pair){
    const points=[];for(const [index,item]of pair.entries())for(const field of item.detail?.fields||[]){const color=field.color_comparison?.[item.mode];if(finite(color?.color_noise_luma)&&finite(color?.delta_e00_mean))points.push({field:field.id,x:color.color_noise_luma,y:color.delta_e00_mean,label:item.label,index});}
    if(!points.length){$("pixel-color-scatter").innerHTML='<p class="pixel-empty">색 오차와 시간 잡음을 함께 가진 평가 시야가 없습니다.</p>';return;}
    const width=680,height=230,p={l:55,r:40,t:18,b:44},xmin=Math.min(...points.map(point=>point.x))*.9,xmax=Math.max(...points.map(point=>point.x))*1.1,ymax=Math.max(1,...points.map(point=>point.y))*1.15,x=v=>p.l+(v-xmin)/Math.max(1e-9,xmax-xmin)*(width-p.l-p.r),y=v=>height-p.b-v/ymax*(height-p.t-p.b);let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="시야별 색 오차 대 시간 잡음">`;
    for(let i=0;i<=4;i++){const yy=ymax*i/4,xx=xmin+(xmax-xmin)*i/4;svg+=`<line x1="${p.l}" x2="${width-p.r}" y1="${y(yy)}" y2="${y(yy)}" stroke="#e0e9dc"/><text x="${p.l-7}" y="${y(yy)+3}" text-anchor="end">${fmt(yy,1)}</text><text x="${x(xx)}" y="${height-24}" text-anchor="middle">${fmt(xx,5)}</text>`;}
    for(const point of points){const selected=point.field===state.field,color=point.index?"#087f83":"#b8823c";svg+=`<circle cx="${x(point.x)}" cy="${y(point.y)}" r="${selected?6:4}" fill="${color}" stroke="${selected?"#294d3c":"white"}" stroke-width="${selected?2:1}"><title>${point.label} ${esc(point.field)} · ΔE00 ${fmt(point.y,3)} · Y RMS ${fmt(point.x,6)}</title></circle>`;if(selected||["center","outer-ne"].includes(point.field))svg+=`<text x="${x(point.x)+8}" y="${y(point.y)+(point.index?12:-8)}">${point.label} ${point.field==="center"?"중심":esc(point.field)}</text>`;}
    $("pixel-color-scatter").innerHTML=svg+`<text x="${width/2}" y="${height-5}" text-anchor="middle">Y 시간 잡음 · signed linear sRGB RMS</text><text x="13" y="${height/2}" transform="rotate(-90 13 ${height/2})" text-anchor="middle">평균 ΔE00</text></svg><div class="pixel-legend"><span><i style="background:#b8823c"></i>A</span><span><i style="background:#087f83"></i>B</span><span>시야 선택은 ‘주변부 적합성’의 9점 버튼을 사용합니다.</span></div>`;
  }
  function renderPixelPatches(pair){
    const patches=[...new Set(pair.flatMap(item=>(item.value?.patches||[]).map(row=>row.patch)))];
    if(!patches.length){$("pixel-patch-delta").innerHTML='<p class="pixel-empty">이 시야의 패치별 평가가 없습니다.</p>';return;}
    $("pixel-patch-delta").innerHTML=`<table class="pixel-color-table"><thead><tr><th>패치</th><th>A ΔE00</th><th>B ΔE00</th><th>B − A</th></tr></thead><tbody>${patches.map(patch=>{const [a,b]=pair.map(item=>item.value?.patches?.find(row=>row.patch===patch)?.delta_e00);return `<tr><td>${esc(patch)}</td><td>${fmt(a,2)}</td><td>${fmt(b,2)}</td><td>${fmt(finite(a)&&finite(b)?b-a:null,2)}</td></tr>`;}).join("")}</tbody></table>`;
  }
  function pixelSource(item){const ref=item.detail?.source_refs?.color,candidateId=ref?.candidates?.[item.mode]||item.value?.candidate_id;if(!ref?.run_id||!candidateId||!item.value)return null;const base=item.value.files_base||`${ref.files_base||API+"/runs/"+encodeURIComponent(ref.run_id)+"/files/"}${(ref.files_base||"").endsWith("/")||!ref.files_base?"":"/"}${encodeURIComponent(candidateId)}/`;return {runId:ref.run_id,candidateId,base};}
  function pixelSourceURL(source,path){if(!source||typeof path!=="string"||!path)return null;if(path.startsWith("/")||/^https?:/.test(path))return path;return source.base.replace(/\/$/,"")+"/"+path.split("/").map(encodeURIComponent).join("/");}
  function renderPixelImages(){
    const pair=pixelColorPair();for(const [index,item]of pair.entries()){
      const side=index?"b":"a",source=pixelSource(item),images=item.value?.images?.[pixelState.imageChart],path=images?.[pixelState.imageStage],url=pixelSourceURL(source,path),image=$("pixel-image-"+side),empty=$("pixel-image-"+side+"-empty"),token=JSON.stringify([pixelState.study?.id,item.detail?.id,item.mode,state.field,pixelState.imageChart,pixelState.imageStage]);
      image.dataset.binding=token;image.hidden=true;empty.hidden=false;empty.textContent=url?"선택 원본 영상을 읽고 있습니다.":"선택한 원본·시야·단계의 영상이 없습니다.";
      text("pixel-image-"+side+"-title",`${item.label} · CFA ×${fmt(item.detail?.cfa_thickness_scale,1)} · ${item.mode==="munsell"?"Munsell CCM":"기존 CCM"}`);text("pixel-image-"+side+"-source",source?`${source.runId} / ${source.candidateId} · ${state.field} · ${pixelState.imageChart}`:"해당 후보의 색 처리 원본 연결이 없습니다.");
      image.onload=()=>{if(image.dataset.binding!==token)return;image.hidden=false;empty.hidden=true;};image.onerror=()=>{if(image.dataset.binding!==token)return;image.hidden=true;empty.hidden=false;empty.textContent="이 원본 영상 파일을 불러오지 못했습니다.";};if(url){if(image.getAttribute("src")!==url)image.src=url;else if(image.complete&&image.naturalWidth>0)image.onload();}else image.removeAttribute("src");
      const raw=pixelSourceURL(source,images?.raw),link=$("pixel-image-"+side+"-raw");link.classList.toggle("hidden",!raw);if(raw)link.href=raw;
    }
    const a=pixelSource(pair[0]),b=pixelSource(pair[1]),link=$("pixel-open-images");link.classList.toggle("hidden",!a||!b);if(a&&b){const query=new URLSearchParams({run:b.runId,candidate:b.candidateId,baseline:a.runId,baseline_candidate:a.candidateId,field:state.field,chart:pixelState.imageChart,stage:pixelState.imageStage==="raw_preview"?"raw":pixelState.imageStage==="display"?"isp":"linear"});link.href="/reference-workshop.html?"+query;$("pixel-bench-link").href=link.href;}
    text("pixel-image-binding",`D65 · 1000 cd/m² · 4 ms · gain 1 · 원본 8회 취득. ${pixelState.wavelength} nm 선택은 이 영상을 변경하지 않습니다.`);
  }
  function renderPixelLab(){
    pixelControls();$("pixel-wave").value=pixelState.wavelength;$("pixel-site").value=pixelState.site;$("pixel-angle-mode").value=pixelState.mode;$("pixel-color-mode").value=pixelState.colorMode;
    const fieldRows=pixelState.b?.fields||fallbackPoints;$("pixel-field").innerHTML=fieldRows.map(field=>`<option value="${esc(field.id)}">${esc(fallbackPoints.find(point=>point.id===field.id)?.label||field.id)} · ${esc(field.id)}</option>`).join("");$("pixel-field").value=state.field;
    document.querySelectorAll("[data-pixel-story]").forEach(button=>{const selected=button.dataset.pixelStory===pixelState.story;button.classList.toggle("active",selected);button.setAttribute("aria-selected",String(selected));});document.querySelectorAll("[data-pixel-panel]").forEach(panel=>panel.classList.toggle("hidden",panel.dataset.pixelPanel!==pixelState.story));
    const a=pixelState.a,b=pixelState.b;$("pixel-binding").innerHTML=a&&b?`<strong>${esc(pixelState.study.id)}</strong><br>A: ${esc(a.id)} · CFA ×${fmt(a.cfa_thickness_scale,1)} / B: ${esc(b.id)} · CFA ×${fmt(b.cfa_thickness_scale,1)} · ${esc(state.field)}<br>진단 ${pixelState.wavelength} nm / ${esc(pixelState.site)} / ${esc(pixelState.mode)} · 원본 취득·색 보정 링크는 아래 영상에 표시합니다.`:"선택 후보를 읽는 동안 이전 진단과 영상을 표시하지 않습니다.";
    if(a&&b&&document.body?.classList.contains("pixel-only")){text("lens-reference","US10310255B2 · p0014");text("lens-origin","고정 특허 처방 · intermediate · 보존된 기하 광선 응답");text("sensor-reference","IMX462 구조 기반 픽셀");text("sensor-origin",`${fmt(b.structure?.cross_section?.pixel_pitch_um,1)} µm · CFA ×0.9 / 1.0 / 1.1 · 재료·readout은 선언 모델`);}
    renderPixelStructure();renderPixelBudget();renderPixelQE();renderPixelAngular();renderPixelColor();renderPixelImages();
    const report=$("pixel-report"),url=pixelState.study?.report_url;report.classList.toggle("hidden",!url);if(url)report.href=url;
  }
  async function submitPixelStudy(){
    if(!pixelState.template||pixelState.active)return;pixelState.active={status:"submitting"};pixelControls();
    try{const job=await api("/pixel-studies",clone(pixelState.template));pixelState.active=job;pixelStatus("고정된 세 CFA의 진단 작업을 제출했습니다. 보존 RAW를 다시 취득하지 않습니다.");pixelPoll();}catch(err){pixelState.active=null;pixelControls();pixelStatus(err.message,true);}
  }
  async function pixelPoll(){
    if(!pixelState.active?.id)return;const id=pixelState.active.id;
    try{const job=await api(`/runs/${encodeURIComponent(id)}`);if(pixelState.active?.id!==id)return;pixelState.active=job;if(busy(job)){pixelStatus(`Pixel study ${names[job.status]} · ${job.progress?.stage||job.progress?.message||id}`);pixelState.poll=setTimeout(pixelPoll,1800);return;}pixelState.active=null;await refreshPixelStudies();pixelControls();if(finished(job))await openPixelStudy(id);else pixelStatus(`${names[job.status]||job.status} · ${typeof job.error==="string"?job.error:JSON.stringify(job.error||{})}`,true);}catch(err){pixelStatus("작업 상태 확인 실패 · "+err.message,true);pixelState.poll=setTimeout(pixelPoll,3000);}
  }
  async function savePixelComparison(){
    if(!pixelState.a||!pixelState.b||pixelState.loading)return;const token=pixelState.token,id=pixelState.study.id,payload={name:$("pixel-comparison-name").value.trim()||"Pixel design comparison",candidate_id:pixelState.candidateId,baseline_candidate_id:pixelState.baselineId,field_id:state.field,wavelength_nm:pixelState.wavelength,site:pixelState.site,color_mode:pixelState.colorMode,angle_mode:pixelState.mode,story:pixelState.story};
    $("pixel-save-comparison").disabled=true;try{const record=await api(`/pixel-studies/${encodeURIComponent(id)}/comparisons`,payload);if(token!==pixelState.token)return;const link=$("pixel-comparison-download");link.href=API+`/pixel-studies/${encodeURIComponent(id)}/comparisons/${encodeURIComponent(record.id)}`;link.classList.remove("hidden");pixelStatus(`비교 선택을 저장했습니다 · ${record.id}. 원본 계산 결과는 변경하지 않았습니다.`);}finally{pixelControls();}
  }
  function bindPixelLab(){
    $("pixel-wave").innerHTML=Array.from({length:21},(_,i)=>450+i*10).map(w=>`<option value="${w}">${w} nm</option>`).join("");$("pixel-wave").value=550;
    for(const id of ["pixel-lab-toggle","open-pixel-lab"])$(id).onclick=()=>{showPixelLab();$("pixel-lab-panel").scrollIntoView({behavior:"smooth",block:"start"});};
    $("pixel-study-refresh").onclick=()=>refreshPixelStudies(true).catch(err=>pixelStatus(err.message,true));$("pixel-study-select").onchange=()=>openPixelStudy($("pixel-study-select").value).catch(err=>pixelStatus(err.message,true));
    $("pixel-study-submit").onclick=submitPixelStudy;$("pixel-save-comparison").onclick=()=>savePixelComparison().catch(err=>pixelStatus(err.message,true));
    $("pixel-field").onchange=()=>{state.field=$("pixel-field").value;renderPixelLab();pixelURL();};
    for(const id of ["pixel-baseline","pixel-candidate"])$(id).onchange=()=>openPixelStudy(pixelState.study.id,$("pixel-candidate").value,$("pixel-baseline").value).catch(err=>pixelStatus(err.message,true));
    for(const [id,key]of [["pixel-wave","wavelength"],["pixel-site","site"],["pixel-angle-mode","mode"],["pixel-color-mode","colorMode"],["pixel-image-stage","imageStage"],["pixel-image-chart","imageChart"]])$(id).onchange=()=>{pixelState[key]=key==="wavelength"?Number($(id).value):$(id).value;renderPixelLab();pixelURL();};
    document.querySelectorAll("[data-pixel-story]").forEach(button=>button.onclick=()=>{pixelState.story=button.dataset.pixelStory;renderPixelLab();pixelURL();});
  }
  async function initPixelLink(query){
    pixelState.visible=true;$("pixel-lab-panel").classList.remove("hidden");
    document.body.classList.add("pixel-only");
    text("lens-reference","특허 렌즈 Reference");text("lens-origin","선택 study의 고정 렌즈 원본을 확인하고 있습니다.");text("sensor-reference","IMX462 구조 기반 픽셀");text("sensor-origin","선택 study의 보존 구조·응답을 확인하고 있습니다.");
    notice("Pixel Design Lab · 고정 원본의 분광·방향 진단을 먼저 엽니다.");
    for(const [key,param,allowed]of [["story","pixel_story",["photon_budget","angular_match","color_noise"]],["site","pixel_site",["R","Gr","Gb","B"]],["mode","pixel_mode",["normal","chief","pupil"]],["colorMode","pixel_color",["frozen","munsell","same_cfa"]]])if(allowed.includes(query.get(param)))pixelState[key]=query.get(param);
    const wave=Number(query.get("pixel_wave"));if(wave>=450&&wave<=650&&wave%10===0)pixelState.wavelength=wave;
    await refreshPixelStudies();await openPixelStudy(query.get("pixel_study"),query.get("pixel_candidate"),query.get("pixel_baseline"));
  }
  function bind(){
    bindPixelLab();
    $("design-form").onsubmit=event=>{event.preventDefault();submit("capture").catch(error);};$("design-form").addEventListener("input",refreshEditor);$("design-form").addEventListener("change",refreshEditor);
    $("reset-config").onclick=()=>{if(state.template){applySpec(state.template);state.revision=null;$("revision-select").value="";text("revision-note","기준 입력을 불러왔습니다. 보존된 실행은 그대로 유지합니다.");}};
    document.querySelectorAll("[data-editor]").forEach(button=>button.onclick=()=>setEditor(button.dataset.editor));
    $("isp-profile").onchange=()=>{const profile=$("isp-profile").value;$("denoise").value=profile==="lsc_denoise"?.85:state.template.isp.denoise;refreshEditor();};
    $("luminance").onchange=()=>{$("display-white").value=Number($("luminance").value)/1000;refreshEditor();toast("광량 조건에 맞는 사전 고정 표시 범위를 적용했습니다.");};
    $("replay-button").onclick=()=>submit("replay").catch(error);
    $("isp-preview-button").onclick=()=>submitIspPreview().catch(error);
    $("isp-tuning-submit").onclick=()=>submitIspStudy("parameter_tuning").catch(error);
    $("tuning-preset").onclick=()=>{for(const axis of tuningAxes)$(axis.id).value=axis.default.join(", ");$("isp-family").value="v2";$("lsc-enabled").checked=true;refreshEditor();tuningEdited();setEditor("isp");};
    for(const axis of tuningAxes)$(axis.id).addEventListener("input",tuningEdited);
    for(const id of ["constraint-brightness-min","constraint-brightness-max","constraint-uniformity"])$(id).addEventListener("input",tuningEdited);
    $("isp-family").onchange=()=>{refreshEditor();toast($("isp-family").value==="v2"?"RAW ISP의 별도 설정을 사용합니다. 기존 Gaussian 강도를 변환하지 않습니다.":"기존 RGB Gaussian 파이프라인을 선택했습니다.");};
    $("clear-calibration").onclick=()=>{$("color-calibration-id").value="";refreshEditor();};
    document.querySelectorAll("[data-isp-recipe]").forEach(button=>button.onclick=()=>submitIspStudy(button.dataset.ispRecipe).catch(error));
    document.querySelectorAll("[data-isp-action]").forEach(button=>button.onclick=()=>{$("isp-family").value="legacy";$("isp-profile").value="lsc_denoise";$("denoise").value=.85;$("sharpen").value=0;setEditor("isp");refreshEditor();submit("replay").catch(error);});
    $("save-revision").onclick=async()=>{if(!state.ready||hasAdoptedReadout(state.current))return;try{const spec=readSpec(),revision=await api("/revisions",{name:spec.name,kind:state.editor==="isp"?"camera":state.editor,specification:spec,parent_id:state.revision?.id||null});state.revision=revision;await refreshRevisions();$("revision-select").value=revision.id;text("revision-note",`파생 revision 저장 · ${revision.id}`);}catch(err){error(err);}};
    $("revision-select").onchange=()=>{const revision=state.revisions.find(r=>r.id===$("revision-select").value);if(revision){state.revision=revision;applySpec(revision.specification);text("revision-note",`불러온 파생 revision · ${revision.id}`);}};
    $("pin-baseline").onclick=()=>{state.baseline=state.current;renderResults();updateURL();toast("현재 결과를 기준안 A로 고정했습니다.");};
    for(const button of document.querySelectorAll("[data-stage]"))button.onclick=()=>{state.stage=button.dataset.stage;renderViewer();updateURL();};
    $("chart").onchange=()=>{state.chart=$("chart").value;renderViewer();buttons();updateURL();};$("field-metric").onchange=renderMap;
    document.querySelectorAll("[data-study]").forEach(button=>button.onclick=()=>{const kind=button.dataset.study;if(kind==="optical")submit("sweep",[{path:"lens.curvature_scale",values:[.995,1,1.005]},{path:"lens.sensor_shift_um",values:[-20,0,20]}]).catch(error);else if(kind==="pixel")submit("sweep",[{path:"pixel.cfa_thickness_scale",values:[.9,1,1.1]}]).catch(error);else{$("isp-family").value="legacy";$("isp-profile").value=kind==="lsc"?"lsc":"lsc_denoise";$("denoise").value=kind==="lsc"?state.template.isp.denoise:.85;refreshEditor();setEditor("isp");submit("replay").catch(error);}});
    $("cancel-run").onclick=async()=>{if(state.active)try{await api(`/runs/${encodeURIComponent(state.active.id)}/cancel`,{});notice("취소 요청을 보냈습니다. 완료된 원본과 실패 상태를 보존합니다.","busy");schedulePoll();}catch(err){error(err);}};
    $("history-toggle").onclick=()=>historyPanel($("history-panel").classList.contains("hidden"));$("history-close").onclick=()=>historyPanel(false);$("refresh-runs").onclick=()=>refreshJobs().catch(error);
    $("zoom-in").onclick=()=>{state.zoom=Math.min(6,state.zoom+.5);transform();};$("zoom-out").onclick=()=>{state.zoom=Math.max(1,state.zoom-.5);if(state.zoom===1)state.pan=[0,0];transform();};$("zoom-reset").onclick=()=>{state.zoom=1;state.pan=[0,0];transform();};
    document.querySelectorAll(".image-viewport").forEach(view=>{let drag=null;view.onpointerdown=event=>{if(state.zoom<=1)return;drag=[event.clientX-state.pan[0],event.clientY-state.pan[1]];view.setPointerCapture(event.pointerId);};view.onpointermove=event=>{if(drag){state.pan=[event.clientX-drag[0],event.clientY-drag[1]];transform();}};view.onpointerup=view.onpointercancel=()=>drag=null;});
    document.addEventListener("keydown",event=>{if(event.key==="Escape")historyPanel(false);});
  }
  function hydrateCatalog(value){
    state.catalog=catalogData(value);state.template=value.specification;state.ready=true;
    // A linked result can already be visible. Never replace its selected candidate,
    // chart, stage or saved settings with the catalog's default specification.
    if(!state.current)applySpec(state.template);else refreshEditor();
    renderResults();buttons();
  }
  async function openInitialLink(query){
    const token=++state.openToken,[job,otherBase]=await Promise.all([api(`/runs/${encodeURIComponent(query.get("run"))}`),query.has("baseline")&&query.get("baseline")!==query.get("run")?api(`/runs/${encodeURIComponent(query.get("baseline"))}`):Promise.resolve(null)]);
    if(token!==state.openToken)return;
    const candidate=query.get("candidate")||summary(job)?.study?.selection?.selected_candidate_id||summary(job)?.candidates?.find(c=>!c.status||c.status==="succeeded")?.id||null;
    chooseRecipeView(summary(job)?.study?.recipe,query);
    if(summary(job)?.candidates){state.preparedJobs.set(job.id,job);state.sweep=job;state.baseline=studyBaseline(job);}
    if(query.has("baseline")){const base=otherBase||job;state.baseline=viewFrom(base,query.get("baseline_candidate"));}
    selectView(viewFrom(job,candidate));
    notice(state.ready?"링크에 고정된 Reference와 파생 설계를 열었습니다.":"보존된 영상·지표를 먼저 열었습니다. 카탈로그 준비 후 설정 편집과 재실행을 활성화합니다.",state.ready?"ready":"busy");
  }
  async function init(){
    if(new URLSearchParams(location.search).has("sensor_study"))return;
    bind();buttons();renderMap();renderMetrics();
    const query=new URLSearchParams(location.search),field=query.get("field");chooseEditor(query);chooseRecipeView(null,query);if(field&&points().some(p=>p.id===field))state.field=field;
    if(query.has("pixel_study")){await initPixelLink(query).catch(err=>pixelStatus(err.message,true));return;}
    if(query.has("run")){
      text("run-count","—");
      // Exact saved results do not depend on catalog-wide history verification.
      // Start them first and render as soon as their own checked payloads arrive.
      const preview=openInitialLink(query).catch(error);
      const catalog=api("/catalog").then(value=>{hydrateCatalog(value);refreshRevisions().catch(err=>toast("설계 revision 조회 지연 · "+err.message));if(state.current)notice("Reference 카탈로그 준비 완료 · 보존 결과를 유지하며 설정을 편집할 수 있습니다.");}).catch(error);
      await Promise.all([preview,catalog]);buttons();renderPreparedStudies();return;
    }
    try{hydrateCatalog(await api("/catalog"));refreshRevisions().catch(err=>toast("설계 revision 조회 지연 · "+err.message));
      await refreshJobs();
      const latest=state.jobs.find(job=>job.status==="succeeded"&&!job.axes);if(latest){const job=await api(`/runs/${encodeURIComponent(latest.id)}`);if(summary(job).candidates){chooseRecipeView(summary(job).study?.recipe,query);state.sweep=job;state.baseline=studyBaseline(job);const candidate=summary(job).study?.selection?.selected_candidate_id||summary(job).candidates.find(c=>!c.status||c.status==="succeeded")?.id;if(candidate)selectView(viewFrom(job,candidate));else renderSweep();}else{state.baseline=viewFrom(job);selectView(viewFrom(job));}}renderResults();notice("Reference를 준비했습니다. 기준안에서 한 설계 변수를 바꾸어 비교하세요.");const active=state.jobs.find(busy);if(active){state.active=active;schedulePoll();}
      buttons();renderPreparedStudies();
    }catch(err){error(err);}
  }
  init();
})();

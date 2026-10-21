"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const ROOT = "/api/v1/reference-workshop/sensor-mode-studies";
  const labels = {analog_gain:"Analog gain",digital_gain:"Digital gain",balanced_gain:"균형 gain",pixel_iid:"픽셀 독립 잡음",column_noise:"열 공유 잡음",cds_correlated:"상관 CDS",adc_10bit:"ADC 10 bit",adc_12bit:"ADC 12 bit",adc_14bit:"ADC 14 bit",native:"Native",digital_binning:"Digital binning",charge_binning:"이상적 전하 binning",hcg:"HCG",lcg:"LCG",dcg:"DCG 합성"};
  const chartLabels = {dark:"암흑 입력",uniform:"균일장",resolution:"Slanted edge",texture:"Dead leaves",color:"색 패치",demosaic_chirp:"무채색 chirp",dynamic_range:"밝기 단계",hdr_steps:"밝기 단계"};
  const stageLabels = {model_noise:"모델 잡음 힌트 RMS",measured_noise_valid:"반복 잡음 공통 유효 영역",temporal_residual:"반복평균 제거 잔차",measured_noise:"반복 취득 잡음 RMS",display:"고정 ISP",raw_preview:"RAW 미리보기",valid:"유효 영역",weights:"DCG HCG 기여 가중치",noise:"모델 잡음 RMS"};
  const readoutRecipes=["gain_budget","noise_structure","adc_headroom"];
  const recipeLabels={binning:"Binning",dual_gain:"Dual Gain",gain_budget:"Gain 배분",noise_structure:"CDS · 열잡음",adc_headroom:"ADC · 포화"};
  const readoutPresets={gain_budget:{fields:["center","outer-ne"],charts:["uniform","texture","dynamic_range"],light_scales:[.01,1]},noise_structure:{fields:["center","outer-ne"],charts:["dark","uniform","texture"],light_scales:[.01]},adc_headroom:{fields:["center","outer-ne"],charts:["uniform","dynamic_range"],light_scales:[1]}};
  const readoutDefaults={gain_total:8,post_gain_noise_e:4,cds_rms_e:2,cds_correlation:.8,column_fraction:.75,adc_bits:[10,12,14]};
  const readoutControls=[["gain_total","sensor-gain-total"],["post_gain_noise_e","sensor-post-gain-noise"],["cds_rms_e","sensor-cds-rms"],["cds_correlation","sensor-cds-correlation"],["column_fraction","sensor-column-fraction"]];
  const statusLabels = {queued:"대기",running:"계산 중",cancelling:"취소 중",cancelled:"취소됨",failed:"실패",succeeded:"완료"};
  const colors = ["#b68441","#298b7c","#6c76b0","#b6656d","#589653"];
  const state = {studies:[],template:null,job:null,summary:null,case:null,recipe:"binning",a:"native",b:"charge_binning",field:"center",chart:"uniform",light:null,stage:"display",stagePinned:false,interpolation:"nearest",viewSize:"common",zoom:1,validOverlay:true,token:0,listToken:0,csrf:null,poll:null,visible:false,submitting:false,loading:false,cache:new Map(),imageToken:0};
  const esc = x => String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const finite = x => typeof x === "number" && Number.isFinite(x);
  const fmt = (x,d=3) => finite(x)?x.toLocaleString("ko-KR",{maximumFractionDigits:d}):"—";
  const clone = x => JSON.parse(JSON.stringify(x));
  const busy = job => ["queued","running","cancelling"].includes(job?.status);
  const specOf = job => job?.sensor_mode_study || job?.specification || summaryOf(job)?.specification || null;
  const summaryOf = job => job?.result?.summary || job?.summary || (Array.isArray(job?.cases)?job:null);
  const setText = (id,value) => {$(id).textContent=value;};
  const modeName = mode => mode?.label || labels[mode?.id] || mode?.id || "—";
  const stageName = stage => stage==="noise"&&readoutRecipes.includes(specOf(state.job)?.recipe)?"반복 취득 잡음 RMS":stageLabels[stage]||stage;
  const modes = () => Array.isArray(state.case?.modes)?state.case.modes:[];
  const selected = id => modes().find(m=>m.id===id)||null;
  const caseList = () => state.summary?.cases || [];
  function status(message,error=false){setText("sensor-status",message);$("sensor-status").classList.toggle("error",error);}
  function link(id,url){const node=$(id);if(url){node.href=url;node.classList.remove("hidden");}else{node.removeAttribute("href");node.classList.add("hidden");}}
  async function api(url,payload){
    if(payload!==undefined&&!state.csrf){const response=await fetch("/api/v1/session");if(!response.ok)throw Error("작업 세션을 확인하지 못했습니다.");state.csrf=(await response.json()).token;}
    const response=await fetch(url,{method:payload===undefined?"GET":"POST",headers:payload===undefined?{}:{"Content-Type":"application/json","x-platform-token":state.csrf},...(payload===undefined?{}:{body:JSON.stringify(payload)})});
    let data;try{data=await response.json();}catch{throw Error(`서버 응답을 읽지 못했습니다 (${response.status}).`);}
    if(!response.ok){const error=data.detail??data.error??data.message??data;throw Error(typeof error==="string"?error:JSON.stringify(error));}return data;
  }
  function filesURL(path){
    if(path&&typeof path==="object")path=path.path||path.url;
    if(typeof path!=="string"||!path)return null;
    if(path.startsWith("/"))return path;
    if(/^[a-z]+:/i.test(path)||path.split("/").includes(".."))return null;
    const base=state.summary?.files_base||`${ROOT}/${encodeURIComponent(state.job.id)}/files/`;
    return base.replace(/\/$/,"")+"/"+path.split("/").map(encodeURIComponent).join("/");
  }
  function options(id,items,value,label=x=>x){const node=$(id);node.innerHTML=items.map(item=>`<option value="${esc(item)}">${esc(label(item))}</option>`).join("");node.value=String(value??"");node.disabled=!items.length;}
  function updateButtons(){
    for(const key of ["field","chart","light"])$("sensor-"+key).disabled=state.loading||!state.summary;
    $("sensor-submit").disabled=!state.template||state.submitting||state.loading||busy(state.job);
    $("sensor-profile-save").disabled=!state.case||!selected(state.a)||!selected(state.b)||state.submitting||state.loading;
    setText("sensor-replay",specOf(state.job)?.recipe==="dual_gain"?"보존 RAW로 재합성":"같은 명세 재실행");
    $("sensor-replay").disabled=!specOf(state.job)||busy(state.job)||state.submitting||state.loading;
    $("sensor-cancel").classList.toggle("hidden",!busy(state.job));
    document.querySelectorAll("[data-sensor-recipe]").forEach(node=>{node.classList.toggle("active",node.dataset.sensorRecipe===state.recipe);node.setAttribute("aria-pressed",String(node.dataset.sensorRecipe===state.recipe));});
    renderCostPreview();
    $("sensor-charge-options").classList.toggle("hidden",state.recipe!=="binning");
    $("sensor-dcg-options").classList.toggle("hidden",state.recipe!=="dual_gain");
    $("sensor-readout-options").classList.toggle("hidden",!readoutRecipes.includes(state.recipe));
    for(const id of [...readoutControls.map(pair=>pair[1]),"sensor-adc-bits"])$(id).disabled=state.submitting||state.loading||busy(state.job);
    setText("sensor-recipe-note",({binning:"RGGB 동일 사이트 4개 합산 · Native RAW → digital 재처리 / 평균 전자율 → 전하 합산 취득",dual_gain:"한 번 생성한 전하를 HCG·LCG로 읽습니다. 공유 shot noise·경로 공분산을 유지한 합성입니다.",gain_budget:"같은 총 gain의 배분 위치를 바꿉니다. 증폭 후 전압 잡음·ADC 양자화·포화가 달라지는 가상 회로입니다.",noise_structure:"독립 잡음과 열 공유 잡음의 공간 구조, 두 표본의 상관을 선언한 CDS 잔차를 비교합니다.",adc_headroom:"같은 광전자·gain·ADC 입력 범위에서 bit depth를 바꿉니다. 양자화와 clipping 위치를 따로 봅니다."})[state.recipe]||"선택 recipe의 저장 결과를 확인하세요.");
  }
  function submissionScope(){
    const recorded=specOf(state.job),preserved=readoutRecipes.includes(state.recipe)&&recorded?.recipe===state.recipe;
    const specification=clone(preserved?recorded:state.template||{});
    if(readoutRecipes.includes(state.recipe)&&!preserved)Object.assign(specification,clone(readoutPresets[state.recipe]));
    return {specification,preserved};
  }
  function renderCostPreview(){
    if(!readoutRecipes.includes(state.recipe)){setText("sensor-cost-preview","");return;}
    const {specification,preserved}=submissionScope(),cases=(specification.fields?.length||0)*(specification.charts?.length||0)*(specification.light_scales?.length||0),repeats=specification.frames||8;
    const bits=$("sensor-adc-bits").value.split(",").map(value=>value.trim()),modeCount=state.recipe==="adc_headroom"?bits.filter(Boolean).length:3;
    setText("sensor-cost-preview",`${preserved?"보존 명세 범위 유지":"준비 preset · "+recipeLabels[state.recipe]} · ${cases}조건 × ${modeCount} readout × ${repeats}회 = ${cases*modeCount*repeats} RAW 예상 · ${specification.fields?.length||0}시야 / ${(specification.charts||[]).map(chart=>chartLabels[chart]||chart).join("·")} / 광량 ${(specification.light_scales||[]).join(", ")}×`);
  }
  function hydrateSettings(spec){
    const readout={...readoutDefaults,...spec?.readout_lab};for(const[key,id]of readoutControls)$(id).value=readout[key];$("sensor-adc-bits").value=readout.adc_bits.join(", ");setText("sensor-readout-draft","표시된 결과는 저장 명세에 연결됩니다. 변경한 값은 새 계산 제출 시 적용됩니다.");
    if(spec?.isp)$("sensor-display-white").value=spec.isp.display_white_level;
    for(const [section,pairs]of [["charge_binning",[["capacity_multiplier","sensor-capacity"],["conversion_gain_multiplier","sensor-conversion-gain"],["read_noise_multiplier","sensor-read-noise"]]],["dcg_merge",[["saturation_margin_dn","sensor-dcg-margin"],["read_noise_guard_sigma","sensor-dcg-read-guard"],["gain_path_agreement_sigma","sensor-dcg-agreement"]]]])if(spec?.[section])for(const [key,id]of pairs)$(id).value=spec[section][key];
  }
  function updateURL(){if(!state.job)return;const url=new URL(location.href);["run","candidate","baseline","baseline_candidate","pixel_study"].forEach(k=>url.searchParams.delete(k));url.searchParams.set("sensor_study",state.job.id);if(state.case)url.searchParams.set("sensor_case",state.case.id);else url.searchParams.delete("sensor_case");url.searchParams.set("field",state.field);url.searchParams.set("sensor_a",state.a);url.searchParams.set("sensor_b",state.b);url.searchParams.set("sensor_interpolation",state.interpolation);url.searchParams.set("sensor_invalid",state.validOverlay?"on":"off");url.searchParams.set("sensor_stage",state.stage);history.replaceState(null,"",url);}
  function resampleDisplay(source,width,height,targetWidth,targetHeight,interpolation="bilinear",validity=null){
    const out=new Uint8ClampedArray(targetWidth*targetHeight*4);
    for(let y=0;y<targetHeight;y++){const sy=Math.max(0,Math.min(height-1,(y+.5)*height/targetHeight-.5)),y0=Math.floor(sy),y1=Math.min(height-1,y0+1),fy=sy-y0;
      for(let x=0;x<targetWidth;x++){const sx=Math.max(0,Math.min(width-1,(x+.5)*width/targetWidth-.5)),x0=Math.floor(sx),x1=Math.min(width-1,x0+1),fx=sx-x0,at=(y*targetWidth+x)*4;
        const indices=interpolation==="nearest"?[[Math.round(sy)*width+Math.round(sx),1]]:[[y0*width+x0,(1-fx)*(1-fy)],[y0*width+x1,fx*(1-fy)],[y1*width+x0,(1-fx)*fy],[y1*width+x1,fx*fy]];
        const invalid=validity&&indices.some(([index,weight])=>weight>0&&validity[index*4]<=127);
        if(invalid){const checker=(Math.floor(x/8)+Math.floor(y/8))%2;out[at]=checker?184:105;out[at+1]=checker?111:117;out[at+2]=checker?178:126;out[at+3]=255;continue;}
        for(let c=0;c<4;c++)out[at+c]=indices.reduce((sum,[index,weight])=>sum+source[index*4+c]*weight,0);
      }
    }return out;
  }
  function bilinearPixels(source,width,height,targetWidth,targetHeight){return resampleDisplay(source,width,height,targetWidth,targetHeight,"bilinear");}
  function decodeImage(image){const source=document.createElement("canvas");source.width=image.naturalWidth;source.height=image.naturalHeight;const context=source.getContext("2d",{willReadFrequently:true});context.drawImage(image,0,0);return context.getImageData(0,0,source.width,source.height);}
  function displayCanvas(image,mask,canvas,width,url,maskURL,interpolation){
    if(mask&&(mask.naturalWidth!==image.naturalWidth||mask.naturalHeight!==image.naturalHeight))throw Error("Validity map shape does not match the reconstructed image.");
    const height=Math.max(1,Math.round(width*image.naturalHeight/image.naturalWidth)),pixels=decodeImage(image),valid=mask?decodeImage(mask):null;canvas.width=width;canvas.height=height;
    const context=canvas.getContext("2d"),result=context.createImageData(width,height);result.data.set(resampleDisplay(pixels.data,image.naturalWidth,image.naturalHeight,width,height,interpolation,valid?.data));context.putImageData(result,0,0);
    canvas.style.width=width+"px";canvas.style.height=height+"px";canvas.dataset.sourceUrl=url;canvas.dataset.interpolation=interpolation+"_encoded_display_rgb";if(maskURL)canvas.dataset.validSourceUrl=maskURL;else delete canvas.dataset.validSourceUrl;canvas.hidden=false;image.hidden=true;
  }
  function clearImages(message="선택한 계산 결과를 불러오는 중입니다."){
    state.imageToken++;
    for(const side of ["a","b"]){const image=$("sensor-image-"+side),canvas=$("sensor-canvas-"+side);canvas.hidden=true;delete canvas.dataset.sourceUrl;delete canvas.dataset.validSourceUrl;image.hidden=true;image.removeAttribute("src");const empty=$("sensor-image-"+side+"-empty");empty.hidden=false;empty.textContent=message;link("sensor-image-"+side+"-array",null);setText("sensor-image-"+side+"-caption","");}
  }
  function clearResult(message="선택한 계산 결과를 불러오는 중입니다."){
    clearImages(message);for(const id of ["sensor-metrics","sensor-valid-note","sensor-flow","sensor-signal-plot","sensor-noise-plot","sensor-saturation-plot","sensor-sfr-plot","sensor-assumptions","sensor-sources","sensor-readout-budget","sensor-row-noise-plot","sensor-column-noise-plot","sensor-readout-nps-plot","sensor-ptc-plot","sensor-readout-binding"])$(id).innerHTML="";
    $("sensor-readout-diagnostics").classList.add("hidden");setText("sensor-binding",message);$("sensor-profile-download").classList.add("hidden");$("sensor-mode-a").disabled=true;$("sensor-mode-b").disabled=true;
  }
  function renderImages(){
    clearImages();const token=state.imageToken;
    for(const side of ["a","b"]){
      const mode=selected(state[side]),image=$("sensor-image-"+side),canvas=$("sensor-canvas-"+side),empty=$("sensor-image-"+side+"-empty"),path=mode?.files?.[state.stage],url=filesURL(path);
      setText("sensor-image-"+side+"-title",`${side.toUpperCase()} · ${modeName(mode)}`);
      const shape=mode?.shape||[],width=shape[1]||shape[0];
      setText("sensor-image-"+side+"-caption",mode?`${state.case.id} · ${mode.id} · ${shape.join(" × ")} 실제 출력 · ${mode.contribution_count??1} native pixel/site · ${state.stage==="display"?"고정 ISP · 자동 밝기 정규화 없음":`${stageName(state.stage)} · 수치 원본은 별도 보존`}`:"");
      if(state.interpolation==="bilinear")$("sensor-image-"+side+"-caption").textContent+=" · 표시 RGB에서 bilinear · native 측정값 유지";
      if(state.stage==="display")$("sensor-image-"+side+"-caption").textContent+=state.validOverlay?" · 회색·자홍 체크 = 저장된 유효 영역 밖 · 복원한 색이 아님":" · 무효 영역 표시 OFF · 포화·무효 영역의 원본 표시색을 복원된 색으로 해석하지 않습니다";
      if(state.stage==="display")$("sensor-image-"+side+"-caption").textContent+=` · 공통 white ${fmt(specOf(state.job)?.isp?.display_white_level)}`;
      const scales=mode?.preview_scales;if(scales&&["noise","measured_noise"].includes(state.stage))$("sensor-image-"+side+"-caption").textContent+=` · 흰색 = ${fmt(scales.noise_white_e_per_contributor)} e⁻ RMS / 기여 native 픽셀`;
      if(scales&&state.stage==="temporal_residual")$("sensor-image-"+side+"-caption").textContent+=` · 회색 = 0 · ±${fmt(scales.residual_limit_e)} e⁻ 공통 범위 · 첫 반복 − ${specOf(state.job)?.frames??8}회 반복평균 · 새로운 RAW 아님`;
      if(scales&&["weights","valid"].includes(state.stage))$("sensor-image-"+side+"-caption").textContent+=` · ${scales[state.stage]||""}`;
      link("sensor-image-"+side+"-array",filesURL(mode?.files?.raw||mode?.files?.linear));
      if(!url){empty.textContent="선택한 단계의 저장 영상이 없습니다.";continue;}
      const base=state.viewSize==="native"&&finite(width)?width:320,targetWidth=Math.round(base*state.zoom);
      const interpolation=state.interpolation,overlay=state.stage==="display"&&state.validOverlay;
      const failed=message=>{if(token!==state.imageToken)return;canvas.hidden=true;image.hidden=true;empty.hidden=false;empty.textContent=message;};
      const render=mask=>{if(token!==state.imageToken||image.getAttribute("src")!==url)return;try{if(mask||interpolation==="bilinear")displayCanvas(image,mask,canvas,targetWidth,url,mask?filesURL(mode.files.valid):null,interpolation);else{canvas.hidden=true;image.hidden=false;}empty.hidden=true;}catch(error){failed("영상과 유효 영역의 좌표를 표시하지 못했습니다. 원본 배열을 확인하세요.");}};
      image.onload=()=>{if(token!==state.imageToken||image.getAttribute("src")!==url)return;
        if(!overlay){render(null);return;}const validURL=filesURL(mode?.files?.valid);if(!validURL){failed("저장된 유효 영역이 없습니다. 무효 영역 표시를 끄면 원본 PNG를 확인할 수 있습니다.");return;}
        const mask=new Image();mask.onload=()=>render(mask);mask.onerror=()=>failed("유효 영역 파일을 읽지 못해 표시를 중단했습니다.");mask.src=validURL;
      };
      image.onerror=()=>{if(token!==state.imageToken||image.getAttribute("src")!==url)return;image.hidden=true;empty.hidden=false;empty.textContent="영상 파일을 불러오지 못했습니다. 수치·원본 연결을 확인하세요.";};
      image.src=url;image.style.imageRendering=state.interpolation==="nearest"?"pixelated":"auto";
      image.dataset.sourceUrl=url;image.style.width=`${targetWidth}px`;image.style.height="auto";
      if(image.complete&&image.naturalWidth)image.onload();
    }
  }
  function renderFlow(){
    const steps={analog_gain:[["광전자·well",""],["Analog gain","sum"],["증폭 후 noise · ADC","readout"]],digital_gain:[["광전자·well",""],["증폭 후 noise · ADC","readout"],["Digital gain·clip","sum"]],balanced_gain:[["광전자·well",""],["Analog gain","sum"],["증폭 후 noise · ADC","readout"],["Digital gain·clip","sum"]],pixel_iid:[["광전자·well",""],["독립 pixel read noise","readout"],["ADC","readout"]],column_noise:[["광전자·well",""],["pixel + column 공유 잡음","readout"],["ADC","readout"]],cds_correlated:[["광전자·well",""],["상관된 두 표본 차분","readout"],["ADC","readout"]],native:[["픽셀별 전하·well",""],["Read noise·ADC","readout"]],digital_binning:[["픽셀별 전하·well",""],["픽셀별 Read noise·ADC","readout"],["같은 site 4개 합산","sum"]],charge_binning:[["픽셀별 전하·well",""],["같은 site 4개 전하 합산","sum"],["합산 노드 제한","sum"],["한 번의 Read noise·ADC","readout"]],hcg:[["공유 전하·well",""],["HCG Read noise·ADC","readout"]],lcg:[["공유 전하·well",""],["LCG Read noise·ADC","readout"]],dcg:[["공유 전하·well",""],["HCG + LCG","readout"],["공분산 기반 합성","sum"]]};
    $("sensor-flow").innerHTML=[state.a,state.b].map(id=>`<div class="sensor-flow-row"><strong>${esc(modeName(selected(id)))}</strong>${(steps[id]||(id.startsWith("adc_")?[["광전자·well",""],["고정 analog 입력 범위",""],["선택 bit depth ADC","readout"]]:[])).map(([label,kind],i)=>`${i?" → ":""}<span class="${kind}">${esc(label)}</span>`).join("")} → <span>공통 ISP</span></div>`).join("");
  }
  function renderMetrics(){
    const a=selected(state.a)?.metrics||{},b=selected(state.b)?.metrics||{};
    const rows=[["평균 신호","mean_electrons","e⁻ / 기여 native 픽셀",1],["시간 잡음","temporal_noise_e","e⁻ RMS / 기여 native 픽셀",1],["SNR","snr","신호 / 시간 잡음",1],["Well / path 포화","well_saturation_fraction","% · 평균 반복 취득",100],["ADC 포화","adc_saturation_fraction","% · 평균 반복 취득",100],...(readoutRecipes.includes(specOf(state.job)?.recipe)?[["Digital 출력 rail","digital_saturation_fraction","% · ADC rail과 중복 가능",100],...(finite(a.digital_added_saturation_fraction)||finite(b.digital_added_saturation_fraction)?[["추가 Digital clip","digital_added_saturation_fraction","% · ADC rail 제외",100]]:[])]:[]),["국소 대비","local_contrast","선형 출력 · 저장된 진단",1]];
    setText("sensor-valid-note",`신호·잡음은 각 모드에서 ${specOf(state.job)?.frames??8}회 모두 유효한 내부 영역 기준입니다. A 유효 ${fmt(finite(a.valid_fraction)?a.valid_fraction*100:null,1)}% · B 유효 ${fmt(finite(b.valid_fraction)?b.valid_fraction*100:null,1)}%. 포화율의 분모에는 무효·포화 픽셀도 포함합니다.${readoutRecipes.includes(specOf(state.job)?.recipe)?" Well·ADC·digital rail은 중복될 수 있어 합산하지 않습니다. 추가 digital clip은 ADC rail을 제외합니다.":""}`);
    $("sensor-metrics").innerHTML=rows.map(([name,key,unit,scale])=>{const darkTest=state.chart==="dark"&&["snr","local_contrast"].includes(key);return `<article class="sensor-metric"><span>${name}</span><div><b>${darkTest?"N/A":finite(a[key])?fmt(a[key]*scale):"—"}</b><b>${darkTest?"N/A":finite(b[key])?fmt(b[key]*scale):"—"}</b></div><small>A / B · ${darkTest?"암신호 시험":unit}</small></article>`;}).join("");
  }
  function plot(id,series,{xlabel="광량 배율",ylabel="",xmax=null,markers=[],logX=false}={}){
    const valid=series.map((s,i)=>({...s,color:s.color||colors[i%colors.length],points:s.points.filter(([x,y])=>finite(x)&&finite(y)&&(!logX||x>0))})).filter(s=>s.points.length);
    if(!valid.length){$(id).innerHTML='<p class="sensor-empty">선택한 시야·입력의 저장 곡선이 없습니다. 없는 값을 보간해 채우지 않습니다.</p>';return;}
    const points=valid.flatMap(s=>s.points),xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
    let lo=Math.min(...xs),hi=xmax??Math.max(...xs),ylo=Math.min(0,...ys),yhi=Math.max(...ys);
    if(lo===hi){if(logX){lo/=2;hi*=2;}else{lo=Math.max(0,lo-.5);hi+=.5;}}if(ylo===yhi)yhi=ylo+1;
    const w=460,h=215,left=52,top=15,bottom=40,right=18,xx=x=>left+(logX?(Math.log10(x)-Math.log10(lo))/(Math.log10(hi)-Math.log10(lo)):(x-lo)/(hi-lo))*(w-left-right),yy=y=>h-bottom-(y-ylo)/(yhi-ylo)*(h-bottom-top);
    let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(ylabel)} · ${esc(xlabel)}">`;
    for(let n=0;n<=4;n++){const y=ylo+(yhi-ylo)*n/4;svg+=`<path d="M ${left} ${yy(y)} H ${w-right}" stroke="#e6eddf"/><text x="${left-8}" y="${yy(y)+3}" text-anchor="end">${fmt(y,2)}</text>`;const x=logX?10**(Math.log10(lo)+(Math.log10(hi)-Math.log10(lo))*n/4):lo+(hi-lo)*n/4;svg+=`<text x="${xx(x)}" y="${h-bottom+16}" text-anchor="middle">${fmt(x,3)}</text>`;}
    for(const marker of markers)if(finite(marker.x)&&marker.x>=lo&&marker.x<=hi)svg+=`<path d="M ${xx(marker.x)} ${top} V ${h-bottom}" stroke="${marker.color||"#9caa94"}" stroke-dasharray="4 3"/><text x="${xx(marker.x)-3}" y="${top+10}" text-anchor="end">${esc(marker.label)}</text>`;
    for(const line of valid){svg+=`<polyline fill="none" stroke="${line.color}" stroke-width="2" ${line.dashed?'stroke-dasharray="5 3"':''} points="${line.points.map(([x,y])=>`${xx(x)},${yy(y)}`).join(" ")}"/>`;for(const [x,y]of line.points)svg+=`<circle cx="${xx(x)}" cy="${yy(y)}" r="3" fill="${line.color}"><title>${esc(line.label)} · ${fmt(x)} / ${fmt(y)}</title></circle>`;}
    svg+=`<text x="${(left+w-right)/2}" y="${h-4}" text-anchor="middle">${esc(xlabel)}</text></svg><div class="sensor-legend">${valid.map(s=>`<span><i style="background:${s.color}"></i>${esc(s.label)}${s.dashed?" · 점선":""}</span>`).join("")}</div>`;$(id).innerHTML=svg;
  }
  function renderPlots(){
    const rows=(state.summary?.curves||[]).filter(row=>row.field_id===state.field&&row.chart===state.chart),pair=[selected(state.a),selected(state.b)].filter(Boolean);
    const tone=state.chart==="dynamic_range"&&pair.some(mode=>Array.isArray(mode.tone_curve));
    const series=(key,scale=1)=>pair.map((mode,i)=>({label:modeName(mode),color:colors[i],points:(tone?(mode.tone_curve||[]):rows.filter(row=>row.mode===mode.id)).map(row=>[tone?row.input_mean_electrons:row.light_scale,finite(row[key])?row[key]*scale:null]).sort((a,b)=>a[0]-b[0])}));
    const axis={xlabel:tone?"입력 광전자 / native 픽셀 · log 축":"광량 배율",logX:tone};
    plot("sensor-signal-plot",series("mean_electrons"),{...axis,ylabel:"평균 신호 e⁻"});plot("sensor-noise-plot",series("temporal_noise_e"),{...axis,ylabel:"시간 잡음 e⁻ RMS"});
    const sat=series("well_saturation_fraction",100).map(s=>({...s,label:s.label+" well/path"})).concat(series("adc_saturation_fraction",100).map(s=>({...s,label:s.label+" ADC",dashed:true})));
    if(readoutRecipes.includes(specOf(state.job)?.recipe)){sat.push(...series("digital_saturation_fraction",100).map(s=>({...s,label:s.label+" digital rail",dashed:true})));sat.push(...series("digital_added_saturation_fraction",100).map(s=>({...s,label:s.label+" 추가 digital clip",dashed:true})));}
    plot("sensor-saturation-plot",sat,{...axis,ylabel:"포화율 %"});
    const sfr=pair.map((mode,i)=>({label:modeName(mode),color:colors[i],points:(mode.sfr?.frequency_cycles_per_native_pixel||[]).map((x,j)=>[x,mode.sfr?.mtf?.[j]])}));
    const markers=pair.map((mode,i)=>({x:mode.sfr?.nyquist_cycles_per_native_pixel,label:`${modeName(mode)} Nyquist`,color:colors[i]}));
    plot("sensor-sfr-plot",sfr,{xlabel:"cycles / 원본 native pixel",ylabel:"처리 후 SFR",xmax:.5,markers});
  }
  function readReadoutSettings(){
    const settings={};for(const[key,id]of readoutControls){if(!$(id).reportValidity())throw Error("Readout 파라미터 범위를 확인하세요.");const value=Number($(id).value);if(!String($(id).value).trim()||!finite(value))throw Error("Readout 파라미터는 유한한 숫자여야 합니다.");settings[key]=value;}
    if(settings.gain_total<1||settings.gain_total>32||settings.post_gain_noise_e<0||settings.post_gain_noise_e>100||settings.cds_rms_e<0||settings.cds_rms_e>100||settings.cds_correlation< -1||settings.cds_correlation>1||settings.column_fraction<0||settings.column_fraction>1)throw Error("Readout 파라미터가 지원 범위를 벗어났습니다.");
    const tokens=$("sensor-adc-bits").value.split(",").map(value=>value.trim()),bits=tokens.map(Number);if(!tokens.length||tokens.length>3||tokens.some(value=>!value)||bits.some(value=>!Number.isInteger(value)||value<8||value>16)||new Set(bits).size!==bits.length)throw Error("ADC bits는 8–16 범위의 서로 다른 정수 1–3개로 입력하세요.");settings.adc_bits=bits;return settings;
  }
  function readoutMetadata(mode){return mode?.metadata?.metadata||mode?.metadata||{};}
  function renderReadoutDiagnostics(){
    const root=$("sensor-readout-diagnostics"),pair=[selected(state.a),selected(state.b)].filter(Boolean),present=readoutRecipes.includes(specOf(state.job)?.recipe);root.classList.toggle("hidden",!present);if(!present)return;
    setText("sensor-readout-binding",`${state.job.id} / ${state.case.id} · ${state.field} · ${chartLabels[state.chart]||state.chart} · A ${modeName(pair[0])} / B ${modeName(pair[1])}`);
    const components=[["광전자 shot","photo_shot_e2_mean"],["암전류 shot","dark_shot_e2_mean"],["입력 read noise","input_read_e2"],["증폭 후 noise의 입력 환산","post_gain_read_e2"],["CDS 잔차","cds_residual_e2"],["ADC 양자화","adc_quantization_e2"],["Digital 양자화","digital_quantization_e2"]];
    const budgets=pair.map(mode=>readoutMetadata(mode).noise_budget||mode.readout_diagnostics?.noise_budget||{});
    $("sensor-readout-budget").innerHTML=`<div class="sensor-budget-heading"><h4>모델 유도 잡음 예산 · 입력 기준 e⁻²</h4><span>실제 시간 측정과 구분 · clipping 이전의 분산 가정</span></div><table class="sensor-readout-table"><thead><tr><th>잡음 성분 · 분산</th>${pair.map(mode=>`<th>${esc(modeName(mode))}</th>`).join("")}</tr></thead><tbody>${components.map(([label,key])=>`<tr><td>${label}</td>${budgets.map(budget=>`<td>${fmt(budget[key],5)}</td>`).join("")}</tr>`).join("")}</tbody></table><p class="sensor-diagnostic-note">열 공유·CDS의 상관구조는 같은 RMS라도 다른 공간 형태를 만듭니다. 아래 곡선은 보존된 반복 취득에서 계산한 진단입니다.</p>`;
    const diagnostics=pair.map(mode=>({mode,data:mode.readout_diagnostics||{}}));
    for(const [id,key,label]of [["sensor-row-noise-plot","row_temporal_rms_e","행 index"],["sensor-column-noise-plot","column_temporal_rms_e","열 index"]])plot(id,diagnostics.map(({mode,data},i)=>({label:modeName(mode),color:colors[i],points:(data[key]||[]).map((value,index)=>[index,value])})),{xlabel:label,ylabel:"행·열 평균 잔차의 시간 RMS e⁻"});
    plot("sensor-readout-nps-plot",diagnostics.map(({mode,data},i)=>({label:modeName(mode),color:colors[i],points:data.noise_power_spectrum?.status==="ok"?(data.noise_power_spectrum.frequency_cycles_per_pixel||[]).map((value,index)=>[value,data.noise_power_spectrum.radial_power?.[index]]):[]})),{xlabel:"Gr · cycles/native pixel · Nyquist 0.25",ylabel:"Gr 시간 NPS · e⁻² / (cycles/native pixel)²",xmax:.25});
    plot("sensor-ptc-plot",diagnostics.map(({mode,data},i)=>({label:modeName(mode),color:colors[i],points:["ok","partial"].includes(data.ptc?.status)?(data.ptc.points||[]).filter(point=>point.site==="Gr"&&(!point.status||point.status==="ok")).map(point=>[point.mean_electrons,point.temporal_variance_e2]).sort((a,b)=>a[0]-b[0]):[]})),{xlabel:"Gr 평균 신호 e⁻",ylabel:"Gr 시간 분산 e⁻²"});
  }
  function flatten(value,prefix=""){
    if(value===null||value===undefined)return[];
    if(Array.isArray(value))return value.map((v,i)=>[`${prefix}${prefix?" · ":""}${i+1}`,typeof v==="object"?JSON.stringify(v):String(v)]);
    if(typeof value!=="object")return[[prefix,String(value)]];
    return Object.entries(value).flatMap(([key,v])=>v&&typeof v==="object"&&!Array.isArray(v)?flatten(v,prefix?`${prefix}.${key}`:key):[[prefix?`${prefix}.${key}`:key,Array.isArray(v)?v.map(x=>typeof x==="object"?JSON.stringify(x):String(x)).join(" · "):String(v)]]);
  }
  function renderDetails(){
    const assumptions=state.case?.assumptions||state.summary?.assumptions||[],spec=specOf(state.job)||{};
    const row=(key,value)=>`<div class="sensor-assumption-row"><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`;
    let html=Array.isArray(assumptions)?`<ul>${assumptions.map(a=>`<li>${esc(a)}</li>`).join("")}</ul>`:flatten(assumptions).map(([k,v])=>row(k,v)).join("");
    if(spec.recipe==="binning")html+=row("합산 노드 용량",`${fmt(spec.charge_binning?.capacity_multiplier)} × 기준 full-well`)+row("합산 후 변환 gain",`${fmt(spec.charge_binning?.conversion_gain_multiplier)} × 기준`)+row("합산 후 read noise",`${fmt(spec.charge_binning?.read_noise_multiplier)} × 기준`);
    for(const mode of modes().filter(m=>[state.a,state.b].includes(m.id))){const meta=mode.metadata?.metadata||mode.metadata||{},path=meta.calibration_path||{};
      html+=`<h4>${esc(modeName(mode))}</h4>`+row("원본 취득 정보",meta.raw_reused?"보존 RAW에서 파생한 결과":"고정 평균 응답으로 새 readout 계산")+row("기여 native 픽셀 수",mode.contribution_count??meta.contribution_count??"—")+row("변환",`${fmt(meta.dn_per_e??path.dn_per_e,6)} DN/e⁻`)+row("CFA · 반복 노출",`${meta.cfa||"RGGB"} · ${fmt((meta.exposure_s??.004)*1000)} ms`);
      if(readoutRecipes.includes(spec.recipe))html+=row("Analog / Digital gain",`${fmt(meta.analog_gain)} / ${fmt(meta.digital_gain)}`)+row("ADC bits",meta.bit_depth??"—")+row("Conversion gain",`${fmt(finite(meta.conversion_gain_v_per_e)?meta.conversion_gain_v_per_e*1e6:null,3)} µV/e⁻`)+row("증폭 후 잡음 전압",`${fmt(finite(meta.post_gain_noise_v)?meta.post_gain_noise_v*1e6:null,3)} µV RMS`)+row("같은 후단 잡음 · unity analog 환산",`${fmt(meta.post_gain_noise_e_at_unity_analog,3)} e⁻ RMS · 선언 가정`);
      if(meta.shot_noise_shared)html+=row("Shot noise","같은 전하를 공유 · 경로 잡음과 구분");
      if(meta.fixed_pattern)html+=row("고정 패턴 가정",`PRNU σ ${fmt(meta.fixed_pattern.prnu_sigma)} · DSNU ${fmt(meta.fixed_pattern.dsnu_e_per_s)} e⁻/s · seed ${meta.fixed_pattern.seed}`);
      if(meta.output_kind)html+=row("출력 의미",meta.output_kind);
    }
    html+=`<details class="sensor-raw-details"><summary>저장된 readout 모델 전체</summary><pre>${esc(JSON.stringify(modes().filter(m=>[state.a,state.b].includes(m.id)).map(m=>({id:m.id,metadata:m.metadata})),null,2))}</pre></details>`;$("sensor-assumptions").innerHTML=html;
    const refs=state.case?.source_refs||state.summary?.source_refs||{},source=refs.source?.run_id||refs.run_id||refs.source_run_id||spec.source?.run_id;
    $("sensor-sources").innerHTML=`<h4>보존 원본 연결</h4>`+row("Reference 실행",source||"—")+row("Source manifest SHA-256",refs.source?.manifest_sha256||spec.source?.manifest_sha256||"—")+row("평균 응답 출처",refs.response?.kind||"—")+row("이 case에서 재사용한 native RAW",Array.isArray(refs.native_raw)?`${refs.native_raw.length}개`:"—")+`<details class="sensor-raw-details"><summary>원본 경로와 파일 해시</summary><pre>${esc(JSON.stringify(refs,null,2))}</pre></details>`;
    if(typeof source==="string"&&/^run-[a-f0-9]+$/.test(source))$("sensor-sources").innerHTML+=`<p><a href="/reference-workshop.html?run=${encodeURIComponent(source)}&field=${encodeURIComponent(state.field)}" target="_blank" rel="noopener">같은 시야의 원본 Reference 열기 ↗</a></p>`;
    const pixel=state.summary?.pixel_study_id;if(typeof pixel==="string")$("sensor-pixel-link").href=`/reference-workshop.html?pixel_study=${encodeURIComponent(pixel)}&field=${encodeURIComponent(state.field)}`;
  }
  function renderCase(){
    if(!state.case)return;const ids=modes().map(m=>m.id);if(!ids.includes(state.a))state.a=ids[0];if(!ids.includes(state.b))state.b=ids.at(-1);
    const pair=[selected(state.a),selected(state.b)].filter(Boolean),measuredAlias=pair.length>0&&pair.every(mode=>mode.files?.noise&&mode.files?.measured_noise&&(mode.files.noise===mode.files.measured_noise||readoutRecipes.includes(specOf(state.job)?.recipe)));
    // New readout jobs retain noise.png as an identical compatibility copy of measured-noise.png.
    // Existing binning/DCG noise remains a model map, a different stage from measured noise.
    if(measuredAlias&&state.stage==="noise")state.stage="measured_noise";
    const stages=Object.keys(stageLabels).filter(stage=>!(measuredAlias&&stage==="noise")&&pair.some(mode=>mode?.files?.[stage]));if(!stages.includes(state.stage))stages.push(state.stage);options("sensor-stage",stages,state.stage,stageName);
    options("sensor-mode-a",ids,state.a,id=>modeName(selected(id)));options("sensor-mode-b",ids,state.b,id=>modeName(selected(id)));
    setText("sensor-binding",`${state.job.id} / ${state.case.id} · ${state.field} · ${chartLabels[state.chart]||state.chart} · 광량 ×${state.light} · ${specOf(state.job)?.exposure_ms??4} ms / ${specOf(state.job)?.frames??8}회 · probe FOV ${fmt(state.case.geometry?.horizontal_fov_deg)}° · 저장 원본에 연결된 결과`);
    renderImages();renderFlow();renderMetrics();renderPlots();renderReadoutDiagnostics();renderDetails();updateButtons();updateURL();
  }
  function selectControls(){
    const all=caseList(),fields=[...new Set(all.map(c=>c.field_id))];if(!fields.includes(state.field))state.field=fields[0];
    options("sensor-field",fields,state.field,id=>id==="center"?"중심":id==="outer-ne"?"주변 · 우상":id);
    const charts=[...new Set(all.filter(c=>c.field_id===state.field).map(c=>c.chart))];if(!charts.includes(state.chart))state.chart=charts[0];options("sensor-chart",charts,state.chart,id=>chartLabels[id]||id);
    const lights=[...new Set(all.filter(c=>c.field_id===state.field&&c.chart===state.chart).map(c=>c.light_scale))].sort((a,b)=>a-b);if(!lights.includes(Number(state.light)))state.light=lights[0];else state.light=Number(state.light);options("sensor-light",lights,state.light,x=>`${x} ×`);
    return all.find(c=>c.field_id===state.field&&c.chart===state.chart&&c.light_scale===state.light)||null;
  }
  async function openCase(id){
    const token=++state.token,job=state.job?.id;state.case=null;state.loading=true;clearResult();updateButtons();
    if(!id){state.loading=false;updateButtons();status("이 실행에는 표시할 완료 case가 없습니다.",true);return;}
    try{const key=`${job}/${id}`,detail=state.cache.get(key)||await api(`${ROOT}/${encodeURIComponent(job)}/cases/${encodeURIComponent(id)}`);if(token!==state.token||job!==state.job?.id)return;if(detail.id!==id)throw Error("응답의 case 식별자가 요청과 다릅니다.");state.cache.set(key,detail);state.case=detail;state.loading=false;state.field=detail.field_id;state.chart=detail.chart;state.light=detail.light_scale;selectControls();renderCase();status(`${statusLabels[state.job.status]||state.job.status} · ${state.job.id} · ${caseList().length}개 저장 case. 선택은 보존 결과를 탐색하며 재계산하지 않습니다.`);}
    catch(error){if(token!==state.token)return;state.loading=false;updateButtons();status(error.message,true);clearImages("선택한 결과를 읽지 못했습니다.");}
  }
  async function chooseCase(){const current=selectControls();await openCase(current?.id);}
  function renderJob(){
    const progress=$("sensor-progress");progress.classList.toggle("hidden",!busy(state.job));const value=state.job?.progress;const number=finite(value)?value:value?.fraction;if(finite(number))progress.value=number;else progress.removeAttribute("value");
    link("sensor-report",state.summary?.report_url||null);link("sensor-bundle",state.summary?.bundle_url||null);updateButtons();
  }
  async function openStudy(id,caseId=null){
    const token=++state.token;clearTimeout(state.poll);state.case=null;state.summary=null;state.loading=true;clearResult();link("sensor-report",null);link("sensor-bundle",null);updateButtons();status("저장된 취득·모드 결과를 확인합니다.");
    try{const job=await api(`${ROOT}/${encodeURIComponent(id)}`);if(token!==state.token)return;if(job.id!==id)throw Error("응답의 실행 식별자가 요청과 다릅니다.");state.job=job;state.summary=summaryOf(job);state.loading=false;hydrateSettings(specOf(job));state.recipe=state.summary?.recipe||specOf(job)?.recipe||state.recipe;if(!state.stagePinned){if(state.recipe==="noise_structure")state.stage="temporal_residual";else if(state.stage==="temporal_residual")state.stage="display";}if(!caseId&&state.recipe==="dual_gain"&&caseList().some(c=>c.chart==="dynamic_range")){state.chart="dynamic_range";state.light=1;}const item=state.studies.find(s=>s.id===id);if(item)item.status=job.status;options("sensor-study-select",state.studies.map(s=>s.id),id,key=>{const record=state.studies.find(s=>s.id===key);return `${record.name||key} · ${statusLabels[record.status]||record.status} · ${record.recipe||""}`;});renderJob();updateURL();
      if(busy(job)){status(`${statusLabels[job.status]} · ${id} · ${typeof (job.progress?.message||job.progress?.stage)==="string"?(job.progress.message||job.progress.stage):"프레임·모드 결과를 저장하고 있습니다."}`);state.poll=setTimeout(()=>openStudy(id,caseId),1600);return;}
      if(job.status!=="succeeded"){status(`${statusLabels[job.status]||job.status} · ${typeof job.error==="string"?job.error:JSON.stringify(job.error||job.result?.error||"완료된 결과가 없습니다.")}`,true);return;}
      const requested=caseList().find(c=>c.id===caseId);if(requested){state.field=requested.field_id;state.chart=requested.chart;state.light=requested.light_scale;}const selectedCase=selectControls();await openCase(selectedCase?.id);
    }catch(error){if(token!==state.token)return;state.loading=false;updateButtons();status(error.message,true);clearImages("실행 결과를 읽지 못했습니다.");}
  }
  async function refreshStudies(openId=null){
    const token=++state.listToken;try{const result=await api(ROOT);if(token!==state.listToken)return;state.studies=result.studies||[];state.template=result.template||null;
      if(state.template&&!state.job)hydrateSettings(state.template);
      options("sensor-study-select",state.studies.map(s=>s.id),openId||state.job?.id,id=>{const s=state.studies.find(x=>x.id===id);return `${s.name||id} · ${statusLabels[s.status]||s.status} · ${s.recipe||s.specification?.recipe||""}`;});updateButtons();
      const id=openId||(!state.job&&(state.studies.find(s=>s.status==="succeeded"&&s.recipe===state.recipe)||state.studies.find(s=>s.status==="succeeded")||state.studies[0])?.id);if(id){const query=new URLSearchParams(location.search);await openStudy(id,query.get("sensor_study")===id?query.get("sensor_case"):null);}else if(!state.job)status("보존된 Sensor study가 없습니다. 고정 Reference에서 recipe를 제출하세요.");
    }catch(error){status(error.message,true);}
  }
  async function submit(replay=false){
    if(state.submitting||busy(state.job))return;const payload=replay?clone(specOf(state.job)):submissionScope().specification;if(!payload||!payload.schema)return;payload.recipe=replay?payload.recipe:state.recipe;payload.reprocess_of=replay&&payload.recipe==="dual_gain"?state.job.id:null;
    if(replay&&payload.recipe==="dual_gain")payload.name=(payload.name+" · RAW 재합성").slice(0,160);
    if(!replay){payload.name=`Reference Sensor Readout · ${recipeLabels[payload.recipe]||payload.recipe}`;if(payload.recipe==="dual_gain"&&!payload.charts.includes("dynamic_range"))payload.charts.push("dynamic_range");
      if(readoutRecipes.includes(payload.recipe)){payload.readout_lab=readReadoutSettings();}
      for(const [key,id]of [["capacity_multiplier","sensor-capacity"],["conversion_gain_multiplier","sensor-conversion-gain"],["read_noise_multiplier","sensor-read-noise"]]){if(!$(id).reportValidity())return;payload.charge_binning[key]=Number($(id).value);}}
    if((!replay||payload.recipe==="dual_gain")&&payload.isp){if(!$("sensor-display-white").reportValidity())return;payload.isp.display_white_level=Number($("sensor-display-white").value);}
    if(payload.recipe==="dual_gain"&&payload.dcg_merge)for(const [key,id]of [["saturation_margin_dn","sensor-dcg-margin"],["read_noise_guard_sigma","sensor-dcg-read-guard"],["gain_path_agreement_sigma","sensor-dcg-agreement"]]){if(!$(id).reportValidity())return;payload.dcg_merge[key]=Number($(id).value);}
    state.submitting=true;updateButtons();status("명세를 고정하고 계산 작업을 제출합니다.");
    try{const job=await api(ROOT,payload);state.job=job;state.case=null;state.summary=null;clearResult("새 작업을 접수했습니다. 완료된 결과가 준비되면 표시합니다.");link("sensor-report",null);link("sensor-bundle",null);renderJob();updateURL();await refreshStudies(job.id);if(busy(state.job)&&state.job.id===job.id){clearTimeout(state.poll);state.poll=setTimeout(()=>openStudy(job.id),1600);}}catch(error){status(error.message,true);}finally{state.submitting=false;updateButtons();}
  }
  async function saveProfile(){
    if(!state.case)return;const job=state.job.id,caseId=state.case.id,token=state.token,payload={name:$("sensor-profile-name").value.trim()||"Sensor mode comparison",case_id:caseId,mode:state.b,baseline_mode:state.a,interpolation:state.interpolation};
    try{const result=await api(`${ROOT}/${encodeURIComponent(job)}/profiles`,payload);if(token!==state.token)return;link("sensor-profile-download",result.download_url||`${ROOT}/${encodeURIComponent(job)}/profiles/${encodeURIComponent(result.id)}`);status(`프로필 ${result.id} 저장 완료 · ${caseId} · 원본 RAW는 변경하지 않았습니다.`);}catch(error){status(error.message,true);}
  }
  function show(){state.visible=true;document.body.classList.remove("pixel-only");document.body.classList.add("sensor-only");$("sensor-mode-panel").classList.remove("hidden");$("sensor-lab-toggle").setAttribute("aria-expanded","true");if(!state.studies.length)refreshStudies();}
  function bind(){
    $("sensor-lab-toggle").onclick=show;$("sensor-study-refresh").onclick=()=>refreshStudies();$("sensor-study-select").onchange=()=>openStudy($("sensor-study-select").value);
    $("sensor-submit").onclick=()=>submit().catch(error=>status(error.message,true));$("sensor-replay").onclick=()=>submit(true).catch(error=>status(error.message,true));$("sensor-profile-save").onclick=saveProfile;
    for(const id of [...readoutControls.map(pair=>pair[1]),"sensor-adc-bits"])$(id).oninput=()=>{setText("sensor-readout-draft","편집값은 아직 계산하지 않았습니다. 아래 A/B·수치는 보존 결과이며 새 설정은 제출 후 반영됩니다.");renderCostPreview();};
    $("sensor-cancel").onclick=async()=>{if(!busy(state.job))return;try{await api(`/api/v1/reference-workshop/runs/${encodeURIComponent(state.job.id)}/cancel`,{});status("취소를 요청했습니다. 완료된 원본과 작업 상태를 보존합니다.");await openStudy(state.job.id);}catch(error){status(error.message,true);}};
    document.querySelectorAll("[data-sensor-recipe]").forEach(button=>button.onclick=()=>{state.recipe=button.dataset.sensorRecipe;if(state.recipe==="dual_gain")$("sensor-display-white").value=16;else if(state.recipe==="binning")$("sensor-display-white").value=1;updateButtons();const prepared=state.studies.find(s=>s.status==="succeeded"&&s.recipe===state.recipe)||state.studies.find(s=>busy(s)&&s.recipe===state.recipe);if(prepared)openStudy(prepared.id);else{state.token++;clearTimeout(state.poll);state.case=null;state.job=null;state.summary=null;state.loading=false;clearResult("선택 recipe는 아직 계산되지 않았습니다. 새 계산 제출이 필요합니다.");link("sensor-report",null);link("sensor-bundle",null);updateButtons();status("선택 recipe의 준비 결과가 없습니다. 계산을 제출하면 진행 상태와 실제 결과를 표시합니다.");}});
    for(const key of ["field","chart","light"])$("sensor-"+key).onchange=()=>{state[key]=$("sensor-"+key).value;chooseCase();};
    for(const side of ["a","b"])$("sensor-mode-"+side).onchange=()=>{state[side]=$("sensor-mode-"+side).value;$("sensor-profile-download").classList.add("hidden");renderCase();};
    $("sensor-invalid-overlay").onchange=()=>{state.validOverlay=$("sensor-invalid-overlay").checked;renderImages();updateURL();};
    for(const [id,key]of [["sensor-stage","stage"],["sensor-view-size","viewSize"],["sensor-interpolation","interpolation"],["sensor-zoom","zoom"]])$(id).onchange=()=>{state[key]=key==="zoom"?Number($(id).value):$(id).value;if(key==="stage")state.stagePinned=true;renderImages();updateURL();};
    let syncing=false;for(const side of ["a","b"]){const viewport=$("sensor-viewport-"+side),other=$("sensor-viewport-"+(side==="a"?"b":"a"));viewport.onscroll=()=>{if(syncing)return;syncing=true;other.scrollLeft=viewport.scrollLeft;other.scrollTop=viewport.scrollTop;syncing=false;};let drag=null;viewport.onpointerdown=event=>{if(event.button!==0)return;drag={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture?.(event.pointerId);};viewport.onpointermove=event=>{if(!drag)return;viewport.scrollLeft=drag.left+drag.x-event.clientX;viewport.scrollTop=drag.top+drag.y-event.clientY;};viewport.onpointerup=viewport.onpointercancel=()=>{drag=null;};}
  }
  async function init(){
    if(!$("sensor-mode-panel"))return;bind();const query=new URLSearchParams(location.search);state.field=query.get("field")||"center";if(Object.hasOwn(stageLabels,query.get("sensor_stage"))){state.stage=query.get("sensor_stage");state.stagePinned=true;}state.a=query.get("sensor_a")||"native";state.b=query.get("sensor_b")||"charge_binning";if(["nearest","bilinear"].includes(query.get("sensor_interpolation")))state.interpolation=query.get("sensor_interpolation");$("sensor-interpolation").value=state.interpolation;state.validOverlay=query.get("sensor_invalid")!=="off";$("sensor-invalid-overlay").checked=state.validOverlay;
    if(query.has("sensor_study")){state.visible=true;document.body.classList.add("sensor-only");$("sensor-mode-panel").classList.remove("hidden");$("sensor-lab-toggle").setAttribute("aria-expanded","true");await refreshStudies(query.get("sensor_study"));}
  }
  init();
})();

"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const API = "/api/v1/virtual-lab";
  const state = {token: null, template: null, catalog: {}, jobs: [], current: null, baseline: null,
    active: null, profile: "balanced", stage: "isp", preview: "primary", zoom: 1, pan: [0, 0], poll: null, toast: null, lastLens: null, conventionalOptics: null, stopped: null};
  const labels = {resolution: "해상력 차트", color: "24색 차트", uniform: "균일장", tone: "명암 계단", natural: "합성 도로 장면", grid: "각도 격자"};
  const profileLabels = {balanced: "Balanced", low_noise: "Low noise", detail: "High detail"};
  const profiles = {balanced: {denoise: .2, sharpen: .3}, low_noise: {denoise: .85, sharpen: 0}, detail: {denoise: 0, sharpen: 1.2}};
  const stages = {scene: ["scene", "scene_rgb", "scene_preview"], optical: ["optical", "optical_image", "oi", "optics"],
    raw: ["raw", "raw_preview", "raw_dn"], linear: ["linear", "linear_rgb", "sensor_rgb"], isp: ["isp", "output", "processed", "isp_rgb"],cra:["cra"],angular_gain:["angular_gain"]};
  const statusLabels = {queued: "대기 중", running: "계산 중", succeeded: "완료", failed: "실패", cancelled: "취소됨", cancelling: "취소 중"};
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const fmt = (value, digits = 2) => finite(value) ? value.toLocaleString("ko-KR", {maximumFractionDigits: digits, minimumFractionDigits: digits}) : "—";
  const result = job => job?.result?.summary || job?.result || null;
  const config = job => result(job)?.configuration || job?.specification || {};
  const completed = job => job?.status === "succeeded" && Boolean(result(job));
  const isBusy = job => ["queued", "running", "cancelling"].includes(job?.status);
  const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const fieldConfig = spec => ({mode:spec.field_model?.mode ?? "center_local",horizontal_fov_deg:spec.field_model?.horizontal_fov_deg ?? null});
  const angularConfig = spec => ({mode:spec.angular_response?.mode ?? "off",exponents_r_gr_gb_b:spec.angular_response?.exponents_r_gr_gb_b ?? [2,2,2,2]});

  const wideDefaults = {projection:"rectilinear",native_shape_row_col:[1080,1920],exit_pupil_distance_mm:6,radial_blur_um:0,tangential_blur_um:0,transmission:1,relative_illumination_power:0,sampling:"auto",tile_size:256,max_working_bytes:4294967296,min_free_disk_bytes:10737418240};
  const wideConfig = spec => Object.fromEntries(Object.entries(wideDefaults).map(([key,value])=>[key,spec.wide_angle?.[key] ?? value]));
  function readWide(){return {projection:$("wide-projection").value,native_shape_row_col:[1080,1920],exit_pupil_distance_mm:Number($("wide-exit-pupil").value),radial_blur_um:Number($("wide-radial-blur").value),tangential_blur_um:Number($("wide-tangential-blur").value),transmission:Number($("wide-transmission").value),relative_illumination_power:Number($("wide-ri-power").value),sampling:$("wide-sampling").value==="auto"?"auto":Number($("wide-sampling").value),tile_size:Number($("wide-tile").value),max_working_bytes:Math.round(Number($("wide-memory").value)*2**30),min_free_disk_bytes:Math.round(Number($("wide-disk").value)*2**30)};}
  function applyWide(spec){const value=wideConfig(spec),fields={"wide-projection":"projection","wide-exit-pupil":"exit_pupil_distance_mm","wide-radial-blur":"radial_blur_um","wide-tangential-blur":"tangential_blur_um","wide-transmission":"transmission","wide-ri-power":"relative_illumination_power","wide-sampling":"sampling","wide-tile":"tile_size"};Object.entries(fields).forEach(([id,key])=>$(id).value=value[key]);$("wide-memory").value=value.max_working_bytes/2**30;$("wide-disk").value=value.min_free_disk_bytes/2**30;}

  async function api(path, body) {
    if (body !== undefined && !state.token) {
      const response = await fetch("/api/v1/session");
      if (!response.ok) throw Error("세션을 확인할 수 없습니다. 페이지를 새로고침하세요.");
      state.token = (await response.json()).token;
    }
    const response = await fetch(API + path, {method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : {"Content-Type": "application/json", "x-platform-token": state.token},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let value;
    try { value = await response.json(); } catch (_) { throw Error(`서버 응답을 읽을 수 없습니다 (${response.status}).`); }
    if (!response.ok) {
      const detail = value.detail ?? value.error ?? value.message ?? value;
      throw Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return value;
  }

  function notice(message, type = "ready") {
    $("status-message").textContent = message;
    $("status-bar").classList.toggle("error", type === "error");
    $("status-bar").classList.toggle("busy", type === "busy");
  }
  function toast(message) {
    clearTimeout(state.toast); $("toast").textContent = message; $("toast").classList.remove("hidden");
    state.toast = setTimeout(() => $("toast").classList.add("hidden"), 4000);
  }
  function optionName(collection, id) { return (state.catalog[collection] || []).find(item => item.id === id)?.label || id || "—"; }
  function sliderLabels() {
    for (const key of ["denoise", "sharpen", "gamma", "lsc"]) $(key + "-value").textContent = Number($(key).value).toFixed(2);
    $("pipe-isp").textContent = profileLabels[state.profile] || state.profile;
  }
  function setProfile(profile, applyValues = true) {
    state.profile = profile;
    document.querySelectorAll("[data-profile]").forEach(button => {
      const active = button.dataset.profile === profile;
      button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
    });
    if (applyValues && profiles[profile]) {
      $("denoise").value = profiles[profile].denoise; $("sharpen").value = profiles[profile].sharpen;
    }
    sliderLabels();
    updateButtons();
  }
  function readISP() {
    const ccm = $("ccm").value.trim().split(/\n|;/).map(row => row.trim().split(/[\s,]+/).map(Number));
    if (ccm.length !== 3 || ccm.some(row => row.length !== 3 || row.some(n => !Number.isFinite(n)))) throw Error("CCM은 숫자 3개씩, 3행으로 입력하세요.");
    return {profile: state.profile, denoise: Number($("denoise").value), sharpen: Number($("sharpen").value),
      black_offset_dn: Number($("black-offset").value), lsc_strength: Number($("lsc").value),
      tone_gamma: Number($("gamma").value), wb: ["wb-r", "wb-g", "wb-b"].map(id => Number($(id).value)), ccm};
  }
  function readSpec() {
    return {schema: "camera_virtual_lab_v1", name: $("run-name").value.trim() || "Camera Virtual Lab",
      lens_id: $("lens").value, sensor_id: $("sensor").value, scene: $("scene").value,
      test_suite: $("test-suite").value, illuminant: $("illuminant").value,
      luminance_cd_m2: Number($("luminance").value), exposure_ms: Number($("exposure").value), gain: Number($("gain").value),
      field_model:{mode:$("field-mode").value,horizontal_fov_deg:$("horizontal-fov").value.trim()===""?null:Number($("horizontal-fov").value)},
      angular_response:{mode:$("angular-mode").value,exponents_r_gr_gb_b:["angular-r","angular-gr","angular-gb","angular-b"].map(id=>Number($(id).value))},
      frames: state.template?.frames || 8, seed: state.template?.seed ?? 42,
      ...($("lens").value==="wide_parametric"?{wide_angle:readWide()}:{}),
      isp: readISP(), optics: {f_number: Number($("f-number").value), defocus_um: Number($("defocus").value)}};
  }
  function applySpec(spec) {
    const fields = {"run-name": "name", lens: "lens_id", sensor: "sensor_id", scene: "scene", "test-suite": "test_suite",
      illuminant: "illuminant", luminance: "luminance_cd_m2", exposure: "exposure_ms", gain: "gain"};
    Object.entries(fields).forEach(([id, key]) => { if (spec[key] !== undefined) $(id).value = spec[key]; });
    const isp = spec.isp || {};
    $("denoise").value = isp.denoise ?? .2; $("sharpen").value = isp.sharpen ?? .3; $("gamma").value = isp.tone_gamma ?? 2.2;
    $("black-offset").value = isp.black_offset_dn ?? 0; $("lsc").value = isp.lsc_strength ?? 0;
    ["wb-r", "wb-g", "wb-b"].forEach((id, i) => $(id).value = isp.wb?.[i] ?? 1);
    $("ccm").value = (isp.ccm || [[1, 0, 0], [0, 1, 0], [0, 0, 1]]).map(row => row.join(", ")).join("\n");
    $("f-number").value = spec.optics?.f_number ?? 4; $("defocus").value = spec.optics?.defocus_um ?? 0;
    const field=fieldConfig(spec),angular=angularConfig(spec);$("field-mode").value=field.mode;$("horizontal-fov").value=field.horizontal_fov_deg ?? "";$("angular-mode").value=angular.mode;
    ["angular-r","angular-gr","angular-gb","angular-b"].forEach((id,i)=>$(id).value=angular.exponents_r_gr_gb_b[i]);
    applyWide(spec);state.lastLens=spec.lens_id;
    setProfile(isp.profile || "balanced", false); updateConfiguration(false); updateButtons();
  }
  function updateConfiguration(changedLens = false) {
    const lens = (state.catalog.lenses || []).find(item => item.id === $("lens").value);
    const sensor = (state.catalog.sensors || []).find(item => item.id === $("sensor").value);
    $("lens-description").textContent = lens?.description || lens?.model_kind || "";
    $("sensor-description").textContent = sensor?.description || "";
    const wide=lens?.id==="wide_parametric";
    if(changedLens && wide && state.lastLens!=="wide_parametric"){
      state.conventionalOptics={fNumber:$("f-number").value,defocus:$("defocus").value,fov:$("horizontal-fov").value,field:$("field-mode").value,angular:$("angular-mode").value,scene:$("scene").value};
      $("f-number").value=2.8;$("defocus").value=0;$("horizontal-fov").value=120;$("field-mode").value="full_field";$("angular-mode").value="physical_pupil";
    }else if(changedLens && !wide && state.lastLens==="wide_parametric"){
      const saved=state.conventionalOptics;
      $("f-number").value=saved?.fNumber ?? state.template?.optics?.f_number ?? 4;$("defocus").value=saved?.defocus ?? 0;$("horizontal-fov").value=saved?.fov ?? "";$("field-mode").value=saved?.field ?? "full_field";$("angular-mode").value=saved?.angular ?? "physical_relative";
      if($("scene").value==="grid")$("scene").value=saved?.scene || "resolution";
    }
    state.lastLens=lens?.id;
    $("wide-controls").classList.toggle("hidden",!wide);
    $("wide-controls").querySelectorAll("input,select").forEach(input=>input.disabled=!wide);
    $("field-mode").disabled=wide;if(wide)$("field-mode").value="full_field";
    $("horizontal-fov").max=wide?120:40;$("horizontal-fov").required=wide;$("fov-range").textContent=wide?"° · 요청 범위 2…120":"° · 요청 범위 2…40";
    $("scene").querySelector('option[value="grid"]').disabled=!wide;
    const controls = lens?.supports || lens?.controls || lens?.capabilities || {};
    const enabled = key => Array.isArray(controls) ? controls.includes(key) : Boolean(controls[key]);
    const adjustable = wide || lens?.kind === "analytic" || lens?.model_kind === "analytic" || lens?.id?.includes("analytic");
    $("f-number").disabled = !(enabled("f_number") || enabled("aperture") || adjustable);
    $("defocus").disabled = !(enabled("defocus_um") || enabled("defocus") || adjustable);
    if ($("f-number").disabled) $("f-number").value = Number(Number(lens?.model?.effective_f_number ?? lens?.f_number ?? state.template?.optics?.f_number ?? 4).toFixed(6));
    if(wide)$("defocus").disabled=true;
    if ($("defocus").disabled) $("defocus").value = 0;
    $("optics-note").textContent = wide ? "투영·회절·선언한 방사/접선 blur 모델입니다. 기존 DB 처방을 120°로 확대한 모델이 아닙니다." : $("f-number").disabled ? "저장 광학 응답의 조리개·초점 조건을 사용합니다." : "광학 모델을 재계산해 조리개·초점 영향을 반영합니다.";
    $("pipe-scene").textContent = labels[$("scene").value] || $("scene").value;
    $("pipe-lens").textContent = lens?.label || "광학 모델"; $("pipe-sensor").textContent = sensor?.label || "센서 모델";
    const localOnly=$("field-mode").value==="center_local",previousAngular=$("angular-mode").value;
    if(localOnly)$("angular-mode").value="off";
    const pupilUnavailable=$("lens").value==="analytic";
    const pupilOption=$("angular-mode").querySelector('option[value="physical_pupil"]');
    if(pupilOption)pupilOption.disabled=pupilUnavailable;
    if(!localOnly && pupilUnavailable && $("angular-mode").value==="physical_pupil")$("angular-mode").value="physical_relative";
    $("angular-mode").disabled=localOnly;
    const angularMode=$("angular-mode").value;
    $("angular-exponents").classList.toggle("hidden",angularMode!=="cosine_power");
    $("angular-note").textContent=localOnly?(previousAngular!=="off"?"중심 국소 PSF 선택으로 입사각 응답을 OFF로 변경했습니다. 전체 관측 시야 모드에서 사용할 수 있습니다.":"중심 국소 PSF에서는 입사각 응답이 OFF입니다. 전체 관측 시야 모드에서 활성화할 수 있습니다."):angularMode==="physical_pupil"?`${wide?"선언한 출사동공 원판":"저장 동공 방향"}의 가중평균 상대 응답을 정면 QE에 한 번 적용합니다. 수신측 위치별 평균이며 광선 도달 위치·각도를 함께 반영한 PSF 재계산은 아닙니다.`:angularMode==="physical_relative"?`${pupilUnavailable?(previousAngular==="physical_pupil"?"일반 광학에는 동공 방향 묶음이 없어 Chief-ray 모드로 변경했습니다. ":"일반 광학은 동공 방향 평균을 지원하지 않습니다. "):""}저장 정면 QE × 층 모델 상대 응답. Chief ray 기준이며 실측 QE 정합 모델은 아닙니다.`:angularMode==="cosine_power"?"채널별 cosⁿ(입사각)을 선언하는 근사입니다. 센서 제품의 측정 응답은 아닙니다.":"픽셀의 각도 응답을 적용하지 않습니다. CRA는 별도로 표시합니다.";
    updateButtons();
  }
  function sameAcquisition() {
    if (!completed(state.current)) return false;
    try {
      const source = config(state.current), next = readSpec();
      return ["lens_id", "sensor_id", "scene", "test_suite", "illuminant", "luminance_cd_m2", "exposure_ms", "gain"].every(key => source[key] === next[key]) && ["f_number","defocus_um"].every(key=>Math.abs(Number(source.optics?.[key])-Number(next.optics[key]))<1e-9) && JSON.stringify(fieldConfig(source))===JSON.stringify(fieldConfig(next)) && JSON.stringify(angularConfig(source))===JSON.stringify(angularConfig(next)) && (next.lens_id!=="wide_parametric" || JSON.stringify(wideConfig(source))===JSON.stringify(wideConfig(next)));
    } catch (_) { return false; }
  }
  function updateButtons() {
    const busy = Boolean(state.active);
    $("run-button").disabled = !state.template || busy;
    $("replay").disabled = busy || !sameAcquisition();
    $("replay").title = !sameAcquisition() && completed(state.current) ? "카메라·장면·취득 설정이 달라졌습니다. 새 촬영을 실행하거나 저장 결과의 구성을 불러오세요." : "현재 결과의 원본 RAW를 보존해 ISP만 다시 계산합니다.";
    $("pin-baseline").disabled = !completed(state.current);
    $("cancel-run").classList.toggle("hidden", !busy);
    $("resume-run").classList.toggle("hidden",busy || !state.stopped || !["failed","cancelled"].includes(state.stopped.status) || config(state.stopped).lens_id!=="wide_parametric" || Boolean(result(state.stopped)?.source_run_id));
    const pending=$("configuration-notice");let changed=false;
    if(!busy && completed(state.current)){
      if(!sameAcquisition()){changed=true;pending.textContent="설정 변경됨 · 표시 결과는 이전 계산입니다. 새 촬영이 필요합니다.";}
      else{
        try{const currentISP=readISP(),savedISP=config(state.current).isp || {};
          changed=Object.keys(currentISP).some(key=>JSON.stringify(currentISP[key])!==JSON.stringify(savedISP[key] ?? (["black_offset_dn","lsc_strength"].includes(key)?0:undefined)));
          if(changed)pending.textContent="ISP 설정 변경됨 · 표시 결과는 이전 계산입니다. 같은 RAW로 재처리하거나 새로 촬영하세요.";
        }catch(_){changed=true;pending.textContent="설정 입력을 확인하세요. 표시 결과는 이전 계산입니다.";}
      }
    }
    pending.classList.toggle("hidden",!changed);
    updateFieldBudget();
  }
  function updateFieldBudget(){
    if($("lens").value==="wide_parametric"){
      $("field-budget").textContent=`Native 1920 × 1080 · 2.07 MP 고정. HFOV에 맞춰 초점거리를 계산합니다. ${$("wide-memory").value} GiB 작업 메모리 / ${$("wide-disk").value} GiB 여유 디스크 정책; 부족하면 중단하며 자동 축소하지 않습니다.`;$("field-budget").classList.remove("budget-exceeded");return;
    }
    const lenses=state.catalog.lenses || [],sensors=state.catalog.sensors || [];
    const baselineLens=lenses.find(lens=>lens.id==="L1") || lenses.find(lens=>lens.model?.effective_focal_length_mm);
    const lens=$("lens").value==="analytic"?baselineLens:lenses.find(item=>item.id===$("lens").value);
    const baselineSensor=sensors.find(sensor=>sensor.id==="S1") || sensors[0],sensor=sensors.find(item=>item.id===$("sensor").value);
    const focal=lens?.model?.effective_focal_length_mm,pitch=sensor?.pixel_pitch_um;
    let text="모델 지원 범위와 400만 native 픽셀 소프트웨어 예산 내에서 실행합니다. 초과 시 실패하며 해상도를 자동으로 낮추지 않습니다.",over=false;
    if(finite(focal)&&finite(pitch)&&focal>0&&pitch>0){
      const declared=$("horizontal-fov").value.trim(),defaultWidth=640*(baselineSensor?.pixel_pitch_um || 0)/1000/(baselineLens?.model?.effective_focal_length_mm || 1);
      const width=declared?2*Math.tan(Number(declared)*Math.PI/360):defaultWidth;
      if(finite(width)&&width>0){const cols=2*Math.ceil(width*focal*1000/pitch/2),rows=2*Math.ceil(cols*9/16/2),pixels=cols*rows;over=pixels>4000000;
        text=`예상 native ${cols.toLocaleString()} × ${rows.toLocaleString()} · ${fmt(pixels/1000000,2)} MP. 400만 픽셀 소프트웨어 예산${over?" 초과":" 이내"}; 초과 시 실패하며 자동 축소하지 않습니다. 렌즈·픽셀 모델의 시야 지원 범위도 적용됩니다.`;
      }
    }
    $("field-budget").textContent=text;$("field-budget").classList.toggle("budget-exceeded",over);
  }
  function fileURL(job, path) {
    if (!path) return null;
    if (typeof path !== "string") path = path.path || path.url || path.preview;
    if (!path) return null;
    if (path.startsWith("/")) return path;
    const base = result(job)?.files_base || `${API}/runs/${encodeURIComponent(job.id)}/files/`;
    return base + path.split("/").map(encodeURIComponent).join("/");
  }
  function imagePath(job) {
    const images = result(job)?.images || {};
    if(["cra","angular_gain"].includes(state.stage))return images[state.stage] || metrics(job).field_response?.maps?.[state.stage] || null;
    if (state.preview !== "primary") {
      if(result(job)?.image_sets?.[state.preview]?.[state.stage])return result(job).image_sets[state.preview][state.stage];
      if (state.preview === config(job).scene) {
        for (const key of stages[state.stage]) if (images[key]) return images[key];
      }
      if (state.stage === "isp" && images[state.preview]) return images[state.preview];
      return images[`${state.preview}_${state.stage}`] || images.by_scene?.[state.preview]?.[state.stage] || null;
    }
    for (const key of stages[state.stage]) if (images[key]) return images[key];
    return null;
  }
  function updatePreview() {
    const images=result(state.current)?.images || {};
    for (const option of $("preview-scene").options) option.disabled = option.value !== "primary" && !images[option.value];
    if(state.preview!=="primary" && !images[state.preview]){state.preview="primary";$("preview-scene").value="primary";}
    document.querySelectorAll("[data-stage]").forEach(button=>{
      const stage=button.dataset.stage;
      button.disabled=["cra","angular_gain"].includes(stage)?!images[stage] && !metrics(state.current).field_response?.maps?.[stage]:state.preview!=="primary" && state.preview!==config(state.current).scene && stage!=="isp" && !result(state.current)?.image_sets?.[state.preview]?.[stage] && !images[`${state.preview}_${stage}`] && !images.by_scene?.[state.preview]?.[stage];
      if(button.disabled && stage===state.stage)state.stage="isp";
    });
    document.querySelectorAll("[data-stage]").forEach(button=>{button.classList.toggle("active",button.dataset.stage===state.stage);button.setAttribute("aria-selected",String(button.dataset.stage===state.stage));});
    $("preview-note").textContent=state.preview==="primary"?"같은 실행에 보존된 결과를 바로 확인합니다.":"저장된 시험 결과 · 재촬영하지 않습니다.";
    updateMapLegend();
  }
  function updateMapLegend(){const target=$("field-map-legend"),pupil=(metrics(state.current).field_response?.angular_mode ?? angularConfig(config(state.current)).mode)==="physical_pupil";target.classList.toggle("hidden",!["cra","angular_gain"].includes(state.stage));target.textContent=state.stage==="cra"?`CRA · 고정 표시 범위 0–60° · 550 nm chief-ray 진단${pupil?" · 픽셀 응답은 별도의 동공 방향 평균":" · 설정한 관측 시야 전체"}`: `상대 각도 응답${pupil?" · 동공 방향 가중평균":""} · 고정 표시 범위 0–1.5× · 550 nm R/Gr/Gb/B 평균 · 4채널 원본 보존`;}
  function renderImage(prefix, job) {
    const data = result(job), spec = config(job), img = $(prefix + "-image"), empty = $(prefix + "-empty");
    const path = imagePath(job);
    $(prefix + "-name").textContent = spec.name || (prefix === "baseline" ? "기준안" : "현재 설계안");
    $(prefix + "-meta").textContent = data ? `${optionName("lenses", spec.lens_id)} · ${optionName("sensors", spec.sensor_id)}` : (prefix === "baseline" ? "결과를 기준안으로 고정하세요" : "렌즈 · 센서 · ISP 통합 결과");
    img.hidden = !path; empty.classList.toggle("hidden", Boolean(path));
    if (path) { img.src = fileURL(job, path); img.alt = `${spec.name || "카메라"} · ${state.stage} 계산 영상`; }
    else if (data) { empty.querySelector("strong").textContent = "이 단계의 저장 영상이 없습니다"; empty.querySelector("p").textContent = "다른 계산 단계를 선택하세요."; }
    const geometry = data?.geometry || data?.provenance?.geometry || {};
    const native = data?.native_shape || geometry.native_shape || data?.sampling?.native_shape || data?.capture?.native_shape || data?.native_shape_row_col;
    const output = data?.output_shape || geometry.common_shape || geometry.output_shape || [360, 640];
    $(prefix + "-caption").textContent = data ? `${output[1]} × ${output[0]} 미리보기${native ? ` · native ${native[1]} × ${native[0]}` : ""}${finite(geometry.common_horizontal_fov_deg)?` · 관측 FOV ${fmt(geometry.common_horizontal_fov_deg,2)}°`:""}` : "계산 결과가 없습니다.";
    const nativeLink=$(prefix+"-native"),kind=state.preview==="primary"?spec.scene:state.preview,nativePath=data?.image_sets?.[kind]?.native_isp || (kind===spec.scene?data?.images?.native_isp:null);
    nativeLink.classList.toggle("hidden",!nativePath);if(nativePath){nativeLink.href=fileURL(job,nativePath);nativeLink.textContent=`Native ISP ${native?`${native[1]}×${native[0]}`:""} ↗`;}
    const raw = $(prefix + "-raw");
    raw.classList.toggle("hidden", !data); raw.textContent = "원본 ZIP ↓";
    if (data) raw.href = `${API}/runs/${encodeURIComponent(job.id)}/bundle`;
    const overlay = $(prefix + "-viewport").querySelector(".roi-overlay"); overlay.replaceChildren();
    const sites = metrics(job).resolution?.sites || {};
    Object.entries(sites).forEach(([name, site]) => {
      const measuredROI=site[$("resolution-orientation").value]?.roi || site.roi || site.radial?.roi; if (!Array.isArray(measuredROI)) return;
      const [r0, r1, c0, c1] = measuredROI, roiShape=metrics(job).resolution?.schema==="camera_virtual_lab_wide_resolution_v1" && native?native:output, marker = document.createElement("div"); marker.className = "roi-marker";
      marker.style.cssText = `left:${100*c0/roiShape[1]}%;top:${100*r0/roiShape[0]}%;width:${100*(c1-c0)/roiShape[1]}%;height:${100*(r1-r0)/roiShape[0]}%`;
      marker.textContent = ["center","row2_col2"].includes(name) ? "CENTER" : ""; overlay.append(marker);
    });
    overlay.classList.toggle("visible", $("show-roi").checked && !["scene","cra","angular_gain"].includes(state.stage) && (state.preview==="resolution" || (state.preview==="primary" && spec.scene==="resolution")) && Object.keys(sites).length > 0);
    alignOverlay(prefix);
  }
  function alignOverlay(prefix){
    const img=$(prefix+"-image"),view=$(prefix+"-viewport"),overlay=view.querySelector(".roi-overlay");
    if(!img.naturalWidth || !view.clientWidth)return;
    const ratio=img.naturalWidth/img.naturalHeight,w=Math.min(view.clientWidth,view.clientHeight*ratio),h=w/ratio;
    Object.assign(overlay.style,{left:`${(view.clientWidth-w)/2}px`,top:`${(view.clientHeight-h)/2}px`,width:`${w}px`,height:`${h}px`,right:"auto",bottom:"auto"});
  }
  function metrics(job) { const value = result(job); return value?.metrics || value?.tests || {}; }
  function lineChart(id, curves, {xLabel = "", yLabel = "", yMax = null, xMax = null, markers = []} = {}) {
    curves = curves.map(curve => ({...curve, points: curve.x?.map((x, i) => [x, curve.y?.[i]]).filter(p => p.every(finite)) || []})).filter(curve => curve.points.length >= 2);
    const w = 310, h = 145, left = 32, right = 12, top = 10, bottom = 30;
    const all = curves.flatMap(curve => curve.points), xmax = xMax || Math.max(...all.map(p => p[0]), 1e-9), ymax = yMax || Math.max(...all.map(p => p[1]), 1e-9)*1.07;
    const ymin = Math.min(0, ...all.map(p => p[1]));
    const X = x => left + x / xmax * (w-left-right), Y = y => top + (ymax-y)/(ymax-ymin)*(h-top-bottom);
    let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHTML(yLabel || xLabel)}">`;
    for (let i=0;i<=4;i++) {
      const y = top + i*(h-top-bottom)/4, value = ymax - i*(ymax-ymin)/4;
      svg += `<line class="grid-line" x1="${left}" x2="${w-right}" y1="${y}" y2="${y}"/><text text-anchor="end" x="${left-5}" y="${y+3}">${all.length ? fmt(value, value>10?0:1) : ""}</text>`;
    }
    svg += `<line class="axis-line" x1="${left}" y1="${h-bottom}" x2="${w-right}" y2="${h-bottom}"/>`;
    if (all.length) for (let i=0;i<=4;i++) svg += `<text x="${X(xmax*i/4)}" y="${h-bottom+14}" text-anchor="middle">${fmt(xmax*i/4,xmax>10?0:2)}</text>`;
    markers.filter(marker => finite(marker.x) && marker.x <= xmax).forEach(marker => { svg += `<line x1="${X(marker.x)}" x2="${X(marker.x)}" y1="${top}" y2="${h-bottom}" stroke="${marker.color || "#c3ceca"}" stroke-dasharray="2 3"/><text x="${X(marker.x)-3}" y="${top+7}" text-anchor="end">${escapeHTML(marker.label)}</text>`; });
    curves.forEach(curve => { svg += `<polyline points="${curve.points.filter(p => p[0]<=xmax).map(p=>`${X(p[0]).toFixed(2)},${Y(p[1]).toFixed(2)}`).join(" ")}" fill="none" stroke="${curve.color}" stroke-width="${curve.width || 1.8}" ${curve.dashed?'stroke-dasharray="4 3"':''} stroke-linejoin="round"/>`; });
    svg += `<text x="${left}" y="8">${escapeHTML(yLabel)}</text><text text-anchor="end" x="${w-right}" y="${h-1}">${escapeHTML(xLabel)}</text></svg>`;
    $(id).innerHTML = svg + (all.length ? "" : '<div class="chart-empty">실행 결과 대기</div>');
  }
  function delta(id, a, b, lowerBetter = false, suffix = "") {
    const target = $(id); target.textContent = ""; target.classList.remove("negative");
    if (!finite(a) || !finite(b) || state.current?.id === state.baseline?.id) return;
    const change = b-a; target.textContent = `${change>0?"+":""}${fmt(change, 2)}${suffix} vs A`;
    target.classList.toggle("negative", lowerBetter ? change>0 : change<0);
  }
  function colorCSS(rgb) {
    if (!Array.isArray(rgb) || !rgb.every(finite)) return "#e7ede9";
    const gamma = value => value <= .0031308 ? 12.92*value : 1.055*Math.pow(value,1/2.4)-.055;
    return `rgb(${rgb.map(value=>Math.round(255*Math.max(0,Math.min(1,gamma(Math.max(0,value)))))).join(",")})`;
  }
  function xyzRGB(xyz) { return [3.2404542*xyz[0]-1.5371385*xyz[1]-.4985314*xyz[2], -.969266*xyz[0]+1.8760108*xyz[1]+.041556*xyz[2], .0556434*xyz[0]-.2040259*xyz[1]+1.0572252*xyz[2]]; }
  function renderMetrics() {
    const a = metrics(state.baseline), b = metrics(state.current);
    const selectedSite=$("resolution-site").value, oldOptions=[...$("resolution-site").options].map(o=>[o.value,o.textContent]);
    const fieldLabels={center:"중심",top_left:"좌상",top_right:"우상",bottom_left:"좌하",bottom_right:"우하",upper_left:"좌상",upper_right:"우상",lower_left:"좌하",lower_right:"우하",row1_col1:"좌상",row1_col2:"상단",row1_col3:"우상",row2_col1:"좌측",row2_col2:"중심",row2_col3:"우측",row3_col1:"좌하",row3_col2:"하단",row3_col3:"우하",top:"상단",bottom:"하단",left:"좌측",right:"우측",top_center:"상단",bottom_center:"하단",middle_left:"좌측",middle_right:"우측"};
    const fullField=fieldConfig(config(state.current)).mode==="full_field",wide=b.resolution?.schema==="camera_virtual_lab_wide_resolution_v1";
    $("resolution-orientation").classList.toggle("hidden",!wide);const orientation=$("resolution-orientation").value;
    const centerSite=b.resolution?.sites?.center?"center":wide?"row2_col2":"center";
    const chartSites=fullField?Object.keys(b.resolution?.sites || {}).filter(name=>name!==centerSite):[];
    const siteOptions=[["main",fullField?"영상 중심 · 실제 ROI":"공통 차트 · 중심 국소 PSF"],...chartSites.map(name=>[`chart:${name}`,`영상 ${fieldLabels[name] || name} · 실제 ROI`]),...(b.resolution?.field_windows || []).map(field=>[String(field.id),`${fieldLabels[field.id] || field.id} · ${finite(field.object_angle_radius_deg)?`${fmt(field.object_angle_radius_deg,2)}°` : "각도 미제공"} · model field ${fmt(field.field_fraction,3)}`])];
    if(JSON.stringify(oldOptions)!==JSON.stringify(siteOptions)){
      $("resolution-site").replaceChildren();
      siteOptions.forEach(([id,label])=>{const option=document.createElement("option");option.value=id;option.textContent=label;option.title=label;$("resolution-site").append(option);});
      if(siteOptions.some(([id])=>id===selectedSite))$("resolution-site").value=selectedSite;
    }
    $("resolution-site").disabled=fullField && $("resolution-response").value==="optical";
    if($("resolution-site").disabled)$("resolution-site").value="main";
    const site=$("resolution-site").value, response=$("resolution-response").value;
    const chartSite=site.startsWith("chart:")?site.slice(6):centerSite;
    const fieldMetrics = metric => site==="main" || site.startsWith("chart:")?metric.resolution:(metric.resolution?.field_windows || []).find(field=>String(field.id)===site)?.metrics;
    const siteRecord=metric=>{const sites=fieldMetrics(metric)?.sites || {},value=sites[chartSite] || (site==="main"?(sites.center || sites.row2_col2):null);return value?.[orientation] || value || {};};
    const res = metric => siteRecord(metric)[response] || {};
    const ra = res(a), rb = res(b), hasBaselineCurve=Array.isArray(ra.sfr), angular = Array.isArray(rb.frequency?.cycles_per_degree) && (!hasBaselineCurve || Array.isArray(ra.frequency?.cycles_per_degree));
    const fkey = angular ? "cycles_per_degree" : "cycles_per_pixel", vkey = angular ? "mtf50_cycles_per_degree" : "mtf50_cycles_per_pixel";
    $("mtf-value").textContent = fmt(rb[vkey], 3); $("mtf-unit").textContent = `MTF50 · ${angular ? "cycles/degree" : "cycles/pixel"}`;
    delta("mtf-delta",ra[vkey],rb[vkey]);
    const curves = [], addSFR = (r,color,dashed=false) => { if(r.frequency) curves.push({x:r.frequency[fkey],y:r.sfr,color,dashed}); };
    addSFR(ra,"#b8823c"); if(response==="processed_system")addSFR(siteRecord(b).linear_system || {},"#6cbab0",true); addSFR(rb,"#087f83");
    if(response==="optical"){
      const optical = metric => {
        const field=(site==="main"?metric.resolution?.field_windows?.find(window=>window.id==="center")?.metrics:fieldMetrics(metric))?.optical;
        const direct=metric.resolution?.optical || metric.optical;
        return (field || (direct?.axes?direct:direct?.[site==="main"?"center":site]))?.axes?.horizontal || {};
      };
      const oa=optical(a),ob=optical(b);const factor=ob.cycles_per_mm?.[1]?ob.cycles_per_degree?.[1]/ob.cycles_per_mm[1]:null;
      $("mtf-value").textContent=fmt(finite(factor)&&finite(ob.mtf50_cycles_per_mm)?ob.mtf50_cycles_per_mm*factor:null,3);$("mtf-unit").textContent="광학 MTF50 · cycles/degree";
      $("mtf-delta").textContent="";
      lineChart("resolution-chart",[[oa,"#b8823c"],[ob,"#087f83"]].map(([r,color])=>({x:r.cycles_per_degree,y:r.mtf,color})),{xLabel:"cycles/degree",yLabel:"Optical MTF",yMax:1.05,markers:[{x:finite(factor)?ob.sensor_nyquist_cycles_per_mm*factor:null,label:"Sensor Nyquist"}]});
      $("resolution-detail").textContent=Array.isArray(ob.mtf)&&ob.mtf.length?`${fullField?"550 nm 중심 광학 PSF 진단":"선택한 위치의 광학 PSF 응답"} · ISP·센서 픽셀 개구와 구분${ob.mtf50_cycles_per_mm===null?" · 표시 대역에서 50% 교차 없음":""}`:b.resolution?"이번 결과에 광학 PSF 진단 곡선이 저장되지 않았습니다. 선형/ISP SFR을 선택하세요.":"해상력 시험을 실행하면 곡선이 표시됩니다.";
      if(b.resolution && !ob.mtf?.length){const empty=$("resolution-chart").querySelector(".chart-empty");if(empty)empty.textContent="광학 진단 미저장";}
    }else{
      lineChart("resolution-chart",curves,{xLabel:angular?(wide?"cycles/degree (국소)":fullField?"cycles/degree (근축)":"cycles/degree"):"cycles/pixel",yLabel:response==="processed_system"?"ISP SFR":"Linear SFR",markers:[{x:rb[angular?"nyquist_cycles_per_degree":"nyquist_cycles_per_pixel"],label:"Nyquist"}]});
      $("resolution-detail").textContent = b.resolution ? `${wide?`Native ${fieldLabels[chartSite] || chartSite} · ${orientation==="radial"?"방사":"접선"} SFR · 국소 투영 Jacobian${siteRecord(b).angular_geometry?.angular_frequency_qualified===false?" · B: ROI 각도척도 변동 >5%, 국소 각도축 제한":""}${hasBaselineCurve && siteRecord(a).angular_geometry?.angular_frequency_qualified===false?" · A: ROI 각도척도 변동 >5%, 국소 각도축 제한":""}${hasBaselineCurve && !angular?" · A/B 공통 native 주파수":""}`:fullField?`영상 ${fieldLabels[chartSite] || chartSite} 실제 ROI · FOV/640 근축 환산 · 국소 왜곡 미보정`:site==="main"?"메인 차트: 중심 국소 PSF":"독립 계산한 시야 위치"}${response==="processed_system"?" · 점선: 선형 응답":""}${finite(rb.overshoot_fraction)?` · overshoot ${fmt(rb.overshoot_fraction*100,1)}%`:""}` : "해상력 시험을 실행하면 곡선이 표시됩니다.";
    }
    $("color-value").textContent = fmt(b.color?.delta_e00_mean, 2); delta("color-delta", a.color?.delta_e00_mean,b.color?.delta_e00_mean,true);
    const patches = b.color?.patches || []; $("color-chart").replaceChildren();
    for(let i=0;i<24;i++) { const patch=patches[i], el=document.createElement("div"), measured=document.createElement("span");el.className="patch";
      if(patch){el.style.background=colorCSS(patch.reference_linear_rgb || (patch.reference_xyz?xyzRGB(patch.reference_xyz):null)); measured.style.background=colorCSS(patch.measured_linear_rgb);el.title=`패치 ${i+1} · 왼쪽 기준 / 오른쪽 측정 · ΔE00 ${fmt(patch.delta_e00,2)}`;}
      el.append(measured);$("color-chart").append(el);
    }
    $("color-detail").textContent = b.color ? `왼쪽 기준 · 오른쪽 출력 · 최대 ΔE00 ${fmt(b.color.delta_e00_max,2)} · WB 색도 오차 ${fmt(b.color.neutral_xy_error_mean,4)}` : "각 패치의 기준색 · 출력색을 함께 비교합니다.";
    const noiseRows = value => value.noise?.photon_transfer_points || value.noise?.levels || value.noise?.curve || value.noise?.response || [];
    const point = row => row.processed || row.isp || row.raw || row;
    const noisePoint = value => value.noise?.processed || value.noise?.isp || value.noise?.raw || value.noise || {};
    const na = noisePoint(a), nb = noisePoint(b); $("noise-value").textContent=fmt(nb.snr_db,1);delta("noise-delta",na.snr_db,nb.snr_db,false," dB");
    const hasMeasured=noiseRows(b).length>0;
    lineChart("noise-chart",[[a,"#b8823c"],[b,"#087f83"]].map(([value,color])=>({x:noiseRows(value).length?noiseRows(value).map(row=>row.mean_electrons ?? row.luminance_cd_m2 ?? row.input_level ?? row.scale ?? row.luminance_scale):value.noise?.model_snr_curve?.mean_electrons,y:noiseRows(value).length?noiseRows(value).map(row=>point(row).snr_db):value.noise?.model_snr_curve?.snr_db,color,dashed:!noiseRows(value).length})),{xLabel:"평균 광전자 (e⁻)",yLabel:hasMeasured?"측정 SNR (dB)":"모델 예측 SNR (dB)"});
    $("noise-detail").textContent = b.noise ? `Gr · ${nb.repeat_count || config(state.current).frames || 8}회 시간 분산 · σ ${fmt(nb.temporal_std,3)} ${nb.unit || ""}${b.noise.after_isp?` · ISP σ ${fmt(b.noise.after_isp.temporal_std,4)}`:""}` : "독립 반복 취득으로 시간 잡음을 측정합니다.";
    const toneRows = value => value.tone?.response || value.tone?.levels || [];
    const clip = value => {const values=toneRows(value).map(row=>row.adc_saturation_fraction).filter(finite);return values.length?Math.max(...values):value.tone?.adc_saturation_fraction;};
    const ta=clip(a), tb=clip(b);$("tone-value").textContent=finite(tb)?`${fmt(tb*100,2)}%`:"—";delta("tone-delta",finite(ta)?ta*100:ta,finite(tb)?tb*100:tb,true," pp");
    lineChart("tone-chart",[[a,"#b8823c"],[b,"#087f83"]].map(([value,color])=>({x:toneRows(value).map(row=>row.input_level),y:toneRows(value).map(row=>row.mean_electrons),color})),{xLabel:"입력 광량 배율",yLabel:"mean e⁻"});
    const shading=b.shading || b.tone?.shading, ratios=Object.entries(shading?.sites || {}).filter(([name])=>name!=="center").map(([,site])=>site.relative_to_center).filter(finite);
    $("tone-detail").textContent = b.tone ? `Well ${fmt(b.tone.full_well_e,0)} e⁻ · ADC ${fmt(b.tone.adc_white_dn,0)} DN${ratios.length?` · 주변/중심 ${fmt(100*ratios.reduce((x,y)=>x+y,0)/ratios.length,1)}%`:""}` : "광량별 응답과 well·ADC 포화를 확인합니다.";
    renderFieldResponse(a.field_response,b.field_response);
    $("results-note").textContent=wide?"9개 위치의 방사·접선 SFR은 native 영상에서 측정합니다. 각도축은 국소 구면 투영 Jacobian이며 ROI 내 척도 변화가 크면 제한으로 표시합니다. 640×360 영상은 미리보기입니다.":fullField?"시야별 시스템 SFR의 cycles/degree는 FOV/640 근축 환산입니다. 국소 왜곡을 보정한 물체각 MTF가 아닙니다. 광학 MTF는 별도의 550 nm 중심 PSF 진단입니다.":"수치는 실행된 시험에만 표시됩니다. 광학 MTF와 ISP 이후 SFR은 별도의 응답입니다.";
  }
  function renderFieldResponse(a,b){
    const maxCRA=b?.cra_deg?.max ?? b?.theta_deg_range?.[1];$("cra-value").textContent=fmt(maxCRA,2);
    const geometry=result(state.current)?.provenance?.geometry || result(state.current)?.geometry || {};
    const mode=b?.angular_mode ?? angularConfig(config(state.current)).mode,pupil=mode==="physical_pupil",wide=config(state.current).lens_id==="wide_parametric";
    $("field-response-model").textContent=pupil?"PUPIL DIRECTION MEAN":mode==="off"?"ANGULAR RESPONSE OFF":"CHIEF-RAY MODEL";
    $("field-response-scope").textContent=b?`${b.mode==="full_field"?"설정한 관측 시야 전체":"중심 국소 광학 모델"}${finite(geometry.common_horizontal_fov_deg)?` · 수평 ${fmt(geometry.common_horizontal_fov_deg,2)}°`:""} · ${wide?(pupil?"선언한 출사동공 원판 평균 · CRA는 chief-ray 진단":"선언한 출사동공 기하 · Chief ray"):pupil?"저장 동공 방향의 가중평균 · CRA는 chief-ray 진단":b.cra_origin==="analytic_thin_lens_chief_ray_proxy"?"얇은 렌즈 chief-ray 근사":"저장 렌즈 chief-ray 방향"}`:"시야 계산을 실행하면 입사각과 채널별 상대 응답이 표시됩니다.";
    lineChart("cra-chart",[[a,"#b8823c"],[b,"#087f83"]].map(([r,color])=>({x:r?.profile?.radius_fraction,y:r?.profile?.cra_deg,color})),{xLabel:"관측창 반경 비율",yLabel:"CRA (°)"});
    const colors=["#bf6e63","#238778","#76a465","#587bc0"],channels=["R","Gr","Gb","B"];
    lineChart("angular-chart",channels.map((_,i)=>({x:b?.profile?.radius_fraction,y:b?.profile?.gain_r_gr_gb_b?.map(row=>row[i]),color:colors[i]})),{xLabel:"관측창 반경 비율",yLabel:"상대 각도 응답 (×)"});
    $("angular-gain-ranges").replaceChildren();
    channels.forEach((channel,i)=>{const row=document.createElement("span"),low=b?.gain_r_gr_gb_b?.min?.[i] ?? b?.gain_range_r_gr_gb_b?.[i]?.[0],high=b?.gain_r_gr_gb_b?.max?.[i] ?? b?.gain_range_r_gr_gb_b?.[i]?.[1];row.textContent=`${channel} ${fmt(low,3)}–${fmt(high,3)}×`;row.style.borderTopColor=colors[i];$("angular-gain-ranges").append(row);});
    $("field-response-note").textContent=b?(pupil?`정면 QE 1회 적용 × ${wide?"선언한 출사동공 원판":"저장 동공 방향"}의 조건부 가중평균. 수신측 위치별 응답을 분리해 평균하며 광선 도달 위치·각도를 함께 반영한 PSF 재계산은 아닙니다. 정규화된 가중치는 투과율을 추가하지 않습니다.${finite(b.pupil_sampling?.source_pupil_sample_count)?` 동공 표본 ${b.pupil_sampling.source_pupil_sample_count}개.`:""}`:mode==="physical_relative"?"저장 정면 QE × 층 모델의 상대 각도 응답 · chief ray만 적용 · 제품 실측 QE와 정합한 모델이 아닙니다.":mode==="cosine_power"?"선언한 채널별 cosⁿ 근사 · chief ray만 적용 · 수광각별 실측 응답이 아닙니다.":"각도 응답 적용 OFF · 상대 gain 1 · CRA 지도는 렌즈 모델에서 별도로 계산됩니다."):"지원되는 모델 범위와 계산 출처를 결과에 보존합니다.";
  }
  function renderNotes() {
    const value=result(state.current), root=$("assumptions-content"); root.replaceChildren();
    if(!value){const p=document.createElement("p");p.textContent="모델 기반 가상 Lab입니다. 실행 결과에는 모델 출처, 실제 계산 격자, ISP 설정과 측정 조건이 보존됩니다.";root.append(p);return;}
    const dl=document.createElement("dl");
    const facts={"실행 ID":state.current.id,"원본 RAW":value.source_run_id?`재처리 · ${value.source_run_id}`:"새 취득 · 노출별 독립 잡음",
      "모델":`${optionName("lenses",config(state.current).lens_id)} / ${optionName("sensors",config(state.current).sensor_id)}`,
      "촬영 조건":`${config(state.current).exposure_ms} ms · gain ${config(state.current).gain} · ${config(state.current).illuminant} · ${config(state.current).luminance_cd_m2} cd/m²`,
      "시야 · 입사각":`${fieldConfig(config(state.current)).mode} · ${angularConfig(config(state.current)).mode}`,
      "실행 시간":finite(value.timings?.capture_elapsed_s)?`${fmt(value.timings.capture_elapsed_s,1)} s (취득)${finite(value.timings.replay_elapsed_s)?` · ${fmt(value.timings.replay_elapsed_s,1)} s (재처리)`:""}`:null};
    Object.entries(facts).forEach(([key,text])=>{if(!text)return;const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=key;dd.textContent=text;dl.append(dt,dd);});root.append(dl);
    const notes=value.limitations || value.assumptions || []; const ul=document.createElement("ul");
    (Array.isArray(notes)?notes:Object.values(notes)).forEach(note=>{const li=document.createElement("li");li.textContent=typeof note==="string"?note:JSON.stringify(note);ul.append(li);});root.append(ul);
    const a=document.createElement("a");a.href=fileURL(state.current,"result.json");a.textContent="전체 수치 · 모델 출처 JSON ↗";a.target="_blank";a.rel="noopener";root.append(a);
  }
  function renderGeometry(){
    const value=result(state.current),g=value?.geometry || value?.provenance?.geometry || {},wide=config(state.current).lens_id==="wide_parametric",root=$("geometry-summary");root.replaceChildren();root.classList.toggle("hidden",!wide);if(!wide)return;
    const facts=[["HFOV",g.horizontal_fov_deg,"°"],["VFOV",g.vertical_fov_deg,"°"],["DFOV",g.diagonal_fov_deg,"°"],["초점거리",g.focal_length_mm," mm"],["Pitch",g.pixel_pitch_um," µm"]];
    for(const [label,n,unit] of facts){const span=document.createElement("span"),strong=document.createElement("strong");span.textContent=label+" ";strong.textContent=fmt(n,2)+unit;span.append(strong);root.append(span);}
    const note=document.createElement("small");note.textContent=`${g.projection || config(state.current).wide_angle?.projection || ""} · Native ${g.native_shape?.[1] ?? "—"} × ${g.native_shape?.[0] ?? "—"} · 640 × 360 미리보기 · 선언한 투영/회절/blur/출사동공 근사 · DB 렌즈 처방 아님`;root.append(note);
  }
  function renderAll() {
    updatePreview();renderGeometry();
    renderImage("baseline",state.baseline);renderImage("candidate",state.current);renderMetrics();renderNotes();updateButtons();renderHistory();
    const href=result(state.current)?.report_url;$("report-link").classList.toggle("disabled",!href);$("report-link").setAttribute("aria-disabled",String(!href));
    if(href)$("report-link").href=href;
    $("candidate-status").textContent=completed(state.current)?(result(state.current)?.source_run_id?"RAW REPLAY":"COMPUTED"):"READY";
  }
  function renderHistory() {
    const list=$("history-list");list.replaceChildren();$("run-count").textContent=state.jobs.length;
    if(!state.jobs.length){const p=document.createElement("p");p.className="history-empty";p.textContent="첫 Lab 실행이 이곳에 저장됩니다.";list.append(p);return;}
    state.jobs.forEach(job=>{const item=document.createElement("article");item.className="history-item"+(job.id===state.current?.id?" selected":"");
      const h=document.createElement("h3");h.textContent=job.name || config(job).name || "Camera Virtual Lab";
      const p=document.createElement("p");p.textContent=`${optionName("lenses",config(job).lens_id)} · ${optionName("sensors",config(job).sensor_id)}`;
      const meta=document.createElement("p");meta.textContent=job.id;const actions=document.createElement("div");actions.className="history-actions";
      const badge=document.createElement("span");badge.className="state"+(job.status==="failed"?" failed":"");badge.textContent=statusLabels[job.status] || job.status;
      const button=document.createElement("button");button.className="icon-button";button.textContent=job.status==="succeeded"?"결과 · 구성 불러오기":"상태 보기";
      button.onclick=()=>openRun(job.id).catch(showError);actions.append(badge,button);item.append(h,p,meta,actions);list.append(item);
    });
  }
  async function refreshJobs(autoload=false) {
    const response=await api("/runs");state.jobs=Array.isArray(response)?response:response.runs || response.items || [];
    state.jobs.sort((a,b)=>String(b.created_at || b.created || "").localeCompare(String(a.created_at || a.created || "")));
    if(autoload){const ready=state.jobs.filter(job=>job.status==="succeeded");
      if(ready.length){state.current=await api(`/runs/${encodeURIComponent(ready[0].id)}`);
        let baselineId;try{baselineId=sessionStorage.getItem("camera-virtual-lab-baseline");}catch(_){}
        const baseline=ready.find(job=>job.id===baselineId) || ready[ready.length-1];
        state.baseline=baseline.id===state.current.id?state.current:await api(`/runs/${encodeURIComponent(baseline.id)}`);
        applySpec(config(state.current));renderAll();notice("저장된 계산 결과를 열었습니다. ISP를 조정하거나 다른 카메라를 촬영해 비교하세요.");
      }else{renderAll();notice("준비되었습니다. 카메라 구성과 시험을 선택해 첫 가상 촬영을 실행하세요.");}
      const active=state.jobs.find(isBusy);if(active){state.active=active;updateButtons();schedulePoll();}
    }else renderHistory();
  }
  async function openRun(id) {
    const job=await api(`/runs/${encodeURIComponent(id)}`);state.stopped=["failed","cancelled"].includes(job.status)?job:null;
    if(completed(job)){state.current=job;applySpec(config(job));renderAll();notice(`저장 결과 · ${config(job).name}`);}
    else if(isBusy(job)){state.active=job;updateButtons();schedulePoll();}
    else notice(`${statusLabels[job.status] || job.status} · ${typeof job.error==="string"?job.error:JSON.stringify(job.error || {})}`,job.status==="failed"?"error":"ready");
    updateButtons();toggleHistory(false);
  }
  async function openLinkedRuns(params) {
    const runId=(params.get("run") || "").trim(),baselineId=(params.get("baseline") || "").trim();
    if(!runId || (params.has("baseline") && !baselineId))throw Error("데모 링크의 run 또는 baseline 실행 ID가 비어 있습니다.");
    const [current,baseline]=await Promise.all([api(`/runs/${encodeURIComponent(runId)}`),baselineId && baselineId!==runId?api(`/runs/${encodeURIComponent(baselineId)}`):Promise.resolve(null)]);
    if(!completed(current))throw Error(`요청한 결과 ${runId}는 완료된 계산이 아닙니다 (${statusLabels[current.status] || current.status}).`);
    if(baseline && !completed(baseline))throw Error(`요청한 기준안 ${baselineId}는 완료된 계산이 아닙니다 (${statusLabels[baseline.status] || baseline.status}).`);
    state.current=current;state.baseline=baseline || (baselineId===runId?current:null);
    applySpec(config(current));renderAll();notice("링크에 지정한 완료 결과를 열었습니다. A/B 영상과 구성은 해당 실행에 고정되어 있습니다.");
  }
  function showError(error){notice(error.message || String(error),"error");updateButtons();}
  async function submit(replay=false) {
    if(state.active)return;
    if(!$("capture-form").reportValidity())return;
    $("run-button").disabled=true;$("replay").disabled=true;
    try {
      const payload=replay?{source_run_id:state.current.id,name:`${$("run-name").value.trim()} · ISP`,isp:readISP()}:readSpec();
      const job=await api(replay?"/replays":"/runs",payload);state.stopped=null;state.active=job;updateButtons();
      notice(replay?"원본 RAW를 유지하고 ISP와 Lab 지표를 다시 계산합니다.":"가상 촬영을 제출했습니다. 광학·센서 계산과 Lab 시험을 진행합니다.","busy");
      await refreshJobs();schedulePoll();
    }catch(error){showError(error);}
  }
  function schedulePoll(){clearTimeout(state.poll);state.poll=setTimeout(poll,1500);}
  async function poll() {
    if(!state.active)return;
    try{
      const job=await api(`/runs/${encodeURIComponent(state.active.id)}`);state.active=job;
      if(isBusy(job)){const progress=job.progress?.message || job.progress?.stage || job.lab_progress?.message || job.lab_progress?.stage || job.study_progress?.message;
        notice(progress || `${statusLabels[job.status]} · 광학 → RAW → ISP → Lab 시험`,"busy");schedulePoll();return;}
      state.active=null;state.stopped=["failed","cancelled"].includes(job.status)?job:null;
      if(completed(job)){state.current=job;if(!state.baseline){state.baseline=job;try{sessionStorage.setItem("camera-virtual-lab-baseline",job.id);}catch(_){}}
        applySpec(config(job));renderAll();notice(`완료 · ${config(job).name} · 원본과 측정 결과가 저장되었습니다.`);toast("가상 촬영과 Lab 측정이 완료되었습니다.");
      }else notice(`${statusLabels[job.status] || job.status}${job.error?` · ${typeof job.error==="string"?job.error:JSON.stringify(job.error)}`:""}`,job.status==="failed"?"error":"ready");
      await refreshJobs();updateButtons();
    }catch(error){notice(`상태 확인을 다시 시도합니다 · ${error.message}`,"error");schedulePoll();}
  }
  function toggleHistory(open){$("history-panel").classList.toggle("hidden",!open);$("history-toggle").setAttribute("aria-expanded",String(open));}
  function transformImages(){document.querySelectorAll(".image-content").forEach(el=>el.style.transform=`translate(${state.pan[0]}px,${state.pan[1]}px) scale(${state.zoom})`);$("zoom-label").textContent=`${Math.round(state.zoom*100)}%`;}
  function zoom(value){state.zoom=Math.min(5,Math.max(1,value));if(state.zoom===1)state.pan=[0,0];transformImages();}
  function bind() {
    $("capture-form").onsubmit=event=>{event.preventDefault();submit().catch(showError);};
    $("replay").onclick=()=>submit(true).catch(showError);
    $("resume-run").onclick=async()=>{if(state.active || !state.stopped)return;try{const job=await api(`/runs/${encodeURIComponent(state.stopped.id)}/resume`,{});state.active=job;state.stopped=null;updateButtons();notice("저장된 타일의 해시를 확인하며 native 촬영을 재개합니다.","busy");await refreshJobs();schedulePoll();}catch(error){showError(error);}};
    $("cancel-run").onclick=async()=>{if(!state.active)return;try{await api(`/runs/${encodeURIComponent(state.active.id)}/cancel`,{});notice("취소를 요청했습니다. 실행 상태를 확인합니다.","busy");schedulePoll();}catch(error){showError(error);}};
    $("reset-config").onclick=()=>{if(state.template){applySpec(clone(state.template));toast("기본 카메라 구성을 불러왔습니다.");}};
    $("pin-baseline").onclick=()=>{if(!completed(state.current))return;state.baseline=state.current;try{sessionStorage.setItem("camera-virtual-lab-baseline",state.current.id);}catch(_){}renderAll();toast("현재 결과를 기준안 A로 고정했습니다.");};
    document.querySelectorAll("[data-profile]").forEach(button=>button.onclick=()=>setProfile(button.dataset.profile));
    document.querySelectorAll("[data-stage]").forEach(button=>button.onclick=()=>{state.stage=button.dataset.stage;document.querySelectorAll("[data-stage]").forEach(tab=>{tab.classList.toggle("active",tab===button);tab.setAttribute("aria-selected",String(tab===button));});renderImage("baseline",state.baseline);renderImage("candidate",state.current);updateMapLegend();});
    document.querySelectorAll("[data-focus]").forEach(button=>button.onclick=()=>{const target=$(button.dataset.focus);if(target.tagName==="DETAILS")target.open=true;target.scrollIntoView({behavior:"smooth",block:"center"});});
    $("capture-form").addEventListener("input",()=>{sliderLabels();updateButtons();});
    ["scene","sensor","illuminant","field-mode","angular-mode"].forEach(id=>$(id).addEventListener("change",()=>updateConfiguration()));
    $("lens").addEventListener("change",()=>updateConfiguration(true));
    $("show-roi").onchange=()=>{renderImage("baseline",state.baseline);renderImage("candidate",state.current);};
    $("resolution-site").onchange=$("resolution-response").onchange=$("resolution-orientation").onchange=()=>{renderMetrics();renderImage("baseline",state.baseline);renderImage("candidate",state.current);};
    $("preview-scene").onchange=()=>{state.preview=$("preview-scene").value;updatePreview();renderImage("baseline",state.baseline);renderImage("candidate",state.current);};
    $("zoom-in").onclick=()=>zoom(state.zoom+.5);$("zoom-out").onclick=()=>zoom(state.zoom-.5);$("zoom-reset").onclick=()=>zoom(1);
    document.querySelectorAll(".image-viewport").forEach(view=>{let drag=null;
      view.onpointerdown=event=>{if(state.zoom<=1)return;drag=[event.clientX-state.pan[0],event.clientY-state.pan[1]];view.setPointerCapture(event.pointerId);};
      view.onpointermove=event=>{if(!drag)return;state.pan=[event.clientX-drag[0],event.clientY-drag[1]];transformImages();};
      view.onpointerup=view.onpointercancel=()=>drag=null;
    });
    $("history-toggle").onclick=()=>toggleHistory($("history-panel").classList.contains("hidden"));$("history-close").onclick=()=>toggleHistory(false);
    $("refresh-runs").onclick=()=>refreshJobs().catch(showError);
    document.addEventListener("keydown",event=>{if(event.key==="Escape")toggleHistory(false);});
    for(const prefix of ["baseline","candidate"])$(prefix+"-image").onerror=()=>{const empty=$(prefix+"-empty");$(prefix+"-image").hidden=true;empty.classList.remove("hidden");empty.querySelector("strong").textContent="영상 파일을 열 수 없습니다";empty.querySelector("p").textContent="실행 기록에서 결과를 다시 불러오세요.";};
    for(const prefix of ["baseline","candidate"])$(prefix+"-image").onload=()=>alignOverlay(prefix);
    window.addEventListener("resize",()=>{alignOverlay("baseline");alignOverlay("candidate");});
  }
  async function init(){bind();renderMetrics();updateButtons();
    try{const template=await api("/template");state.template=template.specification;state.catalog=template.catalog || {};
      [["lens","lenses"],["sensor","sensors"]].forEach(([id,key])=>{$(id).replaceChildren();(state.catalog[key] || []).forEach(item=>{const option=document.createElement("option");option.value=item.id;option.textContent=item.label || item.id;$(id).append(option);});});
      applySpec(state.template);
      const linked=new URLSearchParams(window.location.search);
      if(linked.has("run") || linked.has("baseline")){await refreshJobs();await openLinkedRuns(linked);}
      else await refreshJobs(true);
    }catch(error){showError(error);}
  }
  init();
})();

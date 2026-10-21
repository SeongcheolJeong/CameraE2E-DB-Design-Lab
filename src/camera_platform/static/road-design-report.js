"use strict";
const data = JSON.parse(document.getElementById("payload").textContent);
const $ = id => document.getElementById(id);
const fmt = (value, digits = 3) => value == null ? "—" : Number(value).toFixed(digits);
const pitch = value => Array.isArray(value) ? value.map(v => fmt(v,3)).join("×") : fmt(value,3);
const esc = value => String(value ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const colors = {"L1-S1":"#ffd16c", "L1-S2":"#73dcca", "L2-S1":"#8ab9ff", "L2-S2":"#eea6da"};
const names = {improvement_confirmed:"개선 확인", conditional_selection:"조건부 선택",
  baseline_retained:"기준안 유지", first_real_photo_executed:"첫 사진 계산 완료 · 선정 전"};
const ev = data.decision.evaluation, sel = data.decision.selection;
const assessment = data.assessment;
$("status").textContent = names[data.decision.status] || data.decision.status;
$("summary").textContent = ev
  ? `권장안 ${data.decision.recommended_id} · 선정 고정안 ${sel.selected_id} · 기준 ${data.spec.baseline_id}\n` +
    `독립 평가 저광량 CNR 중앙값 변화 ${fmt(ev.median_lowlight_cnr_gain * 100, 1)}% ` +
    `· 95% 구간 [${ev.median_lowlight_cnr_gain_ci95.map(v => fmt(v * 100, 1)).join(", ")}]% ` +
    `· 최소 국소 대비 보존 ${fmt(ev.minimum_local_contrast_ratio * 100, 1)}%` +
    (ev.baseline_out_of_spec ? "\n기준안도 지정한 밝은 영역의 포화 제약을 충족하지 못했습니다." : "")
  : "첫 사진에서 두 후보의 RAW·ISP·시간 잡음을 확인했습니다. 12장 설계 결정은 아직 완료되지 않았습니다.";
if (assessment?.contrast_failures.length) $("summary").textContent +=
  "\n대비 95% 보존이 필수인 용도에서는 기준안을 유지합니다. 선택안은 저조도 이득을 위한 조건부 대안입니다.";
const links = ev
  ? [["decision.json","결정 JSON"], ["specification.json","고정 명세"],
     ["selection-lock.json","선정 고정 기록"], ["package-manifest.json","자료 해시"],
     ["design-assessment.json","적용 조건·실패 위치"]]
  : [["first-photo.json","첫 사진 결과"], ["specification.json","고정 명세"]];
for (const [path, label] of links) {
  const link = document.createElement("a"); link.href = path; link.textContent = label;
  $("downloads").append(link, document.createTextNode(" · "));
}
for (const image of data.spec.images) {
  const option = document.createElement("option"); option.value = image.id;
  option.textContent = `${image.split === "selection" ? "선정" : "평가"} · ${image.channel} · ${image.id.slice(0,8)}`;
  $("image").append(option);
}
function roiOptions() {
  const image = data.spec.images.find(i => i.id === $("image").value);
  $("roi").replaceChildren();
  image.rois.forEach((roi, index) => {
    const option = document.createElement("option"); option.value = roi.id;
    option.textContent = `${index + 1}. ${roi.kind === "highlight" ? "밝은 영역" : roi.category} · ${roi.common_shape_row_col.join("×")} 표본`;
    $("roi").append(option);
  });
  render();
}
async function magnify(canvas, path) {
  const image = new Image(); image.src = path; await image.decode();
  canvas.width = image.width; canvas.height = image.height;
  const context = canvas.getContext("2d"); context.drawImage(image, 0, 0);
  const factor = Number($("display").value);
  if (factor !== 1) {
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    for (let i = 0; i < pixels.data.length; i += 4)
      for (let c = 0; c < 3; c++) pixels.data[i+c] = Math.min(255, pixels.data[i+c] * factor);
    context.putImageData(pixels, 0, 0);
  }
}
function profilePlot(rows, view) {
  const canvas = $("profile"), context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const all = rows.flatMap(r => r.spatial_profile || []);
  if (!all.length) { $("profileInfo").textContent = "이 단계의 선 프로파일이 없습니다."; return; }
  const minimum = Math.min(0, ...all), maximum = Math.max(1e-8, ...all);
  const x = i => 70 + i / Math.max(1, rows[0].spatial_profile.length - 1) * 970;
  const y = value => 205 - (value - minimum) / (maximum - minimum) * 165;
  context.strokeStyle = "#526674"; context.beginPath();
  context.moveTo(70, 35); context.lineTo(70, 205); context.lineTo(1040, 205); context.stroke();
  context.fillStyle = "#bdcddb"; context.font = "14px system-ui";
  context.fillText(fmt(maximum, 4), 2, 42); context.fillText(fmt(minimum, 4), 2, 205);
  context.fillText("공통 물체각 격자 열 위치", 425, 258);
  rows.forEach((row, index) => {
    context.strokeStyle = colors[row.candidate_id] || "#fff";
    context.lineWidth = 2; context.beginPath();
    row.spatial_profile.forEach((value, column) => {
      if (column === 0) context.moveTo(x(column), y(value)); else context.lineTo(x(column), y(value));
    }); context.stroke();
    context.fillStyle = context.strokeStyle;
    context.fillText(row.candidate_id, 80 + index * 155, 20);
  });
  $("profileInfo").textContent = `고정 물체 마스크 중앙의 행 ${view.line_row} · signed pre-tone luminance · 공통 좌표·범위`;
}
function render() {
  const image = data.spec.images.find(i => i.id === $("image").value);
  const roi = image.rois.find(r => r.id === $("roi").value);
  const light = Number($("light").value);
  const view = data.roi_views?.[image.id + "/" + roi.id];
  $("context").src = image.context_path;
  $("sourceInfo").textContent = `${image.id} · ${image.log.location} · 원본 ${image.original_shape_row_col.join("×")} ` +
    `· ROI ${roi.common_shape_row_col.join("×")} 유효 표본 · 원본 위치 [${roi.source_bbox_xyxy.join(", ")}]`;
  if (view) $("mask").src = view.mask_file;
  const original = new Image(); original.src = `inputs/${image.id}/original.jpg`;
  original.onload = () => {
    if ($("image").value !== image.id || $("roi").value !== roi.id) return;
    const box = roi.source_bbox_xyxy, canvas = $("sourceCrop");
    canvas.width = box[2] - box[0]; canvas.height = box[3] - box[1];
    canvas.getContext("2d").drawImage(original, box[0], box[1], canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
  };
  const rows = data.records.filter(r => r.image_id === image.id && r.roi_id === roi.id && r.brightness === light);
  $("panels").replaceChildren();
  for (const row of rows) {
    const card = document.createElement("article"); card.className = "card";
    const title = document.createElement("h2");
    title.textContent = row.candidate_id + (row.candidate_id === data.spec.baseline_id ? " · 기준" : "") +
      (row.candidate_id === data.decision.recommended_id ? " · 권장" : ""); card.append(title);
    const canvas = document.createElement("canvas"); card.append(canvas);
    magnify(canvas, row.preview_file).catch(() => { canvas.title = "영상 읽기 실패"; });
    const statistics = document.createElement("div"); statistics.className = "kv";
    statistics.innerHTML = `<span>CNR · 95% 구간</span><span>${fmt(row.cnr)} ${row.cnr_ci95 ? "["+row.cnr_ci95.map(v=>fmt(v,2)).join(", ")+"]" : ""}</span>` +
      `<span>native ROI 평균 광전자</span><span>${fmt(row.mean_photoelectrons,1)} e⁻/px</span>` +
      `<span>국소 대비</span><span>${fmt(row.local_contrast)}</span>` +
      `<span>Well / ADC 포화</span><span>${fmt(row.well_fraction*100,2)}% / ${fmt(row.adc_fraction*100,2)}%</span>` +
      `<span>취득 / native 격자</span><span>${row.repeats}회 / ${row.native_roi_shape.join("×")}</span>`;
    card.append(statistics);
    const link = document.createElement("a"); link.href = row.raw_file; link.textContent = "정수 RAW (.npy)";
    card.append(link); $("panels").append(card);
  }
  if (!rows.length) $("panels").textContent = "이 단계에서 계산하지 않은 후보·장면입니다.";
  profilePlot(rows, view);
}
$("image").onchange = roiOptions;
for (const id of ["roi", "light", "display"]) $(id).onchange = render;
roiOptions();
$("candidates").innerHTML = "<tr><th>후보</th><th>렌즈 / F#</th><th>저장 센서 모델</th><th>pitch</th><th>가상 영역 / 전체 MP</th><th>기준 대비 면적·픽셀 수</th></tr>" +
  data.spec.candidates.map(candidate => {
    const geometry = data.geometry_diagnostics[candidate.id];
    return `<tr><td>${esc(candidate.id)}</td><td>${esc(candidate.lens.identity.simulation_id)}<br>${fmt(candidate.lens.model.effective_focal_length_mm,2)} mm / F${fmt(candidate.lens.model.effective_f_number,2)}</td>` +
      `<td>${esc(candidate.sensor.revision.device_name)}<br>${esc(candidate.sensor.revision.report_code)}<br><small>${esc(candidate.sensor.readout.classification)}</small></td>` +
      `<td>${fmt(candidate.sensor.native_response.pixel_pitch_um,2)} µm</td>` +
      `<td>${geometry.declared_virtual_width_height_mm.map(v=>fmt(v,2)).join(" × ")} mm<br>${fmt(geometry.declared_virtual_megapixels,2)} MP</td>` +
      `<td>${fmt(geometry.area_ratio_to_baseline,2)}× / ${fmt(geometry.pixel_count_ratio_to_baseline,2)}×</td></tr>`;
  }).join("");
if (ev) {
  const checks = {median_lowlight_cnr:"저광량 CNR 중앙값 개선: 95% 하한 ≥10%",
    individual_lowlight_cnr:"개별 주요 ROI의 CNR 보존: 95% 하한 ≥95%",
    local_contrast:"국소 대비 보존 ≥95%", highlight_well:"밝은 영역 Well 포화 ≤1%",
    highlight_adc:"밝은 영역 ADC 포화 ≤1%", source_grid:"대표 ROI 입력 격자 변화 ≤1%"};
  $("metrics").innerHTML = "<table><tr><th>선정용 후보</th><th>최악 저광량 CNR</th><th>제약</th></tr>" +
    sel.ranking.map(r=>`<tr><td>${esc(r.id)}</td><td>${fmt(r.worst_cnr)}</td><td class="${r.feasible?"pass":"fail"}">${r.feasible?"충족":esc(r.failures.join("; "))}</td></tr>`).join("") +
    "</table><table><tr><th>독립 평가 조건</th><th>판정</th></tr>" +
    Object.entries(ev.acceptance_checks).map(([key, pass])=>`<tr><td>${esc(checks[key]||key)}</td><td class="${pass?"pass":"fail"}">${pass?"충족":"미충족"}</td></tr>`).join("") +
    "</table><p>선정용 6장과 평가용 6장의 로그는 겹치지 않습니다. 평가 결과로 후보를 다시 선택하지 않았습니다. 신뢰구간은 이 고정 입력의 취득 잡음에 대한 구간이며 도로 전체에 대한 통계 보증이 아닙니다.</p>";
  if (assessment?.contrast_failures.length) {
    const explanation = document.createElement("p");
    explanation.textContent = "대비 보존 제약을 위반한 조건입니다. 위치 열기로 원본과 기준·선택안을 직접 확인할 수 있습니다.";
    $("metrics").append(explanation);
    for (const failed of assessment.contrast_failures) {
      const button = document.createElement("button");
      button.style.cssText = "margin:6px;padding:10px;border:1px solid #7d6870;border-radius:6px;background:#293140;color:#fff;cursor:pointer";
      button.textContent = `${failed.image_id.slice(0,8)} · ${failed.category} · ${failed.brightness}× · 대비 ${fmt(failed.contrast_ratio*100,1)}% — 위치 열기`;
      button.onclick = () => {
        $("image").value = failed.image_id; roiOptions();
        $("roi").value = failed.roi_id; $("light").value = String(failed.brightness); render();
        $("image").scrollIntoView({behavior:"smooth",block:"center"});
      };
      $("metrics").append(button);
    }
  }
} else $("metrics").textContent = "선정 전: 먼저 입력 격자 수렴과 별도 장면 평가를 진행합니다.";
const sampling = data.decision.sampling?.results || [];
$("sampling").innerHTML = sampling.length ? "<table><tr><th>후보</th><th>PSF / convolution / sensor pitch µm</th><th>광전자 변화</th><th>윤곽 대비 변화</th></tr>" +
  sampling.map(s=>`<tr><td>${esc(s.candidate_id)}</td><td>${pitch(s.stored_psf_pitch_um)} / ${fmt(s.coarse_convolution_pitch_um,3)} → ${fmt(s.fine_convolution_pitch_um,3)} / ${fmt(s.sensor_pitch_um,3)}</td><td>${fmt(s.mean_relative_l1_delta*100,3)}%</td><td>${fmt(s.contrast_relative_delta*100,3)}%</td></tr>`).join("") + "</table>" : "대표 ROI 격자 비교를 진행 중입니다.";
$("limits").textContent = (data.decision.limitations || ["현재 첫 사진 연결 단계이며 설계 권장안을 제시하지 않습니다."]).join(" ");

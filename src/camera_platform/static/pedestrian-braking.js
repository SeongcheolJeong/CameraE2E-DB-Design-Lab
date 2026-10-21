"use strict";
const $ = id => document.getElementById(id);
let token;

async function responseJSON(response) {
  if (!response.ok) throw Error(await response.text());
  return response.json();
}

async function refresh() {
  const audit = await responseJSON(await fetch("/api/v1/pedestrian-braking-studies/intake"));
  $("intake").textContent = audit.message || "아직 입력 적격성 점검 기록이 없습니다.";
  $("connection").replaceChildren();
  if (audit.connection_manifest_sha256) {
    const line = document.createElement("p");line.textContent = "실제 RAW·고정 ISP·YOLO 첫 프레임 (1280×720 관측창). " + audit.connection_message;$("connection").append(line);
    const base = "/api/v1/pedestrian-braking-studies/connection-files/";
    const context = document.createElement("details");
    const originalLabel = document.createElement("summary");originalLabel.textContent = "원본 도로 사진의 맥락 열기 (계산은 중앙 30° 관측창)";context.append(originalLabel);
    const original = document.createElement("img");original.src = base + "original.jpg";original.alt = "nuScenes 원본 전방 사진";original.style.width = "100%";context.append(original);$("connection").append(context);
    const comparison = document.createElement("div");comparison.style.display = "grid";comparison.style.gridTemplateColumns = "repeat(auto-fit,minmax(min(100%,360px),1fr))";comparison.style.gap = "16px";$("connection").append(comparison);
    for (const id of ["L1-S1", "L2-S1"]) {
      const panel = document.createElement("div");comparison.append(panel);
      const label = document.createElement("p");label.textContent = id;panel.append(label);
      const image = document.createElement("img");image.src = base + id + "/rgb-20261003.png";image.alt = id+" 실제 고정 ISP 출력";image.style.width = "100%";panel.append(image);
    }
    const link = document.createElement("a");link.href = base + "connection-result.json";link.textContent = "원본 계산·검출·seed·기하 기록";$("connection").append(link);
  }
  const jobs = await responseJSON(await fetch("/api/v1/pedestrian-braking-studies"));
  $("jobs").replaceChildren();
  if (!jobs.length) $("jobs").textContent = "아직 제출한 보행자 평가가 없습니다.";
  for (const job of jobs) {
    const spec = job.specification;
    if (!$("path").value && spec) {
      $("path").value = spec.package_path;
      $("sha").value = spec.specification_sha256;
    }
    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = job.name;
    card.append(title);
    const status = document.createElement("p");
    status.textContent = job.status + " · " + spec.stage + " · " + job.id;
    card.append(status);
    const result = job.result?.summary;
    if (result) {
      const a = document.createElement("a");
      a.href = result.viewer_path;
      a.textContent = "시퀀스·인지 시점·제동 여유 열기";
      card.append(a);
    }
    if (["queued", "running"].includes(job.status)) {
      const detail = await responseJSON(await fetch("/api/v1/pedestrian-braking-studies/" + job.id));
      if (detail.study_progress?.message) {
        const progress = document.createElement("p");
        progress.textContent = detail.study_progress.message;
        card.append(progress);
      }
      const button = document.createElement("button");
      button.textContent = "취소";
      button.onclick = async () => {
        try {
          await responseJSON(await fetch("/api/v1/pedestrian-braking-studies/" + job.id + "/cancel", {
            method: "POST",
            headers: { "x-platform-token": token, "Content-Type": "application/json" },
            body: "{}"
          }));
          await refresh();
        } catch (error) { $("message").textContent = error.message; }
      };
      card.append(button);
    }
    if (job.error) {
      const error = document.createElement("p");
      error.textContent = typeof job.error === "string" ? job.error : JSON.stringify(job.error);
      card.append(error);
    }
    $("jobs").append(card);
  }
}

$("refresh").onclick = () => refresh().catch(error => $("message").textContent = error.message);
$("submit").onclick = async () => {
  $("submit").disabled = true;
  try {
    const payload = {
      schema: "camera_pedestrian_braking_job_v1",
      name: "보행자 안정 인지·제동 여유",
      package_path: $("path").value.trim(),
      specification_sha256: $("sha").value.trim(),
      stage: $("stage").value
    };
    await responseJSON(await fetch("/api/v1/pedestrian-braking-studies", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-platform-token": token },
      body: JSON.stringify(payload)
    }));
    $("message").textContent = "제출됐습니다. 작업 목록에서 진행 상태를 확인하세요.";
    await refresh();
  } catch (error) { $("message").textContent = error.message; }
  finally { $("submit").disabled = false; }
};

async function init() {
  const bootstrap = await responseJSON(await fetch("/api/v1/bootstrap"));
  token = bootstrap.token;
  await refresh();
}
init().catch(error => $("message").textContent = error.message);

"use strict";
const $ = id => document.getElementById(id);
let token;

async function responseJSON(response) {
  if (!response.ok) throw Error(await response.text());
  return response.json();
}

async function refresh() {
  const jobs = await responseJSON(await fetch("/api/v1/road-design-studies"));
  $("jobs").replaceChildren();
  if (!jobs.length) $("jobs").textContent = "아직 제출한 도로 평가가 없습니다.";
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
      a.textContent = "원본 위치·RAW·영상·설계 결정 열기";
      card.append(a);
    }
    if (["queued", "running"].includes(job.status)) {
      const detail = await responseJSON(await fetch("/api/v1/road-design-studies/" + job.id));
      if (detail.study_progress?.message) {
        const progress = document.createElement("p");
        progress.textContent = detail.study_progress.message;
        card.append(progress);
      }
      const button = document.createElement("button");
      button.textContent = "취소";
      button.onclick = async () => {
        try {
          await responseJSON(await fetch("/api/v1/road-design-studies/" + job.id + "/cancel", {
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
      schema: "camera_road_design_job_v1",
      name: "도로 사진 설계평가",
      package_path: $("path").value.trim(),
      specification_sha256: $("sha").value.trim(),
      stage: $("stage").value
    };
    await responseJSON(await fetch("/api/v1/road-design-studies", {
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

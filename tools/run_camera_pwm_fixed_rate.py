"""Fixed-50fps RAW filter comparison, including moving edges and light steps.

Reuses saved static acquisitions. New dynamic scenes keep the original optics,
8x8 sensor, trap model, gain=1, 8ms exposure and 20ms frame timing. Only the
declared irradiance sequence changes. Sensor truth is used ONLY for evaluation
masks, never passed to either filter. Writes one NEW study directory.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import html
import json
from pathlib import Path
import sys
import time
from copy import deepcopy

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_camera_pwm_case import ROOT, SOURCE, STATE, encode, evaluate, load_original, sha
from run_camera_pwm_transient import extend_static, relative_span
from camera_platform import electrical_control
from camera_platform.raw_temporal import PairFilterConfig, process_sequence

STATIC = ROOT / "reports/camera_platform/pwm-transient-20260926"
METHODS = ("unprocessed", "pair_mean", "guarded_pair")
SCENARIOS = ("light_step_down", "light_step_up", "moving_edge")
PHASES = (0, 2_000_000, 4_000_000, 6_000_000)


def scene_request(original: dict, scenario: str, phase_ns: int, noise: bool) -> dict:
    """Author explicit zero-order-held irradiance, not interpolation of RAW."""
    request = extend_static(original)
    request.update(noise_enabled=noise, seed=823)
    program = request["capture_program"]
    program["waveform"]["phase_origin_ns"] += phase_ns
    scene0 = request["scene_sequence"]["knots"][0]["scene"]
    base = scene0["spectral_irradiance"]
    times = [f["frame_start_ns"] for f in request["control_sequence"]["frames"]]
    endpoint = request["scene_sequence"]["knots"][-1]["time_ns"]
    scenes = []
    for index, timestamp in enumerate(times):
        if scenario == "light_step_down":
            scale = 1.0 if index < 8 else 0.5
        elif scenario == "light_step_up":
            scale = 0.5 if index < 8 else 1.0
        elif scenario == "moving_edge":
            edge = 2 if index < 8 else (3, 4, 5, 6, 5, 4, 3, 2)[index - 8]
            scale = np.where(np.arange(base.shape[1])[None, :, None] < edge, 1.5, 0.3)
        elif scenario == "real_period_two_light":
            scale = 0.98 if index % 2 == 0 else 1.02
        else:
            raise ValueError("Unknown dynamic scenario")
        scene = deepcopy(scene0)
        scene["spectral_irradiance"] = np.asarray(base * scale, dtype=np.float64)
        scene["source"] = {"kind": "estimated", "description": f"authored-fixed-rate/{scenario}/{index}"}
        scenes.append({"time_ns": timestamp, "scene": scene})
    scenes.append({"time_ns": endpoint, "scene": deepcopy(scenes[-1]["scene"])})
    request["scene_sequence"].update(interpolation="zero_order_hold", knots=scenes)
    if scenario == "real_period_two_light":
        wave = program["waveform"]
        program["waveform"] = {"schema": wave["schema"], "kind": "constant",
                               "clock_id": wave["clock_id"], "level": 1.0}
        policy = program["electrical_control_policy"]
        policy["schema"] = electrical_control.POLICY_SCHEMA
        policy.pop("period_lattice")
        policy["enabled"] = False
    return request


def process(raw: np.ndarray, request: dict, black: float, white: int) -> tuple[dict, np.ndarray, float]:
    frames = request["control_sequence"]["frames"]
    times = [int(f["frame_start_ns"]) for f in frames]
    if np.any(np.diff(times) != 20_000_000):
        raise ValueError("This study must keep the original 50fps cadence")
    keys = [sha(encode({k: v for k, v in f["applied"].items()
                        if k not in ("applied_at_ns", "command_id")})) for f in frames]
    settings = PairFilterConfig(noise_sigma_dn=14.0 if request["noise_enabled"] else 0.0)
    before = sha(raw.tobytes())
    started = time.perf_counter()
    filtered = process_sequence(raw, black_level_dn=black, white_level_dn=white,
                                timestamps_ns=times, cadence_ns=20_000_000,
                                settings_keys=keys, config=settings)
    elapsed = time.perf_counter() - started
    current = raw.astype(float)
    average = current.copy()
    average[1:] = (current[1:] + current[:-1]) / 2
    if sha(raw.tobytes()) != before:
        raise AssertionError("Processing changed original RAW")
    return {"unprocessed": current, "pair_mean": average,
            "guarded_pair": filtered["code_dn"]}, filtered["blend_mask"], elapsed


def evaluate_methods(label: str, scenario: str, phase: int, request: dict,
                     arrays: dict, raw_format: dict, directory: Path) -> list[dict]:
    raw = arrays["raw_groups"][:, 0]
    black, white = raw_format["black_level_dn"], 2**raw_format["bit_depth"] - 1
    outputs, gate, elapsed = process(raw, request, black, white)
    green = np.indices(raw.shape[1:]).sum(axis=0) % 2 == 1
    ideal = arrays["mean_electrons"]
    # Post-optics exposure-integrated photocharge is an evaluation-only oracle.
    # A >5% relative input change defines a moving/step pixel, independent of
    # the RAW filter threshold and of the filtered output.
    change = np.zeros_like(raw, dtype=bool)
    change[1:] = np.abs(ideal[1:] - ideal[:-1]) > 0.05 * np.maximum(
        np.maximum(ideal[1:], ideal[:-1]), 1.0)
    current = outputs["unprocessed"]
    rows = []
    for method, codes in outputs.items():
        meter = (codes[:, green] - black).mean(axis=1)
        added = np.abs(codes - current)
        row = {"case": label, "scenario": scenario, "phase_ns": phase,
               "noise_enabled": bool(request["noise_enabled"]), "seed": int(request["seed"]),
               "method": method, "meter_dn": meter.tolist(),
               "last_eight_span_fraction": relative_span(meter[8:]),
               "last_eight_mean_dn": float(meter[8:].mean()),
               "dynamic_pixel_count": int(change[8:].sum()),
               "dynamic_added_mae_dn": float(added[8:][change[8:]].mean()) if change[8:].any() else None,
               "dynamic_added_max_dn": float(added[8:][change[8:]].max()) if change[8:].any() else None,
               "current_step_retained_fraction": None,
               "original_raw_sha256": sha(raw.tobytes()),
               "source_adc_high_rail_fraction": float(np.mean(raw == white)),
               "guarded_blend_fraction_tail": float(gate[8:].mean()),
               "algorithm_seconds_16_frames": elapsed if method == "guarded_pair" else None}
        if scenario.startswith("light_step"):
            step = float((current[8, green] - current[7, green]).mean())
            row["current_step_retained_fraction"] = float(
                (codes[8, green] - current[7, green]).mean() / step) if step else None
        rows.append(row)
    np.savez_compressed(directory / (label + "-processed.npz"), raw_original=raw,
                        **{m: c for m, c in outputs.items()}, blend_mask=gate,
                        evaluation_change_mask=change)
    return rows


def summarize(rows: list[dict]) -> dict:
    static = [r for r in rows if r["scenario"] == "static" and not r["noise_enabled"]]
    dynamic = [r for r in rows if r["scenario"] in SCENARIOS and not r["noise_enabled"]]
    tests = []
    for phase in PHASES:
        a = next(r for r in static if r["phase_ns"] == phase and r["method"] == "unprocessed")
        b = next(r for r in static if r["phase_ns"] == phase and r["method"] == "guarded_pair")
        tests.append({"criterion": f"static-phase-{phase}", "pass": bool(
            b["last_eight_span_fraction"] <= 0.005
            and b["last_eight_span_fraction"] <= 0.2 * a["last_eight_span_fraction"])})
    for scenario in SCENARIOS:
        for phase in PHASES:
            pair = next(r for r in dynamic if r["scenario"] == scenario and r["phase_ns"] == phase
                        and r["method"] == "pair_mean")
            guard = next(r for r in dynamic if r["scenario"] == scenario and r["phase_ns"] == phase
                         and r["method"] == "guarded_pair")
            passed = guard["dynamic_added_mae_dn"] <= 0.25 * pair["dynamic_added_mae_dn"]
            if scenario.startswith("light_step"):
                passed = passed and 0.9 <= guard["current_step_retained_fraction"] <= 1.1
            tests.append({"criterion": f"{scenario}-phase-{phase}", "pass": bool(passed)})
    return {"criteria": tests, "declared_bounded_criteria_pass": all(t["pass"] for t in tests),
            "physical_lfm_validation": "not_established",
            "universal_scene_preservation": False}


def write_report(directory: Path, result: dict) -> None:
    rows = result["measurements"]
    (directory / "results.json").write_bytes(encode(result))
    with (directory / "metrics.csv").open("w", newline="", encoding="utf-8") as f:
        names = [k for k in rows[0] if k != "meter_dn"]
        writer = csv.DictWriter(f, names, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    lines = ["# 50 fps 유지: 인과적 RAW 처리와 동적 장면 비교", "",
             "## 결론", "",
             f"사전 선언한 제한 조건: **{'통과' if result['decision']['declared_bounded_criteria_pass'] else '미통과'}**.",
             "촬영 20 ms·노출 8 ms·gain 1·원래 sensor/optics/trap을 유지했다. 원본 RAW와 처리된 DN은 별도로 보존한다.",
             "무처리 / 직전 원본과 단순 평균 / 4프레임 원본의 주기 일치 여부로 움직임을 거르는 평균을 같은 RAW에 적용했다.",
             "새 필터는 현재와 3개 과거 RAW만 쓰며 처음 3프레임은 통과시킨다. 미래 프레임·전하 truth는 입력하지 않는다.", "",
             "## 정적 원래 위상(잡음 OFF), 후반 8프레임", "",
             "| 처리 | 상대 변화폭 | 평균 DN |", "|---|---:|---:|"]
    for row in rows:
        if row["scenario"] == "static" and row["phase_ns"] == 0 and not row["noise_enabled"]:
            lines.append(f"| {row['method']} | {100*row['last_eight_span_fraction']:.5f}% | {row['last_eight_mean_dn']:.3f} |")
    lines += ["", "## 동적 장면: 원래 위상·잡음 OFF", "",
              "추가 잔상 오차는 광입력이 바뀐 픽셀에서 현재 미처리 RAW 대비 처리로 추가한 절대 오차다. 실물 정답 영상 오차가 아니다.",
              "| 장면 | 처리 | 추가 잔상 MAE (DN) | 첫 변화 전달률 |", "|---|---|---:|---:|"]
    for row in rows:
        if row["scenario"] in SCENARIOS and row["phase_ns"] == 0 and not row["noise_enabled"]:
            step = row["current_step_retained_fraction"]
            lines.append(f"| {row['scenario']} | {row['method']} | {row['dynamic_added_mae_dn']:.3f} | "
                         + (f"{100*step:.2f}%" if step is not None else "해당 없음") + " |")
    lines += ["", "## 잡음 ON: 네 위상 범위", "",
              "정적 장면은 기존 세 seed의 저장 RAW를 재사용했다. 동적 장면은 새 seed 823을 사용했다.",
              "잡음 sigma=14 DN는 이전 저장 RAW 분석에서 정한 연구 설정이며 실제 센서 교정값이 아니다.",
              "| 처리 | 정적 후반 변화폭 범위 | 움직이는 경계 추가 MAE 범위 |", "|---|---:|---:|"]
    for method in METHODS:
        a = [r["last_eight_span_fraction"] for r in rows if r["scenario"] == "static" and r["noise_enabled"] and r["method"] == method]
        b = [r["dynamic_added_mae_dn"] for r in rows if r["scenario"] == "moving_edge" and r["noise_enabled"] and r["method"] == method]
        lines.append(f"| {method} | {100*min(a):.4f}–{100*max(a):.4f}% | {min(b):.3f}–{max(b):.3f} DN |")
    lines += ["", "## 남는 실패와 대가", "",
              "평균된 픽셀에는 직전 20 ms 전 표본이 50% 섞인다. 명목 정보 나이는 10 ms다. 50 fps 출력과 시간 지연 0은 다르다.",
              "큰 변화는 현재 프레임으로 통과시키지만 임계값보다 작은 움직임/밝기 변화 또는 진짜 2프레임 주기 장면은 잘못 평탄화할 수 있다.",
              "일정 광원에서 실제 장면 밝기를 0.98/1.02로 교대시킨 반례도 제외하지 않고 저장했다:", "",
              "| 처리 | 실제 교대 장면의 후반 상대 변화폭 |", "|---|---:|"]
    for row in rows:
        if row["scenario"] == "real_period_two_light":
            lines.append(f"| {row['method']} | {100*row['last_eight_span_fraction']:.5f}% |")
    lines += ["", "이 반례 때문에 일반 영상 또는 안전 관련 인지 입력에 기본 적용하지 않는다. 밝기 목표 200–300 DN도 별도 과제다.",
              "동적 장면은 8×8 광입력 경계를 프레임별로 명시한 합성 스트레스다. 일반 3D 운동·가림·실제 대상 검출 성능은 평가하지 않았다.",
              "원래 센서의 실측 파라미터, 다주파 PWM, 온도, 실시간 HW 실행, 대형 해상도는 미검증이다.",
              "필터는 독립 opt-in 모듈이며 기존 ISP 프로필·기본 앱·DB·등록 모델은 변경하지 않았다.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    # Lightweight directly-openable visual comparison; no web server needed.
    page = ["<!doctype html><meta charset='utf-8'><title>50 fps RAW comparison</title>",
            "<style>body{font:16px system-ui;max-width:1150px;margin:30px auto;background:#f6f6f6;color:#222}"
            "canvas{width:144px;height:144px;image-rendering:pixelated;margin:4px}section{background:white;padding:20px;margin:20px 0}"
            "pre{white-space:pre-wrap}h1{font-size:30px}</style>",
            "<h1>50 fps · 원본 / 단순 평균 / 변화 감지 평균</h1><p>16프레임. 밝기 표시 범위는 모든 방법에 공통이며 자동 정규화하지 않았습니다. 원본은 8×8입니다.</p>"]
    visual = []
    for scenario in ("static", *SCENARIOS, "real_period_two_light"):
        row = next(r for r in rows if r["scenario"] == scenario and r["phase_ns"] == 0 and not r["noise_enabled"])
        with np.load(directory / (row["case"] + "-processed.npz"), allow_pickle=False) as pack:
            visual.append({"scenario": scenario, "data": {m: pack[m].tolist() for m in METHODS}})
    page += ["<div id='panels'></div><p id='frame'></p><button onclick='playing=!playing'>재생/정지</button>",
             "<script>const scenes=" + json.dumps(visual) + ";const methods=" + json.dumps(METHODS) + ";",
             "let n=0,playing=true;const panels=document.getElementById('panels');"
             "scenes.forEach((s,j)=>{let e=document.createElement('section');e.innerHTML='<h2>'+s.scenario+'</h2>';"
             "methods.forEach((m,k)=>{let d=document.createElement('div');d.style.display='inline-block';d.innerHTML='<div>'+m+'</div><canvas width=8 height=8 id=c'+j+'x'+k+'></canvas>';e.appendChild(d)});panels.appendChild(e)});"
             "function draw(){scenes.forEach((s,j)=>methods.forEach((m,k)=>{let c=document.getElementById('c'+j+'x'+k).getContext('2d');let im=c.createImageData(8,8);s.data[m][n].flat().forEach((v,p)=>{let a=Math.max(0,Math.min(255,(v-64)/1400*255));im.data.set([a,a,a,255],4*p)});c.putImageData(im,0,0)}));document.getElementById('frame').textContent='프레임 '+n+' · 시각 '+(n*20)+' ms (느린 재생)';}"
             "draw();setInterval(()=>{if(playing){n=(n+1)%16;draw()}},400);</script>",
             "<section><h2>결과와 한계</h2><pre>" + html.escape("\n".join(lines)) + "</pre></section>"]
    (directory / "comparison.html").write_text("\n".join(page), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); directory = args.output.resolve()
    if ROOT not in directory.parents or directory.exists():
        parser.error("Use a NEW output directory inside the checkout")
    _, original, historical_raw, evidence, source_hash, _ = load_original(SOURCE, STATE)
    directory.mkdir(parents=True, exist_ok=False)
    plan = {"source_record_sha256": source_hash, "cadence_ns": 20_000_000,
            "exposure_ns": 8_000_000, "gain": 1, "filter_config_noise_off": asdict(PairFilterConfig()),
            "filter_config_noise_on": asdict(PairFilterConfig(noise_sigma_dn=14)),
            "noise_estimate_basis": "prior saved RAW RMS approximately 13..14 DN; fixed before this study",
            "static_input": str(STATIC.relative_to(ROOT)), "phases_ns": PHASES,
            "dynamic_scenarios": SCENARIOS, "dynamic_noise_seed": 823,
            "criteria_before_results": {"static_noise_off": "tail span <=0.5% and >=80% lower at all four phases",
                "dynamic_noise_off": "added MAE <=25% of unguarded pair mean in moving/step input pixels",
                "light_step_noise_off": "first changed frame retains 90..110% of current RAW step",
                "known_ambiguity": "publish real period-two scene distortion; do not claim universal mitigation",
                "latency": "no future frames, 3-frame warmup, 10ms information age at blended pixels"}}
    (directory / "study-plan.json").write_bytes(encode(plan))
    result = {"plan": plan, "status": "running", "measurements": [],
              "new_runtime_acquisitions": 0, "reused_static_acquisitions": 0,
              "source_version": "0.16.1.dev19 working tree + opt-in raw_temporal module"}
    try:
        cached = json.loads((STATIC / "results.json").read_bytes())
        for saved in cached["cases"]:
            if saved["variant"] != "original" or saved["refine"]:
                continue
            with np.load(STATIC / (saved["case"] + ".npz"), allow_pickle=False) as pack:
                arrays = {k: pack[k].copy() for k in pack.files}
            if sha(arrays["raw_groups"].tobytes()) != saved["raw_array_sha256"]:
                raise AssertionError("Saved static RAW differs")
            request = extend_static(original)
            request.update(noise_enabled=saved["noise_enabled"], seed=saved["seed"])
            if saved["phase_ns"] == 0 and not saved["noise_enabled"]:
                if not np.array_equal(arrays["raw_groups"][:4], historical_raw):
                    raise AssertionError("Static acquisition lost original RAW prefix")
            label = "static-" + saved["case"]
            result["measurements"] += evaluate_methods(label, "static", saved["phase_ns"], request,
                arrays, {"black_level_dn": 64, "bit_depth": 12}, directory)
            result["reused_static_acquisitions"] += 1
        for scenario in (*SCENARIOS, "real_period_two_light"):
            for noise in ((False,) if scenario == "real_period_two_light" else (False, True)):
                for phase in ((0,) if scenario == "real_period_two_light" else PHASES):
                    request = scene_request(original, scenario, phase, noise)
                    output, _ = evaluate(request)
                    result["new_runtime_acquisitions"] += 1
                    if not np.all(output["arrays"]["applied_exposure_ns"] == 8_000_000):
                        raise AssertionError("Acquisition exposure changed")
                    label = f"{scenario}-p{phase}-n{int(noise)}"
                    np.savez_compressed(directory / (label + "-acquired.npz"), **output["arrays"])
                    (directory / (label + "-input.npz")).write_bytes(b"")
                    np.savez_compressed(directory / (label + "-input.npz"),
                        irradiance=np.stack([k["scene"]["spectral_irradiance"] for k in request["scene_sequence"]["knots"]]),
                        knot_times_ns=np.asarray([k["time_ns"] for k in request["scene_sequence"]["knots"]]))
                    result["measurements"] += evaluate_methods(label, scenario, phase, request,
                        output["arrays"], output["summary"]["raw_formats"][0], directory)
                    print("computed", label, flush=True)
        result["decision"] = summarize(result["measurements"])
        for checksum, path in evidence.read_hashes.items():
            if sha(Path(path).read_bytes()) != checksum:
                raise AssertionError("Historical input evidence changed")
        if sha(SOURCE.read_bytes()) != source_hash:
            raise AssertionError("Source record changed")
        result.update(status="completed", original_source_unchanged=True)
        write_report(directory, result)
        print("RESULT", directory / "report.md", flush=True)
        return 0
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (directory / "failed.json").write_bytes(encode(result))
        raise


if __name__ == "__main__":
    raise SystemExit(main())

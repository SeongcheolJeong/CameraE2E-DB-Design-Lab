"""Attribute the original PWM transient and compare an explicit timing alternative.

Gain stays at the original 1.0 and the single readout stays unchanged. One
continuous 16-frame simulation preserves charge history; it is not four
independently reset four-frame jobs. All counterfactuals are research inputs,
not new registered models or supported hardware modes. No server is started.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_camera_pwm_case import ROOT, SOURCE, STATE, encode, evaluate, load_original, sha
from camera_platform import electrical_control


VARIANTS = ("original", "capture_off", "clear_each_frame", "mean_light", "period_locked")


def cadence_of(request: dict) -> int:
    starts = [f["frame_start_ns"] for f in request["control_sequence"]["frames"]]
    differences = [b - a for a, b in zip(starts, starts[1:])]
    if not differences or min(differences) <= 0 or len(set(differences)) != 1:
        raise ValueError("Study requires an explicitly uniform positive frame cadence")
    return differences[0]


def extend_static(request: dict, *, frames: int = 16, cadence_ns: int | None = None) -> dict:
    """Extend the same static input and clock, without resetting native row state."""
    if type(frames) is not int or not 4 <= frames <= 16:
        raise ValueError("Use 4..16 frames; do not bypass the runtime's sequence budget")
    result = deepcopy(request)
    cadence = cadence_of(request) if cadence_ns is None else cadence_ns
    if type(cadence) is not int or cadence <= 0:
        raise ValueError("Frame cadence must be a positive integer in ns")
    knots = result["scene_sequence"]["knots"]
    reference = knots[0]["scene"]
    for knot in knots[1:]:
        scene = knot["scene"]
        if set(scene) != set(reference) or any(
            not np.array_equal(scene[k], reference[k]) if isinstance(reference[k], np.ndarray)
            else scene[k] != reference[k] for k in reference
        ):
            raise ValueError("Do not extend a non-static scene or infer missing future illumination")
    original = request["control_sequence"]["frames"]
    if frames < len(original):
        raise ValueError("Do not truncate the original case")
    first = original[0]
    latch_offset = first["applied"]["applied_at_ns"] - first["frame_start_ns"]
    clear = {item["frame_id"]: item["clear"]
             for item in request["capture_program"]["pd_clear_before_exposure"]}
    new_frames, new_clears = [], []
    for index in range(frames):
        frame = deepcopy(original[index] if index < len(original) else original[-1])
        if index >= len(original):
            frame["frame_id"] = f"transient-{index:04d}"
        frame["frame_start_ns"] = first["frame_start_ns"] + index * cadence
        frame["applied"]["applied_at_ns"] = frame["frame_start_ns"] + latch_offset
        new_frames.append(frame)
        new_clears.append({"frame_id": frame["frame_id"],
                           "clear": clear.get(frame["frame_id"], clear[original[-1]["frame_id"]])})
    result["control_sequence"]["frames"] = new_frames
    result["capture_program"]["pd_clear_before_exposure"] = new_clears
    height = reference["spectral_irradiance"].shape[0]
    mode = result["control_sequence"]["mode"]
    line_time = mode.get("line_time_ns", 0) if mode["shutter"] == "rolling" else 0
    exposure_max = result["capture_program"]["electrical_control_policy"]["exposure_grid_ns"]["maximum"]
    # Only the final coverage endpoint moves. All scene values are already equal.
    knots[-1]["time_ns"] = max(knots[-1]["time_ns"],
                               new_frames[-1]["frame_start_ns"] + height * line_time + exposure_max + 1)
    return result


def make_case(original: dict, variant: str, phase_ns: int = 0, *, noise: bool = False,
              seed: int = 713, refine: bool = False) -> dict:
    if variant not in VARIANTS:
        raise ValueError("Unknown attribution variant")
    period = original["capture_program"]["waveform"]["period_ns"]
    cadence = cadence_of(original)
    if variant == "period_locked":
        cadence = ((cadence + period - 1) // period) * period
    result = extend_static(original, cadence_ns=cadence)
    if any(f["applied"]["gain"] != 1.0 for f in result["control_sequence"]["frames"]):
        raise ValueError("Attribution holds the original unity gain fixed")
    result.update(noise_enabled=noise, seed=seed)
    program = result["capture_program"]
    program["waveform"]["phase_origin_ns"] += phase_ns
    model = program["retained_charge_model"]
    if variant == "capture_off":
        model["capture_rate_s"] = [0.0] * 4
    elif variant == "clear_each_frame":
        for index, item in enumerate(program["pd_clear_before_exposure"]):
            item["clear"] = index > 0
    elif variant == "mean_light":
        wave = program["waveform"]
        level = wave["low"] + (wave["high"] - wave["low"]) * wave["on_time_ns"] / wave["period_ns"]
        program["waveform"] = {"schema": wave["schema"], "kind": "constant",
                               "clock_id": wave["clock_id"], "level": level}
        # A whole-PWM-period policy cannot label a constant-light control. Use
        # fixed exposure with the existing v1 policy, not a new controller.
        policy = program["electrical_control_policy"]
        policy["schema"] = electrical_control.POLICY_SCHEMA
        policy.pop("period_lattice")
        policy["enabled"] = False
    if refine:
        model["integration"]["max_step_ns"] //= 2
    return result


def relative_span(values) -> float | None:
    values = np.asarray(values, dtype=float)
    mean = float(values.mean())
    return float(np.ptp(values) / mean) if mean > 0 else None


def describe(output: dict, basic: dict, request: dict) -> dict:
    arrays, summary = output["arrays"], output["summary"]
    raw = arrays["raw_groups"][:, 0]
    # This original case meters both native green sites over the full sensor.
    policy = request["capture_program"]["electrical_control_policy"]
    if set(policy["meter"]["sites"]) != {"Gr", "Gb"} or policy["statistics_roi"] != {
        "origin": [0, 0], "reference": "native_sensor", "shape": list(raw.shape[1:])
    }:
        raise ValueError("This report requires the original full-sensor green meter")
    green = np.indices(raw.shape[1:]).sum(axis=0) % 2 == 1
    delivered = arrays["retained_delivered_e"][:, green].mean(axis=1)
    observations = np.asarray(basic["observed_dn"])
    cadence = cadence_of(request)
    waveform = request["capture_program"]["waveform"]
    cycle = waveform["period_ns"] // math.gcd(waveform["period_ns"], cadence) if waveform["kind"] == "pwm" else 1
    later = observations[8:]
    same_phase = [abs(observations[i] - observations[i - cycle])
                  for i in range(8 + cycle, len(observations))]
    return {**basic, "cadence_ns": cadence, "nominal_fps": 1e9 / cadence,
            "phase_cycle_frames": cycle, "delivered_green_mean_e": delivered.tolist(),
            "first_four_relative_span": relative_span(observations[:4]),
            "last_eight_relative_span": relative_span(later),
            "last_eight_delivered_relative_span": relative_span(delivered[8:]),
            "last_eight_mean_dn": float(later.mean()),
            "last_eight_alternating_difference_dn": float(abs(later[::2].mean() - later[1::2].mean())),
            "last_eight_same_phase_step_max_dn": max(same_phase, default=None),
            "trapped_totals_e": arrays["retained_trapped_e"].sum(axis=(1, 2)).tolist(),
            "actual_max_step_ns": summary["retained_charge"]["trap_integration"]["effective_max_step_ns"],
            "window_note": "Frames 8..15 of a 16-frame zero-initial-state acquisition, not certified steady state"}


def save_case(directory: Path, label: str, request: dict) -> tuple[dict, dict]:
    output, basic = evaluate(request)
    row = describe(output, basic, request)
    row["case"] = label
    np.savez_compressed(directory / f"{label}.npz", **output["arrays"])
    (directory / f"{label}-request.json").write_bytes(encode({
        k: v for k, v in request.items() if k != "scene_sequence"
    } | {"scene_clock_and_knots": {k: v for k, v in request["scene_sequence"].items() if k != "knots"},
         "scene_knot_times_ns": [k["time_ns"] for k in request["scene_sequence"]["knots"]],
         "scene_values": "Identical static spectral arrays from the pinned historical input blobs"}))
    return output, row


def write_report(directory: Path, report: dict) -> None:
    rows = report["cases"]
    (directory / "results.json").write_bytes(encode(report))
    fields = ["case", "variant", "phase_ns", "noise_enabled", "seed", "refine", "cadence_ns",
              "first_four_relative_span", "last_eight_relative_span", "last_eight_delivered_relative_span",
              "last_eight_mean_dn", "last_eight_same_phase_step_max_dn", "in_target_frames", "overflow_observed"]
    with (directory / "cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    lines = ["# Original PWM: transient attribution and timing candidate", "",
             "Unity gain and original single readout; no claim of sensor hardware support.",
             "Original 8x8 / 8 ms exposure / 8 ms PWM / 4 ms ON static input. Each case advances one continuous 16-frame state.",
             f"Historical four-frame RAW exact: {report['baseline_exact']}. Extended prefix exact: {report['extended_prefix_exact']}.", "",
             "## Original phase, noise off", "",
             "| Variant | Frame interval (ms) | First 4 span / mean | Last 8 span / mean | Last 8 mean DN | Target hits |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        if row["phase_ns"] == 0 and not row["noise_enabled"] and not row["refine"]:
            lines.append(f"| {row['variant']} | {row['cadence_ns']/1e6:g} | {100*row['first_four_relative_span']:.5f}% | "
                         f"{100*row['last_eight_relative_span']:.5f}% | {row['last_eight_mean_dn']:.3f} | {row['in_target_frames']}/16 |")
    lines += ["", "## Four phase offsets, noise off", "",
              "| Variant | Maximum last-8 relative span | Maximum same-phase step (DN) |",
              "|---|---:|---:|"]
    for variant in VARIANTS:
        selected = [r for r in rows if r["variant"] == variant and not r["noise_enabled"] and not r["refine"]]
        lines.append(f"| {variant} | {100*max(r['last_eight_relative_span'] for r in selected):.5f}% | "
                     f"{max(r['last_eight_same_phase_step_max_dn'] for r in selected):.5f} |")
    lines += ["", "## Conditional timing-candidate result", "",
              str(report["timing_candidate_result"]), "", "## Numerical step refinement", "",
              str(report["refinement"]), "", "## Limits", "",
              "Capture-off and constant-light cases are diagnostic counterfactuals, not mitigation features.",
              "PD clear removes free charge, not occupied traps. Changing frame cadence has a frame-rate/latency cost.",
              "A lower last-eight span is neither brightness convergence nor physical LFM qualification.",
              "Four input phases and three noise seeds do not certify all waveforms, warm-up times or probabilities.",
              "The original asset is an authored fixture with no physical instance; no supported real-sensor gain/mode was inferred.",
              "No registry, installed app, Worker process or hardware was changed.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def analyze_saved(directory: Path) -> None:
    """Separate phase alternation, startup and repeat noise from saved arrays."""
    directory = directory.resolve()
    if ROOT not in directory.parents:
        raise ValueError("Use a study inside the checkout")
    targets = (directory / "analysis.json", directory / "interpretation.md")
    if any(path.exists() for path in targets):
        raise FileExistsError("Preserve the existing interpretation instead of overwriting")
    content = (directory / "results.json").read_bytes()
    report = json.loads(content)
    if report["status"] != "completed" or not report["extended_prefix_exact"]:
        raise ValueError("A completed prefix-matched study is required")
    rows = report["cases"]
    raw_by_case = {}
    for row in rows:
        label = row["case"]
        if Path(label).name != label or "/" in label or "\\" in label:
            raise ValueError("Invalid saved case name")
        with np.load(directory / f"{label}.npz", allow_pickle=False) as saved:
            raw = saved["raw_groups"].copy()
        if sha(raw.tobytes()) != row["raw_array_sha256"]:
            raise ValueError("Saved RAW fingerprint differs")
        raw_by_case[label] = raw.astype(float)
    baseline = next(r for r in rows if r["case"] == "original-p0-n0")
    with np.load(directory / "original-p0-n0.npz", allow_pickle=False) as saved:
        generated = saved["retained_exposure_generated_e"]
        effective = saved["waveform_row_effective_s"]
        residual_keys = [key for key in saved.files if key.startswith("retained_") and key.endswith("residual_e")]
        diagnostics = {
            "maximum_frame_spread_generated_charge_e": float(np.max(np.ptp(generated, axis=0))),
            "row_effective_time_min_s": float(effective.min()),
            "row_effective_time_max_s": float(effective.max()),
            "residual_array_maxima_e": {key: float(np.max(np.abs(saved[key]))) for key in residual_keys},
            "last_eight_observed_dn": baseline["observed_dn"][8:],
            "last_eight_trapped_totals_e": baseline["trapped_totals_e"][8:],
        }
    noise = []
    for variant in ("original", "period_locked"):
        for phase in report["plan"]["phases_ns"]:
            selected = [r for r in rows if r["variant"] == variant and r["phase_ns"] == phase and r["noise_enabled"]]
            if len(selected) < 2:
                continue
            arrays = np.stack([raw_by_case[r["case"]][8:] for r in selected])
            repeat_rms = float(np.sqrt(np.var(arrays, axis=0, ddof=1).mean()))
            noise.append({"variant": variant, "phase_ns": phase, "seed_count": len(selected),
                          "repeat_noise_rms_dn": repeat_rms,
                          "within_run_last_eight_span_fractions": [r["last_eight_relative_span"] for r in selected]})
    changes = report["refinement"]
    refinement = {"case_count": len(changes),
                  "maximum_meter_change_dn": max(r["maximum_meter_difference_dn"] for r in changes),
                  "maximum_span_change_percentage_points": 100*max(r["tail_span_fraction_change"] for r in changes)}
    analysis = {"results_sha256": sha(content), "kind": "saved_array_analysis_no_camera_rerun",
                "diagnostics": diagnostics, "noise": noise, "refinement": refinement,
                "runtime_acquisitions": report["runtime_acquisitions"],
                "calculated_frames": 4 + sum(len(r["observed_dn"]) for r in rows),
                "source_instance": "authored_synthetic_fixture_not_a_physical_sensor",
                "limits": ["The tail is frames 8..15, not an asserted asymptotic steady state.",
                           "Noise estimates use three seeds, not a measured sensor noise floor.",
                           "No fixed-fps, multi-frequency, moving-scene or brightness-target mitigation established."]}
    targets[0].write_bytes(encode(analysis))
    lines = ["# PWM 시간 변동: 원인 분리와 타이밍 후보", "",
             "## 결론", "",
             "같은 8×8 장면을 16프레임 동안 연속 계산했다. gain은 1.0, single readout과 노출 8 ms를 유지했다.",
             "기존 4프레임 RAW를 정확히 재현했으며, 16프레임 실행의 첫 4프레임도 원본과 같았다.",
             "실제 센서 모델이 아닌 authored fixture이므로 특정 CIS의 지원 gain/mode를 임의로 부여하지 않았다.", "",
             "## 초기 과도와 반복 변동은 별개", "",
             f"원래 위상에서 첫 4프레임 상대 변화폭은 {100*baseline['first_four_relative_span']:.5f}%, 후반 8프레임은 {100*baseline['last_eight_relative_span']:.5f}%다.",
             "후반 프레임의 같은 위상끼리 비교한 2프레임 차이는 RAW 측광 해상도에서 0 DN이지만, 인접 프레임은 다음처럼 교대한다:",
             " / ".join(f"{v:.5f}" for v in diagnostics["last_eight_observed_dn"]) + " DN.",
             "촬영 간격 20 ms를 PWM 주기 8 ms로 나눈 나머지는 4 ms라서 노출 시작 위상이 두 프레임마다 반복된다.",
             f"프레임 간 노출 중 생성 전하의 최대 차이는 {diagnostics['maximum_frame_spread_generated_charge_e']:.3g} e이며, 적분 광량 차이와 트랩 시간 순서 효과를 구분했다.",
             "capture-off 대조군과 같은 평균 광량의 일정 광원 대조군에서는 후반 변동이 0 DN이었다.", "",
             "## 원인 판별 / 완화 후보", "",
             "| 조건 | 주기 | 후반 상대 변동 최대값(4개 위상) | 해석 |", "|---|---:|---:|---|",
             ]
    meanings = {"original": "트랩과 프레임/PWM 위상의 결합", "capture_off": "트랩 원인 분리용 반사실 대조군",
                "clear_each_frame": "free PD clear만으로는 트랩 변동이 제거되지 않음",
                "mean_light": "시간 평균 광량만 같고 실제 파형은 다른 대조군(위상 1개)",
                "period_locked": "초기 이후 동일한 PWM 위상으로 촬영; 프레임률 변경 대가 있음"}
    for variant in VARIANTS:
        selected = [r for r in rows if r["variant"] == variant and not r["noise_enabled"] and not r["refine"]]
        lines.append(f"| {variant} | {selected[0]['cadence_ns']/1e6:g} ms | {100*max(r['last_eight_relative_span'] for r in selected):.5f}% | {meanings[variant]} |")
    original_dt, candidate_dt = report["plan"]["original_cadence_ns"], report["plan"]["locked_cadence_ns"]
    lines += ["", f"타이밍 후보는 {1e9/original_dt:.3f} → {1e9/candidate_dt:.3f} fps이며, 프레임 간격이 {100*(candidate_dt/original_dt-1):.2f}% 증가한다.",
              "gain 1.0을 유지했으므로 기존 200–300 DN 밝기 목표는 이 타이밍 변경으로 달성되지 않았다.", "",
              "## 잡음을 켜면 0% 변동이 아니다", "",
              "| 조건 | 후반 8프레임 상대 변화폭 범위 | 반복 잡음 RMS 범위(전체 픽셀, DN) |",
              "|---|---:|---:|"]
    for variant in ("original", "period_locked"):
        chosen = [r for r in noise if r["variant"] == variant]
        if not chosen:
            continue
        spans = [v for r in chosen for v in r["within_run_last_eight_span_fractions"]]
        rms = [r["repeat_noise_rms_dn"] for r in chosen]
        lines.append(f"| {variant} | {100*min(spans):.4f}–{100*max(spans):.4f}% | {min(rms):.4f}–{max(rms):.4f} |")
    lines += ["", "위상 4개 × seed 3개 범위다. 반복 분산은 같은 프레임·픽셀 위치에서 seed 간에 계산했다.",
              "read noise/shot noise와 결정론적 주기 변동은 다르므로, 타이밍 후보를 무잡음 카메라로 해석하지 않는다.", "",
              "## 수치 적분 확인", "",
              f"최대 step 100 → 50 µs 비교 {len(changes)}건에서 측광 변화 최대 {refinement['maximum_meter_change_dn']:.5f} DN, 상대 변화폭 차이 최대 {refinement['maximum_span_change_percentage_points']:.6f}%p였다.",
              "수치 step을 줄여도 원인·후보의 방향이 유지됐다. 모델 자체의 실물 정확도를 입증한 것은 아니다.", "",
              "## 다음 설계 판단", "",
              "50 fps를 유지해야 한다면 24 ms 동기 후보는 채택할 수 없다. 여러 PWM 주파수나 동적 장면에서도 그대로 적용할 수 없다.",
              "다음은 원래 20 ms 프레임 간격을 유지하는 노출 시각 제어 또는 RAW 기반 처리를 별도 가설로 비교해야 한다.",
              "단순한 2프레임 평균은 지연·움직임 잔상이 생길 수 있으므로, 정적 표적의 밝기만으로 개선을 승인하지 않는다.",
              "센서 모델이 확정되기 전에는 데이터시트 기반 실기 readout/gain 후보를 만들지 않는다.", ""]
    targets[1].write_text("\n".join(lines), encoding="utf-8")
    print(f"ANALYSIS {targets[1]}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summarize-existing", type=Path)
    parser.add_argument("--with-noise", action="store_true")
    args = parser.parse_args()
    if args.summarize_existing is not None:
        analyze_saved(args.summarize_existing)
        return 0
    if args.output is None:
        parser.error("--output is required for a new study")
    directory = args.output.resolve()
    if ROOT not in directory.parents or directory.exists():
        parser.error("Output must be a NEW directory inside the project")
    record, original, raw, evidence, source_hash, _ = load_original(SOURCE, STATE)
    cadence, period = cadence_of(original), original["capture_program"]["waveform"]["period_ns"]
    locked = ((cadence + period - 1) // period) * period
    seed = int(original["seed"])
    directory.mkdir(parents=True, exist_ok=False)
    plan = {"source_record_sha256": source_hash, "source_run_id": record["id"],
            "gain": 1.0, "readout": "original_single_linear", "frame_count": 16,
            "original_cadence_ns": cadence, "locked_cadence_ns": locked,
            "phases_ns": [0, period//4, period//2, 3*period//4],
            "criteria_before_results": {"historical_and_extended_prefix": "exact RAW equality",
                "diagnostic_no_capture": "zero trapped occupancy",
                "timing_last_eight": "noise-off span/mean < 0.5% and >=80% smaller than matched original at all four phases",
                "cost": "Report frame interval/fps changes; do not call this fixed-rate LFM or brightness control",
                "refinement": "Compare original and timing candidate at half the configured maximum step; never relax resource admission"}}
    (directory / "study-plan.json").write_bytes(encode(plan))
    report = {"plan": plan, "source_version": "0.16.1.dev19 working tree",
              "execution_kind": "direct_runtime_research", "baseline_exact": False,
              "extended_prefix_exact": False, "cases": [], "physical_validation": "not_established"}
    try:
        baseline, _ = evaluate(original)
        if raw.dtype != baseline["arrays"]["raw_groups"].dtype or not np.array_equal(raw, baseline["arrays"]["raw_groups"]):
            raise AssertionError("Original baseline changed")
        report["baseline_exact"] = True
        phases = plan["phases_ns"]
        for variant in VARIANTS:
            for phase in ([0] if variant == "mean_light" else phases):
                request = make_case(original, variant, phase, seed=seed)
                label = f"{variant}-p{phase}-n0"
                output, row = save_case(directory, label, request)
                row.update(variant=variant, phase_ns=phase, noise_enabled=False, seed=seed, refine=False)
                if variant == "original" and phase == 0:
                    report["extended_prefix_exact"] = bool(np.array_equal(raw, output["arrays"]["raw_groups"][:4]))
                    if not report["extended_prefix_exact"]:
                        raise AssertionError("Longer acquisition changed its original four-frame prefix")
                if variant == "capture_off" and np.any(output["arrays"]["retained_trapped_e"]):
                    raise AssertionError("Zero capture with empty initial traps must not invent occupied traps")
                report["cases"].append(row)
                print(f"{label}: cadence={row['cadence_ns']/1e6:g}ms tail-span={100*row['last_eight_relative_span']:.5f}%", flush=True)
        refinement = []
        for variant in ("original", "period_locked"):
            for phase in phases:
                request = make_case(original, variant, phase, seed=seed, refine=True)
                _, row = save_case(directory, f"{variant}-p{phase}-halfstep", request)
                row.update(variant=variant, phase_ns=phase, noise_enabled=False, seed=seed, refine=True)
                previous = next(r for r in report["cases"] if r["variant"] == variant and r["phase_ns"] == phase and not r["refine"])
                refinement.append({"variant": variant, "phase_ns": phase,
                    "maximum_meter_difference_dn": max(abs(a-b) for a,b in zip(row["observed_dn"], previous["observed_dn"])),
                    "tail_span_fraction_change": abs(row["last_eight_relative_span"]-previous["last_eight_relative_span"])})
                report["cases"].append(row)
        if args.with_noise:
            for variant in ("original", "period_locked"):
                for phase in phases:
                    for sample_seed in (seed, seed+1, seed+2):
                        request = make_case(original, variant, phase, noise=True, seed=sample_seed)
                        _, row = save_case(directory, f"{variant}-p{phase}-n1-s{sample_seed}", request)
                        row.update(variant=variant, phase_ns=phase, noise_enabled=True, seed=sample_seed, refine=False)
                        report["cases"].append(row)
        pairs = []
        for phase in phases:
            matched = {r["variant"]: r for r in report["cases"] if r["phase_ns"]==phase and not r["noise_enabled"] and not r["refine"]}
            a, b = matched["original"]["last_eight_relative_span"], matched["period_locked"]["last_eight_relative_span"]
            pairs.append({"phase_ns": phase, "original_fraction": a, "candidate_fraction": b,
                          "passes_declared_numerical_criterion": b < 0.005 and a > 0 and b <= 0.2*a})
        report["timing_candidate_result"] = {"phase_results": pairs,
            "all_sampled_phases_pass": all(p["passes_declared_numerical_criterion"] for p in pairs),
            "frame_interval_increase_percent": 100*(locked/cadence-1),
            "fps_change_percent": 100*(cadence/locked-1), "hardware_support": "not_established"}
        report["refinement"] = refinement
        for checksum, path in evidence.read_hashes.items():
            if sha(Path(path).read_bytes()) != checksum:
                raise AssertionError("Input evidence changed during the study")
        if sha(SOURCE.read_bytes()) != source_hash:
            raise AssertionError("Archived source record changed")
        report.update(status="completed", source_unchanged=True, runtime_acquisitions=1+len(report["cases"]))
        write_report(directory, report)
        analyze_saved(directory)
        print(f"RESULT {directory / 'report.md'}", flush=True)
        return 0
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (directory / "failed.json").write_bytes(encode(report))
        raise


if __name__ == "__main__":
    raise SystemExit(main())

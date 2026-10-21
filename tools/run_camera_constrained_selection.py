"""Choose a finite prescription/exposure/gain design, then open held-out cases.

Read archived optical assets only. Freeze a numerical engineering brief before
acquisition, freeze the training winner before held-out input construction, and
never rerank candidates on held-out failures. No registry or server is opened.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from camera_platform.constrained_selection import SelectionLimits, select_candidate, summarize_output, violations
from camera_platform.prescription_response import require_same_design_scene
from camera_platform.response_chain import evaluate_response
from run_camera_prescription_comparison import load_cases, reference_photo, reference_raw


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenarios() -> tuple[list[dict], list[dict]]:
    train = [{"scenario_id": f"train-L{scale:g}", "split": "train", "kind": "original",
              "scale": scale, "domain": "selection"} for scale in (0.5, 2.0, 6.0)]
    held_out = [
        {"scenario_id": "holdout-mirror-L0.75", "split": "holdout", "kind": "mirror",
         "scale": 0.75, "domain": "within_training_per_band_peak_envelope"},
        {"scenario_id": "holdout-rotate-L3", "split": "holdout", "kind": "rotate",
         "scale": 3.0, "domain": "within_training_per_band_peak_envelope"},
        {"scenario_id": "holdout-spectrum-L2", "split": "holdout", "kind": "spectrum",
         "scale": 2.0, "band_factors": [0.6, 1.4],
         "domain": "within_training_per_band_peak_envelope"},
        {"scenario_id": "stress-local-highlight-L20", "split": "holdout", "kind": "highlight",
         "scale": 1.0, "highlight_peak_factor": 20.0,
         "domain": "outside_training_per_band_peak_envelope"},
    ]
    return train, held_out


def scenario_request(case, specification: dict) -> dict:
    """Transform ideal radiance on the same grid, then retain throughput ONCE.

    Each stop's archived ideal input equals its E/L times one shared radiance.
    Linear spatial/band operations commute with its per-band on-axis E/L, so
    they define the same changed radiance for all stops without dividing or
    multiplying a second throughput factor. This is not a new 3-D rendering.
    """
    request = case.rate_request()
    image = np.array(request["scene"]["spectral_irradiance"], dtype=float, copy=True)
    kind = specification["kind"]
    if kind == "mirror":
        image = image[:, ::-1].copy()
    elif kind == "rotate":
        if image.shape[0] != image.shape[1]:
            raise ValueError("Quarter rotation needs a square native input")
        image = np.rot90(image, axes=(0, 1)).copy()
    elif kind == "spectrum":
        factors = np.asarray(specification["band_factors"], dtype=float)
        if factors.shape != (image.shape[2],):
            raise ValueError("Hold-out spectral factors must match the supplied bands")
        image *= factors
    elif kind == "highlight":
        h, w, _ = image.shape
        peak = image.max(axis=(0, 1)) * specification["highlight_peak_factor"]
        image[h//2-2:h//2+2, w//2-2:w//2+2] = peak
    elif kind != "original":
        raise ValueError("Unknown predeclared scenario")
    image *= specification["scale"]
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["source"] = {"kind": "estimated", "description": (
        "Declared ideal-plane transformation of archived shared radiance: "
        + specification["scenario_id"] + "; same native grid and per-band E/L applied once; not re-rendered 3D")}
    return request


def candidate_grid() -> list[dict]:
    return [{"candidate_id": f"{stop}-e{exposure:g}-g{gain:g}", "stop": stop,
             "exposure_ms": exposure, "gain": gain}
            for stop in ("clear", "quarter", "radial")
            for exposure in (1.0, 2.0, 4.0, 8.0) for gain in (1.0, 2.0, 4.0)]


def draw_reports(directory: Path, results: dict) -> None:
    from PIL import Image

    decision, limits = results["selection"], results["plan"]["limits"]
    winner = decision["selected_candidate_id"]
    lines = ["# 처방·노출·gain: 제약 선택과 미사용 입력 평가", "",
        f"선택 후보: **{winner or '없음 — 현재 격자에서 제약 동시 만족 불가'}**.",
        f"훈련 입력에서 적합 후보 {decision['feasible_count']}/{decision['candidate_count']}개.",
        "후보를 먼저 고정하고 hold-out을 계산했다. 평가 실패 후 후보·문턱값을 바꾸지 않았다.", "",
        "## 사전에 고정한 연구 목표", "",
        f"Green 평균 ≥ {limits['minimum_green_dn']:g} DN; green 픽셀의 예측 SNR 하위 10% 분위 ≥ {limits['minimum_green_snr_p10']:g}.",
        f"Well/최종 RAW 상단 비율 각각 ≤ {100*limits['maximum_well_fraction']:g}% / {100*limits['maximum_white_fraction']:g}%; 노출 ≤ {limits['maximum_exposure_ms']:g} ms.",
        "합성 연구용 브리프이며 고객 요구/실제 센서 지원값이 아니다. SNR은 광전하·shot/dark/read/post-gain/양자화 모델 계산값이다.",
        "목적은 최악 입력의 SNR 하위 10% 분위 최대화이며, 동률이면 짧은 노출·낮은 gain·ID 순으로 정한다.", "",
        "## 훈련 입력 선택 결과", "",
        "| 후보 | 적합 | 최소 Green DN | 최악 SNR p10 | 최대 well | 최대 RAW 상단 | 탈락 이유 |",
        "|---|---|---:|---:|---:|---:|---|"]
    for row in decision["candidates"]:
        if row["evaluated_scenarios"]:
            lines.append(f"| {row['candidate_id']} | {'예' if row['feasible'] else '아니오'} | {row['minimum_green_dn']:.3f} | {row['worst_green_snr_p10']:.3f} | {100*row['maximum_well_fraction']:.3f}% | {100*row['maximum_white_fraction']:.3f}% | {', '.join(row['failures']) or '—'} |")
    lines += ["", "8 ms 후보 9개는 노출 상한 때문에 계산 전에 제외했다. 검사 자체가 아닌 실제 계산 결과로 나머지를 판정했다.",
        "", "## 선택에 쓰지 않은 입력: 잡음 OFF", "",
        "| 입력 | 역할 / 후보 | Green DN | SNR p10 | Well | RAW 상단 | 판정 |",
        "|---|---|---:|---:|---:|---:|---|"]
    for row in results["holdout"]:
        if not row["noise_enabled"]:
            lines.append(f"| {row['scenario_id']} | {'/'.join(row['roles'])}: {row['candidate_id']} | {row['green_dn']:.3f} | {row['green_snr_p10']:.3f} | {100*row['well_fraction']:.3f}% | {100*row['white_fraction']:.3f}% | {', '.join(row['violations']) or '통과'} |")
    lines += ["", "## 새로운 seed의 반복", "",
        "| 입력 | 후보 | 통과 / 반복 | Green 반복 잡음 RMS |", "|---|---|---:|---:|"]
    for row in results["noise"]:
        lines.append(f"| {row['scenario_id']} | {row['candidate_id']} | {row['passed']}/{row['count']} | {row['green_repeat_rms_dn']:.3f} DN |")
    lines += ["", "## 결론과 범위", "", results["verdict"]["summary_ko"],
        "지역 강조광 L20은 훈련의 대역별 최대 광입력 L6 밖의 스트레스다. 범위 내 평가와 합산해 보증 비율로 해석하지 않는다.",
        "일부 통과도 연속 광량 범위 전체의 보증은 아니다. 선택은 유한 격자·세 훈련 입력에 한정되며 전역 최적화가 아니다.",
        "Hold-out은 동일 처방·동일 모델의 별도 이상영상 입력이지, 독립 물리 모델이나 실측 데이터가 아니다.",
        "온도·움직임·HDR·공간 색 정확도·센서 실측 잡음·실시간 지연은 평가하지 않았다. 노출 상한은 모션 블러 계산을 대신하지 않는다.",
        "등록 모델 범위를 넓히지 않았다. gain/노출 후보는 직접 Runtime 연구 설정이며 설치본·DB·Worker·ISP는 바꾸지 않았다.",
        "사전 계획 plan.json, 선택 고정 selection.json, 전체 결과 results.json, 후보별 NPZ와 metrics.csv를 함께 보존한다.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def png(array, *, mask=False):
        signal = np.asarray(array, dtype=float) if mask else (np.asarray(array, dtype=float) - 64) / (4095 - 64)
        pixels = np.rint(np.clip(signal, 0, 1) * 255).astype(np.uint8)
        image = Image.fromarray(pixels).resize((240, 240), Image.Resampling.NEAREST)
        stream = io.BytesIO(); image.save(stream, format="PNG")
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()

    cards = []
    for row in results["holdout"]:
        if row["noise_enabled"]:
            continue
        with np.load(directory / row["arrays_file"], allow_pickle=False) as arrays:
            images = [("RAW · 고정 64–4095 DN", png(arrays["raw_dn"])),
                      ("Well 포화", png(arrays["well_saturated_mask"], mask=True)),
                      ("RAW 상단", png(arrays["raw_dn"] == 4095, mask=True))]
        cards.append("<section><h2>" + html.escape(row["scenario_id"]) + "</h2><p>"
                     + html.escape(" / ".join(row["roles"]) + ": " + row["candidate_id"])
                     + " · " + html.escape(", ".join(row["violations"]) or "통과")
                     + "</p><div class='images'>" + "".join(
                         f"<figure><img src='{url}' alt='{html.escape(label)}'><figcaption>{html.escape(label)}</figcaption></figure>"
                         for label, url in images) + "</div></section>")
    (directory / "comparison.html").write_text(
        "<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>CameraE2E · 제약 선택 / Hold-out</title><style>body{font:16px/1.65 system-ui;max-width:1200px;margin:auto;padding:24px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #bbb;padding:18px 0}.images{display:flex;flex-wrap:wrap;gap:12px}"
        "figure{margin:0}img{max-width:100%;image-rendering:pixelated}h1,h2{line-height:1.35}</style>"
        "<h1>처방·노출·Gain 후보 선택과 미사용 입력 평가</h1><p>" + html.escape(results["verdict"]["summary_ko"])
        + "</p><p>표시 범위는 모든 RAW에서 64–4095 DN으로 동일합니다. 기본 앱에는 적용하지 않았습니다.</p>"
        + "".join(cards) + "<h2>전체 선택 근거</h2><pre>" + html.escape("\n".join(lines)) + "</pre></html>", encoding="utf-8")


def run_study(directory: Path) -> None:
    directory = directory.resolve()
    if ROOT not in directory.parents or directory.exists():
        raise ValueError("Use a NEW output directory within the checkout")
    limits = SelectionLimits()
    train, held_out = scenarios()
    candidates = candidate_grid()
    cases = load_cases()
    require_same_design_scene(list(cases.values()))
    directory.mkdir(parents=True, exist_ok=False)
    source_files = [ROOT / "src/camera_platform/constrained_selection.py", Path(__file__),
                    ROOT / "src/camera_platform/response_chain.py",
                    ROOT / "src/camera_platform/prescription_response.py",
                    ROOT / "tools/run_camera_prescription_comparison.py"]
    plan = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "limits": asdict(limits),
            "candidates": candidates, "training": train, "holdout_recipes": held_out,
            "noise_seeds": [1901, 1902, 1903, 1904], "training_noise_enabled": False,
            "source_identity": {n: c.identity() for n, c in cases.items()},
            "implementation_sha256": {str(p.relative_to(ROOT)): sha(p) for p in source_files},
            "selection_objective": "maximize_worst_green_SNR_p10_then_shorter_exposure_lower_gain_id",
            "independent_reference_limits": {"photo_relative_error": 2e-10, "raw_difference_dn": 0},
            "brief_source": "explicit_research_requirements_not_customer_or_sensor_hardware_specification",
            "gain_and_exposure_domain": "direct_runtime_hypotheses_no_registry_validity_expansion",
            "holdout_policy": "freeze_selection_before_constructing_holdout_inputs_no_reselection",
            "scope": "reused_prescription_field_PSF_and_radiometry_static_independent_reset"}
    write_json(directory / "plan.json", plan)
    results = {"status": "in_progress", "plan": plan, "training": [], "holdout": [], "noise": [],
               "reference_checks": [], "new_runtime_captures": 0, "new_factory_jobs": 0,
               "reused_factory_jobs": 6, "physical_validation": "not_established"}
    # Cache only the independent linear reference per (stop, scenario), never
    # use the independent result as an input to the actual response-chain run.
    references = {}

    def acquire(candidate, spec, *, noise=False, seed=1801, roles=None):
        case = cases[candidate["stop"]]
        request = scenario_request(case, spec)
        exposure = candidate["exposure_ms"] / 1000
        output = evaluate_response(request, case.electrical_model, exposure_s=exposure,
                                   analog_gain=candidate["gain"], noise_enabled=noise, seed=seed)
        results["new_runtime_captures"] += 1
        label = f"{candidate['candidate_id']}--{spec['scenario_id']}--{'n'+str(seed) if noise else 'n0'}"
        row = {**candidate, **summarize_output(output), "scenario_id": spec["scenario_id"],
               "split": spec["split"], "domain": spec["domain"], "noise_enabled": noise,
               "seed": seed, "roles": roles or ["selection"], "arrays_file": label + ".npz"}
        row["violations"] = violations(row, limits)
        arrays = output["arrays"]
        row["raw_sha256"] = hashlib.sha256(arrays["raw_dn"].tobytes(order="C")).hexdigest()
        row["input_sha256"] = output["summary"]["response_identity"]["scene_array_sha256"]
        # Distinguish within-envelope changes from a deliberately out-of-range
        # hotspot using supplied ideal radiance, separately for each band.
        peaks = np.asarray(request["scene"]["spectral_irradiance"]).max(axis=(0, 1))
        bounds = np.asarray(case.request["scene"]["spectral_irradiance"]).max(axis=(0, 1)) * 6
        row["maximum_per_band_peak_over_training_bound"] = float(np.max(peaks / bounds))
        if not noise:
            key = candidate["stop"], spec["scenario_id"]
            if key not in references:
                references[key] = reference_photo(request, 1.0)[0]
            expected_photo = references[key] * exposure
            error = float(np.max(np.abs(arrays["mean_photocharge_e"] - expected_photo))
                          / max(float(expected_photo.max()), 1e-20))
            expected_raw = reference_raw(expected_photo, case.electrical_model, exposure, candidate["gain"])
            raw_error = int(np.max(np.abs(arrays["raw_dn"] - expected_raw)))
            check = {"case": label, "photo_relative_error": error, "raw_difference_dn": raw_error}
            results["reference_checks"].append(check)
            if error > 2e-10 or raw_error != 0:
                raise AssertionError("Independent numeric mismatch: " + str(check))
        # Preserve decision quantities; no repeated enormous Factory closure.
        selected_arrays = {k: arrays[k] for k in (
            "raw_dn", "mean_photocharge_e", "well_saturated_mask", "adc_high_mask", "cfa_channel_index")}
        np.savez_compressed(directory / row["arrays_file"], **selected_arrays)
        return row, arrays["raw_dn"].copy(), arrays["cfa_channel_index"] == 1

    try:
        for candidate in candidates:
            if candidate["exposure_ms"] > limits.maximum_exposure_ms:
                continue
            for spec in train:
                row, _, _ = acquire(candidate, spec)
                results["training"].append(row)
            print("TRAIN", candidate["candidate_id"], sorted({v for r in results["training"][-3:] for v in r["violations"]}), flush=True)
        decision = select_candidate(candidates, results["training"],
                                    training_scenario_ids=[s["scenario_id"] for s in train], limits=limits)
        results["selection"] = decision
        frozen = {"frozen_at_utc": datetime.now(timezone.utc).isoformat(),
                  "plan_sha256": sha(directory / "plan.json"), "decision": decision}
        write_json(directory / "selection.json", frozen)
        selection_hash = sha(directory / "selection.json")
        print("SELECTED", decision["selected_candidate_id"], "feasible", decision["feasible_count"], flush=True)
        by_id = {c["candidate_id"]: c for c in candidates}
        evaluated = {"clear-e4-g1": ["baseline"]}
        winner = decision["selected_candidate_id"]
        if winner:
            evaluated.setdefault(winner, []).append("selected")
        for spec in held_out:
            for identity, roles in evaluated.items():
                row, _, green = acquire(by_id[identity], spec, roles=roles)
                results["holdout"].append(row)
                print("HOLDOUT", spec["scenario_id"], identity, row["violations"] or "PASS", flush=True)
                noisy, noisy_rows = [], []
                for seed in plan["noise_seeds"]:
                    sample, raw, _ = acquire(by_id[identity], spec, noise=True, seed=seed, roles=roles)
                    results["holdout"].append(sample); noisy_rows.append(sample); noisy.append(raw)
                stack = np.stack(noisy).astype(float)
                results["noise"].append({"scenario_id": spec["scenario_id"], "candidate_id": identity,
                    "roles": roles, "count": len(noisy), "passed": sum(not r["violations"] for r in noisy_rows),
                    "green_repeat_rms_dn": float(np.sqrt(np.var(stack[:, green], axis=0, ddof=1).mean())),
                    "clipping_in_samples": any(r["well_fraction"] or r["white_fraction"] for r in noisy_rows),
                    "interpretation": "sampled_repeat_noise_diagnostic_not_unclipped_SNR_guarantee"})
        selected_rows = [r for r in results["holdout"] if "selected" in r["roles"]]
        inside = [r for r in selected_rows if r["domain"].startswith("within_")]
        stress = [r for r in selected_rows if r["domain"].startswith("outside_")]
        inside_failures = [r for r in inside if r["violations"]]
        description = ("훈련 격자에 적합 후보가 없어 최종 후보를 선택하지 않았다." if not winner else
                       "선택 후보는 범위 내 세 hold-out과 새 잡음 seed 표본에서 제약을 유지했다." if not inside_failures else
                       "훈련 적합 후보가 범위 내 hold-out에서 일부 제약을 위반했다. 후보를 재선택하거나 평가 결과를 숨기지 않았다.")
        if stress and any(r["violations"] for r in stress):
            description += " 범위 밖 지역 강조광에서는 실패해 확대 사용을 승인하지 않았다."
        results["verdict"] = {"summary_ko": description, "selected_candidate_id": winner,
            "inside_sample_count": len(inside), "inside_failed_samples": len(inside_failures),
            "outside_sample_count": len(stress), "outside_failed_samples": sum(bool(r["violations"]) for r in stress),
            "reselection_after_holdout": False, "automatic_approval": False}
        for case in cases.values():
            case.assert_unchanged()
        if sha(directory / "selection.json") != selection_hash or sha(directory / "plan.json") != frozen["plan_sha256"]:
            raise AssertionError("Frozen plan/selection changed during hold-out")
        for path, checksum in plan["implementation_sha256"].items():
            if sha(ROOT / path) != checksum:
                raise AssertionError("Implementation changed during the study")
        results.update(status="completed", source_unchanged=True,
                       selection_sha256=selection_hash, selection_frozen_before_holdout=True)
        write_json(directory / "results.json", results)
        rows = results["training"] + results["holdout"]
        fields = [k for k in rows[0] if k not in {"roles", "violations"}] + ["roles", "violations"]
        with (directory / "metrics.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fields); writer.writeheader()
            for row in rows:
                writer.writerow({**row, "roles": ";".join(row["roles"]), "violations": ";".join(row["violations"])})
        draw_reports(directory, results)
        print("RESULT", directory / "report.md", description, flush=True)
    except Exception as exc:
        results.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write_json(directory / "failed.json", results)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run_study(parser.parse_args().output)

"""Compare existing, same-prescription stop conditions through response_chain.

Only archived run JSON and content-addressed blobs are read. No registry,
Factory jobs, network, server or installed application is changed.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from camera_platform.prescription_response import ArchivedPrescription, require_same_design_scene
from camera_platform.response_chain import evaluate_response

SOURCE = ROOT / "reports/camera_platform/stop-transmission-v016"
STATE = ROOT / "outputs/camera-platform-stop-v016-qa"
VARIANTS = ("clear", "quarter", "radial")


def load_cases():
    summary = json.loads((SOURCE / "evidence-summary.json").read_text())
    return {name: ArchivedPrescription(SOURCE / (name + "-run.json"), STATE,
                expected_sha256=summary["evidence_files"][name + "-run.json"])
            for name in VARIANTS}


def reference_photo(request: dict, exposure_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Independent direct source-pixel sum, angle/QE, wavelength/area integration.

    Uses the saved field kernels and theta histogram, NOT another optical
    solver. It checks their application and native coordinates, not physical
    lens truth. Deliberately bounded to <=32x32 for independent direct sums.
    """
    image = np.asarray(request["scene"]["spectral_irradiance"], dtype=float)
    h, w, bands = image.shape
    if max(h, w) > 32 or "native_pixel_response" in request:
        raise ValueError("Direct reference supports <=32x32 archived RGB/theta response")
    psf, lens = request["psf"], request["lens"]
    kernels = np.asarray(psf["values"])
    sample = request["scene"]["sampling"]
    pitch = request["pixel_pitch_um"]
    y = (np.arange(h) + sample["crop_origin"][0] - (sample["full_sensor_shape"][0]-1)/2)*pitch/1000
    x = (np.arange(w) + sample["crop_origin"][1] - (sample["full_sensor_shape"][1]-1)/2)*pitch/1000

    def bracket(axis, point):
        if not axis[0] <= point <= axis[-1]:
            raise ValueError("Reference refuses field extrapolation")
        j = min(max(int(np.searchsorted(axis, point, side="right")), 1), len(axis)-1)
        q = (point-axis[j-1])/(axis[j]-axis[j-1])
        return ((j-1, 1-q), (j, q))

    ry, rx = np.indices((h, w))
    blurred = np.zeros_like(image)
    for sy in range(h):
        for sx in range(w):
            ky, kx = ry-sy+kernels.shape[-2]//2, rx-sx+kernels.shape[-1]//2
            valid = (ky >= 0) & (kx >= 0) & (ky < kernels.shape[-2]) & (kx < kernels.shape[-1])
            for fy, wy in bracket(psf["field_y_mm"], y[sy]):
                for fx, wx in bracket(psf["field_x_mm"], x[sx]):
                    for band in range(bands):
                        blurred[..., band][valid] += image[sy, sx, band]*wy*wx*kernels[band, fy, fx, ky[valid], kx[valid]]
    radius = np.hypot(y[:, None], x[None, :]) / lens["field_reference_radius_mm"]
    angular = request["sensor_angular_response"]
    waves = np.asarray(request["wavelength_nm"])
    channel = np.zeros((h, w, 3))
    for c in range(3):
        terms = []
        for b, wave in enumerate(waves):
            response = np.interp(lens["theta_deg"], angular["theta_deg"], angular["relative_response"][c][b])
            field_eff = [sum(p*q for p,q in zip(weights, response, strict=True))*request["qe"][c][b]
                         for weights in lens["angular_weights"][b]]
            eqe = np.interp(radius, lens["field_radius"], field_eff)
            ri = np.interp(radius, lens["field_radius"], lens["relative_illumination"][b])
            terms.append(blurred[..., b]*ri*eqe*(float(wave)*1e-9)/(6.62607015e-34*299792458))
        for b in range(bands-1):
            channel[..., c] += (terms[b]+terms[b+1])*(waves[b+1]-waves[b])/2
    channel *= (pitch*1e-6)**2*exposure_s
    cfa = np.array([[0,1],[1,2]])[(ry+sample["crop_origin"][0])%2, (rx+sample["crop_origin"][1])%2]
    return np.take_along_axis(channel, cfa[...,None], axis=-1)[...,0], blurred


def reference_raw(photo: np.ndarray, model: dict, exposure_s: float, gain: float) -> np.ndarray:
    """Independent deterministic electrical transfer, including separate clipping."""
    fixed = model["fixed_pattern"]
    if fixed["prnu_sigma"] or fixed["dsnu_e_per_s"] or model["dark_current_doubling_c"] is not None:
        raise ValueError("Reference limited to original zero-FPN, fixed-temperature model")
    charge = np.minimum(photo + model["dark_current_e_per_s"]*exposure_s, model["full_well_e"])
    adc = model["adc"]
    white, black = 2**adc["bit_depth"]-1, adc["black_level_dn"]
    code = np.clip(np.rint(black + charge*(model["conversion_gain_v_per_e"]*gain)*white/adc["full_scale_v"]), 0, white)
    return np.clip(np.rint(black + model["digital_gain"]*(code-black)), 0, white).astype(np.int32)


def optical_metrics(case: ArchivedPrescription) -> dict:
    kernels = np.asarray(case.psf["values"])
    cy = case.psf["field_y_mm"].index(0)
    cx = case.psf["field_x_mm"].index(0)
    theta = np.asarray(case.request["lens"]["theta_deg"])
    weights = np.asarray(case.request["lens"]["angular_weights"])
    entries = []
    for b, wave in enumerate(case.request["wavelength_nm"]):
        k = kernels[b, cy, cx]
        y, x = np.indices(k.shape)
        r = np.hypot(y-k.shape[0]//2, x-k.shape[1]//2)*case.request["pixel_pitch_um"]
        order = np.argsort(r.ravel())
        r80 = r.ravel()[order][np.searchsorted(np.cumsum(k.ravel()[order]), .8)]
        # This is a discrete kernel OTF at a declared frequency, not measured SFR.
        otf = sum(np.sum(k, axis=0)*np.exp(-2j*np.pi*.125*(np.arange(k.shape[1])-k.shape[1]//2)))
        entries.append({"wavelength_nm": wave,
            "on_axis_E_per_L_sr": case.request["lens"]["radiometry"]["irradiance_per_radiance_sr"][b][0],
            "on_axis_mean_theta_deg": float(np.dot(weights[b,0],theta)),
            "on_axis_r80_um_with_finite_support": float(r80),
            "kernel_otf_0_125_cycles_per_pixel": float(abs(otf))})
    return {"bands": entries, "prescription": case.prescription,
            "reference_focal_length_mm": case.psf_request["reference_focal_length_mm"],
            "field_grid_mm": [case.psf["field_y_mm"],case.psf["field_x_mm"]]}


def json_write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def metrics(output, *, condition, policy, scale, exposure, gain, noise, seed):
    a, summary = output["arrays"], output["summary"]
    cfa = a["cfa_channel_index"]
    black = summary["electrical"]["model"]["adc"]["black_level_dn"]
    means = [float((a["raw_dn"][cfa==c]-black).mean()) for c in range(3)]
    charges = [float(a["mean_photocharge_e"][cfa==c].mean()) for c in range(3)]
    white = 2**summary["electrical"]["model"]["adc"]["bit_depth"]-1
    return {"condition": condition,"policy":policy,"radiance_scale":scale,
            "exposure_ms": exposure*1000,"gain":gain,"noise_enabled":noise,"seed":seed,
            "green_dn":means[1],"R_over_G":means[0]/means[1],"B_over_G":means[2]/means[1],
            "mean_photocharge_e":float(a["mean_photocharge_e"].mean()), "channel_charge_e":charges,
            "well_saturated_fraction":float(a["well_saturated_mask"].mean()),
            "ADC_high_before_rounding_fraction":float(a["adc_high_mask"].mean()),
            "final_white_fraction":float(np.mean(a["raw_dn"]==white))}


def study(directory: Path):
    directory = directory.resolve()
    if ROOT not in directory.parents or directory.exists():
        raise ValueError("Use a NEW output directory within this checkout")
    cases = load_cases()
    require_same_design_scene(list(cases.values()))
    directory.mkdir(parents=True, exist_ok=False)
    plan = {"created_at_utc":datetime.now(timezone.utc).isoformat(),
        "conditions":list(VARIANTS),"source_identity":{n:c.identity() for n,c in cases.items()},
        "radiance_scales":[1,8],"noise_seeds":[913,914,915,916],
        "policies":["fixed","gain_match","exposure_match"],
        "compensation":"Clear/variant noiseless green PHOTOCHARGE ratio at nominal input, same scene calibration only",
        "criteria_before_results":{"independent_photo_relative_error":2e-10,"independent_raw_max_dn":0,
            "archived_mean_charge_absolute_error_e":1e-8,"quarter_photo_ratio":.25,
            "quarter_conditional_psf_exact":True,"source_scene_and_geometry":"identical except explicit stop transmission"},
        "scope":"Archived prescription-derived PSF and radiometry reused. Not new ray tracing, independent optical validation or registered model expansion."}
    json_write(directory/"study-plan.json",plan)
    results = {"plan":plan,"status":"in_progress","cases":[],"optics":{},"baseline_checks":{},"noise":[]}
    outputs, baseline = {}, {}
    try:
        # Recompute the original-rate chain once per archived optical condition.
        for name, case in cases.items():
            ex = float(case.request["exposure_s"])
            out = evaluate_response(case.rate_request(),case.electrical_model,exposure_s=ex,analog_gain=1,noise_enabled=False,seed=913)
            baseline[name] = out
            # Legacy spatial.mean_electrons is PHOTOcharge before readout adds
            # dark. Compare the same quantity, never erase dark or loosen error.
            error = float(np.max(np.abs(out["arrays"]["mean_photocharge_e"]-case.array("mean_electrons.npy"))))
            if error > plan["criteria_before_results"]["archived_mean_charge_absolute_error_e"]:
                raise AssertionError("Current chain changed archived mean charge")
            results["baseline_checks"][name] = {"max_mean_charge_difference_e":error,
                "quantity":"legacy_mean_electrons_is_photocharge_before_dark"}
            results["optics"][name] = optical_metrics(case)
        np.testing.assert_array_equal(cases["clear"].psf["values"],cases["quarter"].psf["values"])
        np.testing.assert_allclose(baseline["quarter"]["arrays"]["mean_photocharge_e"],.25*baseline["clear"]["arrays"]["mean_photocharge_e"],rtol=2e-12,atol=0)
        cfa = baseline["clear"]["arrays"]["cfa_channel_index"]
        green = cfa==1
        compensations = {n:float(baseline["clear"]["arrays"]["mean_photocharge_e"][green].mean()/o["arrays"]["mean_photocharge_e"][green].mean()) for n,o in baseline.items()}
        results["compensation_factors"] = compensations
        for name, case in cases.items():
            for policy in (["fixed"] if name=="clear" else ["fixed","gain_match","exposure_match"]):
                gain = compensations[name] if policy=="gain_match" else 1.0
                exposure = float(case.request["exposure_s"])*(compensations[name] if policy=="exposure_match" else 1.0)
                for scale,noise,seed in [(1,False,913),(8,False,913)]+[(1,True,s) for s in plan["noise_seeds"]]:
                    req = case.rate_request()
                    req["scene"]["spectral_irradiance"] *= scale
                    out = (baseline[name] if scale==1 and not noise and policy=="fixed" else
                           evaluate_response(req,case.electrical_model,exposure_s=exposure,analog_gain=gain,noise_enabled=noise,seed=seed))
                    label = f"{name}-{policy}-L{scale}-n{int(noise)}-s{seed}"
                    row = metrics(out,condition=name,policy=policy,scale=scale,exposure=exposure,gain=gain,noise=noise,seed=seed)
                    row["case"] = label
                    if not noise:
                        ref,blur = reference_photo(req,exposure)
                        relative = float(np.max(np.abs(out["arrays"]["mean_photocharge_e"]-ref))/max(float(ref.max()),1e-20))
                        expected = reference_raw(ref,case.electrical_model,exposure,gain)
                        raw_error = int(np.max(np.abs(out["arrays"]["raw_dn"]-expected)))
                        if relative > 2e-10 or raw_error != 0:
                            raise AssertionError(f"Independent reference mismatch {label}: {relative}, {raw_error}")
                        row.update(independent_photo_relative_error=relative,independent_raw_max_dn=raw_error)
                    np.savez_compressed(directory/(label+".npz"),**out["arrays"])
                    json_write(directory/(label+"-summary.json"),out["summary"])
                    results["cases"].append(row); outputs[label]=out
                    print(label, "G",round(row["green_dn"],3),"well",row["well_saturated_fraction"],"white",row["final_white_fraction"],flush=True)
        for name in VARIANTS:
            for policy in (["fixed"] if name=="clear" else ["fixed","gain_match","exposure_match"]):
                selected=[r for r in results["cases"] if r["condition"]==name and r["policy"]==policy and r["noise_enabled"]]
                raw=np.stack([outputs[r["case"]]["arrays"]["raw_dn"] for r in selected]).astype(float)
                sigma=float(np.sqrt(np.var(raw[:,green],axis=0,ddof=1).mean()))
                mean=float(np.mean(raw[:,green]-cases[name].electrical_model["adc"]["black_level_dn"]))
                results["noise"].append({"condition":name,"policy":policy,"seed_count":len(selected),"green_repeat_noise_rms_dn":sigma,"mean_over_rms_diagnostic":mean/sigma})
        for case in cases.values(): case.assert_unchanged()
        results.update(status="completed",new_electrical_captures=len(results["cases"]),
            reused_factory_jobs=6,new_factory_jobs=0,source_unchanged=True,physical_validation="not_established")
        json_write(directory/"results.json",results)
        fields=[k for k in results["cases"][0] if k!="channel_charge_e"]
        with (directory/"metrics.csv").open("w",encoding="utf-8",newline="") as f:
            writer=csv.DictWriter(f,fields,extrasaction="ignore"); writer.writeheader();writer.writerows(results["cases"])
        for name,case in cases.items():
            json_write(directory/(name+"-factory-inputs.json"),case.factory_inputs)
        report(directory,results)
    except Exception as exc:
        results.update(status="failed",error=f"{type(exc).__name__}: {exc}")
        json_write(directory/"failed.json",results)
        raise


def report(directory, result):
    rows=result["cases"]
    text=["# 기존 처방 자산: 투과 조건·노출·gain의 실제 비교", "",
          "기존 동일 구면 처방의 clear / quarter / radial Factory 자산을 재사용했다.",
          "실물 샘플이나 상용 렌즈 모델이 아닌, RayOptics/Prysm으로 계산한 합성 설계 자산이다.",
          "동일한 저장 장면 광휘·센서·native 위치를 유지했다. PSF를 다시 추적한 것으로 집계하지 않는다.", "",
          "## 기본 광입력: 같은 조건 또는 nominal green 전하 기준 보상", "",
          "| Stop | 보상 | 노출 ms | Gain | 평균 광전하 e | Green DN | R/G | B/G |",
          "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if not r["noise_enabled"] and r["radiance_scale"]==1:
            text.append(f"| {r['condition']} | {r['policy']} | {r['exposure_ms']:.4f} | {r['gain']:.4f} | {r['mean_photocharge_e']:.3f} | {r['green_dn']:.3f} | {r['R_over_G']:.5f} | {r['B_over_G']:.5f} |")
    text += ["", "## 같은 장면 광휘 8배: 포화 보상은 별도 판단", "",
             "| Stop | 보상 | 전하 저장소 포화 | 최종 RAW 상단 | Green DN |", "|---|---|---:|---:|---:|"]
    for r in rows:
        if not r["noise_enabled"] and r["radiance_scale"]==8:
            text.append(f"| {r['condition']} | {r['policy']} | {100*r['well_saturated_fraction']:.3f}% | {100*r['final_white_fraction']:.3f}% | {r['green_dn']:.2f} |")
    text += ["", "## 네 seed 반복: nominal green 신호와 반복 잡음", "",
             "| Stop | 보상 | 반복 잡음 RMS DN | 평균 / 반복 RMS |", "|---|---|---:|---:|"]
    for r in result["noise"]:
        text.append(f"| {r['condition']} | {r['policy']} | {r['green_repeat_noise_rms_dn']:.3f} | {r['mean_over_rms_diagnostic']:.3f} |")
    text += ["", "## 출처·수치 확인", "",
             f"기존 평균 전하 최대 재현 차이: {max(v['max_mean_charge_difference_e'] for v in result['baseline_checks'].values()):.3g} e.",
             f"독립 source-pixel 합산·각도/QE·면적/파장 적분 최대 상대 차이: {max(r.get('independent_photo_relative_error',0) for r in rows):.3g}.",
             "독립 ADC 반올림·clipping 식과 noiseless RAW 차이: 모든 비교 0 DN.",
             "균일 25% 조건의 PSF 배열은 clear와 정확히 같고, 광전하는 정확히 1/4(수치 허용 범위)다.", "",
             "## 해석 범위", "",
             "광입력에는 해당 처방의 E/L이 이미 한 번 반영돼 있다. 단위합 PSF로 투과 손실을 대체하거나 T/F-number를 다시 곱하지 않았다.",
             "gain/노출 보상은 이 동일 장면의 nominal green 전하로 정한 연구 설정이다. 독립 튜닝 데이터 또는 실제 센서의 지원 범위 검증은 아니다.",
             "색 비율은 합성 광입력과 공급 QE에서 나온 RAW 채널 비율이며, 색 정확도나 백색 균형 성능을 뜻하지 않는다.",
             "노출 보상은 광자를 추가로 수집하지만 동적 장면의 모션 블러·촬영 간격 비용은 이번 정적 독립 노출 모델에 포함하지 않았다.",
             "반복 RMS는 네 seed/동일 픽셀의 작은 표본 진단이다. 실측 SNR이나 통계 보증은 아니다.",
             "이 과거 자산은 RGB QE·theta 분포만 갖는다. Gr/Gb 비대칭·방위각을 임의로 만들지 않았다.",
             "현재 PSF는 위치별, 각도/QE는 수광 위치에서 분리 적용하는 근사다. 독립 참조는 같은 공급 표의 적용을 확인하며 실제 광학 정확도는 확립하지 않는다.",
             "등록 모델·DB·기본 앱·Worker·설치본은 변경하지 않았다.", ""]
    (directory/"report.md").write_text("\n".join(text),encoding="utf-8")
    (directory/"comparison.html").write_text("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>처방 기반 비교</title><style>body{max-width:1100px;margin:32px auto;padding:0 24px;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>처방 기반 투과·전하·RAW 비교</h1><pre>"+html.escape("\n".join(text))+"</pre></html>",encoding="utf-8")


def summarize_saved(directory: Path):
    """Build the optical/decision view from saved arrays, without reacquisition."""
    from PIL import Image

    directory=directory.resolve()
    if ROOT not in directory.parents:
        raise ValueError("Use a saved study within this checkout")
    targets=[directory/"interpretation.md",directory/"comparison-detailed.html"]
    if any(p.exists() for p in targets):
        raise FileExistsError("Saved interpretation exists; preserve it")
    result=json.loads((directory/"results.json").read_text())
    if result["status"]!="completed":
        raise ValueError("Only a completed study can be summarized")
    optics=result["optics"]
    def row(name, policy="fixed", scale=1):
        return next(r for r in result["cases"] if r["condition"]==name and r["policy"]==policy
                    and r["radiance_scale"]==scale and not r["noise_enabled"])
    clear,qgain,qex,rad=(row("clear"),row("quarter","gain_match"),
                        row("quarter","exposure_match"),row("radial","gain_match"))
    nc=next(r for r in result["noise"] if r["condition"]=="clear")
    ng=next(r for r in result["noise"] if r["condition"]=="quarter" and r["policy"]=="gain_match")
    cb,qb,qgb,qeb=row("clear",scale=8),row("quarter",scale=8),row("quarter","gain_match",8),row("quarter","exposure_match",8)
    sample=result["plan"]["source_identity"]["clear"]["sampling"]
    lines=["# 처방 기반 비교: 이번 설계 판단", "",
        f"동일한 합성 구면 처방(기준 초점거리 {optics['clear']['reference_focal_length_mm']:.3f} mm)의 기존 RayOptics/Prysm 자산을 연결했다.",
        f"Native 격자 {sample['full_sensor_shape']}, crop 원점 {sample['crop_origin']}, pixel pitch {sample['pixel_pitch_um']} µm를 보존했다.",
        "독립 동공 가정에서 생성한 PSF로 바꾸지 않았고, 기존 처방의 두 Factory 결과를 재사용했다.", "",
        "## 광학 결과: 투과율과 조건부 PSF는 별개", "",
        "| 조건 | 파장 nm | Clear 대비 E/L | 평균 입사각 deg | 조건부 kernel OTF @ 0.125 cyc/pixel |",
        "|---|---:|---:|---:|---:|"]
    optical_rows=[]
    for name in VARIANTS:
        for i,band in enumerate(optics[name]["bands"]):
            ratio=band["on_axis_E_per_L_sr"]/optics["clear"]["bands"][i]["on_axis_E_per_L_sr"]
            fields=[name,str(band["wavelength_nm"]),f"{ratio:.5f}",f"{band['on_axis_mean_theta_deg']:.5f}",f"{band['kernel_otf_0_125_cycles_per_pixel']:.5f}"]
            optical_rows.append(fields);lines.append("| "+" | ".join(fields)+" |")
    lines += ["", "25% 균일 감쇠는 PSF 형태·조건부 각도 분포를 바꾸지 않는다. 손실은 절대 E/L에 남는다.",
        "반경별 감쇠는 파장마다 광량과 동공 가중치를 다르게 바꾼다. 이 표의 OTF는 픽셀 적분·유한 영역 정규화가 포함된 특정 주파수 값이며 순수 렌즈 MTF/SFR가 아니다.", "",
        "## 같은 밝기와 같은 정보는 다르다", "",
        f"25% 투과 + gain {qgain['gain']:.3f}는 baseline Green {clear['green_dn']:.3f} DN을 {qgain['green_dn']:.3f} DN으로 복원하지만 평균 광전하는 {clear['mean_photocharge_e']:.3f}→{qgain['mean_photocharge_e']:.3f} e로 감소한 채다.",
        f"네 seed에서 Green 반복 잡음은 clear의 {nc['green_repeat_noise_rms_dn']:.3f} DN에서 gain 보상의 {ng['green_repeat_noise_rms_dn']:.3f} DN이었다. 평균/반복 RMS 진단은 {nc['mean_over_rms_diagnostic']:.3f}→{ng['mean_over_rms_diagnostic']:.3f}였다.",
        f"노출 보상은 {clear['exposure_ms']:.3f}→{qex['exposure_ms']:.3f} ms의 비용이 있다. 움직임 blur는 이 정적 실험에서 계산하지 않았다.", "",
        "## 밝은 장면: 포화 위치를 바꿔도 잃은 출력 정보는 돌아오지 않는다", "",
        f"장면 광휘 8배에서 clear는 well {100*cb['well_saturated_fraction']:.4f}%, RAW 상단 {100*cb['final_white_fraction']:.4f}%였다. 25% 투과만 적용하면 각각 {100*qb['well_saturated_fraction']:.4f}% / {100*qb['final_white_fraction']:.4f}%였다.",
        f"Gain 보상은 well {100*qgb['well_saturated_fraction']:.4f}%인 채 RAW 상단 {100*qgb['final_white_fraction']:.4f}%였다.",
        f"노출 보상은 well {100*qeb['well_saturated_fraction']:.4f}%, RAW 상단 {100*qeb['final_white_fraction']:.4f}%였다. 목표 DN만 보는 정책은 이 차이를 놓친다.", "",
        "## 파장 선택 감쇠: 스칼라 보상으로 색 비율은 복원되지 않는다", "",
        f"반경별 감쇠의 gain 보상은 Green {rad['green_dn']:.3f} DN이지만 R/G는 {clear['R_over_G']:.5f}→{rad['R_over_G']:.5f}, B/G는 {clear['B_over_G']:.5f}→{rad['B_over_G']:.5f}였다.",
        "이는 이 합성 조명·공급 QE에서의 채널 비율이다. 색 정확도나 실제 제품의 화이트밸런스 실패를 주장하지 않는다.", "",
        "## 완료와 다음 작업", "",
        f"기존 광전하 최대 차이는 {max(r['max_mean_charge_difference_e'] for r in result['baseline_checks'].values()):.3g} e. 독립 직접 합산/분광 적분 최대 상대 차이는 {max(r.get('independent_photo_relative_error',0) for r in result['cases']):.3g}, ADC 최대 차이는 {max(r.get('independent_raw_max_dn',0) for r in result['cases'])} DN이다.",
        f"새 전기 취득은 {result['new_electrical_captures']}개, 기존 Factory {result['reused_factory_jobs']}개는 재사용했다. 새 광선 추적·물리 측정으로 집계하지 않는다.",
        "다음은 광입력 범위와 최소 신호/포화 제약을 함께 갖는 후보 선택을 이 경로에 연결한다. 미검증 모션 blur를 노출 페널티만으로 검증했다고 부르지 않는다.",
        "실물 적용은 목표 렌즈·센서 식별과 측정이 필요하며, 이번 입력 자산은 명시적인 합성 처방이다.", ""]
    targets[0].write_text("\n".join(lines),encoding="utf-8")

    def png(values, upper=4095, lower=64):
        pixels=np.uint8(np.clip((values-lower)/(upper-lower),0,1)*255)
        stream=io.BytesIO();Image.fromarray(pixels).save(stream,format="PNG")
        return "data:image/png;base64,"+base64.b64encode(stream.getvalue()).decode()

    def table(headers, rows):
        return "<div class='scroll'><table><thead><tr>"+"".join("<th>"+html.escape(v)+"</th>" for v in headers)+"</tr></thead><tbody>"+"".join("<tr>"+"".join("<td>"+html.escape(v)+"</td>" for v in row)+"</tr>" for row in rows)+"</tbody></table></div>"

    parts=["<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>CameraE2E · 처방 조건 비교</title>",
        "<style>body{margin:0;background:#f4f6f5;color:#172820;font:16px/1.6 system-ui}main{max-width:1260px;margin:32px auto;padding:0 24px}.tag{font-size:13px;letter-spacing:.1em;color:#426556}h1{font-size:34px;line-height:1.2}.note{background:#fff3da;padding:16px;border-left:4px solid #a5721c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:white;padding:18px;border:1px solid #dbe2dd;border-radius:12px}.images{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.images img{width:100%;image-rendering:pixelated;background:#000}.images small{font-size:11px}table{width:100%;border-collapse:collapse;background:white}td,th{padding:10px;text-align:left;border-bottom:1px solid #dbe2dd}th{background:#e5eee8}.scroll{overflow-x:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}.numbers{font-variant-numeric:tabular-nums}section{margin:30px 0}</style><main>",
        "<p class='tag'>CAMERAE2E / PRESCRIPTION RESPONSE</p><h1>밝기 보상과 정보 보존은<br>같은 목표가 아니다</h1>",
        "<p class='note'>동일 합성 구면 처방의 기존 PSF·E/L을 사용한 정적 비교입니다. 실제 센서 검증, 새 Factory 계산, 상용 렌즈 정확도 보증이 아닙니다. 원본 16×16 crop이며 이미지는 모두 고정 64–4095 DN 범위입니다.</p>",
        "<section><h2>광량과 조건부 공간 응답</h2>",table(["조건","파장 nm","상대 E/L","평균 입사각 °","Kernel OTF"],optical_rows),"</section>"]
    for scale in [1,8]:
        parts.append(f"<section><h2>장면 광휘 {scale}배</h2><div class='grid'>")
        for row in result["cases"]:
            if row["noise_enabled"] or row["radiance_scale"]!=scale:continue
            label=row["case"]
            if Path(label).name!=label:raise ValueError("Invalid saved case name")
            with np.load(directory/(label+".npz"),allow_pickle=False) as saved:
                raw=saved["raw_dn"].astype(float);well=saved["well_saturated_mask"].astype(float);white=(raw==4095).astype(float)
            parts.append("<article class='card'><h3>"+html.escape(row["condition"]+" / "+row["policy"])+"</h3>"
                +f"<p class='numbers'>{row['exposure_ms']:.3f} ms · gain {row['gain']:.3f}<br>G {row['green_dn']:.2f} DN · {row['mean_photocharge_e']:.1f} e/pixel</p><div class='images'>")
            for title,img in [("RAW · fixed range",png(raw)),("Well saturation",png(well,1,0)),("Final white code",png(white,1,0))]:
                parts.append(f"<div><img alt='{title}' src='{img}'><small>{title}</small></div>")
            parts.append(f"</div><p>Well {100*row['well_saturated_fraction']:.2f}% · RAW 상단 {100*row['final_white_fraction']:.2f}%<br>R/G {row['R_over_G']:.4f} · B/G {row['B_over_G']:.4f}</p></article>")
        parts.append("</div></section>")
    parts += ["<section><h2>설계 판단과 범위</h2><pre>",html.escape("\n".join(lines)),"</pre></section></main></html>"]
    targets[1].write_text("".join(parts),encoding="utf-8")
    print("Saved-array interpretation:",targets[1],flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summarize-existing", type=Path)
    args = parser.parse_args()
    if args.summarize_existing is not None:
        summarize_saved(args.summarize_existing)
        return
    if args.output is not None:
        study(args.output)
        return
    if not args.inspect:
        parser.error("Use --inspect to inspect retained prescription inputs")
    cases = load_cases()
    for name, case in cases.items():
        print(name, json.dumps(case.identity(), ensure_ascii=False))
        print("radiometry", json.dumps(case.request["lens"].get("radiometry"), ensure_ascii=False))
        print("factories", json.dumps(case.factory_inputs, ensure_ascii=False)[:7000] if name == "clear"
              else [list(v) for v in case.factory_inputs])
        print("characterizations", [list(v) for v in case.characterizations])
        print("image", case.scene["shape"], "photo_mean", case.array("mean_electrons.npy").mean())
        case.assert_unchanged()


if __name__ == "__main__":
    main()

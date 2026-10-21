#!/usr/bin/env python3
"""Declared-pupil/native-EQE/gain comparison. Research, not physical validation.

Uniform pupil, fixed focal length, paraxial throughput and separable directions
are explicit assumptions. No registry, server or installed app is changed.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.special import j1
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from camera_platform import electrical, psf_factory, spatial
from camera_platform.response_chain import evaluate_response

WAVE = [500.0, 600.0]
SITES = ["R", "Gr", "Gb", "B"]
SITE_EQE = np.array([0.22, 0.40, 0.50, 0.65])
PITCH, SIZE, EXPOSURE, FOCAL_MM = 2.0, 32, 0.004, 16.0
PIXELS = {"angular_sensitive": [0.60, 0.70, 0.35, 0.50],
          "wide_angle": [0.15, 0.15, 0.15, 0.15]}
LIGHTS = {"low": 1e-4, "bright": 0.004}


def encoded(data):
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def fingerprint(data):
    return hashlib.sha256(encoded(data)).hexdigest()


def native_response(loss):
    """Same normal EQEs; different explicitly authored site angular losses."""
    theta, phi = [0.0, 20.0], [0.0, 90.0, 180.0, 270.0]
    return {
        "schema": "camera_native_pixel_response_v1", "site_names": SITES.copy(),
        "site_index_tile": [[0, 1], [2, 3]], "pixel_pitch_um": PITCH,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": WAVE.copy(), "theta_deg": theta, "phi_deg": phi,
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": [[[(SITE_EQE*(1-np.asarray(loss)*t/20)).tolist() for _ in phi]
                    for t in theta] for _ in WAVE],
        "source": {"kind": "estimated", "description":
            f"Authored EQE=normal*(1-loss*theta/20), loss={loss}; no measurement/crosstalk."},
    }


def pupil_lens(f_number, radial_samples=64):
    """Uniform pupil-area cone. Hash slots identify an authored surrogate only."""
    nodes, weights = leggauss(radial_samples)
    u, weights = (nodes+1)/2, weights/2
    angle = np.arctan(np.sqrt(u)/(2*f_number))
    # The existing direction contract uses the negative-x converging meridian.
    vectors = np.stack([-np.sin(angle), np.zeros_like(angle), np.cos(angle)], axis=-1)
    definition = {"kind": "authored_telecentric_uniform_pupil", "focal_length_mm": FOCAL_MM,
        "f_number": f_number, "pupil_diameter_mm": FOCAL_MM/f_number,
        "u_nodes": u.tolist(), "weights": weights.tolist(),
        "hash_meaning": "analytic surrogate identity, not a traced prescription file"}
    directions = {
        "schema": "camera_lens_directional_response_v1", "wavelength_nm": WAVE.copy(),
        "field_radius": [0, 1], "image_height_mm": [0, 1], "field_reference_radius_mm": 1,
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "reference_plane": "sensor_surface_before_pixel_optics", "field_reference": "positive_x_column",
        "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
        "normalization": "conditional_transmitted_irradiance_fraction",
        "prescription_sha256": fingerprint(definition),
        "stop_transmission_sha256": fingerprint({"authored_uniform_transmission": 1.0}),
        "directions_xyz": [vectors.tolist(), vectors.tolist()],
        "conditional_weights": [[weights.tolist(), weights.tolist()] for _ in WAVE],
        "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
    }
    return {"wavelength_nm": WAVE.copy(), "field_radius": [0, 1],
        "theta_deg": np.rad2deg(angle).tolist(),
        "angular_weights": [[weights.tolist(), weights.tolist()] for _ in WAVE],
        "relative_illumination": [[1, 1] for _ in WAVE],
        "field_reference_radius_mm": 1, "directional_response": directions}, definition


def base_request(psf, lens, pixel, irradiance, throughput):
    request = spatial.request_template()
    for name in ("qe", "sensor_angular_response", "exposure_s", "frames", "seed", "noise_enabled"):
        request.pop(name)
    request.update(wavelength_nm=WAVE.copy(), pixel_pitch_um=PITCH,
                   native_pixel_response=deepcopy(pixel), psf=deepcopy(psf), lens=deepcopy(lens))
    image = np.full((SIZE, SIZE, 2), irradiance*throughput)
    image[:, :SIZE//2] *= 0.25
    request["scene"].update(wavelength_nm=WAVE.copy(), spectral_irradiance=image,
        sampling={"kind": "native_sensor", "full_sensor_shape": [SIZE, SIZE],
                  "crop_origin": [0, 0], "pixel_pitch_um": PITCH},
        source={"kind": "estimated", "description":
            "Same ideal spectral edge at reference F/4; paraxial aperture-area loss applied ONCE."})
    return request


def airy_bins(f_number, size=31, order=20):
    """Independent continuous Airy density integrated over each receiving pixel.

    I(r)=pi/(4*(lambda*N)^2)*(2*J1(v)/v)^2; v=pi*r/(lambda*N).
    No Prysm propagator or discrete pupil is used by this reference.
    """
    q, w = leggauss(order)
    x = ((np.arange(size)-size//2)*PITCH)[:, None]+q[None]*PITCH/2
    radius = np.sqrt(x[:, None, :, None]**2+x[None, :, None, :]**2)
    weights = w[:, None]*w[None, :]*(PITCH/2)**2
    result = []
    for wavelength in WAVE:
        scale = wavelength*1e-3*f_number
        v = np.pi*radius/scale
        amp = np.divide(2*j1(v), v, out=np.ones_like(v), where=v != 0)
        result.append((np.pi/(4*scale**2)*amp**2*weights).sum(axis=(-2, -1)))
    return np.stack(result)


def independent_charge(request, loss, f_number):
    """Direct kernel sum and analytic cone/EQE/spectral integration.

    The supplied PSF is an input to this application reference; Airy separately
    checks PSF generation. Uniform pupil-area E[theta] equals
    atan(a)-(a-atan(a))/a^2 with a=1/(2N), in radians.
    """
    image = request["scene"]["spectral_irradiance"]
    kernels = np.asarray(request["psf"]["values"])
    h, width, _ = image.shape
    blurred = np.zeros_like(image)
    center = kernels.shape[-1]//2
    for ky in range(kernels.shape[-2]):
        dy = ky-center
        oy, iy = slice(max(0, dy), min(h, h+dy)), slice(max(0, -dy), min(h, h-dy))
        for kx in range(kernels.shape[-1]):
            dx = kx-center
            ox, ix = slice(max(0, dx), min(width, width+dx)), slice(max(0, -dx), min(width, width-dx))
            blurred[oy, ox] += image[iy, ix]*kernels[:, ky, kx]
    a = 1/(2*f_number)
    angle = np.rad2deg(np.arctan(a)-(a-np.arctan(a))/a**2)
    eqe = SITE_EQE*(1-np.asarray(loss)*angle/20)
    photons = blurred*np.asarray(WAVE)*1e-9/(6.62607015e-34*299792458)
    integral = (WAVE[1]-WAVE[0])*(photons[..., 0]+photons[..., 1])/2
    channels = integral[..., None]*(PITCH*1e-6)**2*eqe
    sites = np.array([[0, 1], [2, 3]])[np.arange(h)[:, None]%2, np.arange(width)[None, :]%2]
    return np.take_along_axis(channels, sites[..., None], axis=-1)[..., 0]*EXPOSURE


def reference_raw(photo, model, gain):
    """Independent noiseless transfer; keep well and ADC clipping distinct."""
    if model["fixed_pattern"]["prnu_sigma"] != 0 or model["dark_current_e_per_s"] != 0:
        raise ValueError("Reference requires zero PRNU/dark fixture")
    adc = model["adc"]
    white, black = 2**adc["bit_depth"]-1, adc["black_level_dn"]
    q = np.clip(photo, 0, model["full_well_e"])
    value = black+q*model["conversion_gain_v_per_e"]*gain*white/adc["full_scale_v"]
    digitized = np.clip(np.rint(value), 0, white)
    return np.clip(np.rint(black+(digitized-black)*model["digital_gain"]), 0, white).astype(np.int32)


def png_uri(array, black, white):
    pixels = np.uint8(np.rint(np.clip((array-black)/(white-black), 0, 1)*255))
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="PNG")
    return "data:image/png;base64,"+base64.b64encode(buffer.getvalue()).decode()


def write_report(folder, report):
    (folder/"results.json").write_bytes(encoded(report))
    rows = report["cases"]
    fields = ["case", "f_number", "pixel", "gain", "light", "throughput", "mean_photocharge_e",
              "green_raw_dn", "well_fraction", "adc_high_fraction",
              "charge_reference_relative_error", "raw_reference_max_dn"]
    with (folder/"comparison.csv").open("w", newline="", encoding="utf8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    lines = ["# 두 조건 일반 응답: 광학 → 픽셀 전하 → 전기 RAW", "",
        "기존 PSF Factory·네 사이트 EQE·전기 Runtime을 실제로 연결한 합성 연구다.",
        "고정 초점거리 16 mm, F/4·F/8, 2 µm/32×32 픽셀, 500/600 nm, 노출 4 ms.",
        "두 픽셀은 정상 입사 EQE가 같고 각도에 따른 손실만 다르다. gain은 1·2다.",
        "균일 원형 동공·근축 1/N² 광량·분리된 telecentric 방향 분포를 명시했다.",
        "실제 렌즈 설계나 실측 센서의 예측 정확도 검증, 등록 모델 승격은 아니다.", "",
        "## 실제 후보 결과", "",
        "| 조리개 | 픽셀 응답 | 광원 | gain | 평균 광전하(e) | Green RAW(black-relative DN) | Well 포화 | ADC 상단 |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| F/{r['f_number']:g} | {r['pixel']} | {r['light']} | {r['gain']:g} | "
            f"{r['mean_photocharge_e']:.3f} | {r['green_raw_dn']:.3f} | "
            f"{100*r['well_fraction']:.2f}% | {100*r['adc_high_fraction']:.2f}% |")
    lines += ["", "## PSF와 독립 기준", "",
        "| 조리개 | 500/600 nm Kernel OTF @ 0.125 cycles/pixel | Airy 대비 최대 L1 | 유한범위 에너지 오차 최대 |",
        "|---|---|---:|---:|"]
    for p in report["optics"]:
        lines.append(f"| F/{p['f_number']:g} | {' / '.join(f'{v:.5f}' for v in p['otf_at_eighth_cycle'])} | "
                     f"{p['airy_kernel_l1_max']:.6f} | {p['airy_energy_error_max']:.6f} |")
    lines += ["", "OTF에는 수신 픽셀 적분과 유한 PSF 정규화가 포함되며 렌즈 단독 MTF가 아니다.",
        "PSF 생성은 연속 Airy 픽셀 적분과 비교했다. PSF 적용은 별도 직접 합산으로 비교했다.",
        "각도 적분은 균일 동공의 평균 각도 해석식, 전하는 면적·파장·노출 적분과 비교했다.",
        "ADC는 독립 수식으로 검산했다. 단위·계산 적용 검증과 실제 물리 정확도는 구분한다.", "",
        "## 설계 판단과 경계", ""]
    lines.extend(report.get("conclusions", []))
    lines += ["", "잡음·실측 오차·온도·동적 장면·고객 설치 검증은 이번에 수행하지 않았다.",
        "방향 계약의 해시 슬롯은 작성한 해석 모델의 식별자이며 실제 처방 파일의 해시가 아니다.",
        "기존 RAW 필터·기본 앱·DB·등록 모델은 변경하지 않았다.", ""]
    (folder/"report.md").write_text("\n".join(lines), encoding="utf8")
    cards = []
    for r in rows:
        with np.load(folder/(r["case"]+".npz"), allow_pickle=False) as data:
            raw = png_uri(data["raw_dn"], 64, 4095)
            clipped = png_uri(data["adc_high_mask"].astype(float), 0, 1)
        cards.append(f"<article><h3>{html.escape(r['case'])}</h3><div class='pair'>"
            f"<figure><img src='{raw}'><figcaption>RAW · 공통 64–4095 DN</figcaption></figure>"
            f"<figure><img src='{clipped}'><figcaption>ADC 상단 마스크</figcaption></figure></div>"
            f"<p>광전하 {r['mean_photocharge_e']:.2f} e · Well {100*r['well_fraction']:.2f}% · "
            f"ADC {100*r['adc_high_fraction']:.2f}%</p></article>")
    page = """<!doctype html><html lang='ko'><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>응답 조건 비교</title>
<style>body{font:16px system-ui;margin:32px;max-width:1200px}h1{font-size:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:20px}
article{border:1px solid #aaa;padding:16px}h3{overflow-wrap:anywhere;font-size:17px}
.pair{display:flex;gap:12px}figure{margin:0;width:48%}img{width:100%;image-rendering:pixelated}
figcaption,p{font-size:13px;line-height:1.6}pre{white-space:pre-wrap;line-height:1.6}</style>
<h1>광학 → 픽셀 → 전기: 실제 RAW 비교</h1>
<p>동일 영상·노출, 조리개/픽셀/gain 비교. 무잡음 합성 연구이며 실물 검증은 아닙니다.</p>
<p>모든 RAW는 같은 black–white 범위로 표시합니다. 후보별 자동 명암 확대를 하지 않습니다.</p>
<p>ADC 마스크는 반올림 전 연속 코드의 상한 판정입니다. 최종 RAW=4095와 다를 수 있으며, 마스크 0이 전하 포화 부재를 뜻하지 않습니다. <a href='interpretation.md'>최종 RAW 상단을 분리한 설계 해석</a></p>
<div class='grid'>""" + "".join(cards)+"</div><h2>계산 결과와 해석</h2><pre>"+html.escape("\n".join(lines))+"</pre></html>"
    (folder/"comparison.html").write_text(page, encoding="utf8")


def summarize_saved(folder):
    """Read existing arrays once; distinguish final code rails from ADC masks.

    In the existing readout engine adc_high_mask uses the continuous pre-round
    code. A value just below white can round to white. Neither mask alone is
    evidence of absent upstream well saturation. No physics is repeated here.
    """
    folder = folder.resolve()
    if ROOT/"reports"/"camera_platform" not in folder.parents:
        raise ValueError("Use a comparison under reports/camera_platform")
    targets = [folder/"rail-readback.json", folder/"interpretation.md"]
    if any(path.exists() for path in targets):
        raise FileExistsError("Saved interpretation already exists; preserve it")
    content = (folder/"results.json").read_bytes()
    results = json.loads(content)
    if results.get("status") != "completed":
        raise ValueError("A completed comparison is required")
    rows = []
    for row in results["cases"]:
        name = row["case"]
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("Invalid case name")
        with np.load(folder/(name+".npz"), allow_pickle=False) as saved:
            raw = saved["raw_dn"]
            rows.append({**row, "raw_high_rail_fraction": float((raw == 4095).mean()),
                         "raw_low_rail_fraction": float((raw == 0).mean()),
                         "raw_bytes_sha256": hashlib.sha256(raw.tobytes()).hexdigest()})
    analysis = {"source_results_sha256": hashlib.sha256(content).hexdigest(),
                "kind": "saved_array_readback_no_camera_rerun", "cases": rows}
    targets[0].write_bytes(encoded(analysis))
    lines = ["# 광학·픽셀·전기 비교: 설계 해석", "",
        "조리개 2개 × 픽셀 응답 2개 × gain 2개 × 분광조도 2개 = 실제 응답 계산 16개.",
        "광학은 새 Prysm Factory PSF 2개, 픽셀은 정상 EQE가 같은 두 각도 응답이다.",
        "기존 처방·등록 모델이 아니라 가정을 명시한 이상 동공/센서 연구다.", "",
        "## 밝은 장면: 전하 저장소 포화와 최종 RAW 상단 도달은 다르다", "",
        "| 조리개 | 픽셀 응답 | gain | 평균 생성 광전하(e) | Well 포화 | 최종 RAW=4095 |",
        "|---|---|---:|---:|---:|---:|"]
    for row in rows:
        if row["light"] == "bright":
            lines.append(f"| F/{row['f_number']:g} | {row['pixel']} | {row['gain']:g} | "
                f"{row['mean_photocharge_e']:.3f} | {100*row['well_fraction']:.2f}% | {100*row['raw_high_rail_fraction']:.2f}% |")
    lines += ["", "광전하는 clipping 전 값이다. 높은 평균 전하가 항상 좋은 RAW를 뜻하지 않는다.",
        "기존 adc_high_mask는 반올림 전 연속 ADC 코드의 경계를 검사한다. 최종 정수 RAW=4095와 별도다.",
        "F/4 gain1에서는 부동소수점 연산상 ADC 경계 바로 아래라도 반올림 후 4095가 된다.",
        "따라서 이전 표의 adc_high_fraction=0을 포화가 없다는 뜻으로 해석하지 않는다.", "",
        "## 얻은 설계 판단", "", *results["conclusions"], "",
        "## 독립 수치 대조", "",
        f"전하 최대 상대 차이: {max(r['charge_reference_relative_error'] for r in rows):.9g}.",
        f"독립 전하/전기 식에서 얻은 최종 RAW 최대 차이: {max(r['raw_reference_max_dn'] for r in rows)} DN.",
        "Airy 기준은 독립 연속 회절식의 픽셀 적분이다. 전하 기준은 직접 PSF 합산·평균 입사각 해석식·면적/파장/노출 적분이다.",
        "PSF 적용 검사에서는 공급 PSF를 사용하므로 PSF 생성 검증과 구분한다.", "",
        "## 경계", "",
        "1/N² 광량, 무수차 균일 동공, telecentric 각도 분포, 두 파장 사다리꼴 적분은 작성한 가정이다.",
        "Kernel OTF는 수신 픽셀 적분·유한 영역 정규화를 포함한다. 렌즈 단독 MTF나 실측 SFR가 아니다.",
        "PSF와 입사 방향의 공간 상관, 실제 코팅·미광·렌즈 수차·픽셀 누화·잡음·온도는 검증하지 않았다.",
        "이번 결과는 기존 API/설치 승격이나 실측 정확도가 아니라, 비교 가능한 수치 경로와 설계 상충을 확인한 것이다.",
        "다음은 같은 비교기를 기존 처방 Factory의 두 광학 조건에 연결하고, 실측 자료가 확보된 부분부터 별도로 대조한다.", ""]
    targets[1].write_text("\n".join(lines), encoding="utf8")
    print("ANALYSIS", targets[1], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summarize-existing", type=Path)
    args = parser.parse_args()
    if args.summarize_existing is not None:
        summarize_saved(args.summarize_existing)
        return
    if args.output is None:
        parser.error("--output is required for a new comparison")
    folder = args.output.resolve()
    if ROOT/"reports"/"camera_platform" not in folder.parents or folder.exists():
        parser.error("Use a NEW directory under reports/camera_platform")
    folder.mkdir(parents=True, exist_ok=False)
    plan = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "f_numbers": [4.0, 8.0], "pixel_angular_losses": PIXELS,
        "normal_site_eqe": SITE_EQE.tolist(), "gains": [1.0, 2.0],
        "ideal_irradiance_at_f4": LIGHTS, "focal_length_mm": FOCAL_MM,
        "exposure_s": EXPOSURE, "wavelength_nm": WAVE, "pixel_pitch_um": PITCH,
        "frame_shape": [SIZE, SIZE], "noise_enabled": False,
        "criteria_before_results": {"airy_kernel_l1_max": 0.006,
            "airy_retained_energy_abs_error_max": 0.0005,
            "independent_charge_relative_error_max": 2e-6,
            "end_to_end_integer_raw_difference_max_dn": 1,
            "electrical_reference_using_actual_charge": "exact integer RAW",
            "causal_effects": "gain leaves charge unchanged; pixel leaves PSF blur unchanged",
            "expected_optical_change": "F/8 kernel OTF lower than F/4 at 0.125 cycles/pixel"},
        "scope": "authored separable paraxial pupil; numerical research, not factory-published model",
    }
    (folder/"study-plan.json").write_bytes(encoded(plan))
    report = {"plan": plan, "status": "running", "optics": [], "cases": [],
              "physical_validation": "not_established", "separate_worker_runs": 0}
    outputs = {}
    try:
        for fnum in plan["f_numbers"]:
            spec = psf_factory.request_template()
            spec.update(wavelength_nm=WAVE.copy(), f_number=fnum,
                pupil_diameter_mm=FOCAL_MM/fnum, pixel_pitch_um=PITCH,
                pupil_samples=128, kernel_size=31, subpixel_samples=5,
                opd_nm={"defocus": 0.0, "astigmatism": 0.0, "coma_x": 0.0},
                minimum_retained_energy=0.95)
            result = psf_factory.characterize(spec)
            np.savez_compressed(folder/f"psf-f{fnum:g}.npz", **result["arrays"])
            (folder/f"psf-f{fnum:g}.json").write_bytes(encoded(result["summary"]))
            if result["model_candidate"] is None:
                raise AssertionError(f"Existing PSF numerical admission refused F/{fnum:g}")
            reference = airy_bins(fnum)
            ref_norm = reference/reference.sum(axis=(1, 2))[:, None, None]
            kernel = result["arrays"]["psf_kernel"]
            l1 = float(np.max(np.abs(kernel-ref_norm).sum(axis=(1, 2))))
            energy_error = float(np.max(np.abs(result["arrays"]["retained_energy_fraction"][-1]-reference.sum(axis=(1, 2)))))
            x = np.arange(kernel.shape[-1])-kernel.shape[-1]//2
            otf = np.abs((kernel*np.exp(-2j*np.pi*0.125*x)[None, None, :]).sum(axis=(1, 2)))
            optical_row = {"f_number": fnum, "otf_at_eighth_cycle": otf.tolist(),
                "airy_kernel_l1_max": l1, "airy_energy_error_max": energy_error,
                "retained_energy": result["arrays"]["retained_energy_fraction"][-1].tolist()}
            report["optics"].append(optical_row)
            np.savez_compressed(folder/f"airy-reference-f{fnum:g}.npz", bin_energy=reference, normalized=ref_norm)
            print("OPTICS", json.dumps(optical_row), flush=True)
            if l1 > 0.006 or energy_error > 0.0005:
                raise AssertionError("Independent Airy reference criterion not met; do not relax it")
            lens, definition = pupil_lens(fnum)
            (folder/f"pupil-f{fnum:g}.json").write_bytes(encoded(definition))
            for pixel, losses in PIXELS.items():
                for light, intensity in LIGHTS.items():
                    request = base_request(result["model_candidate"], lens, native_response(losses), intensity, (4/fnum)**2)
                    model = electrical.default_electrical_model(request["readout"])
                    expected_photo = independent_charge(request, losses, fnum)
                    for gain in plan["gains"]:
                        label = f"f{fnum:g}-{pixel}-{light}-g{gain:g}"
                        output = evaluate_response(request, model, exposure_s=EXPOSURE, analog_gain=gain)
                        arrays = output["arrays"]
                        photo, raw = arrays["mean_photocharge_e"], arrays["raw_dn"]
                        relative_error = float(np.max(np.abs(photo-expected_photo)/np.maximum(expected_photo, 1e-12)))
                        raw_error = int(np.max(np.abs(raw-reference_raw(expected_photo, model, gain))))
                        np.testing.assert_array_equal(raw, reference_raw(photo, model, gain))
                        if relative_error > 2e-6 or raw_error > 1:
                            raise AssertionError("Independent charge/RAW criterion failed")
                        if gain == 2:
                            np.testing.assert_array_equal(photo, outputs[(fnum, pixel, light, 1.0)]["mean_photocharge_e"])
                        if pixel == "wide_angle":
                            np.testing.assert_array_equal(arrays["blurred_spectral_irradiance"],
                                outputs[(fnum, "angular_sensitive", light, gain)]["blurred_spectral_irradiance"])
                        outputs[(fnum, pixel, light, gain)] = arrays
                        site = arrays["native_site_index"]
                        row = {"case": label, "f_number": fnum, "pixel": pixel, "light": light,
                            "gain": gain, "throughput": (4/fnum)**2,
                            "mean_photocharge_e": float(photo.mean()),
                            "green_raw_dn": float((raw[(site == 1)|(site == 2)]-64).mean()),
                            "site_charge_means_e": arrays["channel_mean_photocharge_e"].mean(axis=(0, 1)).tolist(),
                            "well_fraction": float(arrays["well_saturated_mask"].mean()),
                            "adc_high_fraction": float(arrays["adc_high_mask"].mean()),
                            "adc_high_without_well_fraction": float((arrays["adc_high_mask"] & ~arrays["well_saturated_mask"]).mean()),
                            "charge_reference_relative_error": relative_error, "raw_reference_max_dn": raw_error}
                        np.savez_compressed(folder/(label+".npz"), **arrays, independent_photo_e=expected_photo)
                        metadata = {**deepcopy(request), "electrical_model": model,
                                    "exposure_s": EXPOSURE, "analog_gain": gain}
                        metadata["scene"]["spectral_irradiance"] = metadata["scene"]["spectral_irradiance"].tolist()
                        (folder/(label+"-request.json")).write_bytes(encoded(metadata))
                        (folder/(label+"-summary.json")).write_bytes(encoded(output["summary"]))
                        report["cases"].append(row)
                        print("CASE", label, "photo", round(row["mean_photocharge_e"], 3),
                              "well", row["well_fraction"], "ADC", row["adc_high_fraction"], flush=True)
        if not np.all(np.asarray(report["optics"][1]["otf_at_eighth_cycle"]) < report["optics"][0]["otf_at_eighth_cycle"]):
            raise AssertionError("Expected diffraction broadening absent")
        conclusions = ["F/4→F/8에서는 입력 광량을 명시적 근축 가정에 따라 1/4로 줄였고 PSF도 다시 계산했다.",
            "좁아진 입사각 분포는 각도 손실을 줄이지만 광량 감소와 회절 확대를 되돌리지는 않는다.",
            "gain 1→2는 광전하를 늘리지 않는다. ADC 상단과 full-well 포화는 별도 마스크로 저장한다."]
        for fnum in plan["f_numbers"]:
            group = {r["pixel"]: r for r in report["cases"] if r["f_number"] == fnum and r["light"] == "low" and r["gain"] == 1}
            a, b = group["angular_sensitive"], group["wide_angle"]
            ratio = b["site_charge_means_e"][1]/a["site_charge_means_e"][1]
            conclusions.append(f"F/{fnum:g}에서 wide-angle 픽셀의 Gr 광전하가 {100*(ratio-1):.3f}% 증가했다. 이는 작성한 EQE 가정 안의 비교다.")
        report.update(status="completed", criteria_pass=True, response_chain_evaluations=len(report["cases"]),
                      psf_characterizations=len(report["optics"]), conclusions=conclusions)
        write_report(folder, report)
        summarize_saved(folder)
        print("RESULT", folder/"report.md", flush=True)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        (folder/"failed.json").write_bytes(encoded(report))
        raise


if __name__ == "__main__":
    main()

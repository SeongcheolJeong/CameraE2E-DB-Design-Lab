"""Source-pinned IRCF spectral acquisition and same-RAW color reprocessing."""

from __future__ import annotations

import html

import numpy as np

from pyisetcam.metrics import delta_e_ab

from . import electrical
from . import reference_ircf_spectra as spectra
from . import reference_workshop_optics as optics
from .reference_isp_color import fit_white_constrained
from .reference_sensor_modes import process_electrons
from .reference_workshop_engine import write_json
from .virtual_lab_engine import XYZ_TO_RGB

EXPOSURE = 0.004
SITE_POSITIONS = ((0, 0), (0, 1), (1, 0), (1, 1))


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def rgb_sites(values):
    return np.stack((values[..., 0], values[..., 1:3].mean(axis=-1), values[..., 3]), axis=-1)


def mosaic(planes):
    rows, cols = np.indices(planes.shape[:2])
    sites = np.array([[0, 1], [2, 3]])[rows % 2, cols % 2]
    return np.take_along_axis(planes, sites[..., None], axis=-1)[..., 0]


def callback(assets, response, filter_id, angular_mode, n_eff):
    def apply(theta, phi, wave):
        transmission = spectra.filter_transmission(
            assets, filter_id, wave, theta, angular_mode=angular_mode, n_eff=n_eff
        )
        return spectra.absolute_response(response, theta, phi, wave) * transmission[..., None]

    return apply


def spectral_response(solution, field, assets, response, filter_id, angular_mode, n_eff):
    """Per-band uniform-plane response; same admitted pupil weights as convolution."""
    arrays = solution["arrays"]
    ri = [0.0, 0.5, 0.85].index(field["normalized_radius"])
    wave = np.asarray(assets["wavelength_nm"])
    effective, transmissions = [], []
    for wi, wavelength in enumerate(wave):
        # Share the exact spectral direction table and admitted photon weights
        # with convolution. The legacy directions_xyz table is only a reference
        # wavelength and cannot calibrate the extended IRCF optical response.
        theta, phi, weights = optics.pupil_samples(solution, field["id"], wi)
        t = spectra.filter_transmission(
            assets, filter_id, float(wavelength), theta, angular_mode=angular_mode, n_eff=n_eff
        )
        qe = spectra.absolute_response(response, theta, phi, float(wavelength))
        effective.append(np.sum(weights[:, None] * t[:, None] * qe, axis=0))
        transmissions.append(float(np.sum(weights * t)))
    effective = np.asarray(effective)
    factor = arrays["irradiance_per_radiance_sr"][:, ri] * (2.9e-6) ** 2
    factor *= np.asarray(assets["wavelength_bin_width_nm"])
    return effective * factor[:, None], np.asarray(transmissions)


def calibrations(solution, assets, response, fields, filters, n_eff):
    center = next(f for f in solution["result"]["fields"] if f["id"] == "center")
    light = assets["illuminants"]["d65_visible"]["photons_per_m2_sr_s_nm"]
    visible = np.asarray(assets["wavelength_nm"]) <= 700
    reflectances = assets["munsell_visible"]["reflectance"]
    target = light[:, None] * assets["xyz_per_photon"] / 1000
    matrices, records = {}, {}
    for filter_id in dict.fromkeys(["baseline", *filters]):
        rate, _ = spectral_response(solution, center, assets, response, filter_id, "pupil", n_eff)
        per_band = rgb_sites(rate * light[:, None] * EXPOSURE)
        x = reflectances.T @ per_band[visible]
        y = reflectances.T @ target[visible]
        matrix_xyz = fit_white_constrained(
            x, y, per_band[visible].sum(axis=0), target[visible].sum(axis=0)
        )
        matrices[filter_id] = matrix_xyz @ XYZ_TO_RGB.T
        records[filter_id] = {
            "matrix": matrices[filter_id].tolist(),
            "matrix_xyz": matrix_xyz.tolist(),
            "method": "Munsell64_visible400_700_D65_center_pupil_white_constrained",
            "filter_id": filter_id,
            "training_count": 64,
            "evaluation_used_for_fit": False,
            "white_error": float(
                np.max(
                    np.abs(per_band[visible].sum(axis=0) @ matrix_xyz - target[visible].sum(axis=0))
                )
            ),
            "nir_aware_fit": False,
        }
    base, _ = spectral_response(solution, center, assets, response, "baseline", "pupil", n_eff)
    center_white = (base * light[:, None]).sum(axis=0)
    lsc = {}
    for field in fields:
        rate, _ = spectral_response(solution, field, assets, response, "baseline", "pupil", n_eff)
        current_white = (rate * light[:, None]).sum(axis=0)
        gains = np.minimum(center_white / np.maximum(current_white, 1e-30), 4)
        image = np.empty((256, 256))
        for site, (r, c) in enumerate(SITE_POSITIONS):
            image[r::2, c::2] = gains[site]
        lsc[field["id"]] = image
    return (
        matrices,
        lsc,
        {
            "calibrations": records,
            "white_balance": [1, 1, 1],
            "lsc_basis": "baseline_filter_visible_D65_fixed_per_field_site_gain_max4",
            "center_matrix_applied_to_all_fields": True,
            "asset_metadata": assets["metadata"],
            "physical_calibration": False,
        },
    )


def chart_pattern(geometry):
    rows, cols = geometry["input_shape_row_col"]
    r0, _, c0, _ = geometry["crop_bounds_row_col"]
    yy, xx = np.indices((rows, cols))
    # Extend the same physical patch coordinate definition through the halo.
    py = np.minimum(np.maximum((yy - r0) * 4 // 256, 0), 3)
    px = np.minimum(np.maximum((xx - c0) * 6 // 256, 0), 5)
    return py * 6 + px


def scene(assets, field, illuminant, chart):
    light = assets["illuminants"][illuminant]["photons_per_m2_sr_s_nm"]
    shape = tuple(field["geometry"]["input_shape_row_col"])
    if chart == "uniform":
        return np.broadcast_to(light, (*shape, len(light)))
    pattern = chart_pattern(field["geometry"])
    return assets["macbeth_reflectance"].T[pattern] * light


def patch_regions():
    regions = []
    for r in range(4):
        for c in range(6):
            r0, r1 = int(np.ceil(r * 256 / 4)), int(np.ceil((r + 1) * 256 / 4))
            c0, c1 = int(np.ceil(c * 256 / 6)), int(np.ceil((c + 1) * 256 / 6))
            mr, mc = (r1 - r0) // 4, (c1 - c0) // 4
            regions.append((r0 + mr, r1 - mr, c0 + mc, c1 - mc))
    return regions


def png(path, image):
    from PIL import Image

    Image.fromarray(np.uint8(np.round(np.clip(image, 0, 1) * 255))).save(path)


def acquire(rate, model, seed, progress=None):
    rows = []
    for frame in range(8):
        if progress:
            progress()
        rows.append(
            electrical.capture_electrons(
                np.asarray(rate * EXPOSURE),
                EXPOSURE,
                model,
                analog_gain=1,
                temperature_c=25,
                geometry={"full_sensor_shape": [256, 256], "crop_origin": [0, 0]},
                noise_enabled=True,
                seed=(seed + frame) % 2**32,
            )
        )
    raw = np.stack([r["arrays"]["raw_dn"] for r in rows])
    well = np.stack([r["arrays"]["well_saturated_mask"] for r in rows])
    adc = np.stack([r["arrays"]["adc_low_mask"] | r["arrays"]["adc_high_mask"] for r in rows])
    calibration = rows[0]["calibration"]
    electrons = (raw.astype(float) - calibration["black_level_dn"]) / calibration[
        "dn_per_e"
    ] - calibration["dark_e"]
    valid = ~(well | adc)
    variance = (
        rows[0]["arrays"]["mean_total_charge_e"]
        + calibration["read_noise_variance_e2"]
        + calibration["quantization_variance_e2"]
    )
    return {
        "raw_frames": raw,
        "electron_frames": electrons,
        "well_saturated_frames": well,
        "adc_saturated_frames": adc,
        "valid_frames": valid,
        "variance_e2": variance,
        "calibration": calibration,
        "summary": rows[0]["summary"],
        "seed_sequence": [(seed + i) % 2**32 for i in range(8)],
    }


def processed_metrics(capture, matrix, lsc, spec, assets, illuminant, chart):
    outputs = [
        process_electrons(
            e,
            matrix,
            lsc_gain=lsc,
            display_white=spec["isp"]["display_white_level"],
            gamma=spec["isp"]["tone_gamma"],
            valid=v,
            variance_e2=capture["variance_e2"],
        )
        for e, v in zip(capture["electron_frames"], capture["valid_frames"], strict=True)
    ]
    linear = np.stack([o["linear"] for o in outputs])
    valid = np.stack([o["valid"] for o in outputs]).all(axis=0)
    interior = np.zeros_like(valid)
    interior[8:-8, 8:-8] = True
    common = valid & interior
    dark_corrected = capture["electron_frames"][:, common]
    noise = float(np.sqrt(np.var(dark_corrected, axis=0, ddof=1).mean())) if common.any() else None
    rgb_noise = (
        np.sqrt(np.var(linear[:, common], axis=0, ddof=1).mean(axis=0)).tolist()
        if common.any()
        else [None] * 3
    )
    luma = linear @ np.array([0.2126, 0.7152, 0.0722])
    errors = []
    light = assets["illuminants"][illuminant]["photons_per_m2_sr_s_nm"]
    white = np.sum(light[:, None] * assets["xyz_per_photon"], axis=0) / 1000
    if chart == "color":
        targets = (
            assets["macbeth_reflectance"].T @ (light[:, None] * assets["xyz_per_photon"]) / 1000
        )
        mean = linear.mean(axis=0) @ np.linalg.inv(XYZ_TO_RGB.T)
        for idx, (r0, r1, c0, c1) in enumerate(patch_regions()):
            mask = valid[r0:r1, c0:c1]
            if not mask.all():
                errors.append(None)
            else:
                prediction = mean[r0:r1, c0:c1].mean(axis=(0, 1))
                errors.append(float(delta_e_ab(targets[idx], prediction, white, "2000")))
    finite = [e for e in errors if e is not None]
    metrics = {
        "mean_electrons": float(dark_corrected.mean()) if common.any() else None,
        "temporal_noise_e": noise,
        "rgb_temporal_noise_linear": rgb_noise,
        "luminance_temporal_noise": float(np.sqrt(np.var(luma[:, common], axis=0, ddof=1).mean()))
        if common.any()
        else None,
        "delta_e00_mean": float(np.mean(finite)) if finite else None,
        "delta_e00_max": float(np.max(finite)) if finite else None,
        "color_valid_patches": len(finite),
        "color_total_patches": len(errors),
        "well_saturation_fraction": float(capture["well_saturated_frames"].mean()),
        "adc_saturation_fraction": float(capture["adc_saturated_frames"].mean()),
        "valid_fraction": float(common[8:-8, 8:-8].mean()),
        "noise_scope": (
            "pixelwise temporal sample variance; fixed interior, all-repeat-valid; units explicit"
        ),
        "rgb_noise_unit": "signed_linear_srgb",
    }
    return {
        "linear": linear,
        "display": outputs[0]["display"],
        "valid": valid,
        "metrics": metrics,
        "patch_delta_e": errors,
    }


def run_case(root, item, spec, solution, assets, pixel, matrices, lsc, progress):
    field = next(f for f in solution["result"]["fields"] if f["id"] == item["field_id"])
    wave = np.asarray(assets["wavelength_nm"])
    cube = scene(assets, field, item["illuminant"], item["chart"])
    result = optics.apply_field(
        solution,
        field["id"],
        cube,
        angular_response=callback(
            assets,
            pixel["response"],
            item["filter_id"],
            item["angular_mode"],
            spec["effective_index"],
        ),
        spectral_groups={"vis": wave <= 700, "nir": wave > 700},
    )
    del cube
    target = root / "cases" / item["id"]
    target.mkdir(parents=True, exist_ok=True)
    rate = result["mean_electron_rate"]
    captured = acquire(
        rate, pixel["cell"]["electrical_model"], spec["seed"] + item["seed_offset"], progress
    )
    calibration = captured.pop("calibration")
    summary = captured.pop("summary")
    seeds = captured.pop("seed_sequence")
    np.savez_compressed(target / "capture.npz", **captured, dark_e=calibration["dark_e"])
    write_json(
        target / "readout.json",
        json_value({"calibration": calibration, "capture": summary, "seed_sequence": seeds}),
    )
    arrays = {
        "mean_electron_rate": rate,
        "planes_electron_rate": result["planes_electron_rate"],
        "lsc_gain": lsc[field["id"]],
    }
    groups = result["group_planes_electron_rate"]
    for key, value in groups.items():
        arrays[key + "_planes_electron_rate"] = value
    denominator = float(result["planes_electron_rate"].sum())
    fraction = float(groups["nir"].sum() / denominator) if denominator else 0
    site_means = (result["planes_electron_rate"].mean(axis=(0, 1)) * EXPOSURE).tolist()
    metrics, patches, files = {}, {}, {}
    for ccm, matrix in (("baseline", matrices["baseline"]), ("refit", matrices[item["filter_id"]])):
        rendered = processed_metrics(
            captured, matrix, lsc[field["id"]], spec, assets, item["illuminant"], item["chart"]
        )
        png(target / (ccm + ".png"), rendered["display"])
        arrays[ccm + "_linear_frames"] = rendered["linear"]
        arrays[ccm + "_valid"] = rendered["valid"]
        metrics[ccm] = {
            **rendered["metrics"],
            "nir_fraction": fraction,
            "site_mean_electrons": site_means,
        }
        patches[ccm] = rendered["patch_delta_e"]
        files[ccm + "_display"] = f"cases/{item['id']}/{ccm}.png"
    png(target / "raw.png", captured["raw_frames"][0] / calibration["white_level_dn"])
    np.savez_compressed(target / "arrays.npz", **arrays)
    unit, transmission = spectral_response(
        solution,
        field,
        assets,
        pixel["response"],
        item["filter_id"],
        item["angular_mode"],
        spec["effective_index"],
    )
    light = assets["illuminants"][item["illuminant"]]["photons_per_m2_sr_s_nm"]
    mean_reflectance = (
        assets["macbeth_reflectance"].mean(axis=1)
        if item["chart"] == "color"
        else np.ones(len(wave))
    )
    spectral = {
        "wavelength_nm": wave,
        "transmission_normal": spectra.filter_transmission(
            assets,
            item["filter_id"],
            wave,
            0,
            angular_mode="independent",
            n_eff=spec["effective_index"],
        ),
        "transmission_effective": transmission,
        "site_electrons_per_band": unit * (light * mean_reflectance)[:, None] * EXPOSURE,
        "contribution_scope": (
            "equal-weight uniform patch diagnostic; spatially integrated totals in arrays"
        ),
        "nir_definition": (
            "wavelength >700nm; explicit study band, not a claim of zero visual response"
        ),
    }
    files.update(
        {
            "raw": f"cases/{item['id']}/capture.npz",
            "raw_preview": f"cases/{item['id']}/raw.png",
            "arrays": f"cases/{item['id']}/arrays.npz",
            "readout": f"cases/{item['id']}/readout.json",
        }
    )
    case = {
        **item,
        "metrics": metrics,
        "patch_delta_e": patches,
        "files": files,
        "spectral": spectral,
        "optical_metadata": result["metadata"],
        "geometry": field["geometry"],
        "ccm_reuses_same_raw": True,
        "source": spec["source"],
        "exposure_ms": 4,
        "frames": 8,
        "nir_scale": spec["nir_scale"],
    }
    write_json(target / "case.json", json_value(case))
    return json_value(case)


def report(root, result, cases):
    e = html.escape
    rows = []

    def n(value):
        return "N/A" if value is None else f"{value:.3f}"

    for case in cases:
        metrics = case["metrics"]
        rows.append(
            f"<tr><td>{e(case['id'])}</td><td>{e(case['field_id'])}</td>"
            f"<td>{e(case['filter_id'])} / {e(case['angular_mode'])}</td>"
            f"<td>{e(case['illuminant'])} / {e(case['chart'])}</td>"
            f"<td>{n(metrics['baseline']['nir_fraction'] * 100)}%</td>"
            f"<td>{n(metrics['baseline']['delta_e00_mean'])} → "
            f"{n(metrics['refit']['delta_e00_mean'])}</td>"
            f"<td><img src='{e(case['files']['baseline_display'], quote=True)}'>"
            f"<img src='{e(case['files']['refit_display'], quote=True)}'></td></tr>"
        )
    attribution = (
        "<h2>분광 자료와 이용 조건</h2>"
        "<p><a href='spectral-assets.json'>분광 입력·반사율·출처 해시와 이용 조건</a></p>"
        "<p>색 일치 함수: CIE 2019, Colour-matching functions of CIE 1931 standard "
        "colorimetric observer, "
        "<a href='https://doi.org/10.25039/CIE.DS.xvudnb9b'>DOI 10.25039/CIE.DS.xvudnb9b</a>. "
        "<a href='https://creativecommons.org/licenses/by-sa/4.0/'>CC BY-SA 4.0</a>. "
        "원본 1 nm 표에서 400–830 nm의 5 nm 표본을 선택하고 선형 보간했습니다. "
        "해당 데이터 파생본에는 같은 이용 조건이 적용됩니다.</p>"
    )
    (root / "report.html").write_text(
        "<!doctype html><html lang='ko'><meta charset='utf-8'>"
        "<title>Reference IRCF Design Lab</title><style>"
        "body{font:15px system-ui;margin:35px;color:#193d38}"
        "table{border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #ddd}"
        "img{width:160px;margin:3px}code{word-break:break-all}</style>"
        "<h1>Reference IRCF Design Lab</h1>"
        "<p>특허 렌즈 × 가시광 구조 모델 + 선언 NIR EQE proxy. "
        "실제 IMX462 NIR 성능·필터 제품 검증이 아닙니다.</p>"
        "<p>필터 변경은 새 RAW, 색 재보정은 동일 RAW. "
        "중심의 가시광 Munsell fitting / 광대역 Macbeth 평가.</p>"
        "<p>표의 색차는 유효한 패치에서만 계산하며, 전체·유효 패치 수와 포화는 "
        "원본 case.json에 보존합니다.</p><table><thead><tr>"
        "<th>Case</th><th>시야</th><th>필터</th><th>입력</th><th>&gt;700nm 기여</th>"
        "<th>ΔE00 기준→재보정</th><th>동일 RAW ISP 비교</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table><h2>적용 범위</h2><ul>"
        + "".join("<li>" + e(value) + "</li>" for value in result["assumptions"])
        + "</ul>"
        + attribution
        + "</html>"
    )

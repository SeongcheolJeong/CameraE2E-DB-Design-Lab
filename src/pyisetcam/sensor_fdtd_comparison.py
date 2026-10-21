"""Fixed-control spectral/angular comparisons from validated sensor FDTD LUTs."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .assets import AssetStore
from .camera import camera_create, camera_get, camera_set
from .image_sensor_db import image_sensor_db_config
from .metrics import delta_e_ab
from .scene import macbeth_ideal_color, scene_adjust_illuminant, scene_adjust_luminance, scene_get
from .sensor_capture import resample_sensor_capture
from .sensor_comparison import (
    _color_metrics,
    _fixed_isp_matrices,
    _jsonable,
    _linear_isp,
    _make_scene,
    _patch_means,
    _physical_geometry,
    _physical_resample,
    _snr_metrics,
)
from .sensor_estimate import image_sensor_db_model
from .system_faca import camerae2e_run_scenario

_SENSORS = ("DEF-2011-804", "DEF-1807-801", "DEF-2206-805")
_LUMA = np.array([0.213, 0.715, 0.072])


def _comparison_illuminant(name, wave):
    """Bin-average official CIE spectra to avoid point-sampling FL11 peaks."""
    if name.upper() not in {"A", "F11"}:
        return name, name, {"name": name, "source": "ISETCam illuminant asset"}
    path = Path(__file__).parent / "data/sensor_estimate/cie_illuminants_visible.json"
    record = json.loads(path.read_text())["illuminants"][name.upper()]
    x = np.asarray(record["wavelength_nm"])
    y = np.asarray(record["relative_energy"])
    wave = np.asarray(wave, dtype=float)
    if wave.size < 2 or not np.allclose(np.diff(wave), np.diff(wave)[0]):
        raise ValueError("Comparison illuminants require a uniform wavelength grid.")
    step = float(wave[1] - wave[0])
    if wave[0] - step / 2 < x[0] or wave[-1] + step / 2 > x[-1]:
        raise ValueError("Comparison wavelength bins exceed the packaged CIE spectrum.")
    energy = []
    for center in wave:
        lo, hi = center - step / 2, center + step / 2
        knots = np.r_[lo, x[(x > lo) & (x < hi)], hi]
        energy.append(np.trapezoid(np.interp(knots, x, y), knots) / step)
    energy = np.asarray(energy)
    provenance = {k: v for k, v in record.items() if k not in {
        "wavelength_nm", "relative_energy",
    }}
    provenance.update({"name": name, "resampling": "linear spectrum integrated over bins",
                       "bin_width_nm": step})
    return energy, {"name": name, "wave": wave, "energy": energy}, provenance


def _converged_pupil(model, theta, phi, fnumber):
    from .fdtd_estimate import fdtd_response

    wave = model["wavelength_nm"]
    previous = None
    comparisons = []
    for radial, azimuth in ((3, 8), (6, 16), (12, 32), (24, 64)):
        illumination = sensor_pupil_directions(
            theta, phi, fnumber, radial_samples=radial, azimuth_samples=azimuth,
        )
        response = fdtd_response(model, wave, illumination)
        if previous is not None:
            difference = np.abs(response - previous)
            normalized = difference / np.maximum(np.abs(response), 0.01)
            error = float(np.max(normalized))
            comparisons.append({"radial_samples": radial, "azimuth_samples": azimuth,
                                "max_relative_error_with_0_01_floor": error})
            if error <= 0.01:
                return illumination, {"passed": True, "tolerance": 0.01,
                                      "comparisons": comparisons,
                                      "ray_count": radial * azimuth}
        previous = response
    raise ValueError(f"Pupil integration failed 1% convergence at CRA={theta}, F/{fnumber}.")


def sensor_pupil_directions(
    theta_deg: float, phi_deg: float, image_side_f_number: float,
    *, radial_samples: int = 3, azimuth_samples: int = 8,
) -> dict[str, Any]:
    """Uniform projected-irradiance pupil in direction-cosine space.

    A constant CRA describes a local field patch. Weights have unit sum and do
    not attenuate the optical image a second time for aperture or vignetting.
    """
    values = np.asarray([theta_deg, phi_deg, image_side_f_number], dtype=float)
    if not np.all(np.isfinite(values)) or not 0 <= theta_deg < 90 or image_side_f_number <= 0:
        raise ValueError("Pupil requires finite angles, theta in [0,90), and positive F/#.")
    if radial_samples < 1 or azimuth_samples < 4:
        raise ValueError("Pupil requires positive radial count and at least four azimuth samples.")
    points, weights = np.polynomial.legendre.leggauss(radial_samples)
    radius = np.sqrt((points + 1) / 2) / (2 * image_side_f_number)
    weights = weights / 2
    center = np.sin(np.deg2rad(theta_deg)) * np.array(
        [np.cos(np.deg2rad(phi_deg)), np.sin(np.deg2rad(phi_deg))],
    )
    rays = []
    for r, w in zip(radius, weights, strict=True):
        for a in np.arange(azimuth_samples) * 2 * np.pi / azimuth_samples:
            u, v = center + r * np.array([np.cos(a), np.sin(a)])
            rho = float(np.hypot(u, v))
            if rho >= 1:
                raise ValueError("Pupil contains nonpropagating directions; revise illumination.")
            rays.append({
                "theta_deg": float(np.rad2deg(np.arcsin(rho))),
                "phi_deg": float(np.rad2deg(np.arctan2(v, u)) % 360),
                "weight": float(w / azimuth_samples), "polarization": "unpolarized",
            })
    return {"directions": rays}


def camerae2e_compare_fdtd_sensors(
    lut_paths: Mapping[str, str | Path], *,
    output_dir: str | Path,
    sensor_ids: Sequence[str] = _SENSORS,
    root: str | Path | None = None,
    output_size: tuple[int, int] = (128, 128),
    nframes: int = 32,
    illuminants: Sequence[str] = ("D65", "A", "F11"),
    scene_types: Sequence[str] = ("uniform", "macbeth_d65"),
    fnumbers: Sequence[float] = (2, 4, 8),
    cra_degrees: Sequence[float] = (0, 10, 20),
    azimuth_degrees: Sequence[float] = (0, 45),
    luminance_cd_m2: float = 100,
    exposure_s: float = 0.004,
    seed: int = 0,
    asset_store: AssetStore | None = None,
) -> dict[str, Any]:
    """Write actual E2E comparisons, or an explicit pending report for missing LUTs.

    Only validated inputs reach FACA. Synthetic test fixtures are never used as
    a substitute for missing solver results. Native RAW frames stay integer.
    Each mode uses its model's fixed central D65 matrix under every illuminant.
    """
    from .fdtd_estimate import image_sensor_fdtd_model

    if nframes < 2 or int(nframes) != nframes:
        raise ValueError("Temporal comparison needs at least two frames.")
    if len(output_size) != 2 or any(int(v) != v or v < 8 for v in output_size):
        raise ValueError("output_size must contain two integers of at least eight.")
    if not np.isfinite(luminance_cd_m2) or luminance_cd_m2 <= 0:
        raise ValueError("Luminance must be positive and finite.")
    if not np.isfinite(exposure_s) or exposure_s <= 0:
        raise ValueError("Exposure must be positive and finite.")
    dimensions = (illuminants, scene_types, fnumbers, cra_degrees, azimuth_degrees)
    if not sensor_ids or not all(dimensions):
        raise ValueError("Comparison dimensions must not be empty.")
    for n in fnumbers:
        sensor_pupil_directions(0, 0, float(n))
    for theta in cra_degrees:
        for phi in azimuth_degrees:
            sensor_pupil_directions(float(theta), float(phi), float(min(fnumbers)))
    store = asset_store or AssetStore.default()
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected, pending = [], []
    pitches = []
    for code in sensor_ids:
        base = image_sensor_db_model(code, root=root)
        pitches.append(base["pixel_pitch_um"] * 1e-6)
        path = lut_paths.get(code, lut_paths.get(base["sensor_id"]))
        if path is None:
            pending.append({"sensor_id": code, "reason": "No validated FDTD LUT supplied."})
            selected.append(None)
            continue
        try:
            fdtd = image_sensor_fdtd_model(code, path, root=root)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            pending.append({"sensor_id": code, "reason": str(exc)})
            selected.append(None)
            continue
        selected.append((base, fdtd, path))
    geometry = _physical_geometry(pitches, output_size)
    focal_length = 0.004
    fov = float(np.rad2deg(2 * np.arctan(
        1.2 * geometry["common_extent_m"][1] / (2 * focal_length),
    )))
    report = {
        "schema": "camerae2e_fdtd_comparison_v1", "estimate_only": True,
        "status": "pending" if pending else "complete", "pending_sensors": pending,
        "measured_accuracy_verified": False, "cases": [], "geometry": geometry,
        "controls": {"nframes": nframes, "seed": seed, "exposure_s": exposure_s,
                     "luminance_cd_m2": luminance_cd_m2, "auto_exposure": False,
                     "white_balance": "fixed central D65", "illuminants": list(illuminants)},
        "limitations": [
            "Uniform/local illumination response; sensor spatial PSF and crosstalk unverified.",
            "Macbeth uses the calibration surfaces: color results are in-sample residuals.",
            "CRA patches are controlled sensor illumination, not a measured lens.",
            "400–700 nm model; narrowband illuminants are integrated into 10 nm bins.",
        ],
        "illuminant_provenance": {},
    }
    arrays = {}
    for index, entry in enumerate(selected):
        if entry is None:
            continue
        base, fdtd, path = entry
        code = str(sensor_ids[index])
        pupil_cache = {}
        camera = camera_create(asset_store=store)
        camera_set(camera, "optics focal length", focal_length)
        camera_set(camera, "sensor size", geometry["native_sizes"][index])
        camera_set(camera, "ip render scale", False)
        for scene_type in scene_types:
            scene, scene_geometry = _make_scene(scene_type, output_size, fov, store)
            for illuminant in illuminants:
                spectrum, ideal_illuminant, provenance = _comparison_illuminant(
                    illuminant, scene_get(scene, "wave"),
                )
                report["illuminant_provenance"][illuminant] = provenance
                stimulus = scene_adjust_illuminant(scene.clone(), spectrum, asset_store=store)
                stimulus = scene_adjust_luminance(stimulus, luminance_cd_m2, asset_store=store)
                for fnumber in fnumbers:
                    camera_set(camera, "optics fnumber", fnumber)
                    for theta in cra_degrees:
                        for phi in azimuth_degrees:
                            if theta == 0 and phi != azimuth_degrees[0]:
                                continue
                            pupil_key = (theta, phi, fnumber)
                            if pupil_key not in pupil_cache:
                                pupil_cache[pupil_key] = _converged_pupil(fdtd, *pupil_key)
                            for mode in ("db_estimate_v1", "fdtd_normal", "fdtd_pupil"):
                                if mode == "db_estimate_v1":
                                    scenario = image_sensor_db_config(
                                        code, root=root, model="db_estimate_v1"
                                    )["scenario"]
                                else:
                                    illumination = (
                                        {"directions": [{"theta_deg": 0, "phi_deg": 0,
                                                         "weight": 1,
                                                         "polarization": "unpolarized"}]}
                                        if mode == "fdtd_normal" else pupil_cache[pupil_key][0]
                                    )
                                    scenario = image_sensor_db_config(
                                        code, model="fdtd_estimate_v1", lut_path=path, root=root,
                                        sensor_illumination=illumination,
                                    )["scenario"]
                                scenario.update({"sensor_resize": False, "sensor": {
                                    "integration_time": exposure_s, "noise_flag": 2,
                                }})
                                result = camerae2e_run_scenario(
                                    scenario, scene=stimulus, camera=camera, asset_store=store,
                                    seed=seed, include_arrays=False,
                                )
                                sensor = camera_get(result["camera"], "sensor")
                                matrices = _fixed_isp_matrices(
                                    sensor, camera_get(result["camera"], "ip"), store,
                                )
                                case_id = f"case_{len(report['cases']):05d}"
                                record, stored = _capture_metrics(
                                    sensor, matrices, nframes, seed, pitches[index], geometry,
                                )
                                if scene_type == "macbeth_d65":
                                    ideal = macbeth_ideal_color(
                                        ideal_illuminant, "XYZ", asset_store=store,
                                    )
                                    record["color"] = _color_metrics(
                                        stored["xyz_mean"], scene_geometry["patch_boxes"], ideal,
                                    )
                                    record["color"]["evaluation_contract"] = (
                                        "same_Macbeth_surfaces_fixed_D65_calibration"
                                    )
                                    record["patch_xyz"] = _patch_means(
                                        stored["xyz_mean"], scene_geometry["patch_boxes"],
                                    ).tolist()
                                record.update({
                                    "case_id": case_id, "sensor_id": code, "mode": mode,
                                    "scene": scene_type, "illuminant": illuminant,
                                    "fnumber": fnumber, "cra_deg": theta, "azimuth_deg": phi,
                                    "capture": result["sensor_capture"],
                                    "model_provenance": result["sensor_estimate"],
                                    "pupil_convergence": pupil_cache[pupil_key][1]
                                    if mode == "fdtd_pupil" else None,
                                })
                                report["cases"].append(record)
                                arrays.update({f"{case_id}_{k}": v for k, v in stored.items()})
    report["case_count"] = len(report["cases"])
    report["sample_count"] = nframes * len(report["cases"])
    _center_comparisons(report["cases"])
    _write_fdtd_comparison(output, report, arrays)
    return _jsonable(report)


def _capture_metrics(sensor, matrices, nframes, seed, pitch, geometry):
    def resize(rgb):
        return _physical_resample(
            rgb, pitch, geometry["common_extent_m"], tuple(geometry["output_size"]),
        )

    reference, _ = _linear_isp(sensor, sensor.data["reference_dn"], matrices)
    reference = resize(reference)
    dark = sensor.clone()
    dark.data["mean_electrons"] = np.zeros_like(sensor.data["mean_electrons"])
    dark.fields["noise_flag"] = 0
    dark = resample_sensor_capture(dark, seed=seed)
    dark_rgb, _ = _linear_isp(dark, dark.data["reference_dn"], matrices)
    signal = (reference - resize(dark_rgb)) @ _LUMA
    mean = np.zeros_like(reference)
    xyz_mean = np.zeros_like(reference)
    m2 = np.zeros_like(reference)
    luma_mean = np.zeros_like(signal)
    luma_m2 = np.zeros_like(signal)
    raw_mean = np.zeros_like(sensor.data["raw_dn"], dtype=float)
    raw_m2 = np.zeros_like(raw_mean)
    first = None
    burst_saturation = {}
    for frame in range(nframes):
        sample = resample_sensor_capture(sensor, seed=seed + frame)
        for key, value in sample.metadata["sensor_capture"]["saturation"].items():
            burst_saturation.setdefault(key, []).append(float(value))
        rgb, xyz = _linear_isp(sample, sample.data["raw_dn"], matrices)
        rgb = resize(rgb)
        luma = rgb @ _LUMA
        count = frame + 1
        xyz_mean += (resize(xyz) - xyz_mean) / count
        d = rgb - mean
        mean += d / count
        m2 += d * (rgb - mean)
        d = luma - luma_mean
        luma_mean += d / count
        luma_m2 += d * (luma - luma_mean)
        raw = sample.data["raw_dn"]
        d = raw - raw_mean
        raw_mean += d / count
        raw_m2 += d * (raw - raw_mean)
        if first is None:
            first = raw.copy()
    means = np.mean(mean, axis=(0, 1))
    pattern = np.asarray(sensor.fields["pattern"])
    native_sites = []
    for row in range(pattern.shape[0]):
        for column in range(pattern.shape[1]):
            location = (slice(row, None, pattern.shape[0]),
                        slice(column, None, pattern.shape[1]))
            photo = sensor.data["mean_electrons"][location]
            native_sites.append({
                "pattern_row": row, "pattern_column": column,
                "filter_name": sensor.fields["filter_names"][int(pattern[row, column]) - 1],
                "mean_photoelectrons": float(np.mean(photo)),
                "mean_raw_dn": float(np.mean(raw_mean[location])),
                "mean_temporal_raw_variance_dn2": float(np.mean(
                    raw_m2[location] / (nframes - 1),
                )),
            })
    record = {
        "mean_photoelectrons": float(np.mean(sensor.data["mean_electrons"])),
        "common_output": _snr_metrics(signal, luma_m2 / (nframes - 1)),
        "rgb_means": means.tolist(),
        "r_over_g": float(means[0] / means[1]) if means[1] > 0 else None,
        "b_over_g": float(means[2] / means[1]) if means[1] > 0 else None,
        "saturation": {k: float(np.mean(v)) for k, v in burst_saturation.items()},
        "saturation_max_frame": {k: max(v) for k, v in burst_saturation.items()},
        "native_cfa_sites": native_sites,
    }
    record["snr_eligible"] = all(v <= 0.001 for v in record["saturation"].values())
    record["snr_interpretation"] = (
        "unsaturated temporal SNR" if record["snr_eligible"]
        else "clipped capture: diagnostic SNR only, excluded from comparison curves"
    )
    stored = {"raw_dn": first.astype(np.int32), "rgb_mean": mean.astype(np.float32),
              "xyz_mean": xyz_mean.astype(np.float32),
              "rgb_variance": (m2 / (nframes - 1)).astype(np.float32),
              "raw_variance": (raw_m2 / (nframes - 1)).astype(np.float32),
              "mean_electrons": sensor.data["mean_electrons"].astype(np.float32)}
    return record, stored


def _center_comparisons(cases):
    def key(c):
        return tuple(c[k] for k in ("sensor_id", "mode", "scene", "illuminant", "fnumber"))

    centers = {key(c): c for c in cases if c["cra_deg"] == 0}
    for c in cases:
        center = centers.get(key(c))
        if center is None:
            c["center_comparison"] = {"available": False, "reason": "No center case."}
            continue
        reference = center["common_output"]["mean_signal"]
        c["center_comparison"] = {
            "available": reference > 0,
            "relative_signal": c["common_output"]["mean_signal"] / reference
            if reference > 0 else None,
            "reference_case_id": center["case_id"],
        }
        if "patch_xyz" in c:
            ref = np.asarray(center["patch_xyz"])
            if ref[3, 1] > 0:
                scale = ref[3, 1]
                error = delta_e_ab(
                    np.asarray(c["patch_xyz"]) / scale, ref / scale, ref[3] / scale, "1976",
                )
                c["center_comparison"]["mean_delta_e76"] = float(np.mean(error))


def _write_fdtd_comparison(output, report, arrays):
    figures = _comparison_figures(output, report, arrays) if arrays else []
    report["artifacts"] = {"json": str(output / "comparison.json"),
                           "html": str(output / "index.html"),
                           "figures": figures,
                           "arrays": str(output / "arrays.npz") if arrays else None}
    if arrays:
        np.savez_compressed(output / "arrays.npz", **arrays)
    (output / "comparison.json").write_text(
        json.dumps(_jsonable(report), indent=2, allow_nan=False) + "\n",
    )
    rows = []
    for c in report["cases"]:
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            c["sensor_id"], c["mode"], c["illuminant"], c["scene"], c["fnumber"],
            c["cra_deg"], c["azimuth_deg"], round(c["mean_photoelectrons"], 3),
            c["common_output"].get("snr_db") if c["snr_eligible"] else "clipped",
            c["r_over_g"], c["b_over_g"],
        )) + "</tr>")
    pending = "".join(
        f"<li>{html.escape(item['sensor_id'])}: {html.escape(item['reason'])}</li>"
        for item in report["pending_sensors"]
    )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>FDTD sensor E2E</title>'
        '<style>body{font:15px system-ui;margin:32px}table{border-collapse:collapse}'
        'td,th{border:1px solid #ddd;padding:6px}li{margin:8px 0}</style>'
        f"<h1>FDTD sensor E2E — {report['status']}</h1>"
        f"<p>{report['case_count']} cases, {report['sample_count']} temporal samples. "
        "DB estimates; measured product accuracy unverified.</p>"
        f"<ul>{pending}</ul><p>Missing/unvalidated LUTs are not replaced with synthetic data.</p>"
        + "".join(f'<p><img style="max-width:100%" src="{Path(p).name}" '
                  'alt="Sensor angular response and fixed-scale RGB comparison"></p>'
                  for p in figures)
        +
        "<table><thead><tr><th>Sensor</th><th>Mode</th><th>Light</th><th>Scene</th>"
        "<th>F/#</th><th>CRA</th><th>Azimuth</th><th>e/pixel</th><th>SNR dB</th>"
        "<th>R/G</th><th>B/G</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>",
    )


def _comparison_figures(output, report, arrays):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .utils import linear_to_srgb

    figures = []
    groups = sorted({(c["sensor_id"], c["illuminant"]) for c in report["cases"]})
    for sensor_id, illuminant in groups:
        cases = [c for c in report["cases"] if (c["sensor_id"], c["illuminant"]) == (
            sensor_id, illuminant,
        )]
        preferred_scene = "uniform" if any(c["scene"] == "uniform" for c in cases) else (
            cases[0]["scene"]
        )
        preferred_fnumber = min({c["fnumber"] for c in cases}, key=lambda n: abs(n - 4))
        curve_cases = [c for c in cases if c["scene"] == preferred_scene
                       and c["fnumber"] == preferred_fnumber]
        figure, axes = plt.subplots(2, 3, figsize=(13, 7))
        for mode in ("db_estimate_v1", "fdtd_normal", "fdtd_pupil"):
            selected = [c for c in curve_cases if c["mode"] == mode]
            for azimuth in sorted({c["azimuth_deg"] for c in selected}):
                curve = sorted([c for c in selected if c["azimuth_deg"] == azimuth
                                or c["cra_deg"] == 0], key=lambda c: c["cra_deg"])
                angles = [c["cra_deg"] for c in curve]
                label = f"{mode}, φ={azimuth}°"
                axes[0, 0].plot(angles, [c["center_comparison"].get("relative_signal")
                                      for c in curve], ".-", label=label)
                axes[0, 1].plot(angles, [c["r_over_g"] for c in curve], ".-", label=label + " R/G")
                axes[0, 1].plot(angles, [c["b_over_g"] for c in curve], ".--", label=label + " B/G")
                axes[0, 2].plot(angles, [c["common_output"].get("snr_db")
                                       if c["snr_eligible"] else np.nan for c in curve],
                                ".-", label=label)
        for axis, title in zip(axes[0], ("Signal / center", "RGB ratios", "Temporal SNR (dB)"),
                               strict=True):
            axis.set(title=title, xlabel="CRA (degrees)")
            axis.grid(alpha=0.2)
            axis.legend(fontsize=6)
        image_cases = [c for c in cases if c["scene"] == (
            "macbeth_d65" if any(r["scene"] == "macbeth_d65" for r in cases) else preferred_scene
        ) and c["fnumber"] == preferred_fnumber]
        scale = max(float(np.percentile(arrays[c["case_id"] + "_rgb_mean"], 99))
                    for c in image_cases)
        scale = 0.9 / scale if scale > 0 else 1
        for axis, mode in zip(
            axes[1], ("db_estimate_v1", "fdtd_normal", "fdtd_pupil"), strict=True,
        ):
            case = max((c for c in image_cases if c["mode"] == mode),
                       key=lambda c: (c["cra_deg"], c["azimuth_deg"]))
            rgb = arrays[case["case_id"] + "_rgb_mean"]
            axis.imshow(np.clip(linear_to_srgb(np.maximum(rgb * scale, 0)), 0, 1))
            axis.set_title(f"{mode}, CRA {case['cra_deg']}°, φ {case['azimuth_deg']}°", fontsize=9)
            axis.axis("off")
        figure.suptitle(f"{sensor_id} / {illuminant} / F{preferred_fnumber} — estimated; "
                        "shared display scale, fixed D65 calibration")
        figure.tight_layout()
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_"
                            for c in f"{sensor_id}_{illuminant}")
        path = output / f"{safe_name}.png"
        figure.savefig(path, dpi=120)
        plt.close(figure)
        figures.append(str(path))
    return figures

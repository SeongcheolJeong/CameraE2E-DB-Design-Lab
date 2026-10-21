"""Practical camera comparisons using explicit, unmeasured optical estimates.

The common-readout cases isolate optical assumptions. Native-profile cases
reuse exactly the same photoelectron image and change only the readout model.
No FDTD result, synthetic fixture, or measured sensor response is required.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .assets import AssetStore
from .camera import camera_create, camera_get, camera_set
from .image_sensor_db import image_sensor_db_config
from .scene import macbeth_ideal_color, scene_adjust_illuminant, scene_adjust_luminance, scene_get
from .sensor_capture import configure_e2e_sensor, resample_sensor_capture
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
from .sensor_fdtd_comparison import (
    _capture_metrics,
    _center_comparisons,
    _comparison_illuminant,
    sensor_pupil_directions,
)
from .system_faca import camerae2e_run_scenario

_SENSORS = ("DEF-2011-804", "DEF-1807-801", "DEF-2206-805")
_COMMON_READOUT = {
    "gain": 1.0,
    "full_well_e": 10000.0,
    "read_noise_e": 10.0,
    "dark_current_e_per_s": 10.0,
    "bit_depth": 12,
    "black_level_dn": 64.0,
}
_MODES = (
    "db_reference_common",
    "physical_normal_common",
    "physical_pupil_common",
    "physical_pupil_native",
)
_NORMAL = {"directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1}]}


def _common_capture(sensor, seed):
    """Use identical electronics and zero unspecified FPN in optical controls."""
    sensor = configure_e2e_sensor(sensor, readout=_COMMON_READOUT)
    sensor.fields["pixel"]["prnu_sigma"] = 0.0
    sensor.fields["pixel"]["dsnu_sigma_v"] = 0.0
    sensor.fields["column_fpn"] = [0.0, 0.0]
    for key in ("gain_fpn_image", "offset_fpn_image", "column_gain_fpn", "column_offset_fpn"):
        sensor.fields.pop(key, None)
    return resample_sensor_capture(sensor, seed=seed)


def _practical_capture_metrics(sensor, matrices, nframes, seed, pitch, geometry):
    record, stored = _capture_metrics(sensor, matrices, nframes, seed, pitch, geometry)
    # Keep the same fixed offsets/gains in the signal and dark expectations.
    # The reusable FDTD helper assumes a zero-pattern dark reference.
    dark = sensor.clone()
    dark.data["mean_electrons"] = np.zeros_like(sensor.data["mean_electrons"])
    dark.fields["noise_flag"] = 2
    dark = resample_sensor_capture(dark, seed=seed)
    reference_rgb, _ = _linear_isp(sensor, sensor.data["reference_dn"], matrices)
    dark_rgb, _ = _linear_isp(dark, dark.data["reference_dn"], matrices)
    signal = _physical_resample(
        reference_rgb - dark_rgb,
        pitch,
        geometry["common_extent_m"],
        tuple(geometry["output_size"]),
    ) @ np.array([0.213, 0.715, 0.072])
    record["common_output"] = _snr_metrics(
        signal,
        np.asarray(record["common_output"]["temporal_noise_rms"] ** 2),
    )
    return record, stored


def _converged_physical_pupil(model, theta, phi, fnumber):
    from .physical_estimate import physical_response

    previous = None
    comparisons = []
    for radial, azimuth in ((3, 8), (6, 16), (12, 32), (24, 64)):
        rays = sensor_pupil_directions(
            theta,
            phi,
            fnumber,
            radial_samples=radial,
            azimuth_samples=azimuth,
        )
        response = physical_response(model, model["wavelength_nm"], rays)
        if previous is not None:
            error = float(np.max(np.abs(response - previous) / np.maximum(response, 0.01)))
            comparisons.append({"ray_count": radial * azimuth, "max_error": error})
            if error <= 0.01:
                return rays, {
                    "passed": True,
                    "tolerance": 0.01,
                    "normalization_floor": 0.01,
                    "comparisons": comparisons,
                    "ray_count": radial * azimuth,
                }
        previous = response
    raise ValueError(f"Pupil integration failed 1% convergence: {theta=}, {phi=}, {fnumber=}.")


def _native_variance_check(sensor, record, stored, nframes):
    """Compare independent shot + dark + read + ADC variance with actual RAW."""
    readout = sensor.fields["sensor_readout"]
    photo = np.asarray(sensor.data["mean_electrons"])
    dark = readout["dark_current_e_per_s"] * float(sensor.fields["integration_time"])
    dn_per_e = sensor.metadata["sensor_capture"]["dn_per_electron"]
    quantization_variance = 1 / (12 * dn_per_e**2)
    pixel_gain = np.asarray(sensor.fields.get("gain_fpn_image", 1.0))
    column_gain = np.asarray(sensor.fields.get("column_gain_fpn", 1.0))
    predicted = float(np.mean((photo + dark) * pixel_gain**2 * column_gain**2))
    predicted += readout["read_noise_e"] ** 2 + quantization_variance
    observed = float(np.mean(stored["raw_variance"])) / dn_per_e**2
    mean_photo = float(np.mean(photo))
    error = abs(observed - predicted) / predicted if predicted > 0 else 0
    # A burst estimates variance over every native pixel, with finite sampling.
    tolerance = max(0.10, 6 * np.sqrt(2 / (photo.size * (nframes - 1))))
    eligible = record["snr_eligible"]
    return {
        "eligible": eligible,
        "passed": bool(error <= tolerance) if eligible else None,
        "observed_variance_e2": observed,
        "predicted_variance_e2": predicted,
        "native_raw_snr_db": float(20 * np.log10(mean_photo / np.sqrt(observed)))
        if mean_photo > 0 and observed > 0
        else None,
        "predicted_native_raw_snr_db": float(20 * np.log10(mean_photo / np.sqrt(predicted)))
        if mean_photo > 0 and predicted > 0
        else None,
        "relative_error": error,
        "relative_tolerance": tolerance,
        "mean_dark_electrons": dark,
        "adc_quantization_variance_e2": quantization_variance,
        "formula": (
            "mean((photo_e + dark_e)*pixel_gain^2*column_gain^2) "
            "+ read_noise_e^2 + 1/(12*dn_per_e^2)"
        ),
        "scope": "unsaturated temporal RAW variance; fixed offsets affect the mean only",
    }


def _sensitivity_cases(sensor, matrices, profile, pitch, geometry, nframes, seed, anchor):
    """One-factor bounds, preserving the nominal color calibration and photons."""
    from .sensor_readout_profiles import apply_readout_profile

    variations = [("collection_efficiency", x, {"charge_scale": x / 0.75}) for x in (0.5, 0.75, 1)]
    fields = profile["provenance"]["fields"]
    for key in ("read_noise_e", "full_well_e"):
        for value in fields.get(key, {}).get("range", []):
            variations.append((key, value, {"readout": {key: value}}))
    result = []
    for parameter, value, changes in variations:
        resolved = {
            **profile,
            "readout": {
                **sensor.fields["sensor_readout"],
                **changes.get("readout", {}),
            },
        }
        sample = apply_readout_profile(sensor, resolved)
        if "charge_scale" in changes:
            sample.data["mean_electrons"] *= changes["charge_scale"]
        sample = resample_sensor_capture(sample, seed=seed)
        record, stored = _practical_capture_metrics(
            sample, matrices, nframes, seed, pitch, geometry
        )
        variance = _native_variance_check(sample, record, stored, nframes)
        cg = sample.fields["pixel"]["conversion_gain_v_per_electron"]
        result.append(
            {
                **anchor,
                "parameter": parameter,
                "value": float(value),
                "mean_photoelectrons": record["mean_photoelectrons"],
                "snr_db": record["common_output"]["snr_db"],
                "native_raw_snr_db": variance["native_raw_snr_db"],
                "snr_eligible": record["snr_eligible"],
                "saturation": record["saturation"],
                "readout": sample.fields["sensor_readout"],
                "effective_dsnu_e": sample.fields["pixel"]["dsnu_sigma_v"] / cg,
                "realized_dsnu_map_rms_e": float(
                    np.sqrt(
                        np.mean(
                            np.asarray(sample.fields.get("offset_fpn_image", 0)) ** 2,
                        )
                    )
                )
                / cg,
                "readout_provenance": sample.metadata["sensor_readout_profile"]["provenance"],
                "classification": "assumption_range"
                if parameter == "collection_efficiency"
                else fields.get(parameter, {}).get("classification", "assumption_range"),
            }
        )
    return result


def _stress_bursts(sensor, matrices, pitch, geometry, nframes, seed, code, start_index):
    nominal_exposure = float(sensor.fields["integration_time"])
    peak = float(np.max(sensor.data["mean_electrons"]))
    well = sensor.fields["sensor_readout"]["full_well_e"]
    cases, arrays = [], {}
    for kind, scale in (("dark", 0), ("bright", 1.2 * well / max(peak, 1e-30))):
        sample = sensor.clone()
        sample.fields["integration_time"] = nominal_exposure * scale
        sample.data["mean_electrons"] *= scale
        sample = resample_sensor_capture(sample, seed=seed)
        record, stored = _practical_capture_metrics(
            sample,
            matrices,
            nframes,
            seed,
            pitch,
            geometry,
        )
        case_id = f"stress_{start_index + len(cases):05d}"
        record.update(
            {
                "case_id": case_id,
                "sensor_id": code,
                "kind": kind,
                "nframes": nframes,
                "exposure_s": sample.fields["integration_time"],
                "readout": sample.fields["sensor_readout"],
                "capture": sample.metadata["sensor_capture"],
                "method": "cached charge scaled after independent exposure-linearity check",
                "native_noise_check": _native_variance_check(sample, record, stored, nframes),
            }
        )
        cases.append(record)
        arrays.update({f"{case_id}_{key}": value for key, value in stored.items()})
    return cases, arrays


def camerae2e_compare_physical_sensors(
    *,
    output_dir: str | Path,
    sensor_ids: Sequence[str] = _SENSORS,
    root: str | Path | None = None,
    output_size: tuple[int, int] = (32, 32),
    nframes: int = 8,
    illuminants: Sequence[str] = ("D65", "A", "F11"),
    scene_types: Sequence[str] = ("uniform", "macbeth_d65"),
    fnumbers: Sequence[float] = (2, 4, 8),
    cra_degrees: Sequence[float] = (0, 20),
    azimuth_degrees: Sequence[float] = (0, 45),
    luminance_cd_m2: float = 100,
    exposure_s: float = 0.004,
    seed: int = 0,
    sensitivity: bool = True,
    readout_profiles: Mapping[str, str] | None = None,
    operating_points: Mapping[str, str] | None = None,
    optical_assumptions: Mapping[str, Any] | None = None,
    asset_store: AssetStore | None = None,
) -> dict[str, Any]:
    """Write actual fixed-control estimates and explicit model sanity checks.

    Native profiles are public-camera operating points or declared assumptions,
    never a claim that a particular bare sensor has been measured. Collection
    efficiency is held at .75 in the nominal grid and swept independently.
    """
    from .physical_estimate import (
        image_sensor_physical_model,
        physical_optical_response,
        physical_response,
    )
    from .sensor_readout_profiles import apply_readout_profile, resolve_sensor_readout

    if nframes < 2 or int(nframes) != nframes:
        raise ValueError("Temporal comparison requires at least two integer frames.")
    if len(output_size) != 2 or any(int(x) != x or x < 8 for x in output_size):
        raise ValueError("output_size requires two integers of at least eight.")
    if not all((sensor_ids, illuminants, scene_types, fnumbers, cra_degrees, azimuth_degrees)):
        raise ValueError("Comparison dimensions must not be empty.")
    if any(not np.isfinite(x) or x <= 0 for x in (luminance_cd_m2, exposure_s)):
        raise ValueError("Luminance and exposure must be finite and positive.")
    for theta in cra_degrees:
        for phi in azimuth_degrees:
            for fnumber in fnumbers:
                sensor_pupil_directions(float(theta), float(phi), float(fnumber))
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    store = asset_store or AssetStore.default()
    bases = [image_sensor_db_model(code, root=root) for code in sensor_ids]
    pitches = [base["pixel_pitch_um"] * 1e-6 for base in bases]
    geometry = _physical_geometry(pitches, output_size)
    focal_length = 0.004
    fov = float(
        np.rad2deg(
            2
            * np.arctan(
                1.2 * geometry["common_extent_m"][1] / (2 * focal_length),
            )
        )
    )
    report = {
        "schema": "camerae2e_physical_comparison_v1",
        "status": "complete",
        "estimate_only": True,
        "measured_accuracy_verified": False,
        "cases": [],
        "sensitivity": [],
        "routing_sensitivity": [],
        "stress_cases": [],
        "reasonableness": {"checks": []},
        "geometry": geometry,
        "illuminant_provenance": {},
        "readout_profiles": {},
        "controls": {
            "nframes": nframes,
            "seed": seed,
            "exposure_s": exposure_s,
            "luminance_cd_m2": luminance_cd_m2,
            "auto_exposure": False,
            "white_balance": "fixed central D65",
            "demosaic": "matched Bayer bilinear; merged ISP greens, four native RAW sites retained",
            "common_readout": _COMMON_READOUT,
            "common_fixed_pattern": "zero PRNU, DSNU and column pattern",
            "collection_efficiency": 0.75,
            "optical_assumptions": optical_assumptions or {},
            "illuminants": list(illuminants),
        },
        "comparison_contract": {
            "db_reference_common": "historical aggregate eta=.75; different loss semantics",
            "physical_normal_common": "post-absorption collection eta=.75; shared readout",
            "physical_pupil_common": "same optical model plus pupil; shared readout",
            "physical_pupil_native": "same pupil charge; selected operating-point readout",
        },
        "limitations": [
            "Material, microlens geometry and collection parameters remain unmeasured proxies.",
            "No ranking: assumption ranges are not statistical confidence intervals.",
            "Readout profiles are paired camera operating points, not bare-sensor measurements.",
            "Macbeth errors use the calibration surfaces and are in-sample residuals.",
            "Local CRA patches; no measured lens, sensor spatial PSF, spatial crosstalk or MTF.",
            "Nominal routing preserves photons; own-site routing is a pessimistic sensitivity.",
            "400–700 nm only; A/F11 spectra integrated into 10 nm wavelength bins.",
            "Displayed RGB uses a shared scale within each figure; no adaptive white balance.",
            "Native RAW SNR and matched Bayer output SNR are distinct metrics, both retained.",
        ],
    }
    arrays = {}
    report["implementation_sha256"] = {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in (
            "physical_estimate.py",
            "sensor_capture.py",
            "ip.py",
            "sensor_physical_comparison.py",
            "sensor_readout_profiles.py",
        )
    }
    optical_solves = 0
    for index, code in enumerate(sensor_ids):
        model = image_sensor_physical_model(
            code, root=root, optical_assumptions=optical_assumptions
        )
        profile = resolve_sensor_readout(
            code,
            profile=(readout_profiles or {}).get(code),
            operating_point=(operating_points or {}).get(code),
            root=root,
        )
        report["readout_profiles"][code] = profile
        camera = camera_create(asset_store=store)
        camera_set(camera, "optics focal length", focal_length)
        camera_set(camera, "sensor size", geometry["native_sizes"][index])
        camera_set(camera, "ip render scale", False)
        pupil_cache = {}
        normal_qe = physical_response(model, model["wavelength_nm"], _NORMAL)
        report["reasonableness"]["checks"].append(
            {
                "sensor_id": code,
                "check": "normal_external_quantum_efficiency_bounds",
                "minimum": float(np.min(normal_qe)),
                "maximum": float(np.max(normal_qe)),
                "channel_maximum": np.max(normal_qe, axis=0).tolist(),
                "passed": bool(
                    np.all(np.isfinite(normal_qe))
                    and np.all(normal_qe >= 0)
                    and np.all(normal_qe <= 1)
                ),
                "meaning": "physical bounds only; does not establish measured product accuracy",
            }
        )
        budgets = [
            physical_optical_response(
                model,
                model["wavelength_nm"],
                theta_deg=theta,
                phi_deg=phi,
            )
            for theta in cra_degrees
            for phi in azimuth_degrees
        ]
        energy_error = max(float(np.max(np.abs(b["energy_residual"]))) for b in budgets)
        report["reasonableness"]["checks"].append(
            {
                "sensor_id": code,
                "check": "incoherent_stack_energy_balance",
                "max_absolute_residual": energy_error,
                "passed": energy_error < 1e-10,
                "definition": "R + T + absorption = 1; routing and collection are separate",
            }
        )
        linearity_done = False
        for scene_type in scene_types:
            scene, scene_geometry = _make_scene(scene_type, output_size, fov, store)
            for illuminant in illuminants:
                spectrum, ideal_illuminant, provenance = _comparison_illuminant(
                    illuminant,
                    scene_get(scene, "wave"),
                )
                report["illuminant_provenance"][illuminant] = provenance
                stimulus = scene_adjust_illuminant(scene.clone(), spectrum, asset_store=store)
                stimulus = scene_adjust_luminance(stimulus, luminance_cd_m2, asset_store=store)
                for fnumber in fnumbers:
                    camera_set(camera, "optics fnumber", fnumber)
                    constant_cache = {}
                    for theta in cra_degrees:
                        for phi in azimuth_degrees:
                            if theta == 0 and phi != azimuth_degrees[0]:
                                continue
                            pupil_key = (theta, phi, fnumber)
                            if pupil_key not in pupil_cache:
                                pupil_cache[pupil_key] = _converged_physical_pupil(
                                    model, *pupil_key
                                )
                            pupil_sensor = None
                            for mode in _MODES:
                                is_native = mode == "physical_pupil_native"
                                if is_native:
                                    sensor = resample_sensor_capture(
                                        apply_readout_profile(pupil_sensor, profile),
                                        seed=seed,
                                    )
                                elif mode in constant_cache:
                                    sensor, matrices, model_provenance = constant_cache[mode]
                                    sensor = sensor.clone()
                                else:
                                    options = {"assumptions": _COMMON_READOUT}
                                    if mode == "db_reference_common":
                                        options["model"] = "db_estimate_v1"
                                    else:
                                        options.update(
                                            {
                                                "model": "physical_estimate_v1",
                                                "sensor_illumination": _NORMAL
                                                if "normal" in mode
                                                else pupil_cache[pupil_key][0],
                                                "optical_assumptions": optical_assumptions,
                                            }
                                        )
                                    scenario = image_sensor_db_config(code, root=root, **options)[
                                        "scenario"
                                    ]
                                    scenario.update(
                                        {
                                            "sensor_resize": False,
                                            "sensor": {
                                                "integration_time": exposure_s,
                                                "noise_flag": 2,
                                            },
                                        }
                                    )
                                    run = camerae2e_run_scenario(
                                        scenario,
                                        scene=stimulus,
                                        camera=camera,
                                        asset_store=store,
                                        seed=seed,
                                        include_arrays=False,
                                    )
                                    optical_solves += 1
                                    sensor = camera_get(run["camera"], "sensor")
                                    matrices = _fixed_isp_matrices(
                                        sensor,
                                        camera_get(run["camera"], "ip"),
                                        store,
                                    )
                                    model_provenance = run["sensor_estimate"]
                                    if mode in _MODES[:2]:
                                        constant_cache[mode] = (
                                            sensor.clone(),
                                            matrices,
                                            model_provenance,
                                        )
                                    if not linearity_done and mode == "physical_normal_common":
                                        doubled = {
                                            **scenario,
                                            "sensor": {
                                                **scenario["sensor"],
                                                "integration_time": 2 * exposure_s,
                                            },
                                        }
                                        twice = camerae2e_run_scenario(
                                            doubled,
                                            scene=stimulus,
                                            camera=camera,
                                            asset_store=store,
                                            seed=seed,
                                            include_arrays=False,
                                        )
                                        optical_solves += 1
                                        measured = camera_get(twice["camera"], "sensor").data[
                                            "mean_electrons"
                                        ]
                                        expected = 2 * sensor.data["mean_electrons"]
                                        relative = float(
                                            np.max(
                                                np.abs(measured - expected)
                                                / np.maximum(expected, 1e-12)
                                            )
                                        )
                                        report["reasonableness"]["checks"].append(
                                            {
                                                "sensor_id": code,
                                                "check": "independent_double_exposure",
                                                "max_relative_error": relative,
                                                "passed": relative < 1e-8,
                                                "exposure_pair_s": [exposure_s, 2 * exposure_s],
                                            }
                                        )
                                        linearity_done = True
                                if not is_native:
                                    sensor = _common_capture(sensor, seed)
                                if mode == "physical_pupil_common":
                                    pupil_sensor = sensor.clone()
                                record, stored = _practical_capture_metrics(
                                    sensor,
                                    matrices,
                                    nframes,
                                    seed,
                                    pitches[index],
                                    geometry,
                                )
                                case_id = f"case_{len(report['cases']):05d}"
                                record.update(
                                    {
                                        "case_id": case_id,
                                        "sensor_id": code,
                                        "mode": mode,
                                        "scene": scene_type,
                                        "illuminant": illuminant,
                                        "fnumber": fnumber,
                                        "cra_deg": theta,
                                        "azimuth_deg": phi,
                                        "capture": sensor.metadata["sensor_capture"],
                                        "readout": sensor.fields["sensor_readout"],
                                        "readout_classification": profile["provenance"][
                                            "classification"
                                        ]
                                        if is_native
                                        else "shared_assumption_for_optical_comparison",
                                        "model_provenance": model_provenance,
                                        "pupil_convergence": pupil_cache[pupil_key][1]
                                        if "pupil" in mode
                                        else None,
                                    }
                                )
                                record["native_noise_check"] = _native_variance_check(
                                    sensor,
                                    record,
                                    stored,
                                    nframes,
                                )
                                if scene_type == "macbeth_d65":
                                    ideal = macbeth_ideal_color(
                                        ideal_illuminant, "XYZ", asset_store=store
                                    )
                                    record["color"] = _color_metrics(
                                        stored["xyz_mean"],
                                        scene_geometry["patch_boxes"],
                                        ideal,
                                    )
                                    record["color"]["evaluation_contract"] = (
                                        "same_surfaces_fixed_D65"
                                    )
                                    record["patch_xyz"] = _patch_means(
                                        stored["xyz_mean"],
                                        scene_geometry["patch_boxes"],
                                    ).tolist()
                                report["cases"].append(record)
                                arrays.update(
                                    {f"{case_id}_{key}": value for key, value in stored.items()}
                                )
                                if (
                                    sensitivity
                                    and is_native
                                    and scene_type == "uniform"
                                    and illuminant == "D65"
                                    and fnumber == 4
                                    and phi == azimuth_degrees[0]
                                ):
                                    report["sensitivity"].extend(
                                        _sensitivity_cases(
                                            sensor,
                                            matrices,
                                            profile,
                                            pitches[index],
                                            geometry,
                                            max(16, nframes),
                                            seed,
                                            {
                                                "sensor_id": code,
                                                "reference_case_id": case_id,
                                                "cra_deg": theta,
                                                "fnumber": fnumber,
                                                "nframes": max(16, nframes),
                                            },
                                        )
                                    )
                                    lower_options = {
                                        **dict(optical_assumptions or {}),
                                        "routing_mode": "own_site_lower_bound",
                                    }
                                    lower_scenario = image_sensor_db_config(
                                        code,
                                        model="physical_estimate_v1",
                                        root=root,
                                        sensor_illumination=pupil_cache[pupil_key][0],
                                        optical_assumptions=lower_options,
                                        readout_profile=profile["profile"],
                                        operating_point=profile["operating_point"],
                                    )["scenario"]
                                    lower_scenario.update(
                                        {
                                            "sensor_resize": False,
                                            "sensor": {
                                                "integration_time": exposure_s,
                                                "noise_flag": 2,
                                            },
                                        }
                                    )
                                    lower_run = camerae2e_run_scenario(
                                        lower_scenario,
                                        scene=stimulus,
                                        camera=camera,
                                        asset_store=store,
                                        seed=seed,
                                        include_arrays=False,
                                    )
                                    optical_solves += 1
                                    lower_sensor = camera_get(lower_run["camera"], "sensor")
                                    lower_record, lower_arrays = _practical_capture_metrics(
                                        lower_sensor,
                                        matrices,
                                        max(16, nframes),
                                        seed,
                                        pitches[index],
                                        geometry,
                                    )
                                    lower_id = f"routing_{len(report['routing_sensitivity']):05d}"
                                    lower_record.update(
                                        {
                                            "case_id": lower_id,
                                            "reference_case_id": case_id,
                                            "sensor_id": code,
                                            "cra_deg": theta,
                                            "azimuth_deg": phi,
                                            "fnumber": fnumber,
                                            "nframes": max(16, nframes),
                                            "optical_assumptions": lower_options,
                                            "classification": "pessimistic_own_site_routing_proxy",
                                            "calibration": "nominal model central D65",
                                            "model_provenance": lower_run["sensor_estimate"],
                                            "native_noise_check": _native_variance_check(
                                                lower_sensor,
                                                lower_record,
                                                lower_arrays,
                                                max(16, nframes),
                                            ),
                                            "relative_photocharge": lower_record[
                                                "mean_photoelectrons"
                                            ]
                                            / max(record["mean_photoelectrons"], 1e-30),
                                        }
                                    )
                                    report["routing_sensitivity"].append(lower_record)
                                    arrays.update(
                                        {
                                            f"{lower_id}_{key}": value
                                            for key, value in lower_arrays.items()
                                        }
                                    )
                                if (
                                    is_native
                                    and scene_type == "uniform"
                                    and illuminant == "D65"
                                    and fnumber == 4
                                    and theta == 0
                                ):
                                    stress_cases, stress_arrays = _stress_bursts(
                                        sensor,
                                        matrices,
                                        pitches[index],
                                        geometry,
                                        max(16, nframes),
                                        seed,
                                        code,
                                        len(report["stress_cases"]),
                                    )
                                    report["stress_cases"].extend(stress_cases)
                                    arrays.update(stress_arrays)
                                if is_native and not any(
                                    c["check"] == "noiseless_dark_charge" and c["sensor_id"] == code
                                    for c in report["reasonableness"]["checks"]
                                ):
                                    dark = sensor.clone()
                                    dark.data["mean_electrons"] = np.zeros_like(
                                        sensor.data["mean_electrons"]
                                    )
                                    dark.fields["noise_flag"] = 0
                                    dark = resample_sensor_capture(dark, seed=seed)
                                    metadata = dark.metadata["sensor_capture"]
                                    inferred = (
                                        dark.data["reference_dn"] - metadata["black_level_dn"]
                                    )
                                    inferred /= metadata["dn_per_electron"]
                                    expected_dark = (
                                        sensor.fields["sensor_readout"]["dark_current_e_per_s"]
                                        * exposure_s
                                    )
                                    expected_dark = min(
                                        expected_dark,
                                        sensor.fields["sensor_readout"]["full_well_e"],
                                    )
                                    dark_error = float(np.max(np.abs(inferred - expected_dark)))
                                    report["reasonableness"]["checks"].append(
                                        {
                                            "sensor_id": code,
                                            "check": "noiseless_dark_charge",
                                            "expected_electrons": expected_dark,
                                            "max_absolute_error_electrons": dark_error,
                                            "passed": dark_error < 1e-8,
                                            "scope": "zero photocharge; noise_flag=0 disables FPN",
                                        }
                                    )
        # Optical response varies with field, but scene/exposure never alters QE.
        all_qe = [
            physical_response(model, model["wavelength_nm"], rays)
            for rays, _ in pupil_cache.values()
        ]
        report["reasonableness"]["checks"].append(
            {
                "sensor_id": code,
                "check": "all_pupil_external_quantum_efficiency_bounds",
                "minimum": float(np.min(all_qe)),
                "maximum": float(np.max(all_qe)),
                "passed": bool(
                    np.all(np.isfinite(all_qe)) and np.min(all_qe) >= 0 and np.max(all_qe) <= 1
                ),
            }
        )
    _center_comparisons(report["cases"])
    report["case_count"] = len(report["cases"])
    report["sample_count"] = nframes * report["case_count"]
    report["sensitivity_sample_count"] = sum(c["nframes"] for c in report["sensitivity"])
    report["stress_sample_count"] = sum(c["nframes"] for c in report["stress_cases"])
    report["routing_sample_count"] = sum(c["nframes"] for c in report["routing_sensitivity"])
    report["optical_solve_count"] = optical_solves
    eligible = [
        c["native_noise_check"] for c in report["cases"] if c["native_noise_check"]["eligible"]
    ]
    report["reasonableness"].update(
        {
            "numerical_checks_passed": all(c["passed"] for c in report["reasonableness"]["checks"]),
            "temporal_noise_cases_checked": len(eligible),
            "temporal_noise_cases_passed": sum(c["passed"] for c in eligible),
            "maximum_temporal_variance_relative_error": max(
                (c["relative_error"] for c in eligible), default=None
            ),
            "measured_accuracy_verified": False,
        }
    )
    report["reasonableness"]["status"] = (
        "passed"
        if report["reasonableness"]["numerical_checks_passed"]
        and all(c["passed"] for c in eligible)
        else "failed"
    )
    _write_physical_comparison(output, report, arrays)
    return _jsonable(report)


def _write_physical_comparison(output, report, arrays):
    _separate_effective_readout_provenance(report)
    figures = _physical_figures(output, report, arrays)
    report["artifacts"] = {
        "json": str(output / "comparison.json"),
        "html": str(output / "index.html"),
        "arrays": str(output / "arrays.npz"),
        "figures": figures,
    }
    np.savez_compressed(output / "arrays.npz", **arrays)
    (output / "comparison.json").write_text(
        json.dumps(_jsonable(report), indent=2, allow_nan=False) + "\n"
    )
    rows = []
    for case in report["cases"]:
        values = (
            case["sensor_id"],
            case["mode"],
            case["illuminant"],
            case["scene"],
            case["fnumber"],
            case["cra_deg"],
            case["azimuth_deg"],
            round(case["mean_photoelectrons"], 2),
            round(case["native_noise_check"]["native_raw_snr_db"], 2)
            if case["snr_eligible"]
            else "clipped",
            round(case["common_output"]["snr_db"], 2) if case["snr_eligible"] else "clipped",
            case["readout"]["full_well_e"],
            case["readout"]["read_noise_e"],
        )
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in values) + "</tr>")
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Practical sensor E2E</title>'
        "<style>body{font:15px system-ui;margin:32px;max-width:1400px}"
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px}"
        "img{max-width:100%}li{margin:8px 0}</style><h1>Practical sensor E2E estimates</h1>"
        f"<p>{report['case_count']} cases, {report['sample_count']} temporal RAW frames. "
        "Physical sanity checks are separate from unverified product accuracy.</p>"
        + "<ul>"
        + "".join(f"<li>{html.escape(x)}</li>" for x in report["limitations"])
        + "</ul>"
        + "<p>Common readout isolates optical response. Native profile changes only readout. "
        "The DB reference uses an aggregate efficiency with different loss semantics. "
        "Sensitivity intervals describe assumptions; no sensor ranking is assigned.</p>"
        + "".join(
            f'<p><img src="{Path(p).name}" alt="Estimated response and uncertainty"></p>'
            for p in figures
        )
        + "<table><thead><tr><th>Sensor</th><th>Mode</th><th>Light</th><th>Scene</th>"
        "<th>F/#</th><th>CRA</th><th>Azimuth</th><th>Photo e/pixel</th><th>RAW SNR dB</th>"
        "<th>Bayer output SNR dB</th>"
        "<th>FW e</th><th>Read noise e</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>",
    )


def _separate_effective_readout_provenance(report):
    """Cached optical results must not carry stale electronics into native cases."""
    for case in report["cases"]:
        noise = case["native_noise_check"]
        signal = case["mean_photoelectrons"]
        for key, variance in (
            ("native_raw_snr_db", noise["observed_variance_e2"]),
            ("predicted_native_raw_snr_db", noise["predicted_variance_e2"]),
        ):
            noise[key] = (
                float(20 * np.log10(signal / np.sqrt(variance)))
                if signal > 0 and variance > 0
                else None
            )
        optical = dict(case["model_provenance"])
        optical.pop("readout", None)
        optical.pop("readout_profile", None)
        case["model_provenance"] = optical
        if case["mode"] == "physical_pupil_native":
            profile = report["readout_profiles"][case["sensor_id"]]
            case["readout_profile_reference"] = {
                "sensor_id": case["sensor_id"],
                "profile": profile["profile"],
                "operating_point": profile["operating_point"],
                "fixed_pattern": profile["fixed_pattern"],
            }
        else:
            case["readout_profile_reference"] = {
                "classification": "shared_assumption_for_optical_comparison",
                "fixed_pattern": dict.fromkeys(
                    ("prnu_fraction", "dsnu_e", "column_prnu_fraction", "column_dsnu_e"), 0.0
                ),
            }


def _physical_figures(output, report, arrays):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .utils import linear_to_srgb

    figures = []
    for sensor_id, light in sorted({(c["sensor_id"], c["illuminant"]) for c in report["cases"]}):
        cases = [
            c for c in report["cases"] if (c["sensor_id"], c["illuminant"]) == (sensor_id, light)
        ]
        preferred_scene = (
            "uniform" if any(c["scene"] == "uniform" for c in cases) else cases[0]["scene"]
        )
        fnumber = min({c["fnumber"] for c in cases}, key=lambda x: abs(x - 4))
        curves = [c for c in cases if c["scene"] == preferred_scene and c["fnumber"] == fnumber]
        figure, axes = plt.subplots(2, 4, figsize=(16, 7))
        for mode in _MODES:
            selected = [c for c in curves if c["mode"] == mode]
            for azimuth in sorted({c["azimuth_deg"] for c in selected}):
                curve = sorted(
                    [c for c in selected if c["azimuth_deg"] == azimuth or c["cra_deg"] == 0],
                    key=lambda c: c["cra_deg"],
                )
                angles = [c["cra_deg"] for c in curve]
                label = mode.replace("physical_", "") + f", φ={azimuth}°"
                axes[0, 0].plot(
                    angles, [c["mean_photoelectrons"] for c in curve], ".-", label=label
                )
                axes[0, 1].plot(
                    angles,
                    [c["center_comparison"].get("relative_signal") for c in curve],
                    ".-",
                    label=label,
                )
                axes[0, 2].plot(angles, [c["r_over_g"] for c in curve], ".-", label=label + " R/G")
                axes[0, 2].plot(angles, [c["b_over_g"] for c in curve], ".--", label=label + " B/G")
                axes[0, 3].plot(
                    angles,
                    [
                        c["native_noise_check"]["native_raw_snr_db"]
                        if c["snr_eligible"]
                        else np.nan
                        for c in curve
                    ],
                    ".-",
                    label=label,
                )
        for axis, title in zip(
            axes[0],
            ("Photoelectrons", "Signal / center", "Color ratios", "Native RAW SNR (dB)"),
            strict=True,
        ):
            axis.set(title=title, xlabel="CRA (degrees)")
            axis.grid(alpha=0.2)
            axis.legend(fontsize=5)
        image_scene = (
            "macbeth_d65" if any(c["scene"] == "macbeth_d65" for c in cases) else preferred_scene
        )
        image_cases = [c for c in cases if c["scene"] == image_scene and c["fnumber"] == fnumber]
        peak = max(
            float(np.percentile(arrays[c["case_id"] + "_rgb_mean"], 99)) for c in image_cases
        )
        scale = 0.9 / peak if peak > 0 else 1
        for axis, mode in zip(axes[1], _MODES, strict=True):
            case = max(
                (c for c in image_cases if c["mode"] == mode),
                key=lambda c: (c["cra_deg"], c["azimuth_deg"]),
            )
            rgb = arrays[case["case_id"] + "_rgb_mean"]
            axis.imshow(np.clip(linear_to_srgb(np.maximum(rgb * scale, 0)), 0, 1))
            axis.set(
                title=(
                    f"{mode.replace('physical_', '')}\n"
                    f"CRA {case['cra_deg']}°, φ {case['azimuth_deg']}°"
                )
            )
            axis.axis("off")
        figure.suptitle(
            f"{sensor_id} / {light} / F{fnumber}: estimates, fixed D65, shared display scale"
        )
        figure.tight_layout()
        path = output / f"{sensor_id}_{light}.png"
        figure.savefig(path, dpi=120)
        plt.close(figure)
        figures.append(str(path))
    if report["sensitivity"]:
        figure, axes = plt.subplots(1, 3, figsize=(14, 4))
        for axis, parameter in zip(
            axes, ("collection_efficiency", "read_noise_e", "full_well_e"), strict=True
        ):
            groups = sorted({(c["sensor_id"], c["cra_deg"]) for c in report["sensitivity"]})
            for sensor_id, theta in groups:
                values = sorted(
                    [
                        c
                        for c in report["sensitivity"]
                        if c["sensor_id"] == sensor_id
                        and c["cra_deg"] == theta
                        and c["parameter"] == parameter
                    ],
                    key=lambda c: c["value"],
                )
                axis.plot(
                    [c["value"] for c in values],
                    [c["native_raw_snr_db"] if c["snr_eligible"] else np.nan for c in values],
                    ".-",
                    label=f"{sensor_id}, CRA {theta}°",
                )
            axis.set(title=parameter, xlabel="Assumed value", ylabel="Native RAW SNR (dB)")
            axis.grid(alpha=0.2)
            axis.legend(fontsize=6)
        exposure_ms = 1e3 * report["controls"]["exposure_s"]
        figure.suptitle(
            f"One-factor uncertainty at D65 / F4 / {exposure_ms:g} ms; conditional ranges"
        )
        figure.tight_layout()
        path = output / "sensitivity.png"
        figure.savefig(path, dpi=120)
        plt.close(figure)
        figures.append(str(path))
    if report["routing_sensitivity"]:
        figure, axes = plt.subplots(1, 2, figsize=(11, 4))
        nominal = {c["case_id"]: c for c in report["cases"]}
        for sensor_id in sorted({c["sensor_id"] for c in report["routing_sensitivity"]}):
            values = sorted(
                [c for c in report["routing_sensitivity"] if c["sensor_id"] == sensor_id],
                key=lambda c: c["cra_deg"],
            )
            angles = [c["cra_deg"] for c in values]
            axes[0].plot(angles, [c["relative_photocharge"] for c in values], ".-", label=sensor_id)
            axes[1].plot(
                angles,
                [
                    c["native_noise_check"]["native_raw_snr_db"] if c["snr_eligible"] else np.nan
                    for c in values
                ],
                ".--",
                label=f"{sensor_id} own-site",
            )
            axes[1].plot(
                angles,
                [
                    nominal[c["reference_case_id"]]["native_noise_check"]["native_raw_snr_db"]
                    if nominal[c["reference_case_id"]]["snr_eligible"]
                    else np.nan
                    for c in values
                ],
                ".-",
                label=f"{sensor_id} nominal",
            )
        axes[0].axhline(1, color="black", linestyle=":", label="nominal no routing loss")
        axes[0].set(title="Own-site charge / nominal charge", xlabel="CRA (degrees)")
        axes[1].set(title="Conditional native RAW SNR (dB)", xlabel="CRA (degrees)")
        for axis in axes:
            axis.grid(alpha=0.2)
            axis.legend(fontsize=7)
        figure.suptitle("Routing uncertainty: neighboring recollection unmeasured, D65 / F4")
        figure.tight_layout()
        path = output / "routing_sensitivity.png"
        figure.savefig(path, dpi=120)
        plt.close(figure)
        figures.append(str(path))
    return figures

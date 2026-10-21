"""Compute reproducible, uncalibrated sensor records without writing a database.

The caller owns persistence and quarantine. Optical capability follows the
existing automatic model selection; an unsupported angular response is never
filled with a normal-incidence curve. Native RAW is retained for every CFA.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from .assets import AssetStore
from .camera import camera_create, camera_get, camera_set
from .image_sensor_db import image_sensor_db_config, image_sensor_db_get
from .optics import oi_compute
from .scene import scene_adjust_illuminant, scene_adjust_luminance, scene_get
from .sensor import sensor_compute
from .sensor_capture import resample_sensor_capture
from .sensor_comparison import _fixed_isp_matrices, _jsonable, _linear_isp, _make_scene
from .sensor_estimate import apply_sensor_estimate, image_sensor_db_model
from .sensor_fdtd_comparison import _comparison_illuminant, sensor_pupil_directions
from .sensor_readout_profiles import apply_readout_profile, resolve_sensor_readout
from .sensor_simulation_policy import sensor_simulation_policy

_DEFAULTS = {
    "size": 32,
    "nframes": 8,
    "illuminants": ("D65", "A", "F11"),
    "exposure_s": (0.001, 0.004, 0.016),
    "seed": 0,
    "luminance_cd_m2": 100.0,
    "theta_deg": tuple(range(0, 61, 5)),
    "phi_deg": (0.0, 45.0),
    "pupil_fnumbers": (2.0, 4.0, 8.0),
    "pupil_cra_deg": (0.0, 20.0, 40.0),
}


def _settings(value):
    supplied = dict(value or {})
    if set(supplied) - set(_DEFAULTS):
        raise ValueError(f"Unknown simulation settings: {sorted(set(supplied) - set(_DEFAULTS))}")
    settings = {**_DEFAULTS, **supplied}
    for name, minimum in (("size", 8), ("nframes", 2), ("seed", 0)):
        v = settings[name]
        if (
            not isinstance(v, (int, float, np.integer, np.floating))
            or isinstance(v, bool)
            or not np.isfinite(v)
            or int(v) != v
            or v < minimum
        ):
            raise ValueError(f"{name} requires an integer >= {minimum}.")
        settings[name] = int(v)
    if settings["size"] % 4:
        raise ValueError("size must be a multiple of four to preserve native Bayer/Quad cells.")
    for name in ("exposure_s", "theta_deg", "phi_deg", "pupil_fnumbers", "pupil_cra_deg"):
        values = np.asarray(settings[name], float)
        if values.ndim != 1 or not values.size or not np.all(np.isfinite(values)):
            raise ValueError(f"{name} requires a nonempty finite sequence.")
        if len(np.unique(values)) != len(values):
            raise ValueError(f"{name} must not repeat values.")
        if name in {"exposure_s", "pupil_fnumbers"} and np.any(values <= 0):
            raise ValueError(f"{name} must be positive.")
        if name in {"theta_deg", "pupil_cra_deg"} and np.any((values < 0) | (values > 60)):
            raise ValueError(f"{name} must lie in [0,60].")
        settings[name] = values.tolist()
    settings["illuminants"] = list(settings["illuminants"])
    if not settings["illuminants"] or any(
        x not in {"D65", "A", "F11"} for x in settings["illuminants"]
    ):
        raise ValueError("illuminants must be a nonempty sequence drawn from D65, A, F11.")
    if len(set(settings["illuminants"])) != len(settings["illuminants"]):
        raise ValueError("illuminants must not repeat.")
    if not np.isfinite(settings["luminance_cd_m2"]) or settings["luminance_cd_m2"] <= 0:
        raise ValueError("luminance_cd_m2 must be finite and positive.")
    return settings


def resolved_simulation_settings(settings=None) -> dict[str, Any]:
    """Validate and expand settings before any batch work or source access."""
    return _jsonable(_settings(settings))


def _pupil(model, wave, cra, phi, fnumber):
    """Vectorized quadrature of the existing global direction-cosine pupil."""
    from .physical_estimate import physical_optical_response

    maximum_sine = np.sin(np.deg2rad(cra)) + 1 / (2 * fnumber)
    limit = float(np.rad2deg(np.arcsin(min(maximum_sine, 1))))
    info = {
        "cra_deg": cra,
        "phi_deg": phi,
        "fnumber": fnumber,
        "continuous_pupil_max_theta_deg": limit,
    }
    if maximum_sine > np.sin(np.deg2rad(60)) + 1e-12:
        return (
            None,
            None,
            {
                **info,
                "status": "limited",
                "reason": (
                    "Full direction-cosine pupil exceeds the 60-degree model domain; "
                    "no clipping or renormalization."
                ),
            },
        )
    previous = None
    comparisons = []
    for radial, azimuth in ((3, 8), (6, 16), (12, 32)):
        rays = sensor_pupil_directions(
            cra, phi, fnumber, radial_samples=radial, azimuth_samples=azimuth
        )
        directions = rays["directions"]
        theta = np.asarray([d["theta_deg"] for d in directions])
        angle = np.asarray([d["phi_deg"] for d in directions])
        weights = np.asarray([d["weight"] for d in directions])
        response = physical_optical_response(
            model, wave[None, :], theta_deg=theta[:, None], phi_deg=angle[:, None]
        )["eqe"]
        qe = np.sum(response * weights[:, None, None], axis=0)
        if previous is not None:
            error = float(np.max(np.abs(qe - previous) / np.maximum(qe, 0.01)))
            comparisons.append({"ray_count": len(directions), "max_relative_error": error})
            if error <= 0.01:
                return (
                    qe,
                    rays,
                    {
                        **info,
                        "status": "simulated",
                        "converged": True,
                        "relative_tolerance": 0.01,
                        "normalization_floor": 0.01,
                        "ray_count": len(directions),
                        "comparisons": comparisons,
                    },
                )
        previous = qe
    return (
        qe,
        rays,
        {
            **info,
            "status": "simulated",
            "converged": False,
            "relative_tolerance": 0.01,
            "normalization_floor": 0.01,
            "ray_count": len(directions),
            "comparisons": comparisons,
        },
    )


def _optical_arrays(model, settings):
    arrays = {
        "wave_nm": np.asarray(model["wavelength_nm"], float),
        "spectral_eqe": np.asarray(model["eqe"], float),
    }
    info = {"spectral_axes": ["wavelength", "native_filter"], "pupil_requests": []}
    raw_rays = None
    if model["kind"] != "physical_estimate_v1":
        info.update(
            angular_response_supported=False,
            pupil_response_supported=False,
            angular_reason="DB estimate supplies no calibrated or physical angular response.",
        )
        return arrays, info, raw_rays
    from .physical_estimate import physical_optical_response

    wave = arrays["wave_nm"]
    theta, phi = np.asarray(settings["theta_deg"]), np.asarray(settings["phi_deg"])
    arrays.update(theta_deg=theta, phi_deg=phi)
    budget = physical_optical_response(
        model, wave[None, None, :], theta_deg=theta[:, None, None], phi_deg=phi[None, :, None]
    )
    for name, value in budget.items():
        if isinstance(value, Mapping):
            for layer, absorption in value.items():
                arrays[f"angular_{name}_{layer}"] = absorption
        else:
            arrays[f"angular_{name}"] = value
    responses, coordinates = [], []
    cache = {}
    for fnumber in settings["pupil_fnumbers"]:
        for cra in settings["pupil_cra_deg"]:
            for azimuth in settings["phi_deg"]:
                key = (fnumber, cra, azimuth)
                qe, rays, details = _pupil(model, wave, cra, azimuth, fnumber)
                cache[key] = (qe, rays, details)
                if qe is not None:
                    details["array_index"] = len(responses)
                    responses.append(qe)
                    coordinates.append(key)
                info["pupil_requests"].append(details)
    raw = cache.get((4.0, 0.0, 0.0))
    if raw is None:
        raw = _pupil(model, wave, 0.0, 0.0, 4.0)
    _, raw_rays, raw_info = raw
    arrays["pupil_eqe"] = np.stack(responses) if responses else np.empty((0, len(wave), 4))
    arrays["pupil_coordinates"] = np.asarray(coordinates, float).reshape(-1, 3)
    info.update(
        angular_response_supported=True,
        pupil_response_supported=True,
        angular_axes=["theta", "phi", "wavelength", "native_filter"],
        pupil_axes=["valid_pupil_request", "wavelength", "native_filter"],
        pupil_coordinate_columns=["fnumber", "cra_deg", "phi_deg"],
        pupil_definition=(
            "Global direction-cosine disk with normalized projected-irradiance "
            "weights; not a rotated fixed-half-angle cone."
        ),
        raw_pupil=raw_info,
    )
    return arrays, info, raw_rays


def _raw_burst(sensor, nframes, seed):
    """Retain real integer RAW; temporal variance excludes static FPN scatter."""
    mean = np.zeros_like(sensor.data["mean_electrons"], dtype=float)
    m2 = np.zeros_like(mean)
    clipping = []
    first = None
    for index in range(nframes):
        frame = resample_sensor_capture(sensor, seed=seed + index)
        raw = frame.data["raw_dn"]
        delta = raw - mean
        mean += delta / (index + 1)
        m2 += delta * (raw - mean)
        clipping.append(frame.metadata["sensor_capture"]["saturation"])
        if first is None:
            first = frame
    variance = m2 / (nframes - 1)
    capture = first.metadata["sensor_capture"]
    photo = sensor.data["mean_electrons"]
    readout = sensor.fields["sensor_readout"]
    dark = float(sensor.fields["integration_time"]) * readout["dark_current_e_per_s"]
    dn_per_e = capture["dn_per_electron"]
    gain = np.asarray(first.fields.get("gain_fpn_image", 1.0))
    gain = gain * np.asarray(first.fields.get("column_gain_fpn", 1.0))
    expected = (photo + dark) * gain**2 + readout["read_noise_e"] ** 2 + 1 / (12 * dn_per_e**2)
    saturation = {name: float(np.mean([c[name] for c in clipping])) for name in clipping[0]}
    eligible = all(v <= 0.001 for v in saturation.values())

    def metrics(location):
        signal = float(np.mean(photo[location]))
        noise_dn = float(np.sqrt(np.mean(variance[location])))
        observed = float(np.mean(variance[location])) / dn_per_e**2
        predicted = float(np.mean(expected[location]))
        count = photo[location].size
        tolerance = max(0.10, 6 * np.sqrt(2 / (count * (nframes - 1))))
        error = abs(observed - predicted) / predicted if predicted else 0.0
        return {
            "mean_photoelectrons": signal,
            "mean_dark_electrons": dark,
            "mean_raw_dn": float(np.mean(mean[location])),
            "temporal_std_dn": noise_dn,
            "temporal_snr_db": float(20 * np.log10(signal * dn_per_e / noise_dn))
            if eligible and signal > 0 and noise_dn > 0
            else None,
            "snr_eligible": eligible and signal > 0,
            "noise_check": {
                "eligible": eligible,
                "passed": bool(error <= tolerance) if eligible else None,
                "predicted_variance_e2": predicted,
                "observed_variance_e2": observed,
                "relative_error": error,
                "relative_tolerance": tolerance,
            },
        }

    sites = []
    pattern = np.asarray(sensor.fields["pattern"])
    for row in range(pattern.shape[0]):
        for col in range(pattern.shape[1]):
            loc = (slice(row, None, pattern.shape[0]), slice(col, None, pattern.shape[1]))
            sites.append(
                {
                    "pattern_row": row,
                    "pattern_column": col,
                    "filter_name": sensor.fields["filter_names"][int(pattern[row, col]) - 1],
                    **metrics(loc),
                }
            )
    record = {
        **metrics(np.s_[:, :]),
        "sites": sites,
        "saturation": saturation,
        "saturation_max_frame": {name: max(c[name] for c in clipping) for name in clipping[0]},
        "capture": capture,
        "noise_formula": (
            "mean((photo_e+dark_e)*pixel_gain^2*column_gain^2)+read_noise_e^2+1/(12*dn_per_e^2)"
        ),
        "noise_scope": "Temporal per-pixel variance; static DSNU is not added as temporal noise.",
        "nframes": nframes,
        "seed_first_frame": seed,
    }
    stored = {
        "raw_dn": first.data["raw_dn"].astype(np.int32),
        "raw_mean_dn": mean,
        "raw_variance_dn2": variance,
        "mean_electrons": photo.copy(),
        "reference_dn": first.data["reference_dn"].copy(),
    }
    return record, stored, first


def simulate_sensor_record(sensor_id, *, root=None, settings=None):
    """Return JSON-safe metadata and NumPy arrays from actual optical/RAW runs.

    Defaults: 32x32 native pixels, eight frames, D65/A/F11 uniform 100 cd/m²,
    F/4, 1/4/16 ms. Native readout profiles are camera proxies or explicit
    generic assumptions. Unsupported records raise instead of fabricating data.
    """
    from .physical_estimate import apply_physical_estimate, image_sensor_physical_model

    controls = resolved_simulation_settings(settings)
    identity = image_sensor_db_get(sensor_id, root=root)
    policy = sensor_simulation_policy(identity["code"])
    if policy["action"] == "exclude":
        raise ValueError(f"{identity['code']}: excluded by source-scope policy: {policy['reason']}")
    config = image_sensor_db_config(
        sensor_id, root=root, model=policy.get("model_override", "auto")
    )
    kind = config["model"]
    profile = resolve_sensor_readout(config["sensor_id"], root=root)
    if kind == "physical_estimate_v1":
        model = image_sensor_physical_model(sensor_id, root=root, readout=profile["readout"])
    else:
        model = image_sensor_db_model(sensor_id, root=root)
    if policy["action"] == "override":
        original_pattern = model.get("cfa_pattern")
        if policy.get("cfa_pattern_override"):
            model["cfa_pattern"] = policy["cfa_pattern_override"]
        model["provenance"]["source_scope_override"] = {
            "original_cfa_pattern": original_pattern,
            "applied_cfa_pattern": model.get("cfa_pattern"),
            "policy": policy,
        }
        config["model_selection"] = {
            "requested": "auto_with_source_scope_policy",
            "selected": kind,
            "reason": policy["reason"],
        }
        config["requires_frozen_model_snapshot"] = bool(policy.get("cfa_pattern_override"))
    arrays, optical, rays = _optical_arrays(model, controls)
    if rays is not None:
        config["scenario"]["sensor_illumination"] = rays
        config["scenario"]["image_sensor_db"]["illumination_assumption"] = "explicit"
    config["scenario"].update(sensor_resize=False, sensor_output_mode="digital_raw")
    config["scenario"]["sensor"].update(
        integration_time=0.004, noise_flag=2, fixed_pattern_seed=controls["seed"]
    )
    metadata = {
        "schema": "camerae2e_sensor_simulation_v1",
        "sensor_id": config["sensor_id"],
        "report_code": config["code"],
        "device_name": config["device_name"],
        "manufacturer": config["manufacturer"],
        "model_kind": kind,
        "status": "simulated",
        "estimate_only": True,
        "measured_accuracy_verified": False,
        "config": config,
        "model_snapshot": model,
        "readout_profile": profile,
        "source_scope_policy": policy,
        "simulation_settings": {
            **controls,
            "fnumber": 4.0,
            "focal_length_m": 0.004,
            "auto_exposure": False,
            "wavelength_nm": arrays["wave_nm"],
            "fixed_pattern_seed": controls["seed"],
        },
        "optical_capabilities": optical,
        "cases": [],
        "stress_cases": [],
        "illuminant_provenance": {},
        "checks": {},
        "caveats": [
            "Model estimates, not measured sensor calibration or FDTD/TCAD results.",
            "Common CFA optical proxies and assumed collection efficiency remain unmeasured.",
            (
                "Most native readout profiles are generic assumptions; named camera profiles "
                "are not bare-sensor measurements."
            ),
            (
                "Uniform local fields do not validate spatial crosstalk, microlens color shading, "
                "MTF or HDR behavior."
            ),
            "Own-site acceptance is diagnostic; nominal optics apply no unverified routing loss.",
            "Clipped and zero-signal cases do not carry usable temporal SNR.",
            (
                "Output RGB, when available, is fixed-calibration linear RGB "
                "without display gain normalization."
            ),
        ],
    }
    store = AssetStore.default()
    camera = camera_create(asset_store=store)
    camera_set(camera, "optics focal length", 0.004)
    camera_set(camera, "optics fnumber", 4.0)
    camera_set(camera, "sensor size", [controls["size"]] * 2)
    camera_set(camera, "ip render scale", False)
    sensor = camera_get(camera, "sensor")
    sensor = (
        apply_physical_estimate(sensor, model, rays)
        if rays
        else apply_sensor_estimate(sensor, model)
    )
    sensor.fields.update(
        integration_time=0.004,
        auto_exposure=False,
        noise_flag=2,
        fixed_pattern_seed=controls["seed"],
    )
    sensor = apply_readout_profile(sensor, profile)
    metadata["native_cfa"] = {
        "filter_names": sensor.fields["filter_names"],
        "pattern": sensor.fields["pattern"],
        "pattern_index_base": 1,
        "native_size": sensor.fields["size"],
        "pixel_pitch_um": model["pixel_pitch_um"],
    }
    fov = float(
        np.rad2deg(2 * np.arctan(1.2 * controls["size"] * model["pixel_pitch_um"] * 1e-6 / 0.008))
    )
    scene, _ = _make_scene("uniform", (controls["size"], controls["size"]), fov, store)
    matrices = None
    # CFA-specific RAW always remains available; a standard RGB ISP is only claimed for Bayer.
    isp_supported = kind == "physical_estimate_v1" or model.get("cfa_pattern") in {
        "bayer",
        "rggb_bayer",
    }
    metadata["isp"] = {
        "available": isp_supported,
        "calibration": "fixed D65 sensor-to-XYZ; matched Bayer bilinear",
        "reason": None
        if isp_supported
        else "Standard Bayer RGB ISP is not claimed for this native CFA; RAW is retained.",
    }
    nominal_sensors = []
    optical_solves = 0
    for light in controls["illuminants"]:
        spectrum, _, provenance = _comparison_illuminant(light, scene_get(scene, "wave"))
        metadata["illuminant_provenance"][light] = provenance
        stimulus = scene_adjust_illuminant(scene.clone(), spectrum, asset_store=store)
        stimulus = scene_adjust_luminance(stimulus, controls["luminance_cd_m2"], asset_store=store)
        oi = oi_compute(camera_get(camera, "oi").clone(), stimulus)
        base = sensor_compute(sensor.clone(), oi, seed=controls["seed"])
        optical_solves += 1
        if not nominal_sensors:
            doubled = sensor.clone()
            doubled.fields["integration_time"] = 0.008
            twice = sensor_compute(doubled, oi, seed=controls["seed"])
            optical_solves += 1
            expected = base.data["mean_electrons"] * 2
            error = float(
                np.max(
                    np.abs(twice.data["mean_electrons"] - expected) / np.maximum(expected, 1e-15)
                )
            )
            metadata["checks"]["independent_double_exposure"] = {
                "passed": error < 1e-10,
                "maximum_relative_error": error,
                "exposure_pair_s": [0.004, 0.008],
            }
            if isp_supported:
                matrices = _fixed_isp_matrices(base, camera_get(camera, "ip"), store)
                arrays["isp_sensor_to_xyz"] = matrices[0]
                arrays["isp_xyz_to_rgb"] = matrices[1]
        for exposure in controls["exposure_s"]:
            sample = base.clone()
            sample.fields["integration_time"] = exposure
            sample.data["mean_electrons"] = base.data["mean_electrons"] * exposure / 0.004
            nominal_sensors.append(sample)
            case_id = f"case_{len(metadata['cases']):03d}"
            record, stored, first = _raw_burst(
                sample,
                controls["nframes"],
                controls["seed"] + controls["nframes"] * len(nominal_sensors),
            )
            if matrices is not None:
                rgb, xyz = _linear_isp(first, first.data["raw_dn"], matrices)
                stored.update(linear_rgb=rgb, xyz=xyz)
            arrays.update({f"{case_id}_{key}": value for key, value in stored.items()})
            metadata["cases"].append(
                {
                    "case_id": case_id,
                    "illuminant": light,
                    "exposure_s": exposure,
                    "mean_optical_method": (
                        "Independent illuminant solve; exact linear exposure scaling from 4ms mean."
                    ),
                    **record,
                }
            )
    base = nominal_sensors[0]
    for stress in ("dark", "bright"):
        sample = base.clone()
        if stress == "dark":
            sample.fields["integration_time"] = 1.0
            sample.data["mean_electrons"] = np.zeros_like(base.data["mean_electrons"])
        else:
            sample.fields["integration_time"] = 0.004
            sample.data["mean_electrons"] = np.full_like(
                base.data["mean_electrons"], 2 * profile["readout"]["full_well_e"]
            )
        case_id = f"stress_{stress}"
        record, stored, _ = _raw_burst(
            sample,
            controls["nframes"],
            controls["seed"]
            + controls["nframes"] * (len(nominal_sensors) + 1 + (stress == "bright")),
        )
        if stress == "dark":
            noiseless = sample.clone()
            noiseless.fields["noise_flag"] = 0
            noiseless = resample_sensor_capture(noiseless, seed=controls["seed"])
            capture = noiseless.metadata["sensor_capture"]
            electrons = (noiseless.data["reference_dn"] - capture["black_level_dn"]) / capture[
                "dn_per_electron"
            ]
            expected = profile["readout"]["dark_current_e_per_s"]
            error = float(np.max(np.abs(electrons - expected)))
            metadata["checks"]["dark_count"] = {
                "passed": error < 1e-10,
                "expected_electrons": expected,
                "maximum_absolute_error_electrons": error,
                "exposure_s": 1.0,
                "noise_flag": 0,
            }
        metadata["stress_cases"].append(
            {
                "case_id": case_id,
                "kind": stress,
                "input_method": (
                    "Controlled cached photocharge stress, "
                    "not an independently computed bright scene."
                ),
                **record,
            }
        )
        arrays.update({f"{case_id}_{key}": value for key, value in stored.items()})
    eqe_keys = [k for k in arrays if k in {"spectral_eqe", "angular_eqe", "pupil_eqe"}]
    checks = metadata["checks"]
    checks["eqe_bounds"] = {
        "passed": all(np.all((arrays[k] >= 0) & (arrays[k] <= 1)) for k in eqe_keys)
    }
    checks["all_arrays_finite"] = {"passed": all(np.all(np.isfinite(v)) for v in arrays.values())}
    checks["raw_codes"] = {
        "passed": all(
            np.issubdtype(v.dtype, np.integer)
            and np.all((v >= 0) & (v <= 2 ** profile["readout"]["bit_depth"] - 1))
            for k, v in arrays.items()
            if k.endswith("_raw_dn")
        )
    }
    if "angular_energy_residual" in arrays:
        error = float(np.max(np.abs(arrays["angular_energy_residual"])))
        checks["passive_energy_balance"] = {
            "passed": error < 1e-10,
            "maximum_absolute_residual": error,
        }
        checks["pupil_convergence"] = {
            "passed": all(p.get("converged", True) for p in optical["pupil_requests"])
            and optical["raw_pupil"]["converged"]
        }
    eligible = [
        c["noise_check"]
        for c in metadata["cases"] + metadata["stress_cases"]
        if c["noise_check"]["eligible"]
    ]
    checks["temporal_noise"] = {
        "passed": all(c["passed"] for c in eligible),
        "cases_checked": len(eligible),
        "maximum_relative_variance_error": max(c["relative_error"] for c in eligible),
    }
    site_checks = [
        site["noise_check"]
        for case in metadata["cases"] + metadata["stress_cases"]
        for site in case["sites"]
        if site["noise_check"]["eligible"]
    ]
    checks["native_site_temporal_noise"] = {
        "passed": all(c["passed"] for c in site_checks),
        "sites_checked": len(site_checks),
        "maximum_relative_variance_error": max(c["relative_error"] for c in site_checks),
        "sampling_tolerance": "Six Gaussian standard errors, with 10% minimum tolerance.",
    }
    checks["bright_clipping"] = {
        "passed": not metadata["stress_cases"][1]["snr_eligible"]
        and metadata["stress_cases"][1]["saturation"]["well_fraction"] > 0.99
    }
    checks["numerical_passed"] = bool(all(bool(c["passed"]) for c in checks.values()))
    if not checks["numerical_passed"]:
        metadata["status"] = "quarantined"
    metadata["optical_solve_count"] = optical_solves
    metadata["sample_count"] = controls["nframes"] * (
        len(metadata["cases"]) + len(metadata["stress_cases"])
    )
    metadata["array_schema"] = {
        key: {"shape": list(value.shape), "dtype": str(value.dtype)}
        for key, value in arrays.items()
    }
    metadata["array_units"] = {
        "wave_nm": "nm",
        "theta_deg": "degrees",
        "phi_deg": "degrees",
        "spectral_eqe": "collected electrons per incident photon",
        "angular_eqe": "collected electrons per incident photon",
        "pupil_eqe": "collected electrons per incident photon",
        "angular_power_budgets": "fraction of incident optical power",
        "own_site_acceptance": "fraction; diagnostic overlap, not measured crosstalk",
        "mean_electrons": "photoelectrons/pixel before dark, FPN and random noise",
        "raw_dn": "integer DN including black pedestal; first burst frame",
        "raw_mean_dn": "DN including black pedestal; temporal mean",
        "raw_variance_dn2": "DN squared; sample variance with nframes-1 denominator",
        "reference_dn": "continuous DN expectation before rounding, after ADC clipping",
        "linear_rgb": "linear RGB after fixed conversion; no adaptive exposure/display scale",
        "xyz": "XYZ under fixed sensor conversion matrix",
    }
    metadata["implementation_sha256"] = {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in (
            "sensor_simulation_batch.py",
            "physical_estimate.py",
            "sensor_estimate.py",
            "sensor_capture.py",
            "sensor_readout_profiles.py",
            "sensor_simulation_policy.py",
            "ip.py",
        )
    }
    return {"metadata": _jsonable(metadata), "arrays": arrays}

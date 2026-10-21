"""Validated, native-site spectral/angular FDTD absorption estimates.

The runtime averages unpolarized, incoherent incident directions before spectral
integration. LUT absorption is referenced to the entire 2x2 incident cell; four
times each site's fraction is its effective per-pixel optical response. This
response may exceed one at a site through optical redistribution. Cell energy,
not individual-site effective QE, is the conservation constraint.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from .sensor_capture import _DEFAULT_READOUT
from .types import OpticalImage, Sensor

_KIND = "fdtd_estimate_v1"
_SCHEMA = "camerae2e_fdtd_response_v1"
_WAVE = np.arange(400.0, 701.0, 10.0)
_SITES = ["R", "Gr", "Gb", "B"]
_ENERGY_TOLERANCE = 0.01
_RELATIVE_TOLERANCE = 0.03
_PASSIVITY_ROUNDOFF = 1e-10


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"FDTD {name} must be finite.") from exc
    if not np.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"FDTD {name} must be finite{' and positive' if positive else ''}.")
    return number


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"FDTD {name} must be a lowercase SHA-256 digest.")
    return value


def _validate_gates(validation: Any, sample_count: int, grid: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(validation, Mapping) or validation.get("status") != "passed":
        raise ValueError("FDTD response is unverified; all validation gates must pass.")
    gates = validation.get("gates", {})
    evidence = validation.get("evidence", {})
    if not isinstance(gates, Mapping) or not isinstance(evidence, Mapping):
        raise ValueError("FDTD validation gates and evidence must be mappings.")
    aliases = {
        "energy_balance": "energy_balance_pass",
        "spatial": "spatial_convergence_pass",
        "time": "time_convergence_pass",
        "pml": "pml_convergence_pass",
        "interpolation": "interpolation_pass",
    }
    for name, alias in aliases.items():
        passed = gates.get(name, validation.get(alias))
        row = evidence.get(name, {})
        if passed is not True or not isinstance(row, Mapping):
            raise ValueError(f"FDTD {name} validation gate has not passed.")
        key = "max_abs_error" if name == "energy_balance" else "max_relative_error"
        error = _finite(row.get(key), f"{name}.{key}")
        count = _finite(row.get("sample_count"), f"{name}.sample_count", positive=True)
        maximum = _ENERGY_TOLERANCE if name == "energy_balance" else _RELATIVE_TOLERANCE
        if error < 0 or error > maximum or count != int(count):
            raise ValueError(f"FDTD {name} numerical evidence exceeds the supported tolerance.")
        if name == "energy_balance" and count < sample_count:
            raise ValueError("FDTD energy evidence must cover every response sample.")
        if name != "energy_balance":
            ratio_error = _finite(
                row.get("max_angular_ratio_relative_error"), f"{name}.angular_ratio_error"
            )
            if not 0 <= ratio_error <= _RELATIVE_TOLERANCE:
                raise ValueError(f"FDTD {name} angular-ratio evidence exceeds tolerance.")
            if row.get("policy", validation.get("policy")) == "native_response_mixed_v1":
                for field in (
                    "max_weak_response_abs_error",
                    "max_angular_ratio_weak_response_abs_error",
                ):
                    weak_error = _finite(row.get(field), f"{name}.{field}")
                    if not 0 <= weak_error <= 0.001:
                        raise ValueError(
                            f"FDTD {name} weak-response absolute evidence exceeds tolerance."
                        )
            comparisons = row.get("comparisons")
            if (
                not isinstance(comparisons, list)
                or len(comparisons) != int(count)
                or any(not isinstance(item, Mapping) for item in comparisons)
            ):
                raise ValueError(f"FDTD {name} evidence requires the counted comparison records.")
            waves = [
                _finite(item.get("wavelength_nm"), f"{name}.comparison wavelength")
                for item in comparisons
            ]
            angles = [
                _finite(item.get("theta_deg"), f"{name}.comparison theta") for item in comparisons
            ]
            if name in {"spatial", "time", "pml"} and (
                min(waves) > grid["wavelength_nm"][0] or max(angles) < grid["theta_deg"][-1]
            ):
                raise ValueError(
                    f"FDTD {name} evidence must cover the shortest wave and largest angle."
                )
    return copy.deepcopy(dict(validation))


def _response_grid(payload: Mapping[str, Any]) -> tuple[dict[str, Any], float]:
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("FDTD response requires nonempty spectral/angular samples.")
    records: dict[tuple[float, float, float, str], np.ndarray] = {}
    maximum_energy_error = 0.0
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError("FDTD response samples must be mappings.")
        wave = _finite(sample.get("wavelength_nm"), "wavelength_nm", positive=True)
        theta = _finite(sample.get("theta_deg"), "theta_deg")
        phi = _finite(sample.get("phi_deg"), "phi_deg")
        pol = sample.get("polarization")
        if not 0 <= theta < 90 or not 0 <= phi < 360 or pol not in {"s", "p"}:
            raise ValueError("FDTD axes require 0<=theta<90, 0<=phi<360 and s/p polarization.")
        key = (wave, theta, phi, pol)
        if key in records:
            raise ValueError("FDTD response has duplicate wavelength/angular/polarization samples.")
        absorption = np.asarray(sample.get("absorption_cell_fraction"), dtype=float)
        if absorption.shape != (4,) or not np.all(np.isfinite(absorption)):
            raise ValueError("FDTD absorption_cell_fraction requires four finite native sites.")
        layers = sample.get("layer_absorption", {})
        if not isinstance(layers, Mapping):
            raise ValueError("FDTD layer_absorption must be a mapping.")
        if {"reflection", "transmission"} & set(layers):
            raise ValueError("FDTD layer absorption cannot replace reflection/transmission terms.")
        fractions = {
            "reflection": _finite(sample.get("reflection"), "reflection"),
            "transmission": _finite(sample.get("transmission"), "transmission"),
            **{str(k): _finite(v, f"layer_absorption.{k}") for k, v in layers.items()},
        }
        if np.any(
            (absorption < -_PASSIVITY_ROUNDOFF) | (absorption > 1 + _PASSIVITY_ROUNDOFF)
        ) or any(
            value < -_PASSIVITY_ROUNDOFF or value > 1 + _PASSIVITY_ROUNDOFF
            for value in fractions.values()
        ):
            raise ValueError("FDTD energy fractions must lie in [0, 1].")
        silicon = float(np.sum(absorption))
        if "silicon" in layers and not np.isclose(
            float(layers["silicon"]), silicon, rtol=0.0, atol=1e-6
        ):
            raise ValueError("FDTD silicon layer absorption must equal the native-site sum.")
        balance = silicon + sum(value for name, value in fractions.items() if name != "silicon")
        residual = balance - 1.0
        maximum_energy_error = max(maximum_energy_error, abs(residual))
        if abs(residual) > _ENERGY_TOLERANCE:
            raise ValueError("FDTD sample violates incident-cell energy conservation.")
        if "energy_balance_residual" in sample and not np.isclose(
            _finite(sample["energy_balance_residual"], "energy_balance_residual"),
            residual,
            rtol=0,
            atol=1e-6,
        ):
            raise ValueError("FDTD recorded energy residual disagrees with response fractions.")
        # Snap only machine-scale passivity excursions for interpolation.
        # Energy balance above deliberately used the untouched raw fractions.
        records[key] = np.clip(absorption, 0, 1)
    waves = np.array(sorted({key[0] for key in records}))
    theta = np.array(sorted({key[1] for key in records}))
    phi = np.array(sorted({key[2] for key in records if key[1] > 0}))
    if waves.size < 2 or waves[0] > 400 or waves[-1] < 700 or theta[0] != 0:
        raise ValueError("FDTD LUT must cover 400–700 nm and include normal incidence.")
    if phi.size == 0:
        phi = np.array([0.0])
    elif phi.size < 4 or phi[0] != 0 or np.max(np.diff(np.r_[phi, 360.0])) > 90 + 1e-9:
        raise ValueError(
            "FDTD oblique response needs periodic azimuth coverage at <=90 degree gaps."
        )
    grid = np.empty((waves.size, theta.size, phi.size, 4))
    for wi, wavelength in enumerate(waves):
        normal = []
        for pol in ("s", "p"):
            key = (wavelength, 0.0, 0.0, pol)
            if key not in records:
                raise ValueError("FDTD needs both normal-incidence polarizations at phi=0.")
            normal.append(records[key])
        grid[wi, 0, :, :] = np.mean(normal, axis=0)
        for ti in range(1, theta.size):
            for pi, azimuth in enumerate(phi):
                keys = [(wavelength, theta[ti], azimuth, pol) for pol in ("s", "p")]
                if any(key not in records for key in keys):
                    raise ValueError(
                        "FDTD response requires a complete oblique rectangular s/p grid."
                    )
                grid[wi, ti, pi] = (records[keys[0]] + records[keys[1]]) / 2
    expected_count = waves.size * (1 + (theta.size - 1) * phi.size) * 2
    if len(records) != expected_count:
        raise ValueError("FDTD normal azimuth must be collapsed to phi=0; unexpected grid samples.")
    return {
        "wavelength_nm": waves.tolist(),
        "theta_deg": theta.tolist(),
        "phi_deg": phi.tolist(),
        "unpolarized_absorption_cell_fraction": grid.tolist(),
    }, maximum_energy_error


def image_sensor_fdtd_model(
    sensor_id: str,
    lut_path: str | Path,
    *,
    root: str | Path | None = None,
    collection_efficiency: float = 0.75,
) -> dict[str, Any]:
    """Load a validated FDTD estimate tied to current canonical material/geometry.

    Unverified runs and missing/failed numerical gates are errors. A successful
    validation concerns numerical optical consistency, never measured accuracy.
    Oblique samples form a periodic azimuth grid with at most 90-degree gaps;
    normal incidence is represented once at phi=0 with both polarizations.
    """

    from .cfa_materials import image_sensor_cfa_materials
    from .image_sensor_db import image_sensor_db_get

    source = Path(lut_path).expanduser().resolve()
    raw_bytes = source.read_bytes()
    payload = json.loads(raw_bytes)
    if not isinstance(payload, Mapping):
        raise ValueError("FDTD LUT must be a JSON object.")
    record = image_sensor_db_get(sensor_id, root)
    bundle = image_sensor_cfa_materials(sensor_id, root=root)
    if payload.get("schema") != _SCHEMA:
        raise ValueError(f"FDTD LUT schema must be {_SCHEMA}.")
    if (
        payload.get("sensor_id") != record["sensor_id"]
        or payload.get("report_code") != record["code"]
    ):
        raise ValueError("FDTD LUT sensor/report identity does not match the requested DB record.")
    if payload.get("cell_pixels") != [2, 2] or payload.get("site_names") != _SITES:
        raise ValueError("FDTD LUT requires native 2x2 sites R, Gr, Gb, B in row-major order.")
    provenance = payload.get("provenance", {})
    if not isinstance(provenance, Mapping) or not (
        payload.get("estimate_only") is True or provenance.get("estimate_only") is True
    ):
        raise ValueError("FDTD optical response must explicitly be marked estimate_only.")
    hashes = {}
    for key, alias in (
        ("material_bundle_sha256", "material_hash"),
        ("geometry_sha256", "geometry_hash"),
        ("solver_code_sha256", "code_hash"),
    ):
        hashes[key] = _hash(payload.get(key, provenance.get(alias)), key)
        if key != "solver_code_sha256" and hashes[key] != bundle[key]:
            raise ValueError(f"FDTD {key} does not match the current canonical DB bundle.")
    pitch = _finite(payload.get("pixel_pitch_um"), "pixel_pitch_um", positive=True)
    if not np.isclose(pitch, float(bundle["pixel_pitch_um"]), rtol=1e-9, atol=0):
        raise ValueError("FDTD LUT pixel pitch does not match the canonical geometry.")
    efficiency = _finite(collection_efficiency, "collection_efficiency")
    if not 0 <= efficiency <= 1:
        raise ValueError("FDTD collection_efficiency must lie in [0, 1].")
    grid, energy_error = _response_grid(payload)
    validation = _validate_gates(payload.get("validation"), len(payload["samples"]), grid)
    recorded_error = validation["evidence"]["energy_balance"]["max_abs_error"]
    if float(recorded_error) + 1e-6 < energy_error:
        raise ValueError("FDTD energy validation understates the sample energy error.")
    normal = np.asarray(grid["unpolarized_absorption_cell_fraction"])[:, 0, 0, :]
    eqe = (
        np.column_stack(
            [np.interp(_WAVE, grid["wavelength_nm"], normal[:, site]) for site in range(4)]
        )
        * 4
        * efficiency
    )
    return {
        "kind": _KIND,
        "sensor_id": record["sensor_id"],
        "report_code": record["code"],
        "estimate_only": True,
        "pixel_pitch_um": pitch,
        "cell_pixels": [2, 2],
        "filter_names": _SITES.copy(),
        "wavelength_nm": _WAVE.tolist(),
        "eqe": eqe.tolist(),
        "collection_efficiency": efficiency,
        "readout": dict(_DEFAULT_READOUT),
        "response_grid": grid,
        "validation": validation,
        "provenance": {
            **copy.deepcopy(dict(provenance)),
            **hashes,
            "source_path": str(source),
            "lut_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "estimate_only": True,
            "measured_accuracy_verified": False,
            "normalization": "whole 2x2 incident cell",
            "site_to_pixel_area_factor": 4,
            "color_reference": "normal unpolarized D65",
            "angular_rule": "unpolarized periodic-phi multilinear interpolation; no extrapolation",
            "collection_source": "configured scalar assumption, not TCAD or measured collection",
            "collection_efficiency_definition": "carriers_collected_per_active_si_absorbed_photon",
            "passivity_roundoff_epsilon": _PASSIVITY_ROUNDOFF,
            "passivity_roundoff_rule": (
                "native fractions snapped only within epsilon; raw energy evidence retained"
            ),
        },
    }


def _illumination_config(illumination: Any) -> dict[str, Any]:
    if not isinstance(illumination, Mapping):
        raise ValueError("FDTD estimate requires explicit angular illumination or pupil geometry.")
    selected = copy.deepcopy(dict(illumination))
    if selected.get("polarization", "unpolarized") != "unpolarized":
        raise ValueError("FDTD estimate v1 supports unpolarized illumination only.")
    if "directions" in selected:
        if "exit_pupil_distance_m" in selected or "image_side_f_number" in selected:
            raise ValueError("Choose weighted directions or exit-pupil geometry, not both.")
        rays = selected["directions"]
        if not isinstance(rays, list) or not rays:
            raise ValueError("FDTD illumination directions must be a nonempty list.")
        weights = []
        for ray in rays:
            if not isinstance(ray, dict):
                raise ValueError("FDTD illumination directions must contain mappings.")
            theta = _finite(ray.get("theta_deg"), "illumination theta_deg")
            phi = _finite(ray.get("phi_deg"), "illumination phi_deg")
            weight = _finite(ray.get("weight"), "illumination weight")
            if not 0 <= theta < 90 or weight < 0 or not 0 <= phi < 360:
                raise ValueError("FDTD directions require valid theta/phi and nonnegative weights.")
            if ray.get("polarization", "unpolarized") != "unpolarized":
                raise ValueError("FDTD estimate v1 supports unpolarized directions only.")
            ray.update(theta_deg=theta, phi_deg=phi, weight=weight)
            weights.append(weight)
        total = float(np.sum(weights))
        if total <= 0:
            raise ValueError("FDTD angular illumination weights must have positive sum.")
        for ray in rays:
            ray["weight"] /= total
        selected["mode"] = "weighted_directions"
    else:
        selected["exit_pupil_distance_m"] = _finite(
            selected.get("exit_pupil_distance_m"), "exit_pupil_distance_m", positive=True
        )
        selected["image_side_f_number"] = _finite(
            selected.get("image_side_f_number"), "image_side_f_number", positive=True
        )
        for name, default in (("radial_samples", 3), ("azimuth_samples", 8)):
            count = _finite(selected.get(name, default), name, positive=True)
            if count != int(count) or count > 64:
                raise ValueError(f"FDTD pupil {name} must be an integer in [1, 64].")
            selected[name] = int(count)
        selected["mode"] = "exit_pupil"
        selected["f_number_definition"] = "exit-pupil distance / exit-pupil diameter"
        selected["weight_definition"] = "normalized cos(theta_sensor) dOmega = L^2/r^4 dA"
    return selected


def apply_fdtd_estimate(
    sensor: Sensor, model: Mapping[str, Any], illumination: Mapping[str, Any]
) -> Sensor:
    """Apply native-site optics and a fixed central color reference to a clone.

    This replaces optical factors and invalidates all cached captures. Recompute
    before resampling. Existing active FDTD/TCAD layers cannot be compounded.
    """

    if model.get("kind") != _KIND or model.get("validation", {}).get("status") != "passed":
        raise ValueError("apply_fdtd_estimate requires a validated fdtd_estimate_v1 model.")
    for name in ("fdtd_sensor", "tcad_sensor"):
        value = sensor.fields.get(name)
        if value is not None and (not isinstance(value, Mapping) or value.get("enabled", True)):
            raise ValueError(f"FDTD estimate cannot overlap active {name}.")
    if not sensor.fields.get("mosaic", True):
        raise ValueError("FDTD estimate v1 requires a native mosaic sensor.")
    configured_illumination = _illumination_config(illumination)
    result = sensor.clone()
    result.fields.pop("physical_estimate", None)
    result.fields.pop("physical_illumination", None)
    result.fields.pop("e2e_readout_profile", None)
    result.data.clear()
    result.metadata.pop("sensor_capture", None)
    result.metadata.pop("sensor_readout_profile", None)
    result.metadata.pop("sensor_simulation_db", None)
    wave = np.asarray(model["wavelength_nm"], dtype=float)
    pitch = float(model["pixel_pitch_um"]) * 1e-6
    result.fields.update(
        wave=wave.copy(),
        spectrum={"wave": wave.copy()},
        filter_spectra=np.asarray(model["eqe"], dtype=float),
        filter_names=_SITES.copy(),
        pattern=np.array([[1, 2], [3, 4]], dtype=int),
        pixel_qe=np.ones(wave.size),
        ir_filter=np.ones(wave.size),
        etendue=None,
        vignetting=0,
        ocl_group_proxy=None,
        fdtd_sensor={"enabled": False},
        tcad_sensor={"enabled": False},
        fdtd_estimate=copy.deepcopy(dict(model)),
        fdtd_illumination=configured_illumination,
        e2e_model_readout=copy.deepcopy(model["readout"]),
    )
    result.fields["pixel"].update(
        size_m=np.full(2, pitch),
        width_gap_m=0.0,
        height_gap_m=0.0,
        fill_factor=1.0,
        pd_size_m=np.full(2, pitch),
        pd_position_m=np.zeros(2),
        spectrum={"wave": wave.copy()},
    )
    summary = {key: copy.deepcopy(value) for key, value in model.items() if key != "response_grid"}
    summary["illumination"] = configured_illumination
    result.metadata["sensor_estimate"] = summary
    return result


def _response_interpolator(model: Mapping[str, Any]) -> RegularGridInterpolator:
    grid = model["response_grid"]
    phi = np.asarray(grid["phi_deg"], dtype=float)
    data = np.asarray(grid["unpolarized_absorption_cell_fraction"], dtype=float)
    # Extend periodic azimuth with the measured phi=0 plane. The one-direction
    # normal-only case has no angular interpolation/extrapolation.
    if phi.size > 1:
        phi = np.r_[phi, 360.0]
        data = np.concatenate([data, data[:, :, :1, :]], axis=2)
    return RegularGridInterpolator(
        (np.asarray(grid["wavelength_nm"]), np.asarray(grid["theta_deg"]), phi),
        data,
        method="linear",
        bounds_error=True,
    )


def _validate_runtime_optics(sensor: Sensor) -> None:
    """Keep a validated LUT tied to the optical geometry actually being captured."""

    model = sensor.fields["fdtd_estimate"]
    if sensor.fields.get("physical_estimate") is not None:
        raise ValueError("FDTD estimate cannot overlap a physical spectral model.")
    pixel = sensor.fields["pixel"]
    pitch = float(model["pixel_pitch_um"]) * 1e-6
    for name in ("fdtd_sensor", "tcad_sensor"):
        layer = sensor.fields.get(name)
        if layer is not None and (not isinstance(layer, Mapping) or layer.get("enabled", True)):
            raise ValueError(f"FDTD estimate cannot overlap active {name} during capture.")
    if (
        not sensor.fields.get("mosaic", True)
        or not np.array_equal(sensor.fields["pattern"], [[1, 2], [3, 4]])
        or sensor.fields["filter_names"] != _SITES
    ):
        raise ValueError("FDTD capture CFA must retain the LUT's native R/Gr/Gb/B sites.")
    for name in ("size_m", "pd_size_m"):
        values = np.asarray(pixel[name], dtype=float)
        if values.shape != (2,) or not np.allclose(values, pitch, rtol=1e-9, atol=0):
            raise ValueError("FDTD capture pixel geometry differs from the validated LUT.")
    if (
        float(pixel["fill_factor"]) != 1
        or float(pixel.get("width_gap_m", 0)) != 0
        or float(pixel.get("height_gap_m", 0)) != 0
        or np.any(np.asarray(pixel.get("pd_position_m", [0, 0])) != 0)
    ):
        raise ValueError("FDTD capture must retain the LUT's complete per-pixel optical area.")
    if (
        not np.array_equal(sensor.fields["filter_spectra"], model["eqe"])
        or not np.array_equal(sensor.fields["wave"], model["wavelength_nm"])
        or any(
            not np.all(np.asarray(sensor.fields[name], dtype=float) == 1)
            for name in ("pixel_qe", "ir_filter")
        )
    ):
        raise ValueError(
            "FDTD central spectral reference cannot be replaced by generic optical QE."
        )
    if (
        sensor.fields.get("ocl_group_proxy") is not None
        or sensor.fields.get("vignetting", 0) != 0
        or (
            sensor.fields.get("etendue") is not None
            and not np.all(np.asarray(sensor.fields["etendue"]) == 1)
        )
    ):
        raise ValueError("FDTD capture cannot compound generic OCL/vignetting optical factors.")


def _incident_directions(
    illumination: Mapping[str, Any], x: np.ndarray, y: np.ndarray
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    if illumination["mode"] == "weighted_directions":
        return [
            (
                np.full_like(x, row["theta_deg"]),
                np.full_like(x, row["phi_deg"]),
                np.full_like(x, row["weight"]),
            )
            for row in illumination["directions"]
            if row["weight"] > 0
        ]
    distance = illumination["exit_pupil_distance_m"]
    radius = distance / (2 * illumination["image_side_f_number"])
    rays = []
    # Midpoints of equal-area radial annuli crossed with equal azimuth cells.
    for ri in range(illumination["radial_samples"]):
        pupil_r = radius * np.sqrt((ri + 0.5) / illumination["radial_samples"])
        for ai in range(illumination["azimuth_samples"]):
            angle = 2 * np.pi * (ai + 0.5) / illumination["azimuth_samples"]
            dx, dy = x - pupil_r * np.cos(angle), y - pupil_r * np.sin(angle)
            length_squared = distance**2 + dx**2 + dy**2
            theta = np.rad2deg(np.arctan2(np.hypot(dx, dy), distance))
            phi = np.mod(np.rad2deg(np.arctan2(dy, dx)), 360.0)
            rays.append((theta, phi, distance**2 / length_squared**2))
    total = sum(ray[2] for ray in rays)
    return [(theta, phi, weight / total) for theta, phi, weight in rays]


def fdtd_response(
    model: Mapping[str, Any],
    wavelength_nm: Any,
    illumination: Mapping[str, Any],
) -> np.ndarray:
    """Return unpolarized effective pixel QE [wavelength, R/Gr/Gb/B].

    Explicit directions describe one uniform angular distribution. Exit-pupil
    geometry evaluates the sensor center. Weights are normalized irradiance
    fractions and the returned response includes collection_efficiency once.
    This helper shares the interpolation and pupil quadrature used in capture.
    """

    config = _illumination_config(illumination)
    wave = np.asarray(wavelength_nm, dtype=float).reshape(-1)
    grid = model["response_grid"]
    if (
        wave.size == 0
        or not np.all(np.isfinite(wave))
        or np.any((wave < grid["wavelength_nm"][0]) | (wave > grid["wavelength_nm"][-1]))
    ):
        raise ValueError("FDTD response spectrum exceeds the validated LUT; no extrapolation.")
    interpolate = _response_interpolator(model)
    zero = np.zeros((1, 1))
    response = np.zeros((wave.size, 4))
    for theta, phi, weight in _incident_directions(config, zero, zero):
        angle = float(theta[0, 0])
        if angle > grid["theta_deg"][-1]:
            raise ValueError("FDTD response angle exceeds the validated LUT; no extrapolation.")
        azimuth = float(phi[0, 0]) if len(grid["theta_deg"]) > 1 else 0.0
        points = np.column_stack([wave, np.full(wave.size, angle), np.full(wave.size, azimuth)])
        response += interpolate(points) * float(weight[0, 0])
    return response * 4 * float(model["collection_efficiency"])


def fdtd_estimate_current_density(oi: OpticalImage, sensor: Sensor) -> np.ndarray:
    """Return A/m² in four native-site channels before sensor spatial integration.

    The optical-image photon irradiance already carries aperture throughput.
    Angular weights therefore sum to one at every position; this function adds
    neither another f-number throughput factor nor a scene-FOV-to-CRA proxy.
    """

    from .optics import oi_get
    from .sensor import _ELEMENTARY_CHARGE_C

    _validate_runtime_optics(sensor)
    model = sensor.fields["fdtd_estimate"]
    illumination = sensor.fields["fdtd_illumination"]
    photons = np.asarray(oi.data["photons"], dtype=float)
    wave = np.asarray(oi.fields["wave"], dtype=float)
    if photons.ndim != 3 or photons.shape[2] != wave.size or not np.all(np.isfinite(photons)):
        raise ValueError("FDTD illumination requires a finite spectral optical-image cube.")
    if np.any(photons < 0) or wave.size == 0 or np.any(np.diff(wave) <= 0):
        raise ValueError("FDTD photon irradiance must be nonnegative with increasing wavelengths.")
    rows, cols = photons.shape[:2]
    xs = (np.arange(cols) - (cols - 1) / 2) * float(oi_get(oi, "wspatialresolution"))
    ys = (np.arange(rows) - (rows - 1) / 2) * float(oi_get(oi, "hspatialresolution"))
    x, y = np.meshgrid(xs, ys)
    directions = _incident_directions(illumination, x, y)
    interpolate = _response_interpolator(model)
    grid = model["response_grid"]
    min_wave, max_wave = grid["wavelength_nm"][0], grid["wavelength_nm"][-1]
    if np.any(wave < min_wave) or np.any(wave > max_wave):
        raise ValueError("FDTD optical-image spectrum exceeds the validated LUT; no extrapolation.")
    maximum_theta = grid["theta_deg"][-1]
    normal_only = len(grid["theta_deg"]) == 1
    for theta, _, _ in directions:
        if np.any(theta > maximum_theta + 1e-10):
            raise ValueError(
                "FDTD incident/pupil angles exceed the validated LUT; no extrapolation."
            )
    # Pixel photon integration uses wavelength-bin widths, retaining wavelength
    # dependence of angle/color response until the final accumulation.
    widths = np.gradient(wave) if wave.size > 1 else np.ones_like(wave)
    electron_rate_density = np.zeros((rows, cols, 4), dtype=float)
    for wi, wavelength in enumerate(wave):
        qe = np.zeros_like(electron_rate_density)
        for theta, phi, weight in directions:
            azimuth = np.zeros_like(phi) if normal_only else phi
            points = np.stack([np.full_like(theta, wavelength), theta, azimuth], axis=-1)
            qe += interpolate(points) * weight[:, :, None]
        qe *= 4 * model["collection_efficiency"]
        electron_rate_density += photons[:, :, wi, None] * widths[wi] * qe
    return electron_rate_density * _ELEMENTARY_CHARGE_C

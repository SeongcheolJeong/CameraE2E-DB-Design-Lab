"""Explicit relative angular estimates layered onto a saved normal-incidence QE.

The physical solver is CameraE2E's existing incoherent-stack model. The absolute
response is used only to obtain EQE(theta,phi)/EQE(0), never as an extra absolute
QE factor. Canonical DB files and saved sensor runs remain unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
from importlib.resources import files
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

SCHEMA = "camera_virtual_lab_relative_angular_v1"
SITES = ["R", "Gr", "Gb", "B"]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _waves(values):
    result = np.asarray(values, dtype=float)
    if (
        result.ndim != 1
        or not 1 <= result.size <= 256
        or not np.isfinite(result).all()
        or np.any(np.diff(result) <= 0)
        or result[0] < 400
        or result[-1] > 700
    ):
        raise ValueError("Angular response needs 1..256 increasing wavelengths in 400..700 nm")
    return result


def _report_code(workspace, sensor_id):
    if sensor_id not in {"S1", "S2"}:
        return str(sensor_id)
    source = (
        Path(workspace) / "reports/camera_platform/road-design-20261003-reviewed/specification.json"
    )
    plan = json.loads(source.read_text())
    for candidate in plan["candidates"]:
        if candidate["id"].endswith("-" + sensor_id):
            return candidate["sensor"]["revision"]["report_code"]
    raise ValueError("Pinned Lab sensor alias has no source revision")


def _material_bundle(workspace, sensor_id):
    """Build an explicitly derived in-memory material bundle, with source hashes.

    S1's stored CFA proxy misuses the 3.6um total optical stack as CFA thickness.
    The same source has an explicit .68..87um CFA range. We choose its midpoint
    as a new assumption, not as a new measured product value or DB overwrite.
    """
    from pyisetcam.cfa_materials import _material_identity, image_sensor_cfa_materials
    from pyisetcam.image_sensor_db import image_sensor_db_get
    from pyisetcam.sensor_estimate import _enriched_context, _local_generated_path, _optical_model

    root = Path(workspace) / "camerae2e_db/fdtd_tcad/image_sensor_db"
    record = image_sensor_db_get(sensor_id, root=root)
    code = record["code"]
    if code != "DEF-2509-801":
        bundle = copy.deepcopy(image_sensor_cfa_materials(sensor_id, root=root))
        original = str(bundle.get("cfa_pattern", "")).lower()
        aliases = {"rggb_bayer": "rggb", "bayer": "bayer", "rggb": "rggb"}
        if original not in aliases:
            raise ValueError("Relative angular model supports RGB Bayer sensor material bundles")
        bundle["cfa_pattern"] = aliases[original]
        return bundle, [
            {
                "field": "cfa_pattern",
                "original": original,
                "derived": aliases[original],
                "classification": "canonical_alias",
            }
        ]

    enriched, records = _enriched_context(root)
    raw = records[str(code).lower()]
    optical_path, optical = _optical_model(enriched, raw)
    stack_path = _local_generated_path(
        enriched, raw.get("generated_files", {}).get("stack_config"), "generated_stack_configs"
    )
    if stack_path is None:
        raise ValueError("S1 angular estimate requires its existing stack geometry")
    stack = json.loads(stack_path.read_text())
    if stack.get("calibration_status", {}).get("source_report_code") != code:
        raise ValueError("S1 stack identity does not match the selected sensor")
    proxy, cfa = optical["cfa_proxy_nk"], optical["optical"]["cfa"]
    low, high = float(cfa["thickness_min_um"]["value"]), float(cfa["thickness_max_um"]["value"])
    if not np.allclose([low, high], [0.68, 0.87], rtol=0, atol=1e-12):
        raise ValueError("S1 source CFA range changed; review the explicit derived assumption")
    thickness = (low + high) / 2
    channels = {}
    for name in ("red", "green", "blue"):
        samples = proxy["channels"][name]["data"]
        channels[name] = _material_identity(
            {
                "wavelength_nm": [row["wavelength_nm"] for row in samples],
                "n": [row["n"] for row in samples],
                "k": [row["k"] for row in samples],
                "thickness_um": thickness,
                "product_measured": False,
                "source_sha256": _sha(optical_path),
                "source_kind": "common_CFA_proxy_nk",
            }
        )
    material_resource = files("pyisetcam").joinpath(
        "data/sensor_estimate/optical_materials_v1.json"
    )
    shared = json.loads(material_resource.read_text())
    materials = {
        name: _material_identity(copy.deepcopy(value))
        for name, value in shared["materials"].items()
    }
    geometry = copy.deepcopy(stack["geometry_um"])
    height_field = optical["optical"]["geometry"]["optical_stack_height_um"]
    height = float(height_field["value"])
    geometry["cfa_thickness"] = thickness
    geometry["passivation_thickness"] = height - float(geometry["lens_height"]) - thickness
    if geometry["passivation_thickness"] <= 0:
        raise ValueError("Derived S1 geometry leaves no positive passivation thickness")
    bundle = {
        "schema": "camerae2e_cfa_material_bundle_v1",
        "sensor_id": record["sensor_id"],
        "report_code": code,
        "pixel_pitch_um": float(geometry["pitch"]),
        "cfa_pattern": "bayer",
        "geometry_um": geometry,
        "channels": channels,
        "materials": materials,
        "estimate_only": True,
        "provenance": {
            "optical_model_sha256": _sha(optical_path),
            "generated_stack_sha256": _sha(stack_path),
        },
    }
    bundle["material_bundle_sha256"] = _digest({"channels": channels, "materials": materials})
    overlays = [
        {
            "field": "cfa_thickness_um",
            "original": proxy["thickness_um"]["value"],
            "derived": thickness,
            "source_range_um": [low, high],
            "classification": "assumed_midpoint_of_extracted_CFA_range",
            "reason": "Stored 3.6um proxy value describes the whole optical stack.",
        },
        {
            "field": "passivation_thickness_um",
            "derived": geometry["passivation_thickness"],
            "classification": "remaining_stack_height_after_derived_CFA_and_lens",
        },
    ]
    return bundle, overlays


def build_response(workspace, sensor_id, wave_nm):
    """Return [wavelength,theta,phi,site] relative multipliers and provenance."""
    from pyisetcam import physical_estimate

    wave = _waves(wave_nm)
    code = _report_code(workspace, sensor_id)
    bundle, overlays = _material_bundle(workspace, code)
    geometry = bundle["geometry_um"]
    assumptions = physical_estimate._assumptions(
        {"geometry_source": "canonical_db", "routing_mode": "no_routing_loss"}, geometry
    )
    # This is the existing physical solver's complete optical input, independent
    # of readout. Collection cancels in the ratio; it is not applied to DB QE.
    model = {
        "kind": "physical_estimate_v1",
        "material_bundle": bundle,
        "geometry_um": geometry,
        "optical_assumptions": assumptions,
        "collection_efficiency": 0.75,
    }
    theta, phi = np.arange(0.0, 60.01, 5.0), np.arange(0.0, 360.0, 90.0)
    response = physical_estimate.physical_optical_response(
        model, wave[:, None, None], theta_deg=theta[None, :, None], phi_deg=phi[None, None, :]
    )["eqe"]
    normal = response[:, :1, :1, :]
    if np.any(normal <= 1e-15):
        raise ValueError("Physical normal EQE is too small to define a relative angular response")
    relative = response / normal
    relative[:, 0] = 1.0
    if not np.isfinite(relative).all() or np.any(relative < 0):
        raise ValueError("Physical relative angular response is invalid")
    table = {
        "schema": SCHEMA,
        "mode": "db_normal_qe_times_derived_physical_angular_ratio",
        "site_names": SITES.copy(),
        "wavelength_nm": wave.tolist(),
        "theta_deg": theta.tolist(),
        "phi_deg": phi.tolist(),
        "relative_response": relative.tolist(),
        "provenance": {
            "sensor_id": bundle["sensor_id"],
            "report_code": bundle["report_code"],
            "pixel_pitch_um": bundle["pixel_pitch_um"],
            "derived_overlays": overlays,
            "source_basis": (
                "Canonical sensor material n,k and stack geometry with the listed in-memory "
                "overlays; existing physical_estimate incoherent-stack angular solver. "
                "The saved DB normal-incidence QE remains the absolute spectral basis."
            ),
            "material_bundle_sha256": bundle["material_bundle_sha256"],
            "source": bundle.get("provenance", {}),
            "derived_geometry_um": geometry,
            "solver": "pyisetcam.physical_estimate.physical_optical_response",
            "solver_sha256": _sha(physical_estimate.__file__),
            "angular_adapter_sha256": _sha(__file__),
            "optical_assumptions": assumptions,
            "normal_reference": "Physical EQE(theta=0); saved DB spectral QE remains unchanged.",
            "combination": "effective_EQE = saved_DB_normal_EQE * relative_response; once only",
            "normal_EQE_physical": normal[:, 0, 0].tolist(),
            "collection_efficiency": "Cancels in ratio; no extra collection multiplier.",
            "scope": (
                "Unmeasured planar Fresnel/absorption angular sensitivity; no spatial crosstalk."
            ),
            "microlens_scope": "Own-site acceptance is diagnostic only; no routing loss applied.",
            "database_modified": False,
        },
    }
    table["table_sha256"] = _digest(table)
    return table


def cosine_power_response(wave_nm, power=4.0):
    """Explicit generic sensitivity alternative; never a fallback for bad assets."""
    wave = _waves(wave_nm)
    if not np.isfinite(power) or not 0 <= float(power) <= 20:
        raise ValueError("Cosine power must lie in 0..20")
    theta, phi = np.arange(0.0, 60.01, 5.0), np.arange(0.0, 360.0, 90.0)
    relative = np.broadcast_to(
        np.cos(np.deg2rad(theta))[None, :, None, None] ** power,
        (len(wave), len(theta), len(phi), 4),
    ).copy()
    table = {
        "schema": SCHEMA,
        "mode": "declared_cosine_power_sensitivity",
        "site_names": SITES.copy(),
        "wavelength_nm": wave.tolist(),
        "theta_deg": theta.tolist(),
        "phi_deg": phi.tolist(),
        "relative_response": relative.tolist(),
        "provenance": {
            "power": float(power),
            "classification": "explicit_generic_assumption",
            "source_basis": "Declared cos(theta)**power at all wavelengths and CFA sites.",
            "angular_adapter_sha256": _sha(__file__),
            "scope": "Sensitivity model; not a measured or DB product angular response.",
        },
    }
    table["table_sha256"] = _digest(table)
    return table


def interpolate_relative(table, theta_deg, azimuth_deg, wave_nm):
    """Broadcast theta/azimuth/wavelength into nonnegative query-shape + four sites."""
    if (
        not isinstance(table, dict)
        or table.get("schema") != SCHEMA
        or table.get("site_names") != SITES
    ):
        raise ValueError("Relative angular response table schema/site order is unsupported")
    wave = _waves(table["wavelength_nm"])
    theta_axis, phi_axis = (
        np.asarray(table["theta_deg"], float),
        np.asarray(table["phi_deg"], float),
    )
    values = np.asarray(table["relative_response"], float)
    if (
        theta_axis.ndim != 1
        or theta_axis.size < 2
        or theta_axis[0] != 0
        or theta_axis[-1] > 60
        or not np.isfinite(theta_axis).all()
        or np.any(np.diff(theta_axis) <= 0)
        or phi_axis.shape != (4,)
        or not np.array_equal(phi_axis, [0, 90, 180, 270])
        or values.shape != (wave.size, theta_axis.size, 4, 4)
        or not np.isfinite(values).all()
        or np.any(values < 0)
        or not np.allclose(values[:, 0], 1, atol=1e-12, rtol=0)
    ):
        raise ValueError(
            "Relative angular response table has invalid axes, shape or normal response"
        )
    t, p, w = np.broadcast_arrays(
        np.asarray(theta_deg, float), np.asarray(azimuth_deg, float), np.asarray(wave_nm, float)
    )
    if (
        not np.isfinite(t).all()
        or not np.isfinite(p).all()
        or not np.isfinite(w).all()
        or np.any(t < theta_axis[0])
        or np.any(t > theta_axis[-1])
        or np.any(w < wave[0])
        or np.any(w > wave[-1])
    ):
        raise ValueError("Angular query exceeds the declared wavelength/theta domain")
    periodic_values = np.concatenate([values, values[:, :, :1]], axis=2)
    interp = RegularGridInterpolator(
        (wave, theta_axis, np.r_[phi_axis, 360]),
        periodic_values,
        method="linear",
        bounds_error=True,
    )
    query = np.stack([w, t, np.mod(p, 360)], axis=-1)
    return interp(query).reshape(t.shape + (4,))

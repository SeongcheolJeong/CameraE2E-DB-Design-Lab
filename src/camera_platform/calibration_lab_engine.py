"""Bounded measured-chart fitting and fresh conditional camera reproductions.

The camera-effective blur is a replacement for the combined spatial response.
It is never added to an optical PSF or followed by a second pixel-aperture blur.
Parameter recovery, chart agreement and independent physical validation remain
different claims. The original measurements and caller-owned profiles are immutable.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import OptimizeResult, least_squares, lsq_linear, minimize_scalar

from . import electrical, virtual_lab_metrics
from .calibration_lab_contracts import (
    MAX_MEASUREMENT_BYTES,
    _bounded_json_bytes,
    measurement_resource_budget,
)

SCHEMA = "camera_calibration_profile_v1"
CHARTS = ("dark", "ptc", "geometry", "shading", "mtf", "color", "tone", "texture")
SITES = ((0, 0), (0, 1), (1, 0), (1, 1))
MAX_POINTS = 4096
MAX_PROFILE_KERNEL_SAMPLES = 4_000_000
MAX_RENDER_SIDE = 256
REPEATS = 8
MAX_RAW_SAMPLES = 32_000_000
FIELD_COORDINATE_DOMAINS = {"sensor_normalized", "reference_lens_object_direction"}


def default_profile():
    """Return authored assumptions, not a measured calibration or product model."""
    return {
        "schema": SCHEMA,
        "native_shape": [128, 128],
        "pixel_pitch_um": 3.0,
        "cfa": "RGGB",
        "render_shape": [128, 128],
        "conditions": {
            "mode_id": "linear-rggb",
            "gain": 1.0,
            "temperature_c": 25.0,
            "light_id": "D65",
            "aperture_f_number": 4.0,
            "focus_distance_m": 2.0,
        },
        "electronics": {
            "bit_depth": 12,
            "black_level_dn": 64.0,
            "dn_per_e": 0.4,
            "read_noise_e": 2.0,
            "dark_current_e_per_s": 0.1,
            "full_well_e": 10000.0,
            "full_well_identified": False,
        },
        "shading": {"radial_coefficients": [0.0, 0.0]},
        "geometry": {
            "mode": "known_ideal_warp",
            "radial_coefficients": [0.0, 0.0],
            "meaning": "known ideal to observed normalized sensor coordinates",
        },
        "blur": {
            "application": "effective_system_replacement",
            "family": "centered_elliptical_gaussian_v1",
            "field_nodes": [
                {"field_xy": [0.0, 0.0], "sigma_x_px": 1.0, "sigma_y_px": 1.0, "theta_deg": 0.0}
            ],
            "included_effects": ["lens_blur", "pixel_aperture", "charge_diffusion"],
            "spectral_scope": "conditioned_on_measured_illuminant",
            "field_application": "authored_spatially_invariant",
            "phase": "zero",
            "unique_physical_psf": False,
        },
        "isp": {
            "color_matrix": np.eye(3).tolist(),
            "tone_input": [0.0, 1.0],
            "tone_output": [0.0, 1.0],
        },
        "raw_response": {
            "color_matrix": np.eye(3).tolist(),
            "tone_input": [0.0, 1.0],
            "tone_output": [0.0, 1.0],
            "meaning": "conditional mean photocharge response before CFA and shot noise",
        },
        "reference_white_e": 4000.0,
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def _array(value, name, ndim=None, *, minimum=None, maximum=None):
    try:
        source = np.asarray(value)
        if source.dtype.kind not in "uif" or source.dtype.kind == "b":
            raise ValueError
        a = source.astype(float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite real numbers") from exc
    if not a.size or a.size > MAX_POINTS * 9 or not np.isfinite(a).all():
        raise ValueError(f"{name} is empty, nonfinite or exceeds the observation budget")
    if ndim is not None and a.ndim != ndim:
        raise ValueError(f"{name} requires {ndim} dimensions")
    if minimum is not None and np.any(a < minimum):
        raise ValueError(f"{name} is below {minimum}")
    if maximum is not None and np.any(a > maximum):
        raise ValueError(f"{name} exceeds {maximum}")
    return a


def _number(value, name, low, high):
    a = _array([value], name, 1, minimum=low, maximum=high)
    return float(a[0])


def validate_profile(value):
    """Validate the public profile before fitting or allocating render arrays."""
    _bounded_json_bytes(value, MAX_MEASUREMENT_BYTES, "Camera profile")
    p = deepcopy(value)
    p.setdefault("raw_response", deepcopy(default_profile()["raw_response"]))
    if p.get("schema") != SCHEMA or p.get("cfa") != "RGGB":
        raise ValueError("Calibration chart capture requires a v1 linear RGGB profile")
    for key, upper in (("native_shape", 100000), ("render_shape", MAX_RENDER_SIDE)):
        shape = p.get(key, p.get("native_shape"))
        if (
            not isinstance(shape, (list, tuple))
            or len(shape) != 2
            or any(type(x) is not int or x < 32 or x > upper or x % 2 for x in shape)
        ):
            raise ValueError(f"{key} requires two even dimensions in [32,{upper}]")
        p[key] = list(shape)
    if any(a > b for a, b in zip(p["render_shape"], p["native_shape"], strict=True)):
        raise ValueError("Render crop exceeds the native sensor; resizing is unsupported")
    _number(p["pixel_pitch_um"], "pixel_pitch_um", 0.1, 100)
    _number(p["reference_white_e"], "reference_white_e", 1, 1e7)
    e = p["electronics"]
    if type(e["bit_depth"]) is not int or not 8 <= e["bit_depth"] <= 16:
        raise ValueError("Chart simulator supports 8..16 bit linear RAW")
    _number(e["black_level_dn"], "black_level_dn", 0, 2 ** e["bit_depth"] - 2)
    for key, lo, hi in (
        ("dn_per_e", 1e-5, 1000),
        ("read_noise_e", 0, 10000),
        ("dark_current_e_per_s", 0, 1e8),
        ("full_well_e", 1, 1e9),
    ):
        _number(e[key], key, lo, hi)
    for key in ("shading", "geometry"):
        a = _array(p[key].get("radial_coefficients", [0, 0]), key, 1, minimum=-5, maximum=5)
        if a.shape != (2,):
            raise ValueError(f"{key} requires two radial coefficients")
    if p["geometry"].get("mode", "known_ideal_warp") not in {
        "known_ideal_warp",
        "multiview_planar",
    }:
        raise ValueError("Unsupported geometry model")
    if p["geometry"].get("mode") == "multiview_planar":
        k = _array(p["geometry"]["camera_matrix_px"], "camera_matrix_px", 2)
        if k.shape != (3, 3) or min(k[0, 0], k[1, 1]) <= 0:
            raise ValueError("Invalid camera intrinsic matrix")
        _array(p["geometry"]["distortion_coefficients"], "distortion", 1)
    b = p["blur"]
    field_domain = b.get("field_coordinate_domain", "sensor_normalized")
    if field_domain not in FIELD_COORDINATE_DOMAINS:
        raise ValueError("Unsupported blur field_coordinate_domain")
    if (
        field_domain == "reference_lens_object_direction"
        and b.get("field_application") != "explicit_nodes_only"
    ):
        raise ValueError(
            "Reference-model blur requires explicit nodes; extrapolation is unsupported"
        )
    effects = b.get("included_effects")
    supported_effects = effects == ["lens_blur", "pixel_aperture", "charge_diffusion"] or (
        effects == ["lens_blur", "pixel_aperture"]
        and b.get("charge_diffusion_assumption") == "zero_not_identified"
    )
    if (
        b.get("application") != "effective_system_replacement"
        or b.get("family") not in {"centered_elliptical_gaussian_v1", "field_effective_psf_v1"}
        or not supported_effects
    ):
        raise ValueError("Effective blur must replace the declared spatial effects exactly once")
    if not isinstance(b.get("field_nodes"), list) or not b["field_nodes"]:
        raise ValueError("Effective blur requires at least one explicit field node")
    fields = set()
    kernel_samples = 0
    for node in b["field_nodes"]:
        if node.get("field_coordinate_domain", field_domain) != field_domain:
            raise ValueError("PSF node coordinate domain differs from its profile")
        family = node.get("family", "centered_elliptical_gaussian_v1")
        if family not in {"centered_elliptical_gaussian_v1", "sampled_native_kernel_v1"}:
            raise ValueError("Unsupported effective PSF field family")
        if family == "sampled_native_kernel_v1":
            if b["family"] != "field_effective_psf_v1":
                raise ValueError("Sampled kernels require the explicit field effective PSF family")
            kernel_samples += _sampled_kernel(node).size
            if kernel_samples > MAX_PROFILE_KERNEL_SAMPLES:
                raise ValueError(f"Profile exceeds {MAX_PROFILE_KERNEL_SAMPLES} sampled PSF values")
        elif "kernel" in node:
            raise ValueError("Gaussian PSF nodes cannot carry a hidden sampled kernel")
        field = _array(node["field_xy"], "field_xy", 1, minimum=-1, maximum=1)
        if field.shape != (2,) or tuple(field) in fields:
            raise ValueError("Effective blur fields must be unique x,y positions")
        fields.add(tuple(field))
        for key in ("sigma_x_px", "sigma_y_px"):
            _number(node[key], key, 0.03, 8)
        _number(node["theta_deg"], "theta_deg", -180, 180)
    matrix = _array(p["isp"]["color_matrix"], "color_matrix", 2, minimum=-8, maximum=8)
    if matrix.shape != (3, 3):
        raise ValueError("Color matrix must be 3x3")
    tx = _array(p["isp"]["tone_input"], "tone_input", 1, minimum=0, maximum=1)
    ty = _array(p["isp"]["tone_output"], "tone_output", 1, minimum=0, maximum=1)
    if (
        tx.shape != ty.shape
        or not 2 <= len(tx) <= 256
        or tx[0] != 0
        or tx[-1] != 1
        or np.any(np.diff(tx) <= 0)
        or np.any(np.diff(ty) < 0)
    ):
        raise ValueError("Tone LUT requires increasing input [0,1] and monotone output")
    raw_matrix = _array(
        p["raw_response"]["color_matrix"], "raw color matrix", 2, minimum=-8, maximum=8
    )
    rx = _array(p["raw_response"]["tone_input"], "raw tone input", 1, minimum=0, maximum=1)
    ry = _array(p["raw_response"]["tone_output"], "raw tone output", 1, minimum=0, maximum=8)
    if (
        raw_matrix.shape != (3, 3)
        or rx.shape != ry.shape
        or rx[0] != 0
        or rx[-1] != 1
        or np.any(np.diff(rx) <= 0)
        or np.any(np.diff(ry) < 0)
    ):
        raise ValueError("RAW response requires 3x3 matrix and monotone mean-photocharge LUT")
    conditions = p["conditions"]
    _number(conditions["gain"], "gain", 1e-3, 1000)
    _number(conditions["temperature_c"], "temperature_c", -100, 150)
    return p


def _records(measurements, profile):
    measurement_resource_budget(measurements)
    result = deepcopy(measurements)
    identities = set()
    for index, record in enumerate(result):
        if record.get("chart") not in CHARTS or not isinstance(record.get("observations"), dict):
            raise ValueError("Unsupported chart or missing observations")
        record.setdefault("id", f"measurement-{index:03d}")
        if not isinstance(record["id"], str) or record["id"] in identities:
            raise ValueError("Measurement record identifiers must be unique strings")
        identities.add(record["id"])
        if record["chart"] in {"mtf", "texture"}:
            field_domain = _field_domain(profile, record["observations"])
            if _is_raw(record) and field_domain != "sensor_normalized":
                raise ValueError(
                    "Native RAW field coordinates are sensor_normalized; "
                    "a physical sensor-to-reference-lens mapping is not established"
                )
        if record["chart"] == "geometry":
            native = record["observations"].get("native_shape", profile["native_shape"])
            if native != profile["native_shape"]:
                raise ValueError("Geometry observations.native_shape differs from the profile")
        if record["chart"] == "mtf":
            domain = record.get(
                "measurement_domain",
                "camera_raw" if record.get("source_kind") != "measured" else None,
            )
            if domain != "camera_raw":
                raise ValueError(
                    "Effective camera blur requires camera_raw MTF; "
                    "optical/ISP domains need another adapter"
                )
        if record["chart"] in {"color", "tone"}:
            if (
                record.get("source_kind") == "measured"
                and record.get("measurement_domain") not in {"camera_raw", "camera_isp"}
                and not _is_raw(record)
            ):
                raise ValueError(
                    "Measured color/tone requires camera_raw or camera_isp measurement_domain"
                )
            if _raw_response_domain(record) and "raw_format" not in record:
                raise ValueError(
                    "RAW color/tone tables require raw_format for fixed code normalization"
                )
        if record.get("role") not in {"training", "development", "final_validation"}:
            raise ValueError("Measurement role must be training/development/final_validation")
        c = record.get("conditions", {})
        for key in ("mode_id", "gain", "temperature_c", "aperture_f_number", "focus_distance_m"):
            if key not in c or c[key] != profile["conditions"][key]:
                raise ValueError(
                    f"Measurement {record['id']} differs from fixed profile condition {key}"
                )
        if record["chart"] in {"mtf", "shading", "color", "tone", "texture"}:
            if c.get("light_id") != profile["conditions"]["light_id"]:
                raise ValueError("Optical/color profile is conditional on its measured illuminant")
    return result


def _pair(obs, xname, yname, *, ndim=1):
    x, y = _array(obs[xname], xname, ndim), _array(obs[yname], yname, ndim)
    if x.shape != y.shape or len(x) > MAX_POINTS:
        raise ValueError(f"{xname}/{yname} need equal bounded shapes")
    return x, y


def _field_domain(profile, obs):
    expected = profile["blur"].get("field_coordinate_domain", "sensor_normalized")
    observed = obs.get("field_coordinate_domain", "sensor_normalized")
    if expected not in FIELD_COORDINATE_DOMAINS or observed != expected:
        raise ValueError(
            "Observation field_coordinate_domain must explicitly match the blur profile; "
            "reference-lens coordinates are not physical sensor coordinates"
        )
    return expected


def _field_node(profile, obs, *, require_exact=True):
    domain = _field_domain(profile, obs)
    xy = np.asarray(obs.get("field_xy", [0, 0]), float)
    nodes = profile["blur"]["field_nodes"]
    for node in nodes:
        if np.array_equal(xy, node["field_xy"]):
            return node
    # A single authored baseline is explicitly spatially invariant. Fitted
    # multi-field profiles never extrapolate or silently choose a nearest node.
    if (
        len(nodes) == 1
        and nodes[0]["field_xy"] == [0, 0]
        and profile["blur"].get("field_application") == "authored_spatially_invariant"
    ):
        return nodes[0]
    if require_exact or domain == "reference_lens_object_direction":
        raise ValueError(
            "No effective PSF at this field; unmeasured field interpolation is unsupported"
        )
    return nodes[0]


def _sampled_kernel(node):
    """A native source-center to pixel-integrated receiver impulse, applied once."""
    if (
        node.get("kernel_pitch_px") != 1.0
        or node.get("sampling") != "native_cell_center_pixel_integrated"
    ):
        raise ValueError("Sampled PSF requires explicit native pixel-integrated sampling")
    kernel = np.asarray(node.get("kernel"), dtype=float)
    if (
        kernel.ndim != 2
        or kernel.shape[0] != kernel.shape[1]
        or not 1 <= kernel.shape[0] <= 257
        or kernel.shape[0] % 2 != 1
        or not np.isfinite(kernel).all()
        or np.any(kernel < 0)
        or not np.isclose(kernel.sum(), 1.0, rtol=0, atol=1e-10)
    ):
        raise ValueError("Sampled PSF requires a normalized finite nonnegative odd square <=257")
    return kernel


def _mtf(node, frequency, angle):
    if node.get("family") == "sampled_native_kernel_v1":
        kernel = _sampled_kernel(node)
        coordinate = np.arange(kernel.shape[0]) - kernel.shape[0] // 2
        frequency = np.asarray(frequency, float)
        direction = np.radians(float(angle))
        ex = np.exp(-2j * np.pi * frequency[:, None] * coordinate * np.cos(direction))
        ey = np.exp(-2j * np.pi * frequency[:, None] * coordinate * np.sin(direction))
        return np.abs(np.sum((ey @ kernel) * ex, axis=1))
    phi = np.radians(float(angle) - node["theta_deg"])
    variance = node["sigma_x_px"] ** 2 * np.cos(phi) ** 2
    variance += node["sigma_y_px"] ** 2 * np.sin(phi) ** 2
    return np.exp(-2 * np.pi**2 * variance * np.asarray(frequency) ** 2)


def _analytic_mtf_initial(curves, fallback):
    """Initialize an ellipse through covariance, avoiding periodic angle bounds.

    The log transform is used only for a deterministic starting point. Final
    fitting still minimizes the original MTF residual, without log weighting.
    RAW observer curves never use this analytic initialization.
    """
    design, response = [], []
    for frequency, mtf, angle, _record in curves:
        valid = (frequency > 0) & (mtf > 1e-10) & (mtf <= 1)
        if not np.any(valid):
            continue
        a = np.radians(angle)
        direction = np.array([np.cos(a) ** 2, 2 * np.cos(a) * np.sin(a), np.sin(a) ** 2])
        design.append(2 * np.pi**2 * frequency[valid, None] ** 2 * direction)
        response.append(-np.log(mtf[valid]))
    if not design:
        return fallback
    values, _, rank, _ = np.linalg.lstsq(np.vstack(design), np.concatenate(response), rcond=None)
    if rank != 3 or not np.isfinite(values).all():
        return fallback
    covariance = np.array([[values[0], values[1]], [values[1], values[2]]])
    eigenvalues, vectors = np.linalg.eigh(covariance)
    if np.min(eigenvalues) <= 0:
        return fallback
    sigmas = np.clip(np.sqrt(eigenvalues), 0.030001, 7.999999)
    theta = np.degrees(np.arctan2(vectors[1, 0], vectors[0, 0]))
    return [float(sigmas[0]), float(sigmas[1]), float((theta + 90) % 180 - 90)]


def _warp(points, coefficients):
    r2 = np.sum(points * points, axis=1)
    return points * (1 + coefficients[0] * r2 + coefficients[1] * r2**2)[:, None]


def _pava(values):
    blocks = []
    for i, value in enumerate(values):
        blocks.append([float(value), 1, i, i + 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b, a = blocks.pop(), blocks.pop()
            n = a[1] + b[1]
            blocks.append([(a[0] * a[1] + b[0] * b[1]) / n, n, a[2], b[3]])
    result = np.empty(len(values))
    for mean, _, start, end in blocks:
        result[start:end] = mean
    return result


def _is_raw(record):
    return record.get("quality", {}).get("method", "").startswith("native_bayer_")


def _raw_response_domain(record):
    return record.get("measurement_domain") == "camera_raw" or _is_raw(record)


def _raw_scale(profile, record):
    e = profile["electronics"]
    fmt = record["raw_format"]
    denominator = 2 ** fmt["bit_depth"] - 1 - fmt["black_level_dn"]
    exposure = float(record["conditions"].get("exposure_s", 0.01))
    scale = profile["reference_white_e"] * e["dn_per_e"] / denominator
    offset = (
        e["black_level_dn"]
        - fmt["black_level_dn"]
        + e["dark_current_e_per_s"] * exposure * e["dn_per_e"]
    ) / denominator
    return scale, offset


def _source_response(image, profile):
    r = profile["raw_response"]
    levels = np.interp(image, r["tone_input"], r["tone_output"])
    return np.maximum(levels @ np.asarray(r["color_matrix"]), 0)


def _shading_basis(record, profile):
    """Actual CFA ROI moments, never nominal field labels substituted for pixels."""
    full = record["geometry"]["full_sensor_shape"]
    origin = record["geometry"]["crop_origin"]
    channel = record.get("analysis", {}).get("channel", "G1")
    phase = {"R": (0, 0), "G1": (0, 1), "G2": (1, 0), "B": (1, 1)}[channel]
    values = []
    norm2 = ((full[0] - 1) / 2) ** 2 + ((full[1] - 1) / 2) ** 2
    for item in record["analysis"]["rois"]:
        row, col, h, w = item.get("box") if isinstance(item, dict) else item
        rr, cc = np.indices((h, w))
        rr, cc = rr + row + origin[0], cc + col + origin[1]
        mask = (rr % 2 == phase[0]) & (cc % 2 == phase[1])
        r2 = ((rr[mask] - (full[0] - 1) / 2) ** 2 + (cc[mask] - (full[1] - 1) / 2) ** 2) / norm2
        values.append([float(r2.mean()), float((r2**2).mean())])
    return np.asarray(values)


def _fit_geometry(profile, rows):
    modes = {r["observations"].get("mode", "known_ideal_warp") for r in rows}
    if len(modes) != 1:
        raise ValueError("Do not pool ideal-warp and multiview geometry observations")
    if modes == {"known_ideal_warp"}:
        pairs = [_pair(r["observations"], "object_xy", "image_xy", ndim=2) for r in rows]
        x, y = np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
        if x.shape[1] != 2 or len(x) < 6:
            raise ValueError("Known ideal warp requires at least six 2D correspondences")
        r2 = np.sum(x * x, axis=1)
        design = np.stack([x * r2[:, None], x * r2[:, None] ** 2], axis=-1).reshape(-1, 2)
        if np.linalg.matrix_rank(design) != 2 or np.linalg.cond(design) > 1e6:
            raise ValueError("Geometry radii cannot identify both distortion terms")
        fit = lsq_linear(design, (y - x).reshape(-1), bounds=(-1, 1))
        profile["geometry"] = {
            "mode": "known_ideal_warp",
            "radial_coefficients": fit.x.tolist(),
            "meaning": "conditional ideal-image warp; camera intrinsics not identified",
        }
        return {
            "chart": "geometry",
            "status": "fitted",
            "method": "bounded_radial_warp_v1",
            "rank": 2,
            "condition_number": float(np.linalg.cond(design)),
            "camera_intrinsics_identified": False,
        }
    import cv2

    groups = {}
    for record in rows:
        obs = record["observations"]
        x, y = _pair(obs, "object_xy", "image_xy", ndim=2)
        ids = obs.get("view_ids", [])
        if len(ids) != len(x) or x.shape[1] != 2:
            raise ValueError("Planar geometry requires one view_id per 2D correspondence")
        for identity in dict.fromkeys(ids):
            mask = np.asarray(ids) == identity
            if identity in groups:
                raise ValueError("Each multiview pose must belong to one observation record")
            points = x[mask]
            if len(points) < 6 or np.linalg.matrix_rank(points - points.mean(axis=0)) < 2:
                raise ValueError("Each planar pose needs at least six noncollinear points")
            groups[str(identity)] = (
                np.c_[points, np.zeros(len(points))].astype(np.float32),
                y[mask].astype(np.float32),
            )
    if len(groups) < 3:
        raise ValueError("Intrinsics fitting requires at least three distinct planar poses")
    h, w = profile["native_shape"]
    flags = cv2.CALIB_FIX_K3
    error, k, distortion, rvecs, tvecs = cv2.calibrateCamera(
        [v[0] for v in groups.values()],
        [v[1] for v in groups.values()],
        (w, h),
        None,
        None,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-10),
    )
    if not np.isfinite(k).all() or not np.isfinite(distortion).all() or error > max(h, w) / 10:
        raise ValueError("Multiview calibration failed the finite reprojection check")
    normals = np.array([cv2.Rodrigues(v)[0][:, 2] for v in rvecs])
    if np.max(np.linalg.norm(normals - normals[0], axis=1)) < 0.03:
        raise ValueError("Planar poses lack independent tilt diversity for intrinsics")
    profile["geometry"] = {
        "mode": "multiview_planar",
        "radial_coefficients": distortion.ravel()[:2].tolist(),
        "camera_matrix_px": k.tolist(),
        "distortion_coefficients": distortion.ravel().tolist(),
        "view_extrinsics": {
            key: {"rvec": rv.ravel().tolist(), "tvec": tv.ravel().tolist()}
            for key, rv, tv in zip(groups, rvecs, tvecs, strict=True)
        },
        "object_unit": "mm",
        "image_unit": "native_pixel",
        "meaning": "conditional planar target calibration; physical validation not established",
    }
    return {
        "chart": "geometry",
        "status": "fitted",
        "method": "opencv_planar_calibration_v1",
        "reprojection_rmse_px": float(error),
        "view_count": len(groups),
        "camera_intrinsics_identified": "conditional_on_target_geometry_and_pose_diversity",
    }


def _changes(before, after, prefix=""):
    result = []
    for key, value in after.items():
        name = f"{prefix}.{key}" if prefix else key
        old = before.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            result.extend(_changes(old, value, name))
        elif value != old:
            unit = (
                "DN/e"
                if key == "dn_per_e"
                else "e/s"
                if key == "dark_current_e_per_s"
                else "e"
                if key.endswith("_e")
                else ("DN" if key.endswith("_dn") else "pixel" if key.endswith("_px") else "1")
            )
            result.append({"parameter": name, "before": old, "after": value, "unit": unit})
    return result


def fit_profile(base_profile, measurements, *, max_iterations=100, allow_sfr_overshoot=False):
    """Fit development parameters from training data only; never select on holdout."""
    if type(max_iterations) is not int or not 10 <= max_iterations <= 200:
        raise ValueError("max_iterations must be an integer in [10,200]")
    if type(allow_sfr_overshoot) is not bool:
        raise ValueError("allow_sfr_overshoot must be an explicit boolean")
    base = validate_profile(base_profile)
    records = _records(measurements, base)
    training = [
        r
        for r in records
        if r["role"] == "training"
        and not r.get("quality", {}).get("not_fit_reasons")
        and not r.get("missing_observations")
    ]
    if not training:
        raise ValueError("At least one training measurement is required")
    p, diagnostics = (
        deepcopy(base),
        [
            {
                "chart": r["chart"],
                "measurement_id": r["id"],
                "status": "excluded",
                "reason": r.get("quality", {}).get("not_fit_reasons")
                or r.get("missing_observations"),
            }
            for r in records
            if r["role"] == "training" and r not in training
        ],
    )
    groups = {c: [r for r in training if r["chart"] == c] for c in CHARTS}
    e = p["electronics"]
    dark_slope_dn = None
    dark = [r for r in groups["dark"] if {"exposure_s", "mean_dn"} <= r["observations"].keys()]
    if dark:
        pairs = [_pair(r["observations"], "exposure_s", "mean_dn") for r in dark]
        x, y = np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
        if len(np.unique(x)) < 2 or np.any(x < 0):
            raise ValueError("Dark fitting needs at least two distinct nonnegative exposures")
        fit = lsq_linear(np.c_[np.ones(len(x)), x], y, bounds=(0, np.inf))
        e["black_level_dn"], dark_slope_dn = map(float, fit.x)
        diagnostics.append(
            {
                "chart": "dark",
                "status": "fitted",
                "method": "dark_mean_vs_exposure_v1",
                "dark_slope_dn_per_s": dark_slope_dn,
                "spatial_dsnu": "not_identified_from_aggregate_means",
            }
        )
    ptc = [r for r in groups["ptc"] if {"mean_dn", "variance_dn2"} <= r["observations"].keys()]
    if ptc:
        xs, ys = [], []
        for record in ptc:
            x, y = _pair(record["observations"], "mean_dn", "variance_dn2")
            clipped = np.asarray(
                record["observations"].get("clipped_fraction", np.zeros(len(x))), float
            )
            if clipped.shape != x.shape or not np.isfinite(clipped).all() or np.any(clipped < 0):
                raise ValueError("PTC clipping diagnostics must match every observation")
            if np.any(clipped > 0):
                raise ValueError("Clipped PTC points cannot be fitted as a linear noise response")
            xs.extend(x - e["black_level_dn"])
            ys.extend(y)
        x, y = np.asarray(xs), np.asarray(ys)
        if len(np.unique(x)) < 3 or np.min(x) < 0 or np.min(y) < 0 or np.ptp(x) < 1:
            raise ValueError("PTC needs three distinct positive unsaturated linear signal levels")
        fit = lsq_linear(np.c_[x, np.ones(len(x))], y, bounds=([1e-5, 0], [1000, np.inf]))
        e["dn_per_e"] = float(fit.x[0])
        analog_variance_dn2 = max(0.0, float(fit.x[1]) - 1 / 12)
        e["read_noise_e"] = float(np.sqrt(analog_variance_dn2) / fit.x[0])
        diagnostics.append(
            {
                "chart": "ptc",
                "status": "fitted",
                "method": "linear_temporal_ptc_v1",
                "conversion_gain_v_per_e": "not_identified",
                "aggregate_intercept_dn2": float(fit.x[1]),
                "quantization_variance_assumption_dn2": 1 / 12,
                "analog_variance_floor_applied": bool(fit.x[1] < 1 / 12),
                "quantization_scope": "uniform one-DN rounding approximation, "
                "not exact low-noise ADC inversion",
                "full_well_e": "unchanged; ADC saturation does not identify physical capacity",
                "conditional_input": "declared mean signal; no independent radiometric anchor",
            }
        )
    if dark_slope_dn is not None:
        e["dark_current_e_per_s"] = dark_slope_dn / e["dn_per_e"]
        if not ptc:
            noise = []
            for record in dark:
                obs = record["observations"]
                if "variance_dn2" in obs:
                    variance = _array(obs["variance_dn2"], "variance_dn2", 1, minimum=0)
                    exposure = np.asarray(obs["exposure_s"])
                    if variance.shape != exposure.shape:
                        raise ValueError("Dark variance and exposures must match")
                    noise.extend(variance - e["dn_per_e"] * dark_slope_dn * exposure - 1 / 12)
            if noise:
                e["read_noise_e"] = float(np.sqrt(max(0, np.mean(noise))) / e["dn_per_e"])
    if groups["geometry"]:
        diagnostics.append(_fit_geometry(p, groups["geometry"]))
    if groups["shading"]:
        pairs = [
            _pair(r["observations"], "field_radius", "relative_response") for r in groups["shading"]
        ]
        x, y = np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
        designs, targets = [], []
        for row, (radius, response) in zip(groups["shading"], pairs, strict=True):
            if _is_raw(row):
                basis = _shading_basis(row, p)
                if len(basis) != len(response):
                    raise ValueError("Shading ROIs and observations differ")
                designs.append(basis - response[:, None] * basis[0])
            else:
                designs.append(np.c_[radius**2, radius**4])
            targets.append(response - 1)
        design = np.concatenate(designs)
        if np.any(x < 0) or np.any(x > 1.5) or np.any(y <= 0) or np.linalg.matrix_rank(design) < 2:
            raise ValueError(
                "Shading needs positive normalized responses at two distinct nonzero radii"
            )
        fit = lsq_linear(design, np.concatenate(targets), bounds=(-2, 2))
        grid = np.linspace(0, 1.5, 200)
        if np.min(1 + fit.x[0] * grid**2 + fit.x[1] * grid**4) <= 0:
            raise ValueError("Fitted shading becomes nonpositive inside the supported field")
        p["shading"]["radial_coefficients"] = fit.x.tolist()
        diagnostics.append(
            {
                "chart": "shading",
                "status": "fitted",
                "method": "effective_radial_flat_v1",
                "lens_vs_sensor": "not_separated",
                "prnu": "not_identified",
            }
        )
    if groups["mtf"]:
        fields = {}
        for record in groups["mtf"]:
            obs = record["observations"]
            x, y = _pair(obs, "frequency_cy_px", "mtf")
            if (
                x[0] != 0
                or np.any(np.diff(x) <= 0)
                or x[-1] > 0.5
                or np.any(y < 0)
                or (not allow_sfr_overshoot and np.any(y > 1))
            ):
                raise ValueError(
                    "MTF requires ordered 0..0.5 native cycles/pixel, DC=1 and finite nonnegative values; "
                    "values above 1 require explicit native-SFR forward fitting"
                )
            if not np.isclose(y[0], 1, atol=1e-8):
                raise ValueError("MTF must be explicitly normalized at DC")
            # Grouping keys are later emitted in profiles, diagnostics and
            # parameter changes; retain native JSON numbers, not numpy scalars.
            field = tuple(
                _array(obs.get("field_xy", [0, 0]), "field_xy", 1, minimum=-1, maximum=1).tolist()
            )
            if len(field) != 2:
                raise ValueError("MTF field_xy requires x,y")
            fields.setdefault(field, []).append(
                (x, y, float(obs.get("orientation_deg", 0)), record)
            )
        nodes = []
        for field, curves in fields.items():
            directions = {round(angle % 180, 6) for _, _, angle, _ in curves}
            initial = _field_node(
                base,
                {
                    "field_xy": list(field),
                    "field_coordinate_domain": base["blur"].get(
                        "field_coordinate_domain", "sensor_normalized"
                    ),
                },
                require_exact=False,
            )
            angles = np.radians(sorted(directions))
            direction_design = np.column_stack(
                [np.cos(angles) ** 2, 2 * np.cos(angles) * np.sin(angles), np.sin(angles) ** 2]
            )
            direction_rank = int(np.linalg.matrix_rank(direction_design))
            direction_condition = (
                float(np.linalg.cond(direction_design)) if direction_rank == 3 else None
            )
            # Merely counting directions is insufficient when they are nearly
            # collinear. A single edge only constrains its normal projection;
            # isotropy is then a declared model assumption, not a recovered PSF.
            isotropic = direction_rank < 3 or direction_condition > 1e6

            def residual(values, *, isotropic=isotropic, curves=curves):
                node = {
                    "sigma_x_px": values[0],
                    "sigma_y_px": values[0] if isotropic else values[1],
                    "theta_deg": 0 if isotropic else values[2],
                }
                differences = []
                for x, y, angle, record in curves:
                    if record.get("quality", {}).get("method") == "native_bayer_mtf_v1":
                        forward = _observed_mtf_forward(p, record, node)
                        predicted = np.interp(x, forward["frequency_cy_px"], forward["mtf"])
                    else:
                        predicted = _mtf(node, x, angle)
                    differences.append(predicted - y)
                return np.concatenate(differences)

            initial_values = (
                [initial["sigma_x_px"]]
                if isotropic
                else [
                    initial["sigma_x_px"],
                    initial["sigma_y_px"],
                    ((initial["theta_deg"] + 90) % 180) - 90,
                ]
            )
            raw_objective = any(_is_raw(row) for _, _, _, row in curves)
            if not isotropic and not raw_objective:
                initial_values = _analytic_mtf_initial(curves, initial_values)
            if isotropic and raw_objective:
                optimum = minimize_scalar(
                    lambda sigma: float(np.sum(residual([sigma]) ** 2)),
                    bounds=(0.03, 8),
                    method="bounded",
                    options={"maxiter": max_iterations, "xatol": 0.002},
                )
                fit = OptimizeResult(
                    x=np.array([optimum.x]), fun=residual([optimum.x]), success=optimum.success
                )
            else:
                fit = least_squares(
                    residual,
                    initial_values,
                    bounds=([0.03], [8]) if isotropic else ([0.03, 0.03, -90], [8, 8, 90]),
                    max_nfev=max_iterations,
                    diff_step=0.03 if raw_objective else None,
                )
            node = {
                "field_xy": list(field),
                "sigma_x_px": float(fit.x[0]),
                "sigma_y_px": float(fit.x[0] if isotropic else fit.x[1]),
                "theta_deg": float(0 if isotropic else fit.x[2]),
                "training_measurement_ids": [r["id"] for _, _, _, r in curves],
                "measured_directions_deg": sorted({angle % 180 for _, _, angle, _ in curves}),
                "directional_identification": "single_projection_isotropy_assumed"
                if len(directions) == 1
                else "isotropy_assumed_insufficient_direction_information"
                if isotropic
                else "conditional_zero_phase_gaussian_covariance",
            }
            if "field_coordinate_domain" in base["blur"]:
                node["field_coordinate_domain"] = base["blur"]["field_coordinate_domain"]
            if base["blur"]["family"] == "field_effective_psf_v1":
                node.update(
                    family="centered_elliptical_gaussian_v1",
                    source_kind="measured_fit"
                    if all(r.get("source_kind") == "measured" for _, _, _, r in curves)
                    else "implementation_fit",
                    included_effects=["lens_blur", "pixel_aperture", "charge_diffusion"],
                    charge_diffusion_assumption="effective_not_separated",
                )
            nodes.append(node)
            # Reuse the established radial inverse for an independent diagnostic.
            from pyisetcam.mtf_psf import measured_mtf_to_psf

            # An observed camera SFR may exceed unity due to noise/processing.
            # Preserve those observations in the objective. The independent
            # nonnegative radial-PSF inverse has a stricter passive-MTF domain;
            # do not clip the measured curve merely to force that diagnostic.
            inverse = None
            if not np.any(curves[0][1] > 1):
                inverse = measured_mtf_to_psf(
                    curves[0][0],
                    curves[0][1],
                    frequency_unit="cycles/pixel",
                    measurement_pixel_pitch_um=p["pixel_pitch_um"],
                    sample_pitch_um=p["pixel_pitch_um"] / 4,
                    kernel_size=129,
                )
            diagnostics.append(
                {
                    "chart": "mtf",
                    "status": "fitted" if fit.success else "not_converged",
                    "method": "effective_gaussian_full_curve_fit_v1",
                    "field_xy": list(field),
                    "field_coordinate_domain": base["blur"].get(
                        "field_coordinate_domain", "sensor_normalized"
                    ),
                    "symmetry_assumption": "isotropic" if isotropic else "elliptical_zero_phase",
                    "direction_count": len(directions),
                    "measurement_ids": [r["id"] for _, _, _, r in curves],
                    "orientation_deg": [angle for _, _, angle, _ in curves],
                    "direction_design_rank": direction_rank,
                    "direction_design_condition": direction_condition,
                    "directional_identification": node["directional_identification"],
                    "radial_inverse_source_measurement_id": curves[0][3]["id"],
                    "rmse": float(np.sqrt(np.mean(fit.fun**2))),
                    "radial_inverse_roundtrip_passed": bool(inverse["roundtrip"]["passed"]) if inverse else None,
                    "radial_inverse_status": "computed" if inverse else "unavailable_observed_sfr_exceeds_passive_mtf_domain",
                    "observed_sfr_max": float(max(np.max(y) for _, y, _, _ in curves)),
                    "observed_sfr_overshoot_samples": int(sum(np.count_nonzero(y > 1) for _, y, _, _ in curves)),
                    "observed_sfr_clipped": False,
                    "allow_sfr_overshoot": allow_sfr_overshoot,
                    "radial_inverse_is_not_the_fitted_kernel": True,
                    "fit_observer": "shared_native_bayer_mtf_v1"
                    if any(
                        r.get("quality", {}).get("method") == "native_bayer_mtf_v1"
                        for _, _, _, r in curves
                    )
                    else "external_curve_conditional_kernel_transfer",
                    "physical_psf_recovered": False,
                }
            )
        measured_fields = {tuple(node["field_xy"]) for node in nodes}
        inherited = [
            deepcopy(node)
            for node in base["blur"]["field_nodes"]
            if tuple(node["field_xy"]) not in measured_fields
        ]
        p["blur"]["field_nodes"] = nodes + inherited
        p["blur"]["field_application"] = "explicit_nodes_only"
        if base["blur"]["family"] == "field_effective_psf_v1":
            p["blur"].update(
                included_effects=["lens_blur", "pixel_aperture", "charge_diffusion"],
                charge_diffusion_assumption="per_node_effective_not_separated",
                effect_declaration_scope="per_field_node",
                source_kind="fitted_with_inherited_initialization" if inherited else "fitted",
                initialization_only=False,
                phase="per_field_node",
                spectral_scope="conditioned_on_campaign_illuminant",
            )
    raw_tones = [r for r in groups["tone"] if _raw_response_domain(r)]
    if raw_tones:
        xs, ys = [], []
        for row in raw_tones:
            x, y = _pair(row["observations"], "input_level", "output_level")
            scale, offset = _raw_scale(p, row)
            channel = {"R": 0, "G1": 1, "G2": 1, "B": 2}[
                row.get("analysis", {}).get("channel", "G1")
            ]
            neutral_gain = float(np.sum(np.asarray(p["raw_response"]["color_matrix"])[:, channel]))
            if neutral_gain <= 0:
                raise ValueError("Raw neutral channel gain must be positive")
            xs.extend(x)
            ys.extend((y - offset) / scale / neutral_gain)
        x, y = np.asarray(xs), np.asarray(ys)
        unique = np.unique(x)
        if unique[0] != 0 or unique[-1] != 1 or len(unique) < 3 or np.any(y < -0.02):
            raise ValueError(
                "RAW tone response needs known zero/white input endpoints and nonnegative signal"
            )
        fitted = _pava([float(np.mean(y[x == v])) for v in unique])
        p["raw_response"].update(
            tone_input=unique.tolist(), tone_output=np.maximum(fitted, 0).tolist()
        )
        diagnostics.append(
            {
                "chart": "tone",
                "status": "fitted",
                "method": "raw_mean_photoresponse_v1",
                "scope": "conditional mean signal before shot noise; "
                "not a physical electronic transfer identification",
            }
        )
    for raw_domain in (False, True):
        color_rows = [r for r in groups["color"] if _raw_response_domain(r) == raw_domain]
        if not color_rows:
            continue
        pairs = []
        for row in color_rows:
            x, y = _pair(row["observations"], "reference_rgb", "measured_rgb", ndim=2)
            if raw_domain:
                scale, offset = _raw_scale(p, row)
                x = np.interp(x, p["raw_response"]["tone_input"], p["raw_response"]["tone_output"])
                y = (y - offset) / scale
            pairs.append((x, y))
        x, y = np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
        if (
            x.shape[1] != 3
            or len(x) < 4
            or np.linalg.matrix_rank(x) != 3
            or np.linalg.cond(x) > 1e6
        ):
            raise ValueError("Color fitting requires independent linear RGB patches (rank three)")
        matrix = np.column_stack([lsq_linear(x, y[:, c], bounds=(-8, 8)).x for c in range(3)])
        p["raw_response" if raw_domain else "isp"]["color_matrix"] = matrix.tolist()
        diagnostics.append(
            {
                "chart": "color",
                "status": "fitted",
                "method": "effective_linear_rgb_matrix_v1",
                "direction": "reference chart RGB to mean pre-CFA photocharge"
                if raw_domain
                else "reference chart RGB to measured camera linear RGB",
                "response_domain": "raw_mean_photocharge" if raw_domain else "linear_isp_output",
                "white_balance_vs_ccm_vs_qe": "not_separately_identified",
            }
        )
    output_tones = [r for r in groups["tone"] if not _raw_response_domain(r)]
    if output_tones:
        pairs = [_pair(r["observations"], "input_level", "output_level") for r in output_tones]
        x, y = np.concatenate([a for a, _ in pairs]), np.concatenate([b for _, b in pairs])
        if np.any(x < 0) or np.any(x > 1) or np.any(y < 0) or np.any(y > 1):
            raise ValueError("Tone values must be linearized normalized levels in [0,1]")
        unique = np.unique(x)
        if len(unique) < 3 or unique[0] != 0 or unique[-1] != 1:
            raise ValueError("Tone fitting requires endpoints 0 and 1 and an interior level")
        means = [float(np.mean(y[x == value])) for value in unique]
        p["isp"]["tone_input"], p["isp"]["tone_output"] = unique.tolist(), _pava(means).tolist()
        diagnostics.append(
            {
                "chart": "tone",
                "status": "fitted",
                "method": "monotone_piecewise_linear_v1",
                "response_domain": "declared chart levels; no hidden per-image normalization",
            }
        )
    diagnostics.extend(
        {
            "chart": c,
            "status": "validation_only" if c == "texture" else "not_fitted",
            "reason": "texture does not identify an arbitrary denoiser"
            if c == "texture"
            else "no complete training observations",
        }
        for c in CHARTS
        if not any(d["chart"] == c for d in diagnostics)
    )
    p = validate_profile(p)
    if raw_tones and any(_raw_response_domain(row) for row in groups["color"]):
        residuals = []
        for row in raw_tones + [r for r in groups["color"] if _raw_response_domain(r)]:
            forward = predict_observation(p, row)
            error = np.asarray(forward["prediction"]) - np.asarray(forward["measured"])
            residuals.append(
                {
                    "measurement_id": row["id"],
                    "chart": row["chart"],
                    "rmse": float(np.sqrt(np.mean(error**2))),
                    "max_abs": float(np.max(np.abs(error))),
                }
            )
        diagnostics.append(
            {
                "chart": "color",
                "status": "conditional_fit",
                "method": "coupled_raw_color_tone_residual_v1",
                "scope": "tone then color are conditional sequential fits; "
                "the matrix can change neutral tone gain. Final combined residuals "
                "must be assessed; independent physical components are not identified.",
                "final_combined_residuals": residuals,
            }
        )
    return {
        "profile": p,
        "diagnostics": diagnostics,
        "changed_parameters": _changes(base, p),
        "training_measurement_ids": [r["id"] for r in training],
        "excluded_evaluation_measurement_ids": [
            r["id"] for r in records if r["role"] != "training"
        ],
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "candidate_status": "unapproved",
        "resource_budget": measurement_resource_budget(records),
        "field_coverage": {
            "fitted_field_count": sum(d["chart"] == "mtf" and "field_xy" in d for d in diagnostics),
            "profile_field_count": len(p["blur"]["field_nodes"]),
            "inherited_field_count": len(p["blur"]["field_nodes"])
            - sum(d["chart"] == "mtf" and "field_xy" in d for d in diagnostics),
            "interpolation": "not_established; explicit measured positions only",
        },
    }


def _geometry_prediction(profile, obs):
    x, y = _pair(obs, "object_xy", "image_xy", ndim=2)
    model = profile["geometry"]
    if obs.get("mode", "known_ideal_warp") == "known_ideal_warp":
        return _warp(
            x, model["radial_coefficients"]
        ), "known ideal-image coordinates; not K calibration"
    import cv2

    h, w = profile["native_shape"]
    k = np.asarray(
        model.get("camera_matrix_px", [[w, 0, (w - 1) / 2], [0, w, (h - 1) / 2], [0, 0, 1]]), float
    )
    d = np.asarray(model.get("distortion_coefficients", [0, 0, 0, 0, 0]), float)
    ids = np.asarray(obs["view_ids"])
    result, estimated = np.empty_like(y), []
    for identity in dict.fromkeys(ids.tolist()):
        mask = ids == identity
        points = np.c_[x[mask], np.zeros(mask.sum())]
        pose = obs.get("view_extrinsics", {}).get(str(identity))
        if pose is None:
            pose = model.get("view_extrinsics", {}).get(str(identity))
        if pose is None:
            ok, r, t = cv2.solvePnP(points, y[mask], k, d)
            if not ok:
                raise ValueError(
                    "Cannot establish a conditional target pose for this geometry view"
                )
            estimated.append(str(identity))
        else:
            r, t = np.asarray(pose["rvec"], float), np.asarray(pose["tvec"], float)
        result[mask] = cv2.projectPoints(points, r, t, k, d)[0].reshape(-1, 2)
    return result, (
        "pose estimated from evaluation correspondences; conditional reprojection only"
        if estimated
        else "fixed supplied or training target pose"
    )


def predict_observation(profile, record):
    """Deterministic forward metric prediction, independent of fitting routines."""
    p = validate_profile(profile)
    chart, obs, e = record["chart"], record["observations"], p["electronics"]
    scope = "conditional forward response; physical model uncertainty unknown"
    if chart == "dark":
        x = _array(obs["exposure_s"], "exposure_s", 1, minimum=0)
        measured = _array(obs["mean_dn"], "mean_dn", 1)
        prediction = e["black_level_dn"] + x * e["dark_current_e_per_s"] * e["dn_per_e"]
        unit, metric, axis_unit = "DN", "dark.mean_dn", "s"
    elif chart == "ptc":
        x, measured = _pair(obs, "mean_dn", "variance_dn2")
        prediction = e["dn_per_e"] * np.maximum(x - e["black_level_dn"], 0)
        prediction += (e["read_noise_e"] * e["dn_per_e"]) ** 2
        prediction += 1 / 12
        unit, metric, axis_unit = "DN^2", "ptc.temporal_variance", "DN"
        scope = "variance conditional on declared mean DN; not an independent radiance prediction"
    elif chart == "shading":
        x, measured = _pair(obs, "field_radius", "relative_response")
        a, b = p["shading"]["radial_coefficients"]
        if _is_raw(record):
            basis = _shading_basis(record, p)
            signal = 1 + basis @ np.array([a, b])
            prediction = signal / signal[0]
        else:
            prediction = 1 + a * x**2 + b * x**4
        unit, metric, axis_unit = "1", "flat.relative_response", "normalized_radius"
    elif chart in {"mtf", "texture"}:
        x, measured = _pair(obs, "frequency_cy_px", "mtf" if chart == "mtf" else "response")
        node = _field_node(p, obs)
        prediction = _mtf(node, x, obs.get("orientation_deg", 0))
        unit, metric, axis_unit = "1", f"{chart}.contrast_transfer", "cycles/native_pixel"
        scope = (
            "retained native sampled kernel transfer; model-derived, not measured camera SFR"
            if node.get("family") == "sampled_native_kernel_v1"
            else "effective Gaussian kernel transfer; "
            "not a unique optical PSF or ISP SFR reconstruction"
        )
    elif chart == "geometry":
        points, measured = _pair(obs, "object_xy", "image_xy", ndim=2)
        prediction, scope = _geometry_prediction(p, obs)
        x = np.arange(measured.size)
        unit = (
            "native_pixel"
            if obs.get("mode") == "multiview_planar"
            else "normalized_sensor_coordinate"
        )
        metric, axis_unit = "geometry.image_coordinates", "coordinate_index"
    elif chart == "color":
        reference, measured = _pair(obs, "reference_rgb", "measured_rgb", ndim=2)
        if _raw_response_domain(record):
            scale, offset = _raw_scale(p, record)
            prediction = _source_response(reference, p) * scale + offset
        else:
            prediction = reference @ np.asarray(p["isp"]["color_matrix"])
        x, unit, metric, axis_unit = (
            np.arange(measured.size),
            "linear_rgb",
            "color.linear_rgb",
            "patch_channel",
        )
    elif chart == "tone":
        x, measured = _pair(obs, "input_level", "output_level")
        if _raw_response_domain(record):
            scale, offset = _raw_scale(p, record)
            channel = {"R": 0, "G1": 1, "G2": 1, "B": 2}[
                record.get("analysis", {}).get("channel", "G1")
            ]
            prediction = (
                _source_response(np.repeat(x[:, None], 3, axis=1), p)[:, channel] * scale + offset
            )
        else:
            prediction = np.interp(x, p["isp"]["tone_input"], p["isp"]["tone_output"])
        unit, metric, axis_unit = "1", "tone.output_level", "linear_input"
    else:
        raise ValueError("Unsupported chart")
    if np.shape(prediction) != np.shape(measured):
        raise ValueError("Forward metric and measured values have different shapes")
    return {
        "x": np.asarray(x).reshape(-1).tolist(),
        "measured": measured.reshape(-1).tolist(),
        "prediction": prediction.reshape(-1).tolist(),
        "unit": unit,
        "axis_unit": axis_unit,
        "metric_id": metric,
        "scope": scope,
        "value_shape": list(measured.shape),
    }


def _electrical_model(profile):
    e, gain = profile["electronics"], profile["conditions"]["gain"]
    white = 2 ** e["bit_depth"] - 1
    model = electrical.default_electrical_model(
        {
            "full_well_e": e["full_well_e"],
            "read_noise_e": e["read_noise_e"],
            "dark_current_e_per_s": e["dark_current_e_per_s"],
            "bit_depth": e["bit_depth"],
            "black_level_dn": e["black_level_dn"],
            "gain": gain,
        }
    )
    model["conversion_gain_v_per_e"] = e["dn_per_e"] / white / gain
    model["adc"]["full_scale_v"] = 1.0
    model["reference_temperature_c"] = profile["conditions"]["temperature_c"]
    return electrical.validate_electrical_model(model)


def _crop(profile, record):
    full, shape = profile["native_shape"], profile["render_shape"]
    shape = list(shape)
    origin = record.get(
        "render_crop_origin",
        record.get("geometry", {}).get(
            "crop_origin", [(a - b) // 2 for a, b in zip(full, shape, strict=True)]
        ),
    )
    original_shape = record.get("quality", {}).get("shape", [])
    provenance = record.get("provenance", {})
    external_esfr_roi = (
        provenance.get("native_roi")
        if record["chart"] == "mtf"
        and provenance.get("import_classification")
        == "external_camera_raw_curve_not_legacy_image_operator"
        else None
    )
    if external_esfr_roi is not None:
        # Existing immutable chart exports predate analysis.rois. Preserve their
        # source ROI extent and global CFA phase rather than generating a
        # 128-pixel central probe at every different measured field position.
        box = external_esfr_roi
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(type(v) is not int for v in box)
            or min(box[:2]) < 0
            or min(box[2:]) < 16
            or max(box[2:]) > MAX_RENDER_SIDE
            or any(v % 2 for v in box[2:])
        ):
            raise ValueError("Exported E-SFR ROI requires a bounded even native crop")
        original = record.get("geometry", {}).get("crop_origin", [0, 0])
        origin = [original[0] + box[0], original[1] + box[1]]
        shape = list(box[2:])
    if (
        _is_raw(record)
        and original_shape
        and max(original_shape[-2:]) > MAX_RENDER_SIDE
        and record["chart"] in {"color", "tone", "shading", "geometry"}
    ):
        raise ValueError(
            "This RAW chart requires an explicitly supplied native crop of at most "
            "256 pixels per side with rebased ROIs; automatic multi-ROI cropping is unsupported"
        )
    if external_esfr_roi is not None:
        pass
    elif original_shape and max(original_shape[-2:]) <= MAX_RENDER_SIDE:
        shape = list(original_shape[-2:])
    elif record["chart"] == "mtf" and record.get("analysis", {}).get("rois"):
        roi = record["analysis"]["rois"][0]
        box = roi.get("box") if isinstance(roi, dict) else roi
        if max(box[2:]) <= MAX_RENDER_SIDE and not any(v % 2 for v in box[2:]):
            origin = [origin[0] + box[0], origin[1] + box[1]]
            shape = list(box[2:])
    if len(origin) != 2 or any(type(x) is not int or x < 0 for x in origin):
        raise ValueError("render_crop_origin requires nonnegative native row,column integers")
    if any(o + s > n for o, s, n in zip(origin, shape, full, strict=True)):
        raise ValueError("Requested chart probe lies outside the native sensor")
    return {"full_sensor_shape": list(full), "crop_origin": list(origin)}, tuple(shape)


def _capture_electrical(profile, model, geometry, charge, exposure, seed, *, noise_enabled=True):
    # v1 has spatially homogeneous electronics (PRNU/DSNU are not fitted).
    # Its output law is position independent, so a local array is exactly the
    # same distribution without allocating full-native random maps for a crop.
    return electrical.capture_electrons(
        charge,
        exposure,
        model,
        analog_gain=profile["conditions"]["gain"],
        temperature_c=profile["conditions"]["temperature_c"],
        geometry={"full_sensor_shape": list(charge.shape), "crop_origin": [0, 0]},
        noise_enabled=noise_enabled,
        seed=seed,
    )


def _observer_metadata(profile, record, geometry, shape):
    meta = deepcopy(record)
    meta["raw_format"] = {
        "cfa": "RGGB",
        "bit_depth": profile["electronics"]["bit_depth"],
        "black_level_dn": record.get("raw_format", {}).get(
            "black_level_dn", profile["electronics"]["black_level_dn"]
        ),
        "response_type": "linear",
    }
    original = record.get("geometry", {}).get("crop_origin", geometry["crop_origin"])
    meta["geometry"] = deepcopy(geometry)
    meta.setdefault("analysis", {})
    if record["chart"] == "dark":
        meta["analysis"]["exposure_s"] = record["observations"]["exposure_s"]
    if record["chart"] == "ptc":
        meta["analysis"].pop("exposure_s", None)
        meta["conditions"].pop("exposure_s", None)
    delta = np.asarray(geometry["crop_origin"]) - np.asarray(original)
    if np.any(delta) and record["chart"] == "mtf":
        meta["analysis"]["rois"] = [[0, 0, *shape]]
    return meta


def _shared_observation(profile, record, geometry, raw):
    from .calibration_lab_raw import analyze_npy

    stream = io.BytesIO()
    np.save(stream, raw.astype(np.int32), allow_pickle=False)
    meta = _observer_metadata(profile, record, geometry, raw.shape[-2:])
    observations, quality, _ = analyze_npy(meta, stream.getvalue())
    return observations, quality


def _observed_mtf_forward(profile, record, node):
    geometry, shape = _crop(profile, record)
    image, _ = _scene(profile, record, shape)
    planes, _ = _effective_blur(_source_response(image, profile), node)
    charge = planes[..., 1] * profile["reference_white_e"]
    raw = _capture_electrical(
        profile,
        _electrical_model(profile),
        geometry,
        charge,
        float(record["conditions"].get("exposure_s", 0.01)),
        0,
        noise_enabled=False,
    )["arrays"]["raw_dn"]
    observed, quality = _shared_observation(profile, record, geometry, raw)
    if not observed or quality["not_fit_reasons"]:
        raise ValueError("Fresh MTF chart cannot reproduce the declared RAW observation operator")
    return observed


def _effective_blur(image, node):
    """Apply either a native sampled impulse or a quarter-pixel Gaussian.

    The entire receiving aperture is already represented by this kernel. There
    is no area-average, second lens blur, diffusion or post-RAW noise smoothing.
    """
    from scipy.signal import fftconvolve

    factor = 4
    if node.get("family") == "sampled_native_kernel_v1":
        kernel = _sampled_kernel(node)
        native = image[2::factor, 2::factor]
        radius = kernel.shape[0] // 2
        if radius == 0:
            return np.maximum(0, native * kernel[0, 0]), kernel
        padded = np.pad(native, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
        # The source kernel already integrates the receiver aperture. Sampling
        # chart radiance at source cell centers reproduces its declared operator.
        blurred = np.stack(
            [fftconvolve(padded[..., c], kernel, mode="same") for c in range(3)], axis=-1
        )
        return np.maximum(0, blurred[radius:-radius, radius:-radius]), kernel
    sx, sy = node["sigma_x_px"], node["sigma_y_px"]
    radius = int(np.ceil(5 * max(sx, sy) * factor))
    coordinate = np.arange(-radius, radius + 1) / factor
    y, x = np.meshgrid(coordinate, coordinate, indexing="ij")
    a = np.radians(node["theta_deg"])
    u, v = x * np.cos(a) + y * np.sin(a), -x * np.sin(a) + y * np.cos(a)
    kernel = np.exp(-0.5 * ((u / sx) ** 2 + (v / sy) ** 2))
    kernel /= kernel.sum()
    padded = np.pad(image, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    blurred = np.stack(
        [fftconvolve(padded[..., c], kernel, mode="same") for c in range(3)], axis=-1
    )
    blurred = blurred[radius:-radius, radius:-radius]
    return np.maximum(0, blurred[2::factor, 2::factor]), kernel


def _scene(profile, record, shape):
    chart, obs = record["chart"], record["observations"]
    h, w = shape
    yy, xx = np.indices((h * 4, w * 4), dtype=float)
    x, y = xx / 4 / w, yy / 4 / h
    image = np.full((h * 4, w * 4, 3), 0.5)
    rois = []
    if chart in {"mtf", "texture"}:
        if chart == "mtf":
            quality = record.get("quality", {})
            slope = float(quality.get("edge_slope", 0.07))
            # Slanted-edge fit convention is x = intercept + slope*y.
            e = profile["electronics"]
            denominator = profile["reference_white_e"] * e["dn_per_e"]
            low = float(quality.get("edge_low_dn", 0.1 * denominator)) / denominator
            high = float(quality.get("edge_high_dn", 0.8 * denominator)) / denominator
            angle = np.radians(float(obs.get("orientation_deg", 0)))
            across = (x - 0.5) * w * np.cos(angle) + (y - 0.5) * h * np.sin(angle)
            along = (y - 0.5) * h * np.cos(angle) - (x - 0.5) * w * np.sin(angle)
            image[:] = (low + (high - low) * (across > slope * along))[..., None]
        else:
            image[:] = (0.45 + 0.3 * np.sin(2 * np.pi * 0.08 * xx / 4))[..., None]
    elif chart in {"color", "tone"}:
        colors = np.asarray(
            obs["reference_rgb"]
            if chart == "color"
            else np.repeat(np.asarray(obs["input_level"])[:, None], 3, axis=1)
        )
        cols = int(np.ceil(np.sqrt(len(colors))))
        rows = int(np.ceil(len(colors) / cols))
        image[:] = 0.18
        declared_rois = record.get("analysis", {}).get("rois", [])
        for i, color in enumerate(colors):
            r, c = divmod(i, cols)
            r0, r1 = round(r * h / rows), round((r + 1) * h / rows)
            c0, c1 = round(c * w / cols), round((c + 1) * w / cols)
            if len(declared_rois) == len(colors):
                item = declared_rois[i]
                r0, c0, rh, cw = item.get("box") if isinstance(item, dict) else item
                r1, c1 = r0 + rh, c0 + cw
                # Chart reference declares uniform patch interiors. Supply a
                # known uniform halo around the observation ROI, not black zero padding.
                pad = 8
                image[
                    max(0, r0 - pad) * 4 : min(h, r1 + pad) * 4,
                    max(0, c0 - pad) * 4 : min(w, c1 + pad) * 4,
                ] = color
            else:
                image[r0 * 4 : r1 * 4, c0 * 4 : c1 * 4] = color
            margin = max(2, min(r1 - r0, c1 - c0) // 5)
            rois.append([r0 + margin, r1 - margin, c0 + margin, c1 - margin])
    elif chart == "shading":
        geometry, _ = _crop(profile, record)
        full, origin = geometry["full_sensor_shape"], geometry["crop_origin"]
        native_x = xx / 4 - 0.5 + origin[1] - (full[1] - 1) / 2
        native_y = yy / 4 - 0.5 + origin[0] - (full[0] - 1) / 2
        radius = np.hypot(native_x, native_y) / np.hypot((full[0] - 1) / 2, (full[1] - 1) / 2)
        a, b = profile["shading"]["radial_coefficients"]
        image[:] = (0.5 * (1 + a * radius**2 + b * radius**4))[..., None]
    elif chart == "geometry":
        image[:] = 0.05
        predicted, _ = _geometry_prediction(profile, obs)
        points = predicted
        geometry, _ = _crop(profile, record)
        origin, full = geometry["crop_origin"], geometry["full_sensor_shape"]
        if obs.get("mode") == "multiview_planar":
            mask = np.asarray(obs["view_ids"]) == obs["view_ids"][0]
            points = points[mask]
            native_points = points
        else:
            native_points = (points + 1) * np.array([full[1] - 1, full[0] - 1]) / 2
        for cx, cy in native_points:
            image[
                (xx / 4 - 0.5 + origin[1] - cx) ** 2 + (yy / 4 - 0.5 + origin[0] - cy) ** 2 < 2.5**2
            ] = 0.8
    return image, rois


def _demosaic(raw, profile, geometry):
    from scipy.ndimage import convolve

    e = profile["electronics"]
    mosaic = (
        (raw.astype(float) - e["black_level_dn"]) / e["dn_per_e"] / profile["reference_white_e"]
    )
    rr, cc = np.indices(raw.shape)
    rr, cc = (rr + geometry["crop_origin"][0]) % 2, (cc + geometry["crop_origin"][1]) % 2
    masks = [(rr == 0) & (cc == 0), rr != cc, (rr == 1) & (cc == 1)]
    kernel = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], float)
    rgb = np.stack(
        [
            convolve(mosaic * mask, kernel, mode="mirror")
            / np.maximum(convolve(mask.astype(float), kernel, mode="mirror"), 1e-12)
            for mask in masks
        ],
        axis=-1,
    )
    linear = rgb @ np.asarray(profile["isp"]["color_matrix"])
    display = np.interp(
        np.clip(linear, 0, 1), profile["isp"]["tone_input"], profile["isp"]["tone_output"]
    )
    return linear, display


def _artifact(path, root, kind):
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": kind,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


def _render(profile, record, root, variant, index, seed):
    from PIL import Image

    geometry, shape = _crop(profile, record)
    directory = root / f"{index:03d}-{record['chart']}" / variant
    directory.mkdir(parents=True)
    chart, obs, e = record["chart"], record["observations"], profile["electronics"]
    model = _electrical_model(profile)
    exposure = float(record["conditions"].get("exposure_s", 0.01))
    observed = {}
    if chart in {"dark", "ptc"}:
        levels = np.asarray(obs["exposure_s"] if chart == "dark" else obs["mean_dn"], float)
        if len(levels) > 64:
            raise ValueError(
                "Repeated RAW chart capture supports at most 64 exposure/signal levels"
            )
        frames, means, variances = [], [], []
        for level_index, level in enumerate(levels):
            t = float(level) if chart == "dark" else exposure
            charge = (
                0.0
                if chart == "dark"
                else max(
                    0, (level - e["black_level_dn"]) / e["dn_per_e"] - e["dark_current_e_per_s"] * t
                )
            )
            if charge >= e["full_well_e"]:
                raise ValueError("PTC reproduction would clip the declared full well")
            stack = []
            for repeat in range(REPEATS):
                out = _capture_electrical(
                    profile,
                    model,
                    geometry,
                    np.full(shape, charge),
                    t,
                    seed + level_index * REPEATS + repeat,
                )
                stack.append(out["arrays"]["raw_dn"])
            stack = np.stack(stack)
            frames.append(stack)
            means.append(float(stack.mean()))
            variances.append(float(stack.astype(float).var(axis=0, ddof=1).mean()))
        raw = np.stack(frames)
        observed.update(
            mean_dn=means,
            variance_dn2=variances,
            quantization_note="integer RAW temporal variance includes actual ADC rounding",
        )
        first = raw[-1, 0]
    else:
        scene, rois = _scene(profile, record, shape)
        # Non-spatial chart metrics do not declare a field position. Their
        # illustrative probe explicitly uses the retained center, with this
        # choice recorded below; it does not relabel sensor geometry as lens FOV.
        field_obs = (
            obs
            if chart in {"mtf", "texture"}
            else {
                "field_xy": [0.0, 0.0],
                "field_coordinate_domain": profile["blur"].get(
                    "field_coordinate_domain", "sensor_normalized"
                ),
            }
        )
        node = _field_node(profile, field_obs, require_exact=chart in {"mtf", "texture"})
        planes, kernel = _effective_blur(_source_response(scene, profile), node)
        np.save(directory / "effective-kernel.npy", kernel)
        rr, cc = np.indices(shape)
        channel = np.where(
            (rr + geometry["crop_origin"][0]) % 2 == 0,
            np.where((cc + geometry["crop_origin"][1]) % 2 == 0, 0, 1),
            np.where((cc + geometry["crop_origin"][1]) % 2 == 0, 1, 2),
        )
        charge = (
            np.take_along_axis(planes, channel[..., None], axis=2)[..., 0]
            * profile["reference_white_e"]
        )
        raw = np.stack(
            [
                _capture_electrical(profile, model, geometry, charge, exposure, seed + r)["arrays"][
                    "raw_dn"
                ]
                for r in range(REPEATS)
            ]
        )
        first = raw[0]
        mean_linear, mean_display = _demosaic(raw.astype(float).mean(axis=0), profile, geometry)
        if rois:
            observed["patch_linear_rgb"] = [
                mean_linear[r0:r1, c0:c1].mean(axis=(0, 1)).tolist() for r0, r1, c0, c1 in rois
            ]
            observed["patch_output_rgb"] = [
                mean_display[r0:r1, c0:c1].mean(axis=(0, 1)).tolist() for r0, r1, c0, c1 in rois
            ]
        if chart == "mtf":
            # Actual unprocessed Gr sites, with the native CFA phase retained.
            dr = (-geometry["crop_origin"][0]) % 2
            dc = (1 - geometry["crop_origin"][1]) % 2
            mono = raw.astype(float).mean(axis=0)[dr::2, dc::2]
            sfr = virtual_lab_metrics.slanted_edge_metrics(mono)
            if sfr["status"] == "ok":
                sfr["frequency"]["cycles_per_native_pixel"] = (
                    np.asarray(sfr["frequency"]["cycles_per_pixel"]) / 2
                ).tolist()
            observed["raw_gr_sfr"] = sfr
            observed["sfr_scope"] = (
                "fresh Gr-sublattice edge measurement, 0..0.25 cycles/native pixel; "
                "separate from kernel transfer"
            )
        if chart == "texture":
            observed["temporal_noise"] = virtual_lab_metrics.temporal_noise_metrics(
                raw, black_level=e["black_level_dn"]
            )
            observed["contrast_std_linear"] = float(mean_linear[8:-8, 8:-8].std())
        if chart == "shading":
            observed["fresh_raw_relative_response"] = (raw.mean(axis=0) - e["black_level_dn"])[
                ::8, ::8
            ].tolist()
            observed["fresh_raw_relative_response_unit"] = (
                "DN above black; displayed without per-image normalization"
            )
        if chart == "geometry":
            observed["rendered_fiducial_prediction"], observed["pose_scope"] = _geometry_prediction(
                profile, obs
            )
            observed["rendered_fiducial_prediction"] = observed[
                "rendered_fiducial_prediction"
            ].tolist()
    raw_path = directory / "raw.npy"
    np.save(raw_path, raw)
    if _is_raw(record) and chart in {"dark", "ptc", "mtf", "shading", "geometry", "color", "tone"}:
        same, quality = _shared_observation(profile, record, geometry, raw)
        observed["shared_observation"] = same
        observed["shared_observation_quality"] = quality
        observed["shared_observer"] = "camera_platform.calibration_lab_raw.analyze_npy"
    linear, display = _demosaic(first, profile, geometry)
    linear_path, image_path = directory / "linear-rgb.npy", directory / "isp.png"
    np.save(linear_path, linear.astype(np.float32))
    Image.fromarray(np.rint(np.clip(display, 0, 1) * 255).astype(np.uint8)).save(image_path)
    return {
        "measurement_id": record["id"],
        "chart": chart,
        "variant": variant,
        "raw": raw_path.relative_to(root).as_posix(),
        "isp": image_path.relative_to(root).as_posix(),
        "linear_rgb": linear_path.relative_to(root).as_posix(),
        "geometry": geometry,
        "shape": list(shape),
        "repeat_count": REPEATS,
        "seed": seed,
        "source": "fresh modeled chart probe; not a pixel-aligned replay of the measured image",
        "blur_field": None
        if chart in {"dark", "ptc"}
        else {
            "field_xy": node["field_xy"],
            "field_coordinate_domain": profile["blur"].get(
                "field_coordinate_domain", "sensor_normalized"
            ),
            "source_field_id": node.get("source_field_id"),
            "selection": "declared_observation" if chart in {"mtf", "texture"} else "center_probe",
        },
        "blur_application": "effective spatial response once, native point sampling, "
        "noise after blur",
        "blur_kernel_sampling": "not_applied_uniform_charge"
        if chart in {"dark", "ptc"}
        else (
            "native_cell_center_pixel_integrated"
            if node.get("family") == "sampled_native_kernel_v1"
            else "quarter_native_pixel_gaussian_then_point_sampling"
        ),
        "electrical_voltage_scale": "normalized 1 V ADC convention; physical V/e not identified",
        "electrical_spatial_sampling": "position-independent electronics on a bounded "
        "native crop; no resize",
        "observed_metrics": observed,
        "artifacts": [
            _artifact(path, root, kind)
            for path, kind in (
                (raw_path, "raw"),
                (linear_path, "linear_rgb"),
                (image_path, "image"),
            )
        ],
    }


def run_comparison(
    base_profile, candidate_profile, measurements, output_dir, *, seed=42, on_progress=None
):
    """Freeze profiles, execute fresh RAW/ISP probes and compare all stored curves."""
    base, candidate = validate_profile(base_profile), validate_profile(candidate_profile)
    if type(seed) is not int or not 0 <= seed <= 2**31 - 65536:
        raise ValueError("seed must be an integer in the admitted deterministic range")
    records = _records(measurements, base)
    _records(records, candidate)
    for field in (
        "native_shape",
        "pixel_pitch_um",
        "cfa",
        "render_shape",
        "conditions",
        "reference_white_e",
    ):
        if base[field] != candidate[field]:
            raise ValueError(f"Baseline/candidate changed the frozen capture field {field}")
    root = Path(output_dir)
    if root.exists() and any(path.name != "progress.json" for path in root.iterdir()):
        raise ValueError("Comparison output directory must be new or empty")
    samples = 0
    for record in records:
        _, shape = _crop(base, record)
        levels = (
            len(record["observations"].get("exposure_s", []))
            if record["chart"] == "dark"
            else (len(record["observations"].get("mean_dn", [])) if record["chart"] == "ptc" else 1)
        )
        if record["chart"] in {"dark", "ptc"} and levels > 32:
            raise ValueError("Repeated RAW reproduction admits at most 32 levels (256 frames)")
        samples += 2 * int(np.prod(shape)) * REPEATS * levels
    if samples > MAX_RAW_SAMPLES:
        raise ValueError("Chart comparison exceeds 32000000 retained RAW samples; split the study")
    root.mkdir(parents=True, exist_ok=True)
    comparisons, images, issues = [], [], []
    for i, record in enumerate(records):
        if on_progress:
            on_progress(
                {
                    "stage": f"{record['chart']} · fresh baseline/candidate RAW",
                    "fraction": i / len(records),
                }
            )
        try:
            b, c = predict_observation(base, record), predict_observation(candidate, record)
        except KeyError as exc:
            issues.append(
                {
                    "measurement_id": record["id"],
                    "chart": record["chart"],
                    "code": "INCOMPLETE_OBSERVATION",
                    "message": f"Missing metric data: {exc}",
                }
            )
            continue
        measured, bv, cv = map(np.asarray, (b["measured"], b["prediction"], c["prediction"]))
        rb, rc = bv - measured, cv - measured
        comparison = {
            "measurement_id": record["id"],
            "chart": record["chart"],
            "metric_id": b["metric_id"],
            "role": record["role"],
            "source_kind": record.get("source_kind"),
            "axes": {"x": b["x"], "unit": b["axis_unit"]},
            "unit": b["unit"],
            "value_shape": b["value_shape"],
            "measured": measured.tolist(),
            "baseline": bv.tolist(),
            "candidate": cv.tolist(),
            "residuals": {
                "baseline": rb.tolist(),
                "candidate": rc.tolist(),
                "baseline_rmse": float(np.sqrt(np.mean(rb**2))),
                "candidate_rmse": float(np.sqrt(np.mean(rc**2))),
                "baseline_max_abs": float(np.max(np.abs(rb))),
                "candidate_max_abs": float(np.max(np.abs(rc))),
            },
            "status": "compared",
            "scope": c["scope"],
            "comparison_basis": "frozen conditional forward metric; "
            "fresh RAW metrics are separately recorded",
            "operator_parity": "external_observer_not_available",
            "physical_validation": "not_established",
        }
        if record["chart"] in {"mtf", "texture"}:
            comparison["field_xy"] = record["observations"].get("field_xy", [0.0, 0.0])
            comparison["field_coordinate_domain"] = base["blur"].get(
                "field_coordinate_domain", "sensor_normalized"
            )
            comparison["orientation_deg"] = record["observations"].get("orientation_deg", 0.0)
        local_images = []
        for variant, profile in (("baseline", base), ("candidate", candidate)):
            item = _render(profile, record, root, variant, i, seed + i * 1024)
            images.append(item)
            local_images.append(item)
        shared = [item["observed_metrics"].get("shared_observation") for item in local_images]
        if all(shared):
            key = {
                "dark": "mean_dn",
                "ptc": "variance_dn2",
                "mtf": "mtf",
                "shading": "relative_response",
                "geometry": "image_xy",
                "color": "measured_rgb",
                "tone": "output_level",
            }[record["chart"]]
            predicted = []
            for result in shared:
                values = np.asarray(result[key]).reshape(-1)
                if record["chart"] == "mtf":
                    f = np.asarray(result["frequency_cy_px"])
                    measured_f = np.asarray(b["x"])
                    if measured_f[0] < f[0] or measured_f[-1] > f[-1] + 1e-12:
                        raise ValueError(
                            "Fresh RAW observer does not cover measured MTF frequencies"
                        )
                    values = np.interp(measured_f, f, values)
                if values.shape != measured.shape:
                    raise ValueError("Shared RAW observer produced a different measurement shape")
                predicted.append(values)
            comparison["analytic_prediction"] = {"baseline": bv.tolist(), "candidate": cv.tolist()}
            comparison["baseline"], comparison["candidate"] = [v.tolist() for v in predicted]
            rb, rc = (v - measured for v in predicted)
            comparison["residuals"] = {
                "baseline": rb.tolist(),
                "candidate": rc.tolist(),
                "baseline_rmse": float(np.sqrt(np.mean(rb**2))),
                "candidate_rmse": float(np.sqrt(np.mean(rc**2))),
                "baseline_max_abs": float(np.max(np.abs(rb))),
                "candidate_max_abs": float(np.max(np.abs(rc))),
            }
            original_shape = record.get("quality", {}).get("shape", [])[-2:]
            exact = original_shape == local_images[0]["shape"] or record["chart"] == "mtf"
            comparison["operator_parity"] = (
                "same_native_raw_observer" if exact else "same_estimator_bounded_native_support"
            )
            comparison["comparison_basis"] = (
                "new electrical RAW captures remeasured by calibration_lab_raw.analyze_npy"
            )
            comparison["scope"] += (
                "; input target brightness is conditional on the supplied chart; "
                "physical uncertainty unknown"
            )
        comparisons.append(comparison)
    summary = {
        "schema": "camera_calibration_comparison_v1",
        "comparisons": comparisons,
        "images": images,
        "issues": issues,
        "baseline_profile": base,
        "candidate_profile": candidate,
        "seed": seed,
        "physical_validation": "not_established",
        "evidence_kind": "conditional_chart_reproduction",
        "uncertainty": {"kind": "unknown"},
        "resource_budget": {
            **measurement_resource_budget(records),
            "retained_raw_samples": samples,
            "retained_raw_samples_limit": MAX_RAW_SAMPLES,
            "render_execution": "sequential_native_crops",
            "max_render_side": MAX_RENDER_SIDE,
        },
        "limitations": [
            "Camera effective blur is an identifiable-family approximation, "
            "not unique physical PSF recovery.",
            "CSV metric agreement does not establish pixel-level measured image reproduction.",
            "Texture is validation-only; no arbitrary denoiser is fitted.",
            "Full well, separate spectral QE and physical conversion voltage "
            "need independent evidence.",
            "PTC analytic intercept subtracts a declared 1/12 DN^2 quantization approximation; "
            "low-noise ADC statistics remain model dependent.",
            "Chart reference_white_e is a fixed radiometric assumption, "
            "not a fitted absolute illumination anchor.",
        ],
    }
    (root / "comparison.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    if on_progress:
        on_progress({"stage": "comparison complete", "fraction": 1.0})
    return summary


def synthetic_fixture(*, seed=None):
    """Independent authored analytic chart values for lifecycle demonstrations."""
    generation_seed = uuid.uuid4().int if seed is None else seed
    if type(generation_seed) is not int or not 0 <= generation_seed < 2**128:
        raise ValueError("Synthetic fixture seed requires an integer in [0, 2**128)")
    rng = np.random.Generator(np.random.PCG64(generation_seed))
    baseline, truth = default_profile(), default_profile()
    truth["electronics"].update(
        black_level_dn=72.0, dn_per_e=0.55, read_noise_e=3.0, dark_current_e_per_s=8.0
    )
    truth["shading"]["radial_coefficients"] = [-0.25, 0.05]
    truth["geometry"]["radial_coefficients"] = [0.04, -0.012]
    truth["blur"]["field_nodes"][0].update(sigma_x_px=1.5, sigma_y_px=1.5)
    truth["isp"]["color_matrix"] = [[0.95, 0.025, 0], [0.03, 1.02, 0.01], [0, 0.01, 0.9]]
    truth["isp"].update(tone_input=[0, 0.25, 0.5, 0.75, 1], tone_output=[0, 0.3, 0.58, 0.81, 1])
    x = np.linspace(-0.8, 0.8, 5)
    y, xg = np.meshgrid(x, x)
    points = np.c_[xg.ravel(), y.ravel()]
    colors = np.array(
        [
            [0.7, 0.1, 0.1],
            [0.1, 0.7, 0.1],
            [0.1, 0.1, 0.7],
            [0.5, 0.5, 0.5],
            [0.8, 0.6, 0.2],
            [0.2, 0.5, 0.7],
        ]
    )
    times, signal = np.array([1e-6, 0.01, 0.1, 0.5, 1]), np.array([30, 100, 300, 700, 1200])
    frequencies, radius = np.linspace(0, 0.5, 33), np.linspace(0, 1, 9)
    observations = {
        "dark": {
            "exposure_s": times.tolist(),
            "mean_dn": (72 + times * 8 * 0.55).tolist(),
            "variance_dn2": ((3 * 0.55) ** 2 + times * 8 * 0.55**2 + 1 / 12).tolist(),
        },
        "ptc": {
            "mean_dn": (72 + signal).tolist(),
            "variance_dn2": (0.55 * signal + (3 * 0.55) ** 2 + 1 / 12).tolist(),
        },
        "shading": {
            "field_radius": radius.tolist(),
            "relative_response": (1 - 0.25 * radius**2 + 0.05 * radius**4).tolist(),
        },
        "mtf": {
            "frequency_cy_px": frequencies.tolist(),
            "mtf": np.exp(-2 * np.pi**2 * 1.5**2 * frequencies**2).tolist(),
            "field_xy": [0, 0],
            "orientation_deg": 0,
        },
        "geometry": {
            "mode": "known_ideal_warp",
            "object_xy": points.tolist(),
            "image_xy": (
                points
                * (1 + 0.04 * np.sum(points**2, axis=1) - 0.012 * np.sum(points**2, axis=1) ** 2)[
                    :, None
                ]
            ).tolist(),
        },
        "color": {
            "reference_rgb": colors.tolist(),
            "measured_rgb": (colors @ np.asarray(truth["isp"]["color_matrix"])).tolist(),
        },
        "tone": {
            "input_level": truth["isp"]["tone_input"],
            "output_level": truth["isp"]["tone_output"],
        },
        "texture": {
            "frequency_cy_px": frequencies.tolist(),
            "response": np.exp(-2 * np.pi**2 * 1.5**2 * frequencies**2).tolist(),
        },
    }
    measurements = []
    suffix = uuid.uuid4().hex[:12]
    for role in ("training", "final_validation"):
        for chart in CHARTS:
            identity = f"synthetic-{chart}-{role}-{suffix}"
            values = deepcopy(observations[chart])
            if role == "final_validation":
                if chart == "dark":
                    t = np.sort(np.exp(rng.uniform(np.log(0.001), np.log(1.5), 5)))
                    values = {
                        "exposure_s": t.tolist(),
                        "mean_dn": (72 + t * 8 * 0.55).tolist(),
                        "variance_dn2": ((3 * 0.55) ** 2 + t * 8 * 0.55**2 + 1 / 12).tolist(),
                    }
                elif chart == "ptc":
                    s = np.sort(rng.uniform(25, 1600, 5))
                    values = {
                        "mean_dn": (72 + s).tolist(),
                        "variance_dn2": (0.55 * s + (3 * 0.55) ** 2 + 1 / 12).tolist(),
                    }
                elif chart in {"mtf", "texture"}:
                    f = np.r_[0.0, np.sort(rng.uniform(0.005, 0.49, 28))]
                    values["frequency_cy_px"] = f.tolist()
                    values["mtf" if chart == "mtf" else "response"] = np.exp(
                        -2 * np.pi**2 * 1.5**2 * f**2
                    ).tolist()
                elif chart == "shading":
                    r = np.r_[0.0, np.sort(rng.uniform(0.07, 0.98, 7))]
                    values = {
                        "field_radius": r.tolist(),
                        "relative_response": (1 - 0.25 * r**2 + 0.05 * r**4).tolist(),
                    }
                elif chart == "geometry":
                    pts = points * rng.uniform(0.72, 0.85) + rng.uniform(-0.03, 0.03, points.shape)
                    r2 = np.sum(pts**2, axis=1)
                    values["object_xy"], values["image_xy"] = (
                        pts.tolist(),
                        (pts * (1 + 0.04 * r2 - 0.012 * r2**2)[:, None]).tolist(),
                    )
                elif chart == "color":
                    cs = rng.uniform(0.02, 0.8, colors.shape)
                    values = {
                        "reference_rgb": cs.tolist(),
                        "measured_rgb": (cs @ np.asarray(truth["isp"]["color_matrix"])).tolist(),
                    }
                elif chart == "tone":
                    t = np.r_[0.0, np.sort(rng.uniform(0.05, 0.95, 4)), 1.0]
                    values = {
                        "input_level": t.tolist(),
                        "output_level": np.interp(
                            t, [0, 0.25, 0.5, 0.75, 1], [0, 0.3, 0.58, 0.81, 1]
                        ).tolist(),
                    }
            measurements.append(
                {
                    "id": identity,
                    "campaign_id": "synthetic-demo",
                    "chart": chart,
                    "source_kind": "synthetic",
                    "measurement_domain": "camera_isp"
                    if chart in {"color", "tone"}
                    else "camera_raw",
                    "physical_instance": "synthetic-camera",
                    "session_id": f"synthetic-{role}-{suffix}",
                    "acquisition_id": identity,
                    "role": role,
                    "conditions": deepcopy(baseline["conditions"]),
                    "observations": values,
                    "uncertainty": {"kind": "unknown"},
                    "method": {"id": "synthetic_reference", "version": "1"},
                    "instrument": None,
                    "reference": {
                        "id": "synthetic-reference",
                        "source_uri": f"synthetic://chart/{chart}/{role}?seed={generation_seed}",
                        "method": "authored analytic chart; distinct heldout coordinates; "
                        f"PCG64 generation seed={generation_seed}, fixture version=2",
                    },
                }
            )
    campaign = {
        "name": "Synthetic all-chart calibration",
        "camera_id": "synthetic-camera",
        "native_shape": baseline["native_shape"],
        "pixel_pitch_um": baseline["pixel_pitch_um"],
        "conditions": deepcopy(baseline["conditions"]),
        "baseline_profile": baseline,
        "thresholds": {
            "dark": 1.0,
            "ptc": 10.0,
            "mtf": 0.05,
            "shading": 0.05,
            "geometry": 0.01,
            "color": 0.05,
            "tone": 0.05,
            "texture": 0.05,
        },
    }
    return {
        "baseline_profile": baseline,
        "truth_profile": truth,
        "profile": baseline,
        "campaign": campaign,
        "measurements": measurements,
        "source_kind": "synthetic",
        "generation_seed": generation_seed,
    }

"""Measured E-SFR study with frozen development-only effective blur fitting.

The candidate is a chart/condition-bound camera response model. It is neither a
lens prescription nor a reconstruction of asymmetric optical phase. Synthetic
edge replays use development-frozen geometry and levels, never holdout tuning.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import convolve, gaussian_filter
from scipy.optimize import minimize, minimize_scalar
from scipy.special import ndtr

from .esfr_analysis import edge_metrics
from .esfr_io import RawFrame, read_isp

CHANNELS = ("R", "Gr", "Gb", "B")
SCHEMA = "camera_esfr_effective_response_v1"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _labels(cfa):
    labels = list(cfa)
    if cfa not in {"RGGB", "BGGR", "GRBG", "GBRG"}:
        raise ValueError("Unsupported Bayer CFA")
    red_row = labels.index("R") // 2
    return [
        ("Gr" if i // 2 == red_row else "Gb") if label == "G" else label
        for i, label in enumerate(labels)
    ]


def bilinear_isp(raw, cfa, crop_origin, levels):
    """Fixed normalized bilinear demosaic; no fitted WB/CCM/tone/sharpening."""
    raw = np.asarray(raw, dtype=float)
    yy, xx = np.indices(raw.shape)
    site = ((yy + int(crop_origin[0])) % 2) * 2 + (xx + int(crop_origin[1])) % 2
    labels = _labels(cfa)
    normalized = np.zeros(raw.shape, dtype=float)
    masks = {}
    for i, channel in enumerate(labels):
        mask = site == i
        level = levels[channel]
        normalized[mask] = (raw[mask] - level["black_dn"]) / (level["white_dn"] - level["black_dn"])
        name = "G" if channel in {"Gr", "Gb"} else channel
        masks[name] = masks.get(name, np.zeros(raw.shape, dtype=bool)) | mask
    kernels = {
        "R": np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=float),
        "G": np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]], dtype=float),
    }
    kernels["B"] = kernels["R"]
    result = []
    for channel in ("R", "G", "B"):
        mask, kernel = masks[channel], kernels[channel]
        denominator = convolve(mask.astype(float), kernel, mode="mirror")
        result.append(
            convolve(normalized * mask, kernel, mode="mirror") / np.maximum(denominator, 1e-12)
        )
    return np.stack(result, axis=-1).astype(np.float32)


def simulate_edge(profile, roi_geometry, levels, shape, cfa="RGGB", crop_origin=(0, 0)):
    """Replay one frozen ideal edge through effective Gaussian, Bayer and ISP.

    ``normal_xy`` and ``offset_px`` define nx*x+ny*y-offset in original ROI
    coordinates. The analytical Gaussian/step convolution is sampled at native
    pixel centres, followed by declared DN quantization. No gamma, chart-MTF or
    optical pixel-aperture deconvolution is inferred. Two native halo pixels
    provide real neighbouring samples for bilinear ISP before cropping.
    """
    field = profile["fields"][roi_geometry["field_id"]]
    sx, sy = float(field["sigma_x_px"]), float(field["sigma_y_px"])
    theta = float(field.get("theta_deg", 0)) * np.pi / 180
    normal = np.asarray(roi_geometry["normal_xy"], dtype=float)
    if (
        normal.shape != (2,)
        or not np.isfinite(normal).all()
        or abs(np.linalg.norm(normal) - 1) > 1e-6
    ):
        raise ValueError("Edge normal must be a finite unit vector")
    if not np.isfinite([sx, sy, theta]).all() or min(sx, sy) <= 0:
        raise ValueError("Gaussian widths must be positive and finite")
    rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    covariance = rotation @ np.diag([sx * sx, sy * sy]) @ rotation.T
    sigma = float(np.sqrt(normal @ covariance @ normal))
    height, width = map(int, shape)
    if min(height, width) < 1 or height * width > 4_000_000:
        raise ValueError("Replay ROI dimensions exceed bounds")
    halo = 2
    yy, xx = np.indices((height + 2 * halo, width + 2 * halo), dtype=float)
    xx -= halo
    yy -= halo
    edge = ndtr((normal[0] * xx + normal[1] * yy - float(roi_geometry["offset_px"])) / sigma)
    site = ((yy.astype(int) + int(crop_origin[0])) % 2) * 2 + (
        (xx.astype(int) + int(crop_origin[1])) % 2
    )
    raw = np.zeros(edge.shape, dtype=np.uint16)
    for i, channel in enumerate(_labels(cfa)):
        level = levels[channel]
        if not np.isfinite(list(level.values())).all():
            raise ValueError("Replay levels must be finite")
        values = level["black_dn"] + level["low_dn"] + edge * (level["high_dn"] - level["low_dn"])
        raw[site == i] = np.rint(np.clip(values[site == i], 0, level["white_dn"])).astype(np.uint16)
    rgb = bilinear_isp(raw, cfa, (crop_origin[0] - halo, crop_origin[1] - halo), levels)
    raw, rgb = raw[halo:-halo, halo:-halo].copy(), rgb[halo:-halo, halo:-halo].copy()
    planes = {}
    for i, channel in enumerate(_labels(cfa)):
        sr, sc = divmod(i, 2)
        dr, dc = (sr - crop_origin[0]) % 2, (sc - crop_origin[1]) % 2
        planes[channel] = raw[dr::2, dc::2].astype(float) - levels[channel]["black_dn"]
    return {
        "raw": raw,
        "isp": rgb,
        "raw_planes": planes,
        "geometry": dict(roi_geometry),
        "method": "continuous_gaussian_step_native_bayer_bilinear_v1",
    }


def _unavailable(reason):
    return {"status": "unavailable", "reason": reason, "frequency_cy_native_px": [], "sfr": []}


def _stats(plane):
    return {
        "mean": float(np.mean(plane)),
        "spatial_std": float(np.std(plane)),
        "spatial_variance": float(np.var(plane)),
        "sample_count": int(plane.size),
        "interpretation": "spatial variation includes chart texture and fixed pattern; "
        "not read noise",
    }


def _metrics_isp(rgb):
    return {
        **{channel: edge_metrics(rgb[..., i]) for i, channel in enumerate(("R", "G", "B"))},
        "Y": edge_metrics(rgb @ np.array([0.2126, 0.7152, 0.0722])),
    }


def _lateral_ca(planes, sampling, raw_metrics):
    """Channel edge offsets in native coordinates along the measured Gr normal."""
    if raw_metrics["Gr"]["status"] != "ok":
        return {"status": "unavailable", "reason": "reference_gr_edge_unavailable"}
    angle = np.deg2rad(raw_metrics["Gr"]["edge_angle_deg"] + 90)
    nx, ny = float(np.cos(angle)), float(np.sin(angle))
    positions = {}
    for channel, plane in planes.items():
        if raw_metrics[channel]["status"] != "ok":
            continue
        gy, gx = np.gradient(gaussian_filter(plane, 1.0))
        weight = np.abs(gx * nx + gy * ny)
        weight[weight < weight.max() * 0.2] = 0
        yy, xx = np.indices(plane.shape, dtype=float)
        origin = sampling[channel]["native_origin"]
        position = nx * (2 * xx + origin[1]) + ny * (2 * yy + origin[0])
        positions[channel] = float(np.sum(position * weight) / np.sum(weight))
    reference = positions["Gr"]
    return {
        "status": "ok",
        "reference_channel": "Gr",
        "normal_xy": [nx, ny],
        "native_edge_positions_px": positions,
        "relative_edge_offset_native_px": {
            key: value - reference for key, value in positions.items()
        },
        "interpretation": "assembled-camera projected channel edge offsets; "
        "not lens-only chromatic aberration",
    }


def _geometry(roi, planes, sampling, metric, metadata):
    gr = planes["Gr"]
    tangent = np.deg2rad(metric["edge_angle_deg"])
    normal = np.array([-np.sin(tangent), np.cos(tangent)])
    row, col, height, width = roi["box"]
    full_origin = np.array(metadata["geometry"]["crop_origin"]) + [row, col]
    start = np.array(sampling["Gr"]["native_origin"]) - full_origin
    yy, xx = np.indices(gr.shape, dtype=float)
    xx, yy = 2 * xx + start[1], 2 * yy + start[0]
    blurred = gaussian_filter(gr, 1)
    gy, gx = np.gradient(blurred)
    weight = np.hypot(gx, gy)
    weight[weight < weight.max() * 0.2] = 0
    distance = normal[0] * xx + normal[1] * yy
    offset = float(np.sum(distance * weight) / np.sum(weight))
    side = distance - offset
    if np.mean(gr[side > 1]) < np.mean(gr[side < -1]):
        normal, offset = -normal, -offset
    levels = {}
    for channel, plane in planes.items():
        channel_start = np.array(sampling[channel]["native_origin"]) - full_origin
        yy, xx = np.indices(plane.shape, dtype=float)
        signed = (
            normal[0] * (2 * xx + channel_start[1])
            + normal[1] * (2 * yy + channel_start[0])
            - offset
        )
        low_region, high_region = (
            signed < -min(height, width) * 0.18,
            signed > min(height, width) * 0.18,
        )
        if not low_region.any() or not high_region.any():
            raise ValueError("Development ROI needs both edge plateaus")
        levels[channel] = {
            "low_dn": float(np.median(plane[low_region])),
            "high_dn": float(np.median(plane[high_region])),
            "black_dn": sampling[channel]["black_level_dn"],
            "white_dn": sampling[channel]["white_level_dn"],
        }
    return {
        "field_id": roi["field_id"],
        "normal_xy": normal.tolist(),
        "offset_px": offset,
        "box": roi["box"],
        "source_roi_id": roi["id"],
        "crop_origin": full_origin.tolist(),
    }, levels


def _curve_rmse(measured, simulated):
    if measured.get("status") != "ok" or simulated.get("status") != "ok":
        return None
    f = np.asarray(measured["frequency_cy_native_px"])
    sf = np.asarray(simulated["frequency_cy_native_px"])
    valid = (f >= max(0, sf[0])) & (f <= sf[-1])
    if np.count_nonzero(valid) < 4:
        return None
    difference = np.asarray(measured["sfr"])[valid] - np.interp(f[valid], sf, simulated["sfr"])
    return float(np.sqrt(np.mean(difference**2)))


def _field(sigma, orientations):
    return {
        "sigma_x_px": float(sigma),
        "sigma_y_px": float(sigma),
        "theta_deg": 0.0,
        "model": "isotropic_effective_gaussian",
        "orientation_support_deg": orientations,
        "scope": "chart_condition_bound_effective_camera_response",
        "phase_assumption": "centred_zero_phase",
        "directional_fit": "isotropic_shared_full_curves",
    }


def _ellipse(parameters, orientations):
    field = _field(float(np.exp(parameters[0])), orientations)
    field.update(
        sigma_y_px=float(np.exp(parameters[1])),
        theta_deg=float(parameters[2] % 180),
        model="elliptical_effective_gaussian",
        directional_fit="positive_definite_covariance_full_curves",
    )
    return field


def _save_images(out, capture_id, roi_id, stage, raw, isp, levels):
    folder = Path(out) / "images" / capture_id / roi_id
    folder.mkdir(parents=True, exist_ok=True)
    raw_path, isp_path = folder / f"{stage}-raw16.png", folder / f"{stage}-isp.png"
    Image.fromarray(np.asarray(raw, dtype=np.uint16)).save(raw_path)
    np.save(folder / f"{stage}-raw.npy", raw, allow_pickle=False)
    black = min(x["black_dn"] for x in levels.values())
    white = max(x["white_dn"] for x in levels.values())
    preview = np.clip((raw.astype(float) - black) / max(1, white - black), 0, 1) ** (1 / 2.2)
    Image.fromarray(np.uint8(preview * 255)).save(folder / f"{stage}-raw-preview.png")
    result = {
        "raw": (folder / f"{stage}-raw-preview.png").relative_to(out).as_posix(),
        "raw16": raw_path.relative_to(out).as_posix(),
        "raw_array": (folder / f"{stage}-raw.npy").relative_to(out).as_posix(),
        "preview_transfer": "clipped_fixed_1_over_2.2_display_only",
    }
    if isp is not None:
        np.save(folder / f"{stage}-isp-values.npy", isp.astype(np.float32), allow_pickle=False)
        Image.fromarray(np.uint8(np.clip(isp, 0, 1) ** (1 / 2.2) * 255)).save(isp_path)
        result.update(
            isp=isp_path.relative_to(out).as_posix(),
            isp_array=(folder / f"{stage}-isp-values.npy").relative_to(out).as_posix(),
        )
    return result


def validate_roi_budget(rois):
    """Bound patch accumulators and peak edge projection independently of count."""
    if sum(r["box"][2] * r["box"][3] for r in rois) > 8_000_000:
        raise ValueError("Total native ROI area exceeds 8 megapixels; split the study")
    if any(r["kind"] == "edge" and r["box"][2] * r["box"][3] > 262_144 for r in rois):
        raise ValueError("Each edge ROI must contain at most 262144 native pixels")


def run(job, captures, out, progress=lambda message: None):
    """Read development first, freeze fitted profile, then evaluate holdouts."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    spec = job["specification"]
    rois = spec["rois"]
    validate_roi_budget(rois)
    dev_ids, val_ids = set(job["development_ids"]), set(job["validation_ids"])
    by_id = {record["id"]: (record, raw_path, isp_path) for record, raw_path, isp_path in captures}
    if not dev_ids or dev_ids & val_ids or dev_ids | val_ids != set(by_id):
        raise ValueError("Capture partition must contain disjoint development and validation IDs")
    first = by_id[job["development_ids"][0]][0]["metadata"]
    from .esfr_measured import isp_is_diagnostic

    physical_isp_diagnostic = isp_is_diagnostic(first)
    observations, templates, raw_targets, temporal = [], {}, defaultdict(list), {}
    frozen = job.get("frozen_profile_snapshot")
    if frozen is not None:
        # The service pins this snapshot from an immutable activated profile.
        frozen = json.loads(_canonical(frozen))
        for key in ("camera_id", "conditions", "raw_format", "source_kind"):
            if frozen.get(key) != first.get(key):
                raise ValueError(f"Frozen profile capture context differs: {key}")
        for key in ("crop_origin", "full_sensor_shape"):
            if frozen["geometry"].get(key) != first["geometry"].get(key):
                raise ValueError(f"Frozen profile sensor geometry differs: {key}")
        templates = frozen["geometry"]["roi_nuisance"]
        for roi in rois:
            if roi["kind"] == "edge" and roi["id"] in templates:
                geometry = templates[roi["id"]]["geometry"]
                if geometry["box"] != roi["box"] or geometry["field_id"] != roi["field_id"]:
                    raise ValueError("Frozen profile ROI coordinates or field identity differ")
    preview_captures = {job["development_ids"][0]}
    if job["validation_ids"]:
        preview_captures.add(job["validation_ids"][0])
    measured_images = {}

    def observe(identity, role):
        record, raw_path, isp_path = by_id[identity]
        metadata = record["metadata"]
        progress(f"{role}: measuring native RAW / ISP {identity}")
        isp, isp_info = read_isp(isp_path, metadata["isp"])
        registered = metadata["isp"]["coordinate_mapping"] == "same_native_raster"
        observation = {
            "capture_id": identity,
            "role": role,
            "source_kind": metadata["source_kind"],
            "rois": [],
            "isp_info": isp_info,
        }
        with RawFrame(raw_path, metadata) as raw:
            if registered and tuple(isp.shape[:2]) != tuple(raw.shape):
                raise ValueError("Declared same-native-raster dimensions differ")
            for roi in rois:
                row, col, height, width = roi["box"]
                planes, sampling = {}, {}
                for channel in CHANNELS:
                    planes[channel], sampling[channel] = raw.plane(roi["box"], channel)
                item = {key: roi[key] for key in ("id", "kind", "field_id", "box")}
                item["sampling"] = sampling
                if roi["kind"] == "edge":
                    item["raw"] = {
                        channel: edge_metrics(
                            plane,
                            pitch_native=2,
                            black_level=0,
                            white_level=sampling[channel]["white_level_dn"]
                            - sampling[channel]["black_level_dn"],
                        )
                        for channel, plane in planes.items()
                    }
                    item["lateral_ca"] = _lateral_ca(planes, sampling, item["raw"])
                    if registered:
                        crop = isp[row : row + height, col : col + width]
                        item["isp"] = _metrics_isp(crop)
                        item["isp_interpretation"] = (
                            "decoded_camera_pipeline_diagnostic_not_matched_operator"
                            if physical_isp_diagnostic else
                            "linear_system_sfr"
                            if isp_info["linear"]
                            else "nonlinear_code_value_sfr_only"
                        )
                    else:
                        item["isp"] = {
                            x: _unavailable("isp_coordinates_unregistered")
                            for x in ("R", "G", "B", "Y")
                        }
                    gr = item["raw"]["Gr"]
                    if role == "development" and gr["status"] == "ok" and frozen is None:
                        raw_targets[roi["id"]].append(gr)
                        if roi["id"] not in templates:
                            geometry, levels = _geometry(roi, planes, sampling, gr, metadata)
                            templates[roi["id"]] = {
                                "geometry": geometry,
                                "levels": levels,
                                "shape": [height, width],
                                "capture_id": identity,
                            }
                else:
                    item["raw"] = {channel: _stats(plane) for channel, plane in planes.items()}
                    item["isp"] = (
                        {
                            channel: _stats(isp[row : row + height, col : col + width, i])
                            for i, channel in enumerate(("R", "G", "B"))
                        }
                        if registered
                        else {"status": "unavailable", "reason": "isp_coordinates_unregistered"}
                    )
                    item["absolute_reference_status"] = "not_established"
                    if roi["kind"] == "wedge":
                        item["diagnostic"] = (
                            "native crop retained; aliasing onset unavailable "
                            "without registered wedge frequency map"
                        )
                    for channel, plane in planes.items():
                        key = (role, roi["id"], channel)
                        if key not in temporal:
                            temporal[key] = {
                                "count": 0,
                                "sum": np.zeros_like(plane),
                                "sumsq": np.zeros_like(plane),
                            }
                        temporal[key]["count"] += 1
                        temporal[key]["sum"] += plane
                        temporal[key]["sumsq"] += plane * plane
                if identity in preview_captures and roi["kind"] == "edge":
                    levels = {
                        ch: {
                            "black_dn": sampling[ch]["black_level_dn"],
                            "white_dn": sampling[ch]["white_level_dn"],
                        }
                        for ch in CHANNELS
                    }
                    measured_images[(identity, roi["id"])] = _save_images(
                        out,
                        identity,
                        roi["id"],
                        "measured",
                        raw.read(roi["box"]),
                        isp[row : row + height, col : col + width] if registered else None,
                        levels,
                    )
                observation["rois"].append(item)
        observations.append(observation)

    for identity in job["development_ids"]:
        observe(identity, "development")
    fields, fit_rows = (frozen["fields"] if frozen is not None else {}), []
    baseline_sigma = float(spec.get("baseline_sigma_native_px", 1.5))
    field_rois = defaultdict(list)
    for roi in rois:
        if roi["id"] in templates:
            field_rois[roi["field_id"]].append(roi)
    if spec.get("fit", True) and frozen is None:
        for field_id, group in field_rois.items():
            progress(f"Fitting development-only effective Gaussian: {field_id}")
            angles = sorted(
                {round(raw_targets[roi["id"]][0]["frequency_direction_deg"], 3) for roi in group}
            )

            def field_objective(field, field_id=field_id, group=group):
                trial = {"fields": {field_id: field}}
                errors = []
                for roi in group:
                    template = templates[roi["id"]]
                    replay = simulate_edge(
                        trial,
                        template["geometry"],
                        template["levels"],
                        template["shape"],
                        first["raw_format"]["cfa"],
                        template["geometry"]["crop_origin"],
                    )
                    simulated = edge_metrics(replay["raw_planes"]["Gr"], pitch_native=2)
                    for target in raw_targets[roi["id"]]:
                        error = _curve_rmse(target, simulated)
                        errors.append(error if error is not None else 10.0)
                return float(np.mean(np.square(errors)))

            def objective(sigma, angles=angles, field_objective=field_objective):
                return field_objective(_field(float(sigma), angles))

            fit = minimize_scalar(
                objective,
                bounds=(0.15, 12.0),
                method="bounded",
                options={"xatol": 0.002, "maxiter": 36},
            )
            if fit.success and np.isfinite(fit.fun) and fit.fun < 1:
                fields[field_id] = _field(fit.x, angles)
                normals = np.array([templates[roi["id"]]["geometry"]["normal_xy"] for roi in group])
                directional_design = np.stack(
                    (normals[:, 0] ** 2, 2 * normals[:, 0] * normals[:, 1], normals[:, 1] ** 2),
                    axis=1,
                )
                # At least three independent normal directions identify the
                # three symmetric covariance entries. Two orthogonal edges
                # cannot determine rotation and remain an isotropic model.
                covariance_supported = (
                    len(group) >= 3
                    and np.linalg.matrix_rank(directional_design) == 3
                    and np.linalg.cond(directional_design) < 30
                )
                if covariance_supported:
                    variances = []
                    for roi in group:
                        target = raw_targets[roi["id"]][0]
                        f, response = (
                            np.array(target["frequency_cy_native_px"]),
                            np.array(target["sfr"]),
                        )
                        keep = (f > 0.02) & (response > 0.2) & (response < 0.9)
                        variances.append(
                            float(
                                np.median(-np.log(response[keep]) / (2 * np.pi**2 * f[keep] ** 2))
                            )
                            if keep.any()
                            else float(fit.x**2)
                        )
                    coefficients = np.linalg.lstsq(directional_design, variances, rcond=None)[0]
                    covariance = np.array(
                        [[coefficients[0], coefficients[1]], [coefficients[1], coefficients[2]]]
                    )
                    values, axes = np.linalg.eigh(covariance)
                    values = np.clip(values, 0.15**2, 12**2)
                    start = [
                        float(np.log(np.sqrt(values[-1]))),
                        float(np.log(np.sqrt(values[0]))),
                        float(np.degrees(np.arctan2(axes[1, -1], axes[0, -1])) % 180),
                    ]
                    elliptical = minimize(
                        lambda params, angles=angles, objective=field_objective: objective(
                            _ellipse(params, angles)
                        ),
                        start,
                        method="Powell",
                        bounds=[(np.log(0.15), np.log(12))] * 2 + [(0, 180)],
                        options={"maxiter": 25, "maxfev": 240, "xtol": 0.002, "ftol": 1e-7},
                    )
                    if (
                        elliptical.success
                        and np.isfinite(elliptical.fun)
                        and elliptical.fun < fit.fun
                    ):
                        fields[field_id] = _ellipse(elliptical.x, angles)
                        fit.fun, fit.nfev = elliptical.fun, fit.nfev + elliptical.nfev
                fit_rows.append(
                    {
                        "field_id": field_id,
                        "status": "fitted",
                        "sigma_native_px": float(fit.x),
                        "model": fields[field_id]["model"],
                        "covariance_orientation_supported": bool(covariance_supported),
                        "development_curve_rmse": float(np.sqrt(fit.fun)),
                        "curve_count": sum(len(raw_targets[r["id"]]) for r in group),
                        "method": "bounded_same_observer_full_curve_gaussian_fit",
                        "optimizer_evaluations": int(fit.nfev),
                    }
                )
            else:
                fit_rows.append(
                    {
                        "field_id": field_id,
                        "status": "unavailable",
                        "reason": "fit_did_not_converge",
                    }
                )
    profile = {
        "schema": SCHEMA,
        "camera_id": first["camera_id"],
        "conditions": first["conditions"],
        "raw_format": first["raw_format"],
        "geometry": {**first["geometry"], "roi_nuisance": templates},
        "fields": fields,
        "scope": "chart_condition_bound_effective_camera_response",
        "physical_validation": "not_established",
        "source_kind": first["source_kind"],
        "development_capture_ids": job["development_ids"],
        "fit_assumptions": [
            "centred Gaussian shared by measured directions; "
            "elliptical covariance requires at least three independent normals",
            "finite RAW Gr band 0..0.25 cycles/native-pixel",
            "unknown chart print blur and pixel response remain mixed in effective fit",
            "geometry, levels and Gaussian parameters learned from development only",
            "fixed bilinear ISP; real ISP processing is not calibrated",
        ],
    }
    if frozen is not None:
        profile = frozen
        fit_rows = [
            {"field_id": key, "status": "frozen_replay", "model": value["model"]}
            for key, value in fields.items()
        ]
    profile_sha = hashlib.sha256(_canonical(profile).encode()).hexdigest()
    (out / "frozen-candidate-before-validation.json").write_text(_canonical(profile))
    progress("Candidate frozen; beginning independent validation captures")
    for identity in job["validation_ids"]:
        observe(identity, "validation")
    if hashlib.sha256(_canonical(profile).encode()).hexdigest() != profile_sha:
        raise RuntimeError("Candidate changed while evaluating holdout")
    comparisons, simulation_cache = [], {}
    for roi in rois:
        if roi["id"] not in templates:
            continue
        template = templates[roi["id"]]
        stage_results = {}
        for stage, sigma in (("before", baseline_sigma), ("after", None)):
            if stage == "after" and roi["field_id"] not in fields:
                continue
            use = profile if stage == "after" else {"fields": {roi["field_id"]: _field(sigma, [])}}
            rendered = simulate_edge(
                use,
                template["geometry"],
                template["levels"],
                template["shape"],
                first["raw_format"]["cfa"],
                template["geometry"]["crop_origin"],
            )
            stage_results[stage] = {
                "rendered": rendered,
                "raw": edge_metrics(rendered["raw_planes"]["Gr"], pitch_native=2),
                "isp": edge_metrics(rendered["isp"] @ np.array([0.2126, 0.7152, 0.0722])),
            }
        simulation_cache[roi["id"]] = stage_results
    for observation in observations:
        for item in observation["rois"]:
            if item["kind"] != "edge":
                continue
            identity, roi_id = observation["capture_id"], item["id"]
            replay = simulation_cache.get(roi_id, {})
            comparison = {
                "capture_id": identity,
                "role": observation["role"],
                "roi_id": roi_id,
                "field_id": item["field_id"],
                "curves": {},
                "images": {},
            }
            for stage in ("raw", "isp"):
                measured = item[stage]["Gr" if stage == "raw" else "Y"]
                allowed = stage == "raw" or observation["isp_info"]["linear"]
                curves = {
                    "measured": measured,
                    **{name: value[stage] for name, value in replay.items()},
                }
                before = _curve_rmse(measured, curves.get("before", {})) if allowed else None
                after = _curve_rmse(measured, curves.get("after", {})) if allowed else None
                comparison[stage] = {
                    "status": "ok" if before is not None and after is not None else "unavailable",
                    "before_rmse": before,
                    "after_rmse": after,
                    "reason": None if allowed else "unknown_isp_transfer_no_linear_comparison",
                    "acceptance_eligible": not (stage == "isp" and physical_isp_diagnostic),
                    "acceptance_reason": "physical_isp_processing_not_matched_reference"
                    if stage == "isp" and physical_isp_diagnostic else None,
                }
                comparison["curves"][stage] = curves
            if (identity, roi_id) in measured_images:
                comparison["images"]["measured"] = measured_images[(identity, roi_id)]
                for stage, value in replay.items():
                    comparison["images"][stage] = _save_images(
                        out,
                        identity,
                        roi_id,
                        stage,
                        value["rendered"]["raw"],
                        value["rendered"]["isp"],
                        templates[roi_id]["levels"],
                    )
            comparisons.append(comparison)

    def aggregates(role):
        result = {}
        selected = [x for x in comparisons if x["role"] == role]
        for stage in ("raw", "isp"):
            valid = [x[stage] for x in selected if x[stage]["status"] == "ok"]
            result[f"{stage}_sfr_rmse"] = {
                "before": float(np.mean([x["before_rmse"] for x in valid])) if valid else None,
                "after": float(np.mean([x["after_rmse"] for x in valid])) if valid else None,
                "count": len(valid),
                "unavailable_count": len(selected) - len(valid),
                "worst_after": max((x["after_rmse"] for x in valid), default=None),
            }
        return result

    metrics, thresholds = aggregates("validation"), spec.get("thresholds", {})
    checks = []
    for comparison in comparisons:
        if comparison["role"] != "validation":
            continue
        for stage in ("raw", "isp"):
            key = f"{stage}_sfr_rmse"
            if key in thresholds:
                value = comparison[stage]["after_rmse"]
                checks.append(
                    {
                        "capture_id": comparison["capture_id"],
                        "roi_id": comparison["roi_id"],
                        "metric": key,
                        "value": value,
                        "threshold": thresholds[key],
                        "status": "diagnostic"
                        if stage == "isp" and physical_isp_diagnostic else "unavailable"
                        if value is None
                        else "pass"
                        if value <= thresholds[key]
                        else "fail",
                        "acceptance_eligible": not (stage == "isp" and physical_isp_diagnostic),
                        "interpretation": "diagnostic_reference_only_not_an_acceptance_test"
                        if stage == "isp" and physical_isp_diagnostic else "independent_frozen_profile_error",
                    }
                )
    temporal_results = []
    for (role, roi_id, channel), values in temporal.items():
        n = values["count"]
        variance = (
            np.maximum(0, (values["sumsq"] - values["sum"] ** 2 / n) / (n - 1)) if n > 1 else None
        )
        average_variance = float(np.mean(variance)) if variance is not None else None
        mean_signal = float(np.mean(values["sum"]) / n)
        snr = (
            mean_signal / np.sqrt(average_variance)
            if (average_variance is not None and average_variance > 0 and mean_signal > 0)
            else None
        )
        temporal_results.append(
            {
                "role": role,
                "roi_id": roi_id,
                "channel": channel,
                "frame_count": n,
                "temporal_variance_dn2": average_variance,
                "mean_signal_dn": mean_signal,
                "temporal_snr": float(snr) if snr is not None else None,
                "temporal_snr_db": float(20 * np.log10(snr)) if snr is not None else None,
                "status": "ok" if variance is not None else "insufficient_repeats",
                "interpretation": "repeat-capture temporal variation includes illumination drift; "
                "not dark/PTC inference",
            }
        )
    validation_status = "inconclusive"
    decision_checks = [check for check in checks if check["status"] != "diagnostic"]
    if decision_checks:
        validation_status = (
            "fail"
            if any(x["status"] == "fail" for x in decision_checks)
            else ("pass" if all(x["status"] == "pass" for x in decision_checks) else "inconclusive")
        )
    summary = {
        "schema": "camera_esfr_study_v1",
        "candidate_profile": profile,
        "candidate_profile_sha256": profile_sha,
        "fit": {
            "status": "frozen_replay"
            if frozen is not None
            else "fitted"
            if fields
            else "unavailable"
            if spec.get("fit", True)
            else "no_fit",
            "fields": fit_rows,
            "development_only": True,
            "candidate_frozen_before_holdout": True,
            "changed_parameters": []
            if frozen is not None
            else [f"fields.{field}.effective_gaussian_covariance" for field in fields],
            "baseline": {
                "sigma_native_px": baseline_sigma,
                "source": "explicit_manual_prior_not_db_optics",
            },
        },
        "observations": observations,
        "roi_count": len(rois),
        "edge_roi_count": sum(roi["kind"] == "edge" for roi in rois),
        "patch_summary": [
            {
                "capture_id": observation["capture_id"],
                "role": observation["role"],
                "rois": [roi for roi in observation["rois"] if roi["kind"] != "edge"],
            }
            for observation in observations
        ],
        "comparisons": comparisons,
        "metrics": metrics,
        "development_metrics": aggregates("development"),
        "temporal_patch_noise": temporal_results,
        "validation": {
            "status": validation_status,
            "checks": checks,
            "thresholds": thresholds,
            "scope": "raw_native_sfr_only" if physical_isp_diagnostic else "raw_and_matched_reference_isp_sfr",
            "applicable_metrics": ["raw_sfr_rmse"] if physical_isp_diagnostic else ["raw_sfr_rmse", "isp_sfr_rmse"],
            "diagnostic_metrics": ["isp_sfr_rmse"] if physical_isp_diagnostic else [],
            "isp_comparison_mode": first["isp"].get("comparison_mode", "reference_pipeline"),
            "development_capture_count": len(dev_ids),
            "validation_capture_count": len(val_ids),
            "interpretation": "no acceptance claim without explicit thresholds "
            "and available independent measurements",
        },
        "physical_validation": "not_established",
        "source_kind": first["source_kind"],
        "limitations": [
            "ISO algorithm conformance not established; native straight-edge engineering estimator",
            "effective centred Gaussian cannot identify phase, asymmetric PSF or lens-only blur",
            "all ROI directions retained; directional mismatch remains visible in per-ROI error",
            "DB selection is lineage context; "
            "baseline sigma is the explicitly declared manual prior",
            "fixed bilinear ISP is a declared reference pipeline, not the real camera ISP",
            "independent validation does not prove chart traceability or physical camera identity",
            "absolute color/OECF and QE/Dark/PTC calibration are not inferred from chart patches",
            "wedge crops are diagnostics; no aliasing frequency without calibrated wedge map",
        ],
        "images": [
            path
            for comparison in comparisons
            for paths in comparison["images"].values()
            for kind, path in paths.items()
            if kind in {"raw", "isp"}
        ],
    }
    # A canonical round trip both strips numpy scalar types and rejects NaN/Inf.
    return json.loads(_canonical(summary))

"""Condition-bound E-SFR patch IQ, independent of QE/electron calibration.

Native DN are black-subtracted exactly once and divided by declared ADC range.
The reference is a versioned nominal chart design, not a measured print. All
quantitative matrix outputs stay unclipped. Tone is a forward response model,
not an additional correction silently composed with the color matrix.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import numpy as np
from skimage.color import deltaE_ciede2000, xyz2lab

from .esfr_chart import chart_manifest
from .esfr_engine import bilinear_isp, _labels
from .esfr_io import RawFrame, read_isp
from .registry import digest
from .calibration_ccm import solve_bounded_matrix

SCHEMA = "camera_esfr_iq_v1"
PROFILE_SCHEMA = "camera_esfr_iq_profile_v1"
REFERENCE_ID = "imatest_esfr_2023_nominal_v1"
RAW_CHANNELS = ("R", "Gr", "Gb", "B")
ISP_CHANNELS = ("R", "G", "B")
XYZ_D50_TO_D65 = np.array([[.9555766, -.0230393, .0631636],
                         [-.0282895, 1.0099416, .0210077],
                         [.0122982, -.0204830, 1.3299098]])
SRGB_TO_XYZ = np.array([[.4124564, .2126729, .0193339],
                       [.3575761, .7151522, .1191920],
                       [.1804375, .0721750, .9503041]])
LUMA = np.array([.2126, .7152, .0722])
CONTEXT_KEYS = ("camera_id", "conditions", "raw_format", "geometry", "pixel_pitch_um", "isp", "source_kind")
MAX_CAPTURE_COUNT = 128
MAX_PATCH_PIXELS = 65_536
MAX_TOTAL_PATCH_PIXELS = 500_000


def _signed_reference(lab):
    lightness, a, b = lab
    fy = (lightness + 16) / 116
    f = np.array([fy + a / 500, fy, fy - b / 200])
    d = 6 / 29
    xyz50 = np.where(f > d, f**3, 3 * d**2 * (f - 4 / 29)) * [.96422, 1, .82521]
    xyz65 = XYZ_D50_TO_D65 @ xyz50
    return xyz65, xyz65 @ np.linalg.inv(SRGB_TO_XYZ)


def nominal_reference():
    """Return a detached nominal reference; no clipped preview RGB is reused."""
    manifest = chart_manifest()
    colors = []
    for patch in manifest["color_patches"]:
        xyz, rgb = _signed_reference(patch["nominal_lab_d50"])
        colors.append({"id": patch["id"], "lab_d50": patch["nominal_lab_d50"],
                       "xyz_d65": xyz.tolist(), "linear_srgb_d65": rgb.tolist(),
                       "outside_display_srgb_gamut": bool(np.any((rgb < 0) | (rgb > 1))),
                       "lab_d65": xyz2lab(xyz, illuminant="D65", observer="2").tolist()})
    result = {"id": REFERENCE_ID, "status": "manufacturer_nominal_not_measured_print",
              "color_count": 18, "gray_count": 20,
              "colors": sorted(colors, key=lambda row: row["id"]),
              "grays": [{"id": p["id"], "nominal_density": p["nominal_density"],
                         "nominal_reflectance": p["nominal_linear_reflectance"]}
                        for p in manifest["gray_patches"]],
              "adaptation": {"method": "Bradford_D50_to_D65", "matrix": XYZ_D50_TO_D65.tolist(),
                             "white_Y": 1, "observer": "CIE_1931_2_degree", "clipped": False},
              "source": deepcopy(manifest["photometry"]),
              "physical_validation": "not_established"}
    result["sha256"] = digest(result)
    return result


def _verify(path, descriptor):
    path = Path(path)
    if not path.is_file() or ("bytes" in descriptor and path.stat().st_size != descriptor["bytes"]):
        raise ValueError("IQ original file size differs from capture binding")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != descriptor.get("sha256"):
        raise ValueError("IQ original SHA differs from capture binding")


def _patches(rois, reference):
    expected = {p["id"]: "color" for p in reference["colors"]}
    expected.update({p["id"]: "gray" for p in reference["grays"]})
    rows = [deepcopy(r) for r in rois if r.get("kind") in {"color", "gray"}]
    if len(rows) != 38 or {r["id"]: r["kind"] for r in rows} != expected:
        raise ValueError("IQ requires exactly all 18 nominal color and 20 gray patch identities")
    for row in rows:
        box = row.get("box")
        if (not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box)
                or min(box[:2]) < 0 or min(box[2:]) < 4 or box[2] * box[3] > MAX_PATCH_PIXELS):
            raise ValueError("IQ patch requires bounded native integer box and at least 4 pixels per side")
    if sum(r["box"][2] * r["box"][3] for r in rows) > MAX_TOTAL_PATCH_PIXELS:
        raise ValueError("IQ patch memory budget exceeds 500000 native pixels")
    for index, row in enumerate(rows):
        y, x, h, w = row["box"]
        for other in rows[:index]:
            yy, xx, hh, ww = other["box"]
            if min(y+h, yy+hh) > max(y, yy) and min(x+w, xx+ww) > max(x, xx):
                raise ValueError("IQ color and gray patch boxes must not overlap")
    return sorted(rows, key=lambda row: row["id"])


def _stats(values):
    a = np.asarray(values, dtype=float)
    return {"mean": float(a.mean()), "spatial_std": float(a.std()),
            "spatial_variance": float(a.var()), "sample_count": int(a.size)}


def _update(moments, key, values):
    a = np.asarray(values, dtype=float)
    if key not in moments:
        moments[key] = {"n": 0, "mean": np.zeros_like(a), "m2": np.zeros_like(a)}
    state = moments[key]
    if state["mean"].shape != a.shape:
        raise ValueError("Temporal noise requires exactly registered native patch samples")
    state["n"] += 1
    delta = a - state["mean"]
    state["mean"] += delta / state["n"]
    state["m2"] += delta * (a - state["mean"])


def _temporal(moments):
    rows = []
    for (role, patch_id, domain, channel), state in sorted(moments.items()):
        n, signal = state["n"], float(state["mean"].mean())
        variance = float(np.maximum(state["m2"] / (n-1), 0).mean()) if n > 1 else None
        snr = signal / np.sqrt(variance) if variance is not None and variance > 0 and signal > 0 else None
        rows.append({"role": role, "patch_id": patch_id, "domain": domain, "channel": channel,
                     "frame_count": n, "mean_signal": signal, "temporal_variance": variance,
                     "temporal_std": float(np.sqrt(variance)) if variance is not None else None,
                     "snr": float(snr) if snr is not None else None,
                     "snr_db": float(20*np.log10(snr)) if snr is not None else None,
                     "unit": "DN" if domain == "raw" else "decoded_relative_output",
                     "status": "estimated" if n > 1 else "insufficient_repeats",
                     "interpretation": "registered_repeat_variation_includes_illumination_drift_not_dark_ptc",
                     "uncertainty": "few_repeats" if n < 8 else "no_confidence_interval_established"})
    return rows


def observe_patches(captures, rois, *, actual_isp_color_space="unknown", on_progress=None):
    """Observe original capture dictionaries sequentially; no image is relabelled."""
    if actual_isp_color_space not in {"unknown", "srgb"}:
        raise ValueError("IQ actual ISP color space must be unknown or explicitly srgb")
    if not captures or len(captures) > MAX_CAPTURE_COUNT:
        raise ValueError("IQ needs 1..128 explicitly selected captures")
    reference, progress = nominal_reference(), on_progress or (lambda _: None)
    patches = _patches(rois, reference)
    records = [c["record"] for c in captures]
    if len({r["id"] for r in records}) != len(records):
        raise ValueError("IQ capture identities must be unique")
    first = records[0]["metadata"]
    context = {k: deepcopy(first.get(k)) for k in CONTEXT_KEYS}
    context["actual_isp_color_space"] = actual_isp_color_space
    if first.get("source_kind") not in {"measured", "synthetic", "model_derived"}:
        raise ValueError("IQ requires explicit preserved source_kind")
    observations, bindings, moments = [], [], {}
    for capture in captures:
        record = capture["record"]
        meta, role = record["metadata"], record["metadata"].get("role")
        if role not in {"development", "validation"}:
            raise ValueError("IQ capture role must be explicit development or validation")
        if any(meta.get(k) != context[k] for k in CONTEXT_KEYS):
            raise ValueError("IQ captures must retain identical sensor, nominal conditions and ISP context")
        for domain in ("raw", "isp"):
            _verify(capture[domain + "_path"], record["files"][domain])
        progress("IQ patch observation: " + record["id"])
        isp, isp_info = read_isp(capture["isp_path"], meta["isp"])
        if not isp_info["linear"] or meta["isp"].get("coordinate_mapping") != "same_native_raster":
            raise ValueError("IQ requires registered explicitly decoded linear/sRGB ISP")
        if isp_info.get("orientation_tag", 1) != 1:
            raise ValueError("IQ native registration does not apply an ISP orientation transform")
        colorimetry = actual_isp_color_space == "srgb" and not isp_info.get("grayscale_replicated", False)
        color_reason = None if colorimetry else "unknown_actual_isp_colorimetry"
        if isp_info.get("icc_profile_present") and not isp_info.get("icc_profile_applied"):
            colorimetry, color_reason = False, "unapplied_icc_profile"
        capture_rows = []
        with RawFrame(capture["raw_path"], meta) as raw:
            if tuple(isp.shape[:2]) != tuple(raw.shape):
                raise ValueError("IQ RAW and ISP dimensions must match native raster")
            for patch in patches:
                y, x, h, w = patch["box"]
                planes, stats, normalized, sampling = {}, {}, {}, {}
                clip, below = {}, {}
                for channel in RAW_CHANNELS:
                    plane, info = raw.plane(patch["box"], channel)
                    planes[channel], sampling[channel] = plane, info
                    stats[channel] = _stats(plane)
                    normalized[channel] = plane / (info["white_level_dn"] - info["black_level_dn"])
                    codes = plane + info["black_level_dn"]
                    clip[channel] = float(np.mean((codes <= 0) | (codes >= info["white_level_dn"])))
                    below[channel] = float(np.mean(plane < 0))
                    _update(moments, (role, patch["id"], "raw", channel), plane)
                green = np.concatenate((normalized["Gr"].ravel(), normalized["Gb"].ravel()))
                rgb = [float(normalized["R"].mean()), float(green.mean()), float(normalized["B"].mean())]
                crop = isp[y:y+h, x:x+w]
                isp_stats = {c: _stats(crop[..., i]) for i, c in enumerate(ISP_CHANNELS)}
                isp_clip = [float(np.mean((crop[..., i] <= 0) | (crop[..., i] >= 1))) for i in range(3)]
                for i, channel in enumerate(ISP_CHANNELS):
                    _update(moments, (role, patch["id"], "isp", channel), crop[..., i])
                reasons = (["raw_adc_clipping"] if any(clip.values()) else [])
                if min(rgb) <= 0:
                    reasons.append("nonpositive_camera_patch_mean")
                capture_rows.append({"id": patch["id"], "kind": patch["kind"], "box": patch["box"],
                    "raw": stats, "raw_sampling": sampling, "camera_rgb": rgb,
                    "camera_rgb_definition": "R, sample_count_weighted_Gr_Gb, B after one black subtraction / per-site ADC range",
                    "isp": isp_stats, "actual_isp_rgb": [isp_stats[c]["mean"] for c in ISP_CHANNELS],
                    "qc": {"raw_clipped_fraction": clip, "raw_below_black_fraction": below,
                           "isp_clipped_fraction": isp_clip, "fit_eligible": not reasons,
                           "exclusion_reasons": reasons},
                    "actual_isp_delta_e_eligible": colorimetry,
                    "actual_isp_delta_e_reason": color_reason})
        binding = {"capture_id": record["id"], "role": role, "source_kind": meta["source_kind"],
                   "record_sha256": record.get("content_hash", digest(record)),
                   "raw_sha256": record["files"]["raw"]["sha256"],
                   "isp_sha256": record["files"]["isp"]["sha256"],
                   "native_pixel_sha256": record.get("native_pixel_sha256"),
                   "isp_pixel_sha256": record.get("isp_pixel_sha256"),
                   "session_id": meta.get("session_id"), "acquisition_id": meta.get("acquisition_id")}
        bindings.append(binding)
        observations.append({**binding, "patches": capture_rows, "isp_decode": isp_info})
        del isp
    result = {"schema": SCHEMA, "reference": reference, "context": context,
              "source_kind": first["source_kind"], "rois": patches,
              "roi_sha256": digest(patches), "capture_bindings": bindings,
              "captures": observations, "noise": _temporal(moments),
              "normalization": "fixed_native_dn", "black_subtraction_count": 1,
              "physical_validation": "not_established"}
    result["observation_sha256"] = digest(result)
    return result


def _verify_bundle(bundle):
    if bundle.get("schema") != SCHEMA or bundle.get("observation_sha256") != digest(
            {k: v for k, v in bundle.items() if k != "observation_sha256"}):
        raise ValueError("IQ observation bundle hash differs")
    ref = bundle["reference"]
    if ref.get("sha256") != digest({k: v for k, v in ref.items() if k != "sha256"}):
        raise ValueError("IQ reference hash differs")


def _pava(values):
    blocks = []
    for value in values:
        blocks.append([float(value), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b, a = blocks.pop(), blocks.pop()
            n = a[1] + b[1]
            blocks.append([(a[0]*a[1]+b[0]*b[1])/n, n])
    return [value for value, count in blocks for _ in range(count)]


def _color_metric(rgb, target, *, delta_e_eligible=True, reason=None):
    rgb, target = np.asarray(rgb, float), np.asarray(target, float)
    xyz, reference = rgb @ SRGB_TO_XYZ, target @ SRGB_TO_XYZ
    result = {"linear_rgb_rmse": float(np.sqrt(np.mean((rgb-target)**2))),
              "max_abs_component_error": float(np.max(np.abs(rgb-target))),
              "delta_e00": None, "lab_d65": None, "matrix_outputs_clipped": False,
              "negative_components": int((rgb < 0).sum()), "above_one_components": int((rgb > 1).sum())}
    if not delta_e_eligible:
        result.update(linear_rgb_rmse=None, max_abs_component_error=None,
                      color_error_status=reason or "unknown_colorimetry",
                      delta_e_status=reason or "unknown_colorimetry")
    elif np.any(xyz < 0) or np.any(reference < 0):
        result["delta_e_status"] = "unavailable_negative_xyz_no_clipping"
    else:
        lab, expected = xyz2lab(xyz, illuminant="D65", observer="2"), xyz2lab(reference, illuminant="D65", observer="2")
        result.update(delta_e00=float(deltaE_ciede2000(lab, expected)), lab_d65=lab.tolist(),
                      delta_e_status="nominal_d65_reference_diagnostic")
    return result


def _summary(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return {"mean": float(np.mean(values)) if values else None,
            "max": float(np.max(values)) if values else None, "count": len(values),
            "unavailable_count": len(rows)-len(values)}


def _evaluate(profile, bundle, role):
    reference = {p["id"]: p for p in profile["reference"]["colors"]}
    grays = {p["id"]: p for p in profile["reference"]["grays"]}
    wb, scale, matrix = np.asarray(profile["white_balance"]), profile["exposure_scale"], np.asarray(profile["ccm"]["matrix"])
    color_rows, tone_rows, neutral_rows = [], [], []
    for capture in bundle["captures"]:
        if capture["role"] != role:
            continue
        for patch in capture["patches"]:
            if patch["kind"] == "color":
                camera, target = np.asarray(patch["camera_rgb"]), reference[patch["id"]]["linear_srgb_d65"]
                balanced = camera * wb * scale
                after = balanced @ matrix
                color_rows.append({"capture_id": capture["capture_id"], "patch_id": patch["id"],
                    "role": role, "raw_sha256": capture["raw_sha256"], "same_raw_before_after": True,
                    "target_linear_srgb": target, "nominal_lab_d50": reference[patch["id"]]["lab_d50"],
                    "baseline_reference_rgb": camera.tolist(), "candidate_reference_rgb": after.tolist(),
                    "wb_only_reference_rgb": balanced.tolist(),
                    "actual_isp_rgb": patch["actual_isp_rgb"], "qc": patch["qc"],
                    "before": _color_metric(camera, target), "wb_only": _color_metric(balanced, target),
                    "after": _color_metric(after, target),
                    "actual_isp": _color_metric(patch["actual_isp_rgb"], target,
                        delta_e_eligible=patch["actual_isp_delta_e_eligible"], reason=patch["actual_isp_delta_e_reason"])})
            else:
                x = grays[patch["id"]]["nominal_reflectance"]
                camera = np.asarray(patch["camera_rgb"])
                corrected = camera * wb * scale @ matrix
                neutral_rows.append({"capture_id": capture["capture_id"], "patch_id": patch["id"],
                    "target_reflectance": x, "baseline_reference_rgb": camera.tolist(),
                    "candidate_reference_rgb": corrected.tolist(),
                    "before_rgb_rmse": float(np.sqrt(np.mean((camera-x)**2))),
                    "after_rgb_rmse": float(np.sqrt(np.mean((corrected-x)**2))), "qc": patch["qc"]})
                observed = {"raw_green": patch["camera_rgb"][1],
                            "actual_isp_luma": float(np.asarray(patch["actual_isp_rgb"]) @ LUMA)}
                for domain, value in observed.items():
                    model = profile["tone"][domain]
                    supported = bool(model["input"] and model["input"][0] <= x <= model["input"][-1])
                    prediction = float(np.interp(x, model["input"], model["output"])) if supported else None
                    tone_rows.append({"capture_id": capture["capture_id"], "patch_id": patch["id"], "role": role,
                        "domain": domain, "nominal_reflectance": x, "observed": value, "before": x,
                        "prediction": prediction, "residual": value-prediction if supported else None,
                        "qc": patch["qc"], "status": "diagnostic" if supported else "outside_observed_domain"})
    eligible = [r for r in color_rows if r["qc"]["fit_eligible"]]
    color_summary = {stage: {"delta_e00": _summary([r[stage] for r in eligible], "delta_e00"),
                            "linear_rgb_rmse": _summary([r[stage] for r in eligible], "linear_rgb_rmse")}
                     for stage in ("before", "wb_only", "after", "actual_isp")}
    tone_summary = {}
    for domain in profile["tone"]:
        rows = [r for r in tone_rows if r["domain"] == domain and r["prediction"] is not None and r["qc"]["fit_eligible"]]
        tone_summary[domain] = {"count": len(rows),
            "before_rmse": float(np.sqrt(np.mean([(r["observed"]-r["before"])**2 for r in rows]))) if rows else None,
            "after_rmse": float(np.sqrt(np.mean([r["residual"]**2 for r in rows]))) if rows else None}
    return {"status": "diagnostic_no_acceptance_thresholds", "role": role, "refit_performed": False,
            "reference_status": profile["reference"]["status"], "profile_sha256": profile["profile_sha256"],
            "observation_sha256": bundle["observation_sha256"], "color": {"patches": color_rows, "summary": color_summary,
            "comparison_stages": {"before": "native_DN_normalized_identity_reference_ISP",
                                  "wb_only": "development_fixed_WB_and_exposure_identity_CCM",
                                  "after": "development_fixed_WB_exposure_and_CCM",
                                  "actual_isp": "separate_observed_camera_output"},
            "total_patch_observations": len(color_rows), "eligible_patch_observations": len(eligible),
            "excluded": [{"capture_id": r["capture_id"], "patch_id": r["patch_id"], "reasons": r["qc"]["exclusion_reasons"]}
                         for r in color_rows if not r["qc"]["fit_eligible"]]},
            "tone": {"patches": tone_rows, "models": deepcopy(profile["tone"]), "summary": tone_summary,
                     "interpretation": "forward_response_fit_not_a_composed_isp_correction"},
            "neutral_reference_isp": {"patches": neutral_rows,
                "before_rgb_rmse": _summary([r for r in neutral_rows if r["qc"]["fit_eligible"]], "before_rgb_rmse"),
                "after_rgb_rmse": _summary([r for r in neutral_rows if r["qc"]["fit_eligible"]], "after_rgb_rmse"),
                "interpretation": "combined_WB_exposure_CCM_neutral_response_not_a_tone_fit"},
            "noise": [r for r in bundle["noise"] if r["role"] == role],
            "same_raw_before_after": True, "raw_sha256": [c["raw_sha256"] for c in bundle["capture_bindings"] if c["role"] == role],
            "physical_validation": "not_established"}


def fit_profile(bundle):
    """Fit development midgray WB/exposure then a bounded 3x3 color correction."""
    _verify_bundle(bundle)
    if not bundle["captures"] or any(c["role"] != "development" for c in bundle["captures"]):
        raise ValueError("IQ fitting accepts development observations only")
    ref = bundle["reference"]
    midgray = min(ref["grays"], key=lambda row: abs(row["nominal_reflectance"]-.18))
    mid = [next(p for p in c["patches"] if p["id"] == midgray["id"]) for c in bundle["captures"]]
    if any(not p["qc"]["fit_eligible"] for p in mid):
        raise ValueError("The fixed nominal midgray is clipped or invalid in development")
    neutral = np.mean([p["camera_rgb"] for p in mid], axis=0)
    wb = neutral[1] / neutral
    scale = midgray["nominal_reflectance"] / neutral[1]
    if not np.isfinite(wb).all() or np.any(wb <= 0) or np.any(wb > 32) or not 1e-6 <= scale <= 1e6:
        raise ValueError("Development midgray produces invalid WB/exposure normalization")
    target_map = {p["id"]: p["linear_srgb_d65"] for p in ref["colors"]}
    patches = [p for c in bundle["captures"] for p in c["patches"] if p["kind"] == "color" and p["qc"]["fit_eligible"]]
    if len({p["id"] for p in patches}) < 4:
        raise ValueError("CCM requires at least four eligible distinct color patches")
    x = np.asarray([p["camera_rgb"] for p in patches]) * wb * scale
    target = np.asarray([target_map[p["id"]] for p in patches])
    ridge, bound = 1e-6, 8.0
    matrix, rank, condition = solve_bounded_matrix(x, target, ridge=ridge, coefficient_bound=bound)
    tones = {}
    for domain in ("raw_green", "actual_isp_luma"):
        points = []
        for gray in ref["grays"]:
            rows = [p for c in bundle["captures"] for p in c["patches"] if p["id"] == gray["id"] and p["qc"]["fit_eligible"]
                    and (domain != "actual_isp_luma" or not any(p["qc"]["isp_clipped_fraction"]))]
            if rows:
                y = np.mean([p["camera_rgb"][1] if domain == "raw_green" else np.asarray(p["actual_isp_rgb"]) @ LUMA for p in rows])
                points.append((gray["nominal_reflectance"], float(y), gray["id"]))
        points.sort()
        if len(points) < 3:
            raise ValueError("Tone response needs at least three eligible distinct gray levels")
        tones[domain] = {"input": [p[0] for p in points], "output": _pava([p[1] for p in points]),
                         "observed_means": [p[1] for p in points], "patch_ids": [p[2] for p in points],
                         "method": "observed_domain_isotonic_piecewise_linear_v1", "extrapolation": "unavailable",
                         "synthetic_endpoints_added": False, "direction": "nominal_reflectance_to_observed_response"}
    profile = {"schema": PROFILE_SCHEMA, "source_kind": bundle["source_kind"], "context": bundle["context"],
               "roi_sha256": bundle["roi_sha256"], "reference": ref, "reference_sha256": ref["sha256"],
               "normalization": {"kind": "fixed_native_dn", "black_subtraction_count": 1,
                                 "formula": "(native_DN - site_black_DN) / (site_white_DN - site_black_DN)",
                                 "green_definition": "sample_count_weighted_Gr_Gb", "dn_per_e_used": False,
                                 "per_capture_rescaling": False},
               "white_balance": wb.tolist(), "exposure_scale": float(scale),
               "neutral_reference": {"patch_id": midgray["id"], "nominal_reflectance": midgray["nominal_reflectance"],
                                     "development_camera_rgb": neutral.tolist()},
               "ccm": {"matrix": matrix.tolist(), "matrix_convention": "row_vector", "baseline_matrix": np.eye(3).tolist(),
                       "application": "(camera_rgb * white_balance * exposure_scale) @ matrix", "rank": rank,
                       "condition_number": condition, "ridge": ridge, "coefficient_bound": bound,
                       "bound_active": bool(np.isclose(np.abs(matrix), bound, atol=1e-7).any()),
                       "method": "bounded_linear_least_squares_v1", "fitted_observations": len(patches)},
               "tone": tones, "training": {"capture_bindings": bundle["capture_bindings"],
                                              "observation_sha256": bundle["observation_sha256"]},
               "scope": "conditional_nominal_chart_correction_no_qe_or_full_isp_identification",
               "limitations": ["Nominal chart targets are not measured print reflectance or absolute color validation.",
                    "WB/exposure are derived once from development midgray before conditional CCM fitting; components are not uniquely identified.",
                    "Tone models describe observed responses over measured gray levels; no invented 0/1 endpoints or tone correction is applied.",
                    "Native CFA patch means and bilinear image values are different observers near patch boundaries; previews are not measurements.",
                    "ISP color-space declarations do not authenticate physical camera colorimetry; unapplied ICC profiles block DeltaE.",
                    "Repeat variation includes illumination drift and is not a Dark/PTC or electron conversion measurement."],
               "physical_validation": "not_established"}
    profile["profile_sha256"] = digest(profile)
    return {"profile": profile, "development": _evaluate(profile, bundle, "development")}


def _verified_profile(profile):
    if profile.get("schema") != PROFILE_SCHEMA or profile.get("profile_sha256") != digest({k: v for k, v in profile.items() if k != "profile_sha256"}):
        raise ValueError("IQ frozen profile hash differs")


def validate_profile(profile, bundle):
    """Apply a frozen profile to independently acquired validation observations."""
    _verified_profile(profile)
    _verify_bundle(bundle)
    if not bundle["captures"] or any(c["role"] != "validation" for c in bundle["captures"]):
        raise ValueError("IQ validation accepts validation observations only")
    if (profile["context"] != bundle["context"] or profile["roi_sha256"] != bundle["roi_sha256"]
            or profile["reference_sha256"] != bundle["reference"]["sha256"]):
        raise ValueError("IQ validation context, ROI or nominal reference differs from frozen candidate")
    training, validation = profile["training"]["capture_bindings"], bundle["capture_bindings"]
    for key in ("capture_id", "raw_sha256", "isp_sha256", "session_id", "acquisition_id"):
        old, new = {r.get(key) for r in training}, {r.get(key) for r in validation}
        if None in old | new or old & new:
            raise ValueError("IQ validation overlaps training or lacks independent " + key)
    for key in ("native_pixel_sha256", "isp_pixel_sha256"):
        old = {r.get(key) for r in training} - {None}
        new = {r.get(key) for r in validation} - {None}
        if old & new:
            raise ValueError("IQ validation reuses native pixels: " + key)
    result = _evaluate(profile, bundle, "validation")
    result["independent_of_training"] = True
    return result


def run(captures, rois, *, frozen_profile=None, actual_isp_color_space="unknown", on_progress=None):
    bundle = observe_patches(captures, rois, actual_isp_color_space=actual_isp_color_space, on_progress=on_progress)
    if frozen_profile is None:
        result = fit_profile(bundle)
    else:
        result = {"profile": deepcopy(frozen_profile), "validation": validate_profile(frozen_profile, bundle)}
    return {"schema": SCHEMA, **result, "observations": bundle, "reference": bundle["reference"],
            "source_kind": bundle["source_kind"], "physical_validation": "not_established"}


def analyze_repeats(captures, rois, *, actual_isp_color_space="unknown", on_progress=None):
    """No fitting: use an explicitly selected repeat set within one data role."""
    if not captures:
        return {"schema": SCHEMA, "status": "unavailable_no_repeat_set", "noise": [],
                "capture_bindings": [], "fit_performed": False}
    if len(captures) < 2 or len(captures) > 8:
        raise ValueError("An explicit IQ noise set requires 2..8 captures")
    if len({c["record"]["metadata"]["role"] for c in captures}) != 1:
        raise ValueError("An IQ noise set must not combine development and validation")
    bundle = observe_patches(captures, rois, actual_isp_color_space=actual_isp_color_space, on_progress=on_progress)
    return {"schema": SCHEMA, "status": "diagnostic_no_acceptance_thresholds", "noise": bundle["noise"],
            "capture_bindings": bundle["capture_bindings"], "source_kind": bundle["source_kind"],
            "observation_sha256": bundle["observation_sha256"], "fit_performed": False,
            "physical_validation": "not_established"}


def apply_profile_rgb(profile, linear_rgb, *, baseline=False):
    """Fixed normalized camera RGB -> signed linear reference ISP; no clipping."""
    _verified_profile(profile)
    values = np.asarray(linear_rgb)
    if values.ndim < 1 or values.shape[-1] != 3 or values.dtype.kind not in "fiu" or not np.isfinite(values).all():
        raise ValueError("IQ reference ISP requires finite numeric RGB triplets")
    values = values.astype(np.float64)
    if baseline:
        return values.copy()
    return values * np.asarray(profile["white_balance"]) * profile["exposure_scale"] @ np.asarray(profile["ccm"]["matrix"])


def render_reference_preview(capture, profile, *, max_dimension=1280):
    """Native tiled bilinear/WB/CCM then display-only decimation; no image warp.

    This is a reference ISP visualization of the SAME RAW, not the real camera's
    ISP or a quantitative resampled measurement. Tone fits are not composed.
    """
    _verified_profile(profile)
    if type(max_dimension) is not int or not 64 <= max_dimension <= 2048:
        raise ValueError("IQ preview dimension must be 64..2048")
    record, meta = capture["record"], capture["record"]["metadata"]
    _verify(capture["raw_path"], record["files"]["raw"])
    if any(meta.get(k) != profile["context"].get(k) for k in CONTEXT_KEYS):
        raise ValueError("IQ preview capture context differs from frozen profile")
    with RawFrame(capture["raw_path"], meta) as raw:
        height, width = raw.shape
        factor = min(1.0, max_dimension/max(height, width))
        oh, ow = max(1, round(height*factor)), max(1, round(width*factor))
        rows, cols = np.linspace(0, height-1, oh).astype(int), np.linspace(0, width-1, ow).astype(int)
        before, after = np.empty((oh, ow, 3), np.float32), np.empty((oh, ow, 3), np.float32)
        raw_preview = np.empty((oh, ow), np.float32)
        fmt = raw.info["raw_format"]
        levels = {ch: {"black_dn": float(fmt["black_level_dn"][i]), "white_dn": float(fmt["white_level_dn"][i])}
                  for i, ch in enumerate(_labels(meta["raw_format"]["cfa"]))}
        origin = meta["geometry"]["crop_origin"]
        for y in range(0, height, 256):
            iy = np.flatnonzero((rows >= y) & (rows < min(y+256, height)))
            if not len(iy):
                continue
            for x in range(0, width, 256):
                ix = np.flatnonzero((cols >= x) & (cols < min(x+256, width)))
                if not len(ix):
                    continue
                top, left = max(0, y-2), max(0, x-2)
                bottom, right = min(height, y+258), min(width, x+258)
                mosaic = raw.read([top, left, bottom-top, right-left])
                linear = bilinear_isp(mosaic, meta["raw_format"]["cfa"], [origin[0]+top, origin[1]+left], levels)
                sample = linear[(rows[iy]-top)[:, None], (cols[ix]-left)[None, :]]
                before[np.ix_(iy, ix)] = sample
                after[np.ix_(iy, ix)] = sample * np.asarray(profile["white_balance"]) * profile["exposure_scale"] @ np.asarray(profile["ccm"]["matrix"])
                sy, sx = (rows[iy]-top)[:, None], (cols[ix]-left)[None, :]
                codes = mosaic[sy, sx].astype(float)
                site = ((rows[iy][:, None]+origin[0]) % 2)*2 + ((cols[ix][None, :]+origin[1]) % 2)
                black, white = np.asarray(fmt["black_level_dn"])[site], np.asarray(fmt["white_level_dn"])[site]
                raw_preview[np.ix_(iy, ix)] = (codes-black)/(white-black)
    return {"baseline_linear_rgb": before, "candidate_linear_rgb": after, "normalized_raw_preview": raw_preview,
            "raw_sha256": record["files"]["raw"]["sha256"], "same_raw_before_after": True,
            "native_shape": [height, width], "preview_shape": [oh, ow], "display_only": True,
            "method": "native_bilinear_256px_tiles_2px_halo_then_display_decimation",
            "tone_applied": False, "matrix_outputs_clipped": False}

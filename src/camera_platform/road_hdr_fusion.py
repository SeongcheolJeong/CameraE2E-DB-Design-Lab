"""Bounded native-CFA HDR adapters; RAW/calibration only, never scene truth.

Noise estimates are conditional on estimated flow and known simulation FPN.
Warping creates spatial covariance; diagonal variance and an upper bound for
residual testing are retained separately. No deconvolution is performed.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import map_coordinates, maximum_filter

from .saved_sensor_hdr import _merge_native_fpn

PROFILES = ("static", "global", "local")
TILE = 256


def calibrated_rate(raw, exposure_s, calibration, geometry):
    """Reuse the established FPN inverse on bounded unchanged native tiles."""
    if raw.ndim != 2 or any(n % 2 for n in raw.shape) or raw.dtype.kind not in "iu":
        raise ValueError("An even native integer mosaic is required.")
    if not np.isfinite(exposure_s) or exposure_s <= 0:
        raise ValueError("Positive finite exposure required.")
    arrays = {
        k: np.empty(raw.shape, dtype=bool if k == "valid" else np.float64)
        for k in ("rate", "variance", "valid")
    }
    for r in range(0, raw.shape[0], TILE):
        for c in range(0, raw.shape[1], TILE):
            s = np.s_[r : min(r + TILE, raw.shape[0]), c : min(c + TILE, raw.shape[1])]
            cal = {k: (v[s] if isinstance(v, np.ndarray) else v) for k, v in calibration.items()}
            local = {
                **geometry,
                "crop_origin": [geometry["crop_origin"][0] + r, geometry["crop_origin"][1] + c],
            }
            out = _merge_native_fpn(
                raw[s][None].astype(np.int32), [exposure_s], [cal], geometry=local
            )["arrays"]
            arrays["rate"][s] = out["exposure_rate"][0]
            arrays["variance"][s] = out["exposure_estimated_variance"][0]
            arrays["valid"][s] = out["exposure_valid_mask"][0]
    return arrays


def guide(frame, shape):
    """All four sites averaged at each Bayer cell, then uniformly downsampled."""
    import cv2

    h, w = frame["rate"].shape
    value = np.maximum(frame["rate"], 0).reshape(h // 2, 2, w // 2, 2).mean((1, 3))
    valid = frame["valid"].reshape(h // 2, 2, w // 2, 2).all((1, 3))
    # One fixed radiometric compression for both inputs, not framewise normalization.
    return cv2.resize(
        np.log1p(value / 10000).astype(np.float32), shape, interpolation=cv2.INTER_AREA
    ), cv2.resize(valid.astype(np.float32), shape, interpolation=cv2.INTER_AREA) > 0.999


def estimate_motion(reference, candidate, mode):
    import cv2

    h, w = reference["rate"].shape
    shape = (min(640, w // 2), min(360, h // 2))
    if mode == "global":
        # Reuse the existing native even-integer RAW scoring, on explicit
        # distributed native windows so its work cap is unchanged. Invalid
        # window borders prevent fictitious correspondences across seams.
        from .reference_hdr import _translation_for_frame, default_translation_profile

        side = min(64, (h // 4) // 2 * 2, (w // 4) // 2 * 2)
        if side < 12:
            return {
                "flow": np.zeros((shape[1], shape[0], 2), np.float32),
                "trusted": np.zeros((shape[1], shape[0]), bool),
                "summary": {"status": "insufficient_native_windows", "ground_truth_used": False},
            }
        rows = [int(v) // 2 * 2 for v in np.linspace(0, h - side, 4)]
        cols = [int(v) // 2 * 2 for v in np.linspace(0, w - side, 4)]
        packed = {}
        for key in ("rate", "variance", "valid"):
            packed[key] = np.stack(
                [
                    np.block([[f[key][r : r + side, c : c + side] for c in cols] for r in rows])
                    for f in (reference, candidate)
                ]
            )
        for r in range(4):
            for c in range(4):
                tile = packed["valid"][:, r * side : (r + 1) * side, c * side : (c + 1) * side]
                tile[:, :4, :] = False
                tile[:, -4:, :] = False
                tile[:, :, :4] = False
                tile[:, :, -4:] = False
        shift, status, score, gap, count = _translation_for_frame(
            packed["rate"],
            packed["variance"],
            packed["valid"],
            0,
            1,
            default_translation_profile()["registration"],
        )
        flow = np.empty((shape[1], shape[0], 2), np.float32)
        flow[:] = [shift[1] * shape[0] / w, shift[0] * shape[1] / h]
        return {
            "flow": flow,
            "trusted": np.full(flow.shape[:2], status in {"identity_best", "translated"}, bool),
            "summary": {
                "status": status,
                "mode": "existing_even_native_translation_on_distributed_windows",
                "native_shift_yx": list(shift),
                "score": score,
                "score_gap": None if score is None or gap is None else gap - score,
                "common_native_sites": count,
                "native_window_side": side,
                "native_window_count": 16,
                "ground_truth_used": False,
            },
        }
    a, va = guide(reference, shape)
    b, vb = guide(candidate, shape)
    a *= 256.0
    b *= 256.0  # Fixed log-rate scale suitable for Farneback derivative units.
    support = va & vb
    if support.mean() < 0.02 or np.std(a[support]) < 1e-6 or np.std(b[support]) < 1e-6:
        return {
            "flow": np.zeros((*a.shape, 2), np.float32),
            "trusted": np.zeros(a.shape, bool),
            "summary": {"status": "insufficient_guide_support", "ground_truth_used": False},
        }
    aa, bb = np.where(support, a, 0), np.where(support, b, 0)
    shift, response = cv2.phaseCorrelate(
        aa - aa[support].mean(), bb - bb[support].mean(), cv2.createHanningWindow(shape, cv2.CV_32F)
    )
    global_flow = np.empty((*a.shape, 2), np.float32)
    global_flow[:] = shift
    # Flow is reference -> candidate sampling coordinates, no GT input.
    f = cv2.calcOpticalFlowFarneback(
        a, b, global_flow.copy(), 0.5, 3, 21, 5, 7, 1.5, cv2.OPTFLOW_USE_INITIAL_FLOW
    )
    back = cv2.calcOpticalFlowFarneback(
        b, a, -global_flow.copy(), 0.5, 3, 21, 5, 7, 1.5, cv2.OPTFLOW_USE_INITIAL_FLOW
    )
    yy, xx = np.indices(a.shape, dtype=np.float32)
    mx, my = xx + f[..., 0], yy + f[..., 1]
    reverse = cv2.remap(
        back, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=10000
    )
    trusted = support & (np.linalg.norm(f + reverse, axis=-1) <= 0.75) & (response >= 0.05)
    # Reject pronounced loss of guide structure; this is a blur proxy, not inversion.
    gb = cv2.remap(
        cv2.GaussianBlur(np.hypot(*np.gradient(b)), (5, 5), 0),
        mx,
        my,
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )
    ga = cv2.GaussianBlur(np.hypot(*np.gradient(a)), (5, 5), 0)
    trusted &= (ga < 5.12) | (gb >= 0.7 * ga)
    magnitude = np.hypot(f[..., 0] * w / shape[0], f[..., 1] * h / shape[1])
    trusted &= magnitude <= 64
    return {
        "flow": f,
        "trusted": trusted,
        "summary": {
            "status": "estimated_from_acquired_raw",
            "mode": mode,
            "phase_response": float(response),
            "global_shift_guide_xy": list(shift),
            "guide_shape": list(a.shape),
            "trusted_fraction": float(trusted.mean()),
            "maximum_native_displacement": 64,
            "ground_truth_used": False,
            "blur_check": "guide_structure_loss_proxy_not_deblurring",
        },
    }


def warp_cfa_tile(frame, motion, shape, bounds):
    """Bilinear sampling within each separate R/Gr/Gb/B plane.

    Return diagonal noise propagation sum(w² var), plus covariance-safe upper
    bound (sum(w sqrt(var)))² for spatial residual testing. Zero-weight neighbors
    do not invalidate exact integer coordinates.
    """
    r0, r1, c0, c1 = bounds
    yy, xx = np.indices((r1 - r0, c1 - c0), dtype=float)
    yy += r0
    xx += c0
    flow = motion["flow"]
    gh, gw = flow.shape[:2]
    gy = (yy + 0.5) * gh / shape[0] - 0.5
    gx = (xx + 0.5) * gw / shape[1] - 0.5
    dy = map_coordinates(flow[..., 1], [gy, gx], order=1, mode="nearest") * shape[0] / gh
    dx = map_coordinates(flow[..., 0], [gy, gx], order=1, mode="nearest") * shape[1] / gw
    trust = (
        map_coordinates(motion["trusted"].astype(float), [gy, gx], order=1, mode="nearest") > 0.999
    )
    out = {
        k: np.zeros(yy.shape, dtype=bool if k == "valid" else float)
        for k in ("rate", "variance", "variance_bound", "valid")
    }
    for pr in (0, 1):
        for pc in (0, 1):
            s = np.s_[pr::2, pc::2]
            plane = {k: frame[k][pr::2, pc::2] for k in ("rate", "variance", "valid")}
            ry, rx = (yy[s] + dy[s] - pr) / 2, (xx[s] + dx[s] - pc) / 2
            iy, ix = np.floor(ry).astype(int), np.floor(rx).astype(int)
            fy, fx = ry - iy, rx - ix
            rate = np.zeros(ry.shape)
            var = rate.copy()
            std = rate.copy()
            valid = trust[s].copy()
            for oy, ox, weight in (
                (0, 0, (1 - fy) * (1 - fx)),
                (0, 1, (1 - fy) * fx),
                (1, 0, fy * (1 - fx)),
                (1, 1, fy * fx),
            ):
                y, x = iy + oy, ix + ox
                inside = (
                    (y >= 0)
                    & (x >= 0)
                    & (y < plane["rate"].shape[0])
                    & (x < plane["rate"].shape[1])
                )
                yc, xc = (
                    np.clip(y, 0, plane["rate"].shape[0] - 1),
                    np.clip(x, 0, plane["rate"].shape[1] - 1),
                )
                valid &= (weight < 1e-12) | (inside & plane["valid"][yc, xc])
                rate += weight * plane["rate"][yc, xc]
                var += weight**2 * plane["variance"][yc, xc]
                std += weight * np.sqrt(plane["variance"][yc, xc])
            out["rate"][s], out["variance"][s], out["variance_bound"][s], out["valid"][s] = (
                rate,
                var,
                std**2,
                valid,
            )
    return out


def merge(reference, candidate, *, profile="local", static_input=False, tile_side=256):
    if profile not in PROFILES or (profile == "static" and not static_input):
        raise ValueError("Static HDR requires an explicitly declared common static input.")
    if tile_side < 4 or tile_side % 2 or tile_side > 512:
        raise ValueError("Choose an even fusion tile side in 4..512.")
    shape = reference["rate"].shape
    if any(
        frame[k].shape != shape
        for frame in (reference, candidate)
        for k in ("rate", "variance", "valid")
    ):
        raise ValueError("Matched unchanged native geometry required.")
    if profile != "static":
        motion = estimate_motion(reference, candidate, profile)
    else:
        motion = None
    result = {
        k: np.empty(
            shape,
            dtype=bool if k in ("valid", "fallback") else np.uint8 if k == "reason" else float,
        )
        for k in ("rate", "variance", "valid", "fallback", "reason", "long_weight")
    }
    for r in range(0, shape[0], tile_side):
        for c in range(0, shape[1], tile_side):
            r1, c1 = min(r + tile_side, shape[0]), min(c + tile_side, shape[1])
            a, b, d, e = max(0, r - 2), min(shape[0], r1 + 2), max(0, c - 2), min(shape[1], c1 + 2)
            s = np.s_[a:b, d:e]
            core = np.s_[r - a : r1 - a, c - d : c1 - d]
            dst = np.s_[r:r1, c:c1]
            ref = {k: v[s] for k, v in reference.items()}
            long = (
                {
                    **{k: v[s] for k, v in candidate.items()},
                    "variance_bound": candidate["variance"][s],
                }
                if motion is None
                else warp_cfa_tile(candidate, motion, shape, (a, b, d, e))
            )
            difference = np.abs(ref["rate"] - long["rate"])
            match = difference <= 5 * np.sqrt(ref["variance"] + long["variance_bound"])
            rejected = (
                maximum_filter((~match | ~long["valid"]).astype(np.uint8), size=3, mode="nearest")
                > 0
            )
            if profile == "static":
                good = ref["valid"] & long["valid"]
                valid = ref["valid"] | long["valid"]
                w = np.where(
                    good,
                    ref["variance"] / (ref["variance"] + long["variance"]),
                    np.where(~ref["valid"] & long["valid"], 1.0, 0.0),
                )
            else:
                good = ref["valid"] & ~rejected & long["valid"]
                valid = ref["valid"]
                w = np.where(good, ref["variance"] / (ref["variance"] + long["variance"]), 0.0)
            rate = (1 - w) * ref["rate"] + w * long["rate"]
            variance = (1 - w) ** 2 * ref["variance"] + w**2 * long["variance"]
            rate = np.where(valid, rate, np.nan)
            variance = np.where(valid, variance, np.nan)
            reason = np.where(
                ~valid, 1, np.where(~long["valid"], 2, np.where(rejected, 3, 0))
            ).astype(np.uint8)
            for k, v in dict(
                rate=rate,
                variance=variance,
                valid=valid,
                fallback=valid & (w == 0),
                reason=reason,
                long_weight=w,
            ).items():
                result[k][dst] = v[core]
    result["summary"] = {
        "schema": "camera_road_hdr_native_fusion_v1",
        "profile": profile,
        "tile_side": tile_side,
        "reference_recovery": profile == "static",
        "fallback_fraction": float(result["fallback"].mean()),
        "valid_fraction": float(result["valid"].mean()),
        "reason_codes": {
            "0": "accepted",
            "1": "reference_invalid",
            "2": "candidate_invalid_or_registration_rejected",
            "3": "noise_normalized_residual_guard",
        },
        "motion": None if motion is None else motion["summary"],
        "variance_scope": (
            "conditional plug-in independent acquisition; "
            "interpolation covariance not full uncertainty"
        ),
    }
    if motion is not None:
        result["flow"] = motion["flow"]
        result["flow_trusted"] = motion["trusted"]
    return result


def render_common(frame, sensor, coordinates, bounds, profile):
    """One electron-rate color/display pipeline for every arm, native demosaic."""
    from pyisetcam.ip import _ie_bilinear
    from pyisetcam.utils import linear_to_srgb

    from .road_design_compute import to_common_grid

    shape = frame["rate"].shape
    linear = np.empty((*shape, 3), float)
    for r in range(0, shape[0], 512):
        for c in range(0, shape[1], 512):
            r1, c1 = min(r + 512, shape[0]), min(c + 512, shape[1])
            a, b, d, e = max(0, r - 2), min(shape[0], r1 + 2), max(0, c - 2), min(shape[1], c1 + 2)
            rate = np.nan_to_num(frame["rate"][a:b, d:e])
            pattern = np.array([[1, 2], [2, 3]])
            planes = np.zeros((*rate.shape, 3), float)
            cfa = np.tile(pattern, (rate.shape[0] // 2, rate.shape[1] // 2))
            for k in range(3):
                planes[..., k] = np.where(cfa == k + 1, rate, 0)
            value = (_ie_bilinear(planes, pattern) * profile["white_balance_rgb"]) @ np.asarray(
                profile["ccm"]
            ).T
            linear[r:r1, c:c1] = value[r - a : r1 - a, c - d : c1 - d]
    common = to_common_grid(linear, coordinates, bounds)
    # Same invalid-support stencil, including anti-alias dependencies, in every arm.
    invalid = maximum_filter((~frame["valid"]).astype(float), size=3, mode="reflect")
    invalid_common = to_common_grid(invalid, coordinates, bounds)
    valid = np.isfinite(invalid_common) & (invalid_common < 1e-6)
    x = np.maximum(common, 0) * profile["reference_exposure_s"] * profile["tone"]["exposure_scale"]
    display = np.clip(x / (1 + x) * (1 + x / profile["tone"]["white_point"] ** 2), 0, 1)
    display[~valid] = 0
    return common.astype(np.float32), valid, np.rint(linear_to_srgb(display) * 255).astype(np.uint8)

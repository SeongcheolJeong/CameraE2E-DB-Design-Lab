"""RAW-only fusion constrained to the usable samples of one explicit reference.

Agreement permits averaging; it does not establish motion correspondence. The
fixed 3x3 disagreement guard and reference fallback make no registration or
complete deghosting claim. A clipped reference is never recovered from another
frame. All uncertainty is conditional plug-in noise, not physical validation.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from . import hdr

SCHEMA = "camera_reference_hdr_merge_v1"
V2_SCHEMA = "camera_reference_hdr_merge_v2"
TRANSLATION_SCHEMA = "camera_reference_hdr_merge_global_translation_v1"
MAX_FRAMES = 16
MAX_REGISTRATION_WORK = 24_000_000


def default_profile():
    return {
        "schema": SCHEMA,
        "reference_frame_index": 0,
        "base_profile": hdr.default_profile(),
    }


def default_profile_v2():
    from . import nonlinear_hdr

    return {"schema": V2_SCHEMA, "reference_frame_index": 0,
            "base_profile": nonlinear_hdr.default_profile()}


def default_translation_profile():
    """Opt-in RAW-only integer translation for a global-shutter bracket."""
    return {
        "schema": TRANSLATION_SCHEMA,
        "reference_frame_index": 0,
        "base_profile": hdr.default_profile(),
        "registration": {
            "max_shift_px": 4,
            "minimum_common_pixels": 64,
            "maximum_mean_clipped_chi2": 4.0,
            "minimum_score_gap": 3.0,
        },
    }


def validate_profile(profile):
    if not isinstance(profile, Mapping) or profile.get("schema") not in (
        SCHEMA, V2_SCHEMA, TRANSLATION_SCHEMA
    ):
        raise ValueError("Unsupported reference HDR profile schema")
    translated = profile["schema"] == TRANSLATION_SCHEMA
    expected_fields = set(default_translation_profile() if translated else default_profile())
    if set(profile) != expected_fields:
        raise ValueError("Reference HDR profile fields differ from its declared schema")
    index = profile["reference_frame_index"]
    if type(index) is not int or not 0 <= index < MAX_FRAMES:
        raise ValueError("reference_frame_index must be an integer in [0,15]")
    from . import nonlinear_hdr

    base_module = nonlinear_hdr if profile["schema"] == V2_SCHEMA else hdr
    selected = {
        "schema": profile["schema"],
        "reference_frame_index": index,
        "base_profile": base_module.validate_profile(profile["base_profile"]),
    }
    if translated:
        registration = profile["registration"]
        if (not isinstance(registration, Mapping)
                or set(registration) != set(default_translation_profile()["registration"])):
            raise ValueError("Translation registration requires its exact fields")
        radius = registration["max_shift_px"]
        minimum = registration["minimum_common_pixels"]
        if type(radius) is not int or radius not in (2, 4, 6, 8):
            raise ValueError("max_shift_px must be an even integer in {2,4,6,8}")
        if type(minimum) is not int or not 32 <= minimum <= hdr.MAX_FRAME_PIXELS:
            raise ValueError("minimum_common_pixels must be an integer in [32,1048576]")
        selected["registration"] = {
            "max_shift_px": radius,
            "minimum_common_pixels": minimum,
            "maximum_mean_clipped_chi2": hdr._number(
                registration["maximum_mean_clipped_chi2"],
                "maximum_mean_clipped_chi2", positive=True,
            ),
            "minimum_score_gap": hdr._number(
                registration["minimum_score_gap"], "minimum_score_gap", positive=True,
            ),
        }
        if (selected["registration"]["maximum_mean_clipped_chi2"] > 25
                or selected["registration"]["minimum_score_gap"] > 25):
            raise ValueError("Translation score limits cannot exceed clipped chi2=25")
    return selected


def _shift_to_reference(array, dy, dx, fill):
    """Reference[y,x] observes source[y+dy,x+dx]; no Bayer interpolation."""
    height, width = array.shape
    shifted = np.full(array.shape, fill, dtype=array.dtype)
    y0, y1 = max(0, -dy), min(height, height - dy)
    x0, x1 = max(0, -dx), min(width, width - dx)
    if y1 > y0 and x1 > x0:
        shifted[y0:y1, x0:x1] = array[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return shifted


def _translation_for_frame(rates, variances, valid, reference_index, frame_index, spec):
    """Score all shifts on identical valid interior RAW sites, without truth."""
    radius = spec["max_shift_px"]
    height, width = rates.shape[1:]
    shifts = [(0, 0)] + [(dy, dx) for dy in range(-radius, radius + 1, 2)
                         for dx in range(-radius, radius + 1, 2) if (dy, dx) != (0, 0)]
    work = len(shifts) * int(np.prod(rates.shape))
    if work > MAX_REGISTRATION_WORK:
        raise ValueError("Translation registration exceeds bounded comparison work")
    if height <= 2 * radius or width <= 2 * radius:
        return (0, 0), "insufficient_interior", None, None, 0
    core = (slice(radius, height - radius), slice(radius, width - radius))
    reference_valid = valid[reference_index][core]
    common = reference_valid.copy()
    frame_valid = valid[frame_index]
    for dy, dx in shifts:
        common &= frame_valid[radius + dy:height - radius + dy,
                              radius + dx:width - radius + dx]
    count = int(common.sum())
    if count < spec["minimum_common_pixels"]:
        return (0, 0), "insufficient_common_valid", None, None, count
    reference_rate = rates[reference_index][core][common]
    reference_variance = variances[reference_index][core][common]
    scores = []
    for dy, dx in shifts:
        candidate_rate = rates[frame_index][
            radius + dy:height - radius + dy,
            radius + dx:width - radius + dx,
        ][common]
        candidate_variance = variances[frame_index][
            radius + dy:height - radius + dy,
            radius + dx:width - radius + dx,
        ][common]
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            chi2 = (reference_rate - candidate_rate) ** 2 / (
                reference_variance + candidate_variance
            )
        if not np.isfinite(chi2).all():
            raise ValueError("Translation score exceeds its finite numerical domain")
        scores.append(float(np.mean(np.minimum(chi2, 25.0))))
    ordered = np.argsort(scores, kind="stable")
    best, second = int(ordered[0]), int(ordered[1])
    best_score, second_score = scores[best], scores[second]
    if best == 0 and best_score <= spec["maximum_mean_clipped_chi2"]:
        return (0, 0), "identity_best", best_score, second_score, count
    if (best_score <= spec["maximum_mean_clipped_chi2"]
            and second_score - best_score >= spec["minimum_score_gap"]):
        return shifts[best], "translated", best_score, second_score, count
    # Do not contribute an unregistered frame when displacement is suspected.
    return (0, 0), "rejected_ambiguous", best_score, second_score, count


def _merge_translated(original, summary, selected):
    """Align only reliably identifiable whole-image even-pixel shifts."""
    rates = original["exposure_rate"]
    variances = original["exposure_estimated_variance"]
    valid = original["exposure_valid_mask"]
    count, height, width = rates.shape
    reference = selected["reference_frame_index"]
    spec = selected["registration"]
    aligned_rates = np.zeros_like(rates)
    aligned_variances = np.zeros_like(variances)
    aligned_valid = np.zeros_like(valid)
    registered = np.zeros_like(valid)
    diagnostics = []
    for frame in range(count):
        if frame == reference:
            shift, status, score, runner, support = (0, 0), "reference", None, None, 0
        else:
            shift, status, score, runner, support = _translation_for_frame(
                rates, variances, valid, reference, frame, spec,
            )
        if frame == reference or status in {"identity_best", "translated"}:
            aligned_rates[frame] = _shift_to_reference(rates[frame], *shift, 0.0)
            aligned_variances[frame] = _shift_to_reference(variances[frame], *shift, 0.0)
            aligned_valid[frame] = _shift_to_reference(valid[frame], *shift, False)
            registered[frame] = aligned_valid[frame]
        diagnostics.append({
            "frame_index": frame, "status": status, "source_offset_yx_px": list(shift),
            "mean_clipped_chi2": score, "runner_up_mean_clipped_chi2": runner,
            "common_valid_interior_pixels": support,
        })
    reference_valid = valid[reference].copy()
    usable = aligned_valid.sum(axis=0)
    unknown = usable < 2
    consistent = ~unknown
    for i in range(count):
        for j in range(i + 1, count):
            both = aligned_valid[i] & aligned_valid[j]
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                threshold = selected["base_profile"]["consistency_sigma"] * np.hypot(
                    np.sqrt(aligned_variances[i]), np.sqrt(aligned_variances[j])
                )
                difference = np.abs(aligned_rates[i] - aligned_rates[j])
            if (not np.isfinite(threshold[both]).all()
                    or not np.isfinite(difference[both]).all()):
                raise ValueError("Aligned temporal consistency exceeds finite domain")
            consistent &= ~both | (difference <= threshold)
    disagreement = ~consistent & ~unknown
    padded = np.pad(disagreement, 1, mode="reflect")
    guard = np.logical_or.reduce([
        padded[y:y + height, x:x + width] for y in range(3) for x in range(3)
    ])
    fused = reference_valid & ~guard & ~unknown
    fallback = reference_valid & guard
    reference_only = reference_valid & ~guard & unknown
    minimum = np.min(np.where(aligned_valid, aligned_variances, np.inf), axis=0)
    reciprocal = np.zeros_like(aligned_rates)
    np.divide(minimum[None], aligned_variances, out=reciprocal,
              where=aligned_valid)
    weights = np.zeros_like(aligned_rates)
    normalizer = reciprocal.sum(axis=0)
    np.divide(reciprocal, normalizer[None], out=weights,
              where=fused[None])
    weights[reference, fallback | reference_only] = 1.0
    merged = np.sum(weights * aligned_rates, axis=0)
    variance = np.sum(weights**2 * aligned_variances, axis=0)
    merged[~reference_valid] = np.nan
    variance[~reference_valid] = np.nan
    if (not np.isfinite(merged[reference_valid]).all()
            or not np.isfinite(variance[reference_valid]).all()):
        raise ValueError("Registered reference HDR exceeds its finite domain")
    _, pattern = hdr._geometry(summary["geometry"], (height, width))
    linear, rgb_valid, preview = hdr._rgb(
        merged, reference_valid, pattern, selected["base_profile"]
    )
    arrays = {
        "exposure_rate": rates, "exposure_estimated_variance": variances,
        "exposure_valid_mask": valid,
        "aligned_exposure_rate": aligned_rates,
        "aligned_exposure_estimated_variance": aligned_variances,
        "aligned_exposure_valid_mask": aligned_valid,
        "registration_contribution_mask": registered,
        "weights": weights, "merged_rate": merged, "estimated_variance": variance,
        "valid_mask": reference_valid.copy(), "reference_valid_mask": reference_valid,
        "temporal_consistency_mask": consistent,
        "temporal_consistency_unknown_mask": unknown,
        "disagreement_mask": disagreement,
        "expanded_disagreement_mask": guard,
        "fused_mask": fused, "reference_fallback_mask": fallback,
        "reference_only_unknown_mask": reference_only,
        "linear_rgb": linear, "linear_rgb_valid_mask": rgb_valid, "rgb": preview,
    }
    summary.update(
        schema="camera_reference_hdr_merge_result_global_translation_v1",
        method="reference_constrained_raw_even_pixel_global_translation",
        profile=selected,
        registration={
            "scope": "one whole-image even-pixel shift per nonreference frame",
            "score": "mean(min((reference_rate-shifted_rate)^2/(reference_variance+"
                     "shifted_variance),25)) on identical common valid interior sites",
            "shifts": diagnostics,
            "rejected_frame_policy": "no contribution anywhere from an ambiguous frame",
            "geometry": "reference[y,x] receives candidate[y+dy,x+dx]; no interpolation",
        },
        reference_frame_index=reference,
        reference_valid_pixels=int(reference_valid.sum()),
        reference_invalid_pixels=int((~reference_valid).sum()),
        fused_pixels=int(fused.sum()),
        reference_fallback_pixels=int(fallback.sum()),
        reference_only_unknown_pixels=int(reference_only.sum()),
        disagreement_pixels=int(disagreement.sum()),
        expanded_disagreement_pixels=int(guard.sum()),
        temporal_consistent_pixels=int(consistent.sum()),
        temporal_inconsistent_pixels=int(disagreement.sum()),
        temporal_consistency_unknown_pixels=int(unknown.sum()),
        single_usable_exposure_pixels=int((usable == 1).sum()),
        valid_pixels=int(reference_valid.sum()),
        invalid_pixels=int((~reference_valid).sum()),
        linear_rgb_valid_pixels=int(rgb_valid.sum()),
        reference_clipping_recovery=False,
        motion_alignment="bounded_integer_global_translation_from_raw",
        disagreement_guard="OR over the reflected 3x3 reference-RAW neighborhood",
        reference_policy="Reference invalid: NaN/false/zero weights; never fill from another frame",
        fusion_policy="Only accepted registered RAW rates enter inverse-variance fusion; "
                      "guarded disagreement uses reference only",
        consistency_scope="All usable registered RAW pairs; unknown below two exposures; "
                          "not motion truth",
        consistency_test="all usable aligned pairs: "
                         "abs(rate_i-rate_j)<=sigma*sqrt(var_i+var_j)",
        estimated_variance_scope="Plug-in independent shot/read/quantization noise conditional "
                                 "on registration and selected weights; excludes registration "
                                 "error, selection bias and motion error",
        weight_normalization="Sum=1 on usable reference pixels; zero when reference is invalid",
        limitations=[
            "Only global-shutter, constant-light whole-image even-pixel translation is considered.",
            "RAW-only shift scoring cannot prove correspondence or complete deghosting.",
            "Local motion, occlusion, rolling distortion, subpixel motion and intra-exposure blur "
            "can remain or force conservative reference-only output.",
            "An ambiguous global shift removes that frame from all fusion pixels.",
            "Censored reference pixels remain unknown; bright-region headroom is not recovered.",
            "No PWM fusion, vendor LFM or measured temporal/calibration uncertainty is claimed.",
        ],
    )
    summary["array_units"].update({
        "aligned_exposure_rate": "electron/pixel/s",
        "aligned_exposure_estimated_variance": "(electron/pixel/s)^2",
        "aligned_exposure_valid_mask": "boolean",
        "registration_contribution_mask": "boolean",
        "reference_valid_mask": "boolean", "disagreement_mask": "boolean",
        "expanded_disagreement_mask": "boolean", "fused_mask": "boolean",
        "reference_fallback_mask": "boolean", "reference_only_unknown_mask": "boolean",
    })
    return {"arrays": arrays, "summary": summary}


def merge_reference_raw_bracket(raw, exposure_s, calibrations, *, geometry, profile):
    """Fuse agreeing RAW rates, retain the reference at disagreement, never fill it.

    Caller establishes independent acquisitions with unchanged native geometry
    and constant illumination. The input contains no motion or simulation truth.
    ``temporal_consistency_*`` diagnose all usable exposure pairs, independently
    of the reference/output validity. A single usable sample remains unknown.
    """
    selected = validate_profile(profile)
    if not isinstance(raw, np.ndarray) or raw.ndim != 3 or not 2 <= len(raw) <= MAX_FRAMES:
        raise ValueError("Reference HDR requires integer RAW from 2..16 exposures")
    index = selected["reference_frame_index"]
    if index >= len(raw):
        raise ValueError("reference_frame_index is outside the actual RAW bracket")
    if selected["schema"] == TRANSLATION_SCHEMA:
        count, height, width = raw.shape
        radius = selected["registration"]["max_shift_px"]
        if (height - 2 * radius) * (width - 2 * radius) < (
            selected["registration"]["minimum_common_pixels"]
        ):
            raise ValueError("RAW global-translation HDR has insufficient native interior")
        if count * height * width * (radius + 1) ** 2 > MAX_REGISTRATION_WORK:
            raise ValueError("RAW global-translation HDR exceeds bounded comparison work")

    # Reuse the unchanged, independently tested calibration/censoring method.
    # Its provisional blend is discarded; no oracle or alignment is injected.
    from . import nonlinear_hdr

    nonlinear = selected["schema"] == V2_SCHEMA
    base_module = nonlinear_hdr if nonlinear else hdr
    base = base_module.merge_raw_bracket(
        raw, exposure_s, calibrations, geometry=geometry, profile=selected["base_profile"]
    )
    original, summary = base["arrays"], base["summary"]
    if selected["schema"] == TRANSLATION_SCHEMA:
        return _merge_translated(original, summary, selected)
    reference_valid = original["exposure_valid_mask"][index].copy()
    disagreement = (
        ~original["temporal_consistency_mask"]
        & ~original["temporal_consistency_unknown_mask"]
    )
    rows, cols = reference_valid.shape
    padded = np.pad(disagreement, 1, mode="reflect")
    guarded = np.logical_or.reduce([
        padded[y:y + rows, x:x + cols] for y in range(3) for x in range(3)
    ])
    fallback = reference_valid & guarded
    fused = (reference_valid & ~guarded
             & ~original["temporal_consistency_unknown_mask"])
    reference_only = (reference_valid & ~guarded
                      & original["temporal_consistency_unknown_mask"])

    weights = np.where(fused[None, ...], original["weights"], 0.0)
    weights[index, fallback | reference_only] = 1.0
    rates = original["exposure_rate"]
    variances = original["exposure_estimated_variance"]
    if nonlinear:
        rates = np.where(original["exposure_valid_mask"], rates, 0.)
        variances = np.where(original["exposure_valid_mask"], variances, 0.)
    merged = np.sum(weights * rates, axis=0)
    variance = np.sum(weights**2 * variances, axis=0)
    merged[~reference_valid] = np.nan
    variance[~reference_valid] = np.nan
    if (not np.isfinite(merged[reference_valid]).all()
            or not np.isfinite(variance[reference_valid]).all()):
        raise ValueError("Reference HDR fusion exceeds its numerical domain")
    _, pattern = hdr._geometry(geometry, (rows, cols))
    linear, rgb_valid, preview = hdr._rgb(
        merged, reference_valid, pattern, selected["base_profile"]
    )
    arrays = {
        **{key: original[key] for key in (
            "exposure_rate", "exposure_estimated_variance", "exposure_valid_mask",
            "temporal_consistency_mask", "temporal_consistency_unknown_mask",
        )},
        "weights": weights,
        "merged_rate": merged,
        "estimated_variance": variance,
        "valid_mask": reference_valid.copy(),
        "reference_valid_mask": reference_valid,
        "disagreement_mask": disagreement,
        "expanded_disagreement_mask": guarded,
        "fused_mask": fused,
        "reference_fallback_mask": fallback,
        "reference_only_unknown_mask": reference_only,
        "linear_rgb": linear,
        "linear_rgb_valid_mask": rgb_valid,
        "rgb": preview,
    }
    summary.update(
        schema="camera_reference_hdr_merge_result_v1",
        method="reference_constrained_inverse_variance_raw",
        profile=selected,
        reference_frame_index=index,
        reference_valid_pixels=int(reference_valid.sum()),
        reference_invalid_pixels=int((~reference_valid).sum()),
        fused_pixels=int(fused.sum()),
        reference_fallback_pixels=int(fallback.sum()),
        reference_only_unknown_pixels=int(reference_only.sum()),
        disagreement_pixels=int(disagreement.sum()),
        expanded_disagreement_pixels=int(guarded.sum()),
        valid_pixels=int(reference_valid.sum()),
        invalid_pixels=int((~reference_valid).sum()),
        linear_rgb_valid_pixels=int(rgb_valid.sum()),
        reference_clipping_recovery=False,
        motion_alignment=False,
        disagreement_guard="OR over the reflected 3x3 native-RAW neighborhood",
        reference_policy="Reference invalid: NaN/false/zero weights; never fill from another frame",
        fusion_policy="All usable pairs agree outside guard: inverse variance; "
                      "guarded disagreement: reference only; one usable reference: unknown",
        consistency_scope="All usable RAW pairs independently of reference/output validity; "
                          "unknown below two usable exposures; not motion truth",
        estimated_variance_scope="Plug-in independent shot/read/quantization noise conditional "
                                 "on selected weights; excludes selection bias and motion error",
        weight_normalization="Sum=1 on usable reference pixels; zero when reference is invalid",
        limitations=[
            "RAW agreement cannot establish scene correspondence or exclude motion/ghosts.",
            "Disagreement may arise from motion, illumination, noise or calibration error.",
            "No registration, intra-exposure blur correction or complete deghosting is applied.",
            "Reference floor/full-well/ADC censoring is preserved, not recovered by another frame.",
            "No expansion of valid bright-region dynamic range beyond the reference is claimed.",
            "Stable regions can combine exposure noise; conditional variance excludes gate bias.",
            "A single usable exposure cannot establish temporal consistency.",
            "Read noise can conceal clipping; RAW censoring is a declared estimate.",
            "Caller must establish clocks, nonoverlap, independent resets and constant light.",
            "No physical validation, calibrated total uncertainty, vendor LFM "
            "or hardware equivalence.",
        ],
    )
    summary["array_units"].update({
        name: "boolean" for name in arrays if name.endswith("mask")
    })
    if nonlinear:
        bound = np.sum(weights * np.where(original["exposure_valid_mask"],
                       original["exposure_quantization_bound_rate"], 0.), axis=0)
        bound[~reference_valid] = np.nan
        if not np.isfinite(bound[reference_valid]).all():
            raise ValueError("Reference HDR quantization bound exceeds finite domain")
        arrays.update(quantization_bound_rate=bound,
            exposure_quantization_bound_rate=original["exposure_quantization_bound_rate"],
            exposure_quantization_knot_crossing_mask=(
                original["exposure_quantization_knot_crossing_mask"]))
        summary.update(schema="camera_reference_hdr_merge_result_v2",
            method="reference_constrained_local_variance_nonlinear_raw",
            estimated_variance_scope="Local inverse-slope read and plug-in independent shot noise "
            "conditional on selected weights; excludes selection bias, motion "
            "and inverse curvature bias",
            quantization_scope="Preimage bounds including declared float64 allowances; "
            "not noise variance or a formal IEEE interval proof")
        summary["array_units"].update(quantization_bound_rate="electron/pixel/s",
            exposure_quantization_bound_rate="electron/pixel/s",
            exposure_quantization_knot_crossing_mask="boolean")
    return {"arrays": arrays, "summary": summary}

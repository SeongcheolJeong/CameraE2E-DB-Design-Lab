"""Temporal-pixel noise, frozen feasibility and independent evaluation rules."""

from __future__ import annotations

import numpy as np

STATISTICS_METHOD = "centered_frame_residual_bootstrap_v2"


def _moments(frames, mask):
    values = np.asarray(frames, float)[:, np.asarray(mask, bool)]
    if values.shape[1] < 25 or values.shape[0] < 2 or not np.isfinite(values).all():
        raise ValueError("Temporal CNR requires >=2 finite frames and >=25 fixed mask pixels.")
    return values.mean(axis=1), (values * values).mean(axis=1), values @ values.T / values.shape[1]


def cnr_samples(frames, obj, bg, *, replicates=500, seed=0):
    if np.any(np.asarray(obj, bool) & np.asarray(bg, bool)):
        raise ValueError("Object and background masks must be disjoint.")
    n = frames.shape[0]
    om, os, og = _moments(frames, obj)
    bm, bs, bgm = _moments(frames, bg)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, (replicates, n))
    weights = np.zeros((replicates + 1, n))
    weights[0] = 1 / n
    for i, index in enumerate(indices, start=1):
        weights[i] = np.bincount(index, minlength=n) / n
    # This is the mean of each pixel's *temporal* unbiased variance. Spatial
    # texture/FPN contributes to Gram matrices and cancels from the variance.
    ov = (weights @ os - np.einsum("bi,ij,bj->b", weights, og, weights)) * n / (n - 1)
    bv = (weights @ bs - np.einsum("bi,ij,bj->b", weights, bgm, weights)) * n / (n - 1)
    # Raw empirical frame resampling shrinks expected temporal variance by
    # (n-1)/n. Bootstrap centered acquisition residuals after restoring their
    # sample variance; retain the original point estimate at index zero.
    ov[1:] *= n / (n - 1)
    bv[1:] *= n / (n - 1)
    signal = weights @ om - weights @ bm
    signal[1:] = signal[0] + np.sqrt(n / (n - 1)) * (signal[1:] - signal[0])
    denominator = np.sqrt(np.maximum(0.0, ov) + np.maximum(0.0, bv))
    if denominator[0] <= 1e-14:
        raise ValueError(
            "CNR is undefined for zero temporal variance; no spatial-noise substitute."
        )
    cnr = np.divide(
        abs(signal),
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > 1e-14,
    )
    return cnr, float(max(0.0, ov[0])), float(max(0.0, bv[0]))


def cnr_with_interval(frames, obj, bg, *, replicates=500, seed=0):
    samples, ov, bv = cnr_samples(frames, obj, bg, replicates=replicates, seed=seed)
    return {
        "cnr": float(samples[0]),
        "cnr_ci95": np.quantile(samples[1:], [0.025, 0.975]).tolist(),
        "mean_temporal_pixel_variance_object": ov,
        "mean_temporal_pixel_variance_background": bv,
        "bootstrap_cnr": samples[1:].tolist(),
        "bootstrap_unit": "centered independent acquisition residual; fixed pixel masks",
        "statistics_method": STATISTICS_METHOD,
        "bootstrap_variance_correction": frames.shape[0] / (frames.shape[0] - 1),
    }


def ratio_interval(candidate, baseline):
    if candidate["candidate_id"] == baseline["candidate_id"]:
        return {"ratio": 1.0, "ci95": [1.0, 1.0]}
    base = np.asarray(baseline["bootstrap_cnr"], float)
    cand = np.asarray(candidate["bootstrap_cnr"], float)
    if np.any(base <= 0) or baseline["cnr"] <= 0:
        raise ValueError("Baseline CNR cannot support a relative comparison.")
    # Permute baseline draws: captures have different native pixel counts and
    # independent seeds; do not imply a paired physical noise realization.
    ratios = cand / np.random.default_rng(42).permutation(base)
    return {
        "ratio": candidate["cnr"] / baseline["cnr"],
        "ci95": np.quantile(ratios, [0.025, 0.975]).tolist(),
    }


def choose_candidate(spec, records, *, sampling_ok):
    selection_ids = {image["id"] for image in spec["images"] if image["split"] == "selection"}
    if any(row["image_id"] not in selection_ids for row in records):
        raise ValueError("Held-out data cannot enter candidate selection.")
    rule = spec["selection_rule"]
    baseline = spec["baseline_id"]
    by_key = {
        (row["candidate_id"], row["image_id"], row["roi_id"], row["brightness"]): row
        for row in records
    }
    expected = [
        (image["id"], roi["id"], light)
        for image in spec["images"]
        if image["split"] == "selection"
        for roi in image["rois"]
        for light in spec["capture"]["brightness_multipliers"]
    ]
    ranked = []
    for candidate in spec["candidates"]:
        cid = candidate["id"]
        missing = [key for key in expected if (cid, *key) not in by_key]
        if missing:
            raise ValueError(f"Incomplete selection cohort for {cid}: {len(missing)} conditions.")
        rows = [by_key[(cid, *key)] for key in expected]
        failures, ratios, expand = [], [], []
        if not sampling_ok.get(cid, False):
            failures.append("representative source-grid convergence not established")
        for row in rows:
            base = by_key[(baseline, row["image_id"], row["roi_id"], row["brightness"])]
            if row["roi_kind"] == "highlight" and row["brightness"] == 4:
                if row["well_fraction"] > rule["highlight_well_fraction_max"]:
                    failures.append(row["image_id"] + ": highlight well saturation")
                if row["adc_fraction"] > rule["highlight_adc_fraction_max"]:
                    failures.append(row["image_id"] + ": highlight ADC saturation")
            if row["roi_kind"] == "object":
                if (
                    base["local_contrast"] <= 0
                    or row["local_contrast"] / base["local_contrast"]
                    < rule["contrast_retention_min"]
                ):
                    failures.append(row["image_id"] + ": contrast below baseline retention")
                if row["brightness"] == 0.25:
                    ratio = ratio_interval(row, base)
                    ratios.append({"image_id": row["image_id"], "roi_id": row["roi_id"], **ratio})
                    if cid != baseline and any(
                        ratio["ci95"][0] <= threshold <= ratio["ci95"][1]
                        for threshold in (1.0, 0.95, 1.10)
                    ):
                        expand.append({"image_id": row["image_id"], "roi_id": row["roi_id"]})
        low = [row for row in rows if row["roi_kind"] == "object" and row["brightness"] == 0.25]
        ranked.append(
            {
                "id": cid,
                "feasible": not failures,
                "failures": sorted(set(failures)),
                "worst_cnr": min(row["cnr"] for row in low),
                "worst_cnr_ci95": [
                    min(row["cnr_ci95"][0] for row in low),
                    min(row["cnr_ci95"][1] for row in low),
                ],
                "ratios_to_baseline": ratios,
                "expand_conditions": expand,
            }
        )
    feasible = sorted(
        (row for row in ranked if row["feasible"]), key=lambda row: (-row["worst_cnr"], row["id"])
    )
    selected = baseline
    reason = "No feasible replacement; baseline retained with failures disclosed."
    if feasible:
        best = feasible[0]
        baseline_row = next(row for row in ranked if row["id"] == baseline)
        if best["id"] == baseline:
            reason = (
                "Baseline maximizes the predeclared worst lowlight CNR among feasible candidates."
            )
        elif (
            not baseline_row["feasible"]
            or best["worst_cnr_ci95"][0] > baseline_row["worst_cnr_ci95"][1]
        ):
            selected = best["id"]
            reason = (
                "Feasible candidate has a resolved improvement in worst selection lowlight CNR."
            )
        else:
            reason = (
                "Worst-CNR intervals overlap; difference remains unresolved, baseline retained."
            )
    return {
        "selected_id": selected,
        "baseline_id": baseline,
        "reason": reason,
        "ranking": ranked,
        "used_image_ids": sorted(selection_ids),
        "evaluation_images_used": False,
    }


def evaluate_selection(spec, selected_id, records, *, sampling_ok):
    expected_ids = {image["id"] for image in spec["images"] if image["split"] == "evaluation"}
    if any(
        row["image_id"] not in expected_ids
        or row["candidate_id"] not in {selected_id, spec["baseline_id"]}
        for row in records
    ):
        raise ValueError(
            "Evaluation may contain only held-out images and the locked "
            "baseline/selected candidate."
        )
    baseline_id = spec["baseline_id"]
    by_key = {
        (row["candidate_id"], row["image_id"], row["roi_id"], row["brightness"]): row
        for row in records
    }
    expected = [
        (image["id"], roi["id"], light)
        for image in spec["images"]
        if image["split"] == "evaluation"
        for roi in image["rois"]
        for light in spec["capture"]["brightness_multipliers"]
    ]
    if any((cid, *key) not in by_key for cid in {baseline_id, selected_id} for key in expected):
        raise ValueError("Incomplete held-out cohort.")
    lowlight, contrasts, highlights, expand, ratio_draws = [], [], [], [], []
    for key in expected:
        candidate = by_key[(selected_id, *key)]
        base = by_key[(baseline_id, *key)]
        if candidate["roi_kind"] == "object":
            contrasts.append(candidate["local_contrast"] / base["local_contrast"])
            if key[2] == 0.25:
                ratio = ratio_interval(candidate, base)
                lowlight.append({"image_id": key[0], "roi_id": key[1], **ratio})
                if selected_id == baseline_id:
                    draws = np.ones(len(base["bootstrap_cnr"]))
                else:
                    rng = np.random.default_rng(len(ratio_draws) + 103)
                    draws = rng.permutation(candidate["bootstrap_cnr"]) / rng.permutation(
                        base["bootstrap_cnr"]
                    )
                ratio_draws.append(draws)
                if selected_id != baseline_id and any(
                    ratio["ci95"][0] <= limit <= ratio["ci95"][1] for limit in (0.95, 1.10)
                ):
                    expand.append({"image_id": key[0], "roi_id": key[1]})
        elif key[2] == 4:
            highlights.append(
                {
                    "image_id": key[0],
                    "well_fraction": candidate["well_fraction"],
                    "adc_fraction": candidate["adc_fraction"],
                    "baseline_well_fraction": base["well_fraction"],
                    "baseline_adc_fraction": base["adc_fraction"],
                }
            )
    acceptance = spec["acceptance"]
    median_gain = float(np.median([row["ratio"] for row in lowlight]) - 1)
    median_ci = np.quantile(np.median(ratio_draws, axis=0) - 1, [0.025, 0.975]).tolist()
    if selected_id != baseline_id and median_ci[0] <= 0.10 <= median_ci[1]:
        expand += [{"image_id": row["image_id"], "roi_id": row["roi_id"]} for row in lowlight]
    baseline_out_of_spec = any(
        row["baseline_well_fraction"] > acceptance["highlight_well_fraction_max"]
        or row["baseline_adc_fraction"] > acceptance["highlight_adc_fraction_max"]
        for row in highlights
    )
    passed = {
        "median_lowlight_cnr": median_ci[0] >= acceptance["lowlight_median_cnr_gain_min"],
        "individual_lowlight_cnr": all(
            row["ci95"][0] >= acceptance["individual_cnr_ratio_min"] for row in lowlight
        ),
        "local_contrast": min(contrasts) >= acceptance["contrast_ratio_min"],
        "highlight_well": max(row["well_fraction"] for row in highlights)
        <= acceptance["highlight_well_fraction_max"],
        "highlight_adc": max(row["adc_fraction"] for row in highlights)
        <= acceptance["highlight_adc_fraction_max"],
        "source_grid": sampling_ok.get(selected_id, False),
    }
    status = "improvement_confirmed" if all(passed.values()) else "conditional_selection"
    if selected_id == baseline_id:
        status = "baseline_retained"
    elif not passed["source_grid"] or median_ci[0] <= 0:
        status = "baseline_retained"
    return {
        "status": status,
        "selected_id": selected_id,
        "recommended_id": baseline_id if status == "baseline_retained" else selected_id,
        "median_lowlight_cnr_gain": median_gain,
        "median_lowlight_cnr_gain_ci95": median_ci,
        "uncertainty_scope": (
            "capture noise on these fixed held-out inputs; not road-population uncertainty"
        ),
        "baseline_out_of_spec": baseline_out_of_spec,
        "minimum_local_contrast_ratio": min(contrasts),
        "acceptance_checks": passed,
        "lowlight_comparisons": lowlight,
        "highlight_comparisons": highlights,
        "expand_conditions": expand,
        "held_out_image_ids": sorted(expected_ids),
        "reselection_performed": False,
    }

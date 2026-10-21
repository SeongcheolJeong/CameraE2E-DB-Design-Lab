"""Small preserved-RAW Reference ISP recipes and declared demo constraints."""

from __future__ import annotations

import copy
import itertools
import math

from . import reference_isp

RECIPES = ("noise_detail", "demosaic", "color_recalibration", "parameter_tuning")
CONSTRAINTS = {
    "texture_retention_min": 0.95,
    "halo_increase_max": 0.05,
    "delta_e_increase_max": 1.0,
    "noise_improvement_min": 0.01,
}
TUNING_CONSTRAINTS = {
    **CONSTRAINTS,
    "brightness_relative_min": 0.95,
    "brightness_relative_max": 1.05,
    "uniformity_increase_max": 0.05,
}
TUNING_AXES = {
    "raw_denoise.strength": [0.0, 0.2, 0.4],
    "lsc_tuning.strength": [0.85, 1.0],
    "linear_sharpen.amount": [0.0, 0.3],
}
TUNABLE_PATHS = (
    "raw_denoise.strength", "raw_denoise.range_floor_dn2",
    "linear_sharpen.amount", "linear_sharpen.threshold", "linear_sharpen.max_delta",
    "lsc_tuning.strength", "lsc_tuning.gain_cap",
)
MAX_TUNING_CANDIDATES = 12
AXIS_LABELS = {
    "raw_denoise.strength": "NR", "raw_denoise.range_floor_dn2": "floor",
    "linear_sharpen.amount": "detail", "linear_sharpen.threshold": "threshold",
    "linear_sharpen.max_delta": "max delta", "lsc_tuning.strength": "LSC",
    "lsc_tuning.gain_cap": "gain cap",
}
FIELD_IDS = [
    "center",
    "mid-right",
    "mid-top",
    "mid-left",
    "mid-bottom",
    "outer-ne",
    "outer-nw",
    "outer-sw",
    "outer-se",
]


def specification(payload, source_configuration):
    allowed = {"source_run_id", "source_candidate_id", "recipe", "name", "isp", "constraints",
               "parameter_axes"}
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or payload.get("recipe") not in RECIPES
    ):
        raise ValueError("Choose a Reference ISP recipe and an exact preserved source.")
    profile = reference_isp.default_profile(source_configuration["isp"])
    if payload.get("isp") is not None:
        profile = reference_isp.validate_profile(payload["isp"])
    profile["lsc_enabled"] = True
    tuning = payload["recipe"] == "parameter_tuning"
    if not tuning:
        if "parameter_axes" in payload:
            raise ValueError("Explicit parameter axes require the parameter_tuning recipe.")
        # Existing recipes retain their frozen full-map LSC comparison contract.
        profile.pop("lsc_tuning", None)
        profile["raw_denoise"]["strength"] = 0
        profile["linear_sharpen"].update(amount=0, threshold=0.01, max_delta=0.05)
    else:
        profile.setdefault("lsc_tuning", {"strength": 1.0, "gain_cap": 4.0})
    # All recipes use a fixed source display/color basis; only the explicit color recipe refits.
    source = source_configuration["isp"]
    for key in ("wb", "ccm", "tone_gamma", "display_white_level"):
        profile[key] = copy.deepcopy(source[key])
    profile["color_calibration_id"] = source.get("color_calibration_id")
    profile["demosaic_method"] = "adaptive_laplacian"
    values = payload.get("constraints", {})
    defaults = TUNING_CONSTRAINTS if tuning else CONSTRAINTS
    if not isinstance(values, dict) or set(values) - set(defaults):
        raise ValueError("Unknown ISP study constraints.")
    constraints = {**defaults, **values}
    for key, value in constraints.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("ISP constraints must be finite nonnegative numbers.")
        if key not in {"delta_e_increase_max", "brightness_relative_max"} and value > 1:
            raise ValueError("Fractional ISP constraints must be in [0,1].")
    if tuning and not 1 <= constraints["brightness_relative_max"] <= 2:
        raise ValueError("Maximum relative brightness must be in [1,2].")
    result = {
        "schema": "camera_reference_isp_study_v1",
        "recipe": payload["recipe"],
        "isp": profile,
        "constraints": constraints,
        "expected_field_ids": FIELD_IDS.copy(),
        "selection_scope": "same_source_finite_candidates",
    }
    if tuning:
        axes = payload.get("parameter_axes", TUNING_AXES)
        if not isinstance(axes, dict) or not axes or set(axes) - set(TUNABLE_PATHS):
            raise ValueError("Choose explicit supported ISP tuning axes.")
        for samples in axes.values():
            if not isinstance(samples, list) or not 1 <= len(samples) <= 12:
                raise ValueError("Each tuning axis needs 1..12 explicit values.")
            if any(_value(v) is None for v in samples) or len(set(samples)) != len(samples):
                raise ValueError("Tuning values must be finite distinct numbers.")
        if math.prod(len(v) for v in axes.values()) > MAX_TUNING_CANDIDATES:
            raise ValueError("Tuning is bounded to 12 candidates including the baseline.")
        result["parameter_axes"] = copy.deepcopy(axes)
        result["candidate_count"] = len(profiles(result))
    return result


def profiles(study, calibration_id=None):
    base = study["isp"]
    result = []
    if study["recipe"] == "parameter_tuning":
        baseline = copy.deepcopy(base)
        baseline["lsc_tuning"] = {"strength": 1.0, "gain_cap": 4.0}
        baseline["raw_denoise"]["strength"] = 0.0
        baseline["linear_sharpen"]["amount"] = 0.0
        baseline = reference_isp.validate_profile(baseline)
        result.append(("Baseline · full LSC · NR/sharpen OFF", baseline))
        paths = sorted(study["parameter_axes"])
        for values in itertools.product(*(study["parameter_axes"][p] for p in paths)):
            profile = copy.deepcopy(base)
            for path, value in zip(paths, values, strict=True):
                group, key = path.split(".")
                profile[group][key] = value
            profile = reference_isp.validate_profile(profile)
            if any(profile == p for _, p in result):
                continue
            label = " · ".join(f"{AXIS_LABELS[p]} {v:g}"
                               for p, v in zip(paths, values, strict=True))
            result.append((label, profile))
        if len(result) > MAX_TUNING_CANDIDATES:
            raise ValueError("Tuning is bounded to 12 candidates including the baseline.")
    elif study["recipe"] == "noise_detail":
        for strength in (0.0, 0.4, 0.8):
            for amount in (0.0, 0.3):
                profile = copy.deepcopy(base)
                profile["raw_denoise"]["strength"] = strength
                profile["linear_sharpen"]["amount"] = amount
                result.append((f"RAW NR {strength:g} · sharpen {amount:g}", profile))
    elif study["recipe"] == "demosaic":
        for method in ("bilinear", "laplacian", "adaptive_laplacian"):
            profile = copy.deepcopy(base)
            profile["demosaic_method"] = method
            result.append((method, profile))
    else:
        if not calibration_id:
            raise ValueError("Color recipe requires a fitted immutable calibration.")
        for label, identity in (
            ("Frozen spectral CCM", None),
            ("Munsell64 CCM · Macbeth24 withheld", calibration_id),
        ):
            profile = copy.deepcopy(base)
            profile["color_calibration_id"] = identity
            # Identity user corrections distinguish fitted calibration from manual tuning.
            profile["wb"] = [1.0, 1.0, 1.0]
            profile["ccm"] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
            result.append((label, profile))
    return [(name, reference_isp.validate_profile(profile)) for name, profile in result]


def _value(value):
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _field(field):
    tests = field.get("tests", {})
    lab = tests.get("isp_lab", {})
    support = lab.get("metric_support", {})
    if any(support.get(kind, {}).get("eligible") is not True
           for kind in ("color", "resolution", "texture")):
        return None
    halos = {}
    for name, site in tests.get("resolution", {}).get("sites", {}).items():
        edge = site.get("processed_system", {})
        a, b = _value(edge.get("overshoot_fraction")), _value(edge.get("undershoot_fraction"))
        if edge.get("status") != "ok" or a is None or b is None:
            return None
        halos[name + ":overshoot"] = a
        halos[name + ":undershoot"] = b
    values = [
        _value(lab.get("temporal_noise_luma")),
        _value(lab.get("texture_retention")),
        _value(tests.get("color", {}).get("delta_e00_mean")),
    ]
    if (
        field.get("status") != "succeeded"
        or not halos
        or any(v is None or v < 0 for v in values)
        or min(halos.values()) < 0
    ):
        return None
    return dict(noise=values[0], texture=values[1], color=values[2], halos=halos,
                brightness=_value(lab.get("uniform_mean_luma")))


def select(candidates, study):
    """Unknowns never pass. Baseline noise is not optimized away through survivorship."""
    if study["recipe"] not in {"noise_detail", "parameter_tuning"}:
        return {
            "status": "comparison_only",
            "selected_candidate_id": None,
            "reason": "Inspect fixed-source differences; no universal algorithm or sensor winner.",
            "rows": [],
        }
    constraints = study["constraints"]
    baseline = candidates[0] if candidates else {}
    base_fields = {f["id"]: _field(f) for f in baseline.get("fields", [])}
    rows = []
    for candidate in candidates:
        current = {f["id"]: _field(f) for f in candidate.get("fields", [])}
        reasons = []
        row = {
            "candidate_id": candidate["id"],
            "noise": None,
            "texture_retention": None,
            "halo_increase": None,
            "delta_e_increase": None,
        }
        if (
            candidate.get("status") != "succeeded"
            or not base_fields
            or current.keys() != base_fields.keys()
            or set(base_fields) != set(study.get("expected_field_ids", FIELD_IDS))
            or any(v is None for v in base_fields.values())
            or any(v is None for v in current.values())
            or any(current[k]["halos"].keys() != base_fields[k]["halos"].keys()
                   for k in current)
        ):
            reasons.append("missing_or_invalid_field_metrics")
        else:
            row.update(
                noise=max(v["noise"] for v in current.values()),
                texture_retention=min(v["texture"] for v in current.values()),
                halo_increase=max(current[k]["halos"][site] - base_fields[k]["halos"][site]
                                  for k in current for site in current[k]["halos"]),
                delta_e_increase=max(
                    current[k]["color"] - base_fields[k]["color"] for k in current
                ),
            )
            if row["texture_retention"] < constraints["texture_retention_min"]:
                reasons.append("texture_below_limit")
            if row["halo_increase"] > constraints["halo_increase_max"]:
                reasons.append("additional_halo_above_limit")
            if row["delta_e_increase"] > constraints["delta_e_increase_max"]:
                reasons.append("color_regression_above_limit")
            if study["recipe"] == "parameter_tuning":
                if any(v["brightness"] is None or v["brightness"] <= 0
                       for v in [*current.values(), *base_fields.values()]):
                    reasons.append("missing_or_invalid_brightness_metrics")
                else:
                    ratios = [current[k]["brightness"] / base_fields[k]["brightness"]
                              for k in current]
                    uniformity = min(v["brightness"] for v in current.values()) / max(
                        v["brightness"] for v in current.values())
                    base_uniformity = min(v["brightness"] for v in base_fields.values()) / max(
                        v["brightness"] for v in base_fields.values())
                    row.update(brightness_relative_min=min(ratios),
                               brightness_relative_max=max(ratios), uniformity=uniformity,
                               uniformity_regression=base_uniformity - uniformity)
                    relative_noise = max(v["noise"] / v["brightness"] for v in current.values())
                    baseline_relative_noise = max(v["noise"] / v["brightness"]
                                                  for v in base_fields.values())
                    normalized_improvement = (1 - relative_noise / baseline_relative_noise
                                              if baseline_relative_noise > 0 else 0.0)
                    row.update(brightness_normalized_noise=relative_noise,
                               brightness_normalized_noise_improvement=normalized_improvement)
                    if (candidate["id"] != baseline["id"] and normalized_improvement
                            < constraints["noise_improvement_min"]):
                        reasons.append("brightness_normalized_noise_gain_below_limit")
                    if (min(ratios) < constraints["brightness_relative_min"]
                            or max(ratios) > constraints["brightness_relative_max"]):
                        reasons.append("brightness_outside_baseline_limits")
                    if base_uniformity - uniformity > constraints["uniformity_increase_max"]:
                        reasons.append("field_uniformity_regression_above_limit")
        rows.append({**row, "eligible": not reasons, "reasons": reasons})
    feasible = [r for r in rows if r["eligible"]]
    if not feasible or not rows or not rows[0]["eligible"]:
        return {
            "status": "inconclusive",
            "selected_candidate_id": None,
            "reason": "No comparable complete set passes the declared constraints.",
            "rows": rows,
        }
    winner = min(feasible, key=lambda r: (r["noise"], r["candidate_id"]))
    baseline_row = rows[0]
    improvement = 1 - winner["noise"] / baseline_row["noise"] if baseline_row["noise"] > 0 else 0
    if improvement < constraints["noise_improvement_min"]:
        winner = baseline_row
    selected = winner["candidate_id"]
    return {
        "status": "baseline_retained" if selected == baseline["id"] else "recommended_within_demo",
        "selected_candidate_id": selected,
        "reason": "Minimum worst-field temporal noise under fixed texture, "
        "extra-halo and color constraints."
        + (" Brightness, field uniformity and brightness-normalized noise also constrained."
           if study["recipe"] == "parameter_tuning" else ""),
        "noise_improvement_fraction": max(0.0, 1 - winner["noise"] / baseline_row["noise"])
        if baseline_row["noise"]
        else 0.0,
        "rows": rows,
        "constraints": constraints,
    }

"""Fit and independently evaluate a conditional, forward ISP correction CCM.

The input is black-subtracted, linear camera RGB. Row vectors are multiplied on
the right: (camera_rgb * fixed_white_balance * fixed_exposure_scale) @ matrix.
The module neither estimates spectral QE nor edits a camera/RAW response profile.
The camera_linear_rgb target is signed post-CCM, pre-clipping linear ISP output;
it is distinct from the nonnegative, black-subtracted input camera RGB.
A caller must bind declared source hashes to preserved original artifacts; this
pure numerical module verifies consistency, not the provenance of external bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

import numpy as np
from scipy.optimize import lsq_linear
from skimage.color import deltaE_ciede2000, xyz2lab

PROFILE_SCHEMA = "camera_isp_ccm_profile_v1"
APPLICATION = "(camera_rgb * white_balance * exposure_scale) @ matrix"
MAX_PATCHES = 4096
FIXED_FIELDS = (
    "target_space",
    "white_balance",
    "exposure_scale",
    "source_kind",
    "illuminant_id",
    "illuminant_spd_sha256",
    "conditions",
    "whitepoint",
    "normalization",
    "normalization_metadata",
    "input_domain",
)
COMMON_FIELDS = set(FIXED_FIELDS) | {
    "camera_rgb",
    "target_rgb",
    "session_id",
    "acquisition_id",
    "source_artifact_sha256",
    "clipped_patch_indices",
}
LIMITATIONS = [
    "Conditional effective 3x3 ISP correction, not identification of a complete ISP.",
    "Spectral QE, RAW response, black subtraction, demosaicing, WB and exposure are not fitted.",
    "Declared artifact/SPD hashes must be verified against preserved originals by the caller.",
    "Linear input and clipping declarations require an independent upstream measurement check.",
    "Training and holdout agreement do not establish physical camera validation.",
    "CIEDE2000 uses D65, CIE 1931 2-degree XYZ with white Y=1; no chromatic adaptation.",
    "No clipping or gamut mapping is applied to matrix outputs or RMSE metrics.",
    "camera_linear_rgb targets are signed post-CCM/pre-clipping linear ISP output, not XYZ.",
]
# Row-vector linear sRGB -> CIE XYZ, D65, relative white Y=1.
SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.2126729, 0.0193339],
        [0.3575761, 0.7151522, 0.1191920],
        [0.1804375, 0.0721750, 0.9503041],
    ],
    dtype=float,
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _json(value: Any) -> Any:
    """Detach JSON input without accepting arrays, arbitrary objects or NaNs."""
    try:
        encoded = _canonical(value)
        if len(encoded.encode()) > 2 * 1024**2:
            raise ValueError("CCM payload exceeds 2 MiB")
        return json.loads(encoded)
    except (TypeError, OverflowError) as exc:
        raise ValueError("CCM input must contain finite JSON values") from exc


def _array(value: Any, name: str, shape: tuple[int, ...] | None = None) -> np.ndarray:
    a = np.asarray(value)
    if a.dtype.kind not in "uif" or a.dtype.kind == "b":
        raise ValueError(f"{name} requires finite numeric values")
    a = a.astype(float)
    if not np.all(np.isfinite(a)) or (shape is not None and a.shape != shape):
        raise ValueError(f"{name} has invalid values or dimensions")
    return a


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not np.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} is outside [{minimum}, {maximum}]")
    return number


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty identifier of at most 256 characters")
    return value


def _checksum(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} requires a lowercase SHA256")
    return value


def _observation_hash(camera: np.ndarray) -> str:
    # A row permutation or relabeling cannot make training measurements independent.
    return _hash(sorted(camera.tolist()))


def _parse(payload: dict[str, Any], *, fitting: bool) -> tuple[dict, np.ndarray, np.ndarray]:
    p = _json(payload)
    if not isinstance(p, dict):
        raise ValueError("CCM payload must be an object")
    allowed = COMMON_FIELDS | (
        {"baseline_matrix", "ridge", "coefficient_bound"} if fitting else {"thresholds"}
    )
    if set(p) - allowed or COMMON_FIELDS - set(p):
        raise ValueError(
            f"CCM fields: missing={sorted(COMMON_FIELDS - set(p))}, "
            f"unsupported={sorted(set(p) - allowed)}"
        )
    if p["target_space"] not in {"linear_srgb", "xyz", "camera_linear_rgb"}:
        raise ValueError("Unsupported CCM target_space")
    if p["input_domain"] != "camera_linear_rgb":
        raise ValueError("CCM requires black-subtracted camera_linear_rgb, not nonlinear input")
    normalizations = {"unit_reflectance_linear"}
    if p["target_space"] == "camera_linear_rgb":
        normalizations.add("fixed_reference_electrons")
    if p["normalization"] not in normalizations or p["whitepoint"] != "D65":
        raise ValueError(
            "CCM requires D65 and unit_reflectance_linear normalization; "
            "fixed_reference_electrons is allowed only for camera-output reproduction"
        )
    if p["source_kind"] not in {"measured", "synthetic", "model_derived"}:
        raise ValueError("CCM source_kind must be measured, synthetic or model_derived")
    for key in ("session_id", "acquisition_id", "illuminant_id"):
        _identifier(p[key], key)
    for key in ("source_artifact_sha256", "illuminant_spd_sha256"):
        _checksum(p[key], key)
    for key in ("conditions", "normalization_metadata"):
        if not isinstance(p[key], dict) or not p[key] or len(_canonical(p[key])) > 16384:
            raise ValueError(f"{key} requires a nonempty bounded JSON object")
    if not isinstance(p["clipped_patch_indices"], list) or p["clipped_patch_indices"]:
        raise ValueError("Clipped patches are ineligible; supply unclipped measurements only")
    wb = _array(p["white_balance"], "white_balance", (3,))
    if np.any(wb <= 0) or np.any(wb > 32):
        raise ValueError("Fixed white_balance must be positive and at most 32")
    p["white_balance"] = wb.tolist()
    p["exposure_scale"] = _number(p["exposure_scale"], "exposure_scale", 1e-6, 1e6)
    camera = _array(p["camera_rgb"], "camera_rgb")
    target = _array(p["target_rgb"], "target_rgb")
    if (
        camera.ndim != 2
        or camera.shape[1] != 3
        or camera.shape != target.shape
        or not (4 if fitting else 1) <= len(camera) <= MAX_PATCHES
    ):
        raise ValueError("Matching N x 3 patches required (fit 4..4096, validation 1..4096)")
    if np.any(camera < 0) or np.any(camera > 16):
        raise ValueError("Camera RGB requires nonnegative relative linear values <=16")
    # Both linear RGB target spaces describe unclipped matrix outputs, which
    # may be signed. Physical XYZ tristimulus targets cannot be negative.
    if np.any(np.abs(target) > 16) or (p["target_space"] == "xyz" and np.any(target < 0)):
        raise ValueError("Target values are outside the declared relative linear domain")
    p["camera_rgb"], p["target_rgb"] = camera.tolist(), target.tolist()
    if fitting:
        p["coefficient_bound"] = _number(
            p.get("coefficient_bound", 8), "coefficient_bound", 0.1, 64
        )
        p["ridge"] = _number(p.get("ridge", 0), "ridge", 0, 1e6)
        baseline = _array(p.get("baseline_matrix", np.eye(3).tolist()), "baseline_matrix", (3, 3))
        if np.any(np.abs(baseline) > 64):
            raise ValueError("baseline_matrix coefficients exceed 64")
        p["baseline_matrix"] = baseline.tolist()
    else:
        thresholds = p.get("thresholds")
        if (
            not isinstance(thresholds, dict)
            or "max_rmse" not in thresholds
            or set(thresholds) - {"max_rmse", "max_delta_e00"}
        ):
            raise ValueError(
                "Validation requires explicit thresholds.max_rmse (optional max_delta_e00)"
            )
        p["thresholds"] = {k: _number(v, k, 0, 1e6) for k, v in thresholds.items()}
        if "max_delta_e00" in thresholds and p["target_space"] == "camera_linear_rgb":
            raise ValueError(
                "Camera-output reproduction cannot use a CIEDE2000 acceptance threshold"
            )
    return p, camera, target


def _metrics(predicted: np.ndarray, target: np.ndarray, target_space: str) -> dict:
    residual = predicted - target
    result = {
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "channel_rmse": np.sqrt(np.mean(residual**2, axis=0)).tolist(),
        "max_abs_error": float(np.max(np.abs(residual))),
        "bias": np.mean(residual, axis=0).tolist(),
        "negative_output_components": int(np.count_nonzero(predicted < 0)),
        "output_components_above_one": int(np.count_nonzero(predicted > 1)),
        "delta_e00_mean": None,
        "delta_e00_max": None,
        "delta_e00": None,
        "delta_e00_status": "not_applicable_camera_output_reproduction",
    }
    if target_space == "camera_linear_rgb":
        return result
    xyz, reference = (
        (predicted @ SRGB_TO_XYZ, target @ SRGB_TO_XYZ)
        if target_space == "linear_srgb"
        else (predicted, target)
    )
    if np.any(xyz < 0) or np.any(reference < 0):
        result["delta_e00_status"] = "unavailable_negative_xyz_no_clipping_applied"
        return result
    observed_lab = xyz2lab(xyz, illuminant="D65", observer="2")
    reference_lab = xyz2lab(reference, illuminant="D65", observer="2")
    difference = deltaE_ciede2000(observed_lab, reference_lab)
    if not np.all(np.isfinite(difference)):
        result["delta_e00_status"] = "unavailable_nonfinite_lab_conversion"
        return result
    result.update(
        delta_e00_mean=float(np.mean(difference)),
        delta_e00_max=float(np.max(difference)),
        delta_e00=difference.tolist(),
        delta_e00_status="computed_d65_2deg_white_y_1",
    )
    return result


def _compare(profile: dict, camera: np.ndarray, target: np.ndarray) -> dict:
    balanced = camera * np.asarray(profile["white_balance"]) * profile["exposure_scale"]
    baseline = balanced @ np.asarray(profile["baseline_matrix"])
    candidate = balanced @ np.asarray(profile["matrix"])
    return {
        "camera_rgb": camera.tolist(),
        "target_rgb": target.tolist(),
        "baseline": baseline.tolist(),
        "candidate": candidate.tolist(),
        "metrics": {
            "baseline": _metrics(baseline, target, profile["target_space"]),
            "candidate": _metrics(candidate, target, profile["target_space"]),
        },
        "matrix_outputs_clipped": False,
        "target_space": profile["target_space"],
        "target_interpretation": (
            "signed_post_ccm_pre_clipping_linear_isp_output"
            if profile["target_space"] == "camera_linear_rgb"
            else "unclipped_linear_srgb_d65"
            if profile["target_space"] == "linear_srgb"
            else "nonnegative_cie_xyz_d65_white_y_1"
        ),
        "rmse_unit": "relative_linear_target_component",
    }


def solve_bounded_matrix(x, target, *, baseline=None, ridge=0.0, coefficient_bound=8.0):
    """Shared numerical solver; callers own normalization and evidence contracts."""
    x, target = np.asarray(x, dtype=float), np.asarray(target, dtype=float)
    baseline = np.eye(3) if baseline is None else np.asarray(baseline, dtype=float)
    if (x.ndim != 2 or x.shape[1] != 3 or len(x) < 4 or target.shape != x.shape
            or baseline.shape != (3, 3) or not np.isfinite(x).all()
            or not np.isfinite(target).all() or not np.isfinite(baseline).all()
            or not np.isfinite([ridge, coefficient_bound]).all()
            or ridge < 0 or coefficient_bound <= 0):
        raise ValueError("CCM solver requires finite matching RGB arrays and positive bounds")
    rank, condition = int(np.linalg.matrix_rank(x)), float(np.linalg.cond(x))
    if rank != 3 or not np.isfinite(condition) or condition > 1e6:
        raise ValueError("CCM training requires rank-three independent, well-conditioned patches")
    design = np.vstack([x, np.sqrt(ridge) * np.eye(3)]) if ridge else x
    targets = np.vstack([target, np.sqrt(ridge) * baseline]) if ridge else target
    solutions = [lsq_linear(design, targets[:, channel], bounds=(-coefficient_bound, coefficient_bound),
                            tol=1e-12, max_iter=1000) for channel in range(3)]
    if any(not solution.success for solution in solutions):
        raise ValueError("Bounded CCM optimizer did not converge")
    return np.column_stack([solution.x for solution in solutions]), rank, condition


def fit_ccm(payload: dict[str, Any]) -> dict[str, Any]:
    """Fit camera-linear RGB -> declared target with fixed WB/exposure and bounds.

    Ridge minimizes ||X M - Y||² + ridge ||M - baseline_matrix||². No offset,
    spectral/QE parameter, raw response, WB or exposure parameter is optimized.
    The returned ``profile`` is independently usable by ``validate_ccm``.
    """
    p, camera, target = _parse(payload, fitting=True)
    x = camera * np.asarray(p["white_balance"]) * p["exposure_scale"]
    baseline, ridge = np.asarray(p["baseline_matrix"]), p["ridge"]
    bound = p["coefficient_bound"]
    matrix, rank, condition = solve_bounded_matrix(x, target, baseline=baseline,
                                                  ridge=ridge, coefficient_bound=bound)
    training = {
        "session_id": p["session_id"],
        "acquisition_id": p["acquisition_id"],
        "source_artifact_sha256": p["source_artifact_sha256"],
        "payload_sha256": _hash(p),
        "camera_observations_sha256": _observation_hash(camera),
        "target_values_sha256": _hash(target.tolist()),
        "patch_count": len(camera),
    }
    profile = {
        "schema": PROFILE_SCHEMA,
        "kind": "conditional_effective_isp_ccm",
        "matrix_convention": "row_vector",
        "application": APPLICATION,
        "matrix": matrix.tolist(),
        "baseline_matrix": baseline.tolist(),
        **{key: deepcopy(p[key]) for key in FIXED_FIELDS},
        "training": training,
        "fit": {
            "method": "bounded_linear_least_squares_v1",
            "rank": rank,
            "condition_number": condition,
            "coefficient_bound": bound,
            "ridge": ridge,
            "ridge_anchor": "baseline_matrix",
            "converged": True,
            "bound_active": bool(np.any(np.isclose(np.abs(matrix), bound, atol=1e-7, rtol=0))),
        },
        "physical_validation": "not_established",
    }
    profile["profile_sha256"] = _hash(profile)
    return {
        "schema": "camera_isp_ccm_fit_v1",
        "status": "fitted",
        "profile": profile,
        "comparison": _compare(profile, camera, target),
        "training_payload": p,
        "changed_parameters": [
            {
                "parameter": "isp.color_matrix",
                "before": baseline.tolist(),
                "after": matrix.tolist(),
                "unit": "1",
            }
        ],
        "physical_validation": "not_established",
        "limitations": list(LIMITATIONS),
    }


def _profile(profile: dict[str, Any]) -> dict:
    p = _json(profile)
    required = set(FIXED_FIELDS) | {
        "schema",
        "kind",
        "matrix_convention",
        "application",
        "matrix",
        "baseline_matrix",
        "training",
        "fit",
        "physical_validation",
        "profile_sha256",
    }
    if not isinstance(p, dict) or set(p) != required:
        raise ValueError("Unsupported or incomplete CCM profile")
    checksum = p["profile_sha256"]
    if checksum != _hash({k: v for k, v in p.items() if k != "profile_sha256"}):
        raise ValueError("Frozen CCM profile hash mismatch")
    if (
        p["schema"] != PROFILE_SCHEMA
        or p["kind"] != "conditional_effective_isp_ccm"
        or p["matrix_convention"] != "row_vector"
        or p["application"] != APPLICATION
        or p["physical_validation"] != "not_established"
        or p["fit"].get("converged") is not True
    ):
        raise ValueError("Unsupported CCM profile convention or fit state")
    matrix = _array(p["matrix"], "profile.matrix", (3, 3))
    _array(p["baseline_matrix"], "profile.baseline_matrix", (3, 3))
    bound = _number(p["fit"].get("coefficient_bound"), "coefficient_bound", 0.1, 64)
    if np.any(np.abs(matrix) > bound + 1e-10):
        raise ValueError("CCM profile matrix exceeds its frozen coefficient bounds")
    for key in (
        "source_artifact_sha256",
        "payload_sha256",
        "camera_observations_sha256",
        "target_values_sha256",
    ):
        _checksum(p["training"][key], "training." + key)
    return p


def validate_ccm(profile: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen matrix on new independently acquired camera observations.

    Target/reference patches may be reused. Camera observation contents, source
    bytes, session and acquisition must differ from training. This stateless
    function does not track reuse across other candidates; the registry must do so.
    """
    frozen = _profile(profile)
    p, camera, target = _parse(payload, fitting=False)
    mismatches = [key for key in FIXED_FIELDS if p[key] != frozen[key]]
    if mismatches:
        raise ValueError(
            "CCM validation metadata differs from frozen profile: " + ", ".join(mismatches)
        )
    training = frozen["training"]
    if any(
        p[key] == training[key]
        for key in ("session_id", "acquisition_id", "source_artifact_sha256")
    ):
        raise ValueError(
            "Independent CCM validation requires new session, acquisition and source bytes"
        )
    observation_hash = _observation_hash(camera)
    if observation_hash == training["camera_observations_sha256"]:
        raise ValueError("Validation reuses training camera observations, possibly reordered")
    comparison = _compare(frozen, camera, target)
    metrics = comparison["metrics"]["candidate"]
    checks = [
        {
            "metric": "rmse",
            "value": metrics["rmse"],
            "limit": p["thresholds"]["max_rmse"],
            "status": "pass" if metrics["rmse"] <= p["thresholds"]["max_rmse"] else "fail",
        }
    ]
    if "max_delta_e00" in p["thresholds"]:
        value, limit = metrics["delta_e00_mean"], p["thresholds"]["max_delta_e00"]
        checks.append(
            {
                "metric": "delta_e00_mean",
                "value": value,
                "limit": limit,
                "status": "inconclusive" if value is None else "pass" if value <= limit else "fail",
            }
        )
    status = (
        "fail"
        if any(c["status"] == "fail" for c in checks)
        else "inconclusive"
        if any(c["status"] == "inconclusive" for c in checks)
        else "pass"
    )
    return {
        "schema": "camera_isp_ccm_validation_v1",
        "status": status,
        "checks": checks,
        "refit_performed": False,
        "profile_sha256": frozen["profile_sha256"],
        "comparison": comparison,
        "validation_payload": p,
        "validation_payload_sha256": _hash(p),
        "camera_observations_sha256": observation_hash,
        "training_payload_sha256": training["payload_sha256"],
        "independent_of_training": True,
        "physical_validation": "not_established",
        "limitations": list(LIMITATIONS),
    }

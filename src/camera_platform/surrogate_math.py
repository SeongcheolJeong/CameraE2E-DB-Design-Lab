"""Portable thin-plate RBF coefficients with a linear polynomial tail.

Implements the documented RBF block system, independently compared with SciPy
RBFInterpolator in tests. No pickle, hidden private SciPy state or holdout tuning.
https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.RBFInterpolator.html
"""

from __future__ import annotations

import itertools

import numpy as np

METHOD = "normalized_thin_plate_rbf_linear_tail/1"
MIN_SEPARATION = 1e-8


def kernel(a, b):
    squared = np.sum((a[:, None, :] - b[None, :, :]) ** 2, axis=2)
    result = np.zeros_like(squared)
    mask = squared > 0
    result[mask] = 0.5 * squared[mask] * np.log(squared[mask])
    return result


def domain_and_split(training, validation):
    x, v = np.asarray(training, dtype=float), np.asarray(validation, dtype=float)
    if x.ndim != 2 or not 1 <= x.shape[1] <= 6 or not x.shape[1] + 2 <= len(x) <= 192:
        raise ValueError("Training requires dimensions + 2 to 192 points in 1–6 dimensions")
    if v.ndim != 2 or v.shape[1] != x.shape[1] or not max(3, x.shape[1] + 1) <= len(v) <= 64:
        raise ValueError("Held-out validation requires max(3, dimensions + 1) to 64 points")
    if not np.isfinite(x).all() or not np.isfinite(v).all():
        raise ValueError("Surrogate coordinates must be finite")
    lo, hi = x.min(axis=0), x.max(axis=0)
    if np.any(hi <= lo) or not np.isfinite(hi - lo).all():
        raise ValueError("Every training axis needs a finite nonzero span")
    xn, vn = (x - lo) / (hi - lo), (v - lo) / (hi - lo)
    if np.any(v < lo) or np.any(v > hi):
        raise ValueError("Validation coordinates are outside the training domain")
    # A bounding box is used only when its complete corner set is observed.
    # An OAT cross cannot silently authorize unobserved corner combinations.
    observed = {tuple(row) for row in xn}
    if any(corner not in observed for corner in itertools.product((0.0, 1.0), repeat=x.shape[1])):
        raise ValueError(
            "Training must include every domain-box corner; OAT crosses are insufficient"
        )
    all_points = np.concatenate([xn, vn])
    distances = np.linalg.norm(all_points[:, None] - all_points[None, :], axis=2)
    np.fill_diagonal(distances, np.inf)
    if np.any(distances <= MIN_SEPARATION):
        raise ValueError(
            "Duplicate or near-duplicate coordinates within/across training and validation"
        )
    if np.any(np.ptp(vn, axis=0) <= MIN_SEPARATION):
        raise ValueError("Held-out validation must vary every input axis")
    return {
        "lower": lo.tolist(),
        "upper": hi.tolist(),
        "kind": "training_box_with_all_corners",
        "corner_count": 2 ** x.shape[1],
        "normalized_minimum_separation": MIN_SEPARATION,
        "validation_lower": v.min(axis=0).tolist(),
        "validation_upper": v.max(axis=0).tolist(),
        "continuous_error_bound": "unknown",
    }


def fit(training, outputs, domain):
    x, y = np.asarray(training, dtype=float), np.asarray(outputs, dtype=float)
    lo, hi = np.array(domain["lower"]), np.array(domain["upper"])
    xn = (x - lo) / (hi - lo)
    if y.ndim != 2 or len(y) != len(x) or not np.isfinite(y).all():
        raise ValueError("Training outputs must be a finite metric matrix")
    p = np.column_stack([np.ones(len(x)), xn])
    if np.linalg.matrix_rank(p) != p.shape[1]:
        raise ValueError("Training polynomial is rank deficient")
    system = np.block([[kernel(xn, xn), p], [p.T, np.zeros((p.shape[1], p.shape[1]))]])
    condition = float(np.linalg.cond(system))
    if not np.isfinite(condition) or condition > 1e12:
        raise ValueError("Training system is ill-conditioned (> 1e12); revise sample geometry")
    # Mean/range scaling is learned from TRAINING ONLY, including constant outputs.
    offset, scale = y.mean(axis=0), np.ptp(y, axis=0)
    scale[scale == 0] = 1.0
    if not np.isfinite(offset).all() or not np.isfinite(scale).all():
        raise ValueError("Training output scale is not finite")
    target = np.vstack([(y - offset) / scale, np.zeros((p.shape[1], y.shape[1]))])
    coefficients = np.linalg.solve(system, target)
    if not np.isfinite(coefficients).all():
        raise ValueError("Non-finite fitted coefficients")
    return {
        "method_and_version": METHOD,
        "kernel": "thin_plate_spline",
        "polynomial_degree": 1,
        "smoothing": 0.0,
        "normalization": "training_axis_range_and_output_mean_range",
        "domain": domain,
        "centers_normalized": xn.tolist(),
        "radial_coefficients": coefficients[: len(x)].tolist(),
        "polynomial_coefficients": coefficients[len(x) :].tolist(),
        "output_offset": offset.tolist(),
        "output_scale": scale.tolist(),
        "condition_number": condition,
    }


def evaluate(model, points):
    if model["method_and_version"] != METHOD:
        raise ValueError("Unsupported surrogate coefficient method/version")
    x = np.asarray(points, dtype=float)
    lo, hi = np.array(model["domain"]["lower"]), np.array(model["domain"]["upper"])
    if x.ndim != 2 or x.shape[1] != len(lo) or not np.isfinite(x).all():
        raise ValueError("Invalid prediction coordinate matrix")
    if np.any(x < lo) or np.any(x > hi):
        raise ValueError("OUT_OF_DOMAIN: prediction requires the declared training box")
    xn = (x - lo) / (hi - lo)
    p = np.column_stack([np.ones(len(x)), xn])
    result = (
        kernel(xn, np.array(model["centers_normalized"])) @ np.array(model["radial_coefficients"])
        + p @ np.array(model["polynomial_coefficients"])
    ) * np.array(model["output_scale"]) + np.array(model["output_offset"])
    if not np.isfinite(result).all():
        raise ValueError("Non-finite surrogate prediction")
    return result

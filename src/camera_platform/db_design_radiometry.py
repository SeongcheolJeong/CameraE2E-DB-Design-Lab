"""Bounded dense reciprocal quadrature for narrow-iris DB aperture studies.

Uses the Factory's complete enclosing-disk measure and RayOptics trace without
changing its public/default budgets. This local adapter returns weighted actual
sensor directions; it does not manufacture a dense JSON joint-direction LUT.
"""

from __future__ import annotations

import copy
import time

import numpy as np

from . import lens_factory, pupil_transmission

PROFILE = "camera_db_aperture_dense_radiometry_v1"
GRID_LADDER = ((128, 256), (256, 256))
MAX_RAYS = 1_966_080
MAX_SURFACE_VISITS = 100_000_000
MAX_SECONDS = 180.0


def request(prescription):
    return {
        "profile": PROFILE,
        "prescription": copy.deepcopy(prescription),
        "coarse_grid_ladder": [list(grid) for grid in GRID_LADDER],
        "refinement": 2,
        "budget": {
            "max_rays": MAX_RAYS,
            "max_surface_visits": MAX_SURFACE_VISITS,
            "max_seconds": MAX_SECONDS,
        },
    }


def _validate(value):
    if not isinstance(value, dict) or set(value) != set(request({})):
        raise ValueError("Dense radiometry requires its explicit versioned request")
    if value != request(value["prescription"]):
        raise ValueError("Dense radiometry grid and resource budget are fixed")
    # Validate all prescription physics under the unchanged public Factory
    # contract before substituting this adapter's explicitly bounded work grid.
    data = lens_factory.validate_request(value["prescription"])
    if data["schema"] != "lens_prescription_v4" or len(data["wavelength_nm"]) > 2:
        raise ValueError("Dense DB radiometry supports at most two v4 wavelengths per batch")
    if len(data["image_height_mm"]) != 2 or data["image_height_mm"][0] != 0:
        raise ValueError("Dense DB radiometry requires center plus one off-axis image height")
    rays = len(data["wavelength_nm"]) * 2 * sum(nr * na for nr, na in GRID_LADDER) * 5
    visits = rays * (len(data["surfaces"]) + 1)
    if rays > MAX_RAYS or visits > MAX_SURFACE_VISITS:
        raise ValueError("Dense DB radiometry exceeds its explicit ray/surface budget")
    return data, rays, visits


def _agreement(data, coarse, fine):
    ri_delta = np.abs(fine["relative_illumination"] - coarse["relative_illumination"])
    cdf_delta = np.max(
        np.abs(np.cumsum(fine["angular_weights"] - coarse["angular_weights"], axis=-1)), axis=-1
    )
    integral_delta = np.abs(
        fine["irradiance_per_radiance_sr"] - coarse["irradiance_per_radiance_sr"]
    )
    relative_delta = np.divide(
        integral_delta,
        fine["irradiance_per_radiance_sr"],
        out=np.full_like(integral_delta, np.inf),
        where=fine["irradiance_per_radiance_sr"] > 0,
    )
    positive = all(
        bool(
            np.isfinite(row["irradiance_per_radiance_sr"]).all()
            and (row["irradiance_per_radiance_sr"] > 0).all()
        )
        for row in (coarse, fine)
    )
    geometry_valid = not any(bool(row["failure_counts"][..., 3].any()) for row in (coarse, fine))
    settings = data["sampling"]
    converged = bool(
        positive
        and geometry_valid
        and np.max(ri_delta) <= settings["ri_tolerance_absolute"]
        and np.max(cdf_delta) <= settings["angular_cdf_tolerance_absolute"]
        and np.max(relative_delta) <= settings["irradiance_tolerance_relative"]
    )
    return {
        "observed_two_level_agreement": converged,
        "positive_throughput": positive,
        "valid_geometry": geometry_valid,
        "relative_illumination_absolute_change": ri_delta.tolist(),
        "angular_cdf_max_absolute_change": cdf_delta.tolist(),
        "irradiance_integral_absolute_change_sr": integral_delta.tolist(),
        "irradiance_integral_relative_change": relative_delta.tolist(),
        "thresholds": copy.deepcopy(settings),
        "interpretation": (
            "Observed quadrature agreement, not a rigorous error bound or physical validation"
        ),
    }


def _conditional(fine):
    power = (
        fine["quadrature_weights_sr"][None, ...]
        * fine["accepted_rays"]
        * fine["stop_power_transmittance"]
    )
    integral = fine["irradiance_per_radiance_sr"]
    if not np.allclose(power.sum(axis=-1), integral, rtol=1e-12, atol=1e-15):
        raise ValueError("Dense radiometry actual-direction power does not conserve irradiance")
    return np.divide(
        power, integral[..., None], out=np.zeros_like(power), where=integral[..., None] > 0
    )


def characterize(value):
    data, rays, visits = _validate(value)
    started = time.monotonic()
    deadline = started + MAX_SECONDS
    backend = lens_factory._backend()
    paths = lens_factory._paths(data, backend)
    _, _, _, distance = lens_factory._sampling(data, 1)
    support = lens_factory._angular_support(data, distance)
    if support["certificate"] is not None:
        raise ValueError("Dense DB adapter preserves the unmodified full rear-plane disk measure")
    theta = np.linspace(0, support["maximum_sample_angle_deg"], data["sampling"]["theta_bins"] + 1)
    attempts = []
    try:
        for rings, azimuths in GRID_LADDER:
            data["sampling"].update(polar_rings=rings, azimuth_samples=azimuths)
            coarse = lens_factory._pass(data, backend, paths, theta, 1, deadline)
            fine = lens_factory._pass(data, backend, paths, theta, 2, deadline)
            convergence = _agreement(data, coarse, fine)
            attempts.append({"coarse_grid": [rings, azimuths], **convergence})
            if convergence["observed_two_level_agreement"]:
                break
    except lens_factory.LensFactoryError as exc:
        if any(issue["code"] == "TIME_LIMIT" for issue in exc.issues):
            raise lens_factory.LensFactoryError(
                "sampling",
                f"Dense DB radiometry exceeded its {MAX_SECONDS:g} second deadline",
                "TIME_LIMIT",
            ) from exc
        raise
    summary = {
        "schema": "camera_db_aperture_radiometry_summary_v1",
        "status": "PROPOSED" if convergence["observed_two_level_agreement"] else "REFUSED",
        "factory_id": PROFILE,
        "method_and_version": PROFILE,
        "physical_validation": "not_established",
        "request_sha256": pupil_transmission.digest(value),
        "normalized_request": copy.deepcopy(value),
        "actual_prescription": data,
        "elapsed_seconds": time.monotonic() - started,
        "budget": {**value["budget"], "requested_rays": rays, "maximum_surface_visits": visits},
        "sampling": {
            "measure": "equal_area_enclosing_rear_plane_disk",
            "power_weight": (
                "disk_area/sample_count * cos(theta)^4 / plane_distance^2 * actual_stop_power"
            ),
            "plane_distance_mm": distance,
            "integral_unit": "sr",
            "coarse_grid": [rings, azimuths],
            "fine_grid": [2 * rings, 2 * azimuths],
            "coarse_accepted_count": coarse["accepted_rays"].sum(axis=-1).tolist(),
            "fine_accepted_count": fine["accepted_rays"].sum(axis=-1).tolist(),
            "coarse_failure_counts": coarse["failure_counts"].tolist(),
            "fine_failure_counts": fine["failure_counts"].tolist(),
            "on_axis_measure": (
                "Complete equal-area annuli; downstream center response must be azimuth invariant"
            ),
            "direction_basis": (
                "Actual reciprocal sensor directions, negated for propagation into sensor"
            ),
        },
        "irradiance_per_radiance_sr": fine["irradiance_per_radiance_sr"].tolist(),
        "convergence": convergence,
        "refinement_attempts": attempts,
        "assumptions": [
            "Uniform object-space radiance, passive explicit stop transmission, "
            "otherwise lossless optics",
            "Same authored sensor plane and physical iris as forward diffraction PSF",
            "Center angular QE is azimuth invariant; "
            "off-axis directions retain joint angular weights",
        ],
    }
    arrays = {
        key: fine[key]
        for key in (
            "irradiance_per_radiance_sr",
            "relative_illumination",
            "angular_weights",
            "image_direction_cosines",
        )
    }
    arrays.update(
        directional_conditional_weights=_conditional(fine),
        theta_deg=theta,
        wavelength_nm=np.asarray(data["wavelength_nm"]),
        field_radius=np.asarray(data["field_radius"]),
    )
    return {"summary": summary, "arrays": arrays}


def spectral_directions(ray_arrays, waves):
    """Pad unequal refinement grids without changing a positive ray weight."""
    count = max(ray_arrays[w]["conditional"].shape[-1] for w in waves)
    fields = ray_arrays[waves[0]]["conditional"].shape[0]
    weights = np.zeros((len(waves), fields, count))
    directions = np.zeros((len(waves), fields, count, 3))
    directions[..., 2] = 1.0  # Unit normal for unused zero-power padding.
    for wi, wave in enumerate(waves):
        row = ray_arrays[wave]
        n = row["conditional"].shape[-1]
        if row["directions"].shape != (fields, n, 3):
            raise ValueError("Actual reciprocal directions and weights disagree")
        weights[wi, :, :n] = row["conditional"]
        directions[wi, :, :n] = row["directions"]
    return weights, directions

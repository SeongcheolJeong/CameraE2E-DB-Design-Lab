"""Native directional SFR for a declared wide-angle projection.

The angular axis uses the local projection Jacobian, including anisotropy. It
never divides by a constant FOV/image width. These are model-based chart
diagnostics; they do not qualify a real lens or a product ranking.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates

from . import virtual_lab_metrics as lab

SCHEMA = "camera_virtual_lab_wide_resolution_v1"

# Color references and independent temporal repeats retain their existing
# meanings. Wide projection does not require a different color/noise formula.
color_metrics = lab.color_metrics
temporal_noise_metrics = lab.temporal_noise_metrics
tone_metrics = lab.tone_metrics


def _geometry(geometry):
    projection = geometry.get("projection")
    if projection not in {"rectilinear", "equidistant"}:
        raise ValueError("Wide metrics require rectilinear or equidistant projection")
    shape = np.asarray(geometry.get("native_shape"), float)
    fov = float(geometry.get("horizontal_fov_deg", np.nan))
    if (
        shape.shape != (2,)
        or not np.isfinite(shape).all()
        or np.any(shape < 12)
        or np.any(shape != np.floor(shape))
        or not np.isfinite(fov)
        or not 0 < fov < 180
    ):
        raise ValueError("Declare positive native dimensions and horizontal FOV within (0,180)")
    height, width = map(int, shape)
    angle = np.radians(fov)
    focal_pixels = width / (2 * np.tan(angle / 2)) if projection == "rectilinear" else width / angle
    return projection, height, width, focal_pixels


def pixel_to_direction(geometry, row, col):
    """Pixel-center indices → unit directions, with +y toward increasing row."""
    projection, height, width, focal = _geometry(geometry)
    row, col = np.broadcast_arrays(np.asarray(row, float), np.asarray(col, float))
    if not np.isfinite(row).all() or not np.isfinite(col).all():
        raise ValueError("Pixel coordinates must be finite")
    x, y = col + 0.5 - width / 2, row + 0.5 - height / 2
    radius = np.hypot(x, y)
    theta = np.arctan(radius / focal) if projection == "rectilinear" else radius / focal
    if np.any(theta >= np.pi):
        raise ValueError("Equidistant direction lies outside a one-to-one spherical projection")
    factor = np.divide(np.sin(theta), radius, out=np.full_like(radius, 1 / focal), where=radius > 0)
    return np.stack([x * factor, y * factor, np.cos(theta)], axis=-1)


def local_projection_jacobian(geometry, row, col):
    """Return [...,3,2] derivatives of unit direction per native [column,row].

    The derivative norm is in radians per pixel, on the unit sphere. This is
    the exact differential of the declared projection, not a paraxial FOV
    conversion or a ray-traced lens calibration.
    """
    projection, height, width, focal = _geometry(geometry)
    row, col = np.broadcast_arrays(np.asarray(row, float), np.asarray(col, float))
    if not np.isfinite(row).all() or not np.isfinite(col).all():
        raise ValueError("Pixel coordinates must be finite")
    x, y = col + 0.5 - width / 2, row + 0.5 - height / 2
    radius = np.hypot(x, y)
    ux = np.divide(x, radius, out=np.ones_like(radius), where=radius > 0)
    uy = np.divide(y, radius, out=np.zeros_like(radius), where=radius > 0)
    theta = np.arctan(radius / focal) if projection == "rectilinear" else radius / focal
    if np.any(theta >= np.pi):
        raise ValueError("Equidistant direction lies outside a one-to-one spherical projection")
    radial_scale = (
        focal / (focal * focal + radius * radius)
        if projection == "rectilinear"
        else np.full_like(radius, 1 / focal)
    )
    tangent_scale = np.divide(
        np.sin(theta), radius, out=np.full_like(radius, 1 / focal), where=radius > 0
    )
    radial = np.stack([np.cos(theta) * ux, np.cos(theta) * uy, -np.sin(theta)], axis=-1)
    tangent = np.stack([-uy, ux, np.zeros_like(radius)], axis=-1)
    radial_xy = np.stack([ux, uy], axis=-1)
    tangent_xy = np.stack([-uy, ux], axis=-1)
    return (
        radial[..., :, None] * radial_xy[..., None, :] * radial_scale[..., None, None]
        + tangent[..., :, None] * tangent_xy[..., None, :] * tangent_scale[..., None, None]
    )


def _normal(value):
    normal = np.asarray(value, float)
    if normal.shape != (2,) or not np.isfinite(normal).all() or np.linalg.norm(normal) <= 0:
        raise ValueError("Each directional edge requires a finite nonzero normal_xy")
    return normal / np.linalg.norm(normal)


def _angular_scale(jacobian, normal):
    # A spatial frequency is a covector. For a non-principal image direction,
    # 1/|J*n| is wrong under anisotropic projection; use |J*(J^T J)^-1*n|.
    metric = np.einsum("...ki,...kj->...ij", jacobian, jacobian)
    inverse = np.linalg.inv(metric)
    cycles_per_radian_per_cycle_pixel = np.sqrt(
        np.einsum("i,...ij,j->...", normal, inverse, normal)
    )
    return (180 / np.pi) / cycles_per_radian_per_cycle_pixel


def angular_geometry(geometry, bounds, normal_xy, *, variation_tolerance=0.05):
    """Local angular-frequency conversion with finite-ROI nonlinearity flag."""
    _, height, width, _ = _geometry(geometry)
    lab._roi(bounds, (height, width))
    if not np.isfinite(variation_tolerance) or not 0 < variation_tolerance <= 1:
        raise ValueError("Angular variation tolerance must lie within (0,1]")
    normal = _normal(normal_xy)
    r0, r1, c0, c1 = map(int, bounds)
    row, col = (r0 + r1 - 1) / 2, (c0 + c1 - 1) / 2
    jacobian = local_projection_jacobian(geometry, row, col)
    scale = float(_angular_scale(jacobian, normal))
    rr, cc = np.meshgrid(np.linspace(r0, r1 - 1, 5), np.linspace(c0, c1 - 1, 5), indexing="ij")
    sample_scales = _angular_scale(local_projection_jacobian(geometry, rr, cc), normal)
    variation = float(np.max(np.abs(sample_scales / scale - 1)))
    qualified = variation <= variation_tolerance
    return {
        "method": "local_projection_jacobian_covector",
        "projection": geometry["projection"],
        "center_row_col": [row, col],
        "center_direction_xyz": pixel_to_direction(geometry, row, col).tolist(),
        "direction_jacobian_xyz_by_column_row": jacobian.tolist(),
        "frequency_equivalent_degrees_per_pixel": scale,
        "normal_displacement_degrees_per_pixel": float(
            np.linalg.norm(jacobian @ normal) * 180 / np.pi
        ),
        "roi_degrees_per_pixel_range": [float(sample_scales.min()), float(sample_scales.max())],
        "roi_relative_scale_variation": variation,
        "variation_tolerance": float(variation_tolerance),
        "angular_frequency_qualified": bool(qualified),
        "angular_axis_status": "local_linearization" if qualified else "local_only_nonlinear_roi",
        "nonlinear_roi_action": (
            "Native SFR retained. Angular SFR withheld where scale variation exceeds the "
            "threshold; a uniform angular-coordinate edge evaluation is required."
        ),
        "sampling": "5 by 5 ROI locations; observed variation, not a global error bound",
        "physical_lens_calibration": False,
    }


def _fit_normal(mono, hint):
    """Local edge fit for orientation only; original pixels supply the ESF."""
    gy, gx = np.gradient(gaussian_filter(mono, 1.0, mode="nearest"))
    weight = np.abs(gx * hint[0] + gy * hint[1])
    weight[:2] = weight[-2:] = 0
    weight[:, :2] = weight[:, -2:] = 0
    peak = float(weight.max())
    if peak <= 1e-12:
        return hint, None
    weight[weight < peak * 0.25] = 0
    y, x = np.indices(mono.shape, dtype=float)
    points = np.stack([x - (mono.shape[1] - 1) / 2, y - (mono.shape[0] - 1) / 2], axis=-1)
    total = weight.sum()
    center = np.sum(points * weight[..., None], axis=(0, 1)) / total
    centered = points - center
    covariance = np.einsum("hwi,hwj,hw->ij", centered, centered, weight) / total
    _, vectors = np.linalg.eigh(covariance)
    normal = vectors[:, 0]
    if np.dot(normal, hint) < 0:
        normal = -normal
    if np.dot(normal, hint) < np.cos(np.radians(30)):
        return hint, None
    return normal, center


def _sfr(mono, normal, center, pitch_um, angular):
    if min(mono.shape) < 16 or center is None:
        return {"status": "unavailable", "reason": "no_supported_directional_edge"}
    y, x = np.indices(mono.shape, dtype=float)
    distance = (x - (mono.shape[1] - 1) / 2 - center[0]) * normal[0] + (
        y - (mono.shape[0] - 1) / 2 - center[1]
    ) * normal[1]
    oversample, halfwidth = 4, int(min(mono.shape) * 0.4)
    count = 2 * halfwidth * oversample
    indices = np.floor((distance + halfwidth) * oversample).astype(int)
    valid = (indices >= 0) & (indices < count)
    samples = np.bincount(indices[valid], minlength=count)
    sums = np.bincount(indices[valid], weights=mono[valid], minlength=count)
    occupied = samples > 0
    coverage = float(occupied.mean())
    if coverage < 0.6:
        return {
            "status": "unavailable",
            "reason": "insufficient_subpixel_edge_phases",
            "occupied_esf_bins_fraction": coverage,
        }
    positions = (np.arange(count) + 0.5) / oversample - halfwidth
    esf = np.interp(positions, positions[occupied], sums[occupied] / samples[occupied])
    tails = max(3, count // 8)
    left, right = float(esf[:tails].mean()), float(esf[-tails:].mean())
    contrast = abs(right - left)
    if contrast < max(1e-12, float(np.ptp(mono)) * 0.05):
        return {"status": "unavailable", "reason": "insufficient_edge_contrast"}
    step = 1 / oversample
    lsf = np.gradient(esf, step) * np.hanning(count)
    nfft = max(2048, 2 ** int(np.ceil(np.log2(count))))
    frequency = np.fft.rfftfreq(nfft, step)
    response = np.abs(np.fft.rfft(lsf, n=nfft))
    if response[0] <= 1e-12:
        return {"status": "unavailable", "reason": "edge_dc_response_vanished"}
    response /= response[0]
    keep = frequency <= 0.5 + 1e-12
    frequency, response = frequency[keep], response[keep]
    # Undo the known central-difference derivative, not optical/pixel blur.
    derivative = np.sinc(2 * frequency * step)
    response = response / derivative
    mtf50 = lab._crossing(frequency, response)
    scale = (
        angular["frequency_equivalent_degrees_per_pixel"]
        if angular["angular_frequency_qualified"]
        else None
    )
    low, high = min(left, right), max(left, right)
    return {
        "status": "ok",
        "method": "native_pixel_projected_edge_sfr",
        "frequency": lab._frequency_axes(
            frequency, pixel_pitch_um=pitch_um, degrees_per_pixel=scale
        ),
        "sfr": response.tolist(),
        "mtf50_cycles_per_pixel": mtf50,
        "mtf50_cycles_per_degree": None if mtf50 is None or scale is None else mtf50 / scale,
        "mtf50_cycles_per_mm": None
        if mtf50 is None or pitch_um is None
        else mtf50 / (pitch_um * 0.001),
        "mtf50_reason": None if mtf50 is not None else "no_half_response_crossing_below_nyquist",
        "nyquist_cycles_per_pixel": 0.5,
        "nyquist_cycles_per_degree": None if scale is None else 0.5 / scale,
        "nyquist_response": float(response[-1]),
        "edge_contrast": contrast,
        "edge_normal_xy": normal.tolist(),
        "occupied_esf_bins_fraction": coverage,
        "overshoot_fraction": max(0.0, float(esf.max()) - high) / contrast,
        "undershoot_fraction": max(0.0, low - float(esf.min())) / contrast,
        "esf": esf.tolist(),
        "esf_position_native_pixels": positions.tolist(),
        "lsf": lsf.tolist(),
        "angular_axis_status": angular["angular_axis_status"],
        "angular_frequency_qualified": angular["angular_frequency_qualified"],
        "native_pixels_interpolated": False,
        "angular_frequency_reason": None
        if scale is not None
        else "nonlinear_roi_requires_uniform_angular_edge_evaluation",
        "interpretation": (
            "Directional system SFR, native pixel samples projected into edge-normal bins; "
            "not lens-only MTF or ISO certification."
        ),
    }


def _direction_to_pixel(geometry, direction):
    projection, height, width, focal = _geometry(geometry)
    if np.any(direction[..., 2] <= 0):
        raise ValueError("Angular diagnostic requires forward-hemisphere directions")
    if projection == "rectilinear":
        xy = focal * direction[..., :2] / direction[..., 2, None]
    else:
        radius = np.linalg.norm(direction[..., :2], axis=-1)
        theta = np.arctan2(radius, direction[..., 2])
        ratio = np.divide(theta, radius, out=np.ones_like(radius), where=radius > 0)
        xy = direction[..., :2] * (focal * ratio[..., None])
    return xy + np.array([width / 2 - 0.5, height / 2 - 0.5])


def angular_resampled_metrics(linear_roi, processed_roi, *, geometry, bounds, normal_xy):
    """Separate SFR diagnostic on a uniform tangent-angle grid inside the ROI.

    The spherical exponential map gives equally spaced tangent-angle
    coordinates. Bilinear sampling adds its own interpolation response; this
    diagnostic must not replace the independent native-pixel SFR.
    """
    linear, processed = lab._mono(linear_roi), lab._mono(processed_roi)
    _, height, width, _ = _geometry(geometry)
    lab._roi(bounds, (height, width))
    r0, r1, c0, c1 = map(int, bounds)
    if linear.shape != (r1 - r0, c1 - c0) or processed.shape != linear.shape:
        raise ValueError("Angular diagnostic inputs must match the original ROI bounds")
    size = min(96, int(min(linear.shape) * 0.75))
    if size < 24:
        return {"status": "unavailable", "reason": "angular_roi_too_small"}
    row, col = (r0 + r1 - 1) / 2, (c0 + c1 - 1) / 2
    direction = pixel_to_direction(geometry, row, col)
    jac = local_projection_jacobian(geometry, row, col)
    normal = _normal(normal_xy)
    tangent_normal = jac @ np.linalg.solve(jac.T @ jac, normal)
    tangent_normal /= np.linalg.norm(tangent_normal)
    tangent_along = np.cross(direction, tangent_normal)
    # Slight slant supplies subpixel phases to the ESF estimator. This rotates
    # only the diagnostic grid, not the saved native image or native SFR.
    angle = np.radians(5)
    basis_x = np.cos(angle) * tangent_normal + np.sin(angle) * tangent_along
    basis_y = -np.sin(angle) * tangent_normal + np.cos(angle) * tangent_along
    step = float(np.linalg.svd(jac, compute_uv=False).min())
    grid_y, grid_x = np.indices((size, size), dtype=float)
    grid_x -= (size - 1) / 2
    grid_y -= (size - 1) / 2
    for _ in range(24):
        tangent = step * (grid_x[..., None] * basis_x + grid_y[..., None] * basis_y)
        radius = np.linalg.norm(tangent, axis=-1)
        rays = np.cos(radius[..., None]) * direction + np.sinc(radius[..., None] / np.pi) * tangent
        xy = _direction_to_pixel(geometry, rays)
        inside = (
            (xy[..., 0] >= c0)
            & (xy[..., 0] <= c1 - 1)
            & (xy[..., 1] >= r0)
            & (xy[..., 1] <= r1 - 1)
        )
        if inside.all():
            break
        step *= 0.9
    else:
        return {"status": "unavailable", "reason": "no_supported_uniform_angular_grid"}
    coordinates = [xy[..., 1] - r0, xy[..., 0] - c0]
    sampled_linear = map_coordinates(linear, coordinates, order=1, mode="nearest", prefilter=False)
    sampled_processed = map_coordinates(
        processed, coordinates, order=1, mode="nearest", prefilter=False
    )
    hint = np.array([np.cos(angle), -np.sin(angle)])
    fitted, center = _fit_normal(sampled_linear, hint)
    degrees = float(np.degrees(step))
    angular = {
        "frequency_equivalent_degrees_per_pixel": degrees,
        "angular_axis_status": "uniform_tangent_angle_grid",
        "angular_frequency_qualified": True,
    }

    def measure(image):
        result = _sfr(image, fitted, center, None, angular)
        if result["status"] != "ok":
            return result
        result["method"] = "bilinear_uniform_tangent_angle_edge_sfr"
        result["frequency"] = {"cycles_per_degree": result["frequency"]["cycles_per_degree"]}
        for key in ("mtf50_cycles_per_pixel", "mtf50_cycles_per_mm", "nyquist_cycles_per_pixel"):
            result.pop(key, None)
        result["esf_position_angular_samples"] = result.pop("esf_position_native_pixels")
        result["native_pixels_interpolated"] = True
        result["interpretation"] = (
            "Angular resampling diagnostic; bilinear interpolation affects the response. "
            "Native SFR remains the independent sensor-grid measurement."
        )
        return result

    return {
        "status": "ok",
        "method": "spherical_exponential_map_uniform_tangent_angle_grid",
        "grid_shape": [size, size],
        "grid_step_degrees": degrees,
        "center_direction_xyz": direction.tolist(),
        "tangent_basis_xyz": [basis_x.tolist(), basis_y.tolist()],
        "maximum_tangent_chart_metric_variation": float(1 - np.sinc(radius.max() / np.pi)),
        "all_samples_inside_original_roi": True,
        "source_pixel_center_bounds_row_col": [
            float(xy[..., 1].min()),
            float(xy[..., 1].max()),
            float(xy[..., 0].min()),
            float(xy[..., 0].max()),
        ],
        "interpolation": "bilinear; no extrapolation and no claim of native-resolution recovery",
        "linear_system": measure(sampled_linear),
        "processed_system": measure(sampled_processed),
    }


def resolution_metrics(
    linear_rgb: Any,
    processed_rgb: Any,
    *,
    sites: Mapping[str, Any],
    geometry: Mapping[str, Any],
    angular_variation_tolerance: float = 0.05,
):
    """Measure nine native sites, each with radial and tangential edge ROIs.

    ``sites[id]`` contains ``center_row_col`` and ``radial``/``tangential``
    entries, each with ``bounds_row_col`` and native ``normal_xy``. ROI bounds
    are half-open. The same edge fit is used for linear and processed images.
    """
    linear, processed = lab._mono(linear_rgb), lab._mono(processed_rgb)
    _, height, width, _ = _geometry(geometry)
    if linear.shape != processed.shape or linear.shape != (height, width):
        raise ValueError("Wide SFR requires both full native arrays at the declared native shape")
    if not isinstance(sites, Mapping) or len(sites) != 9:
        raise ValueError("Wide resolution metrics require exactly nine declared sites")
    pitch = geometry.get("pixel_pitch_um")
    if pitch is not None:
        pitch = lab._positive(pitch, "pixel_pitch_um")
    results = {}
    for name, site in sites.items():
        entry = {"center_row_col": list(site["center_row_col"])}
        for direction in ("radial", "tangential"):
            edge = site[direction]
            bounds, hint = edge["bounds_row_col"], _normal(edge["normal_xy"])
            index = lab._roi(bounds, linear.shape)
            normal, center = _fit_normal(linear[index], hint)
            angular = angular_geometry(
                geometry, bounds, normal, variation_tolerance=angular_variation_tolerance
            )
            entry[direction] = {
                "roi": list(bounds),
                "normal_xy": normal.tolist(),
                "declared_normal_xy": hint.tolist(),
                "orientation_change_deg": float(
                    np.degrees(np.arccos(np.clip(np.dot(normal, hint), -1, 1)))
                ),
                "angular_geometry": angular,
                "linear_system": _sfr(linear[index], normal, center, pitch, angular),
                "processed_system": _sfr(processed[index], normal, center, pitch, angular),
            }
            if not angular["angular_frequency_qualified"]:
                entry[direction]["angular_resampled"] = angular_resampled_metrics(
                    linear[index],
                    processed[index],
                    geometry=geometry,
                    bounds=bounds,
                    normal_xy=normal,
                )
        # Compatibility for existing top-line KPI renderers; new views should
        # show both explicitly labeled native directions.
        entry["roi"] = entry["radial"]["roi"]
        entry["linear_system"] = entry["radial"]["linear_system"]
        entry["processed_system"] = entry["radial"]["processed_system"]
        results[str(name)] = entry
    return {
        "schema": SCHEMA,
        "status": "ok",
        "native_shape": [height, width],
        "projection": geometry["projection"],
        "sites": results,
        "directions": ["radial", "tangential"],
        "frequency_basis": "native_pixel_and_local_projection_jacobian",
        "main_chart_scope": (
            "Nine native directional chart sites; qualified angular axes use their local "
            "projection differential. Nonlinear ROI angular axes are withheld."
        ),
        "interpretation": (
            "System SFR; linear and processed responses separated. Projection and blur "
            "are declared models, not product rankings."
        ),
    }

"""Full observation field PSF with declared chief or pupil relative response.

Spectral planes stream through existing CameraE2E geometry/PSF operations.
Normal-incidence QE is applied once; the angular model contributes a ratio.
"""

from __future__ import annotations

import math

import numpy as np

SITES = ["R", "Gr", "Gb", "B"]
ANGLES = np.arange(0.0, 361.0, 45.0)
ELEMENTARY_CHARGE = 1.602176634e-19


def source_support(optics, receiver_shape, spacing_mm, wave_nm):
    """Bound inverse-distortion source support before allocating image planes.

    The receiver includes the complete PSF halo. A piecewise-linear radial
    map's radius ratio has extrema at its knots, so these bounds cover every
    receiver coordinate, not only corners at a single wavelength.
    """
    from pyisetcam.optics import _strict_raytrace_field_positions, _strict_raytrace_geometry_axes

    radius = math.hypot((receiver_shape[0] - 1) / 2, (receiver_shape[1] - 1) / 2) * spacing_mm
    maximum = 1.0
    if "raytrace" in optics:
        ray = optics["raytrace"]
        for wavelength in wave_nm:
            ideal, distorted = _strict_raytrace_geometry_axes(ray, float(wavelength))
            for name in ("psf", "relative_illumination"):
                support = _strict_raytrace_field_positions(ray, ray[name], float(wavelength))
                if radius > support[-1] + 1e-9:
                    raise ValueError(f"Full receiver and PSF halo exceed {name} field support")
            if radius > distorted[-1] + 1e-9:
                raise ValueError("Full receiver and PSF halo exceed geometry support")
            knots = np.r_[distorted[(distorted > 0) & (distorted < radius)], radius]
            maximum = max(maximum, float(np.max(np.interp(knots, distorted, ideal) / knots)))
    # Two source cells preserve the bilinear stencil at the extreme query.
    source_shape = tuple(
        2 * int(math.ceil(((n - 1) / 2 * maximum + 2) + 0.5)) for n in receiver_shape
    )
    crop = tuple((s - n) // 2 for s, n in zip(source_shape, receiver_shape, strict=True))
    return {
        "source_shape": list(source_shape),
        "receiver_shape": list(receiver_shape),
        "crop_row_col": list(crop),
        "maximum_inverse_scale": maximum,
        "spacing_mm": float(spacing_mm),
        "all_wavelengths_checked": list(map(float, wave_nm)),
    }


def mapped_chart_rois(optics, geometry, rois, wave_nm):
    """Conservative interior rectangles across all spectral geometry mappings."""
    if "raytrace" not in optics:
        return {key: list(value) for key, value in rois.items()}
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    width = (
        2
        * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
        * geometry["focal_length_mm"]
    )
    spacing = width / 640
    result = {}
    for key, (r0, r1, c0, c1) in rois.items():
        mapped = []
        for wavelength in wave_nm:
            ideal, distorted = _strict_raytrace_geometry_axes(optics["raytrace"], float(wavelength))

            def point(row, col, ideal=ideal, distorted=distorted):
                y = (row - 180) * spacing
                x = (col - 320) * spacing
                radius = np.hypot(x, y)
                factor = np.divide(
                    np.interp(radius, ideal, distorted),
                    radius,
                    out=np.ones_like(radius, dtype=float),
                    where=radius > 0,
                )
                return 180 + y * factor / spacing, 320 + x * factor / spacing

            top = point(np.full(17, r0), np.linspace(c0, c1, 17))[0].max()
            bottom = point(np.full(17, r1), np.linspace(c0, c1, 17))[0].min()
            left = point(np.linspace(r0, r1, 17), np.full(17, c0))[1].max()
            right = point(np.linspace(r0, r1, 17), np.full(17, c1))[1].min()
            mapped.append([top, bottom, left, right])
        values = np.asarray(mapped)
        bounds = [
            int(math.ceil(values[:, 0].max())),
            int(math.floor(values[:, 1].min())),
            int(math.ceil(values[:, 2].max())),
            int(math.floor(values[:, 3].min())),
        ]
        if bounds[1] - bounds[0] < 8 or bounds[3] - bounds[2] < 8:
            raise ValueError(f"Distorted chart ROI {key} has insufficient common spectral interior")
        result[key] = bounds
    return result


def field_coordinates(shape, spacing_mm):
    """Optical +y up, unlike the increasing row index of the raster."""
    rows, cols = shape
    yy, xx = np.meshgrid(
        -((np.arange(rows) + 0.5) - rows / 2) * spacing_mm,
        ((np.arange(cols) + 0.5) - cols / 2) * spacing_mm,
        indexing="ij",
    )
    return xx, yy


def chief_ray_maps(optics, shape, spacing_mm, wavelength_nm):
    """Interpolate stored +x chiefs, then rotate into each receiver azimuth.

    The receiver's distorted radius is inverted to the original field axis.
    Returned phi is +x toward increasing sensor row (row-down convention).
    """
    xx, yy = field_coordinates(shape, spacing_mm)
    radius = np.hypot(xx, yy)
    if "raytrace" not in optics:
        dx, dy = xx / (optics["focal_length_m"] * 1000), -yy / (optics["focal_length_m"] * 1000)
        norm = np.sqrt(1 + dx * dx + dy * dy)
        theta = np.degrees(np.arccos(1 / norm))
        phi = np.mod(np.degrees(np.arctan2(dy, dx)), 360)
        return theta, phi, {"origin": "analytic_thin_lens_chief_ray_proxy"}
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    ray = optics["raytrace"]
    table = ray["psf"]
    waves = np.asarray(table["wavelength_nm"], float)
    fields = np.asarray(table["field_height_mm"], float)
    vectors = np.asarray(ray.get("chief_direction_cosines"), float)
    if vectors.shape != (fields.size, waves.size, 3) or not np.isfinite(vectors).all():
        raise ValueError("Lens DB has no finite bound chief-ray vector table")
    if wavelength_nm < waves[0] or wavelength_nm > waves[-1]:
        raise ValueError("CRA wavelength lies outside stored lens support")
    ideal, distorted = _strict_raytrace_geometry_axes(ray, float(wavelength_nm))
    if radius.max() > distorted[-1] + 1e-9:
        raise ValueError("Full optical field and PSF halo exceed stored CRA/geometry support")
    height = np.interp(radius, distorted, ideal)
    if height.max() > fields[-1] + 1e-9:
        raise ValueError("Lens chief-ray field interpolation would extrapolate")
    spectral = np.array(
        [
            [np.interp(wavelength_nm, waves, vectors[f, :, c]) for c in range(3)]
            for f in range(fields.size)
        ]
    )
    dx, dy, dz = [np.interp(height, fields, spectral[:, c]) for c in range(3)]
    norm = np.sqrt(dx * dx + dy * dy + dz * dz)
    if np.any(norm <= 0) or np.any(dz <= 0):
        raise ValueError("Chief rays must have positive forward z and nonzero norm")
    dx, dy, dz = dx / norm, dy / norm, dz / norm
    azimuth = np.arctan2(yy, xx)
    xr = dx * np.cos(azimuth) - dy * np.sin(azimuth)
    yr_down = -(dx * np.sin(azimuth) + dy * np.cos(azimuth))
    theta = np.degrees(np.arccos(np.clip(dz, -1, 1)))
    phi = np.mod(np.degrees(np.arctan2(yr_down, xr)), 360)
    return (
        theta,
        phi,
        {
            "origin": "lens_db_chief_direction",
            "coordinate_convention": "lens+y_up→sensor_row_down; phi+x_to_row_down",
            "lens_asset_sha256": ray.get("asset_sha256"),
            "interpolation": "wavelength and inverse-distorted field, normalized vector",
        },
    )


def relative_gain(angular, table, theta, phi, wavelength_nm):
    mode = angular["mode"]
    if mode == "off":
        return np.ones((*np.shape(theta), 4), float)
    if mode == "cosine_power":
        powers = np.asarray(angular["exponents_r_gr_gb_b"], float)
        return np.cos(np.radians(theta))[..., None] ** powers
    if mode != "physical_relative" or table is None:
        raise ValueError("Full-field angular model is not available")
    from .virtual_lab_angular import interpolate_relative

    return interpolate_relative(table, theta, phi, float(wavelength_nm))


def integrate_current_density(sensor, oi, density, *, separate_greens=False):
    """Native photosensitive-area integration; input is electrons/s/m²."""
    from pyisetcam.sensor import (
        _sensor_apply_physics_response,
        _sensor_etendue,
        _spatial_integrate_current_density,
    )

    working = sensor.clone()
    if separate_greens:
        working.fields["pattern"] = np.array([[1, 2], [3, 4]])
    current = _spatial_integrate_current_density(density * ELEMENTARY_CHARGE, oi, working)
    current = _sensor_apply_physics_response(working, current)
    return (
        current
        / ELEMENTARY_CHARGE
        * float(sensor.fields["integration_time"])
        * _sensor_etendue(working)
    )


def _profile(theta, gain):
    rows, cols = theta.shape
    y, x = np.meshgrid(np.linspace(-1, 1, rows), np.linspace(-1, 1, cols), indexing="ij")
    radius = np.hypot(x, y) / math.sqrt(2)
    edges = np.linspace(0, 1 + 1e-8, 25)
    index = np.clip(np.digitize(radius, edges) - 1, 0, 23)
    radii, angles, values = [], [], []
    for n in range(24):
        mask = index == n
        if mask.any():
            radii.append(float(radius[mask].mean()))
            angles.append(float(theta[mask].mean()))
            values.append(gain[mask].mean(axis=0).tolist())
    return {"radius_fraction": radii, "cra_deg": angles, "gain_r_gr_gb_b": values}


def capture(optics, sensor, spectra, kind, geometry, controls, seed, context):
    from scipy.signal import fftconvolve

    from pyisetcam.optics import (
        _radiance_to_irradiance,
        _strict_raytrace_apply_psf_bounded,
        _strict_raytrace_geometry,
    )
    from pyisetcam.sensor import _sensor_qe_on_wave
    from pyisetcam.sensor_capture import resample_sensor_capture
    from pyisetcam.types import OpticalImage, Scene

    from .virtual_lab_engine import EDGE_ROIS, WAVE, _kernel, _patch_rois, _pattern

    rows, cols = sensor.fields["size"]
    pitch = float(sensor.fields["pixel"]["size_m"][0])
    spacing = pitch / 2
    # Support depends on stored PSF dimensions; never truncate a nonzero kernel.
    center_psf = _kernel(optics, 550.0, spacing * 1e6, controls=controls)
    if "raytrace" in optics:
        from pyisetcam.optics import _raytrace_expected_kernel_shape

        # Whole table support is shared by every field and wavelength.
        kernel_shape = _raytrace_expected_kernel_shape(optics, spacing)
        halo = max(kernel_shape) // 2 + 8
    else:
        halo = (
            max(
                max(_kernel(optics, float(w), spacing * 1e6, controls=controls).shape) // 2
                for w in WAVE
            )
            + 8
        )
    shape = (rows * 2 + 2 * halo, cols * 2 + 2 * halo)
    support = source_support(optics, shape, spacing * 1000, WAVE)
    source_shape = tuple(support["source_shape"])
    source_crop = tuple(support["crop_row_col"])
    common_width = (
        2
        * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
        * geometry["focal_length_mm"]
        * 1000
        / geometry["pixel_pitch_um"]
    )
    pattern = _pattern(
        kind,
        *source_shape,
        scale_row_col=(
            source_shape[0] / (common_width * 2 * 9 / 16),
            source_shape[1] / (common_width * 2),
        ),
    )
    qe = _sensor_qe_on_wave(sensor, WAVE)
    angular = context["angular_response"]
    table = context.get("angular_table")
    pupil_averaged = angular["mode"] == "physical_pupil"
    if pupil_averaged:
        from .virtual_lab_pupil import pupil_gain_map

        if context.get("pupil_table") is None:
            raise ValueError("Physical pupil mode requires its saved pupil averaging table")
    density = np.zeros((*shape, 4), np.float32)
    before = np.zeros((*shape, 3), np.float32)
    preview = np.zeros((rows, cols), float)
    executions = []
    field_coverage = []
    for band, wavelength in enumerate(WAVE):
        wave = np.array([wavelength])
        radiance = (
            spectra["patches"][pattern, band]
            if kind == "color"
            else pattern * spectra["white"][band]
        )
        # Check angular table support before the costly field convolutions.
        theta, phi, origin = chief_ray_maps(optics, shape, spacing * 1000, float(wavelength))
        gain = (
            pupil_gain_map(optics, context["pupil_table"], shape, spacing * 1000, float(wavelength))
            if pupil_averaged
            else relative_gain(angular, table, theta, phi, float(wavelength))
        )
        hfov = math.degrees(
            2 * math.atan(source_shape[1] * spacing / (2 * geometry["focal_length_mm"] * 0.001))
        )
        vfov = math.degrees(
            2 * math.atan(source_shape[0] * spacing / (2 * geometry["focal_length_mm"] * 0.001))
        )
        scene = Scene(
            fields={"wave": wave, "distance_m": float("inf"), "fov_deg": hfov, "vfov_deg": vfov}
        )
        irradiance = _radiance_to_irradiance(radiance[..., None], optics, scene)
        if "raytrace" in optics:
            distorted = _strict_raytrace_geometry(irradiance, wave, optics, scene)
            cr, cc = source_crop
            distorted = distorted[cr : cr + shape[0], cc : cc + shape[1]]
            optical, execution = _strict_raytrace_apply_psf_bounded(
                distorted,
                optics,
                wave,
                spacing,
                (0, 0),
                ANGLES,
                max_cached_kernels=16,
            )
            plane = optical[..., 0]
            executions.append(execution)
        else:
            kernel = _kernel(optics, float(wavelength), spacing * 1e6, controls=controls)
            cr, cc = source_crop
            plane = fftconvolve(irradiance[..., 0], kernel, mode="same")[
                cr : cr + shape[0], cc : cc + shape[1]
            ]
        if not np.isfinite(gain).all() or np.any(gain < 0):
            raise ValueError("Angular response produced an invalid relative gain")
        for channel in range(3):
            before[..., channel] += np.asarray(plane * qe[band, channel] * 10, np.float32)
        for site, channel in enumerate([0, 1, 1, 2]):
            density[..., site] += np.asarray(
                plane * qe[band, channel] * gain[..., site] * 10, np.float32
            )
        preview += plane[halo : halo + 2 * rows : 2, halo : halo + 2 * cols : 2] / len(WAVE)
        field_coverage.append(
            {"wavelength_nm": float(wavelength), "cra_max_deg": float(theta.max())}
        )
        if context.get("on_band"):
            context["on_band"](band + 1, len(WAVE))
        del irradiance, plane, theta, phi, gain
    oi = OpticalImage(
        fields={
            "wave": WAVE,
            "sample_spacing_m": spacing,
            "width_m": shape[1] * spacing,
            "height_m": shape[0] * spacing,
        },
        data={"photons": np.empty((*shape, 0))},
    )
    mean = integrate_current_density(sensor, oi, density, separate_greens=True)
    mean_before = integrate_current_density(sensor, oi, before)
    del density, before
    result = sensor.clone()
    result.data = {"mean_electrons": np.maximum(mean, 0)}
    result = resample_sensor_capture(result, seed=seed)
    theta, phi, origin = chief_ray_maps(optics, (rows, cols), pitch * 1000, 550.0)
    gain = (
        pupil_gain_map(optics, context["pupil_table"], (rows, cols), pitch * 1000, 550.0)
        if pupil_averaged
        else relative_gain(angular, table, theta, phi, 550.0)
    )
    if angular["mode"] in {"physical_relative", "physical_pupil"}:
        provenance = dict(table["provenance"])
        if pupil_averaged:
            provenance["pupil_average"] = context["pupil_table"]["provenance"]
    else:
        provenance = {
            "model": angular["mode"],
            "exponents_r_gr_gb_b": angular["exponents_r_gr_gb_b"],
            "normalization": "relative_to_normal_incidence",
            "normal_qe_applied_once": True,
        }
    summary = {
        "status": "ok",
        "mode": "full_field",
        "angular_mode": angular["mode"],
        "chief_ray_only": not pupil_averaged,
        "pupil_averaged": pupil_averaged,
        "cra_origin": origin["origin"],
        "coordinate_convention": "lens+y_up→sensor_row_down; phi+x_to_row_down",
        "reference_wavelength_nm": 550.0,
        "native_shape": [rows, cols],
        "psf_azimuth_step_deg": 45.0,
        "full_field_geometry_applied": "raytrace" in optics,
        "cra_deg": {
            "min": float(theta.min()),
            "max": float(theta.max()),
            "mean": float(theta.mean()),
        },
        "gain_r_gr_gb_b": {
            "min": gain.min(axis=(0, 1)).tolist(),
            "max": gain.max(axis=(0, 1)).tolist(),
            "mean": gain.mean(axis=(0, 1)).tolist(),
        },
        "before_after_mean_electrons": {
            "without_angular": float(mean_before.mean()),
            "with_angular": float(mean.mean()),
            "ratio": float(mean.mean() / mean_before.mean()) if mean_before.mean() > 0 else None,
        },
        "profile": _profile(theta, gain),
        "angular_provenance": provenance,
        "wavelength_coverage": field_coverage,
        "optical_executions": executions,
        "input_support": support,
        "gain_map_display_range": [0, 1.5],
        "cra_map_display_range_deg": [0, 60],
        "not_pupil_averaged": not pupil_averaged,
        "qe_application": "saved normal QE × relative angular ratio once",
        "angular_coupling_scope": (
            (
                "receiver-side field-separable conditional pupil-direction average; "
                if pupil_averaged
                else "receiver-side field-separable chief-ray approximation; "
            )
            + "not joint ray-hit/angle integration"
        ),
        "roi_mapping": (
            "common-output interiors of forward-distorted source ROI across all wavelengths"
        ),
    }
    if pupil_averaged:
        summary["pupil_sampling"] = context["pupil_table"]["sampling"]
        summary["pupil_table_sha256"] = context["pupil_table"]["table_sha256"]
        summary["pupil_table_artifact"] = context.get("pupil_table_artifact")
    if kind == "resolution":
        summary["evaluation_rois"] = mapped_chart_rois(optics, geometry, EDGE_ROIS, WAVE)
    if kind == "color":
        summary["evaluation_rois"] = mapped_chart_rois(
            optics, geometry, {str(i): b for i, b in enumerate(_patch_rois())}, WAVE
        )
    if kind == "tone":
        summary["evaluation_rois"] = mapped_chart_rois(
            optics,
            geometry,
            {str(i): [156, 204, (i + 0.25) * 40, (i + 0.75) * 40] for i in range(16)},
            WAVE,
        )
    result.metadata["virtual_lab_field"] = {
        "summary": summary,
        "cra_deg": theta.astype(np.float32),
        "gain_r_gr_gb_b": gain.astype(np.float32),
        "mean_without_angular": mean_before.astype(np.float32),
    }
    return result, preview, center_psf

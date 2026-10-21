"""Camera Virtual Lab: pinned optical PSFs, native sensor capture and RAW replay.

This bounded lab adapter supports full-field or local optical response.
The displayed chart is a native sensor observation, not an enlarged tiny RAW.
It does not turn stored geometric ray PSFs into diffraction/CRA measurements.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, map_coordinates, rotate
from scipy.signal import fftconvolve
from scipy.special import j1

from . import virtual_lab_metrics as metrics
from .road_rgb_source import file_sha256

SCHEMA = "camera_virtual_lab_result_v1"
WAVE = np.arange(450.0, 651.0, 10.0)
COMMON = (360, 640)
DISPLAY_REFERENCE_LUMINANCE = 1000.0
MAX_NATIVE_PIXELS = 4_000_000
PINNED = "reports/camera_platform/road-design-20261003-reviewed/specification.json"
XYZ_TO_RGB = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.969266, 1.8760108, 0.041556],
        [0.0556434, -0.2040259, 1.0572252],
    ]
)
EDGE_ROIS = {
    "center": [148, 212, 288, 352],
    "top_left": [40, 104, 64, 128],
    "top_right": [40, 104, 512, 576],
    "bottom_left": [256, 320, 64, 128],
    "bottom_right": [256, 320, 512, 576],
}


def _json(path, value):
    Path(path).write_text(
        json.dumps(
            value,
            indent=2,
            allow_nan=False,
            default=lambda x: x.tolist() if isinstance(x, np.ndarray) else x.item(),
        ),
        encoding="utf-8",
    )


def _pinned(workspace):
    return json.loads((Path(workspace) / PINNED).read_text())


def catalog(workspace):
    source = _pinned(workspace)
    lenses, sensors = {}, {}
    for candidate in source["candidates"]:
        lid, sid = candidate["id"].split("-")
        lens, sensor = candidate["lens"], candidate["sensor"]
        lenses[lid] = {
            "id": lid,
            "label": (
                f"{lid} · {lens['model']['effective_focal_length_mm']:.2f} mm "
                f"f/{lens['model']['effective_f_number']:.2f}"
            ),
            "f_number": round(lens["model"]["effective_f_number"], 2),
            "description": "Stored geometric ray PSF; fixed lens; unknown unity transmission",
            "supports": {"f_number": False, "defocus_um": False, "pupil_directions": True},
            "model": lens["model"],
            "revision": lens["identity"],
        }
        sensors[sid] = {
            "id": sid,
            "label": (
                f"{sid} · {sensor['revision']['report_code']} · "
                f"{sensor['native_response']['pixel_pitch_um']:.2f} µm"
            ),
            "description": "Saved DB estimate; assumed readout, RGGB",
            "supports": {"exposure": True, "gain": True},
            "revision": sensor["revision"],
            "pixel_pitch_um": sensor["native_response"]["pixel_pitch_um"],
        }
    lenses["analytic"] = {
        "id": "analytic",
        "label": "General diffraction optics",
        "description": "Analytic Airy PSF + geometric defocus disk; focal length fixed to L1",
        "supports": {"f_number": True, "defocus_um": True, "pupil_directions": False},
    }
    lenses["wide_parametric"] = {
        "id": "wide_parametric",
        "label": "Wide parametric ·120° /2MP",
        "description": "Declared rectilinear/equidistant projection and exit-pupil model; "
        "not a DB lens prescription",
        "supports": {"f_number": True, "defocus_um": True, "pupil_directions": True},
    }
    return {
        "schema": "camera_virtual_lab_catalog_v1",
        "lenses": list(lenses.values()),
        "sensors": list(sensors.values()),
        "isp_profiles": [
            {"id": "balanced", "label": "Balanced"},
            {"id": "low_noise", "label": "Low noise"},
            {"id": "detail", "label": "High sharpness"},
        ],
        "scenes": ["resolution", "color", "uniform", "tone", "natural", "grid"],
        "tests": ["all", "resolution", "color", "noise", "tone"],
        "illuminants": ["D65", "A"],
        "output_shape": list(COMMON),
        "spectral_support_nm": [450, 650],
        "field_modes": ["center_local", "full_field"],
        "angular_response_modes": ["off", "physical_relative", "physical_pupil", "cosine_power"],
        "maximum_native_pixels": MAX_NATIVE_PIXELS,
        "limitations": [
            "Full field uses stored PSF/geometry and chief or pupil relative angular response; "
            "local mode remains available.",
            "DB PSFs omit diffraction; transmission/relative illumination are unknown unity.",
            "Sensor QE/readout are model estimates, not measured product modes.",
        ],
    }


def _load(workspace, specification):
    from .lens_sensor_streamed_raw_job import _models

    source = _pinned(workspace)
    lens_id = specification["lens_id"]
    source_lens_id = "L1" if lens_id in {"analytic", "wide_parametric"} else lens_id
    candidate = next(
        c
        for c in source["candidates"]
        if c["id"] == f"{source_lens_id}-{specification['sensor_id']}"
    )
    baseline = source["candidates"][0]
    width_tangent = (
        640
        * baseline["sensor"]["native_response"]["pixel_pitch_um"]
        / 1000
        / baseline["lens"]["model"]["effective_focal_length_mm"]
    )
    field_model = specification.get(
        "field_model", {"mode": "center_local", "horizontal_fov_deg": None}
    )
    if field_model.get("horizontal_fov_deg") is not None:
        width_tangent = 2 * math.tan(math.radians(field_model["horizontal_fov_deg"]) / 2)
    pitch = candidate["sensor"]["native_response"]["pixel_pitch_um"]
    focal = candidate["lens"]["model"]["effective_focal_length_mm"]
    cols = 2 * math.ceil(width_tangent * focal * 1000 / pitch / 2)
    rows = 2 * math.ceil(cols * 9 / 16 / 2)
    if lens_id == "wide_parametric":
        rows, cols = map(int, specification["wide_angle"]["native_shape_row_col"])
    if rows * cols > MAX_NATIVE_PIXELS and lens_id != "wide_parametric":
        suggested = math.degrees(
            2 * math.atan(math.sqrt(MAX_NATIVE_PIXELS * 16 / 9) * pitch / 1000 / focal / 2)
        )
        raise ValueError(
            f"Native resource policy: {cols}×{rows}={cols * rows:,} pixels exceeds "
            f"{MAX_NATIVE_PIXELS:,}; choose FOV below {suggested:.2f}°. "
            "Pixel pitch and optical sampling were not lowered."
        )
    revision = candidate["sensor"]["revision"]
    request = {
        "lens_revision_id": candidate["lens"]["identity"]["revision_id"],
        "sensor_revision": {"identifier": revision["report_code"], "run_id": revision["run_id"]},
        "virtual_sensor_geometry": {"rows": rows, "cols": cols, "pixel_pitch_um": pitch},
        "analytic_scene": {"object_distance_m": "infinity"},
        "capture": {"exposure_s": specification["exposure_ms"] / 1000, "noise_flag": 2},
    }
    lens, saved_sensor, optics, sensor = _models(
        SimpleNamespace(workspace=Path(workspace)), request
    )
    if lens_id not in {"analytic", "wide_parametric"} and (
        not math.isclose(
            specification["optics"]["f_number"], lens["model"]["effective_f_number"], abs_tol=1e-9
        )
        or specification["optics"]["defocus_um"] != 0
    ):
        raise ValueError(
            "Stored DB PSF has fixed f-number and focus; select analytic optics for controls"
        )
    sensor.fields["sensor_readout"]["gain"] = float(specification["gain"])
    sensor.fields["fixed_pattern_seed"] = 1701
    sensor.fields["noise_flag"] = 2
    if lens_id in {"analytic", "wide_parametric"}:
        optics = {
            "f_number": specification["optics"]["f_number"],
            "focal_length_m": focal / 1000,
            "model": "shiftinvariant",
            "transmittance": {"wave": WAVE, "scale": np.ones(WAVE.size)},
        }
    if lens_id == "wide_parametric":
        from .virtual_lab_wide import _digest
        from .virtual_lab_wide_geometry import load_geometry

        geometry = load_geometry(sensor, specification)
        optics["focal_length_m"] = geometry["focal_length_mm"] * 0.001
        optics["model"] = "wide_parametric"
        revision = _digest(
            {
                "wide_angle": specification["wide_angle"],
                "optics": specification["optics"],
                "geometry": geometry,
            }
        )
        lens = {
            "identity": {
                "revision_id": "wide-parametric-" + revision[:16],
                "asset_sha256": revision,
                "source": "declared_parameters_not_DB_lens",
            },
            "model": {
                "effective_f_number": specification["optics"]["f_number"],
                "effective_focal_length_mm": geometry["focal_length_mm"],
                "projection": geometry["projection"],
            },
        }
        return lens, saved_sensor, optics, sensor, geometry
    return (
        lens,
        saved_sensor,
        optics,
        sensor,
        {
            "native_shape": [rows, cols],
            "common_shape": list(COMMON),
            "pixel_pitch_um": pitch,
            "focal_length_mm": focal,
            "common_horizontal_fov_deg": math.degrees(2 * math.atan(width_tangent / 2)),
            "native_horizontal_fov_deg": math.degrees(
                2 * math.atan(cols * pitch / 1000 / focal / 2)
            ),
            "kind": "virtual_native_sensor_window",
            "not_a_product_active_mode": True,
            "source_samples_per_sensor_pixel": 2,
            "field_model": field_model,
        },
    )


def _spectra(workspace, illuminant, luminance):
    from pyisetcam.color import xyz_from_energy
    from pyisetcam.scene import scene_create
    from pyisetcam.utils import quanta_to_energy

    from .offline_assets import offline_asset_store

    assets = offline_asset_store(workspace=workspace, source=Path(workspace) / "camerae2e_db")
    with assets.bound_default(assets):
        scene = scene_create(
            "macbeth d65" if illuminant == "D65" else "macbeth tungsten",
            8,
            WAVE,
            asset_store=assets,
        )
        white = np.asarray(scene.fields["illuminant_photons"], float).reshape(-1)
        patches = np.asarray(scene.data["photons"])[4::8, 4::8].reshape(24, WAVE.size)
        white_xyz = np.asarray(
            xyz_from_energy(quanta_to_energy(white, WAVE), WAVE, asset_store=assets)
        ).reshape(3)
        scale = float(luminance) / white_xyz[1]
        patches, white = patches * scale, white * scale
        cmf_photons = np.asarray(
            xyz_from_energy(quanta_to_energy(np.eye(WAVE.size), WAVE), WAVE, asset_store=assets)
        )
        # The supported450–650nm band cuts part of the blue D65 spectrum.
        # Adopt one fixed development white, never the current scene's white:
        # map that known band-limited D65 to the standard sRGB D65 white.
        # The identical transform applies to A and all luminance settings.
        d65_scene = (
            scene
            if illuminant == "D65"
            else scene_create("macbeth d65", 8, WAVE, asset_store=assets)
        )
        d65_photons = np.asarray(d65_scene.fields["illuminant_photons"], float).reshape(-1)
        band_white = d65_photons @ cmf_photons
        band_white /= band_white[1]
        standard_white = np.array([0.95047, 1.0, 1.08883])
        bradford = np.array(
            [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]
        )
        adaptation = (
            np.linalg.inv(bradford)
            @ np.diag((bradford @ standard_white) / (bradford @ band_white))
            @ bradford
        )
        source_white_xyz = white @ cmf_photons / DISPLAY_REFERENCE_LUMINANCE
        cmf_photons = cmf_photons @ adaptation.T
        reference = patches @ cmf_photons / DISPLAY_REFERENCE_LUMINANCE
        white_xyz = white @ cmf_photons / DISPLAY_REFERENCE_LUMINANCE
    return {
        "patches": patches,
        "white": white,
        "xyz_per_photon": cmf_photons,
        "reference_xyz": reference,
        "white_xyz": white_xyz,
        "source_white_xyz": source_white_xyz,
        "chromatic_adaptation": {
            "method": "fixed_Bradford_band_D65_to_standard_D65",
            "source_white_xyz": band_white.tolist(),
            "target_white_xyz": standard_white.tolist(),
            "matrix_xyz_output_rows": adaptation.tolist(),
            "scene_white_estimated": False,
            "same_transform_for_D65_and_A": True,
            "missing_spectral_content_recovered": False,
        },
        "asset": "CameraE2E macbethChart reflectance; selected illuminant",
        "band_limit_nm": [450, 650],
    }


def _kernel(optics, wavelength, pitch_um, field_fraction=0.0, angle=0.0, controls=None):
    if "raytrace" not in optics:
        fn = float(optics["f_number"])
        radius_um = max(
            10 * wavelength * 0.001 * fn, abs(controls["defocus_um"]) / (2 * fn) + 4 * pitch_um
        )
        side = 2 * int(math.ceil(radius_um / pitch_um)) + 1
        side = min(side, 513)
        yy, xx = np.meshgrid(
            np.arange(side) - side // 2, np.arange(side) - side // 2, indexing="ij"
        )
        rr = np.hypot(xx, yy) * pitch_um
        z = math.pi * rr / (wavelength * 0.001 * fn)
        kernel = np.divide(2 * j1(z), z, out=np.ones_like(z), where=z != 0) ** 2
        blur_radius = abs(controls["defocus_um"]) / (2 * fn)
        if blur_radius > pitch_um / 4:
            disk = (rr <= blur_radius).astype(float)
            kernel = fftconvolve(kernel, disk / disk.sum(), mode="same")
        return kernel / kernel.sum()
    from pyisetcam.optics import (
        _raytrace_psf_grid,
        _raytrace_psf_support_axes,
        _strict_raytrace_spectral,
    )

    table = dict(optics["raytrace"]["psf"], coordinate_convention="pixel_centers")
    samples = _strict_raytrace_spectral(table, wavelength, psf=True)
    heights = np.asarray(table["field_height_mm"], float)
    target = heights[-1] * field_fraction
    low = int(np.clip(np.searchsorted(heights, target, side="right") - 1, 0, heights.size - 2))
    weight = (target - heights[low]) / (heights[low + 1] - heights[low])
    source = (1 - weight) * samples[:, :, low] + weight * samples[:, :, low + 1]
    if field_fraction:
        source = rotate(
            source, angle, reshape=False, order=1, mode="constant", cval=0, prefilter=False
        )
    x, y = _raytrace_psf_support_axes(table)
    tx, ty = _raytrace_psf_grid(pitch_um * 0.001, table)
    rr, cc = np.meshgrid((ty - y[0]) / (y[1] - y[0]), (tx - x[0]) / (x[1] - x[0]), indexing="ij")
    kernel = map_coordinates(source, [rr, cc], order=1, mode="constant", cval=0, prefilter=False)
    if kernel.sum() <= 0:
        raise ValueError("PSF has no energy on the chosen optical grid")
    return kernel / kernel.sum()


def _pattern(kind, rows, cols, *, scale_row_col=(1.0, 1.0)):
    yy, xx = np.meshgrid(
        (np.arange(rows) + 0.5) / rows, (np.arange(cols) + 0.5) / cols, indexing="ij"
    )
    yy = (yy - 0.5) * scale_row_col[0] + 0.5
    xx = (xx - 0.5) * scale_row_col[1] + 0.5
    if kind == "color":
        return np.clip((yy * 4).astype(int), 0, 3) * 6 + np.clip((xx * 6).astype(int), 0, 5)
    if kind == "uniform":
        return np.full((rows, cols), 0.18)
    if kind == "field_edge":
        return np.where(xx - 0.5 - (yy - 0.5) / 12 > 0, 0.75, 0.08)
    if kind == "tone":
        levels = np.geomspace(0.002, 2.0, 16)
        return levels[np.clip((xx * 16).astype(int), 0, 15)]
    if kind == "natural":
        # Deliberately procedural visual context; not a licensed photograph.
        result = 0.14 + 0.35 * (yy < 0.48) + 0.09 * np.sin(45 * xx) ** 2
        road = (yy >= 0.48) & (abs(xx - 0.5) < 0.1 + 0.6 * (yy - 0.48))
        result[road] = 0.11
        lane = road & (abs(xx - 0.5) < 0.004) & (np.sin(70 * yy) > 0)
        result[lane] = 0.8
        result[((xx - 0.66) ** 2 / 0.008**2 + (yy - 0.56) ** 2 / 0.025**2) < 1] = 0.05
        result[(abs(xx - 0.66) < 0.01) & (yy > 0.58) & (yy < 0.72)] = 0.035
        return result
    result = np.full((rows, cols), 0.20)
    for bounds in EDGE_ROIS.values():
        r0, r1, c0, c1 = bounds
        cy, cx = (r0 + r1) / 2 / 360, (c0 + c1) / 2 / 640
        mask = (abs(yy - cy) < 0.12) & (abs(xx - cx) < 0.085)
        threshold = xx - cx - (yy - cy) * 360 / 640 / 12
        result[mask] = np.where(threshold[mask] > 0, 0.75, 0.08)
    return result


def _chart_capture(
    optics,
    sensor,
    spectra,
    kind,
    geometry,
    controls,
    seed,
    field_fraction=0,
    angle=0,
    field_context=None,
):
    if geometry.get("optical_model") == "wide_parametric":
        from .virtual_lab_wide import capture as wide_capture

        return wide_capture(optics, sensor, spectra, kind, geometry, controls, seed, field_context)
    if field_context is not None and geometry.get("field_model", {}).get("mode") == "full_field":
        from .virtual_lab_field import capture

        return capture(optics, sensor, spectra, kind, geometry, controls, seed, field_context)
    from pyisetcam.optics import _radiance_to_irradiance
    from pyisetcam.sensor import sensor_compute
    from pyisetcam.types import OpticalImage, Scene

    rows, cols = sensor.fields["size"]
    pitch = float(sensor.fields["pixel"]["size_m"][0])
    sample_pitch = pitch / 2
    kernels = [
        _kernel(optics, float(w), sample_pitch * 1e6, field_fraction, angle, controls) for w in WAVE
    ]
    halo = max(max(k.shape) // 2 for k in kernels) + 8
    # Extra physical chart input is generated before convolution, not zero padding.
    fullrows, fullcols = 2 * rows + 2 * halo, 2 * cols + 2 * halo
    common_width = (
        2
        * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
        * geometry["focal_length_mm"]
        * 1000
        / geometry["pixel_pitch_um"]
    )
    scale = (
        (1.0, 1.0)
        if kind == "field_edge"
        else (rows / (common_width * 9 / 16), cols / common_width)
    )
    pattern = _pattern(kind, rows * 2, cols * 2, scale_row_col=scale)
    pattern = np.pad(pattern, halo, mode="edge")
    cube = np.empty((fullrows, fullcols, WAVE.size), dtype=np.float32)
    scene = Scene(
        fields={
            "wave": WAVE,
            "distance_m": float("inf"),
            "fov_deg": geometry["native_horizontal_fov_deg"],
        }
    )
    for band, kernel in enumerate(kernels):
        value = (
            spectra["patches"][pattern, band]
            if kind == "color"
            else pattern * spectra["white"][band]
        )
        cube[:, :, band] = fftconvolve(value, kernel, mode="same")
    cube = _radiance_to_irradiance(cube, optics, scene)
    oi = OpticalImage(
        fields={
            "wave": WAVE,
            "sample_spacing_m": sample_pitch,
            "width_m": fullcols * sample_pitch,
            "height_m": fullrows * sample_pitch,
            "optics": optics,
            "fov_deg": geometry["native_horizontal_fov_deg"],
            "rows": fullrows,
            "cols": fullcols,
        },
        data={"photons": cube},
    )
    captured = sensor_compute(sensor, oi, seed=seed)
    optical_preview = cube[halo : halo + 2 * rows : 2, halo : halo + 2 * cols : 2].mean(axis=2)
    return captured, optical_preview, kernels[WAVE.size // 2]


def _calibration(workspace, sensor, optics):
    from pyisetcam.sensor import _sensor_qe_on_wave

    spectral = _spectra(workspace, "D65", DISPLAY_REFERENCE_LUMINANCE)
    qe = _sensor_qe_on_wave(sensor, WAVE)
    area = float(np.prod(sensor.fields["pixel"]["size_m"]) * sensor.fields["pixel"]["fill_factor"])
    # One fixed development condition; independent of evaluated chart or frame.
    reference_f_number = _pinned(workspace)["candidates"][0]["lens"]["model"]["effective_f_number"]
    irradiance = math.pi / (1 + 4 * reference_f_number**2)
    response = qe * spectral["white"][:, None] * area * irradiance * 0.004 * 10
    desired = spectral["xyz_per_photon"] * spectral["white"][:, None] / DISPLAY_REFERENCE_LUMINANCE
    # Constrain the known calibration white while minimizing spectral error.
    # An unconstrained3x3 fit can tint a neutral even with correct sensor data.
    white_response, white_target = response.sum(axis=0), desired.sum(axis=0)
    system = np.block(
        [
            [response.T @ response, white_response[:, None]],
            [white_response[None, :], np.zeros((1, 1))],
        ]
    )
    target = np.vstack([response.T @ desired, white_target])
    xyz_matrix = np.linalg.solve(system, target)[:3]
    matrix = xyz_matrix @ XYZ_TO_RGB.T
    return matrix, {
        "kind": "fixed_D65_white_constrained_spectral_QE_projection_v2",
        "test_chart_fit": False,
        "reference_luminance_cd_m2": DISPLAY_REFERENCE_LUMINANCE,
        "reference_exposure_ms": 4,
        "reference_lens_f_number": reference_f_number,
        "matrix_sensor_electrons_to_linear_srgb": matrix.tolist(),
        "white_constraint": "known_D65_white_electrons_to_neutral_linear_srgb",
        "predicted_reference_white_linear_rgb": (white_response @ matrix).tolist(),
        "chromatic_adaptation": spectral["chromatic_adaptation"],
        "spectral_band_nm": [450, 650],
        "physical_color_calibration": False,
    }


def _common(array, geometry=None):
    array = np.asarray(array, float)
    target_rows, target_cols = array.shape[:2]
    if geometry is not None and geometry.get("optical_model") != "wide_parametric":
        target_cols = (
            2
            * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
            * geometry["focal_length_mm"]
            * 1000
            / geometry["pixel_pitch_um"]
        )
        target_rows = target_cols * 9 / 16
    if array.shape[:2] == COMMON and np.allclose(
        (target_rows, target_cols), COMMON, rtol=0, atol=1e-8
    ):
        return array.copy()
    sigma = (max(0, target_rows / COMMON[0] / 2), max(0, target_cols / COMMON[1] / 2))
    filtered = gaussian_filter(array, sigma if array.ndim == 2 else (*sigma, 0), mode="reflect")
    rr, cc = np.meshgrid(
        (np.arange(COMMON[0]) + 0.5 - COMMON[0] / 2) * target_rows / COMMON[0]
        + (array.shape[0] - 1) / 2,
        (np.arange(COMMON[1]) + 0.5 - COMMON[1] / 2) * target_cols / COMMON[1]
        + (array.shape[1] - 1) / 2,
        indexing="ij",
    )
    if array.ndim == 2:
        return map_coordinates(filtered, [rr, cc], order=1, mode="nearest")
    return np.stack(
        [map_coordinates(filtered[..., b], [rr, cc], order=1, mode="nearest") for b in range(3)], -1
    )


def _process(raw, readout, calibration, tuning):
    from pyisetcam.ip import _ie_bilinear

    # Same signed black-subtract and native RGGB backend as camera_platform.isp.
    # User BLC offset and explicit manual shading map precede demosaicing.
    denominator = (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"]) * readout["gain"]
    mosaic = (
        (raw.astype(float) - readout["black_level_dn"] - float(tuning.get("black_offset_dn", 0)))
        * readout["full_well_e"]
        / denominator
    )
    strength = float(tuning.get("lsc_strength", 0))
    if strength:
        yy, xx = np.meshgrid(
            np.linspace(-1, 1, raw.shape[0]), np.linspace(-1, 1, raw.shape[1]), indexing="ij"
        )
        mosaic *= 1 + strength * (xx * xx + yy * yy) / 2
    pattern = np.array([[1, 2], [2, 3]])
    mask = np.tile(pattern, (raw.shape[0] // 2, raw.shape[1] // 2))
    planes = np.zeros((*raw.shape, 3), float)
    for channel in range(3):
        selected = mask == channel + 1
        planes[..., channel][selected] = mosaic[selected]
    electrons = _ie_bilinear(planes, pattern)
    linear = (electrons @ calibration) * np.asarray(tuning["wb"])
    linear = linear @ np.asarray(tuning["ccm"], float).T
    denoise, sharp = float(tuning["denoise"]), float(tuning["sharpen"])
    processed = linear.copy()
    if denoise > 0:
        smoothed = gaussian_filter(processed, (0.45 + denoise, 0.45 + denoise, 0), mode="reflect")
        mix = min(1.0, denoise)
        processed = (1 - mix) * processed + mix * smoothed
    if sharp > 0:
        processed += sharp * (processed - gaussian_filter(processed, (1, 1, 0), mode="reflect"))
    # Tone and display range are explicit, fixed; never per-frame normalization.
    display = np.clip(processed, 0, 1) ** (1 / float(tuning["tone_gamma"]))
    return linear, processed, display


def _png(path, values, *, gamma=False):
    image = np.asarray(values, float)
    if gamma:
        image = np.clip(image, 0, 1) ** (1 / 2.2)
    Image.fromarray(np.round(np.clip(image, 0, 1) * 255).astype(np.uint8)).save(path)


def _artifact(root, path):
    return {
        "path": str(Path(path).relative_to(root)),
        "sha256": file_sha256(path),
        "bytes": Path(path).stat().st_size,
    }


def _patch_rois():
    return [
        [
            int((r + 0.25) * 360 / 4),
            int((r + 0.75) * 360 / 4),
            int((c + 0.25) * 640 / 6),
            int((c + 0.75) * 640 / 6),
        ]
        for r in range(4)
        for c in range(6)
    ]


def _notify(callback, stage, fraction):
    if callback:
        callback({"stage": stage, "progress": fraction})


def run(workspace, output_dir, specification, on_progress=None):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "capture.json").exists():
        raise ValueError("Virtual Lab capture output already exists; use RAW replay")
    start = time.monotonic()
    spec = copy.deepcopy(specification)
    lens, saved, optics, sensor, geometry = _load(workspace, spec)
    spectral = _spectra(workspace, spec["illuminant"], spec["luminance_cd_m2"])
    calibration, calibration_info = _calibration(workspace, sensor, optics)
    field_mode = spec.get("field_model", {"mode": "center_local"})["mode"]
    angular = spec.get("angular_response", {"mode": "off", "exponents_r_gr_gb_b": [2, 2, 2, 2]})
    field_context = {
        "angular_response": angular,
        "angular_table": None,
        "output_dir": str(root),
        "specification": spec,
    }
    if field_mode == "full_field" and angular["mode"] in {"physical_relative", "physical_pupil"}:
        from .virtual_lab_angular import build_response

        field_context["angular_table"] = build_response(workspace, spec["sensor_id"], WAVE)
        _json(root / "angular-response.json", field_context["angular_table"])
        if angular["mode"] == "physical_pupil" and spec["lens_id"] != "wide_parametric":
            from .virtual_lab_pupil import build_pupil_table

            field_context["pupil_table"] = build_pupil_table(
                optics, field_context["angular_table"], WAVE
            )
            path = root / "pupil-response.json"
            _json(path, field_context["pupil_table"])
            field_context["pupil_table_artifact"] = _artifact(root, path)
    requested = (
        ["resolution", "color", "noise", "tone"]
        if spec["test_suite"] == "all"
        else [spec["test_suite"]]
    )
    kinds = list(
        dict.fromkeys([spec["scene"]] + [("uniform" if k == "noise" else k) for k in requested])
    )
    if spec["lens_id"] == "wide_parametric":
        from .virtual_lab_wide import build_angular_table, preflight

        field_context["wide_preflight"] = preflight(
            root,
            geometry,
            spec["optics"],
            len(kinds),
            5 * int(spec["frames"]) if "uniform" in kinds else 0,
        )
        field_context["wide_angular_table"] = build_angular_table(
            geometry, spec["optics"], angular, field_context["angular_table"], WAVE
        )
        _json(root / "wide-angular-response.json", field_context["wide_angular_table"])
    (root / "raw").mkdir(exist_ok=True)
    (root / "arrays").mkdir(exist_ok=True)
    captures = {}
    readout = copy.deepcopy(sensor.fields["sensor_readout"])
    for index, kind in enumerate(kinds):
        _notify(on_progress, f"Optics → native RAW: {kind}", 0.05 + 0.6 * index / len(kinds))
        field_context["on_band"] = lambda band, total, index=index, kind=kind: _notify(
            on_progress,
            (
                f"Wide {kind}: native tile / sampling probe {band}/{total}"
                if spec["lens_id"] == "wide_parametric"
                else f"Full-field {kind}: spectral band {band}/{total}"
            ),
            0.05 + 0.6 * (index + band / total) / len(kinds),
        )
        captured, oi_preview, psf = _chart_capture(
            optics,
            sensor,
            spectral,
            kind,
            geometry,
            spec["optics"],
            spec["seed"] + index * 100,
            field_context=field_context,
        )
        rawpath = root / "raw" / f"{kind}.npy"
        np.save(rawpath, captured.data["raw_dn"].astype(np.uint16))
        meanpath = root / "arrays" / f"{kind}-mean-electrons.npy"
        np.save(meanpath, captured.data["mean_electrons"].astype(np.float32))
        np.save(root / "arrays" / f"{kind}-optical.npy", oi_preview.astype(np.float32))
        psfpath = root / "arrays" / f"{kind}-center-psf.npy"
        np.save(psfpath, psf)
        captures[kind] = {
            "raw": _artifact(root, rawpath),
            "mean": _artifact(root, meanpath),
            "seed": spec["seed"] + index * 100,
            "shape": list(captured.data["raw_dn"].shape),
            "psf": _artifact(root, psfpath),
        }
        if "virtual_lab_wide" in captured.metadata:
            captures[kind]["wide"] = captured.metadata.pop("virtual_lab_wide")
        if "virtual_lab_field" in captured.metadata:
            detail = captured.metadata.pop("virtual_lab_field")
            paths = {}
            for key in ("cra_deg", "gain_r_gr_gb_b", "mean_without_angular"):
                path = root / "arrays" / f"{kind}-field-{key}.npy"
                np.save(path, detail[key])
                paths[key] = _artifact(root, path)
            captures[kind]["field_response"] = {"summary": detail["summary"], "arrays": paths}
        if kind == "uniform":
            from pyisetcam.sensor_capture import resample_sensor_capture

            levels = []
            for level_index, multiplier in enumerate([0.0625, 0.25, 1.0, 4.0, 16.0]):
                repeats = []
                level_sensor = captured.clone()
                level_sensor.data["mean_electrons"] = captured.data["mean_electrons"] * multiplier
                for frame in range(int(spec["frames"])):
                    _notify(
                        on_progress,
                        f"Noise RAW: level {level_index + 1}/5, frame {frame + 1}/{spec['frames']}",
                        0.05
                        + 0.6
                        * (index + (level_index + (frame + 1) / spec["frames"]) / 5)
                        / len(kinds),
                    )
                    capture_seed = spec["seed"] + 5000 + level_index * 100 + frame
                    if spec["lens_id"] == "wide_parametric":
                        from .virtual_lab_wide import coordinate_readout

                        value = coordinate_readout(
                            level_sensor,
                            level_sensor.data["mean_electrons"],
                            root / "wide-checkpoints" / f"noise-level{level_index}-{frame:02d}",
                            capture_seed,
                            spec["wide_angle"],
                        )
                    else:
                        value = resample_sensor_capture(level_sensor, seed=capture_seed)
                    path = root / "raw" / f"noise-level{level_index}-{frame:02d}.npy"
                    np.save(path, value.data["raw_dn"].astype(np.uint16))
                    repeats.append(_artifact(root, path))
                levels.append(
                    {
                        "light_multiplier": multiplier,
                        "mean_electrons": float(
                            level_sensor.data["mean_electrons"][::2, 1::2].mean()
                        ),
                        "repeats": repeats,
                    }
                )
                if multiplier == 1:
                    captures[kind]["repeats"] = repeats
            captures[kind]["levels"] = levels
    # Five explicitly separate local field observations, never relocated real ROIs.
    fields = []
    if "resolution" in requested and field_mode != "full_field":
        pinned_lenses = [c["lens"]["model"] for c in _pinned(workspace)["candidates"]]
        common_field_tangent = 0.7 * min(
            m["field_support_mm"][-1] / m["effective_focal_length_mm"] for m in pinned_lenses
        )
        field_fraction = (
            common_field_tangent
            * geometry["focal_length_mm"]
            / lens["model"]["field_support_mm"][-1]
        )
        field_sensor = sensor.clone()
        field_sensor.fields["size"] = (96, 96)
        field_geo = {
            **geometry,
            "native_shape": [96, 96],
            "native_horizontal_fov_deg": math.degrees(
                2
                * math.atan(
                    96 * geometry["pixel_pitch_um"] * 0.001 / geometry["focal_length_mm"] / 2
                )
            ),
        }
        for name, fraction, angle in [
            ("center", 0, 0),
            ("top_left", field_fraction, 135),
            ("top_right", field_fraction, 45),
            ("bottom_left", field_fraction, 225),
            ("bottom_right", field_fraction, 315),
        ]:
            # A single central edge fills this metric window.
            captured, _, kernel = _chart_capture(
                optics,
                field_sensor,
                spectral,
                "field_edge",
                field_geo,
                spec["optics"],
                spec["seed"] + 900,
                fraction,
                angle,
            )
            path = root / "raw" / f"field-{name}.npy"
            np.save(path, captured.data["raw_dn"].astype(np.uint16))
            kernelpath = root / "arrays" / f"psf-{name}.npy"
            np.save(kernelpath, kernel.astype(np.float32))
            fields.append(
                {
                    "id": name,
                    "field_fraction": fraction,
                    "object_angle_radius_deg": (
                        math.degrees(math.atan(common_field_tangent)) if fraction else 0
                    ),
                    "azimuth_deg": angle,
                    "raw": _artifact(root, path),
                    "psf": _artifact(root, kernelpath),
                    "interpretation": "local shift-invariant field chart; no full-field image crop",
                }
            )
    reference_spectrum = _spectra(workspace, "D65", DISPLAY_REFERENCE_LUMINANCE)
    reference_fnumber = calibration_info["reference_lens_f_number"]
    optical_reference = (
        float(np.mean(reference_spectrum["white"])) * math.pi / (1 + 4 * reference_fnumber**2)
    )
    capture = {
        "schema": "camera_virtual_lab_capture_v1",
        "specification": spec,
        "geometry": geometry,
        "lens": lens,
        "sensor": saved,
        "readout": readout,
        "calibration": calibration_info,
        "captures": captures,
        "field_windows": fields,
        "color_reference_xyz": spectral["reference_xyz"].tolist(),
        "white_reference_xyz": spectral["white_xyz"].tolist(),
        "elapsed_s": time.monotonic() - start,
        "fixed_pattern_seed": 1701,
        "requested_tests": requested,
        "optical_preview_reference_photons_mean_band": optical_reference,
    }
    _json(root / "capture.json", capture)
    _notify(on_progress, "Common ISP and Virtual Lab metrics", 0.75)
    result = _render(root, root, capture, spec)
    result["timings"]["total_elapsed_s"] = time.monotonic() - start
    _json(root / "result.json", result)
    _notify(on_progress, "Complete", 1)
    return result


def _render(source, root, capture, spec):
    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    readout = capture["readout"]
    calibration = np.asarray(capture["calibration"]["matrix_sensor_electrons_to_linear_srgb"])
    geometry = capture["geometry"]
    wide = geometry.get("optical_model") == "wide_parametric"
    tests, images, arrays, image_sets = {}, {}, {}, {}
    raw_artifacts = []
    native_arrays, native_artifacts = {}, {}
    if wide:
        (root / "isp").mkdir(exist_ok=True)
    for kind, item in capture["captures"].items():
        rawpath = source / item["raw"]["path"]
        if file_sha256(rawpath) != item["raw"]["sha256"]:
            raise ValueError("Original RAW hash changed; replay refused")
        raw = np.load(rawpath, allow_pickle=False)
        linear, processed, display = _process(raw, readout, calibration, spec["isp"])
        lin, proc, disp = (
            _common(linear, geometry),
            _common(processed, geometry),
            _common(display, geometry),
        )
        arrays[kind] = (lin, proc, disp)
        if wide:
            # Derived outputs must never share the hardlinks used for immutable RAW/arrays.
            paths = {}
            for label, value in (("linear", linear), ("processed", processed)):
                relative = f"isp/{kind}-{label}.npy"
                np.save(root / relative, value.astype(np.float32))
                paths[label] = _artifact(root, root / relative)
            native_path = f"isp/{kind}-native.png"
            _png(root / native_path, display)
            paths["display"] = _artifact(root, root / native_path)
            paths["native_shape"] = list(raw.shape)
            native_artifacts[kind] = paths
            if kind in {"resolution", "color"}:
                native_arrays[kind] = tuple(
                    np.load(root / paths[key]["path"], mmap_mode="r", allow_pickle=False)
                    for key in ("linear", "processed")
                )
        _png(root / "images" / f"{kind}-isp.png", disp)
        raw_norm = (raw.astype(float) - readout["black_level_dn"]) / (
            2 ** readout["bit_depth"] - 1 - readout["black_level_dn"]
        )
        _png(root / "images" / f"{kind}-raw.png", _common(raw_norm, geometry), gamma=True)
        images[kind] = f"images/{kind}-isp.png"
        image_sets[kind] = {"isp": f"images/{kind}-isp.png", "raw": f"images/{kind}-raw.png"}
        if wide:
            image_sets[kind]["native_isp"] = f"isp/{kind}-native.png"
        raw_artifacts.append({**item["raw"], "source": str(source), "original_preserved": True})
        for level in item.get("levels", []):
            raw_artifacts.extend(
                {**frame, "source": str(source), "original_preserved": True}
                for frame in level["repeats"]
            )
        if kind == spec["scene"]:
            images.update({"isp": f"images/{kind}-isp.png", "raw": f"images/{kind}-raw.png"})
            if wide:
                images["native_isp"] = f"isp/{kind}-native.png"
            _png(root / "images" / "linear.png", np.clip(lin, 0, 1), gamma=True)
            images["linear"] = "images/linear.png"
            if wide:
                from .virtual_lab_wide_geometry import chart_pattern

                rr, cc = np.meshgrid(
                    (np.arange(COMMON[0]) + 0.5) * geometry["native_shape"][0] / COMMON[0] - 0.5,
                    (np.arange(COMMON[1]) + 0.5) * geometry["native_shape"][1] / COMMON[1] - 0.5,
                    indexing="ij",
                )
                ideal = chart_pattern(
                    kind, rr, cc, geometry, item.get("wide", {}).get("resolution_sites")
                )
            else:
                ideal = _pattern(kind, *COMMON)
            if kind == "color":
                palette = np.asarray(capture["color_reference_xyz"]) @ XYZ_TO_RGB.T
                ideal = palette[np.asarray(ideal, int)]
            _png(root / "images" / "scene.png", ideal, gamma=True)
            images["scene"] = "images/scene.png"
            optical = np.load(source / "arrays" / f"{kind}-optical.npy", allow_pickle=False)
            # Fixed1000cd/m2D65/L1 radiometric reference, independent of frame maxima.
            optical_reference = capture["optical_preview_reference_photons_mean_band"]
            _png(
                root / "images" / "optical.png",
                _common(optical, geometry) / max(optical_reference, 1e-30),
                gamma=True,
            )
            images["optical"] = "images/optical.png"
        if "field_response" in item and kind == spec["scene"]:
            field = copy.deepcopy(item["field_response"]["summary"])
            entries = item["field_response"]["arrays"]
            for entry in entries.values():
                if file_sha256(source / entry["path"]) != entry["sha256"]:
                    raise ValueError("Full-field response array hash changed")
            theta = np.load(source / entries["cra_deg"]["path"])
            gain = np.load(source / entries["gain_r_gr_gb_b"]["path"])

            def heatmap(value, maximum):
                v = np.clip(_common(value, geometry) / maximum, 0, 1)
                return np.stack(
                    [
                        np.clip(1.5 - abs(4 * v - 3), 0, 1),
                        np.clip(1.5 - abs(4 * v - 2), 0, 1),
                        np.clip(1.5 - abs(4 * v - 1), 0, 1),
                    ],
                    axis=-1,
                )

            _png(root / "images/field-cra.png", heatmap(theta, 60))
            _png(root / "images/field-angular-gain.png", heatmap(gain.mean(axis=-1), 1.5))
            field["maps"] = {
                "cra": "images/field-cra.png",
                "angular_gain": "images/field-angular-gain.png",
            }
            field["array_artifacts"] = entries
            pupil_artifact = field.get("pupil_table_artifact")
            if (
                pupil_artifact
                and file_sha256(source / pupil_artifact["path"]) != pupil_artifact["sha256"]
            ):
                raise ValueError("Preserved pupil response table hash changed")
            field["map_gain_aggregation"] = (
                "550nm mean R/Gr/Gb/B of conditional pupil-averaged relative response"
                if field.get("pupil_averaged")
                else "550nm mean R/Gr/Gb/B relative chief response"
            )
            tests["field_response"] = field
            images.update(
                field_cra="images/field-cra.png",
                field_angular_gain="images/field-angular-gain.png",
                cra="images/field-cra.png",
                angular_gain="images/field-angular-gain.png",
            )
            image_sets[kind].update(
                cra="images/field-cra.png", angular_gain="images/field-angular-gain.png"
            )
    degrees = geometry["common_horizontal_fov_deg"] / 640
    if "resolution" in capture["requested_tests"]:
        lin, proc, _ = arrays["resolution"]
        field_summary = (
            capture["captures"]["resolution"].get("field_response", {}).get("summary", {})
        )
        if wide:
            from .virtual_lab_wide_metrics import resolution_metrics

            resolution = resolution_metrics(
                *native_arrays["resolution"],
                sites=capture["captures"]["resolution"]["wide"]["resolution_sites"],
                geometry=geometry,
            )
        else:
            resolution = metrics.resolution_metrics(
                lin,
                proc,
                rois=field_summary.get("evaluation_rois", EDGE_ROIS),
                degrees_per_pixel=degrees,
            )
        if field_summary and not wide:
            resolution["angular_frequency_scope"] = (
                "Paraxial FOV/640 scale only; full-field distortion means this is "
                "not a local object-angle MTF calibration"
            )
            resolution["source_rois"] = EDGE_ROIS
            resolution["evaluation_rois"] = field_summary["evaluation_rois"]
            psfentry = capture["captures"]["resolution"]["psf"]
            if file_sha256(source / psfentry["path"]) != psfentry["sha256"]:
                raise ValueError("Optical PSF artifact changed")
            resolution["optical"] = metrics.optical_psf_metrics(
                np.load(source / psfentry["path"]),
                sample_pitch_um=geometry["pixel_pitch_um"] / 2,
                sensor_pitch_um=geometry["pixel_pitch_um"],
                focal_length_mm=geometry["focal_length_mm"],
            )
            resolution["optical"]["scope"] = (
                "550nm on-axis PSF; full-field response is in five image SFR sites"
            )
        if wide:
            entry = capture["captures"]["resolution"]["psf"]
            if file_sha256(source / entry["path"]) != entry["sha256"]:
                raise ValueError("Wide optical PSF artifact changed")
            info = capture["captures"]["resolution"]["wide"]
            factor = info["sampling"].get("factor", info["sampling"].get("selected_factor", 2))
            resolution["optical"] = metrics.optical_psf_metrics(
                np.load(source / entry["path"]),
                sample_pitch_um=geometry["pixel_pitch_um"] / factor,
                sensor_pitch_um=geometry["pixel_pitch_um"],
                focal_length_mm=geometry["focal_length_mm"],
            )
            resolution["optical"]["scope"] = (
                "Declared on-axis 550nm diffraction/defocus PSF; angular axis on-axis only. "
                "Nine native image sites carry the field-dependent system response."
            )
        fields = []
        for item in capture["field_windows"]:
            raw = np.load(source / item["raw"]["path"], allow_pickle=False)
            if file_sha256(source / item["raw"]["path"]) != item["raw"]["sha256"]:
                raise ValueError("Field RAW hash changed")
            linear, processed, display = _process(raw, readout, calibration, spec["isp"])
            bounds = [16, 80, 16, 80]
            native_deg = math.degrees(
                geometry["pixel_pitch_um"] * 0.001 / geometry["focal_length_mm"]
            )
            measured = metrics.resolution_metrics(
                linear,
                processed,
                rois={"center": bounds},
                pixel_pitch_um=geometry["pixel_pitch_um"],
                degrees_per_pixel=native_deg,
            )
            psf = np.load(source / item["psf"]["path"], allow_pickle=False)
            measured["optical"] = metrics.optical_psf_metrics(
                psf,
                sample_pitch_um=geometry["pixel_pitch_um"] / 2,
                sensor_pitch_um=geometry["pixel_pitch_um"],
                focal_length_mm=geometry["focal_length_mm"],
            )
            path = f"images/field-{item['id']}.png"
            _png(root / path, display)
            fields.append({**item, "metrics": measured, "image": path})
            raw_artifacts.append({**item["raw"], "source": str(source), "original_preserved": True})
        resolution["field_windows"] = fields
        if not wide:
            resolution["main_chart_scope"] = (
                "Full-frame geometry, field-dependent PSF and declared angular pixel response"
                if "field_response" in capture["captures"]["resolution"]
                else "Center-local PSF chart; five independent off-axis windows below"
            )
        tests["resolution"] = resolution
    if "color" in capture["requested_tests"]:
        field_summary = capture["captures"]["color"].get("field_response", {}).get("summary", {})
        color_rois = field_summary.get("evaluation_rois")
        if wide:
            color_rois = capture["captures"]["color"]["wide"]["color_rois_native"]
            color_image = native_arrays["color"][1]
        else:
            color_image = arrays["color"][1]
            color_rois = [color_rois[str(i)] for i in range(24)] if color_rois else _patch_rois()
        tests["color"] = metrics.color_metrics(
            color_image,
            np.asarray(capture["color_reference_xyz"]),
            patch_rois=color_rois,
            white_xyz=[0.95047, 1, 1.08883],
            reference_provenance={
                "kind": "known_reflectance_and_illuminant",
                "band_limit_nm": [450, 650],
                "calibration": capture["calibration"],
                "evaluation_before_tone": True,
            },
        )
    if "noise" in capture["requested_tests"]:
        item = capture["captures"]["uniform"]
        raws = []
        processed = []
        for frame in item["repeats"]:
            path = source / frame["path"]
            if file_sha256(path) != frame["sha256"]:
                raise ValueError("Noise RAW hash changed")
            raw = np.load(path, allow_pickle=False)
            raws.append(raw)
            processed.append(_common(_process(raw, readout, calibration, spec["isp"])[2], geometry))
        stack = np.stack(raws)
        native = metrics.temporal_noise_metrics(
            stack[:, ::2, 1::2], unit="DN", black_level=readout["black_level_dn"]
        )
        native["channel"] = "Gr"
        native["channels"] = {
            name: metrics.temporal_noise_metrics(
                stack[:, r::2, c::2], unit="DN", black_level=readout["black_level_dn"]
            )
            for name, r, c in [("R", 0, 0), ("Gr", 0, 1), ("Gb", 1, 0), ("B", 1, 1)]
        }
        native["after_isp"] = metrics.temporal_noise_metrics(
            np.stack(processed), unit="display_0_1"
        )
        native["fixed_pattern_seed"] = capture["fixed_pattern_seed"]
        native["mean_electrons"] = float(np.load(source / item["mean"]["path"]).mean())
        measured = []
        for level in item.get("levels", []):
            codes = []
            for frame in level["repeats"]:
                path = source / frame["path"]
                if file_sha256(path) != frame["sha256"]:
                    raise ValueError("Photon transfer RAW hash changed")
                codes.append(np.load(path, allow_pickle=False)[::2, 1::2])
            point = metrics.temporal_noise_metrics(
                np.stack(codes), unit="DN", black_level=readout["black_level_dn"]
            )
            point.update(
                light_multiplier=level["light_multiplier"],
                mean_electrons=level["mean_electrons"],
                adc_saturation_fraction=float(
                    np.mean(np.stack(codes) >= 2 ** readout["bit_depth"] - 1)
                ),
            )
            measured.append(point)
        native["photon_transfer_points"] = measured
        native["photon_transfer_kind"] = (
            "5 light levels × repeated readout; mean scaled before shot/read noise and ADC"
        )
        # Analytic expectation curve is identified separately from the repeated acquisition point.
        charges = np.geomspace(0.1, readout["full_well_e"], 40)
        native["model_snr_curve"] = {
            "mean_electrons": charges.tolist(),
            "snr_db": (
                20
                * np.log10(
                    charges
                    / np.sqrt(
                        charges
                        + readout["read_noise_e"] ** 2
                        + readout["dark_current_e_per_s"] * spec["exposure_ms"] / 1000
                    )
                )
            ).tolist(),
            "kind": "readout_model_prediction_not_40_acquisitions",
        }
        tests["noise"] = native
    if "tone" in capture["requested_tests"]:
        item = capture["captures"]["tone"]
        mean = np.load(source / item["mean"]["path"])
        raw = np.load(source / item["raw"]["path"])
        halfwidth = max(2, min(8, raw.shape[1] // 64))
        rows = slice(raw.shape[0] // 2 - 24, raw.shape[0] // 2 + 24)
        cols = [int((j + 0.5) * raw.shape[1] / 16) // 2 * 2 for j in range(16)]
        tone_rois = item.get("field_response", {}).get("summary", {}).get("evaluation_rois")
        if wide:
            native_bounds = item["wide"]["tone_rois_native"]
            response = []
            for level, (r0, r1, c0, c1) in zip(
                np.geomspace(0.002, 2, 16), native_bounds, strict=True
            ):
                stripe = metrics.tone_metrics(
                    [level],
                    mean[None, r0:r1, c0:c1],
                    raw[None, r0:r1, c0:c1],
                    full_well_e=readout["full_well_e"],
                    adc_white=2 ** readout["bit_depth"] - 1,
                )
                response.extend(stripe["response"])
            tests["tone"] = {
                **stripe,
                "response": response,
                "evaluation_rois_native": native_bounds,
            }
        elif tone_rois:
            # Keep individual pixel samples for saturation fractions. Bounds use
            # the same centered physical crop as _common, with even CFA bounds.
            native_bounds = []
            target_cols = (
                2
                * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
                * geometry["focal_length_mm"]
                * 1000
                / geometry["pixel_pitch_um"]
            )
            target_rows = target_cols * 9 / 16
            for i in range(16):
                r0, r1, c0, c1 = tone_rois[str(i)]
                native_bounds.append(
                    [
                        math.ceil(((r0 - 180) * target_rows / 360 + raw.shape[0] / 2) / 2) * 2,
                        math.floor(((r1 - 180) * target_rows / 360 + raw.shape[0] / 2) / 2) * 2,
                        math.ceil(((c0 - 320) * target_cols / 640 + raw.shape[1] / 2) / 2) * 2,
                        math.floor(((c1 - 320) * target_cols / 640 + raw.shape[1] / 2) / 2) * 2,
                    ]
                )
            response = []
            for level, (r0, r1, c0, c1) in zip(
                np.geomspace(0.002, 2, 16), native_bounds, strict=True
            ):
                stripe = metrics.tone_metrics(
                    [level],
                    mean[None, r0:r1, c0:c1],
                    raw[None, r0:r1, c0:c1],
                    full_well_e=readout["full_well_e"],
                    adc_white=2 ** readout["bit_depth"] - 1,
                )
                response.extend(stripe["response"])
            tests["tone"] = {
                **stripe,
                "response": response,
                "evaluation_rois_native": native_bounds,
            }
        else:
            means = np.stack([mean[rows, c - halfwidth : c + halfwidth] for c in cols])
            codes = np.stack([raw[rows, c - halfwidth : c + halfwidth] for c in cols])
            tests["tone"] = metrics.tone_metrics(
                np.geomspace(0.002, 2, 16),
                means,
                codes,
                full_well_e=readout["full_well_e"],
                adc_white=2 ** readout["bit_depth"] - 1,
            )
    result = {
        "schema": SCHEMA,
        "configuration": spec,
        "provenance": {
            "lens_revision": capture["lens"]["identity"],
            "sensor_revision": capture["sensor"]["revision"],
            "geometry": geometry,
            "readout": readout,
            "calibration": capture["calibration"],
            "capture_sha256": file_sha256(source / "capture.json"),
            "capture_source": str(source),
            "sensor_mean_engine": "pyisetcam.sensor_compute / native spatial integration",
            "optical_engine": "CameraE2E spectral PSF interpolation / scipy linear convolution",
            "acquisition": "independent shot/read draws; persistent FPN",
        },
        "tests": tests,
        "metrics": tests,
        "images": images,
        "image_sets": image_sets,
        "raw_artifacts": raw_artifacts,
        "timings": {"capture_elapsed_s": capture["elapsed_s"]},
        "limitations": [
            "Model-based virtual Lab; not a measured camera or product mode.",
            "640×360 is a common angular observation; native sampling and FOV are recorded.",
            (
                "Full-field stored geometry/PSF with declared receiver-side relative angular "
                "response; field-separable, not joint PSF landing-position/angle integration."
                if any("field_response" in item for item in capture["captures"].values())
                else "Local PSFs omit full-field geometry, transmission and angular pixel response."
            ),
            "Stored lens transmission and relative illumination remain declared unity assumptions.",
            "DB ray PSFs exclude diffraction; analytic optics models diffraction + defocus.",
            "21 samples across 450–650 nm only; color values are band-limited model comparisons.",
            "Fixed D65 spectral calibration does not fit the evaluated chart.",
            "Fixed D65 adaptation preserves neutral white; missing spectral bands stay absent.",
            "Temporal repeats separate temporal noise from fixed pattern and scene variation.",
            "Optical preview uses fixed D65/1000cd/m²/L1 reference; metrics use original arrays.",
            "Manual LSC is a declared radial gain map, not measured lens shading compensation.",
        ],
    }
    if wide:
        import resource
        import sys

        result["native_isp_artifacts"] = native_artifacts
        result["tests"]["sampling"] = {
            kind: item["wide"]["sampling"] for kind, item in capture["captures"].items()
        }
        result["provenance"]["sensor_mean_engine"] = (
            "Tiled spectral optical PSF and declared pixel aperture integration"
        )
        result["provenance"]["optical_engine"] = (
            "Declared wide-angle projection, diffraction/defocus and field blur approximation"
        )
        result["resources"] = {
            "capture_policies": {
                kind: item["wide"]["resource"] for kind, item in capture["captures"].items()
            },
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "artifact_bytes_before_report": sum(
                p.stat().st_size for p in root.rglob("*") if p.is_file()
            ),
            "memory_budget_kind": "conservative admission estimate, not OS-enforced RSS limit",
        }
        result["limitations"] = [
            "Parametric wide-angle sensitivity model; not a measured lens or product sensor mode.",
            "1920×1080 native RAW and ISP are preserved; 640×360 is only a preview.",
            "Directional procedural scenes; no missing road-photo coverage is invented.",
            "Declared diffraction, defocus, field blur, transmission and pupil approximations.",
            "Receiver-side angular QE averaging is field-separable, not a joint ray-hit/angle PSF.",
            "Sampling qualification is limited to the saved representative refinement probes.",
            "Native SFR and angular diagnostics are distinct; resampling adds interpolation.",
            "21 wavelengths across450–650nm; fixed D65 calibration is not fitted to the chart.",
            "Temporal repeats separate noise from chart texture; no product ranking claim.",
        ]
    _json(root / "result.json", result)
    return result


def replay(workspace, source_dir, output_dir, specification, on_progress=None):
    del workspace
    start = time.monotonic()
    source, root = Path(source_dir), Path(output_dir)
    if source.resolve() == root.resolve():
        raise ValueError("RAW replay needs a new result directory")
    capture = json.loads((source / "capture.json").read_text())
    original = capture["specification"]
    from .virtual_lab_job import wide_angle_defaults

    legacy_defaults = {
        "field_model": {"mode": "center_local", "horizontal_fov_deg": None},
        "angular_response": {"mode": "off", "exponents_r_gr_gb_b": [2, 2, 2, 2]},
        "wide_angle": wide_angle_defaults(),
    }
    for key in (
        "lens_id",
        "sensor_id",
        "scene",
        "test_suite",
        "illuminant",
        "luminance_cd_m2",
        "exposure_ms",
        "gain",
        "frames",
        "optics",
        "field_model",
        "angular_response",
        "wide_angle",
        "seed",
    ):
        if specification.get(key, legacy_defaults.get(key)) != original.get(
            key, legacy_defaults.get(key)
        ):
            raise ValueError(f"RAW replay cannot change acquisition field {key}")
    # Self-contained replay/export. Hardlink immutable captures when possible;
    # never rewrite or resample the original RAW, including replay of a replay.
    root.mkdir(parents=True, exist_ok=True)
    if (root / "capture.json").exists():
        raise ValueError("Replay output capture already exists")
    for folder in ("raw", "arrays"):
        (root / folder).mkdir(exist_ok=True)
        for path in (source / folder).iterdir():
            target = root / folder / path.name
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
    shutil.copy2(source / "capture.json", root / "capture.json")
    for table_file in (
        "angular-response.json",
        "pupil-response.json",
        "wide-angular-response.json",
    ):
        if (source / table_file).exists():
            shutil.copy2(source / table_file, root / table_file)
    _notify(on_progress, "Replaying preserved RAW", 0.1)
    result = _render(root, root, capture, specification)
    result["provenance"]["original_capture_source"] = str(source)
    result["provenance"]["raw_replay"] = True
    result["timings"]["replay_elapsed_s"] = time.monotonic() - start
    _json(root / "result.json", result)
    _notify(on_progress, "Complete", 1)
    return result

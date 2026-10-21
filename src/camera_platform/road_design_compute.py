"""Pinned DB optics/sensor -> native ROI mean, independent RAW and fixed ISP."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, map_coordinates

from . import isp
from .road_design_inputs import write_json
from .road_rgb_source import (
    display_proxy_basis,
    file_sha256,
    json_sha256,
    make_rgb_strict_geometry_provider,
)


def common_grid(roi):
    x0, y0, x1, y1 = roi["tangent_bounds_xyxy"]
    rows, cols = roi["common_shape_row_col"]
    return np.meshgrid(np.linspace(y0, y1, rows), np.linspace(x0, x1, cols), indexing="ij")


def _even_ceil(value):
    return 2 * math.ceil(value / 2)


def native_coordinates(optics, sensor, roi):
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    yy, xx = common_grid(roi)
    focal_mm = optics["raytrace"]["effective_focal_length_m"] * 1000
    radial_ideal = np.hypot(xx, yy) * focal_mm
    ideal, distorted = _strict_raytrace_geometry_axes(optics["raytrace"], 550.0)
    if radial_ideal.max() > ideal[-1]:
        raise ValueError("ROI exceeds the pinned lens geometry field.")
    factor = np.divide(
        np.interp(radial_ideal, ideal, distorted),
        radial_ideal,
        out=np.ones_like(radial_ideal),
        where=radial_ideal > 0,
    )
    pitch_mm = float(sensor.fields["pixel"]["size_m"][0]) * 1000
    rows, cols = sensor.fields["size"]
    # Same native pixel-center convention as _global_target_axis(samples=1).
    return (
        yy * focal_mm * factor / pitch_mm + rows / 2 - 0.5,
        xx * focal_mm * factor / pitch_mm + cols / 2 - 0.5,
    )


def _antialias_sigma(coordinates):
    r, c = coordinates
    # Bound each native axis by the common-grid footprint, including shear.
    row_step = max(float(np.max(abs(np.diff(r, axis=0)))), float(np.max(abs(np.diff(r, axis=1)))))
    col_step = max(float(np.max(abs(np.diff(c, axis=0)))), float(np.max(abs(np.diff(c, axis=1)))))
    return max(0.0, row_step / 2), max(0.0, col_step / 2)


def _roi_bounds(coordinates, sensor_shape):
    sigma = _antialias_sigma(coordinates)
    halo = [math.ceil(4 * s) + 4 for s in sigma]
    r, c = coordinates
    bounds = (
        2 * math.floor((r.min() - halo[0]) / 2),
        _even_ceil(r.max() + halo[0] + 1),
        2 * math.floor((c.min() - halo[1]) / 2),
        _even_ceil(c.max() + halo[1] + 1),
    )
    if bounds[0] < 0 or bounds[2] < 0 or bounds[1] > sensor_shape[0] or bounds[3] > sensor_shape[1]:
        raise ValueError("ROI and ISP/anti-alias halo exceed the declared sensor extent.")
    return bounds, sigma


def load_candidate(workspace: Path, spec, candidate):
    from .lens_sensor_streamed_raw_job import _models

    lens = candidate["lens"]
    pitch = candidate["sensor"]["native_response"]["pixel_pitch_um"]
    focal_mm = lens["model"]["effective_focal_length_mm"]
    width_mm = 2 * focal_mm * math.tan(math.radians(spec["geometry"]["horizontal_fov_deg"]) / 2)
    cols = _even_ceil(width_mm * 1000 / pitch)
    rows = _even_ceil(cols * spec["geometry"]["aspect_row_col"])
    sensor_revision = candidate["sensor"]["revision"]
    request = {
        "lens_revision_id": lens["identity"]["revision_id"],
        "sensor_revision": {
            "identifier": sensor_revision["report_code"],
            "run_id": sensor_revision["run_id"],
        },
        "virtual_sensor_geometry": {"rows": rows, "cols": cols, "pixel_pitch_um": pitch},
        "analytic_scene": {"object_distance_m": "infinity"},
        "capture": {"exposure_s": spec["capture"]["exposure_s"], "noise_flag": 2},
    }
    actual_lens, actual_sensor, optics, sensor = _models(
        SimpleNamespace(workspace=workspace), request
    )
    if actual_lens["identity"] != lens["identity"] or actual_sensor["revision"] != sensor_revision:
        raise ValueError("Pinned DB revision changed since specification freeze.")
    if float(sensor.fields["sensor_readout"]["gain"]) != spec["capture"]["gain"]:
        raise ValueError("Saved readout gain differs from the frozen common gain.")
    sensor.fields["fixed_pattern_seed"] = spec["capture"]["fixed_pattern_seed"]
    return optics, sensor


def required_native_roi_cells(workspace, spec):
    """Geometry-only resource sizing, before optical/radiometric execution."""
    sizes = []
    for candidate in spec["candidates"]:
        optics, sensor = load_candidate(workspace, spec, candidate)
        for image in spec["images"]:
            for roi in image["rois"]:
                bounds, _ = _roi_bounds(
                    native_coordinates(optics, sensor, roi), sensor.fields["size"]
                )
                sizes.append((bounds[1] - bounds[0]) * (bounds[3] - bounds[2]))
    return max(sizes)


def scene_shape(spec, refinement=1):
    # A shared angular Scene grid derived from the finest native candidate.
    native_step = min(
        item["sensor"]["native_response"]["pixel_pitch_um"]
        / (1000 * item["lens"]["model"]["effective_focal_length_mm"])
        for item in spec["candidates"]
    )
    step = native_step / spec["sampling"]["sensor_pixel_oversampling"] / refinement
    cols = _even_ceil(2 * math.tan(math.radians(spec["geometry"]["horizontal_fov_deg"]) / 2) / step)
    rows = _even_ceil(cols * spec["geometry"]["aspect_row_col"])
    return rows, cols


def make_provider(workspace, spec, image, optics, refinement=1):
    proxy = spec["spectral_proxy"]
    primary, gamma, provenance = display_proxy_basis(
        workspace,
        proxy["wavelength_nm"],
        proxy["linear_grey_luminance_cd_m2"],
    )
    provider = make_rgb_strict_geometry_provider(
        optics,
        image_path=image["source_path"],
        expected_sha256=image["source_sha256"],
        intrinsic=image["camera"]["camera_intrinsic"],
        distortion=image["camera"]["camera_distortion"],
        primary_photons=primary,
        gamma_table=gamma,
        scene_shape_row_col=scene_shape(spec, refinement),
        scene_horizontal_fov_deg=spec["geometry"]["horizontal_fov_deg"],
        wavelength_nm=proxy["wavelength_nm"],
        source_description={"image_id": image["id"], "spectral_proxy": provenance},
    )
    illumination = spec.get("_road_hdr_illumination")
    if illumination is not None:
        linear = provider.linear_rgb.copy()
        mask = (linear @ np.asarray(spec["bright_mask"]["weights"])) > spec["bright_mask"]["threshold"]
        linear *= illumination["multiplier"]
        linear[mask] *= illumination["bright_multiplier"]
        linear.setflags(write=False)
        provider = replace(provider, linear_rgb=linear,
            source_description={**provider.source_description, "illumination": illumination,
                                "bright_mask_rule": spec["bright_mask"],
                                "bright_mask_sha256": __import__("hashlib").sha256(mask.tobytes()).hexdigest()})
    return provider, provenance


def compute_mean(workspace, spec, image, roi, candidate, output, *, refinement=1, brightness=1.0):
    from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
    from .lens_sensor_streamed_offaxis_mean import (
        _compute_preflighted_streamed_analytic_offaxis_mean,
        preflight_streamed_analytic_offaxis_mean,
    )

    optics, sensor = load_candidate(workspace, spec, candidate)
    provider, proxy = make_provider(workspace, spec, image, optics, refinement)
    if brightness != 1.0:
        photons = provider.primary_photons * brightness
        photons.setflags(write=False)
        provider = replace(provider, primary_photons=photons)
    coordinates = native_coordinates(optics, sensor, roi)
    bounds, sigma = _roi_bounds(coordinates, sensor.fields["size"])
    cells = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
    if cells > spec["resources"]["maximum_native_roi_cells"]:
        raise ValueError(
            f"Native ROI needs {cells} cells; increase the explicit ROI policy or use tiled ISP. "
            "No pixel pitch was changed."
        )
    source_side = spec["resources"]["source_tile_side"]
    policy = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=spec["resources"]["estimated_optical_working_bytes"],
        max_geometry_query_cells=500_000_000,
        max_source_tiles=1024,
        max_output_roi_cells=1_048_576,
        max_source_tile_cells=provider.max_tile_cells,
    )
    start = time.monotonic()
    # The established optical ROI path has a one-million-cell admission cap.
    # Split native output in global coordinates, preserving optical/sensor
    # halos on every tile. This is a work policy, never a physical RAM limit.
    ratio = float(sensor.fields["pixel"]["size_m"][0]) * 1e6 / provider.oi_pitch_um
    side = max(8, min(128, 2 * math.floor(800 / max(1.0, ratio) / 2)))
    mean = np.empty((bounds[1] - bounds[0], bounds[3] - bounds[2]), float)
    tiles = []
    for r0 in range(bounds[0], bounds[1], side):
        for c0 in range(bounds[2], bounds[3], side):
            tile = (r0, min(bounds[1], r0 + side), c0, min(bounds[3], c0 + side))
            admitted = None
            actual_source_side = source_side
            while actual_source_side >= 32:
                arguments = {
                    "sensor_tile_bounds_row_col_exclusive": tile,
                    "source_tile_shape_row_col": (actual_source_side, actual_source_side),
                    "sample_angles_deg": np.asarray(
                        spec["sampling"]["psf_sample_angles_deg"], float
                    ),
                    "resource_policy": policy,
                }
                admitted = preflight_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
                if admitted["status"] == "ready_under_declared_policy":
                    break
                if admitted["status"] not in {
                    "blocked_working_memory_policy",
                    "blocked_source_query_tile_policy",
                }:
                    raise ValueError(
                        f"resource_blocked: {admitted['status']}: {admitted['reason']}"
                    )
                actual_source_side //= 2
            if admitted["status"] != "ready_under_declared_policy":
                raise ValueError(f"No optical tile fits the declared policy: {admitted['reason']}")
            value, execution = _compute_preflighted_streamed_analytic_offaxis_mean(
                provider,
                sensor,
                sensor_tile_bounds_row_col_exclusive=tile,
                source_tile_shape_row_col=(actual_source_side, actual_source_side),
                sample_angles_deg=np.asarray(spec["sampling"]["psf_sample_angles_deg"], float),
                resource_policy=policy,
                admitted_plan=admitted,
            )
            mean[
                tile[0] - bounds[0] : tile[1] - bounds[0], tile[2] - bounds[2] : tile[3] - bounds[2]
            ] = value
            tiles.append(
                {
                    "native_bounds": list(tile),
                    "optical_bounds": execution["preflight"]["optical_roi_row_col_exclusive"]
                    if "preflight" in execution
                    else None,
                    "execution": execution,
                }
            )
    report = {
        "status": "strict_global_roi_mean_executed",
        "native_tile_side": side,
        "native_tile_count": len(tiles),
        "tile_executions": tiles,
        "full_spectral_scene_materialized": False,
    }
    report.update(
        {
            "road_input": provider.metadata,
            "spectral_proxy": proxy,
            "elapsed_s": time.monotonic() - start,
            "candidate_id": candidate["id"],
            "native_roi_bounds": list(bounds),
            "common_grid_antialias_sigma_native_pixels": list(sigma),
            "psf_source_pitch_um": [
                x * 1000 for x in candidate["lens"]["model"]["sample_spacing_mm_row_col"]
            ],
            "convolution_pitch_um": provider.oi_pitch_um,
            "sensor_pixel_pitch_um": candidate["sensor"]["native_response"]["pixel_pitch_um"],
            "original_rgb_information_limit_retained": True,
        }
    )
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "mean_electrons.npy", mean)
    write_json(output / "optical-execution.json", report)
    return mean, report, optics, sensor, coordinates


def roi_capture_sensor(sensor, bounds, mean):
    """Explicit global FPN maps plus CameraE2E's native stochastic readout.

    Fixed pattern maps use the already supported coordinate-keyed streams;
    shot/read draws use resample_sensor_capture. Global location survives a
    crop and repeated captures share the same fixed maps.
    """
    from .lens_sensor_out_of_core_readout import (
        _STREAM_COLUMN_GAIN,
        _STREAM_COLUMN_OFFSET,
        _STREAM_PIXEL_GAIN,
        _STREAM_PIXEL_OFFSET,
        _calibrated_maps,
        _config,
        _normal,
    )

    config = _config(sensor)
    maps = _calibrated_maps(sensor, tuple(sensor.fields["size"]))
    r0, r1, c0, c1 = bounds
    row, col = np.arange(r0, r1, dtype=np.uint64), np.arange(c0, c1, dtype=np.uint64)
    result = sensor.clone()
    result.data = {"mean_electrons": np.asarray(mean, float)}
    result.fields["size"] = (r1 - r0, c1 - c0)
    definitions = (
        ("offset_fpn_image", "pixel_offset_v", 0.0, _STREAM_PIXEL_OFFSET),
        ("gain_fpn_image", "pixel_gain", 1.0, _STREAM_PIXEL_GAIN),
        ("column_offset_fpn", "column_offset_v", 0.0, _STREAM_COLUMN_OFFSET),
        ("column_gain_fpn", "column_gain", 1.0, _STREAM_COLUMN_GAIN),
    )
    for name, sigma_name, center, stream in definitions:
        if maps[name] is not None:
            value = maps[name][r0:r1, c0:c1] if maps[name].ndim == 2 else maps[name][c0:c1]
        else:
            rr = row if "image" in name else np.array([0], dtype=np.uint64)
            value = center + config["fixed_pattern_sigmas"][sigma_name] * _normal(
                config["fixed_pattern_seed"], rr, col, stream
            )
            if "image" not in name:
                value = value[0]
        result.fields[name] = np.asarray(value, float).copy()
    result.metadata["road_roi"] = {
        "full_shape": list(sensor.fields["size"]),
        "bounds": list(bounds),
        "fixed_pattern_contract": (
            "absolute coordinate keyed maps reused across repeats; native shot/read RNG"
        ),
    }
    return result


def well_saturation_mask(sensor, seed):
    """Replay only the preclip draw; never modify the pinned sensor engine."""
    from pyisetcam.sensor_capture import _fixed_pattern_components

    rng = np.random.default_rng(seed)
    readout = sensor.fields["sensor_readout"]
    mean = sensor.data["mean_electrons"] + readout["dark_current_e_per_s"] * float(
        sensor.fields["integration_time"]
    )
    charge = rng.poisson(mean).astype(float)
    gain, _ = _fixed_pattern_components(sensor, mean.shape)
    return charge * gain >= readout["full_well_e"]


def fixed_profile(sensor, primary_photons, wave):
    """Fix chromatic conversion without removing QE or camera brightness."""
    from pyisetcam.sensor import _sensor_qe_on_wave

    response = primary_photons @ _sensor_qe_on_wave(sensor, np.asarray(wave, float))
    if response.shape != (3, 3) or np.linalg.cond(response) > 100:
        raise ValueError("The sparse spectral proxy cannot support a stable fixed RGB transform.")
    # Keep the mean white response; normalize chromatic direction only.
    white_mean_response = float(response.sum(axis=0).mean())
    matrix = white_mean_response * np.linalg.inv(response.T)
    profile = isp.default_profile()
    profile["ccm"]["matrix_rgb"] = matrix.tolist()
    return isp.validate_profile(profile), {
        "calibration": "fixed display-primary/QE proxy; mean white response retained",
        "response_matrix": response.tolist(),
        "condition_number": float(np.linalg.cond(response)),
        "white_mean_response": white_mean_response,
        "physical_color_calibration": False,
    }


def fixed_isp(raw, sensor, bounds, profile):
    readout = sensor.fields["sensor_readout"]
    fmt = {
        "cfa": "RGGB",
        "bit_depth": int(readout["bit_depth"]),
        "black_level_dn": readout["black_level_dn"],
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }
    geometry = {
        "kind": "native_sensor",
        "full_sensor_shape": list(sensor.fields["size"]),
        "crop_origin": [bounds[0], bounds[2]],
    }
    if raw.size <= isp.MAX_FRAME_PIXELS:
        output = isp.process_raw(raw, fmt, profile, geometry=geometry)
        return output["arrays"]["ccm_rgb_frames"][0], output["summary"]
    linear = np.empty((*raw.shape, 3), float)
    tiles = []
    for r0 in range(0, raw.shape[0], 512):
        for c0 in range(0, raw.shape[1], 512):
            r1, c1 = min(raw.shape[0], r0 + 512), min(raw.shape[1], c0 + 512)
            a, b, c, d = (
                max(0, r0 - 2),
                min(raw.shape[0], r1 + 2),
                max(0, c0 - 2),
                min(raw.shape[1], c1 + 2),
            )
            local = {**geometry, "crop_origin": [bounds[0] + a, bounds[2] + c]}
            output = isp.process_raw(raw[a:b, c:d], fmt, profile, geometry=local)
            linear[r0:r1, c0:c1] = output["arrays"]["ccm_rgb_frames"][
                0, r0 - a : r1 - a, c0 - c : c1 - c
            ]
            tiles.append([r0, r1, c0, c1])
    return linear, {
        "profile": profile,
        "native_tiled_bilinear": True,
        "demosaic_halo_pixels": 2,
        "output_tiles": tiles,
        "global_geometry": geometry,
        "raw_format": fmt,
    }


def to_common_grid(array, coordinates, bounds, *, antialias=True):
    coordinates = (coordinates[0] - bounds[0], coordinates[1] - bounds[2])
    original = np.asarray(array, float)
    if antialias:
        sigma = _antialias_sigma(coordinates)
        original = gaussian_filter(
            original, sigma if original.ndim == 2 else (*sigma, 0.0), mode="reflect", truncate=4.0
        )
    if original.ndim == 2:
        return map_coordinates(
            original, coordinates, order=1, mode="constant", cval=np.nan, prefilter=False
        )
    return np.stack(
        [
            map_coordinates(
                original[..., channel],
                coordinates,
                order=1,
                mode="constant",
                cval=np.nan,
                prefilter=False,
            )
            for channel in range(3)
        ],
        axis=-1,
    )


def native_mask(sensor, optics, bounds, roi, common_mask):
    """Count physical native pixels within a fixed angular mask, before filtering."""
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    r0, r1, c0, c1 = bounds
    rows, cols = sensor.fields["size"]
    pitch_mm = float(sensor.fields["pixel"]["size_m"][0]) * 1000
    yy, xx = np.meshgrid(
        (np.arange(r0, r1) + 0.5 - rows / 2) * pitch_mm,
        (np.arange(c0, c1) + 0.5 - cols / 2) * pitch_mm,
        indexing="ij",
    )
    radius = np.hypot(xx, yy)
    ideal, distorted = _strict_raytrace_geometry_axes(optics["raytrace"], 550.0)
    factor = np.divide(
        np.interp(radius, distorted, ideal), radius, out=np.ones_like(radius), where=radius > 0
    )
    focal_mm = optics["raytrace"]["effective_focal_length_m"] * 1000
    x0, y0, x1, y1 = roi["tangent_bounds_xyxy"]
    query = [
        ((yy * factor / focal_mm) - y0) / (y1 - y0) * (common_mask.shape[0] - 1),
        ((xx * factor / focal_mm) - x0) / (x1 - x0) * (common_mask.shape[1] - 1),
    ]
    result = (
        map_coordinates(common_mask.astype(float), query, order=0, mode="constant", prefilter=False)
        > 0.5
    )
    if result.sum() < 25:
        raise ValueError("The angular mask has insufficient native sensor support.")
    return result


def reference_linear_isp(mean, sensor, bounds, profile):
    from pyisetcam.ip import _ie_bilinear

    readout = sensor.fields["sensor_readout"]
    signal = np.asarray(mean, float) * readout["gain"] / readout["full_well_e"]
    pattern = np.array([[1, 2], [2, 3]])
    phase = pattern[
        (np.arange(signal.shape[0]) + bounds[0])[:, None] % 2,
        (np.arange(signal.shape[1]) + bounds[2])[None, :] % 2,
    ]
    planes = np.stack(
        [np.where(phase == channel + 1, signal, 0.0) for channel in range(3)], axis=-1
    )
    return _ie_bilinear(planes, pattern) @ np.asarray(profile["ccm"]["matrix_rgb"]).T


def capture_series(
    spec,
    candidate,
    image,
    roi,
    output,
    *,
    mean,
    report,
    optics,
    sensor,
    coordinates,
    target_repeats=None,
):
    from pyisetcam.sensor_capture import resample_sensor_capture
    from pyisetcam.utils import linear_to_srgb

    requested_repeats = target_repeats or spec["capture"]["initial_repeats"]
    bounds = tuple(report["native_roi_bounds"])
    proxy = report["spectral_proxy"]
    profile, calibration = fixed_profile(
        sensor, np.asarray(proxy["primary_photons"]), spec["spectral_proxy"]["wavelength_nm"]
    )
    # The caller passes the package root explicitly through report to avoid
    # depending on output-directory depth when used from a Worker.
    mask_file = Path(report["package_root"]) / roi["mask_path"]
    if file_sha256(mask_file) != roi["mask_sha256"]:
        raise ValueError("Frozen ROI mask changed.")
    with np.load(mask_file) as masks:
        obj, bg = masks["object"], masks["background"]
    native_obj = native_mask(sensor, optics, bounds, roi, obj)
    results = []
    for brightness in spec["capture"]["brightness_multipliers"]:
        target_repeats = (
            requested_repeats.get(brightness, spec["capture"]["initial_repeats"])
            if isinstance(requested_repeats, dict)
            else requested_repeats
        )
        directory = output / f"light-{brightness:g}"
        directory.mkdir(parents=True, exist_ok=True)
        series_path = directory / "linear_luminance_frames.npy"
        native_path = directory / "raw_dn_frames.npy"
        completed_path = directory / "capture.json"
        previous = json_load(completed_path) if completed_path.exists() else None
        already = previous["repeats"] if previous else 0
        if previous:
            expected = {
                "candidate_id": candidate["id"],
                "image_id": image["id"],
                "roi_id": roi["id"],
                "brightness": brightness,
                "profile_sha256": json_sha256(profile),
                "fixed_pattern_seed": spec["capture"]["fixed_pattern_seed"],
                "native_roi_bounds": list(bounds),
            }
            if any(previous.get(key) != value for key, value in expected.items()):
                raise ValueError("Cached capture does not match its frozen condition or ISP.")
            if json_sha256(previous["readout"]) != json_sha256(sensor.fields["sensor_readout"]):
                raise ValueError("Cached RAW uses a different readout model.")
            previous_native = Path(report["package_root"]) / previous["raw_file"]
            previous_series = Path(report["package_root"]) / previous["luminance_file"]
            for key, path in (
                ("raw_sha256", previous_native),
                ("luminance_sha256", previous_series),
            ):
                if file_sha256(path) != previous[key]:
                    raise ValueError("Cached capture artifact changed; refusing to reuse it.")
        if already >= target_repeats:
            if bg.any():
                from .road_design_statistics import STATISTICS_METHOD, cnr_with_interval

                if previous.get("statistics_method") != STATISTICS_METHOD:
                    previous.update(
                        cnr_with_interval(
                            np.load(previous_series, mmap_mode="r"),
                            obj,
                            bg,
                            replicates=spec["statistics"]["bootstrap_replicates"],
                            seed=spec["capture"]["seed"],
                        )
                    )
                    write_json(completed_path, previous)
            results.append(previous)
            continue
        import shutil

        required_disk = target_repeats * (mean.size * 4 + obj.size * 8)
        free = shutil.disk_usage(directory).free
        if free < required_disk + spec["resources"]["min_free_disk_bytes"]:
            raise ValueError(
                f"This capture needs {required_disk} bytes plus the configured free-disk reserve; "
                f"available {free} bytes. Move the package to a larger workspace volume and resume."
            )
        if already:
            # Additional repetitions get new files. Earlier RAW arrays and
            # first-photo evidence keep their original byte hashes and paths.
            native_path = directory / f"raw_dn_frames-{target_repeats}.npy"
            series_path = directory / f"linear_luminance_frames-{target_repeats}.npy"
        frames = np.lib.format.open_memmap(
            directory / "luminance.tmp.npy",
            mode="w+",
            dtype=np.float64,
            shape=(target_repeats, *obj.shape),
        )
        raws = np.lib.format.open_memmap(
            directory / "raw.tmp.npy",
            mode="w+",
            dtype=np.int32,
            shape=(target_repeats, *mean.shape),
        )
        well_fractions, adc_fractions, seeds = [], [], []
        roi_sensor = roi_capture_sensor(sensor, bounds, mean * brightness)
        white = 2 ** roi_sensor.fields["sensor_readout"]["bit_depth"] - 1
        summary = None
        for index in range(target_repeats):
            # Stable identity key; fresh independent draws for every condition.
            seed = int(
                json_sha256(
                    [
                        spec["capture"]["seed"],
                        candidate["id"],
                        image["id"],
                        roi["id"],
                        brightness,
                        index,
                    ]
                )[:15],
                16,
            )
            seeds.append(seed)
            if index < already:
                frames[index] = np.load(previous_series, mmap_mode="r")[index]
                raws[index] = np.load(previous_native, mmap_mode="r")[index]
                well_fractions.append(previous["well_fractions"][index])
                adc_fractions.append(previous["adc_fractions"][index])
                continue
            capture = resample_sensor_capture(roi_sensor, seed=seed)
            raw = capture.data["raw_dn"]
            well = well_saturation_mask(roi_sensor, seed)
            if (
                abs(
                    float(well.mean())
                    - capture.metadata["sensor_capture"]["saturation"]["well_fraction"]
                )
                > 1e-14
            ):
                raise ValueError("Well mask replay differs from the pinned RAW readout.")
            linear, summary = fixed_isp(raw, sensor, bounds, profile)
            common = to_common_grid(linear, coordinates, bounds)
            frames[index] = common @ [0.2126, 0.7152, 0.0722]
            raws[index] = raw
            well_fractions.append(float(well[native_obj].mean()))
            adc_fractions.append(float((raw[native_obj] == white).mean()))
            if index == 0:
                np.save(directory / "fixed_isp_linear_rgb.npy", common)
                Image.fromarray(
                    np.rint(linear_to_srgb(np.clip(common, 0, 1)) * 255).astype(np.uint8)
                ).save(directory / "preview.png")
                write_json(
                    directory / "isp.json",
                    {"execution": summary, "chromatic_calibration": calibration},
                )
        frames.flush()
        raws.flush()
        del frames, raws
        (directory / "luminance.tmp.npy").replace(series_path)
        (directory / "raw.tmp.npy").replace(native_path)
        noise_free = to_common_grid(
            reference_linear_isp(mean * brightness, sensor, bounds, profile), coordinates, bounds
        )
        np.save(directory / "noise_free_linear_rgb.npy", noise_free)
        y = noise_free @ [0.2126, 0.7152, 0.0722]
        contrast = abs(float(y[obj].mean() - y[bg].mean())) if bg.any() else None
        local = (
            contrast / max(1e-12, abs(float(y[obj].mean())) + abs(float(y[bg].mean())))
            if bg.any()
            else None
        )
        result = {
            "candidate_id": candidate["id"],
            "image_id": image["id"],
            "roi_id": roi["id"],
            "roi_kind": roi["kind"],
            "brightness": brightness,
            "repeats": target_repeats,
            "well_fractions": well_fractions,
            "adc_fractions": adc_fractions,
            "well_fraction": float(np.mean(well_fractions)),
            "adc_fraction": float(np.mean(adc_fractions)),
            "noise_free_contrast": contrast,
            "local_contrast": local,
            "mean_photoelectrons": float((mean * brightness).mean()),
            "readout": sensor.fields["sensor_readout"],
            "native_roi_shape": list(mean.shape),
            "common_shape_row_col": list(obj.shape),
            "native_roi_bounds": list(bounds),
            "seeds": seeds,
            "fixed_pattern_seed": spec["capture"]["fixed_pattern_seed"],
            "profile_sha256": json_sha256(profile),
            "profile": profile,
            "raw_file": str(native_path.relative_to(Path(report["package_root"]))),
            "raw_sha256": file_sha256(native_path),
            "luminance_sha256": file_sha256(series_path),
            "luminance_file": str(series_path.relative_to(Path(report["package_root"]))),
            "preview_file": str(
                (directory / "preview.png").relative_to(Path(report["package_root"]))
            ),
            "cnr_signal": "signed pre-tone linear ISP luminance on fixed common angular grid",
            "fixed_pattern": roi_sensor.metadata["road_roi"],
            "saturation_mask_native_pixels": int(native_obj.sum()),
            "saturation_count_domain": (
                "native pixels in the frozen angular mask, before ISP and anti-alias filtering"
            ),
            "well_mask_check": (
                "exact match to native preclip saturation fraction for every fresh capture"
            ),
        }
        if bg.any():
            from .road_design_statistics import cnr_with_interval

            result.update(
                cnr_with_interval(
                    np.load(series_path, mmap_mode="r"),
                    obj,
                    bg,
                    replicates=spec["statistics"]["bootstrap_replicates"],
                    seed=spec["capture"]["seed"],
                )
            )
        else:
            result.update({"cnr": None, "cnr_ci95": None})
        write_json(completed_path, result)
        results.append(result)
    return results


def json_load(path):
    import json

    return json.loads(Path(path).read_text())

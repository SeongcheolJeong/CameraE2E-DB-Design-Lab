"""Local background response at a selected sensor-plane ghost probe.

This is an explicitly interpolated local-field approximation over the preserved
Reference optical nodes, not a new lens trace. Point-source/ghost transport is
computed separately at the exact receiver coordinates by reference_ghost_engine.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as pixel

PITCH_UM = 2.9
NATIVE_SHAPE = (256, 256)
BLACK_CHARTS = {"black", "black_source"}


def _xy(value):
    xy = np.asarray(value, dtype=float)
    if xy.shape != (2,) or not np.isfinite(xy).all():
        raise ValueError("Native ghost probe requires two finite sensor-plane coordinates in mm")
    half = PITCH_UM * NATIVE_SHAPE[0] / 2000
    if np.any(np.abs(xy) + half > optics.MAX_HEIGHT_MM):
        raise ValueError("The entire 256×256 probe must lie inside the ±13.66 mm optical map")
    return xy


def _radial_nodes(solution):
    """Select preserved radial samples; no nearest-field fallback."""
    fields = solution["result"]["fields"]
    nodes = []
    for radius in (0.0, 0.5, 0.85):
        matches = [
            (i, f)
            for i, f in enumerate(fields)
            if f["normalized_radius"] == radius and f["status"] == "succeeded"
        ]
        if not matches:
            raise ValueError(f"Preserved optical response is missing radial node {radius}")
        i, field = matches[0]
        nodes.append((float(np.linalg.norm(field["image_xy_mm"])), i, field))
    heights = np.asarray([n[0] for n in nodes])
    if abs(heights[0]) > 1e-8 or not np.all(np.diff(heights) > 0):
        raise ValueError("Reference chief-image radial nodes must start at zero and increase")
    return nodes


def _bracket(nodes, height):
    heights = np.asarray([n[0] for n in nodes])
    if height > heights[-1] + 1e-10:
        raise ValueError(
            f"Dark/contrast probe center radius {height:.4f} mm exceeds preserved background "
            f"support {heights[-1]:.4f} mm; select a point inside that radius or use black_source. "
            "A new optical field solve is required outside it."
        )
    if height <= 1e-10:
        return [(0, 1.0)]
    for i, r in enumerate(heights):
        if abs(height - r) <= 1e-10:
            return [(i, 1.0)]
    upper = int(np.searchsorted(heights, height))
    mix = (height - heights[upper - 1]) / (heights[upper] - heights[upper - 1])
    return [(upper - 1, float(1 - mix)), (upper, float(mix))]


def supported_radius_mm(source_or_solution):
    """Inspect preserved radial support before running costly ghost transport."""
    solution = (
        source_or_solution
        if isinstance(source_or_solution, dict)
        else optics.load_solution(Path(source_or_solution) / "optics")
    )
    return _radial_nodes(solution)[-1][0]


def _rotate_kernel(kernel, degrees, size):
    """Rotate about the shared 550-nm origin, retaining chromatic centroid shifts.

    Array rows increase downward while physical +Y is up. A positive azimuth
    therefore moves a +X impulse upward. Enlarged odd support avoids rotation
    clipping. Each receiving-cell mass is bilinearly deposited at its rotated
    center; energy and centroid are conserved while the rotated cell footprint
    itself remains an approximation.
    """
    kernel = np.asarray(kernel, dtype=float)
    if (
        kernel.ndim != 2
        or kernel.shape[0] != kernel.shape[1]
        or kernel.shape[0] % 2 != 1
        or not np.isfinite(kernel).all()
        or np.min(kernel) < 0
        or kernel.sum() <= 0
    ):
        raise ValueError("A finite nonnegative odd square optical PSF is required")
    angle = math.radians(degrees)
    rr, cc = np.indices(kernel.shape, dtype=float)
    rr -= (kernel.shape[0] - 1) / 2
    cc -= (kernel.shape[1] - 1) / 2
    target_r = math.cos(angle) * rr - math.sin(angle) * cc + (size - 1) / 2
    target_c = math.sin(angle) * rr + math.cos(angle) * cc + (size - 1) / 2
    r0, c0 = np.floor(target_r).astype(int), np.floor(target_c).astype(int)
    fr, fc = target_r - r0, target_c - c0
    rotated = np.zeros((size, size))
    for dr, wr in ((0, 1 - fr), (1, fr)):
        for dc, wc in ((0, 1 - fc), (1, fc)):
            r, c = r0 + dr, c0 + dc
            weight = kernel * wr * wc
            inside = (r >= 0) & (r < size) & (c >= 0) & (c < size)
            if np.any((weight > 1e-15) & ~inside):
                raise ValueError("Rotated PSF support is too small; enlarge explicit halo")
            np.add.at(rotated, (r[inside], c[inside]), weight[inside])
    retained = float(rotated.sum() / kernel.sum())
    if retained <= 0:
        raise ValueError("Rotated optical PSF has no retained mass")
    return rotated / rotated.sum(), retained


def interpolated_response(solution, response, probe_xy_mm):
    """Return PSF, integrated irradiance×absolute QE, and explicit provenance."""
    xy = _xy(probe_xy_mm)
    height = float(np.linalg.norm(xy))
    azimuth = math.degrees(math.atan2(xy[1], xy[0])) % 360 if height else 0.0
    nodes = _radial_nodes(solution)
    weights = _bracket(nodes, height)
    arrays = solution["arrays"]
    raw_size = arrays["psf"].shape[-1]
    size = int(math.ceil((raw_size - 1) * math.sqrt(2))) + 1
    size += 1 - size % 2
    psfs = np.zeros((len(optics.WAVE), size, size))
    transfer = np.zeros((len(optics.WAVE), 4))
    rotations = []
    for ri, weight in weights:
        _, fi, field = nodes[ri]
        retained = []
        for wi, wave in enumerate(optics.WAVE):
            kernel, mass = _rotate_kernel(
                arrays["psf"][wi, fi], azimuth - field["azimuth_deg"], size
            )
            psfs[wi] += kernel * weight
            retained.append(mass)
            conditional = arrays["conditional_weights"][wi, ri]
            admitted = conditional > 0
            directions = arrays["directions_xyz"][ri, admitted]
            theta = np.degrees(np.arccos(np.clip(directions[:, 2], -1, 1)))
            phi = (np.degrees(np.arctan2(directions[:, 1], directions[:, 0])) + azimuth) % 360
            qe = pixel.absolute_angular_response(response, theta, phi, float(wave))
            rotated_qe = pixel.absolute_angular_response(
                response, theta, (phi + 90) % 360, float(wave)
            )
            if (
                np.asarray(qe).shape != (len(theta), 4)
                or not np.isfinite(qe).all()
                or np.min(qe) < 0
                or np.max(qe) > 1
                or not np.allclose(qe, rotated_qe, rtol=1e-10, atol=1e-12)
            ):
                raise ValueError(
                    "Probe requires the source's absolute azimuth-invariant angular QE"
                )
            effective = np.sum(qe * conditional[admitted, None], axis=0)
            transfer[wi] += weight * arrays["irradiance_per_radiance_sr"][wi, ri] * effective
        rotations.append(
            {
                "field_id": field["id"],
                "image_height_mm": nodes[ri][0],
                "weight": weight,
                "rotation_deg": azimuth - field["azimuth_deg"],
                "pre_normalization_rotation_mass_ratio_min": min(retained),
                "pre_normalization_rotation_mass_ratio_max": max(retained),
            }
        )
    geometry = copy.deepcopy(nodes[0][2]["geometry"])
    halo = size // 2
    geometry.update(
        input_shape_row_col=[256 + 2 * halo] * 2,
        crop_bounds_row_col=[halo, halo + 256, halo, halo + 256],
        field_approximation="radially_interpolated_rotated_local_geometric_PSF",
        native_row_axis="decreasing_sensor_y",
    )
    metadata = {
        "schema": "camera_reference_ghost_probe_background_v1",
        "probe_xy_mm": xy.tolist(),
        "native_row_axis": "decreasing_sensor_y",
        "image_height_mm": height,
        "azimuth_deg": azimuth,
        "object_angle_deg": sum(
            weight * nodes[ri][2]["object_angle_deg"] for ri, weight in weights
        ),
        "supported_center_radius_mm": [nodes[0][0], nodes[-1][0]],
        "support_scope": (
            "ROI center; original local-field response approximation within native ROI"
        ),
        "optical_revision": solution["result"]["revision"],
        "source_arrays_sha256": solution["result"].get("arrays_sha256"),
        "radial_weights": rotations,
        "geometry": geometry,
        "pixel_aperture_applications": 1,
        "angular_response": "radial interpolation of irradiance times pupil-averaged absolute QE",
        "psf_interpolation": (
            "energy-conserving bilinear deposition of receiving-cell centers about common "
            "550nm origin; chromatic offsets retained; finite-cell rotation approximate"
        ),
        "lsc_policy": "disabled for all comparison arms; no nearest-field calibration map borrowed",
        "limitations": [
            "Interpolated local geometric response, not newly traced selected optical field",
            "No diffraction, within-ROI distortion, or PSF/landing-direction correlation",
            "Coating applied as pupil-average spectral transmission; "
            "PSF apodization not recomputed",
            "Geometric/angular source model retains its original "
            "validation and material assumptions",
        ],
    }
    return psfs, transfer, metadata


def background(
    workspace,
    source,
    capture,
    prescription,
    coating_index,
    probe_xy_mm,
    chart,
    samples,
    chart_scene_fn,
):
    """Compute mosaic e-/s for a custom probe; gain-map policy is always disabled.

    ``capture`` is accepted for the shared acquisition adapter signature; its
    geometry or calibration never overrides the selected physical coordinates.
    """
    xy = _xy(probe_xy_mm)
    if chart in BLACK_CHARTS:
        geometry = {
            "native_shape_row_col": list(NATIVE_SHAPE),
            "input_shape_row_col": list(NATIVE_SHAPE),
            "crop_bounds_row_col": [0, 256, 0, 256],
            "pixel_pitch_um": PITCH_UM,
            "native_row_axis": "decreasing_sensor_y",
            "cfa_origin": {
                "local_row": 0,
                "local_col": 0,
                "pattern": "RGGB",
                "scope": "independent_virtual_bench_ROI",
            },
        }
        _, chart_metadata = chart_scene_fn("black", geometry, {"white": np.ones(len(optics.WAVE))})
        return np.zeros(NATIVE_SHAPE), {
            "schema": "camera_reference_ghost_probe_background_v1",
            "probe_xy_mm": xy.tolist(),
            "native_row_axis": "decreasing_sensor_y",
            "background": "zero radiance; direct source and ghosts added separately before readout",
            "unit": "electrons/s",
            "geometry": geometry,
            "chart_definition": chart_metadata,
            "transmission": np.ones(len(optics.WAVE)).tolist(),
            "lsc_policy": (
                "disabled for all comparison arms; no nearest-field calibration map borrowed"
            ),
            "local_background_optics": "not_required_for_zero_radiance",
        }
    from .reference_ghost_engine import direct_transmission
    from .virtual_lab_engine import _spectra

    solution = optics.load_solution(Path(source) / "optics")
    response = json.loads((Path(source) / "pixel-response.json").read_text())
    psfs, transfer, metadata = interpolated_response(solution, response, xy)
    transmission = direct_transmission(prescription, metadata["object_angle_deg"], samples)[
        :, coating_index
    ]
    photons, chart_metadata = chart_scene_fn(
        chart, metadata["geometry"], _spectra(workspace, "D65", 1000)
    )
    photons = np.asarray(photons)
    if (
        photons.shape != (*metadata["geometry"]["input_shape_row_col"], len(optics.WAVE))
        or not np.isfinite(photons).all()
        or np.min(photons) < 0
    ):
        raise ValueError("Probe chart must provide finite nonnegative spectral radiance with halo")
    r0, r1, c0, c1 = metadata["geometry"]["crop_bounds_row_col"]
    planes = np.zeros((*NATIVE_SHAPE, 4))
    for wi in range(len(optics.WAVE)):
        irradiance = np.maximum(fftconvolve(photons[..., wi], psfs[wi], mode="same"), 0)[
            r0:r1, c0:c1
        ]
        planes += (
            irradiance[..., None] * transfer[wi] * transmission[wi] * 10 * (PITCH_UM * 1e-6) ** 2
        )
    rr, cc = np.indices(NATIVE_SHAPE)
    sites = np.array([[0, 1], [2, 3]])[rr % 2, cc % 2]
    rate = np.take_along_axis(planes, sites[..., None], axis=-1)[..., 0]
    metadata.update(
        chart_definition=chart_metadata,
        unit="electrons/s",
        source_input_unit="photons/s/m2/sr/nm",
        spectral_bin_width_nm=10,
        transmission=transmission.tolist(),
    )
    return rate, metadata

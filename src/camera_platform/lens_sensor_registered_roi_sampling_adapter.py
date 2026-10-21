"""Read-only frequency-planner inputs from an already loaded registered ROI.

This adapter never loads a Lens/Sensor DB asset, decodes a PSF array, plans an
optical tile, or changes a queued RAW job. Use it only after the registered
ROI's pinned models and active-mode declaration have been loaded and checked
by the existing Worker path. Its output is keyword arguments for
``plan_frequency_sampling``; it is not an accuracy or execution admission.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

import numpy as np


def _positive(value: Any, label: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{label} must be a finite positive scalar.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive scalar.")
    return number


def _sensor_pixel_pitch_um(sensor: Any) -> tuple[float, float]:
    fields = getattr(sensor, "fields", None)
    if not isinstance(fields, Mapping) or not isinstance(fields.get("pixel"), Mapping):
        raise ValueError("Loaded sensor pixel metadata is required.")
    size = fields["pixel"].get("size_m")
    if not isinstance(size, (tuple, list)) and getattr(size, "shape", None) != (2,):
        raise ValueError("Loaded sensor needs exactly two pixel-size coordinates.")
    if len(size) != 2:
        raise ValueError("Loaded sensor needs exactly two pixel-size coordinates.")
    return (_positive(size[0], "Sensor row pitch") * 1e6,
            _positive(size[1], "Sensor column pitch") * 1e6)


def frequency_sampling_kwargs_for_registered_roi(
    specification: Mapping[str, Any],
    *,
    optics: Mapping[str, Any],
    sensor: Any,
    active_mode: Mapping[str, Any],
    scene_grid_mode: Literal["explicit", "adaptive"] = "explicit",
) -> dict[str, Any]:
    """Return only planner kwargs for the current registered ROI model.

    The source PSF pitch comes from the *loaded* strict raytrace PSF table,
    never an inferred or enlarged array. The image distance uses the exact
    ``_image_distance_m`` rule used by the registered analytic Scene provider.
    ``adaptive`` only proposes alternate fixed-FOV grids; it does not alter
    the submitted specification or run any captures. The caller must still
    evaluate PSF source fidelity, Scene/PSF halo and numerical convergence.
    """
    from pyisetcam.optics import _image_distance_m, _raytrace_is_strict
    from pyisetcam.types import Scene

    from .lens_sensor_registered_roi_disk_raw_job import (
        _MAX_SCENE_AXIS,
        _MAX_SCENE_CELLS,
    )
    from .lens_sensor_registered_roi_disk_raw_job import specification as checked_spec

    spec = checked_spec(dict(specification))
    if scene_grid_mode not in {"explicit", "adaptive"}:
        raise ValueError("Scene grid mode must be explicit or adaptive.")
    if not isinstance(active_mode, Mapping):
        raise ValueError("Loaded active-mode declaration is required.")
    selected = spec["sensor_revision"]
    if (
        active_mode.get("id") != spec["active_mode_declaration_id"]
        or active_mode.get("sensor_run_id") != selected["run_id"]
        or str(active_mode.get("sensor_report_code", "")).lower()
        != selected["identifier"].lower()
    ):
        raise ValueError("Active mode does not match the selected saved sensor run.")

    sensor_pitch = _sensor_pixel_pitch_um(sensor)
    mode_pitch = _positive(active_mode.get("pixel_pitch_um"), "Active-mode pixel pitch")
    if any(not math.isclose(pitch, mode_pitch, rel_tol=1e-9, abs_tol=1e-9)
           for pitch in sensor_pitch):
        raise ValueError("Loaded sensor pitch differs from the registered active mode.")
    fields = sensor.fields
    if tuple(fields.get("size", ())) != (
        active_mode.get("active_rows"), active_mode.get("active_cols")
    ):
        raise ValueError("Loaded sensor shape differs from the registered active mode.")
    saved = getattr(sensor, "metadata", {}).get("sensor_simulation_db", {})
    if (
        not isinstance(saved, Mapping)
        or saved.get("run_id") != selected["run_id"]
        or str(saved.get("report_code", "")).lower() != selected["identifier"].lower()
        or saved.get("result_sha256") != active_mode.get("sensor_result_sha256")
    ):
        raise ValueError("Loaded sensor metadata differs from the pinned saved run.")

    if not isinstance(optics, Mapping) or optics.get("model") != "raytrace":
        raise ValueError("Loaded strict raytrace optics are required.")
    raytrace = optics.get("raytrace")
    if (
        not isinstance(raytrace, dict)
        or not _raytrace_is_strict(raytrace)
        or raytrace.get("psf_kind") != "geometric"
        or raytrace.get("diffraction_included") is not False
        or not math.isinf(float(raytrace.get("object_distance_m", math.nan)))
        or raytrace.get("geometric_psf_rehistogram") is not None
    ):
        raise ValueError("Planner adapter requires the stored infinity-conjugate geometric PSF.")
    psf_table = raytrace.get("psf")
    if not isinstance(psf_table, Mapping):
        raise ValueError("Loaded strict raytrace PSF metadata is required.")
    spacing = psf_table.get("sample_spacing_mm")
    if (
        not isinstance(spacing, (tuple, list))
        and getattr(spacing, "shape", None) not in ((1,), (2,))
    ):
        raise ValueError("Loaded PSF needs one or two source-spacing coordinates.")
    if len(spacing) == 1:
        spacing = (spacing[0], spacing[0])
    elif len(spacing) != 2:
        raise ValueError("Loaded PSF needs one or two source-spacing coordinates.")
    psf_pitch_um = (
        _positive(spacing[0], "Stored PSF row pitch") * 1e3,
        _positive(spacing[1], "Stored PSF column pitch") * 1e3,
    )
    distance_m = _positive(
        _image_distance_m(dict(optics), Scene(fields={"distance_m": math.inf}, data={})),
        "Strict raytrace image distance",
    )
    result = {
        "native_pixel_pitch_um_row_col": sensor_pitch,
        "stored_psf_pitch_um_row_col": psf_pitch_um,
        "image_distance_m": distance_m,
        "scene_horizontal_fov_deg": spec["analytic_scene"]["horizontal_fov_deg"],
        "reference_scene_shape_row_col": tuple(spec["analytic_scene"]["shape_row_col"]),
        "max_scene_axis": _MAX_SCENE_AXIS,
        "max_virtual_scene_cells": _MAX_SCENE_CELLS,
    }
    if scene_grid_mode == "explicit":
        result["manual_scene_shape_row_col"] = tuple(spec["analytic_scene"]["shape_row_col"])
    return result

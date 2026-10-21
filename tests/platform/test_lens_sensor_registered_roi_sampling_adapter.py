"""Loaded registered ROI metadata can seed a planner without reading PSF pixels."""

from __future__ import annotations

import math
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform.lens_sensor_frequency_sampling import plan_frequency_sampling
from camera_platform.lens_sensor_registered_roi_disk_raw_job import default_spec
from camera_platform.lens_sensor_registered_roi_sampling_adapter import (
    frequency_sampling_kwargs_for_registered_roi,
)


class _ForbiddenPsfPixels:
    def __array__(self, *_args, **_kwargs):
        raise AssertionError("A read-only sampling adapter decoded the PSF array.")


def _inputs():
    spec = default_spec()
    spec.update(
        lens_revision_id="00000000-0000-0000-0000-000000000001",
        sensor_revision={"identifier": "DEP-2209-801", "run_id": "a" * 64},
        active_mode_declaration_id="sampling-mode",
        accept_approximate_lens_radiometry=True,
        acknowledge_unverified_mode_and_uniform_sensor_response=True,
    )
    mode = {
        "id": "sampling-mode",
        "sensor_run_id": "a" * 64,
        "sensor_report_code": "DEP-2209-801",
        "sensor_result_sha256": "b" * 64,
        "active_rows": 2048,
        "active_cols": 14000,
        "pixel_pitch_um": 0.61,
    }
    sensor = SimpleNamespace(
        fields={
            "size": [2048, 14000],
            "pixel": {"size_m": np.array([0.61e-6, 0.61e-6])},
        },
        metadata={"sensor_simulation_db": {
            "run_id": "a" * 64,
            "report_code": "DEP-2209-801",
            "result_sha256": "b" * 64,
        }},
    )
    optics = {
        "model": "raytrace",
        "focal_length_m": 0.01,
        "raytrace": {
            "contract_version": 2,
            "psf_kind": "geometric",
            "diffraction_included": False,
            "object_distance_m": math.inf,
            "effective_focal_length_m": 0.012,
            "psf": {
                "sample_spacing_mm": np.array([0.0004, 0.0005]),
                "function": _ForbiddenPsfPixels(),
            },
        },
    }
    return spec, mode, sensor, optics


def test_planner_inputs_use_loaded_physical_pitches_without_decoding_psf():
    spec, mode, sensor, optics = _inputs()
    original = deepcopy(spec)
    kwargs = frequency_sampling_kwargs_for_registered_roi(
        spec, optics=optics, sensor=sensor, active_mode=mode
    )
    assert kwargs["native_pixel_pitch_um_row_col"] == pytest.approx((0.61, 0.61))
    assert kwargs["stored_psf_pitch_um_row_col"] == pytest.approx((0.4, 0.5))
    assert kwargs["image_distance_m"] == 0.012
    assert kwargs["manual_scene_shape_row_col"] == (2048, 2048)
    assert spec == original
    plan = plan_frequency_sampling(**kwargs)
    assert plan["mode"] == "explicit_grid"
    assert plan["physical_accuracy_qualified"] is False


def test_adaptive_mode_only_proposes_a_different_fixed_fov_grid():
    spec, mode, sensor, optics = _inputs()
    kwargs = frequency_sampling_kwargs_for_registered_roi(
        spec, optics=optics, sensor=sensor, active_mode=mode,
        scene_grid_mode="adaptive",
    )
    assert "manual_scene_shape_row_col" not in kwargs
    assert kwargs["scene_horizontal_fov_deg"] == spec["analytic_scene"]["horizontal_fov_deg"]
    assert plan_frequency_sampling(**kwargs)["mode"] == "adaptive_starting_grid"


def test_mismatched_active_sensor_or_derived_psf_is_rejected():
    spec, mode, sensor, optics = _inputs()
    wrong_mode = dict(mode, id="other-declaration")
    with pytest.raises(ValueError, match="Active mode does not match"):
        frequency_sampling_kwargs_for_registered_roi(
            spec, optics=optics, sensor=sensor, active_mode=wrong_mode
        )
    sensor.fields["pixel"]["size_m"] = np.array([0.7e-6, 0.61e-6])
    with pytest.raises(ValueError, match="pitch differs"):
        frequency_sampling_kwargs_for_registered_roi(
            spec, optics=optics, sensor=sensor, active_mode=mode
        )
    sensor.fields["pixel"]["size_m"] = np.array([0.61e-6, 0.61e-6])
    optics["raytrace"]["geometric_psf_rehistogram"] = {"derived": True}
    with pytest.raises(ValueError, match="stored infinity-conjugate"):
        frequency_sampling_kwargs_for_registered_roi(
            spec, optics=optics, sensor=sensor, active_mode=mode
        )

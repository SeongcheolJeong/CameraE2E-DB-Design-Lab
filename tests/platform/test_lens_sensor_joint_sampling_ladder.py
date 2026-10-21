"""Paired Scene-grid refinement cannot qualify an unrefined PSF source."""

from __future__ import annotations

import numpy as np
import pytest

from camera_platform.lens_sensor_joint_sampling_ladder import (
    run_joint_scene_dense_psf_ladder,
)
from pyisetcam.types import Sensor


def _models():
    source = {"raytrace": {"asset_sha256": "source", "psf": np.array([1.0])}}
    candidate = {"raytrace": {"asset_sha256": "candidate", "psf": np.array([0.9])}}
    sensor = Sensor(fields={"name": "fixed sensor"})
    return source, candidate, sensor


def _fake_runner(monkeypatch, *, errors, invalid=None, wrong_source_identity=False):
    from camera_platform import lens_sensor_registered_roi_frequency_execution as execution

    calls = []
    grids = [(64 * 2**index, 64 * 2**index) for index in range(len(errors))]
    base = np.array([[10.0, 20.0], [30.0, 40.0]])

    def capture(optics, _sensor, *, return_transient_means, **kwargs):
        assert return_transient_means is True
        assert kwargs["max_refinements"] == 0
        assert kwargs["source_tile_shape_row_col"] == (8, 8)
        shape = kwargs["first_scene_shape_row_col"]
        index = grids.index(shape)
        is_candidate = optics["raytrace"]["asset_sha256"] == "candidate"
        calls.append((index, "candidate" if is_candidate else "source"))
        offset = float(errors[index]) + (0.1 if is_candidate else 0.0)
        means = (base + offset, base + offset + 1.0)
        identities = [f"scene-{index}-base", f"scene-{index}-shift"]
        if is_candidate and wrong_source_identity:
            identities[0] = "wrong scene"
        result = {
            "status": "inconclusive_single_grid_no_refinement",
            "sensor_identity": execution._sensor_identity(_sensor),
            "frequency_target_points_status": "inconclusive_mosaic_channel_contract",
            "fixed_physical_input": {
                "reference_scene_aspect_row_col": list(shape),
                "scene_horizontal_fov_deg": 30.0,
                "vertical_fov_deg": 30.0,
                "baseline_edge_offset_deg": 5.0,
                "fixed_shift_deg": 0.001,
                "wavelength_nm": [550.0],
                "bright_side_photons": [1.0],
                "optics_state_sha256": "candidate" if is_candidate else "source",
            },
            "steps": [{
                "scene_shape_row_col": list(shape),
                "oi_pitch_um": 1.0 / 2**index,
                "source_identity_by_phase": identities,
                "mean_electrons_sha256_by_phase": [
                    f"{index}-{is_candidate}-0", f"{index}-{is_candidate}-1",
                ],
                "mean_phase_signal_valid": (index, is_candidate) not in (invalid or set()),
            }],
        }
        return result, means

    monkeypatch.setattr(execution, "run_registered_roi_frequency_sampling", capture)
    return grids, calls


def _run(models, grids):
    return run_joint_scene_dense_psf_ladder(
        *models,
        scene_grid_ladder_row_col=grids,
        runner_kwargs={"source_tile_shape_row_col": (8, 8)},
        mean_delta_tolerance=0.01,
        shift_delta_tolerance=0.01,
    )


def _has_array(item):
    if isinstance(item, np.ndarray):
        return True
    if isinstance(item, dict):
        return any(_has_array(value) for value in item.values())
    if isinstance(item, (tuple, list)):
        return any(_has_array(value) for value in item)
    return False


def test_full_ladder_checks_both_optics_at_each_grid_and_keeps_only_scalars(monkeypatch):
    models = _models()
    grids, calls = _fake_runner(monkeypatch, errors=[4.0, 0.2, 0.1, 0.05])
    report = _run(models, grids)
    assert calls == [
        (index, label) for index in range(4) for label in ("source", "candidate")
    ]
    assert report["all_grids_executed"] is True
    assert report["scene_grid_mean_phase_converged_for_both_optics"] is True
    assert report["selected_scene_shape_row_col"] == list(grids[-1])
    assert report["steps"][-1]["consecutive_refinement_passes_by_optics"] == {
        "source": 2, "candidate": 2,
    }
    assert report["steps"][-1]["source_vs_candidate_fixed_grid"][
        "mean_electron_full_scale_normalized_max_delta"
    ] > 0
    assert report["frequency_target_points_status"] == "inconclusive_mosaic_channel_contract"
    assert report["independent_pupil_density_convergence_established"] is False
    assert report["psf_histogram_pitch_refinement_assessed"] is False
    assert report["physical_mtf_comparison_allowed"] is False
    assert report["product_ranking_allowed"] is False
    assert _has_array(report) is False


def test_later_failed_refinement_revokes_previous_two_passes(monkeypatch):
    models = _models()
    grids, calls = _fake_runner(monkeypatch, errors=[0.2, 0.1, 0.05, 3.0])
    report = _run(models, grids)
    assert len(calls) == 8
    assert report["steps"][2]["consecutive_refinement_passes_by_optics"] == {
        "source": 2, "candidate": 2,
    }
    assert report["steps"][3]["consecutive_refinement_passes_by_optics"] == {
        "source": 0, "candidate": 0,
    }
    assert report["status"] == "inconclusive_joint_scene_grid_refinement"
    assert report["scene_grid_mean_phase_converged_for_both_optics"] is False
    assert "selected_scene_shape_row_col" not in report


def test_previous_invalid_edge_cannot_count_as_refinement_pass(monkeypatch):
    models = _models()
    grids, _calls = _fake_runner(
        monkeypatch, errors=[0.2, 0.1, 0.05], invalid={(0, False)}
    )
    report = _run(models, grids)
    assert report["steps"][1]["source_grid_delta_from_previous"][
        "within_declared_tolerances"
    ] is False
    assert report["steps"][2]["consecutive_refinement_passes_by_optics"] == {
        "source": 1, "candidate": 2,
    }
    assert report["scene_grid_mean_phase_converged_for_both_optics"] is False


def test_mismatched_phase_source_or_unpaired_grid_fails_closed(monkeypatch):
    models = _models()
    grids, _calls = _fake_runner(
        monkeypatch, errors=[0.2, 0.1, 0.05], wrong_source_identity=True
    )
    with pytest.raises(ValueError, match="different physical inputs"):
        _run(models, grids)
    with pytest.raises(ValueError, match="double both axes"):
        _run(models, [(64, 64), (128, 128), (256, 254)])


def test_single_grid_and_grid_override_are_rejected():
    models = _models()
    with pytest.raises(ValueError, match="3..5"):
        _run(models, [(64, 64)])
    with pytest.raises(ValueError, match="without grid/refinement"):
        run_joint_scene_dense_psf_ladder(
            *models, scene_grid_ladder_row_col=[(64, 64), (128, 128), (256, 256)],
            runner_kwargs={"max_refinements": 0},
            mean_delta_tolerance=0.01, shift_delta_tolerance=0.01,
        )

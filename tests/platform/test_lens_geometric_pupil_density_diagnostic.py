"""Selected-node geometric pupil-density sensitivity without physical claims."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from camera_platform import lens_geometric_pupil_density_diagnostic as density
from camera_platform.lens_geometric_psf_rehistogram import _histogram, _pupil_points


def _point(location):
    x, y = np.asarray(location, dtype=float)
    return np.array([0.025 * x + 0.003 * x * y, 0.02 * y], dtype=float)


def _node():
    pupil = _pupil_points(5)
    points = np.asarray([_point(location) for location in pupil])
    histogram, retained = _histogram(points, 101, 0.001, min_retained=0.999)
    assert retained == pytest.approx(1.0)
    return density._PinnedNode(
        revision_id="pinned-revision", simulation_id="sim", lens_db_sha256="a" * 64,
        source_psf_asset_sha256="b" * 64,
        source_prescription_sha256="c" * 64,
        builder_generator_sha256="d" * 64,
        model=object(), field=object(), field_index=2, field_height_mm=3.0,
        wavelength_nm=550.0, source_pupil_samples=5, source_pupil=pupil,
        source_success_mask=np.ones(len(pupil), dtype=bool),
        source_success_count=len(pupil), saved_chief_mm_xy=np.zeros(2),
        source_histogram=histogram.astype(np.float32), source_pitch_mm=0.001,
        minimum_retained_fraction=0.999,
    )


def _run(monkeypatch, *, node=None, trace=None, **overrides):
    source = _node() if node is None else node
    monkeypatch.setattr(density, "_load_pinned_node", lambda *_args: source)
    monkeypatch.setattr(
        density, "_trace", lambda _model, pupil, _field, _wave: (
            _point(pupil) if trace is None else trace(pupil)
        ),
    )
    kwargs = {
        "catalog_root": ".",
        "field_index": 2,
        "wavelength_nm": 550.0,
        "higher_pupil_samples": [7, 9, 11],
        "target_frequency_cycles_per_mm_row_col": [[5.0, 10.0], [5.0, 10.0]],
        "maximum_complex_otf_delta": 0.1,
    }
    kwargs.update(overrides)
    return density.diagnose_pinned_geometric_pupil_density("pinned-revision", **kwargs)


def test_two_independent_pupil_density_refinements_stay_model_only(monkeypatch):
    result = _run(monkeypatch)
    assert result["status"] == (
        "dense_geometric_pupil_reference_stable_at_declared_targets_model_only"
    )
    assert result["required_consecutive_refinement_passes"] == 2
    assert [step["pupil_samples"] for step in result["steps"]] == [5, 7, 9, 11]
    assert [step["consecutive_passes"] for step in result["steps"]] == [0, 0, 1, 2]
    assert result["steps"][1]["comparison_role"] == (
        "source_to_first_higher_grid_baseline"
    )
    assert result["pinned_source_pupil_density_adequate_at_declared_targets"] is True
    assert result["pinned_source_pupil_vs_stable_dense_ray_reference"][
        "dense_reference_pupil_samples"
    ] == 11
    assert result["steps"][0]["source_histogram_reproduced"] is True
    assert len({step["pupil_coordinates_sha256"] for step in result["steps"]}) == 4
    assert result["psf_histogram_interpolation_used_for_otf"] is False
    assert result["diffraction_included"] is False
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["saved_psf_histogram_sampling_qualified"] is False


def test_source_to_first_higher_grid_does_not_count_as_dense_refinement(monkeypatch):
    result = _run(monkeypatch, higher_pupil_samples=[7, 9])
    assert result["status"] == "inconclusive_refinement_limit"
    assert result["steps"][-1]["consecutive_passes"] == 1
    assert result["pinned_source_pupil_density_adequate_at_declared_targets"] is None
    assert result["pinned_source_pupil_vs_stable_dense_ray_reference"] is None


def test_stable_dense_reference_can_expose_inadequate_pinned_source(monkeypatch):
    frequencies = [[5.0, 10.0], [5.0, 10.0]]
    pupil_counts = {len(_pupil_points(level)): level for level in (5, 7, 9, 11)}
    values = {5: 0.5, 7: 0.6, 9: 0.602, 11: 0.603}

    def ray_otf(points, targets):
        value = values[pupil_counts[len(points)]]
        return tuple(np.full(len(axis), value + 0j) for axis in targets)

    monkeypatch.setattr(density, "_direct_ray_axis_otf", ray_otf)
    result = _run(
        monkeypatch, maximum_complex_otf_delta=0.01,
        target_frequency_cycles_per_mm_row_col=frequencies,
    )
    assert result["status"] == (
        "dense_geometric_pupil_reference_stable_at_declared_targets_model_only"
    )
    assert result["pinned_source_pupil_density_adequacy_status"] == (
        "inadequate_at_declared_targets_geometric_model_only"
    )
    assert result["pinned_source_pupil_density_adequate_at_declared_targets"] is False
    comparison = result["pinned_source_pupil_vs_stable_dense_ray_reference"]
    assert comparison["maximum_complex_otf_delta"] == pytest.approx(0.103)
    assert comparison["admitted_pupil_fraction_delta"] == pytest.approx(0.0)


def test_source_adequacy_not_evaluated_until_dense_reference_stable(monkeypatch):
    pupil_counts = {len(_pupil_points(level)): level for level in (5, 7, 9, 11)}
    values = {5: 0.5, 7: 0.6, 9: 0.7, 11: 0.8}

    def ray_otf(points, targets):
        value = values[pupil_counts[len(points)]]
        return tuple(np.full(len(axis), value + 0j) for axis in targets)

    monkeypatch.setattr(density, "_direct_ray_axis_otf", ray_otf)
    result = _run(monkeypatch, maximum_complex_otf_delta=0.01)
    assert result["status"] == "inconclusive_refinement_limit"
    assert result["pinned_source_pupil_density_adequacy_status"] == (
        "not_evaluated_without_stable_dense_reference"
    )
    assert result["pinned_source_pupil_density_adequate_at_declared_targets"] is None
    assert result["pinned_source_pupil_vs_stable_dense_ray_reference"] is None


def test_one_stable_step_does_not_claim_density_stability(monkeypatch):
    result = _run(
        monkeypatch, maximum_complex_otf_delta=0.001,
        target_frequency_cycles_per_mm_row_col=[[100.0, 500.0], [100.0, 500.0]],
    )
    assert result["status"] == "inconclusive_refinement_limit"
    assert len(result["steps"]) == 4
    assert result["steps"][-1]["consecutive_passes"] < 2


def test_ray_budget_refuses_before_any_trace(monkeypatch):
    node = _node()
    monkeypatch.setattr(density, "_load_pinned_node", lambda *_args: node)

    def no_trace(*_args):
        raise AssertionError("Ray budget must be checked before tracing")

    monkeypatch.setattr(density, "_trace", no_trace)
    result = density.diagnose_pinned_geometric_pupil_density(
        "pinned-revision", catalog_root=".", field_index=2, wavelength_nm=550.0,
        higher_pupil_samples=[7, 9],
        target_frequency_cycles_per_mm_row_col=[[100.0], [100.0]],
        max_total_requested_pupil_rays=30,
    )
    assert result["status"] == "inconclusive_pupil_ray_budget"
    assert result["steps"] == []


def test_pinned_source_must_reproduce_its_saved_histogram(monkeypatch):
    node = _node()
    bad = node.source_histogram.copy()
    bad[50, 50] += 0.01
    with pytest.raises(ValueError, match="does not reproduce saved PSF node"):
        _run(monkeypatch, node=replace(node, source_histogram=bad))


def test_denser_pupil_must_remain_in_same_physical_window(monkeypatch):
    def changed_trace(location):
        x, _y = np.asarray(location, dtype=float)
        if np.isclose(x, 1.0 / 3.0, rtol=0, atol=1e-12):
            return np.array([0.2, 0.2])
        return _point(location)

    result = _run(monkeypatch, trace=changed_trace)
    assert result["status"] == "inconclusive_fixed_physical_support"
    assert result["steps"][-1]["fixed_support_retained_ray_fraction"] < 0.999


def test_invalid_density_and_frequency_requests_fail_closed(monkeypatch):
    with pytest.raises(ValueError, match="higher pupil grids"):
        _run(monkeypatch, higher_pupil_samples=[7, 7])
    with pytest.raises(ValueError, match="frequencies"):
        _run(monkeypatch, target_frequency_cycles_per_mm_row_col=[[100.0, 100.0], [200.0]])

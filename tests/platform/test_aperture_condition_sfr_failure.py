"""Real low-light edge-estimator failure must not discard valid sensor capture."""

from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_metrics as metrics


def test_actual_lowlight_edge_returns_unavailable_without_fabricated_sfr():
    path = Path(__file__).parent / "fixtures" / "aperture-lowlight-invalid-edge.npy"
    mono = np.load(path, allow_pickle=False)
    assert mono.shape == (40, 40)
    assert np.isfinite(mono).all()
    result = metrics.slanted_edge_metrics(mono)
    assert result["status"] == "unavailable"
    assert result["reason"] == "edge_estimator_failed"
    assert result["estimator_error_type"] == "ValueError"
    assert "broadcast" in result["estimator_error"]
    assert "mtf50_cycles_per_pixel" not in result
    assert "sfr" not in result


def test_numeric_estimator_failure_is_local_to_measurement(monkeypatch):
    rows, cols = np.indices((40, 40))
    edge = np.where(cols > 20 + .1 * (rows - 20), .8, .2)

    def fail(*args, **kwargs):
        raise FloatingPointError("invalid LSF centroid")

    monkeypatch.setattr(metrics, "iso12233", fail)
    result = metrics.resolution_metrics(edge, edge, rois={"center": [0, 40, 0, 40]})
    assert result["sites"]["center"]["processed_system"]["status"] == "unavailable"
    assert result["sites"]["center"]["linear_system"]["reason"] == "edge_estimator_failed"


def test_programming_or_invalid_input_errors_are_not_hidden(monkeypatch):
    with pytest.raises(ValueError):
        metrics.slanted_edge_metrics(np.full((40, 40), np.nan))
    rows, cols = np.indices((40, 40))
    edge = np.where(cols > 20 + .1 * (rows - 20), .8, .2)

    def programming_error(*args, **kwargs):
        raise TypeError("unexpected argument contract")

    monkeypatch.setattr(metrics, "iso12233", programming_error)
    with pytest.raises(TypeError, match="argument contract"):
        metrics.slanted_edge_metrics(edge)


def test_supported_edge_still_uses_real_sfr():
    rows, cols = np.indices((80, 80))
    edge = .2 + .6 / (1 + np.exp(-(cols - 40 - .15 * (rows - 40))))
    result = metrics.slanted_edge_metrics(edge)
    assert result["status"] == "ok"
    assert 0 < result["mtf50_cycles_per_pixel"] < .5

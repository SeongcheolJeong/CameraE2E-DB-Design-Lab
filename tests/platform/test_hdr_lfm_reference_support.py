"""Unregistered measured reference must never inherit a model pixel mask."""

from __future__ import annotations

import io

import numpy as np
import pytest

from camera_platform.hdr_lfm_reference_job import _load_array, _phase_rate_means


def test_reference_phase_mean_uses_its_whole_unregistered_support():
    model = np.full((4, 2, 2), 10.0)
    valid = np.zeros((4, 2, 2), dtype=bool)
    valid[:, 0, 0] = True
    reference = np.zeros((4, 2, 2), dtype=np.float64)
    reference[:, 1, 1] = 100.0  # disjoint from the model-valid pixel
    result = _phase_rate_means(model, valid, reference)
    assert result[0]["model_mean_rate_on_model_valid_support"] == 10.0
    assert result[0]["reference_mean_rate_on_whole_phase"] == 25.0
    assert result[0]["support_relation"] == (
        "unregistered_different_supports_no_pixelwise_error"
    )


def test_no_model_valid_support_preserves_unknown_model_rate():
    model = np.full((4, 2, 2), np.nan)
    valid = np.zeros((4, 2, 2), dtype=bool)
    reference = np.full((4, 2, 2), 5.0)
    result = _phase_rate_means(model, valid, reference)
    assert result[2]["model_mean_rate_on_model_valid_support"] is None
    assert result[2]["reference_mean_rate_on_whole_phase"] == 5.0
    with pytest.raises(ValueError, match="four-phase"):
        _phase_rate_means(model[:3], valid[:3], reference[:3])


def test_npy_intake_rejects_truncated_or_wrong_shape_before_loading():
    stream = io.BytesIO()
    np.save(stream, np.ones((2, 4, 2, 2), dtype=np.int32), allow_pickle=False)
    valid_bytes = stream.getvalue()
    np.testing.assert_array_equal(
        _load_array(valid_bytes, [2, 4, 2, 2], "int32", "RAW"),
        np.ones((2, 4, 2, 2), dtype=np.int32),
    )
    with pytest.raises(ValueError, match="cannot be decoded"):
        _load_array(valid_bytes[:-4], [2, 4, 2, 2], "int32", "RAW")
    with pytest.raises(ValueError, match="cannot be decoded"):
        _load_array(valid_bytes, [2, 4, 100_000, 100_000], "int32", "RAW")

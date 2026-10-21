"""Source-addressed carrier ledgers remain full size with temporal receiver crops."""

from copy import deepcopy

import numpy as np
import pytest
from test_charge_collection_runtime import GENERATION, _collection, _image, _reference, _request
from test_native_temporal import native_request as native_request

from camera_platform.dcg_runtime import SharedChargeDCGRuntime
from camera_platform.hdr_runtime import HDRBracketRuntime
from camera_platform.temporal import TemporalSceneRuntime


@pytest.mark.parametrize("mode", ["temporal", "hdr", "dcg"])
def test_full_source_collection_crop_preserves_temporal_evidence_geometry(native_request, mode):
    request = _request(native_request, mode=mode)
    capture = {"crop_origin": [0, 1], "shape": [8, 8]}
    for knot in request["scene_sequence"]["knots"]:
        knot["scene"]["sampling"]["capture_crop"] = capture
    runtime = {
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    assert runtime.validate_request(request) == []
    resources = runtime.estimate_resources(request)
    result = runtime.execute(request)
    arrays, summary = result["arrays"], result["summary"]
    expected = _reference(_image(), GENERATION, _collection())
    np.testing.assert_allclose(
        arrays["collection_generated_rate_scenes"][0], expected["generated"],
        rtol=4e-13, atol=1e-11,
    )
    np.testing.assert_allclose(
        arrays["collection_intrinsic_loss_rate_scenes"][0], expected["intrinsic_loss"],
        rtol=4e-13, atol=1e-11,
    )
    np.testing.assert_allclose(
        arrays["collection_off_sensor_loss_rate_scenes"][0], expected["off_sensor_loss"],
        rtol=4e-13, atol=1e-11,
    )
    np.testing.assert_allclose(
        arrays["source_support_collected_photoelectron_rate_scenes"][0],
        expected["collected"], rtol=4e-13, atol=1e-11,
    )
    np.testing.assert_allclose(
        arrays["mean_electrons"],
        np.array([.002, .004])[:, None, None] * expected["collected"][:, 1:9],
        rtol=4e-13, atol=1e-11,
    )
    assert summary["source_support"]["shape"] == [8, 10]
    assert summary["capture_crop"]["crop_origin"] == [0, 1]
    assert summary["array_geometry"]["collection_generated_rate_scenes"] == "source_support"
    assert (summary["array_geometry"]["source_support_collected_photoelectron_rate_scenes"]
            == "source_support")
    assert summary["array_geometry"]["channel_photoelectron_rate_scenes"] == "capture_crop"
    assert summary["output_size"] == [8, 8]
    full_request = deepcopy(request)
    for knot in full_request["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].pop("capture_crop")
    full_arrays = runtime.execute(full_request)["arrays"]
    raw_key = "raw_groups" if mode == "dcg" else "raw_frames"
    np.testing.assert_array_equal(arrays[raw_key], full_arrays[raw_key][..., :, 1:9])
    assert resources["working_array_bytes_estimate"] >= sum(
        array.nbytes for array in arrays.values()
    )


@pytest.mark.parametrize("mode", ["temporal", "hdr", "dcg"])
def test_partial_source_carrier_halo_matches_full_native_raw(native_request, mode):
    request = _request(native_request, mode=mode)
    rows, cols = np.indices((10, 12))
    source_image = 1e-4 * np.stack([.25 + cols / 12, 1.5 - rows / 12], axis=-1)
    capture = {"crop_origin": [1, 1], "shape": [8, 8]}
    for knot in request["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = source_image.copy()
        knot["scene"]["sampling"].update(
            full_sensor_shape=[12, 14], crop_origin=[0, 0], capture_crop=capture
        )
    runtime = {
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    assert runtime.validate_request(request) == []
    partial = runtime.execute(request)
    arrays, summary = partial["arrays"], partial["summary"]
    assert arrays["collection_outside_support_collected_rate_scenes"].shape == (1, 10, 12)
    assert arrays["collection_outside_support_collected_rate_scenes"].sum() > 0
    assert summary["array_geometry"]["collection_outside_support_collected_rate_scenes"] == (
        "source_support"
    )
    assert summary["source_support"]["shape"] == [10, 12]
    full_request = deepcopy(request)
    full_image = np.zeros((12, 14, 2))
    full_image[:10, :12] = source_image
    for knot in full_request["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = full_image.copy()
        knot["scene"]["sampling"].pop("capture_crop")
    full_arrays = runtime.execute(full_request)["arrays"]
    raw_key = "raw_groups" if mode == "dcg" else "raw_frames"
    np.testing.assert_array_equal(arrays[raw_key], full_arrays[raw_key][..., 1:9, 1:9])
    np.testing.assert_allclose(
        arrays["source_support_collected_photoelectron_rate_scenes"][0, 1:9, 1:9],
        full_arrays["channel_photoelectron_rate_scenes"][0, 1:9, 1:9].sum(axis=-1),
        rtol=4e-13, atol=1e-10,
    )

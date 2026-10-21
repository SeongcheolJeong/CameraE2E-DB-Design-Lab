"""Nonlinear RAW code comparisons retain the exact program and observation meaning."""

from copy import deepcopy

import pytest
from test_dcg_validation import fixture as paired_fixture
from test_temporal_validation import fixture as sequence_fixture

from camera_platform.validation import compare


def samples(paired):
    prediction, reference, spec = paired_fixture() if paired else sequence_fixture()
    method = ("sampled_nonlinear_shared_charge_raw_black_relative_v1" if paired
              else "sampled_nonlinear_raw_black_relative_v1")
    if paired:
        stimulus = spec["stimulus"]
        stimulus["schema"] = "shared_charge_dcg_test_v2"
        contract = stimulus["sample_contract"]
        contract.update(schema="camera_shared_charge_samples_v2", observation=method)
        for fmt in contract["raw_formats"]:
            fmt["response_type"] = "nonlinear_charge_voltage"
    else:
        contract = {
            "schema": "camera_nonlinear_raw_samples_v1", "capture_program_id": "program:v2",
            "capture_program_hash": "c" * 64,
            "raw_format": {"cfa": "RGGB", "bit_depth": 12, "black_level_dn": 64,
                           "response_type": "nonlinear_charge_voltage", "packing": "unpacked",
                           "layout": "bayer", "hdr_streams": 1},
            "sampling": {"kind": "native_sensor", "full_sensor_shape": [16, 16],
                         "crop_origin": [0, 0], "pixel_pitch_um": 3.},
            "raster_shape": [16, 16], "observation": method, "channels": ["R", "G", "B"],
        }
        stimulus = {"schema": "electrical_hdr_test_v2", "sample_contract": contract,
                    "capture_program_id": "program:v2", "capture_program_hash": "c" * 64}
        spec["stimulus"] = stimulus
    stimulus.update(observation_method=method, dark_correction="not_applied_to_raw_code_metric")
    for record in (prediction, reference):
        record["sample_geometry"] = deepcopy(contract)
        record["method_and_version"] = method
        if paired:
            record["raw_formats"] = deepcopy(contract["raw_formats"])
    prediction["uncertainty"] = {"kind": "unknown"}
    return prediction, reference, spec


@pytest.mark.parametrize("paired", [False, True])
def test_versioned_raw_comparison_does_not_need_unrepresentable_measurement_fields(paired):
    data = samples(paired)
    assert "dark_correction" not in data[1] and "raw_format" not in data[1]
    result = compare(*data)
    assert result["status"] == "INCONCLUSIVE", result["issues"]
    assert not result["physical_validation_pass"]


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("change", ["linear", "old_method", "other_program", "crop", "shape"])
def test_matching_values_cannot_hide_nonlinear_identity_substitution(paired, change):
    data = samples(paired)
    reference = data[1]
    contract = reference["sample_geometry"]
    if change == "linear":
        fmt = contract["raw_formats"][0] if paired else contract["raw_format"]
        fmt["response_type"] = "linear"
    elif change == "old_method":
        reference["method_and_version"] = "sampled_raw_dark_corrected_v1"
    elif change == "other_program":
        contract["capture_program_hash"] = "b" * 64
    elif change == "crop":
        contract["sampling"]["crop_origin"][0] += 1
    else:
        contract["raster_shape"][0] += 2
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON", result["issues"]
    assert not result["physical_validation_pass"]


@pytest.mark.parametrize("paired", [False, True])
def test_mutually_consistent_linear_relabelling_is_invalid(paired):
    data = samples(paired)
    contract = data[2]["stimulus"]["sample_contract"]
    formats = contract["raw_formats"] if paired else [contract["raw_format"]]
    for fmt in formats:
        fmt["response_type"] = "linear"
    for record in data[:2]:
        record["sample_geometry"] = deepcopy(contract)
    assert compare(*data)["status"] == "INVALID_COMPARISON"


@pytest.mark.parametrize("paired", [False, True])
def test_nonlinear_observation_cannot_reuse_a_legacy_test(paired):
    data = samples(paired)
    data[2]["stimulus"]["schema"] = (
        "shared_charge_dcg_test_v1" if paired else "electrical_hdr_test_v1")
    assert compare(*data)["status"] == "INVALID_COMPARISON"

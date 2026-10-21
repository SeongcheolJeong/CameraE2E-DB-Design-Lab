"""Native temporal validation coordinates preserve both independent green sites."""

from copy import deepcopy

import pytest
from test_temporal_validation import codes, fixture

from camera_platform.validation import compare


def native_fixture():
    prediction, reference, spec = fixture()
    for record in (prediction, reference):
        record["axes"][1]["values"] = ["R", "Gr", "Gb", "B"]
        record["values"] = [110, 220, 280, 90, 340, 640, 790, 250]
    spec["validation_kind"] = "implementation_verification"
    reference["source_kind"] = "simulated"
    reference.pop("physical_instance")
    return prediction, reference, spec


def test_distinct_four_site_temporal_comparison_preserves_all_eight_samples():
    data = native_fixture()
    original = deepcopy(data)
    result = compare(*data)
    assert result["status"] == "PASS", result["issues"]
    assert result["summary"]["sample_count"] == 8
    assert result["residuals"]["values"] == [0] * 8
    assert result["residuals"]["axes"][1]["values"] == ["R", "Gr", "Gb", "B"]
    assert not result["physical_validation_pass"]
    assert data == original


@pytest.mark.parametrize("target", [0, 1])
def test_rgb_and_native_sites_never_compare_by_collapsing_greens(target):
    data = native_fixture()
    legacy = fixture()[target]
    data[target]["axes"] = legacy["axes"]
    data[target]["values"] = legacy["values"]
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "axis_mismatch" in codes(result)
    assert not result["comparison_valid"] and not result["physical_validation_pass"]


@pytest.mark.parametrize("sites", [["R", "Gb", "Gr", "B"], ["R", "G1", "G2", "B"],
                                  ["R", "Gr", "Gr", "B"]])
def test_mutually_matching_wrong_four_site_axes_are_not_accepted(sites):
    data = native_fixture()
    for record in data[:2]:
        record["axes"][1]["values"] = sites
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "temporal_axis_mismatch" in codes(result)


def test_green_site_swap_is_detected_as_four_residuals_not_hidden_by_average():
    prediction, reference, spec = native_fixture()
    for start in (0, 4):
        reference["values"][start + 1], reference["values"][start + 2] = (
            reference["values"][start + 2], reference["values"][start + 1])
    result = compare(prediction, reference, spec)
    assert result["status"] == "FAIL", result["issues"]
    assert result["summary"]["sample_count"] == 8
    assert sum(value != 0 for value in result["residuals"]["values"]) == 4
    assert not result["physical_validation_pass"]


def test_unknown_native_physics_uncertainty_does_not_become_physical_pass():
    prediction, reference, spec = native_fixture()
    spec["validation_kind"] = "physical_validation"
    prediction["uncertainty"] = {"kind": "unknown"}
    result = compare(prediction, reference, spec)
    assert result["status"] == "INCONCLUSIVE"
    assert not result["physical_validation_pass"]

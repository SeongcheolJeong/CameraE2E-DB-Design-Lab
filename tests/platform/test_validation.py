"""Scientific acceptance gates, including tempting but unsupported PASS claims."""

import json
from copy import deepcopy

import pytest

from camera_platform.validation import compare


def comparison_fixture():
    prediction = {
        "id": "prediction:1",
        "metric_id": "raw.channel_ratio",
        "unit": "1",
        "values": [1.0, 0.8, 0.9, 0.7],
        "axes": [
            {"name": "field", "unit": "1", "values": [0.0, 1.0]},
            {"name": "channel", "unit": "1", "values": ["R/G", "B/G"]},
        ],
        "conditions": {
            "temperature_c": {"value": 25.0, "unit": "degC"},
            "illuminant": {"value": "D65", "unit": "1"},
        },
        "reference_plane": "sensor.raw",
        "included_effects": ["cfa", "lens_shading"],
        "valid_domain": {"temperature_c": {"min": 0, "max": 50, "unit": "degC"}},
        "uncertainty": {"kind": "absolute_bound", "value": 0.01, "unit": "1"},
        "source_kind": "simulated",
        "method_and_version": "response-runtime:1",
        "calibration_dataset_ids": ["calibration:camera-A"],
    }
    measurement = deepcopy(prediction)
    measurement.update(
        {
            "id": "measurement:1",
            "source_kind": "measured",
            "method_and_version": "raw-chart-analysis:1",
            "physical_instance": {"serial": "camera-B"},
            "dataset_ids": ["independent:camera-B"],
        }
    )
    spec = {
        key: deepcopy(prediction[key])
        for key in ("metric_id", "unit", "conditions", "reference_plane", "included_effects")
    }
    spec.update(
        {
            "id": "test:1",
            "validation_kind": "physical_validation",
            "absolute_tolerance": {"value": 0.05, "unit": "1"},
        }
    )
    return prediction, measurement, spec


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_independent_physical_pass_requires_complete_evidence():
    report = compare(*comparison_fixture())
    assert report["status"] == "PASS", report["issues"]
    assert report["physical_validation_pass"]
    assert report["independent_validation"]
    assert report["summary"]["sample_count"] == 4
    assert report["residuals"]["shape"] == [4]
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("source", ["synthetic", "simulated", "estimated", "vendor"])
def test_synthetic_or_vendor_reference_cannot_pass_physical_validation(source):
    prediction, measurement, spec = comparison_fixture()
    measurement["source_kind"] = source
    report = compare(prediction, measurement, spec)
    assert report["status"] == "INCONCLUSIVE"
    assert not report["physical_validation_pass"]
    assert "not_physical_measurement" in codes(report)


def test_default_comparison_kind_is_conservatively_physical():
    prediction, measurement, spec = comparison_fixture()
    del spec["validation_kind"]
    measurement["source_kind"] = "synthetic"
    assert compare(prediction, measurement, spec)["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "kind,label",
    [
        ("implementation_verification", "Implementation Verification"),
        ("cross_tool_comparison", "Cross-tool Comparison"),
    ],
)
def test_implementation_and_cross_tool_passes_are_not_physical(kind, label):
    prediction, measurement, spec = comparison_fixture()
    spec["validation_kind"] = kind
    measurement["source_kind"] = "synthetic"
    report = compare(prediction, measurement, spec)
    assert report["status"] == "PASS"
    assert report["validation_label"] == label
    assert not report["physical_validation_pass"]


def test_same_tool_is_not_cross_tool_evidence():
    prediction, measurement, spec = comparison_fixture()
    spec["validation_kind"] = "cross_tool_comparison"
    measurement["method_and_version"] = prediction["method_and_version"]
    assert compare(prediction, measurement, spec)["status"] == "INVALID_COMPARISON"


def test_calibration_dataset_overlap_rejects_independent_claim():
    prediction, measurement, spec = comparison_fixture()
    measurement["dataset_ids"].append("calibration:camera-A")
    report = compare(prediction, measurement, spec)
    assert report["status"] == "INVALID_COMPARISON"
    assert "calibration_dataset_overlap" in codes(report)
    assert not report["independent_validation"]


def test_calibration_lineage_declared_by_test_is_also_checked():
    prediction, measurement, spec = comparison_fixture()
    spec["calibration_dataset_ids"] = measurement["dataset_ids"]
    assert "calibration_dataset_overlap" in codes(compare(prediction, measurement, spec))


@pytest.mark.parametrize(
    "target,field",
    [
        (0, "uncertainty"),
        (1, "uncertainty"),
        (0, "valid_domain"),
        (0, "calibration_dataset_ids"),
        (1, "physical_instance"),
        (1, "dataset_ids"),
        (0, "method_and_version"),
        (0, "reference_plane"),
        (0, "included_effects"),
        (0, "axes"),
        (0, "values"),
        (2, "absolute_tolerance"),
    ],
)
def test_missing_evidence_remains_inconclusive(target, field):
    records = comparison_fixture()
    del records[target][field]
    report = compare(*records)
    assert report["status"] == "INCONCLUSIVE", report["issues"]
    assert not report["physical_validation_pass"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("metric_id", "isp.sfr"),
        ("unit", "%"),
        ("reference_plane", "isp.rgb"),
        ("included_effects", ["cfa"]),
    ],
)
def test_semantic_mismatch_invalidates_numerical_agreement(field, value):
    prediction, measurement, spec = comparison_fixture()
    measurement[field] = value
    assert compare(prediction, measurement, spec)["status"] == "INVALID_COMPARISON"


def test_lens_mtf_is_never_aliased_to_processed_image_sfr():
    prediction, measurement, spec = comparison_fixture()
    prediction["metric_id"] = "lens.optical_mtf"
    measurement["metric_id"] = spec["metric_id"] = "isp.processed_sfr"
    report = compare(prediction, measurement, spec)
    assert "metric_id_mismatch" in codes(report)
    assert report["status"] == "INVALID_COMPARISON"


def test_same_value_different_condition_units_are_not_silently_converted():
    prediction, measurement, spec = comparison_fixture()
    measurement["conditions"]["temperature_c"]["unit"] = "K"
    assert "condition_mismatch" in codes(compare(prediction, measurement, spec))


def test_missing_one_condition_is_unknown_not_equivalent():
    prediction, measurement, spec = comparison_fixture()
    del measurement["conditions"]["illuminant"]
    assert compare(prediction, measurement, spec)["status"] == "INCONCLUSIVE"


def test_axis_reordering_is_not_implicit_alignment():
    prediction, measurement, spec = comparison_fixture()
    measurement["axes"][0]["values"].reverse()
    report = compare(prediction, measurement, spec)
    assert report["status"] == "INVALID_COMPARISON"
    assert "axis_mismatch" in codes(report)


def test_coordinate_count_must_match_flattened_values():
    prediction, measurement, spec = comparison_fixture()
    for record in (prediction, measurement):
        record["axes"][0]["values"].append(2.0)
    assert "axis_shape_mismatch" in codes(compare(prediction, measurement, spec))


def test_domain_extrapolation_is_gated_even_with_zero_residual():
    prediction, measurement, spec = comparison_fixture()
    for record in (prediction, measurement, spec):
        record["conditions"]["temperature_c"]["value"] = 60
    report = compare(prediction, measurement, spec)
    assert report["status"] == "OUT_OF_DOMAIN"
    assert not report["physical_validation_pass"]


def test_missing_domain_coordinate_is_inconclusive():
    prediction, measurement, spec = comparison_fixture()
    prediction["valid_domain"]["exposure_s"] = {"min": 0.001, "max": 0.01, "unit": "s"}
    assert "missing_domain_condition" in codes(compare(prediction, measurement, spec))


def test_declared_axis_points_also_gate_model_validity():
    prediction, measurement, spec = comparison_fixture()
    prediction["valid_domain"]["field"] = {"min": 0, "max": 0.9, "unit": "1"}
    assert compare(prediction, measurement, spec)["status"] == "OUT_OF_DOMAIN"


def test_scalar_condition_cannot_hide_out_of_domain_coordinate():
    prediction, measurement, spec = comparison_fixture()
    for record in (prediction, measurement, spec):
        record["conditions"]["field"] = {"value": 0.5, "unit": "1"}
    prediction["valid_domain"]["field"] = {"min": 0, "max": 0.9, "unit": "1"}
    assert compare(prediction, measurement, spec)["status"] == "OUT_OF_DOMAIN"


def test_explicitly_unknown_uncertainty_remains_inconclusive():
    prediction, measurement, spec = comparison_fixture()
    prediction["uncertainty"] = {"kind": "unknown"}
    assert compare(prediction, measurement, spec)["status"] == "INCONCLUSIVE"


def test_bad_calibration_declaration_never_claims_independence():
    prediction, measurement, spec = comparison_fixture()
    prediction["calibration_dataset_ids"] = "not-a-list"
    report = compare(prediction, measurement, spec)
    assert report["status"] == "INVALID_COMPARISON"
    assert not report["independent_validation"]


@pytest.mark.parametrize(
    "offset,bound,tolerance,expected",
    [
        (0.02, 0.01, 0.05, "PASS"),
        (0.04, 0.01, 0.05, "INCONCLUSIVE"),
        (0.08, 0.01, 0.05, "FAIL"),
        (0.00, 0.20, 0.05, "INCONCLUSIVE"),
    ],
)
def test_bounded_uncertainty_controls_acceptance(offset, bound, tolerance, expected):
    prediction, measurement, spec = comparison_fixture()
    prediction["values"] = [value + offset for value in measurement["values"]]
    for record in (prediction, measurement):
        record["uncertainty"]["value"] = bound
    spec["absolute_tolerance"]["value"] = tolerance
    report = compare(prediction, measurement, spec)
    assert report["status"] == expected
    assert report["summary"]["mae"] == pytest.approx(offset)


def test_one_decisive_failure_fails_the_test():
    prediction, measurement, spec = comparison_fixture()
    prediction["values"][2] += 1
    assert compare(prediction, measurement, spec)["status"] == "FAIL"


@pytest.mark.parametrize("values", [[float("nan")], [float("inf")], [[1], [2, 3]], [], [True]])
def test_nonfinite_or_malformed_values_are_rejected(values):
    prediction, measurement, spec = comparison_fixture()
    prediction["values"] = values
    report = compare(prediction, measurement, spec)
    assert report["status"] == "INVALID_COMPARISON"
    json.dumps(report, allow_nan=False)


def test_equal_flattened_count_with_different_shape_is_not_comparable():
    prediction, measurement, spec = comparison_fixture()
    prediction["values"] = [[1, 0.8], [0.9, 0.7]]
    assert compare(prediction, measurement, spec)["status"] == "INVALID_COMPARISON"


def test_nested_shape_must_agree_with_coordinate_axis_lengths():
    prediction, measurement, spec = comparison_fixture()
    for record in (prediction, measurement):
        record["values"] = [[1], [0.8], [0.9], [0.7]]
    assert "axis_shape_mismatch" in codes(compare(prediction, measurement, spec))


@pytest.mark.parametrize(
    "uncertainty",
    [
        {"kind": "standard_deviation", "value": 0.01, "unit": "1"},
        {"kind": "absolute_bound", "value": -0.1, "unit": "1"},
        {"kind": "absolute_bound", "values": [0.1], "unit": "1"},
        {"kind": "absolute_bound", "value": 0.1, "unit": "%"},
    ],
)
def test_unsupported_or_invalid_uncertainty_is_not_a_bound(uncertainty):
    prediction, measurement, spec = comparison_fixture()
    measurement["uncertainty"] = uncertainty
    assert compare(prediction, measurement, spec)["status"] == "INVALID_COMPARISON"


def test_comparison_does_not_mutate_source_records():
    inputs = comparison_fixture()
    before = deepcopy(inputs)
    compare(*inputs)
    assert inputs == before

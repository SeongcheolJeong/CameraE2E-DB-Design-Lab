"""Recorded-frame comparison identity and model applicability, without physical promotion."""

import json
from copy import deepcopy

import pytest

from camera_platform.validation import compare


def fixture():
    clock = {"clock_id": "clock:camera-A", "unit": "ns", "reference": "relative",
             "origin": "Recorded common trigger; no inferred synchronization"}
    frames = [
        {"frame_id": "f0", "frame_start_ns": 0, "applied_at_ns": -1_000_000,
         "exposure_ns": 4_000_000, "gain": 1, "isp_model_id": "isp:0"},
        {"frame_id": "f1", "frame_start_ns": 10_000_000, "applied_at_ns": 9_000_000,
         "exposure_ns": 8_000_000, "gain": 2, "isp_model_id": "isp:1"},
    ]
    prediction = {
        "id": "prediction:temporal", "metric_id": "raw.channel_mean_dn_sequence", "unit": "DN",
        "values": [110, 220, 90, 340, 640, 250],
        "axes": [{"name": "frame", "unit": "1", "values": ["f0", "f1"]},
                 {"name": "channel", "unit": "1", "values": ["R", "G", "B"]}],
        "conditions": {"temperature_c": {"value": 25, "unit": "degC"},
                       "light_id": {"value": "timeline:light", "unit": "1"}},
        "clock": clock, "frame_conditions": frames,
        "reference_plane": "sensor_raw", "included_effects": ["temporal_exposure_integration"],
        "valid_domain": {"temperature_c": {"min": 0, "max": 50, "unit": "degC"},
                         "exposure_s": {"min": 0.001, "max": 0.010, "unit": "s"},
                         "gain": {"min": 1, "max": 4, "unit": "1"}},
        "uncertainty": {"kind": "absolute_bound", "value": 0.5, "unit": "DN"},
        "source_kind": "simulated", "method_and_version": "temporal:implementation:1",
        "calibration_dataset_ids": ["training:A"],
    }
    measurement = deepcopy(prediction)
    measurement.update(id="measurement:B", source_kind="measured", dataset_ids=["holdout:B"],
                       physical_instance={"serial": "camera:B"}, method_and_version="capture:2")
    spec = {key: deepcopy(prediction[key]) for key in (
        "metric_id", "unit", "conditions", "clock", "frame_conditions", "reference_plane",
        "included_effects")}
    spec.update(id="test:temporal", validation_kind="physical_validation",
                absolute_tolerance={"value": 2, "unit": "DN"},
                required_conditions=["temperature_c", "exposure_s", "gain"])
    return prediction, measurement, spec


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def frame_domains(prediction):
    prediction["frame_valid_domains"] = [
        {"frame_id": frame["frame_id"], "valid_domain": deepcopy(prediction["valid_domain"])}
        for frame in prediction["frame_conditions"]
    ]


def cropped_linear_contract(test, schema="electrical_hdr_test_v1"):
    test["stimulus"] = {
        "schema": schema,
        "capture_crop": {"crop_origin": [12, 14], "shape": [8, 10]},
        "source_support": {
            "kind": "native_sensor", "full_sensor_shape": [64, 64],
            "crop_origin": [8, 10], "shape": [24, 24],
        },
    }
    return {key: deepcopy(test["stimulus"][key])
            for key in ("capture_crop", "source_support")}


@pytest.mark.parametrize("schema", [
    "temporal_scene_test_v1", "electrical_hdr_test_v1",
    "electrical_hdr_components_test_v1",
])
def test_cropped_linear_temporal_comparison_requires_exact_receiver_and_source_geometry(schema):
    prediction, measurement, test = fixture()
    geometry = cropped_linear_contract(test, schema)
    assert compare(prediction, measurement, test)["status"] == "INVALID_COMPARISON"
    prediction["sample_geometry"] = deepcopy(geometry)
    assert compare(prediction, measurement, test)["status"] == "INVALID_COMPARISON"
    measurement["sample_geometry"] = deepcopy(geometry)
    matched = compare(prediction, measurement, test)
    assert matched["status"] == "PASS", matched["issues"]
    for record in (prediction, measurement):
        for part, field in (("capture_crop", "crop_origin"),
                            ("source_support", "crop_origin")):
            changed = deepcopy(record["sample_geometry"])
            changed[part][field][0] += 2
            record["sample_geometry"] = changed
            result = compare(prediction, measurement, test)
            assert result["status"] == "INVALID_COMPARISON"
            assert "linear_crop_geometry_mismatch" in codes(result)
            record["sample_geometry"] = deepcopy(geometry)


def test_cropped_linear_temporal_contract_rejects_missing_or_invalid_source_support():
    prediction, measurement, test = fixture()
    geometry = cropped_linear_contract(test)
    prediction["sample_geometry"] = deepcopy(geometry)
    measurement["sample_geometry"] = deepcopy(geometry)
    test["stimulus"].pop("source_support")
    result = compare(prediction, measurement, test)
    assert result["status"] == "INVALID_COMPARISON"
    assert "invalid_linear_crop_contract" in codes(result)
    test["stimulus"]["source_support"] = deepcopy(geometry["source_support"])
    test["stimulus"]["capture_crop"]["crop_origin"] = [40, 40]
    result = compare(prediction, measurement, test)
    assert result["status"] == "INVALID_COMPARISON"
    assert "invalid_linear_crop_contract" in codes(result)


def test_exact_recorded_frames_with_complete_declared_bounds_can_be_compared():
    data = fixture()
    frozen = deepcopy(data)
    result = compare(*data)
    assert result["status"] == "PASS", result["issues"]
    assert result["physical_validation_pass"]
    assert result["temporal_comparison"]["domain_policy"] == "global_evaluated_per_frame"
    assert result["summary"]["sample_count"] == 6
    assert result["residuals"]["values"] == [0] * 6
    json.dumps(result, allow_nan=False)
    result["temporal_comparison"]["frame_conditions"][0]["gain"] = 99
    assert data == frozen


def test_current_runtime_unknown_uncertainty_never_passes_physical_validation():
    prediction, measurement, test = fixture()
    prediction["uncertainty"] = {"kind": "unknown"}
    frame_domains(prediction)
    result = compare(prediction, measurement, test)
    assert result["status"] == "INCONCLUSIVE"
    assert result["independent_validation"]
    assert not result["physical_validation_pass"]
    assert "missing_uncertainty" in codes(result)


@pytest.mark.parametrize("kind", ["simulated", "synthetic", "estimated", "vendor"])
def test_temporal_synthetic_reference_cannot_become_physical_evidence(kind):
    prediction, measurement, test = fixture()
    measurement["source_kind"] = kind
    result = compare(prediction, measurement, test)
    assert result["status"] == "INCONCLUSIVE"
    assert not result["independent_validation"]
    assert not result["physical_validation_pass"]


@pytest.mark.parametrize("kind", ["implementation_verification", "cross_tool_comparison"])
def test_temporal_numerical_comparison_labels_remain_distinct(kind):
    prediction, measurement, test = fixture()
    test["validation_kind"] = kind
    measurement["source_kind"] = "simulated"
    result = compare(prediction, measurement, test)
    assert result["status"] == "PASS", result["issues"]
    assert not result["physical_validation_pass"]


@pytest.mark.parametrize("target", [0, 1, 2])
@pytest.mark.parametrize("field", ["clock", "frame_conditions"])
def test_missing_temporal_metadata_is_invalid_not_assumed_from_another_record(target, field):
    data = fixture()
    del data[target][field]
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert not result["physical_validation_pass"]


@pytest.mark.parametrize("field,value", [
    ("applied_at_ns", 8_000_000),
    ("frame_start_ns", 11_000_000),
    ("exposure_ns", 7_000_000),
    ("gain", 2.1),
    ("isp_model_id", "isp:changed"),
    ("frame_id", "different-frame"),
])
@pytest.mark.parametrize("target", [1, 2])
def test_one_changed_recorded_setting_invalidates_identical_metric_values(target, field, value):
    data = fixture()
    data[target]["frame_conditions"][1][field] = value
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "frame_conditions_mismatch" in codes(result)


@pytest.mark.parametrize("key,value", [("clock_id", "another"), ("origin", "another origin")])
def test_clock_identity_mismatch_has_no_implicit_alignment(key, value):
    data = fixture()
    data[1]["clock"][key] = value
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "temporal_clock_mismatch" in codes(result)


def test_identically_wrong_units_or_frame_order_cannot_pass_by_mutual_agreement():
    data = fixture()
    for item in data:
        item["clock"]["unit"] = "us"
        item["frame_conditions"].reverse()
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert {"invalid_temporal_clock", "invalid_frame_order"} <= codes(result)


@pytest.mark.parametrize("mutate", [
    lambda f: f.pop("applied_at_ns"),
    lambda f: f.pop("exposure_ns"),
    lambda f: f.update(exposure_ns=4.0),
    lambda f: f.update(exposure_ns=0),
    lambda f: f.update(exposure_ns=10**1000),
    lambda f: f.update(frame_start_ns=True),
    lambda f: f.update(frame_start_ns=2**53),
    lambda f: f.update(applied_at_ns=float("nan")),
    lambda f: f.update(gain=float("inf")),
    lambda f: f.update(gain=10**1000),
    lambda f: f.update(gain=False),
    lambda f: f.update(gain=0),
    lambda f: f.update(isp_model_id=" "),
    lambda f: f.update(exposure_s=0.004),
    lambda f: f.update(gain={"value": 1, "unit": "1"}),
])
def test_malformed_flat_frame_contract_is_refused(mutate):
    data = fixture()
    mutate(data[1]["frame_conditions"][0])
    assert compare(*data)["status"] == "INVALID_COMPARISON"


def test_duplicate_frame_ids_and_conflicting_applied_event_are_invalid():
    data = fixture()
    for record in data:
        record["frame_conditions"][1]["frame_id"] = "f0"
        record["frame_conditions"][1]["applied_at_ns"] = -1_000_000
    result = compare(*data)
    assert {"duplicate_frame_id", "conflicting_applied_event"} <= codes(result)
    assert result["status"] == "INVALID_COMPARISON"


@pytest.mark.parametrize("mutate", [
    lambda a: a[0].update(unit="ns"),
    lambda a: a[0].update(values=["f1", "f0"]),
    lambda a: a[0].update(values=[0, 1]),
    lambda a: a[0].update(name="sequence"),
    lambda a: a[1].update(values=["B", "G", "R"]),
    lambda a: a[1].update(values=["R/G", "B/G"]),
    lambda a: a.reverse(),
])
def test_axes_must_match_declared_order_and_channels_even_when_both_arrays_match(mutate):
    data = fixture()
    mutate(data[0]["axes"])
    data[1]["axes"] = deepcopy(data[0]["axes"])
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "temporal_axis_mismatch" in codes(result)


@pytest.mark.parametrize("key,quantity", [
    ("exposure_s", {"value": 0.004, "unit": "s"}),
    ("exposure_ns", {"value": 4_000_000, "unit": "ns"}),
    ("gain", {"value": 1, "unit": "1"}),
])
def test_global_settings_cannot_override_recorded_frame_conditions(key, quantity):
    data = fixture()
    for record in data:
        record["conditions"][key] = quantity
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "global_temporal_setting" in codes(result)


def test_global_domain_fallback_is_evaluated_at_every_applied_exposure_and_gain():
    prediction, measurement, test = fixture()
    prediction["valid_domain"]["exposure_s"]["max"] = 0.005
    result = compare(prediction, measurement, test)
    assert result["status"] == "OUT_OF_DOMAIN"
    outside = [issue for issue in result["issues"] if issue["code"] == "outside_valid_domain"]
    assert [item["path"] for item in outside] == [
        "prediction.frame_valid_domains[1].valid_domain.exposure_s"
    ]
    prediction["valid_domain"]["exposure_s"]["max"] = 0.010
    prediction["valid_domain"]["gain"]["max"] = 1.5
    result = compare(prediction, measurement, test)
    assert result["status"] == "OUT_OF_DOMAIN"
    assert any(issue["path"].endswith("[1].valid_domain.gain") for issue in result["issues"])


def test_per_frame_model_domains_replace_first_frame_intersection_and_gate_correct_isp():
    prediction, measurement, test = fixture()
    frame_domains(prediction)
    prediction["valid_domain"]["exposure_s"]["max"] = 0.004  # Legacy first-frame intersection.
    first = prediction["frame_valid_domains"][0]["valid_domain"]
    second = prediction["frame_valid_domains"][1]["valid_domain"]
    first["exposure_s"].update(min=0.004, max=0.004)
    first["gain"].update(min=1, max=1)
    second["exposure_s"].update(min=0.008, max=0.008)
    second["gain"].update(min=2, max=2)
    result = compare(prediction, measurement, test)
    assert result["status"] == "PASS", result["issues"]
    assert result["temporal_comparison"]["domain_policy"] == "per_frame"
    second["gain"]["max"] = 1.5
    second["gain"]["min"] = 1
    assert compare(prediction, measurement, test)["status"] == "OUT_OF_DOMAIN"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(frame_valid_domains=None),
    lambda p: p.update(frame_valid_domains=[]),
    lambda p: p["frame_valid_domains"].pop(),
    lambda p: p["frame_valid_domains"].reverse(),
    lambda p: p["frame_valid_domains"][1].update(frame_id="f0"),
    lambda p: p["frame_valid_domains"][1].pop("valid_domain"),
    lambda p: p["frame_valid_domains"][1].update(extra="ignored?"),
])
def test_incomplete_declared_per_frame_domains_never_fall_back_to_permissive_global(mutate):
    data = fixture()
    frame_domains(data[0])
    mutate(data[0])
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "invalid_frame_valid_domains" in codes(result)


def test_unknown_frame_domain_is_inconclusive_and_bad_units_are_invalid():
    data = fixture()
    frame_domains(data[0])
    data[0]["frame_valid_domains"][1]["valid_domain"] = {}
    result = compare(*data)
    assert result["status"] == "INCONCLUSIVE"
    assert "missing_valid_domain" in codes(result)
    data[0]["frame_valid_domains"][1]["valid_domain"] = {
        "exposure_s": {"min": 0, "max": 100, "unit": "ms"}
    }
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "domain_unit_mismatch" in codes(result)


def test_static_optical_condition_is_merged_and_checked_for_every_frame():
    data = fixture()
    frame_domains(data[0])
    data[0]["frame_valid_domains"][1]["valid_domain"]["temperature_c"]["max"] = 20
    result = compare(*data)
    assert result["status"] == "OUT_OF_DOMAIN"
    assert any(issue["path"].endswith("[1].valid_domain.temperature_c")
               for issue in result["issues"])


def test_temporal_training_data_remains_disallowed_as_independent_holdout():
    data = fixture()
    data[1]["dataset_ids"].append("training:A")
    result = compare(*data)
    assert result["status"] == "INVALID_COMPARISON"
    assert "calibration_dataset_overlap" in codes(result)
    assert not result["independent_validation"]


def test_nontemporal_null_default_fields_preserve_existing_comparison_behavior():
    prediction, measurement, test = fixture()
    for record in (prediction, measurement, test):
        record.update(metric_id="raw.channel_mean_dn", clock=None, frame_conditions=None)
        record["conditions"]["exposure_s"] = {"value": 0.004, "unit": "s"}
        record["conditions"]["gain"] = {"value": 1, "unit": "1"}
    assert compare(prediction, measurement, test)["status"] == "PASS"

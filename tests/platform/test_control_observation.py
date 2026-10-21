"""Independent RAW arithmetic, incomplete sequences and response comparison semantics."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform.control_observation import (
    ResponseSpecification,
    analyze,
    method,
    metric,
    metric_fields,
)
from camera_platform.controls import default_policy
from camera_platform.validation import compare


def fixture(observable="green_mean_normalized", reduction="trajectory", **options):
    clock = {"clock_id": "test", "unit": "ns", "reference": "relative", "origin": "fixture trigger"}
    spec = ResponseSpecification.model_validate(
        {
            "observable": observable,
            "reduction": reduction,
            "raw_format": {
                "cfa": "RGGB",
                "bit_depth": 8,
                "black_level_dn": 16.0,
                "packing": "unpacked",
                "layout": "bayer",
                "response_type": "linear",
                "hdr_streams": 1,
            },
            "geometry": {
                "kind": "native_sensor",
                "full_sensor_shape": [6, 6],
                "crop_origin": [1, 1],
            },
            "frame_shape": [4, 4],
            "statistics_policy": default_policy([4, 4]),
            "mode": {
                "shutter": "global",
                "line_time_ns": 0,
                "hidden_top_rows": 0,
                "readout_basis": "full_sensor_rows",
            },
            "maximum_frame_alignment_error_ns": 0,
            **options,
        }
    ).model_dump()
    test = {
        "id": "test-1",
        "validation_kind": "implementation_verification",
        "metric_id": metric(spec)[0],
        "unit": metric(spec)[1],
        "reference_plane": "sensor_raw",
        "observation_method": method(spec),
        "observation_specification": spec,
        "observation_shape": [4, 4],
        "clock": clock,
        "frame_conditions": None,
        "included_effects": ["native_raw"],
        "conditions": {"temperature_c": {"value": 20, "unit": "C"}},
        "required_conditions": ["temperature_c"],
        "calibration_dataset_ids": [],
        "absolute_tolerance": {"value": 0.01, "unit": metric(spec)[1]},
        "stimulus": {
            "schema": "closed_loop_scene_test_v1",
            "control_program_hash": "a" * 64,
            "control_policy_hash": "b" * 64,
            "scene_sequence_hash": "c" * 64,
            "planned_frames": [
                {"frame_id": f"f{i}", "frame_start_ns": i * 10_000_000} for i in range(4)
            ],
        },
    }
    acquisition = {
        "clock": deepcopy(clock),
        "test_clock_offset_ns": 0,
        "alignment_absolute_bound_ns": 0,
        "alignment_source_uri": "fixture://clock",
        "alignment_method_and_version": "exact:1",
        "program_hash": "a" * 64,
        "policy_hash": "b" * 64,
        "scene_hash": "c" * 64,
        "execution_source_uri": "fixture://independent-formula",
        "execution_method_and_version": "fixture:1",
        "frames": [
            {
                "frame_id": f"f{i}",
                "raw_index": i,
                "frame_start_ns": i * 10_000_000,
                "latch_at_ns": i * 10_000_000,
                "applied": {
                    "exposure_ns": 1_000_000,
                    "gain": 1.0,
                    "wb_gains_rgb": [2.0, 1.0, 0.5],
                    "isp_model_id": "isp-1",
                },
            }
            for i in range(4)
        ],
        "commands": [],
    }
    # Odd native origin: B G / G R. Codes must retain full-sensor CFA phase.
    raw = np.tile(np.array([[128, 96], [96, 64]], dtype=np.uint16), (4, 2, 2))
    return raw, test, acquisition


def record(raw, test, acquisition, *, identity="prediction", bound=0.0):
    result = metric_fields(analyze(raw, test, acquisition))
    result.update(
        id=identity,
        source_kind="simulated",
        method_and_version="fixture:1",
        dataset_ids=[identity],
        calibration_dataset_ids=[],
        included_effects=test["included_effects"],
        conditions=test["conditions"],
        uncertainty={"kind": "absolute_bound", "value": bound, "unit": test["unit"]},
        valid_domain={
            "temperature_c": {"min": 0, "max": 40, "unit": "C"},
            "exposure_s": {"min": 0.00001, "max": 0.008, "unit": "s"},
            "gain": {"min": 1, "max": 8, "unit": "1"},
        },
        acquisition_mode="closed_loop",
    )
    return result


@pytest.mark.parametrize(
    "observable,expected",
    [
        ("green_mean_normalized", 80 / 239),
        ("wb_adjusted_rg_ratio", 1.2),
        ("wb_adjusted_bg_ratio", 0.7),
        ("exposure_ns", 1_000_000),
        ("gain", 1.0),
        ("wb_red_gain", 2.0),
        ("wb_blue_gain", 0.5),
    ],
)
def test_actual_codes_native_phase_and_applied_wb(observable, expected):
    raw, test, acquisition = fixture(observable)
    result = analyze(raw, test, acquisition)
    assert [p["value"] for p in result["points"]] == pytest.approx([expected] * 4)
    assert result["samples"][0]["statistics"]["awb"]["valid_cells"] == 4


def test_black_is_not_clamped_or_replaced_with_expected_signal():
    raw, test, acquisition = fixture()
    raw[:] = 0
    result = analyze(raw, test, acquisition)
    assert result["points"][0]["value"] == pytest.approx(-16 / 239)


def test_common_cells_reject_whole_saturated_cell_without_rgb_fallback():
    raw, test, acquisition = fixture("wb_adjusted_rg_ratio")
    raw[:, 1, 1] = 255
    result = analyze(raw, test, acquisition)
    assert result["samples"][0]["statistics"]["awb"]["valid_cells"] == 3
    assert result["points"][0]["value"] == pytest.approx(1.2)
    raw[:] = 255
    result = analyze(raw, test, acquisition)
    assert result["points"][0] == {
        "frame_id": "f0",
        "value": None,
        "status": "insufficient_common_cells",
    }
    assert metric_fields(result)["values"] == []


def test_missing_raw_and_applied_controls_are_separate_unknowns():
    raw, test, acquisition = fixture("wb_adjusted_rg_ratio")
    acquisition["frames"][1]["raw_index"] = None
    acquisition["frames"][2]["raw_index"] = 1
    acquisition["frames"][3]["raw_index"] = 2
    acquisition["frames"][2]["applied"] = None
    result = analyze(raw[[0, 2, 3]], test, acquisition)
    assert [p["status"] for p in result["points"]] == [
        "valid",
        "missing_raw",
        "unknown_applied_controls",
        "valid",
    ]
    assert result["coverage"]["raw_frames"] == 3


@pytest.mark.parametrize(
    "change", ["frame_order", "raw_reuse", "foreign_frame", "duplicate_frame", "duplicate_command"]
)
def test_incoherent_frame_or_command_identity_is_refused(change):
    raw, test, acquisition = fixture()
    if change == "frame_order":
        acquisition["frames"].reverse()
    elif change == "raw_reuse":
        acquisition["frames"][1]["raw_index"] = 0
    elif change == "foreign_frame":
        acquisition["frames"][0]["frame_id"] = "alien"
    elif change == "duplicate_frame":
        acquisition["frames"][1]["frame_id"] = "f0"
    else:
        command = {
            "command_id": "c",
            "kind": "ae",
            "source_frame_id": "f0",
            "status": "unknown",
            "issued_at_ns": None,
            "applied_at_ns": None,
            "applied_frame_id": None,
        }
        acquisition["commands"] = [command, command]
    with pytest.raises(ValueError):
        analyze(raw, test, acquisition)


def test_command_latency_uses_source_frame_and_real_events_only():
    raw, test, acquisition = fixture("ae_command_latency_ns")
    acquisition["commands"] = [
        {
            "command_id": "c0",
            "kind": "ae",
            "source_frame_id": "f0",
            "status": "applied",
            "issued_at_ns": 5_000_000,
            "applied_at_ns": 20_000_000,
            "applied_frame_id": "f2",
        },
        {
            "command_id": "c1",
            "kind": "ae",
            "source_frame_id": "f1",
            "status": "applied",
            "issued_at_ns": None,
            "applied_at_ns": 30_000_000,
            "applied_frame_id": "f3",
        },
    ]
    result = analyze(raw, test, acquisition)
    assert [p["value"] for p in result["points"]] == [15_000_000, None, None, None]
    assert result["points"][1]["status"] == "unknown_command_event"
    assert result["points"][2]["status"] == "no_issued_command"
    acquisition["commands"][0]["issued_at_ns"] = 21_000_000
    with pytest.raises(ValueError, match="precedes"):
        analyze(raw, test, acquisition)


def test_sampled_settling_requires_remaining_window_and_dwell():
    raw, test, acquisition = fixture(
        reduction="sampled_settling_time",
        target=94 / 239,
        settling_band=2 / 239,
        window_start_frame_id="f0",
        window_end_frame_id="f3",
        minimum_dwell_frames=2,
    )
    for frame, code in zip(raw, [40, 128, 111, 110], strict=True):
        frame[:] = code
    result = analyze(raw, test, acquisition)
    assert result["points"][0]["value"] == 20_000_000
    raw[3] = 130
    result = analyze(raw, test, acquisition)
    assert result["points"][0]["status"] == "not_settled_in_window"
    assert result["points"][0]["value"] is None


def test_peak_error_preserves_nominal_target_residual():
    raw, test, acquisition = fixture(
        reduction="peak_absolute_target_error",
        target=0.5,
        window_start_frame_id="f1",
        window_end_frame_id="f3",
    )
    raw[0] = 255  # Outside the specified window.
    result = analyze(raw, test, acquisition)
    assert result["points"][0]["value"] == pytest.approx(0.5 - 80 / 239)


def test_same_test_can_compare_different_applied_controls():
    raw, test, acquisition = fixture("gain")
    predicted = record(raw, test, acquisition)
    for frame in acquisition["frames"]:
        frame["applied"]["gain"] = 2.0
    measured = record(raw, test, acquisition, identity="reference")
    report = compare(predicted, measured, test)
    assert report["status"] == "FAIL"
    assert report["comparison_valid"]
    assert report["residuals"]["values"] == [-1.0] * 4
    assert not any(i["code"] == "condition_mismatch" for i in report["issues"])


def test_missing_points_are_reported_without_reordering_or_false_pass():
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    acquisition["frames"][1]["raw_index"] = None
    acquisition["frames"][2]["raw_index"] = 1
    acquisition["frames"][3]["raw_index"] = 2
    measured = record(raw[[0, 2, 3]], test, acquisition, identity="reference")
    report = compare(predicted, measured, test)
    assert report["status"] == "INCONCLUSIVE"
    assert report["response_comparison"]["paired_samples"] == ["f0", "f2", "f3"]
    assert report["residuals"]["values"] == [0.0] * 3


@pytest.mark.parametrize("bound,status", [(0, "PASS"), (None, "INCONCLUSIVE"), (1, "INCONCLUSIVE")])
def test_one_explicit_clock_mapping_and_alignment_bound(bound, status):
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    acquisition["clock"]["clock_id"] = "instrument-clock"
    acquisition["test_clock_offset_ns"] = 1000
    acquisition["alignment_absolute_bound_ns"] = bound
    for frame in acquisition["frames"]:
        frame["frame_start_ns"] -= 1000
        frame["latch_at_ns"] -= 1000
    measured = record(raw, test, acquisition, identity="reference")
    assert compare(predicted, measured, test)["status"] == status


def test_time_mismatch_and_actuator_domain_are_not_numerical_pass():
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    acquisition["test_clock_offset_ns"] = 1
    measured = record(raw, test, acquisition, identity="reference")
    assert compare(predicted, measured, test)["status"] == "INVALID_COMPARISON"
    acquisition["test_clock_offset_ns"] = 0
    acquisition["frames"][3]["applied"]["gain"] = 9.0
    measured = record(raw, test, acquisition, identity="reference")
    assert compare(predicted, measured, test)["status"] == "OUT_OF_DOMAIN"


def test_unknown_total_uncertainty_and_nonphysical_reference_stay_inconclusive():
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    measured = record(raw, test, acquisition, identity="reference")
    predicted["uncertainty"] = {"kind": "unknown", "unit": "1"}
    report = compare(predicted, measured, test)
    assert report["status"] == "INCONCLUSIVE" and report["residuals"]["values"] == [0.0] * 4
    predicted["uncertainty"] = {"kind": "absolute_bound", "unit": "1", "value": 0.0}
    test["validation_kind"] = "physical_validation"
    report = compare(predicted, measured, test)
    assert not report["physical_validation_pass"] and report["status"] == "INCONCLUSIVE"


def test_metric_cannot_be_forged_separately_from_preserved_analysis():
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    measured = deepcopy(predicted)
    measured["values"][0] += 1
    with pytest.raises(ValueError, match="preserved analysis"):
        compare(predicted, measured, test)


def test_all_unusable_awb_samples_remain_empty_and_inconclusive():
    raw, test, acquisition = fixture("wb_adjusted_rg_ratio")
    raw[:] = 255
    predicted = record(raw, test, acquisition)
    measured = record(raw, test, acquisition, identity="reference")
    report = compare(predicted, measured, test)
    assert report["status"] == "INCONCLUSIVE"
    assert report["residuals"] is None
    assert len(report["response_comparison"]["missing_samples"]) == 4
    assert not report["physical_validation_pass"]


@pytest.mark.parametrize("bound,status", [(0.0, "FAIL"), (0.1, "INCONCLUSIVE")])
def test_bounded_residuals_do_not_ignore_measurement_uncertainty(bound, status):
    raw, test, acquisition = fixture()
    predicted = record(raw, test, acquisition)
    measured = record(raw + 5, test, acquisition, identity="reference", bound=bound)
    report = compare(predicted, measured, test)
    assert report["status"] == status
    assert report["residuals"]["values"] == pytest.approx([-5 / 239] * 4)


def test_unknown_clock_span_and_command_values_are_not_repaired():
    from camera_platform.control_observation import ResponseAcquisition

    _, _, acquisition = fixture()
    acquisition["commands"] = [
        {
            "command_id": "c0",
            "kind": "ae",
            "source_frame_id": "f0",
            "status": "unknown",
            "issued_at_ns": None,
            "applied_at_ns": None,
            "applied_frame_id": None,
            "requested": {"exposure_ns": 123456.7},
            "commanded": {"exposure_ns": 100000, "gain": 1.0},
        }
    ]
    result = ResponseAcquisition.model_validate(acquisition).model_dump()
    assert result["commands"][0]["requested"]["exposure_ns"] == 123456.7
    assert result["commands"][0]["applied_at_ns"] is None
    acquisition["commands"][0]["commanded"]["exposure_ns"] = 100000.5
    with pytest.raises(ValueError, match="exact integer"):
        ResponseAcquisition.model_validate(acquisition)

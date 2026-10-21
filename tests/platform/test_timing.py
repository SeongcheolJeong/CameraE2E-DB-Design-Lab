"""Independent temporal geometry, event causality, and no-extrapolation contracts."""

import json
from copy import deepcopy

import pytest

from camera_platform.timing import (
    MAX_TIME_NS,
    TimingError,
    row_windows,
    summarize_timing,
    validate_control_sequence,
    validate_timed_scene,
    validate_timing_coverage,
)


def clock():
    return {"clock_id": "capture-clock", "unit": "ns", "reference": "relative",
            "origin": "Authored sequence trigger; no absolute-clock calibration claimed"}


def geometry():
    return {"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": [3, 5]}


def controls(starts=(100, 110, 120)):
    return {
        "schema": "recorded_control_sequence_v1", "clock": clock(),
        "mode": {"shutter": "rolling", "line_time_ns": 7, "hidden_top_rows": 2,
                 "readout_basis": "full_sensor_rows"},
        "frames": [{"frame_id": f"f{i}", "frame_start_ns": start,
                    "applied": {"applied_at_ns": start, "exposure_ns": 5,
                                "gain": 1.0, "isp_model_id": "isp:fixed:v1", "command_id": None}}
                   for i, start in enumerate(starts)],
    }


def timeline(times=(135, 150, 181)):
    return {"schema": "timed_spectral_scene_v1", "clock": clock(),
            "interpolation": "linear_irradiance",
            "knots": [{"time_ns": t, "spectral_image_id": f"image:{i}"}
                      for i, t in enumerate(times)]}


def validate(data):
    return validate_control_sequence(data, geometry=geometry(), frame_shape=[4, 6])


def summary(data):
    return summarize_timing(data, geometry=geometry(), frame_shape=[4, 6])


def coverage(scene, sequence):
    return validate_timing_coverage(scene, sequence, geometry=geometry(), frame_shape=[4, 6])


def test_independent_crop_row_arithmetic_preserves_native_offset_and_hidden_rows():
    data = validate(controls())
    actual = row_windows(data["frames"][0], data["mode"], geometry(), [4, 6])
    # Trigger100 + 2 hidden rows*7 + sensor rows[3,4,5,6]*7; duration5.
    assert actual == {"row_start_ns": [135, 142, 149, 156],
                      "row_end_ns": [140, 147, 154, 161]}
    altered = geometry()
    altered["crop_origin"][1] = 9
    assert row_windows(data["frames"][0], data["mode"], altered, [4, 6]) == actual
    altered["crop_origin"][0] += 1
    assert row_windows(data["frames"][0], data["mode"], altered, [4, 6])["row_start_ns"] == [
        142, 149, 156, 163
    ]


def test_global_rows_share_exact_window_and_overlap_boundary_is_allowed():
    data = controls((100, 105))
    data["mode"].update(shutter="global", line_time_ns=0, hidden_top_rows=0)
    out = summary(data)
    assert out["frames"][0]["row_start_ns"] == [100] * 4
    assert out["frames"][0]["row_end_ns"] == [105] * 4
    assert out["frames"][1]["row_start_ns"] == [105] * 4


def test_adjacent_rolling_frames_can_overlap_different_rows_not_same_pixel():
    out = summary(controls((100, 105)))
    first, second = out["frames"]
    assert first["row_end_ns"][-1] > second["row_start_ns"][0]
    assert all(a <= b for a, b in zip(first["row_end_ns"], second["row_start_ns"], strict=True))
    bad = controls((100, 104))
    with pytest.raises(TimingError) as exc:
        validate(bad)
    assert exc.value.issues[0]["code"] == "OVERLAPPING_EXPOSURES"


def test_applied_deadline_is_first_full_sensor_active_row_not_crop_row():
    data = controls((100,))
    data["frames"][0]["applied"]["applied_at_ns"] = 114  # 100 + hidden2*line7
    validate(data)
    data["frames"][0]["applied"]["applied_at_ns"] = 115  # Crop does not start until135.
    with pytest.raises(TimingError, match="first full-sensor"):
        validate(data)


def test_global_command_links_allow_delayed_application_and_requested_actual_difference():
    data = controls()
    issued = {"command_id": "request-late", "issued_at_ns": 99, "exposure_ns": 999,
              "gain": 10.0, "isp_model_id": "isp:requested:v2"}
    data["frames"][0]["commanded"] = issued
    data["frames"][2]["applied"]["command_id"] = "request-late"
    data["frames"][2]["applied"]["gain"] = 3.0
    frozen = deepcopy(data)
    out = summary(data)
    assert out["frames"][0]["command_to_applied_ns"] is None
    assert out["frames"][2]["command_to_applied_ns"] == 21
    assert out["frames"][2]["commanded"] == issued
    assert out["frames"][2]["applied"]["exposure_ns"] == 5
    assert out["frames"][2]["applied"]["gain"] == 3.0
    assert data == frozen
    assert out["closed_loop_control_simulated"] is False
    assert out["electronic_schedule_simulated"] is False


def test_command_record_container_order_does_not_override_recorded_causality():
    data = controls()
    data["frames"][2]["commanded"] = {
        "command_id": "issued-early", "issued_at_ns": 90, "exposure_ns": 5,
        "gain": 1, "isp_model_id": "isp:fixed:v1",
    }
    data["frames"][0]["applied"]["command_id"] = "issued-early"
    assert summary(data)["frames"][0]["command_to_applied_ns"] == 10


def test_unknown_observation_events_are_not_fabricated_from_exposure_windows():
    data = controls((100,))
    out = summary(data)
    item = out["frames"][0]
    for key in ("sensor_readout_end_ns", "transport_end_ns", "application_arrival_ns",
                "command_to_applied_ns", "readout_to_transport_end_ns",
                "transport_end_to_application_ns", "command_transmission_ns", "command_arrival_ns"):
        assert item[key] is None
    assert out["timestamp_accuracy"] == "unknown"
    assert "sensor_readout_end_ns" not in validate(data)["frames"][0]
    data["frames"][0]["sensor_readout_end_ns"] = None
    assert validate(data)["frames"][0]["sensor_readout_end_ns"] is None


def test_observed_event_differences_require_both_endpoints_and_order():
    data = controls((100,))
    # Full active final row end:100+(2+19)*7+5=252. Crop final end is161.
    data["frames"][0].update(sensor_readout_end_ns=252, transport_end_ns=260,
                              application_arrival_ns=273)
    item = summary(data)["frames"][0]
    assert item["readout_to_transport_end_ns"] == 8
    assert item["transport_end_to_application_ns"] == 13
    data["frames"][0]["transport_end_ns"] = None
    item = summary(data)["frames"][0]
    assert item["readout_to_transport_end_ns"] is None
    assert item["transport_end_to_application_ns"] is None
    assert item["application_arrival_ns"] == 273


@pytest.mark.parametrize("interpolation", ["linear_irradiance", "zero_order_hold"])
def test_exact_coverage_includes_last_endpoint_without_extrapolation(interpolation):
    scene = timeline((135, 150, 181))
    scene["interpolation"] = interpolation
    out = coverage(scene, controls())
    assert out["status"] == "COVERED"
    assert out["exposure_start_ns"] == 135
    assert out["exposure_end_ns"] == 181
    assert out["interval_convention"] == "half_open"
    assert out["extrapolation"] is False
    # A crop input need not cover unseen full-sensor rows; no full-frame image invented.
    assert out["scene_end_ns"] < 272


@pytest.mark.parametrize("times", [(136, 150, 181), (135, 150, 180)])
def test_one_nanosecond_source_coverage_gap_refuses_instead_of_clamping(times):
    with pytest.raises(TimingError) as exc:
        coverage(timeline(times), controls())
    assert exc.value.issues[0]["code"] == "OUT_OF_TIME_DOMAIN"


@pytest.mark.parametrize("key,value", [("clock_id", "unrelated"), ("origin", "other trigger")])
def test_matching_numbers_on_different_clock_or_origin_are_not_comparable(key, value):
    scene = timeline()
    scene["clock"][key] = value
    with pytest.raises(TimingError) as exc:
        coverage(scene, controls())
    assert exc.value.issues[0]["code"] == "CLOCK_MISMATCH"


def test_relative_integer_bound_keeps_one_nanosecond_differences_and_json_roundtrip():
    base = MAX_TIME_NS - 200
    data = controls((base, base + 5))
    result = summary(json.loads(json.dumps(data)))
    assert result["frames"][0]["row_start_ns"][0] == base + 35
    assert result["frames"][0]["row_end_ns"][0] - result["frames"][0]["row_start_ns"][0] == 5
    low = controls((-MAX_TIME_NS, -MAX_TIME_NS + 5))
    assert summary(low)["frames"][0]["row_start_ns"][0] == -MAX_TIME_NS + 35


def test_last_derived_full_row_endpoint_can_equal_limit_but_not_exceed():
    data = controls((MAX_TIME_NS - 152,))
    assert summary(data)["frames"][0]["row_end_ns"][-1] == MAX_TIME_NS - 91
    data = controls((MAX_TIME_NS - 151,))
    with pytest.raises(TimingError) as exc:
        validate(data)
    assert exc.value.issues[0]["code"] == "TIME_OVERFLOW"


def test_return_values_are_detached_without_mutating_recorded_input():
    data, scene = controls(), timeline()
    frozen_data, frozen_scene = deepcopy(data), deepcopy(scene)
    result = validate(data)
    result["frames"][0]["applied"]["gain"] = 100
    result["clock"]["origin"] = "changed"
    result_scene = validate_timed_scene(scene)
    result_scene["knots"][0]["spectral_image_id"] = "changed"
    assert data == frozen_data
    assert scene == frozen_scene


def test_expanded_scene_mode_requires_explicit_boundary_and_detaches_data():
    scene = timeline()
    for knot in scene["knots"]:
        knot.pop("spectral_image_id")
        knot["scene"] = {"spectral_irradiance": [[[1, 2]]]}
    with pytest.raises(TimingError):
        validate_timed_scene(scene)
    result = validate_timed_scene(scene, expanded=True)
    result["knots"][0]["scene"]["spectral_irradiance"][0][0][0] = 9
    assert scene["knots"][0]["scene"]["spectral_irradiance"][0][0][0] == 1
    assert validate_timing_coverage(scene, controls(), geometry=geometry(), frame_shape=[4, 6],
                                    expanded=True)["status"] == "COVERED"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="other"),
    lambda d: d.update(exposure_ns=5),
    lambda d: d["clock"].update(unit="s"),
    lambda d: d["clock"].pop("origin"),
    lambda d: d["clock"].update(reference="absolute"),
    lambda d: d["clock"].update(origin=" "),
    lambda d: d.update(frames=[]),
    lambda d: d.update(frames=d["frames"] * 6),
    lambda d: d["mode"].update(shutter="hdr"),
    lambda d: d["mode"].update(line_time_ns=0),
    lambda d: d["mode"].update(line_time_ns=True),
    lambda d: d["mode"].update(hidden_top_rows=-1),
    lambda d: d["mode"].update(readout_basis="crop_rows"),
    lambda d: d["mode"].update(shutter="global", line_time_ns=0),
    lambda d: d["frames"][0].update(frame_start_ns=100.0),
    lambda d: d["frames"][0].update(frame_start_ns=10**1000),
    lambda d: d["frames"][0].update(frame_start_ns=True),
    lambda d: d["frames"][1].update(frame_id="f0"),
    lambda d: d["frames"][1].update(frame_start_ns=100),
    lambda d: d["frames"][0].pop("applied"),
    lambda d: d["frames"][0]["applied"].pop("command_id"),
    lambda d: d["frames"][0]["applied"].pop("exposure_ns"),
    lambda d: d["frames"][0]["applied"].pop("gain"),
    lambda d: d["frames"][0]["applied"].pop("isp_model_id"),
    lambda d: d["frames"][0]["applied"].update(isp_model_id=""),
    lambda d: d["frames"][0]["applied"].update(exposure_ns=0),
    lambda d: d["frames"][0]["applied"].update(exposure_ns=1.5),
    lambda d: d["frames"][0]["applied"].update(exposure_ns=3600_000_000_001),
    lambda d: d["frames"][0]["applied"].update(exposure_ns=10**1000),
    lambda d: d["frames"][0]["applied"].update(gain=float("nan")),
    lambda d: d["frames"][0]["applied"].update(gain=float("inf")),
    lambda d: d["frames"][0]["applied"].update(gain=True),
    lambda d: d["frames"][0]["applied"].update(gain=10**1000),
    lambda d: d["frames"][0]["applied"].update(command_id="missing"),
    lambda d: d["frames"][1]["applied"].update(applied_at_ns=99),
    lambda d: d["frames"][1]["applied"].update(applied_at_ns=100, gain=2),
    lambda d: d["frames"][0].update(sensor_readout_end_ns=251),
    lambda d: d["frames"][0].update(transport_end_ns=251),
    lambda d: d["frames"][0].update(application_arrival_ns=251),
    lambda d: d["frames"][0].update(sensor_readout_end_ns=260, application_arrival_ns=259),
    lambda d: d["frames"][0].update(transport_end_ns="unknown"),
    lambda d: d["frames"][0].update(transport_end_ns=MAX_TIME_NS + 1),
])
def test_malformed_or_impossible_control_refuses(mutate):
    data = controls()
    mutate(data)
    with pytest.raises(TimingError):
        validate(data)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(knots=[]),
    lambda d: d.update(knots=d["knots"][:1]),
    lambda d: d.update(knots=d["knots"] * 43),
    lambda d: d.update(interpolation="cubic"),
    lambda d: d["knots"][1].update(time_ns=135),
    lambda d: d["knots"][1].update(time_ns=134),
    lambda d: d["knots"][1].update(time_ns=150.0),
    lambda d: d["knots"][1].update(time_ns=True),
    lambda d: d["knots"][1].update(time_ns=float("nan")),
    lambda d: d["knots"][1].update(time_ns=MAX_TIME_NS + 1),
    lambda d: d["knots"][0].update(time_ns=-MAX_TIME_NS),
    lambda d: d["knots"][0].update(spectral_image_id=None),
    lambda d: d["knots"][0].update(exposure_s=1),
    lambda d: d["clock"].update(unit="us"),
])
def test_malformed_timeline_refuses(mutate):
    data = timeline()
    mutate(data)
    with pytest.raises(TimingError):
        validate_timed_scene(data)


def test_duplicate_command_identity_or_issue_after_application_refuses():
    command = {"command_id": "cmd", "issued_at_ns": 99, "exposure_ns": 5,
               "gain": 1, "isp_model_id": "isp:fixed:v1"}
    data = controls()
    data["frames"][0]["commanded"] = deepcopy(command)
    data["frames"][1]["commanded"] = deepcopy(command)
    with pytest.raises(TimingError, match="Command IDs"):
        validate(data)
    data["frames"][1].pop("commanded")
    data["frames"][0]["applied"]["command_id"] = "cmd"
    data["frames"][0]["commanded"]["issued_at_ns"] = 101
    with pytest.raises(TimingError, match="before it was issued"):
        validate(data)


@pytest.mark.parametrize("geom,shape", [
    ({"kind": "representative_raster", "raster_shape": [4, 6]}, [4, 6]),
    ({"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": [18, 5]}, [4, 6]),
    ({"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": [3.0, 5]}, [4, 6]),
    (geometry(), [3, 6]),
    (geometry(), [4, 7]),
    (geometry(), [0, 6]),
    (geometry(), [True, 6]),
])
def test_native_geometry_and_shape_refusal(geom, shape):
    with pytest.raises(TimingError):
        validate_control_sequence(controls(), geometry=geom, frame_shape=shape)


def test_total_frame_pixel_bound_and_overflow_line_product_are_preflight_errors():
    data = controls(tuple(100 + 10 * i for i in range(5)))
    large = {"kind": "native_sensor", "full_sensor_shape": [512, 512], "crop_origin": [0, 0]}
    with pytest.raises(TimingError, match="total frame pixels"):
        validate_control_sequence(data, geometry=large, frame_shape=[512, 512])
    data = controls((100,))
    data["mode"]["line_time_ns"] = MAX_TIME_NS
    with pytest.raises(TimingError) as exc:
        validate(data)
    assert exc.value.issues[0]["code"] == "TIME_OVERFLOW"

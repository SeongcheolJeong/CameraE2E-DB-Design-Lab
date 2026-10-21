"""Reference HDR changes fused pixels while preserving the electrical capture path."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import capture_programs, electrical, hdr, hdr_runtime, reference_hdr
from camera_platform.reference_hdr_runtime import (
    PROGRAM_SCHEMA,
    ReferenceHDRRuntime,
    capture_effects,
    default_capture_program,
    validate_capture_program,
)
from camera_platform.runtime import RuntimeRequestError, get_plugin, list_plugins
from camera_platform.temporal import request_template


@pytest.fixture
def request_data():
    data = request_template()
    data["noise_enabled"] = False
    data["control_sequence"]["frames"][0]["applied"]["exposure_ns"] = 2_000_000
    data["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 8_000_000
    data["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 10_000_000
    data["capture_program"] = default_capture_program(data["readout"])
    return data


def run(data):
    return ReferenceHDRRuntime().execute(data)


def plain(data):
    result = deepcopy(data)
    result["capture_program"].update(schema=hdr_runtime.PROGRAM_SCHEMA, hdr_merge=None)
    return result


def test_moving_capture_has_raw_rgb_and_reference_fallback_with_static_background(request_data):
    baseline = hdr_runtime.HDRBracketRuntime().execute(plain(request_data))
    out = run(request_data)
    a, s = out["arrays"], out["summary"]
    # The new merge cannot alter original acquired RAW or its configured ISP.
    for name in ("raw_frames", "rgb_frames", "channel_electrons", "mean_electrons",
                 "row_start_ns", "row_end_ns", "electrical_adc_dn"):
        np.testing.assert_array_equal(a[name], baseline["arrays"][name])
    assert not np.array_equal(a["raw_frames"][0], a["raw_frames"][1])
    assert not np.array_equal(a["rgb_frames"][0], a["rgb_frames"][1])
    assert s["hdr_merge"]["fused_pixels"] > 0
    assert s["hdr_merge"]["reference_fallback_pixels"] > 0
    ref = s["hdr_merge"]["reference_frame_index"]
    fallback = a["hdr_reference_fallback_mask"]
    np.testing.assert_array_equal(
        a["hdr_merged_rate"][fallback], a["hdr_exposure_rate"][ref][fallback]
    )
    background = a["hdr_fused_mask"]
    assert np.all(a["hdr_estimated_variance"][background]
                  < a["hdr_exposure_estimated_variance"][ref][background])
    assert s["hdr_merge"]["reference_frame_id"] == s["frame_ids"][ref]
    assert s["capture_program"]["schema"] == PROGRAM_SCHEMA
    assert s["prediction"]["values"] == baseline["summary"]["prediction"]["values"]
    assert s["prediction"]["included_effects"] == s["included_effects"]
    assert "raw_hdr_bracket_merge" not in s["included_effects"]
    assert "reference_constrained_raw_merge" in s["included_effects"]
    assert set(a) == set(s["array_units"])
    assert set(s["hdr_merge"]["array_units"]) == {
        name.removeprefix("hdr_") for name in a if name.startswith("hdr_")
    }
    assert s["physical_validation"] == "not_established"
    assert s["simulation_oracle"]["used_by_hdr_merge"] is False
    json.dumps(s, allow_nan=False)


def test_raw_only_fusion_ignores_modified_simulator_oracles(request_data, monkeypatch):
    expected = run(request_data)
    real_capture = electrical.capture_electrons
    real_merge = reference_hdr.merge_reference_raw_bracket
    calls = []

    def altered(*args, **kwargs):
        out = real_capture(*args, **kwargs)
        out["arrays"]["mean_total_charge_e"][:] = 777_777
        out["arrays"]["well_saturated_mask"][:] = True
        return out

    def observe(raw, exposures, calibrations, **kwargs):
        assert raw.dtype.kind in "iu"
        assert all(set(cal) == hdr.CALIBRATION_FIELDS for cal in calibrations)
        calls.append(hashlib.sha256(raw.tobytes()).hexdigest())
        return real_merge(raw, exposures, calibrations, **kwargs)

    monkeypatch.setattr(electrical, "capture_electrons", altered)
    monkeypatch.setattr(reference_hdr, "merge_reference_raw_bracket", observe)
    result = run(request_data)
    assert len(calls) == 1
    for key in expected["arrays"]:
        if key.startswith("hdr_") or key in {"raw_frames", "rgb_frames"}:
            np.testing.assert_array_equal(expected["arrays"][key], result["arrays"][key])


def test_reference_clipping_does_not_expand_output_validity(request_data):
    request_data["capture_program"]["hdr_merge"]["reference_frame_index"] = 1
    request_data["control_sequence"]["frames"][1]["applied"]["gain"] = 100
    out = run(request_data)
    a = out["arrays"]
    clipped_reference = ~a["hdr_exposure_valid_mask"][1]
    assert clipped_reference.any()
    assert a["hdr_exposure_valid_mask"][0][clipped_reference].any()
    assert not a["hdr_valid_mask"][clipped_reference].any()
    assert np.isnan(a["hdr_merged_rate"][clipped_reference]).all()
    assert out["summary"]["hdr_merge"]["reference_clipping_recovery"] is False


def test_constant_light_is_allowed_and_changes_actual_captured_pixels(request_data):
    original = run(request_data)
    request_data["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "constant",
        "clock_id": request_data["control_sequence"]["clock"]["clock_id"], "level": 0.5,
    }
    changed = run(request_data)
    assert not np.array_equal(original["arrays"]["raw_frames"], changed["arrays"]["raw_frames"])
    np.testing.assert_allclose(changed["arrays"]["waveform_row_exposure_ratio"], 0.5)


def pwm(data, phase=0):
    return {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": phase, "low": 0.0, "high": 1.0,
    }


def test_whole_period_pwm_is_still_rejected_before_capture(request_data, monkeypatch):
    request_data["capture_program"]["waveform"] = pwm(request_data)
    with pytest.raises(ValueError, match="constant illumination only"):
        validate_capture_program(request_data["capture_program"])
    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("captured"))
    assert ReferenceHDRRuntime().validate_request(request_data)
    with pytest.raises(RuntimeRequestError, match="constant illumination only"):
        run(request_data)


def test_motion_pwm_equal_on_time_does_not_establish_common_rate(request_data):
    data = plain(request_data)
    data["control_sequence"]["mode"].update(shutter="global", line_time_ns=0)
    for frame in data["control_sequence"]["frames"]:
        frame["applied"]["exposure_ns"] = 2_000_000
    data["capture_program"]["waveform"] = pwm(data)
    first = hdr_runtime.HDRBracketRuntime().execute(data)
    data["capture_program"]["waveform"] = pwm(data, 500_000)
    second = hdr_runtime.HDRBracketRuntime().execute(data)
    np.testing.assert_array_equal(first["arrays"]["waveform_row_effective_s"],
                                  second["arrays"]["waveform_row_effective_s"])
    for name in ("mean_electrons", "raw_frames", "rgb_frames"):
        assert not np.array_equal(first["arrays"][name], second["arrays"][name])


@pytest.mark.parametrize("violation", [
    "actual_reference", "one_frame", "overlap", "clock", "coverage", "geometry", "adc",
    "seed", "missing_program", "extra_program", "null_merge", "wrong_schema",
])
def test_invalid_domain_is_rejected_before_electrical_readout(request_data, monkeypatch, violation):
    if violation == "actual_reference":
        request_data["capture_program"]["hdr_merge"]["reference_frame_index"] = 2
    elif violation == "one_frame":
        request_data["control_sequence"]["frames"] = request_data["control_sequence"]["frames"][:1]
    elif violation == "overlap":
        request_data["control_sequence"]["frames"][1]["frame_start_ns"] = 1_000_000
        request_data["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 1_000_000
    elif violation == "clock":
        request_data["scene_sequence"]["clock"] = {
            **request_data["scene_sequence"]["clock"], "clock_id": "different-clock",
        }
    elif violation == "coverage":
        request_data["scene_sequence"]["knots"][-1]["time_ns"] = 11_000_000
    elif violation == "geometry":
        request_data["scene_sequence"]["knots"][1]["scene"]["sampling"]["crop_origin"] = [1, 0]
    elif violation == "adc":
        request_data["capture_program"]["electrical_model"]["adc"]["black_level_dn"] += 1
    elif violation == "seed":
        request_data["seed"] = 2**32 - 1
    elif violation == "missing_program":
        request_data.pop("capture_program")
    elif violation == "extra_program":
        request_data["capture_program"]["scene_truth"] = []
    elif violation == "null_merge":
        request_data["capture_program"]["hdr_merge"] = None
    else:
        request_data["capture_program"]["schema"] = hdr_runtime.PROGRAM_SCHEMA
    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("captured"))
    assert ReferenceHDRRuntime().validate_request(request_data)
    with pytest.raises(RuntimeRequestError):
        run(request_data)


def test_reference_resource_guard_applies_before_capture(request_data, monkeypatch):
    original = hdr_runtime.HDRBracketRuntime().estimate_resources(plain(request_data))
    estimate = ReferenceHDRRuntime().estimate_resources(request_data)
    assert estimate["working_array_bytes_estimate"] > original["working_array_bytes_estimate"]
    assert estimate["working_array_bytes_estimate"] >= sum(
        a.nbytes for a in run(request_data)["arrays"].values()
    )
    monkeypatch.setattr(hdr_runtime, "MAX_WORKING_ARRAY_BYTES",
                        original["working_array_bytes_estimate"] + 1)
    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("captured"))
    with pytest.raises(RuntimeRequestError, match="working array memory"):
        run(request_data)


def test_plugin_routing_and_program_effects_keep_distinct_acquisition_contracts(request_data):
    program = request_data["capture_program"]
    assert isinstance(get_plugin("reference_hdr_v1"), ReferenceHDRRuntime)
    assert capture_programs.runtime_for_payload(program) == "reference_hdr_v1"
    assert (capture_programs.runtime_for_payload(plain(request_data)["capture_program"])
            == "hdr_bracket_v1")
    assert any(item["plugin_id"] == "reference_hdr_v1" for item in list_plugins())
    assert capture_effects(program)[-1] == "reference_constrained_raw_merge"
    assert "raw_hdr_bracket_merge" not in capture_effects(program)
    selected = validate_capture_program(program)
    selected["electrical_model"]["digital_gain"] = 2
    assert program["electrical_model"]["digital_gain"] == 1


def test_static_old_runtime_results_remain_identical_after_reference_execution(request_data):
    static = plain(request_data)
    static["scene_sequence"]["knots"][1]["scene"] = deepcopy(
        static["scene_sequence"]["knots"][0]["scene"]
    )
    static["capture_program"]["hdr_merge"] = hdr.default_profile()
    first = hdr_runtime.HDRBracketRuntime().execute(static)
    run(request_data)
    second = hdr_runtime.HDRBracketRuntime().execute(static)
    assert first["summary"] == second["summary"]
    for name in first["arrays"]:
        np.testing.assert_array_equal(first["arrays"][name], second["arrays"][name])


def test_opt_in_raw_translation_preserves_acquired_frames_and_declares_registered_arrays(
    request_data,
):
    request_data["control_sequence"]["mode"].update(shutter="global", line_time_ns=0)
    request_data["capture_program"]["hdr_merge"] = reference_hdr.default_translation_profile()
    plain_output = hdr_runtime.HDRBracketRuntime().execute(plain(request_data))
    output = run(request_data)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_array_equal(arrays["raw_frames"], plain_output["arrays"]["raw_frames"])
    np.testing.assert_array_equal(arrays["rgb_frames"], plain_output["arrays"]["rgb_frames"])
    assert summary["hdr_merge"]["schema"] == (
        "camera_reference_hdr_merge_result_global_translation_v1"
    )
    assert summary["hdr_merge"]["registration"]["shifts"][0]["status"] == "reference"
    assert "reference_constrained_raw_global_translation_merge" in summary["included_effects"]
    assert set(arrays) == set(summary["array_units"])
    assert summary["simulation_oracle"]["used_by_hdr_merge"] is False
    json.dumps(summary, allow_nan=False)


def test_translation_rejects_rolling_and_over_budget_work_before_capture(
    request_data, monkeypatch,
):
    request_data["capture_program"]["hdr_merge"] = reference_hdr.default_translation_profile()
    request_data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=5000)
    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("captured"))
    with pytest.raises(RuntimeRequestError, match="global shutter"):
        run(request_data)
    request_data["control_sequence"]["mode"].update(shutter="global", line_time_ns=0)
    monkeypatch.setattr(reference_hdr, "MAX_REGISTRATION_WORK", 1)
    with pytest.raises(RuntimeRequestError, match="comparison work"):
        run(request_data)


def test_translation_does_not_accept_nonlinear_profile_under_linear_program(request_data):
    profile = reference_hdr.default_translation_profile()
    profile["base_profile"] = reference_hdr.default_profile_v2()["base_profile"]
    request_data["capture_program"]["hdr_merge"] = profile
    with pytest.raises(ValueError, match="Unsupported HDR schema"):
        validate_capture_program(request_data["capture_program"])

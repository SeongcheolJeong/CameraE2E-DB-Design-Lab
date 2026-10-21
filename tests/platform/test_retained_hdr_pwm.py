"""RAW-only retained HDR under an authored PWM clock, not LED validation."""

from copy import deepcopy

import numpy as np
import pytest
from test_native_temporal import expected_rate
from test_native_temporal import native_request as native_request
from test_retained_hdr import _models

from camera_platform import electrical, retained_charge, retained_hdr
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_photo_hdr_capture_program,
)


def test_raw_inverse_uses_row_light_integral_but_full_dark_time_and_keeps_known_state():
    model, reservoir = _models()
    model["dark_current_e_per_s"] = 200_000.0
    # Independently counted ON intervals for a 1 ms period, 0.4 ms ON:
    # first 0.2 ms exposure starts at row phases 0, .4, .6, .8 ms;
    # second 1.2 ms exposure starts at the same phases after two periods.
    photo_time = np.array([[.0002, 0, 0, 0], [.0006, .0004, .0004, .0004]])
    exposures = np.array([.0002, .0012])
    gaps = np.array([[0.0] * 4, [.0018] * 4])
    source_rate = 500_000.0
    state = retained_charge.initial_state((4, 4), reservoir, noise_enabled=False)
    rng = np.random.default_rng(23)
    raw = []
    for index in range(2):
        step = retained_charge.step(
            state, np.broadcast_to(source_rate * photo_time[index, :, None], (4, 4)),
            float(exposures[index]), gaps[index], reservoir,
            np.ones((4, 4)), np.full((4, 4), model["dark_current_e_per_s"]),
            False, False, rng,
        )
        state = step["state"]
        raw.append(electrical.read_delivered_charge(
            step["arrays"]["delivered_e"], model, analog_gain=1,
            temperature_c=25, noise_enabled=False, rng=rng,
        )["arrays"]["raw_dn"])
    raw = np.stack(raw)
    original = raw.copy()
    result = retained_hdr.reconstruct_and_select(
        raw, model, reservoir, retained_hdr.default_profile(),
        exposures_s=exposures, photo_exposures_s=photo_time, gaps_s=gaps,
        clears=[False, False], analog_gains=[1, 1], temperature_c=25,
        noise_enabled=False,
        geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4],
                  "crop_origin": [0, 0]},
    )
    arrays = result["arrays"]
    np.testing.assert_array_equal(raw, original)
    assert result["summary"]["photo_time_basis"] == "authored_waveform_row_integral_s"
    assert result["summary"]["frame_diagnostics"][0]["unilluminated_rows"] == 3
    assert arrays["state_known_mask"][0].all()
    assert not arrays["exposure_valid_mask"][0, 1:].any()
    assert arrays["state_known_mask"][1].all()
    assert arrays["valid_mask"].all()
    np.testing.assert_allclose(arrays["merged_rate_e_s"], source_rate, rtol=0, atol=10_000)
    with pytest.raises(ValueError, match="photo_exposures_s"):
        retained_hdr.reconstruct_and_select(
            raw, model, reservoir, retained_hdr.default_profile(),
            exposures_s=exposures, photo_exposures_s=np.ones((2, 3)), gaps_s=gaps,
            clears=[False, False], analog_gains=[1, 1], temperature_c=25,
            noise_enabled=False,
            geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4],
                      "crop_origin": [0, 0]},
        )


def test_retained_runtime_passes_exact_pwm_row_integrals_to_raw_hdr(native_request):
    data = native_request
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(
            full_sensor_shape=[8, 10], crop_origin=[0, 0]
        )
    for index, frame in enumerate(data["control_sequence"]["frames"]):
        frame["applied"]["exposure_ns"] = [2_000_000, 8_000_000][index]
    frame_ids = [frame["frame_id"] for frame in data["control_sequence"]["frames"]]
    program = default_photo_hdr_capture_program(data["readout"], frame_ids)
    program["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 400_000,
        "phase_origin_ns": 130_000, "low": 0, "high": 1,
    }
    data["capture_program"] = program
    output = RetainedChargeRuntime().execute(data)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_allclose(
        arrays["waveform_row_effective_s"],
        np.array([[.0008] * 8, [.0032] * 8]), rtol=0, atol=1e-18,
    )
    assert summary["waveform"]["photo_hdr_row_integral_normalization"] is True
    assert summary["photo_hdr"]["known_waveform_required"] is True
    assert summary["photo_hdr"]["state_truth_used"] is False
    site = arrays["cfa_channel_index"]
    np.testing.assert_allclose(
        arrays["photo_hdr_merged_rate_e_s"], expected_rate(data)[site], rtol=.05,
    )
    assert arrays["photo_hdr_valid_mask"].all()
    assert summary["physical_validation"] == "not_established"


def test_rolling_pwm_phase_changes_raw_but_known_timing_recovers_static_rate(native_request):
    data = native_request
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(
            full_sensor_shape=[8, 10], crop_origin=[0, 0]
        )
        knot["scene"]["spectral_irradiance"] *= 10
    data["control_sequence"]["mode"].update(
        shutter="rolling", line_time_ns=137_000, hidden_top_rows=0
    )
    for index, frame in enumerate(data["control_sequence"]["frames"]):
        frame["applied"]["exposure_ns"] = [700_000, 1_700_000][index]
    program = default_photo_hdr_capture_program(
        data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]]
    )
    program["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 400_000,
        "phase_origin_ns": 130_000, "low": 0, "high": 1,
    }
    data["capture_program"] = program

    def independent_on_time(start, end, phase):
        return sum(
            max(0, min(end, phase + cycle * 1_000_000 + 400_000)
                - max(start, phase + cycle * 1_000_000))
            for cycle in range(-1, 25)
        )

    def checked(candidate):
        output = RetainedChargeRuntime().execute(candidate)
        arrays = output["arrays"]
        phase = candidate["capture_program"]["waveform"]["phase_origin_ns"]
        for frame_index, frame in enumerate(candidate["control_sequence"]["frames"]):
            for row in range(8):
                start = frame["frame_start_ns"] + row * 137_000
                end = start + frame["applied"]["exposure_ns"]
                assert arrays["waveform_row_effective_s"][frame_index, row] == pytest.approx(
                    independent_on_time(start, end, phase) * 1e-9, abs=1e-18
                )
        assert np.ptp(arrays["waveform_row_effective_s"][0]) > 0
        assert arrays["photo_hdr_valid_mask"].all()
        np.testing.assert_allclose(
            arrays["photo_hdr_merged_rate_e_s"],
            expected_rate(candidate)[arrays["cfa_channel_index"]] * 10,
            rtol=.05,
        )
        return arrays

    original = checked(data)
    shifted = deepcopy(data)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] += 220_000
    changed = checked(shifted)
    assert not np.array_equal(original["raw_groups"], changed["raw_groups"])

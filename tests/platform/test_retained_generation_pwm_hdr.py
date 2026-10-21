"""Combined generated-charge transport, rolling PWM, retained RAW and HDR ledger.

The scalar source-to-well reference and ON-window enumeration are independent
of the runtime. All inputs are authored; passing is not physical LFM evidence.
"""

import numpy as np
from test_charge_collection_runtime import GENERATION, _collection, _image, _reference, _request
from test_native_temporal import native_request as native_request

from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_photo_hdr_capture_program,
)


def _on_ns(start, end, phase):
    """Enumerate the small fixture's half-open 1 ms PWM cycles directly."""
    return sum(
        max(0, min(end, phase + cycle * 1_000_000 + 400_000)
            - max(start, phase + cycle * 1_000_000))
        for cycle in range(-1, 25)
    )


def test_generation_collection_rolling_pwm_retention_raw_and_hdr(native_request):
    request = _request(native_request, mode="temporal")
    for knot in request["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 20
    controls = request["control_sequence"]
    controls["mode"].update(shutter="rolling", line_time_ns=137_000, hidden_top_rows=0)
    for index, frame in enumerate(controls["frames"]):
        frame["applied"]["exposure_ns"] = [700_000, 1_700_000][index]
    program = default_photo_hdr_capture_program(
        request["readout"], [frame["frame_id"] for frame in controls["frames"]]
    )
    program["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": controls["clock"]["clock_id"], "period_ns": 1_000_000,
        "on_time_ns": 400_000, "phase_origin_ns": 130_000,
        "low": 0, "high": 1,
    }
    request["capture_program"] = program

    expected_rate = _reference(_image() * 20, GENERATION, _collection())["collected"]
    expected_photo_time = np.array([
        [
            _on_ns(
                frame["frame_start_ns"] + row * 137_000,
                frame["frame_start_ns"] + row * 137_000 + frame["applied"]["exposure_ns"],
                130_000,
            ) * 1e-9
            for row in range(8)
        ]
        for frame in controls["frames"]
    ])
    expected_photo = expected_photo_time[:, :, None] * expected_rate
    eta = 0.8
    residual = np.zeros((8, 10))
    expected_delivered, expected_raw = [], []
    black, white, capacity = 64, 4095, request["readout"]["full_well_e"]
    for frame_photo in expected_photo:
        pretransfer = residual + frame_photo
        assert np.max(pretransfer) < capacity
        delivered = eta * pretransfer
        residual = pretransfer - delivered
        expected_delivered.append(delivered)
        expected_raw.append(np.rint(black + delivered * (white - black) / capacity))

    output = RetainedChargeRuntime().execute(request)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_allclose(arrays["waveform_row_effective_s"], expected_photo_time,
                               rtol=0, atol=1e-18)
    np.testing.assert_allclose(arrays["mean_electrons"], expected_photo,
                               rtol=4e-13, atol=1e-10)
    np.testing.assert_allclose(arrays["retained_delivered_e"], expected_delivered,
                               rtol=4e-13, atol=1e-10)
    np.testing.assert_array_equal(arrays["raw_groups"][:, 0], expected_raw)
    assert arrays["photo_hdr_valid_mask"].all()
    np.testing.assert_allclose(arrays["photo_hdr_merged_rate_e_s"], expected_rate,
                               rtol=.03)
    assert np.ptp(expected_photo_time[0]) > 0
    assert summary["photo_hdr"]["state_truth_used"] is False
    assert summary["photo_hdr"]["photo_time_basis"] == "authored_waveform_row_integral_s"
    assert summary["physical_validation"] == "not_established"

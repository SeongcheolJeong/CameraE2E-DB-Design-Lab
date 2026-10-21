"""Independent source-qualification boundaries; no RAW or synthetic truth oracle."""
from copy import deepcopy

import pytest

from camera_platform.electrical_response_study import _validate_stimulus


def inputs(interpolation, hashes, *, start=0, end=20, waveform=None):
    images = [
        {
            "id": f"separate-immutable-record-{index}",
            "shape": [4, 4, 3],
            "wavelength_nm": [450, 550, 650],
            "sampling": {"reference": "native_sensor", "origin": [0, 0]},
            "unit": "W/m^2/nm",
            "reference_plane": "ideal_image",
            "artifacts": [{"name": "spectral-image.npy", "sha256": value * 64}],
        }
        for index, value in enumerate(hashes)
    ]
    sources = {
        "capture_program_id": {"payload": {"waveform": waveform}},
        "scene_sequence_id": {
            "payload": {
                "interpolation": interpolation,
                "knots": [
                    {"time_ns": 10 * index, "spectral_image_id": image["id"]}
                    for index, image in enumerate(images)
                ],
            },
            "image_snapshots": images,
        },
    }
    spec = {"evaluation": {"stimulus": {"kind": "constant_step"},
                           "window": {"change_point_ns": start, "end_ns": end}}}
    return spec, sources


def test_linear_ramp_crossing_declared_change_is_not_a_constant_step():
    spec, sources = inputs("linear_irradiance", ["a", "b", "b"], start=5)
    with pytest.raises(ValueError, match="varies within a linear segment"):
        _validate_stimulus(spec, sources)


def test_constant_linear_tail_accepts_distinct_records_with_identical_signal_bytes():
    spec, sources = inputs("linear_irradiance", ["a", "b", "b"], start=10)
    before = deepcopy((spec, sources))
    proof = _validate_stimulus(spec, sources)
    assert proof["post_change_interval_ns"] == [10, 20]
    assert proof["irradiance_identity"]["irradiance_sha256"] == "b" * 64
    assert proof["physical_step_timing"] == "not_established"
    proof["irradiance_identity"]["sampling"]["origin"][0] = 2
    assert (spec, sources) == before


def test_zero_order_hold_step_at_change_is_used_but_terminal_knot_is_not():
    spec, sources = inputs("zero_order_hold", ["a", "b", "c"], start=10)
    proof = _validate_stimulus(spec, sources)
    assert proof["interval_convention"] == "half_open"
    assert proof["irradiance_identity"]["irradiance_sha256"] == "b" * 64


def test_zero_order_hold_change_inside_post_change_window_is_refused():
    spec, sources = inputs("zero_order_hold", ["a", "b", "c"], start=5, end=15)
    with pytest.raises(ValueError, match="varies across the window"):
        _validate_stimulus(spec, sources)


@pytest.mark.parametrize("start,end", [(-1, 20), (0, 21)])
def test_no_constant_step_authority_outside_the_scene_coverage(start, end):
    spec, sources = inputs("zero_order_hold", ["a", "a", "a"], start=start, end=end)
    with pytest.raises(ValueError, match="covered by the scene"):
        _validate_stimulus(spec, sources)


def test_pwm_scene_variation_cannot_acquire_constant_step_qualification():
    spec, sources = inputs("linear_irradiance", ["a", "b", "c"], waveform={"kind": "pwm"})
    with pytest.raises(ValueError, match="differs from capture waveform"):
        _validate_stimulus(spec, sources)
    spec["evaluation"]["stimulus"]["kind"] = "pwm"
    proof = _validate_stimulus(spec, sources)
    assert proof["step_settling"] == "not_applicable"
    assert "irradiance_identity" not in proof

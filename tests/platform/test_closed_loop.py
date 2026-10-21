"""Physical stage changes and causality for declared, synthetic control programs."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.closed_loop import ClosedLoopSceneRuntime, _apply_pending, request_template
from camera_platform.control_contracts import CameraControlPolicy, ClosedLoopProgram
from camera_platform.controls import default_policy, quantize, raw_statistics
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime
from camera_platform.timing import MAX_TIME_NS


@pytest.fixture
def request_data():
    data = request_template()
    data["noise_enabled"] = False
    data["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = np.full((16, 20, 5), 1e-5)
        knot["scene"]["sampling"]["full_sensor_shape"] = [16, 20]
    data["control_program"]["policy"] = default_policy([16, 20])
    return data


def run(request):
    return ClosedLoopSceneRuntime().execute(request)


def trace(result):
    return result["summary"]["control_trace"]


def recorded_request(data):
    p = data["control_program"]
    mode = p["mode"]
    return {
        **{k: v for k, v in data.items() if k != "control_program"},
        "control_sequence": {
            "schema": "recorded_control_sequence_v1",
            "clock": p["clock"],
            "mode": mode,
            "frames": [
                {
                    **f,
                    "applied": {
                        **p["initial"],
                        "command_id": None,
                        "applied_at_ns": f["frame_start_ns"]
                        + mode["hidden_top_rows"] * mode["line_time_ns"],
                    },
                }
                for f in p["frames"]
            ],
        },
    }


@pytest.mark.parametrize("noise", [False, True])
def test_disabled_controls_equal_full_recorded_capture_including_noise(request_data, noise):
    data = request_data
    data["noise_enabled"] = noise
    data["control_program"]["policy"]["ae"]["enabled"] = False
    data["control_program"]["policy"]["awb"]["enabled"] = False
    actual = run(data)
    recorded = TemporalSceneRuntime().execute(recorded_request(data))
    for key in actual["arrays"]:
        np.testing.assert_array_equal(actual["arrays"][key], recorded["arrays"][key])
    assert trace(actual)["commands"] == []
    assert actual["summary"]["seed_sequence"] == list(range(223, 239))
    assert actual["summary"]["frames"] == 16
    assert len(actual["summary"]["prediction"]["values"]) == 48
    assert len(actual["summary"]["prediction"]["conditions_per_frame"]) == 16


def test_ae_changes_collected_charge_and_raw_after_minimum_delay(request_data):
    policy = request_data["control_program"]["policy"]
    policy["awb"]["enabled"] = False
    policy["ae"]["minimum_delay_frames"] = 2
    output = run(request_data)
    frames = trace(output)["frames"]
    assert [f["applied"]["exposure_ns"] for f in frames[:3]] == [1_000_000, 1_000_000, 2_000_000]
    np.testing.assert_allclose(
        output["arrays"]["channel_electrons"][2],
        2 * output["arrays"]["channel_electrons"][0],
        rtol=2e-15,
    )
    assert output["arrays"]["raw_frames"][2].mean() > output["arrays"]["raw_frames"][0].mean()
    first = trace(output)["commands"][0]
    assert first["applied_frame_index"] == 2
    assert first["source_frame_index"] == 0
    assert first["source_to_applied_frames"] == 2
    assert first["command_to_applied_ns"] == 19_000_000


def test_ae_converges_and_respects_exposure_gain_grid(request_data):
    policy = request_data["control_program"]["policy"]
    policy["awb"]["enabled"] = False
    output = run(request_data)
    frames = trace(output)["frames"]
    assert abs(frames[-1]["statistics"]["ae"]["value"] - 0.18) < 0.012
    for f in frames:
        state = f["applied"]
        assert 100_000 <= state["exposure_ns"] <= 8_000_000
        assert state["exposure_ns"] % 100_000 == 0
        assert state["gain"] * 8 == round(state["gain"] * 8)
    assert any(
        c["proposal"]["actuator_adjustments"]["exposure_ns"]["clamped"]
        for c in trace(output)["commands"]
    )


def test_awb_changes_rgb_not_raw_or_photons_and_preserves_green_anchor(request_data):
    data = request_data
    p = data["control_program"]
    p["initial"]["exposure_ns"] = 8_000_000
    p["initial"]["wb_gains_rgb"] = [2.0, 2.0, 2.0]
    p["policy"]["ae"]["enabled"] = False
    p["policy"]["awb"]["minimum_delay_frames"] = 3
    reference = deepcopy(data)
    reference["control_program"]["policy"]["awb"]["enabled"] = False
    off, output = run(reference), run(data)
    for key in ("raw_frames", "channel_electrons", "black_subtracted_normalized_frames"):
        np.testing.assert_array_equal(output["arrays"][key], off["arrays"][key])
    np.testing.assert_array_equal(
        output["arrays"]["rgb_frames"][:3], off["arrays"]["rgb_frames"][:3]
    )
    assert not np.array_equal(output["arrays"]["rgb_frames"][3], off["arrays"]["rgb_frames"][3])
    assert all(f["applied"]["wb_gains_rgb"][1] == 2 for f in trace(output)["frames"])
    assert trace(output)["commands"][0]["applied_frame_index"] == 3
    assert output["summary"]["isp_per_frame"][3] != output["summary"]["isp_per_frame"][0]


def test_dark_scene_probes_ae_but_holds_awb_without_fallback(request_data):
    data = request_data
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 0
    output = run(data)
    assert all(
        f["control_proposals"]["awb"]["status"] == "insufficient_statistics"
        for f in trace(output)["frames"]
    )
    assert trace(output)["frames"][0]["control_proposals"]["ae"]["status"] == "probe_dark"
    assert all(c["kind"] == "ae" for c in trace(output)["commands"])
    assert all(f["applied"]["wb_gains_rgb"] == [1, 1, 1] for f in trace(output)["frames"])
    assert output["arrays"]["raw_frames"].min() == data["readout"]["black_level_dn"]


def test_declared_latency_defers_commands_and_retains_end_pending(request_data):
    p = request_data["control_program"]["policy"]
    p["awb"]["enabled"] = False
    p["latency"].update(
        readout_tail_ns=1_000_000,
        frame_transport_ns=2_000_000,
        statistics_ns=3_000_000,
        controller_ns=4_000_000,
        command_transport_ns=5_000_000,
        actuator_ns=6_000_000,
    )
    out = run(request_data)
    first = trace(out)["commands"][0]
    assert first["statistics_ready_ns"] == 7_000_000
    assert first["issued_at_ns"] == 11_000_000
    assert first["actuator_ready_ns"] == 22_000_000
    assert first["applied_at_ns"] == 30_000_000
    assert all(f["applied"]["exposure_ns"] == 1_000_000 for f in trace(out)["frames"][:3])
    assert any(c["status"] == "pending_after_sequence" for c in trace(out)["commands"])
    assert "control_sequence" not in out["summary"] and "timing" not in out["summary"]
    assert trace(out)["latency_evidence"] == "declared_simulation_not_hardware_observation"


def test_rolling_full_sensor_timing_not_crop_bottom_controls_readiness(request_data):
    data = request_data
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[40, 50], crop_origin=[3, 5])
    p = data["control_program"]
    p["mode"].update(shutter="rolling", hidden_top_rows=2, line_time_ns=300_000)
    p["policy"]["awb"]["enabled"] = False
    data["scene_sequence"]["knots"][-1]["time_ns"] = 180_000_000
    out = run(data)
    f = trace(out)["frames"][0]
    assert out["arrays"]["cfa_channel_index"][0, 0] == 2
    assert f["latch_at_ns"] == 600_000
    assert f["declared_events"]["full_sensor_exposure_end_ns"] == 13_300_000
    assert out["arrays"]["row_end_ns"][0, -1] == 7_000_000
    assert trace(out)["commands"][0]["applied_frame_index"] == 2


def test_prefix_cannot_depend_on_future_irradiance(request_data):
    data = request_data
    scene = data["scene_sequence"]["knots"][0]["scene"]
    data["scene_sequence"].update(
        interpolation="zero_order_hold",
        knots=[
            {"time_ns": 0, "scene": deepcopy(scene)},
            {"time_ns": 80_000_000, "scene": deepcopy(scene)},
            {"time_ns": 160_000_000, "scene": deepcopy(scene)},
        ],
    )
    initial = run(data)
    data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 20
    changed = run(data)
    for key in ("raw_frames", "rgb_frames", "channel_electrons"):
        np.testing.assert_array_equal(initial["arrays"][key][:8], changed["arrays"][key][:8])
    assert trace(initial)["frames"][:8] == trace(changed)["frames"][:8]
    assert not np.array_equal(
        initial["arrays"]["raw_frames"][8:], changed["arrays"]["raw_frames"][8:]
    )


def test_statistics_reject_common_cells_and_honor_odd_crop_phase():
    policy = default_policy([4, 4])
    policy["awb"]["valid_signal_min"] = 0.01
    phase = np.tile(np.array([[2, 1], [1, 0]], dtype=np.int8), (2, 2))
    raw = np.choose(phase, [200, 400, 800]).astype(np.uint16)
    raw[0, 0] = 1023
    stats = raw_statistics(raw, phase, {"bit_depth": 10, "black_level_dn": 0}, policy)
    assert stats["awb"]["valid_cells"] == 3
    np.testing.assert_allclose(
        stats["awb"]["channel_mean_normalized"], np.array([200, 400, 800]) / 1023
    )
    policy["awb"]["minimum_valid_cells"] = 4
    insufficient = raw_statistics(raw, phase, {"bit_depth": 10, "black_level_dn": 0}, policy)
    assert insufficient["awb"]["channel_mean_normalized"] is None
    with pytest.raises(ValueError, match="one R"):
        raw_statistics(raw, np.ones_like(phase), {"bit_depth": 10, "black_level_dn": 0}, policy)


@pytest.mark.parametrize(
    "value, expected, clamped, rounded",
    [
        (1.25, 1.5, False, True),
        (0.9, 1.0, True, False),
        (3.0, 2.0, True, False),
        (1.5, 1.5, False, False),
    ],
)
def test_declared_rounding(value, expected, clamped, rounded):
    assert quantize(value, {"minimum": 1.0, "maximum": 2.0, "step": 0.5}) == {
        "value": expected,
        "clamped": clamped,
        "quantized": rounded,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["control_program"]["policy"]["ae"].update(minimum_delay_frames=0),
        lambda d: d["control_program"]["policy"]["awb"].update(valid_signal_min=0.99),
        lambda d: d["control_program"]["policy"]["ae"]["readout_gain"].update(step=0.3),
        lambda d: d["control_program"]["initial"].update(exposure_ns=1_000_001),
        lambda d: d["control_program"]["initial"].update(wb_gains_rgb=[1.0, 1.0, 20.0]),
        lambda d: d["control_program"]["initial"].update(exposure_ns=True),
        lambda d: d["control_program"]["policy"]["statistics_roi"].update(origin=[1, 0]),
        lambda d: d["control_program"]["policy"]["ae"].update(target_normalized=float("nan")),
        lambda d: d["control_program"]["policy"].update(unrecognized=True),
        lambda d: d["control_program"]["policy"]["latency"].update(actuator_ns=MAX_TIME_NS),
        lambda d: d["control_program"]["frames"][1].update(frame_start_ns=2_000_000),
        lambda d: d["scene_sequence"]["knots"][-1].update(time_ns=155_000_000),
        lambda d: d["control_program"]["clock"].update(clock_id="unrelated-clock"),
    ],
)
def test_invalid_program_preflight_refuses_before_capture(request_data, mutate, monkeypatch):
    mutate(request_data)
    monkeypatch.setattr(
        TemporalSceneRuntime, "_optical_rates", lambda *a: pytest.fail("preflight must refuse")
    )
    assert ClosedLoopSceneRuntime().validate_request(request_data)
    with pytest.raises(RuntimeRequestError):
        run(request_data)


def test_64_frames_and_json_serializable_evidence(request_data):
    p = request_data["control_program"]
    p["frames"] = [{"frame_id": f"f-{i}", "frame_start_ns": i * 10_000_000} for i in range(64)]
    request_data["scene_sequence"]["knots"][-1]["time_ns"] = 640_000_000
    out = run(request_data)
    json.dumps(out["summary"], allow_nan=False)
    assert out["arrays"]["raw_frames"].shape == (64, 16, 20)
    assert out["summary"]["readout_count"] == 64
    assert len(out["summary"]["control_program_sha256"]) == 64
    assert out["summary"]["physical_validation"] == "not_established"
    assert ClosedLoopProgram.model_validate(p).model_dump() == p
    assert CameraControlPolicy.model_validate(p["policy"]).model_dump() == p["policy"]
    assert ClosedLoopSceneRuntime().estimate_resources(request_data)["frames"] == 64


def test_simultaneous_eligible_commands_choose_latest_source_and_reject_stale_updates():
    frame = {"frame_id": "destination", "frame_start_ns": 100}
    mode = {"line_time_ns": 0, "hidden_top_rows": 0}
    state = {"gain": 1.0}
    sources = {kind: {"source_frame_index": -1, "command_id": None} for kind in ("ae", "awb")}
    commands = [
        {
            "command_id": f"c-{i}",
            "kind": "ae",
            "status": "pending",
            "source_frame_index": i,
            "earliest_frame_index": 3,
            "actuator_ready_ns": 80,
            "issued_at_ns": 60,
            "proposal": {"commanded": {"gain": float(i + 2)}},
        }
        for i in range(2)
    ]
    _apply_pending(commands, 3, frame, mode, state, sources)
    assert state["gain"] == 3
    assert [c["status"] for c in commands] == ["superseded", "applied"]
    stale = {**commands[0], "command_id": "late-old", "status": "pending"}
    _apply_pending([stale], 4, frame, mode, state, sources)
    assert stale["status"] == "stale" and state["gain"] == 3


def test_prepared_capture_isolated_from_input_and_output_mutations(request_data):
    data = recorded_request(request_data)
    session = TemporalSceneRuntime().prepare_capture(data)
    frame = deepcopy(data["control_sequence"]["frames"][0])
    reference = session.capture_frame(frame, seed_index=0)
    data["readout"]["full_well_e"] *= 2
    data["isp_profiles"]["authored-isp"]["white_balance"]["gains_rgb"] = [2.0, 2.0, 2.0]
    data["scene_sequence"]["knots"][0]["scene"]["source"]["source_uri"] = "changed://caller"
    reference["summary"]["geometry"]["crop_origin"][0] = 1000
    with pytest.raises(ValueError):
        reference["arrays"]["channel_photoelectron_rate_scenes"][...] = 0
    with pytest.raises(ValueError):
        reference["arrays"]["channel_photoelectron_rate_scenes"].flags.writeable = True
    again = session.capture_frame(frame, seed_index=0)
    for key in ("raw_frames", "rgb_frames", "channel_electrons"):
        np.testing.assert_array_equal(reference["arrays"][key], again["arrays"][key])
    assert again["summary"]["geometry"]["crop_origin"] == [0, 0]
    assert again["summary"]["scene_sources"][0].get("source_uri") != "changed://caller"


def test_prepared_capture_rejects_unresolved_inline_command_profile(request_data):
    data = recorded_request(request_data)
    session = TemporalSceneRuntime().prepare_capture(data)
    frame = deepcopy(data["control_sequence"]["frames"][0])
    frame["commanded"] = {
        "command_id": "unused",
        "issued_at_ns": 0,
        "exposure_ns": 1_000_000,
        "gain": 1,
        "isp_model_id": "absent",
    }
    with pytest.raises(ValueError, match="ISP absent"):
        session.capture_frame(frame, seed_index=0)


def test_public_python_runtime_dispatch(request_data):
    from camera_platform.runtime import get_plugin, run_plugin

    assert get_plugin("closed_loop_scene_v1").describe_capabilities()["limits"]["max_frames"] == 64
    assert (
        run_plugin("closed_loop_scene_v1", request_data)["summary"]["plugin_id"]
        == "closed_loop_scene_v1"
    )


@pytest.mark.parametrize("extra_ns, applied_index", [(0, 1), (1, 2)])
def test_exact_latch_deadline_is_integer_and_never_rounded_early(
    request_data, extra_ns, applied_index
):
    p = request_data["control_program"]["policy"]
    p["awb"]["enabled"] = False
    p["latency"]["actuator_ns"] = 9_000_000 + extra_ns
    out = run(request_data)
    assert trace(out)["commands"][0]["applied_frame_index"] == applied_index


def test_controller_uses_raw_not_expected_charge_or_prediction(request_data, monkeypatch):
    import camera_platform.temporal as temporal

    baseline = run(request_data)
    real_capture = temporal.TemporalCaptureSession.capture_frame
    calls = []

    def altered_expected_output(self, frame, **kwargs):
        output = real_capture(self, frame, **kwargs)
        calls.append(frame["frame_id"])
        output["arrays"]["mean_electrons"][...] = 0
        output["arrays"]["channel_electrons"][...] = 0
        output["summary"]["prediction"]["values"] = [0, 0, 0]
        return output

    monkeypatch.setattr(temporal.TemporalCaptureSession, "capture_frame", altered_expected_output)
    altered = run(request_data)
    assert trace(altered) == trace(baseline)
    assert len(calls) == len(request_data["control_program"]["frames"])
    np.testing.assert_array_equal(altered["arrays"]["raw_frames"], baseline["arrays"]["raw_frames"])


def test_large_integer_clock_translation_preserves_applied_control_and_pixels(request_data):
    baseline = run(request_data)
    shift = 8_000_000_000_000_000
    for frame in request_data["control_program"]["frames"]:
        frame["frame_start_ns"] += shift
    for knot in request_data["scene_sequence"]["knots"]:
        knot["time_ns"] += shift
    translated = run(request_data)
    for key in ("raw_frames", "rgb_frames"):
        np.testing.assert_array_equal(baseline["arrays"][key], translated["arrays"][key])
    assert [f["applied"] for f in trace(baseline)["frames"]] == [
        f["applied"] for f in trace(translated)["frames"]
    ]
    assert trace(translated)["commands"][0]["issued_at_ns"] == shift + 1_000_000


def test_awb_tracks_a_spectral_step_with_distinct_channel_responses(request_data):
    data = request_data
    wave = np.asarray(data["wavelength_nm"])
    data["qe"] = [
        (0.6 * np.exp(-0.5 * ((wave - center) / width) ** 2)).tolist()
        for center, width in [(620, 50), (540, 45), (460, 40)]
    ]
    scene = deepcopy(data["scene_sequence"]["knots"][0]["scene"])
    scene["spectral_irradiance"] *= 3
    changed = deepcopy(scene)
    changed["spectral_irradiance"] *= np.array([1.6, 1.2, 1, 0.8, 0.5])
    data["scene_sequence"].update(
        interpolation="zero_order_hold",
        knots=[
            {"time_ns": 0, "scene": scene},
            {"time_ns": 80_000_000, "scene": changed},
            {"time_ns": 160_000_000, "scene": changed},
        ],
    )
    data["control_program"]["initial"]["exposure_ns"] = 8_000_000
    data["control_program"]["policy"]["ae"]["enabled"] = False
    output = run(data)
    before = trace(output)["frames"][7]["applied"]["wb_gains_rgb"]
    after = trace(output)["frames"][-1]["applied"]["wb_gains_rgb"]
    assert after[0] > before[0] and after[2] < before[2]
    mean = output["arrays"]["white_balanced_rgb_frames"][-1].mean(axis=(0, 1))
    np.testing.assert_allclose(mean / mean[1], [1, 1, 1], atol=0.02, rtol=0)


def test_unreachable_actuator_grid_holds_without_claiming_target_or_command(request_data):
    p = request_data["control_program"]["policy"]
    p["ae"]["exposure_ns"] = {"minimum": 1_000_000, "maximum": 1_000_000, "step": 100_000}
    p["ae"]["readout_gain"] = {"minimum": 1.0, "maximum": 1.0, "step": 0.125}
    p["awb"]["enabled"] = False
    output = run(request_data)
    frames = trace(output)["frames"]
    assert trace(output)["commands"] == []
    assert all(f["control_proposals"]["ae"]["status"] == "actuator_grid_hold" for f in frames)
    assert all(f["control_proposals"]["ae"]["quantized_candidate"]["gain"] == 1 for f in frames)
    assert all("issued_at_ns" not in f["declared_events"] for f in frames)
    assert all("actuator_ready_ns" not in f["declared_events"] for f in frames)
    assert frames[-1]["statistics"]["ae"]["value"] < 0.01

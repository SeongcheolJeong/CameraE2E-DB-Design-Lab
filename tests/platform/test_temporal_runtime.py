"""Analytic authored timeline fixtures, never hardware or physical accuracy evidence."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.isp import default_profile
from camera_platform.runtime import RuntimeRequestError
from camera_platform.spatial import SpatialSceneRuntime, photoelectron_rate
from camera_platform.temporal import TemporalSceneRuntime, request_template


@pytest.fixture
def request_data():
    data = request_template()
    data["noise_enabled"] = False
    data["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = np.full((16, 20, 5), 1e-4)
        knot["scene"]["sampling"]["full_sensor_shape"] = [16, 20]
    return data


def run(data):
    return TemporalSceneRuntime().execute(data)


def spatial_request(data, scene=None):
    request = {key: value for key, value in data.items()
               if key not in {"scene_sequence", "control_sequence", "isp_profiles"}}
    request["scene"] = scene or data["scene_sequence"]["knots"][0]["scene"]
    return request


def single_frame(data, *, start=0, exposure=4_000_000):
    frame = data["control_sequence"]["frames"][0]
    data["control_sequence"]["frames"] = [frame]
    frame["frame_start_ns"] = start
    frame["applied"]["exposure_ns"] = exposure
    return frame


def test_public_rate_has_no_readout_or_synthetic_one_second_bound(request_data, monkeypatch):
    request = spatial_request(request_data)
    request["scene"]["spectral_irradiance"] *= 1e10
    import camera_platform.spatial as spatial

    monkeypatch.setattr(spatial, "_capture", lambda *args: pytest.fail("rate must not read out"))
    result = photoelectron_rate(request)
    assert result["arrays"]["photoelectron_rate"].max() > 1e13
    assert "raw_frames" not in result["arrays"]
    assert set(result["summary"]["array_units"]) == set(result["arrays"])
    assert "mean_dark_charge" not in result["summary"]["included_effects"]
    with pytest.raises(ValueError, match="Unknown"):
        photoelectron_rate({**request, "exposure_s": 1})


@pytest.mark.parametrize("noise", [False, True])
def test_static_sequence_equivalent_to_spatial_capture_and_isp(request_data, noise):
    request_data["noise_enabled"] = noise
    request_data["readout"]["dark_current_e_per_s"] = 120
    temporal = run(request_data)
    spatial = SpatialSceneRuntime().execute({**spatial_request(request_data),
        "exposure_s": .004, "frames": 2, "isp_profile": default_profile()})
    np.testing.assert_array_equal(temporal["arrays"]["raw_frames"], spatial["arrays"]["raw_frames"])
    np.testing.assert_array_equal(temporal["arrays"]["rgb_frames"], spatial["arrays"]["rgb_frames"])
    for index in range(2):
        np.testing.assert_allclose(temporal["arrays"]["mean_electrons"][index],
                                   spatial["arrays"]["mean_electrons"], rtol=2e-15)
    assert temporal["summary"]["readout_count"] == 2
    assert "raw_temporal_variance_dn2" not in temporal["arrays"]
    assert "raw_temporal_snr_db" not in temporal["summary"]


def test_linear_ramp_integral_uses_row_windows_native_crop_and_integer_clock(request_data):
    data = request_data
    data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 3
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[40, 50], crop_origin=[3, 5])
    frame = single_frame(data, start=1_000_000, exposure=3_000_000)
    data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=100_000,
                                             hidden_top_rows=2)
    output = run(data)
    rates = photoelectron_rate(spatial_request(data))["arrays"]["channel_photoelectron_rate"]
    starts = 1_000_000 + (2 + 3 + np.arange(16)) * 100_000
    expected = rates * .003 * (1 + 2 * (starts + 1_500_000) / 20_000_000)[:, None, None]
    np.testing.assert_allclose(output["arrays"]["channel_electrons"][0], expected, rtol=3e-15)
    np.testing.assert_array_equal(output["arrays"]["row_start_ns"][0], starts)
    assert output["arrays"]["cfa_channel_index"][0, 0] == 2
    shift = 8_000_000_000_000_000
    for knot in data["scene_sequence"]["knots"]:
        knot["time_ns"] += shift
    frame["frame_start_ns"] += shift
    frame["applied"]["applied_at_ns"] += shift
    shifted = run(data)
    np.testing.assert_array_equal(shifted["arrays"]["channel_electrons"],
                                  output["arrays"]["channel_electrons"])
    np.testing.assert_array_equal(shifted["arrays"]["raw_frames"], output["arrays"]["raw_frames"])


def test_zoh_step_half_open_endpoint_and_exact_partial_interval(request_data):
    data = request_data
    first = data["scene_sequence"]["knots"][0]["scene"]
    bright = deepcopy(first)
    bright["spectral_irradiance"] *= 5
    data["scene_sequence"].update(interpolation="zero_order_hold", knots=[
        {"time_ns": 0, "scene": first}, {"time_ns": 2_000_000, "scene": bright},
        {"time_ns": 10_000_000, "scene": first}])
    single_frame(data, start=1_000_000, exposure=4_000_000)
    output = run(data)
    rate = photoelectron_rate(spatial_request(data))["arrays"]["channel_photoelectron_rate"]
    np.testing.assert_allclose(output["arrays"]["channel_electrons"][0],
                               rate * (.001 + 5 * .003), rtol=2e-15)
    single_frame(data, start=0, exposure=2_000_000)
    endpoint = run(data)
    np.testing.assert_allclose(endpoint["arrays"]["channel_electrons"][0], rate * .002)


def test_applied_exposure_gain_and_wb_change_correct_stage_only(request_data):
    data = request_data
    baseline = run(data)
    frame = data["control_sequence"]["frames"][1]
    frame["applied"].update(applied_at_ns=10_000_000, exposure_ns=2_000_000, gain=2)
    tuned = run(data)
    np.testing.assert_allclose(tuned["arrays"]["channel_electrons"][1],
                               baseline["arrays"]["channel_electrons"][1] / 2)
    np.testing.assert_array_equal(tuned["arrays"]["raw_frames"], baseline["arrays"]["raw_frames"])
    data["isp_profiles"]["red-isp"] = default_profile()
    data["isp_profiles"]["red-isp"]["white_balance"]["gains_rgb"] = [2, 1, 1]
    frame["applied"]["isp_model_id"] = "red-isp"
    wb = run(data)
    np.testing.assert_array_equal(wb["arrays"]["raw_frames"], tuned["arrays"]["raw_frames"])
    np.testing.assert_array_equal(wb["arrays"]["rgb_frames"][0], tuned["arrays"]["rgb_frames"][0])
    assert (wb["arrays"]["rgb_frames"][1, ..., 0].mean()
            > tuned["arrays"]["rgb_frames"][1, ..., 0].mean())


def test_commanded_values_are_not_implicitly_applied_and_delays_preserved(request_data):
    data = request_data
    baseline = run(data)
    first, second = data["control_sequence"]["frames"]
    first["commanded"] = {"command_id": "command-7", "issued_at_ns": 500_000,
                           "exposure_ns": 1_000_000, "gain": 4, "isp_model_id": "authored-isp"}
    second["applied"].update(command_id="command-7", applied_at_ns=10_000_000,
                             exposure_ns=2_000_000)
    output = run(data)
    np.testing.assert_array_equal(output["arrays"]["raw_frames"][0],
                                  baseline["arrays"]["raw_frames"][0])
    np.testing.assert_allclose(output["arrays"]["mean_electrons"][1],
                               baseline["arrays"]["mean_electrons"][1] / 2)
    assert output["summary"]["timing"]["frames"][1]["command_to_applied_ns"] == 9_500_000
    assert output["summary"]["timing"]["frames"][1]["command_arrival_ns"] is None
    assert output["summary"]["control_sequence"]["frames"][0]["commanded"]["gain"] == 4


def test_added_exact_knots_do_not_add_read_noise_or_change_seeded_capture(request_data):
    data = request_data
    data["noise_enabled"] = True
    initial = run(data)
    scene = data["scene_sequence"]["knots"][0]["scene"]
    data["scene_sequence"]["knots"] = [{"time_ns": t, "scene": deepcopy(scene)}
                                       for t in range(0, 20_000_001, 250_000)]
    refined = run(data)
    np.testing.assert_array_equal(initial["arrays"]["raw_frames"], refined["arrays"]["raw_frames"])
    assert refined["summary"]["readout_count"] == 2
    assert refined["arrays"]["channel_photoelectron_rate_scenes"].shape[0] == 1
    assert not np.array_equal(refined["arrays"]["raw_frames"][0],
                              refined["arrays"]["raw_frames"][1])
    assert refined["summary"]["seed_sequence"] == [223, 224]


def test_piecewise_linear_knot_refinement_preserves_declared_ramp(request_data):
    data = request_data
    data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 3
    initial = run(data)
    scene = deepcopy(data["scene_sequence"]["knots"][0]["scene"])
    scene["spectral_irradiance"] *= 1.7
    data["scene_sequence"]["knots"].insert(1, {"time_ns": 7_000_000, "scene": scene})
    refined = run(data)
    np.testing.assert_allclose(initial["arrays"]["mean_electrons"],
                               refined["arrays"]["mean_electrons"], rtol=3e-15)
    np.testing.assert_array_equal(initial["arrays"]["raw_frames"], refined["arrays"]["raw_frames"])


def test_gaussian_translation_timeline_and_psf_have_analytic_discrete_moments(request_data):
    data = request_data
    template = data["scene_sequence"]["knots"][0]["scene"]
    template["sampling"]["full_sensor_shape"] = [16, 64]
    knots = []
    for index, center in enumerate(range(20, 33)):
        scene = deepcopy(template)
        gaussian = 1e-3 * np.exp(-.5 * ((np.arange(64) - center) / 2)**2)
        scene["spectral_irradiance"] = np.broadcast_to(gaussian[None, :, None], (16, 64, 5)).copy()
        knots.append({"time_ns": index * 1_000_000, "scene": scene})
    data["scene_sequence"].update(interpolation="zero_order_hold", knots=knots)
    data["psf"]["values"] = np.tile(np.array([[[.25, .5, .25]]]), (5, 1, 1))
    first, second = data["control_sequence"]["frames"]
    first["applied"]["exposure_ns"] = 6_000_000
    second["frame_start_ns"] = 6_000_000
    second["applied"] = deepcopy(first["applied"])
    output = run(data)
    x = np.arange(64)
    for index, center in enumerate((22.5, 28.5)):
        line = output["arrays"]["channel_electrons"][index, 8, :, 1]
        centroid = (x * line).sum() / line.sum()
        variance = ((x - centroid)**2 * line).sum() / line.sum()
        assert centroid == pytest.approx(center, abs=1e-11)
        assert variance == pytest.approx(4 + .5 + np.var(np.arange(6)), abs=1e-10)
    assert not np.array_equal(output["arrays"]["raw_frames"][0], output["arrays"]["raw_frames"][1])
    assert not np.array_equal(output["arrays"]["rgb_frames"][0], output["arrays"]["rgb_frames"][1])


def test_one_readout_after_integration_clips_total_charge_and_adds_dark_once(request_data):
    data = request_data
    data["readout"].update(full_well_e=100, dark_current_e_per_s=1000)
    output = run(data)
    assert output["arrays"]["raw_frames"].max() == 4095
    assert output["summary"]["readout_count"] == 2
    assert not all(output["summary"]["linear_unsaturated_per_frame"])
    # Well clipping is after the combined temporal photocharge + one exposure dark term.
    expected_baseline = 64 + 4 * (4095 - 64) / 100
    np.testing.assert_allclose(output["arrays"]["channel_reference_signal_dn"],
                               4095 - expected_baseline)


def test_moving_edge_rolling_rows_integrate_before_one_readout_each(request_data, monkeypatch):
    import camera_platform.temporal as temporal

    data = request_data
    first = data["scene_sequence"]["knots"][0]["scene"]
    first["spectral_irradiance"][:] = 0
    first["spectral_irradiance"][:, 7:] = 1e-4
    second = deepcopy(first)
    second["spectral_irradiance"][:, :11] = 0
    data["scene_sequence"].update(interpolation="zero_order_hold", knots=[
        {"time_ns": 0, "scene": first}, {"time_ns": 2_000_000, "scene": second},
        {"time_ns": 10_000_000, "scene": second}])
    data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=100_000)
    single_frame(data, start=1_000_000, exposure=2_000_000)
    observed = []
    original = temporal._capture

    def capture(inputs, mean):
        observed.append((inputs["frames"], inputs["exposure"], mean.copy()))
        return original(inputs, mean)

    monkeypatch.setattr(temporal, "_capture", capture)
    output = run(data)
    rate = photoelectron_rate(spatial_request(data))["arrays"]["channel_photoelectron_rate"]
    seconds_before_step = np.maximum(1_000_000 - np.arange(16) * 100_000, 0) * 1e-9
    np.testing.assert_allclose(output["arrays"]["channel_electrons"][0, :, 8, 1],
                               rate[:, 8, 1] * seconds_before_step, atol=1e-12)
    assert len(observed) == 1
    assert observed[0][:2] == (1, .002)
    np.testing.assert_array_equal(observed[0][2], output["arrays"]["mean_electrons"][0])
    # Compare the same scene and exposure with simultaneous rows: rolling capture
    # changes actual RAW and RGB, not only the timestamp table.
    data["control_sequence"]["mode"].update(shutter="global", line_time_ns=0)
    global_output = run(data)
    assert not np.array_equal(output["arrays"]["raw_frames"],
                              global_output["arrays"]["raw_frames"])
    assert not np.array_equal(output["arrays"]["rgb_frames"],
                              global_output["arrays"]["rgb_frames"])


def test_missing_short_pulse_knots_are_not_synthesized_by_uniform_sampling(request_data):
    data = request_data
    first = data["scene_sequence"]["knots"][0]["scene"]
    first["spectral_irradiance"][:] = 0
    pulse = deepcopy(first)
    pulse["spectral_irradiance"][:] = 1
    data["scene_sequence"].update(interpolation="zero_order_hold", knots=[
        {"time_ns": 0, "scene": first}, {"time_ns": 1_000_001, "scene": pulse},
        {"time_ns": 1_000_002, "scene": first}, {"time_ns": 20_000_000, "scene": first}])
    single_frame(data)
    output = run(data)
    rate = photoelectron_rate(spatial_request(data, pulse))["arrays"]["channel_photoelectron_rate"]
    np.testing.assert_allclose(output["arrays"]["channel_electrons"][0], rate * 1e-9,
                               rtol=2e-15)


def test_sequence_summary_and_intermediates_are_finite_self_describing_and_immutable(request_data):
    before = deepcopy(request_data)
    output = run(request_data)
    summary, arrays = output["summary"], output["arrays"]
    assert set(summary["array_units"]) == set(arrays)
    assert all(np.isfinite(value).all() for value in arrays.values())
    assert summary["prediction"]["metric_id"] == "raw.channel_mean_dn_sequence"
    assert summary["prediction"]["axes"][0]["values"] == summary["frame_ids"]
    assert len(summary["prediction"]["values"]) == 6
    assert summary["prediction"]["uncertainty"] == {"kind": "unknown"}
    assert summary["source_kind"] == "simulated"
    json.dumps(summary, allow_nan=False)
    np.testing.assert_array_equal(before["scene_sequence"]["knots"][0]["scene"]["spectral_irradiance"],
        request_data["scene_sequence"]["knots"][0]["scene"]["spectral_irradiance"])


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(exposure_s=.004),
    lambda d: d.update(frames=2),
    lambda d: d.update(output_size=[8, 8]),
    lambda d: d["scene_sequence"].update(interpolation="linear_rgb"),
    lambda d: d["scene_sequence"]["knots"][1].update(time_ns=0),
    lambda d: d["scene_sequence"]["knots"][0].update(time_ns=1),
    lambda d: d["scene_sequence"]["knots"][1]["scene"]["sampling"].update(crop_origin=[1, 0]),
    lambda d: d["scene_sequence"]["knots"][1]["scene"].update(wavelength_nm=[400, 500]),
    lambda d: d["control_sequence"]["frames"][1].update(frame_start_ns=1),
    lambda d: d["control_sequence"]["frames"][0]["applied"].update(exposure_ns=0),
    lambda d: d["control_sequence"]["frames"][0]["applied"].update(gain=float("nan")),
    lambda d: d["control_sequence"]["frames"][0]["applied"].update(applied_at_ns=1),
    lambda d: d["control_sequence"]["clock"].update(clock_id="another-clock"),
    lambda d: d["isp_profiles"].clear(),
    lambda d: d["isp_profiles"].update(extra=default_profile()),
])
def test_invalid_or_ambiguous_temporal_contract_is_refused(request_data, mutation):
    # Template shares its authored clock object; detach before intentional mismatch.
    request_data["control_sequence"]["clock"] = deepcopy(request_data["control_sequence"]["clock"])
    mutation(request_data)
    plugin = TemporalSceneRuntime()
    assert plugin.validate_request(request_data)
    with pytest.raises(RuntimeRequestError):
        plugin.execute(request_data)


def test_declared_knots_budget_never_silently_coarsened(request_data):
    scene = request_data["scene_sequence"]["knots"][0]["scene"]
    request_data["scene_sequence"]["knots"] = [{"time_ns": index * 1_000_000, "scene": scene}
                                              for index in range(129)]
    assert "128" in TemporalSceneRuntime().validate_request(request_data)[0]["message"]


def test_unique_input_and_frame_budgets(request_data, monkeypatch):
    import camera_platform.temporal as temporal

    request_data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 2
    samples = 16 * 20 * 5
    monkeypatch.setattr(temporal, "MAX_UNIQUE_SPECTRAL_SAMPLES", samples)
    assert "spectral samples" in TemporalSceneRuntime().validate_request(request_data)[0]["message"]
    request_data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] /= 2
    assert TemporalSceneRuntime().validate_request(request_data) == []
    estimate = TemporalSceneRuntime().estimate_resources(request_data)
    assert estimate["unique_spectral_samples"] == samples
    assert estimate["readouts"] == 2
    assert estimate["automatic_coarsening"] is False


def test_integrated_unbounded_charge_rejected_before_engine_sampling(request_data, monkeypatch):
    import camera_platform.temporal as temporal

    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 1e12
    monkeypatch.setattr(temporal, "_capture", lambda *args: pytest.fail("unbounded readout"))
    with pytest.raises(RuntimeRequestError, match="Integrated charge"):
        run(request_data)

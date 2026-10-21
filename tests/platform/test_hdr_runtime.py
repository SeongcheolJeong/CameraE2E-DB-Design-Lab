"""Integrated authored-light to electrical RAW to bounded HDR, without hardware claims."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical, hdr, nonlinear_examples
from camera_platform.hdr_runtime import (
    HDRBracketRuntime,
    capture_effects,
    default_capture_program,
    validate_capture_program,
)
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime, request_template


@pytest.fixture
def request_data():
    data = request_template()
    data["noise_enabled"] = False
    data["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    scene = data["scene_sequence"]["knots"][0]["scene"]
    scene["spectral_irradiance"] = np.full((8, 10, 5), 1e-4)
    scene["sampling"]["full_sensor_shape"] = [8, 10]
    data["scene_sequence"]["knots"][1]["scene"] = deepcopy(scene)
    data["control_sequence"]["frames"][0]["applied"]["exposure_ns"] = 2_000_000
    data["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 8_000_000
    data["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 10_000_000
    data["capture_program"] = default_capture_program(data["readout"])
    return data


def run(data):
    return HDRBracketRuntime().execute(data)


def waveform(data, *, kind="pwm", origin=0):
    clock = data["control_sequence"]["clock"]["clock_id"]
    if kind == "constant":
        return {"schema": "camera_light_waveform_v1", "kind": kind, "clock_id": clock, "level": 0.5}
    return {
        "schema": "camera_light_waveform_v1",
        "kind": kind,
        "clock_id": clock,
        "period_ns": 1_000_000,
        "on_time_ns": 500_000,
        "phase_origin_ns": origin,
        "low": 0,
        "high": 1,
    }


def test_program_normalization_is_strict_independent_and_effects_are_actual(request_data):
    program = request_data["capture_program"]
    selected = validate_capture_program(program)
    assert selected == program and selected is not program
    selected["electrical_model"]["digital_gain"] = 2
    assert program["electrical_model"]["digital_gain"] == 1
    assert "raw_hdr_bracket_merge" in capture_effects(program)
    assert "periodic_light_modulation" not in capture_effects(program)
    program["hdr_merge"] = None
    assert "raw_hdr_bracket_merge" not in capture_effects(program)
    program["waveform"] = waveform(request_data)
    assert "periodic_light_modulation" in capture_effects(program)
    for bad in (
        None,
        {},
        {**program, "ambient": 1},
        {**program, "schema": "other"},
        {k: v for k, v in program.items() if k != "waveform"},
    ):
        with pytest.raises(ValueError):
            validate_capture_program(bad)


def test_actual_raw_merges_and_is_retained_with_serializable_bounded_summary(request_data):
    output = run(request_data)
    arrays, summary = output["arrays"], output["summary"]
    assert arrays["raw_frames"].dtype.kind in "iu"
    assert arrays["raw_frames"].shape == (2, 8, 10)
    assert arrays["rgb_frames"].shape == (2, 8, 10, 3)
    np.testing.assert_array_equal(arrays["rgb"], arrays["hdr_rgb"])
    assert summary["hdr_merge_applied"] and summary["readout_count"] == 2
    assert summary["hdr_merge"]["valid_pixels"] == 80
    assert set(arrays) == set(summary["array_units"])
    assert len(summary["timing"]["frames"]) == len(summary["frames"]) == 2
    assert summary["frames"][1]["frame_id"] == summary["frame_ids"][1]
    assert summary["simulation_oracle"]["used_by_hdr_merge"] is False
    assert summary["physical_validation"] == "not_established"
    json.dumps(summary, allow_nan=False)
    estimate = HDRBracketRuntime().estimate_resources(request_data)
    assert estimate["readouts"] == 2 and estimate["unique_scenes"] == 1
    assert estimate["working_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())


def test_actual_raw_prediction_uses_signed_declared_dark_correction(request_data):
    request_data["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1234
    request_data["noise_enabled"] = True
    output = run(request_data)
    arrays, summary = output["arrays"], output["summary"]
    expected = []
    for index, cal in enumerate(summary["calibrations"]):
        corrected = (
            arrays["raw_frames"][index].astype(float)
            - cal["black_level_dn"]
            - arrays["calibration_dark_e"][index] * cal["dn_per_e"]
        )
        expected.extend(float(corrected[arrays["cfa_channel_index"] == c].mean()) for c in range(3))
    assert summary["prediction"]["values"] == expected
    assert summary["prediction"]["method_and_version"] == "sampled_raw_dark_corrected_v1"
    assert np.isfinite(summary["prediction"]["values"]).all()


def test_applied_analog_and_program_digital_gain_change_raw_at_different_stages(request_data):
    request_data["capture_program"]["hdr_merge"] = None
    baseline = run(request_data)
    request_data["control_sequence"]["frames"][0]["applied"]["gain"] = 2
    analog = run(request_data)
    np.testing.assert_array_equal(
        baseline["arrays"]["mean_electrons"], analog["arrays"]["mean_electrons"]
    )
    assert not np.array_equal(
        baseline["arrays"]["raw_frames"][0], analog["arrays"]["raw_frames"][0]
    )
    np.testing.assert_array_equal(
        baseline["arrays"]["raw_frames"][1], analog["arrays"]["raw_frames"][1]
    )
    request_data["control_sequence"]["frames"][0]["applied"]["gain"] = 1
    request_data["capture_program"]["electrical_model"]["digital_gain"] = 2
    digital = run(request_data)
    np.testing.assert_array_equal(
        baseline["arrays"]["electrical_adc_dn"], digital["arrays"]["electrical_adc_dn"]
    )
    assert not np.array_equal(baseline["arrays"]["raw_frames"], digital["arrays"]["raw_frames"])
    assert digital["summary"]["hdr_merge"] is None
    np.testing.assert_array_equal(digital["arrays"]["rgb"], digital["arrays"]["rgb_frames"][0])


def test_arbitrary_pwm_phase_changes_electrons_raw_and_native_rolling_rows(request_data):
    program = request_data["capture_program"]
    program["hdr_merge"] = None
    program["waveform"] = waveform(request_data)
    control = request_data["control_sequence"]
    control["mode"].update(shutter="rolling", line_time_ns=250_000)
    control["frames"][0]["applied"]["exposure_ns"] = 250_000
    control["frames"][1]["applied"]["exposure_ns"] = 250_000
    first = run(request_data)
    np.testing.assert_allclose(
        first["arrays"]["waveform_row_exposure_ratio"][0], [1, 1, 0, 0, 1, 1, 0, 0]
    )
    program["waveform"]["phase_origin_ns"] = 500_000
    shifted = run(request_data)
    np.testing.assert_allclose(
        shifted["arrays"]["waveform_row_exposure_ratio"][0], [0, 0, 1, 1, 0, 0, 1, 1]
    )
    assert not np.array_equal(first["arrays"]["raw_frames"], shifted["arrays"]["raw_frames"])
    assert np.all(first["arrays"]["mean_electrons"][0, 2:4] == 0)
    assert not first["summary"]["waveform"]["on_time_correction"]


def test_whole_cycle_pwm_hdr_is_static_phase_invariant_and_matches_average_light(request_data):
    request_data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=100_000)
    request_data["capture_program"]["waveform"] = waveform(request_data)
    first = run(request_data)
    request_data["capture_program"]["waveform"]["phase_origin_ns"] = 317_123
    shifted = run(request_data)
    np.testing.assert_allclose(first["arrays"]["waveform_row_exposure_ratio"], 0.5)
    np.testing.assert_array_equal(first["arrays"]["raw_frames"], shifted["arrays"]["raw_frames"])
    np.testing.assert_array_equal(
        first["arrays"]["hdr_merged_rate"], shifted["arrays"]["hdr_merged_rate"]
    )
    request_data["capture_program"]["waveform"] = waveform(request_data, kind="constant")
    average = run(request_data)
    np.testing.assert_array_equal(first["arrays"]["raw_frames"], average["arrays"]["raw_frames"])
    np.testing.assert_array_equal(
        first["arrays"]["hdr_merged_rate"], average["arrays"]["hdr_merged_rate"]
    )
    assert first["summary"]["hdr_merge"]["temporal_inconsistent_pixels"] == 0


def test_hdr_only_receives_raw_and_declared_calibrations_not_sensor_truth(
    request_data, monkeypatch
):
    real_merge, real_capture = hdr.merge_raw_bracket, electrical.capture_electrons
    seen = []

    def altered_oracle(*args, **kwargs):
        output = real_capture(*args, **kwargs)
        output["arrays"]["mean_total_charge_e"][:] = 999_999
        output["arrays"]["well_saturated_mask"][:] = True
        return output

    def checked_merge(raw, exposure_s, calibrations, **kwargs):
        assert raw.dtype.kind in "iu"
        assert all(set(cal) == hdr.CALIBRATION_FIELDS for cal in calibrations)
        seen.append(raw.copy())
        return real_merge(raw, exposure_s, calibrations, **kwargs)

    original = run(request_data)
    monkeypatch.setattr(electrical, "capture_electrons", altered_oracle)
    monkeypatch.setattr(hdr, "merge_raw_bracket", checked_merge)
    output = run(request_data)
    assert len(seen) == 1
    np.testing.assert_array_equal(output["arrays"]["raw_frames"], seen[0])
    np.testing.assert_array_equal(
        original["arrays"]["hdr_merged_rate"], output["arrays"]["hdr_merged_rate"]
    )
    assert original["summary"]["prediction"] == output["summary"]["prediction"]


def test_full_sequence_validation_refuses_overlap_even_without_hdr(request_data, monkeypatch):
    request_data["capture_program"]["hdr_merge"] = None
    request_data["control_sequence"]["frames"][1]["frame_start_ns"] = 1_000_000
    request_data["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 1_000_000
    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("captured"))
    assert HDRBracketRuntime().validate_request(request_data)
    with pytest.raises(RuntimeRequestError, match="overlap"):
        run(request_data)


@pytest.mark.parametrize(
    "violation",
    [
        "waveform_clock",
        "scene_clock",
        "coverage",
        "geometry",
        "unknown_field",
        "missing_program",
        "adc_format",
    ],
)
def test_mismatched_clocks_coverage_geometry_and_format_are_refused(request_data, violation):
    data = request_data
    if violation == "waveform_clock":
        data["capture_program"]["waveform"] = {**waveform(data), "clock_id": "other"}
    elif violation == "scene_clock":
        data["scene_sequence"]["clock"] = {**data["scene_sequence"]["clock"], "clock_id": "other"}
    elif violation == "coverage":
        data["scene_sequence"]["knots"][-1]["time_ns"] = 11_000_000
    elif violation == "geometry":
        data["scene_sequence"]["knots"][1]["scene"]["sampling"]["crop_origin"] = [1, 0]
    elif violation == "unknown_field":
        data["extra"] = True
    elif violation == "missing_program":
        data.pop("capture_program")
    else:
        data["capture_program"]["electrical_model"]["adc"]["black_level_dn"] += 1
    assert HDRBracketRuntime().validate_request(data)
    with pytest.raises(RuntimeRequestError):
        run(data)


@pytest.mark.parametrize("violation", ["one_frame", "scene_change", "partial_pwm_cycle"])
def test_hdr_stationarity_constraints_refuse_but_diagnostic_capture_can_run(
    request_data, violation
):
    data = request_data
    if violation == "one_frame":
        data["control_sequence"]["frames"] = data["control_sequence"]["frames"][:1]
    elif violation == "scene_change":
        data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 2
    else:
        data["capture_program"]["waveform"] = waveform(data)
        data["control_sequence"]["frames"][0]["applied"]["exposure_ns"] = 1_250_000
    with pytest.raises(RuntimeRequestError):
        run(data)
    data["capture_program"]["hdr_merge"] = None
    output = run(data)
    assert not output["summary"]["hdr_merge_applied"]
    assert "hdr_merged_rate" not in output["arrays"]


def test_native_crop_preserves_cfa_phase_and_fixed_patterns_across_exposures(request_data):
    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[12, 14], crop_origin=[1, 3])
    fixed = request_data["capture_program"]["electrical_model"]["fixed_pattern"]
    fixed.update(prnu_sigma=0.02, dsnu_e_per_s=2, seed=129)
    output = run(request_data)
    assert output["arrays"]["cfa_channel_index"][0, 0] == 2
    np.testing.assert_array_equal(
        output["arrays"]["electrical_photo_response_map"][0],
        output["arrays"]["electrical_photo_response_map"][1],
    )
    np.testing.assert_array_equal(
        output["arrays"]["electrical_dark_current_e_per_s_map"][0],
        output["arrays"]["electrical_dark_current_e_per_s_map"][1],
    )


def test_noise_is_seeded_per_independent_frame_and_legacy_capture_is_not_called(
    request_data, monkeypatch
):
    import camera_platform.spatial as spatial

    monkeypatch.setattr(spatial, "_capture", lambda *a, **k: pytest.fail("legacy readout"))
    request_data["noise_enabled"] = True
    request_data["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 2_000_000
    first = run(request_data)
    second = run(request_data)
    np.testing.assert_array_equal(first["arrays"]["raw_frames"], second["arrays"]["raw_frames"])
    assert not np.array_equal(first["arrays"]["raw_frames"][0], first["arrays"]["raw_frames"][1])
    assert first["summary"]["seed_sequence"] == [223, 224]


def test_all_censored_hdr_keeps_nan_masks_but_prediction_and_preview_finite(request_data):
    request_data["capture_program"]["electrical_model"]["full_well_e"] = 1
    output = run(request_data)
    assert not output["arrays"]["hdr_valid_mask"].any()
    assert np.isnan(output["arrays"]["hdr_merged_rate"]).all()
    assert np.isfinite(output["arrays"]["rgb"]).all()
    json.dumps(output["summary"], allow_nan=False)


def test_source_description_difference_does_not_invent_motion_for_same_optical_image(request_data):
    request_data["scene_sequence"]["knots"][1]["scene"]["source"]["description"] = (
        "Second preserved source for the identical declared image"
    )
    output = run(request_data)
    assert output["summary"]["hdr_merge_applied"]
    assert len(output["summary"]["scene_sources"]) == 2


def test_pwm_times_linear_irradiance_uses_exact_first_moment_before_raw(request_data):
    request_data["capture_program"]["hdr_merge"] = None
    request_data["capture_program"]["waveform"] = waveform(request_data)
    request_data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 3
    output = run(request_data)
    # Ramp 1->3 over20ms. Frame0 is [0,2ms), with ON [0,.5ms),[1,1.5ms).
    # ON duration1ms; integral(t dt)=.75ms^2; weight=.001 + (2/.02)*.00000075 s.
    expected = output["arrays"]["channel_photoelectron_rate_scenes"][0] * 0.001075
    np.testing.assert_allclose(output["arrays"]["channel_electrons"][0], expected, rtol=2e-15)
    assert output["summary"]["readout_count"] == 2


def test_digital_attenuation_keeps_shifted_adc_clipping_out_of_hdr(request_data):
    request_data["noise_enabled"] = True
    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 0
    model = request_data["capture_program"]["electrical_model"]
    model.update(read_noise_e=1000, digital_gain=0.01)
    output = run(request_data)
    arrays = output["arrays"]
    low = arrays["electrical_adc_low_mask"]
    assert low.any()
    assert (arrays["raw_frames"][low] > 0).all()
    assert not arrays["hdr_exposure_valid_mask"][low].any()
    assert all(cal["linear_floor_dn"] > 0 for cal in output["summary"]["calibrations"])


def test_small_crop_on_product_native_sensor_uses_separate_native_budget(request_data):
    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[1440, 1920], crop_origin=[3, 5])
    output = run(request_data)
    assert output["arrays"]["raw_frames"].shape == (2, 8, 10)
    assert output["summary"]["geometry"]["full_sensor_shape"] == [1440, 1920]
    np.testing.assert_array_equal(
        output["arrays"]["electrical_photo_response_map"][0],
        output["arrays"]["electrical_photo_response_map"][1],
    )


@pytest.mark.parametrize("nonlinear", [False, True])
def test_hdr_preflight_checks_actual_seeded_dsnu_map_before_optics(
    request_data, monkeypatch, nonlinear
):
    if nonlinear:
        request_data["capture_program"] = nonlinear_examples.capture_program(
            request_data["readout"], "independent_bracket_nonlinear"
        )
    model = request_data["capture_program"]["electrical_model"]
    model["dark_current_e_per_s"] = 0
    model["fixed_pattern"]["dsnu_e_per_s"] = 1e12
    model["reference_temperature_c"] = 15
    model["dark_current_doubling_c"] = 1
    request_data["temperature_c"] = 25

    def forbidden_optics(*args, **kwargs):
        pytest.fail("optics must not run after an unsafe native dark map")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden_optics)
    runtime = HDRBracketRuntime()
    issues = runtime.validate_request(request_data)
    assert len(issues) == 1
    assert "Temperature-adjusted dark current" in issues[0]["message"]
    with pytest.raises(RuntimeRequestError, match="Temperature-adjusted dark current"):
        runtime.execute(request_data)


def test_hdr_preflight_checks_seeded_dark_population_at_long_exposure(request_data, monkeypatch):
    request_data["capture_program"]["hdr_merge"] = None
    model = request_data["capture_program"]["electrical_model"]
    model["dark_current_e_per_s"] = 0
    model["fixed_pattern"]["dsnu_e_per_s"] = 1e12
    for index, frame in enumerate(request_data["control_sequence"]["frames"]):
        frame["frame_start_ns"] = index * 25_000_000_000
        frame["applied"]["applied_at_ns"] = 0
        frame["applied"]["exposure_ns"] = 20_000_000_000
    request_data["scene_sequence"]["knots"][-1]["time_ns"] = 60_000_000_000

    def forbidden_optics(*args, **kwargs):
        pytest.fail("optics must not run after an unsafe native dark population")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden_optics)
    runtime = HDRBracketRuntime()
    issues = runtime.validate_request(request_data)
    assert len(issues) == 1
    assert "Seeded native dark charge" in issues[0]["message"]
    with pytest.raises(RuntimeRequestError, match="Seeded native dark charge"):
        runtime.execute(request_data)


def test_hdr_v1_preflight_checks_temperature_adjusted_dark_rate_before_optics(
    request_data, monkeypatch
):
    model = request_data["capture_program"]["electrical_model"]
    model["dark_current_e_per_s"] = 1e12
    model["reference_temperature_c"] = 15
    model["dark_current_doubling_c"] = 1
    request_data["temperature_c"] = 25

    def forbidden_optics(*args, **kwargs):
        pytest.fail("optics must not run after an unsafe dark-current rate")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden_optics)
    runtime = HDRBracketRuntime()
    issues = runtime.validate_request(request_data)
    assert len(issues) == 1
    assert "Temperature-adjusted dark current" in issues[0]["message"]
    with pytest.raises(RuntimeRequestError, match="Temperature-adjusted dark current"):
        runtime.execute(request_data)


def test_hdr_v1_rejects_unresolvable_raw_span_before_optics(request_data, monkeypatch):
    model = request_data["capture_program"]["electrical_model"]
    model.update(full_well_e=1e-6, conversion_gain_v_per_e=1e-15, digital_gain=1e-6)
    model["adc"]["full_scale_v"] = 1e6

    def forbidden_optics(*args, **kwargs):
        pytest.fail("optics must not run with an unresolvable electron-to-RAW span")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden_optics)
    issues = HDRBracketRuntime().validate_request(request_data)
    assert len(issues) == 1
    assert "Electron-to-RAW linear span" in issues[0]["message"]

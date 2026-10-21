"""Real static integration into paired integer RAW, with independent arithmetic checks."""

import json

import numpy as np
import pytest
from test_native_temporal import expected_rate
from test_native_temporal import native_request as native_request

from camera_platform import dcg, dcg_electrical, nonlinear_examples
from camera_platform.dcg_runtime import (
    SharedChargeDCGRuntime,
    capture_effects,
    default_capture_program,
    observation_effects,
    validate_capture_program,
)
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime, request_template


@pytest.fixture
def request_data():
    data = request_template()
    data["noise_enabled"] = False
    data["readout"].update(read_noise_e=0, dark_current_e_per_s=0)
    data["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = np.full((8, 10, 5), 1e-4)
        knot["scene"]["sampling"].update(full_sensor_shape=[8, 10], crop_origin=[0, 0])
    for index, frame in enumerate(data["control_sequence"]["frames"]):
        frame["applied"].update(
            exposure_ns=2_000_000 * (index + 1), applied_at_ns=frame["frame_start_ns"]
        )
    data["capture_program"] = default_capture_program(data["readout"])
    return data


def execute(data):
    return SharedChargeDCGRuntime().execute(data)


def expected_integer_raw(photo, exposure, model, path):
    """Independent noise-free scalar transfer; no electrical calibration/helper call."""
    charge = np.minimum(
        photo + model["dark_current_e_per_s"] * exposure, model["shared_charge_capacity_e"]
    )
    charge = np.minimum(charge, path["input_linear_ceiling_e"])
    black = path["adc"]["black_level_dn"]
    white = 2 ** path["adc"]["bit_depth"] - 1
    slope = (
        path["conversion_gain_v_per_e"] * path["analog_gain"] * white / path["adc"]["full_scale_v"]
    )
    adc = np.clip(np.rint(black + charge * slope), 0, white)
    return np.clip(np.rint(black + path["digital_gain"] * (adc - black)), 0, white)


@pytest.mark.parametrize("nonlinear", [False, True])
def test_dcg_preflight_checks_actual_seeded_dsnu_map_before_optics(
    request_data, monkeypatch, nonlinear
):
    if nonlinear:
        request_data["capture_program"] = nonlinear_examples.capture_program(
            request_data["readout"], "shared_charge_nonlinear"
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
    runtime = SharedChargeDCGRuntime()
    issues = runtime.validate_request(request_data)
    assert len(issues) == 1
    assert "Temperature-adjusted dark current" in issues[0]["message"]
    with pytest.raises(RuntimeRequestError, match="Temperature-adjusted dark current"):
        runtime.execute(request_data)


def test_dcg_native_budget_rejects_before_seeded_map_allocation(request_data, monkeypatch):
    request_data["capture_program"]["electrical_model"]["fixed_pattern"]["dsnu_e_per_s"] = 1
    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"]["full_sensor_shape"] = [10_000, 10_000]

    def forbidden_patterns(*args, **kwargs):
        pytest.fail("oversized native fixed-pattern maps must not be allocated")

    monkeypatch.setattr("camera_platform.electrical._patterns", forbidden_patterns)
    issues = SharedChargeDCGRuntime().validate_request(request_data)
    assert len(issues) == 1
    assert "bounded full native sensor" in issues[0]["message"]


def test_program_strict_detached_and_effects_do_not_claim_bracket(request_data):
    original = request_data["capture_program"]
    normalized = validate_capture_program(original)
    normalized["electrical_model"]["paths"][0]["digital_gain"] = 2
    assert original["electrical_model"]["paths"][0]["digital_gain"] == 1
    assert "raw_dcg_covariance_merge" in capture_effects(original)
    assert "raw_dcg_covariance_merge" not in observation_effects(original)
    assert "raw_hdr_bracket_merge" not in capture_effects(original)
    no_merge = {**original, "merge_profile": None}
    assert "raw_dcg_covariance_merge" not in capture_effects(no_merge)
    for bad in (
        None,
        {},
        {**original, "schema": "camera_capture_program_v1"},
        {**original, "waveform": None},
        {**original, "truth": []},
    ):
        with pytest.raises(ValueError):
            validate_capture_program(bad)


def test_actual_raw_axes_arithmetic_previews_and_serializable_evidence(request_data):
    result = execute(request_data)
    arrays, summary = result["arrays"], result["summary"]
    assert arrays["raw_groups"].shape == (2, 2, 8, 10)
    assert arrays["raw_groups"].dtype.kind in "iu"
    assert arrays["rgb_groups"].shape == (2, 2, 8, 10, 3)
    assert arrays["merged_rate"].shape == (2, 8, 10)
    assert arrays["merged_linear_rgb"].shape == (2, 8, 10, 3)
    np.testing.assert_array_equal(arrays["rgb"], arrays["merged_rgb"][0])
    model = request_data["capture_program"]["electrical_model"]
    for index in range(2):
        for path_index, path in enumerate(model["paths"]):
            expected = expected_integer_raw(
                arrays["mean_electrons"][index], 0.002 * (index + 1), model, path
            )
            np.testing.assert_array_equal(arrays["raw_groups"][index, path_index], expected)
    np.testing.assert_array_equal(arrays["mean_electrons"][1], 2 * arrays["mean_electrons"][0])
    assert summary["acquisition_count"] == 2 and summary["readout_count"] == 4
    assert len(summary["acquisitions"]) == len(summary["timing"]["frames"]) == 2
    assert summary["raw_axes"] == ["acquisition", "readout", "row", "column"]
    assert "frame_count" not in summary and "frames" not in summary
    assert "raw_frames" not in arrays and "rgb_frames" not in arrays
    assert summary["physical_validation"] == "not_established"
    assert "raw_dcg_covariance_merge" in summary["included_effects"]
    assert "raw_dcg_covariance_merge" not in summary["prediction"]["included_effects"]
    assert set(arrays) == set(summary["array_units"])
    json.dumps(summary, allow_nan=False)
    resources = SharedChargeDCGRuntime().estimate_resources(request_data)
    assert resources["acquisitions"] == 2 and resources["readouts"] == 4
    assert resources["working_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())


def test_prediction_uses_both_actual_paths_and_declared_dark_correction(request_data):
    model = request_data["capture_program"]["electrical_model"]
    model["dark_current_e_per_s"] = 1200
    model["paths"][0]["adc"]["black_level_dn"] = 100
    request_data["noise_enabled"] = True
    result = execute(request_data)
    arrays, summary = result["arrays"], result["summary"]
    expected = []
    for index, cal in enumerate(summary["calibrations"]):
        for path_index, path in enumerate(cal["paths"]):
            corrected = (
                arrays["raw_groups"][index, path_index].astype(float)
                - path["black_level_dn"]
                - arrays["calibration_dark_e"][index] * path["dn_per_e"]
            )
            expected.extend(
                float(corrected[arrays["cfa_channel_index"] == c].mean()) for c in range(3)
            )
    prediction = summary["prediction"]
    assert prediction["values"] == expected
    assert [axis["name"] for axis in prediction["axes"]] == ["acquisition", "readout", "channel"]
    assert prediction["axes"][1]["values"] == ["HCG", "LCG"]
    assert prediction["metric_id"] == "raw.channel_mean_dn_shared_charge"
    assert prediction["raw_formats"][0]["black_level_dn"] == 100
    assert prediction["raw_formats"][1]["black_level_dn"] != 100


def test_merge_receives_only_raw_calibration_and_cannot_observe_oracle(request_data, monkeypatch):
    original = execute(request_data)
    real_capture = dcg_electrical.capture_shared_electrons
    real_merge = dcg.merge_shared_charge_raw
    observed = []

    def changed_truth(*args, **kwargs):
        output = real_capture(*args, **kwargs)
        for key in ("shared_charge_e", "mean_total_charge_e"):
            output["arrays"][key][:] = 999_999
        output["arrays"]["well_saturated_mask"][:] = True
        return output

    def checked_merge(raw, calibration, profile):
        assert raw.shape == (2, 8, 10) and raw.dtype.kind in "iu"
        assert set(calibration) == {
            "schema",
            "stream_ids",
            "exposure_s",
            "paths",
            "read_noise_covariance_e2",
            "dark_e",
            "geometry",
        }
        observed.append(raw.copy())
        return real_merge(raw, calibration, profile)

    monkeypatch.setattr(dcg_electrical, "capture_shared_electrons", changed_truth)
    monkeypatch.setattr(dcg, "merge_shared_charge_raw", checked_merge)
    result = execute(request_data)
    np.testing.assert_array_equal(np.stack(observed), result["arrays"]["raw_groups"])
    np.testing.assert_array_equal(
        original["arrays"]["merged_rate"], result["arrays"]["merged_rate"]
    )
    assert original["summary"]["prediction"] == result["summary"]["prediction"]
    assert not result["summary"]["simulation_oracle"]["used_by_merge"]


def test_no_fusion_keeps_original_two_readouts_without_hdr_claim(request_data, monkeypatch):
    request_data["capture_program"]["merge_profile"] = None
    request_data["control_sequence"]["frames"] = request_data["control_sequence"]["frames"][:1]
    monkeypatch.setattr(dcg, "merge_shared_charge_raw", lambda *a: pytest.fail("merge called"))
    result = execute(request_data)
    arrays, summary = result["arrays"], result["summary"]
    assert arrays["raw_groups"].shape == (1, 2, 8, 10)
    assert "merged_rate" not in arrays and "merged_rgb" not in arrays
    np.testing.assert_array_equal(arrays["rgb"], arrays["rgb_groups"][0, 0])
    assert summary["dcg_merge"] is None and not summary["merge_applied"]
    assert summary["readout_count"] == 2 and summary["acquisition_count"] == 1


def test_independent_acquisitions_share_charge_within_paths_and_never_call_legacy(
    request_data, monkeypatch
):
    from camera_platform import electrical, hdr, spatial

    monkeypatch.setattr(electrical, "capture_electrons", lambda *a, **k: pytest.fail("legacy"))
    monkeypatch.setattr(spatial, "_capture", lambda *a, **k: pytest.fail("legacy"))
    monkeypatch.setattr(hdr, "merge_raw_bracket", lambda *a, **k: pytest.fail("bracket"))
    request_data["noise_enabled"] = True
    request_data["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 2_000_000
    first, again = execute(request_data), execute(request_data)
    np.testing.assert_array_equal(first["arrays"]["raw_groups"], again["arrays"]["raw_groups"])
    shared = first["arrays"]["electrical_shared_charge_e"]
    assert not np.array_equal(shared[0], shared[1])
    # Read noise is zero in this fixture. Both actual RAW paths encode exactly
    # the same sampled shared charge through their independently declared ADC.
    model = request_data["capture_program"]["electrical_model"]
    for index in range(2):
        for path_index, path in enumerate(model["paths"]):
            expected = expected_integer_raw(shared[index], 0.002, model, path)
            np.testing.assert_array_equal(
                first["arrays"]["raw_groups"][index, path_index], expected
            )
    assert first["summary"]["seed_sequence"] == [223, 224]


@pytest.mark.parametrize(
    "violation", ["overlap", "scene", "gain", "clock", "waveform", "bits", "temperature", "seed"]
)
def test_unsupported_or_mismatched_capture_refused_before_physics(
    request_data, monkeypatch, violation
):
    data = request_data
    if violation == "overlap":
        data["control_sequence"]["frames"][1].update(frame_start_ns=1_000_000)
        data["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 1_000_000
    elif violation == "scene":
        data["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 2
    elif violation == "gain":
        data["control_sequence"]["frames"][0]["applied"]["gain"] = 2
    elif violation == "clock":
        data["scene_sequence"]["clock"] = {**data["scene_sequence"]["clock"], "clock_id": "other"}
    elif violation == "waveform":
        data["capture_program"]["waveform"] = None
    elif violation == "bits":
        for path in data["capture_program"]["electrical_model"]["paths"]:
            path["adc"]["bit_depth"] -= 1
    elif violation == "temperature":
        data["temperature_c"] = 26
    else:
        data["seed"] = 2**32 - 1
    monkeypatch.setattr(
        dcg_electrical,
        "capture_shared_electrons",
        lambda *a, **k: pytest.fail("capture before validation"),
    )
    runtime = SharedChargeDCGRuntime()
    assert runtime.validate_request(data)
    with pytest.raises(RuntimeRequestError):
        runtime.execute(data)


@pytest.mark.parametrize("origin", [[0, 0], [0, 1], [1, 0], [1, 1]])
def test_native_four_sites_and_every_crop_phase_reach_both_actual_raw(native_request, origin):
    data = native_request
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"]["crop_origin"] = origin
    data["capture_program"] = default_capture_program(data["readout"])
    result = execute(data)
    arrays, summary = result["arrays"], result["summary"]
    site_rates = expected_rate(data)
    site = np.array([[0, 1], [2, 3]])[
        (np.arange(8)[:, None] + origin[0]) % 2, (np.arange(10)[None, :] + origin[1]) % 2
    ]
    np.testing.assert_array_equal(arrays["cfa_channel_index"], site)
    np.testing.assert_allclose(arrays["mean_electrons"][0], site_rates[site] * 0.002)
    model = data["capture_program"]["electrical_model"]
    for path_index, path in enumerate(model["paths"]):
        expected = expected_integer_raw(site_rates[site] * 0.002, 0.002, model, path)
        np.testing.assert_array_equal(arrays["raw_groups"][0, path_index], expected)
        raw = arrays["raw_groups"][0, path_index]
        assert raw[site == 1].mean() < raw[site == 2].mean()
    assert summary["channels"] == ["R", "Gr", "Gb", "B"]
    assert len(summary["prediction"]["values"]) == 2 * 2 * 4
    error = abs(arrays["merged_rate"][0] - site_rates[site])
    assert np.all(error <= arrays["rounding_bound_rate"][0] + 1e-8)
    assert set(arrays) == set(summary["array_units"])


def test_common_saturation_keeps_nan_radiometry_and_finite_json_preview(request_data):
    request_data["capture_program"]["electrical_model"]["shared_charge_capacity_e"] = 1
    result = execute(request_data)
    assert not result["arrays"]["valid_mask"].any()
    assert np.isnan(result["arrays"]["merged_rate"]).all()
    assert np.isfinite(result["arrays"]["rgb"]).all()
    json.dumps(result["summary"], allow_nan=False)


def test_fixed_patterns_and_native_row_windows_are_acquisition_properties(request_data):
    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[12, 14], crop_origin=[1, 3])
    request_data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=100_000)
    request_data["capture_program"]["electrical_model"]["fixed_pattern"].update(
        prnu_sigma=0.02, dsnu_e_per_s=2, seed=9
    )
    result = execute(request_data)
    arrays = result["arrays"]
    np.testing.assert_array_equal(
        arrays["electrical_photo_response_map"][0], arrays["electrical_photo_response_map"][1]
    )
    np.testing.assert_array_equal(
        arrays["electrical_dark_current_e_per_s_map"][0],
        arrays["electrical_dark_current_e_per_s_map"][1],
    )
    assert arrays["row_start_ns"].shape == (2, 8)
    np.testing.assert_array_equal(np.diff(arrays["row_start_ns"][0]), 100_000)
    assert arrays["row_start_ns"][0, 0] == 100_000


@pytest.mark.parametrize("budget", ["MAX_WORKING_ARRAY_BYTES", "MAX_OUTPUT_ARRAY_BYTES"])
def test_budgets_refuse_before_optical_allocation(request_data, monkeypatch, budget):
    import camera_platform.dcg_runtime as runtime

    monkeypatch.setattr(runtime, budget, 3000)
    monkeypatch.setattr(
        runtime.TemporalSceneRuntime,
        "_optical_rates",
        lambda *args: pytest.fail("Optics ran before resource refusal"),
    )
    with pytest.raises(RuntimeRequestError, match="array memory"):
        execute(request_data)


def test_source_labels_do_not_invent_a_second_physical_scene(request_data):
    second = request_data["scene_sequence"]["knots"][1]["scene"]
    second["source"]["description"] = "Independent label for the exact same optical image"
    output = execute(request_data)
    assert output["summary"]["merge_applied"]
    assert len(output["summary"]["scene_sources"]) == 2


def test_paired_read_noise_is_retained_and_raw_fusion_uses_declared_covariance(request_data):
    model = request_data["capture_program"]["electrical_model"]
    model["read_noise"]["covariance_e2"] = [[4, 1], [1, 9]]
    request_data["noise_enabled"] = True
    result = execute(request_data)
    arrays = result["arrays"]
    assert np.any(arrays["electrical_read_noise_e"] != 0)
    for index in range(2):
        for path_index, path in enumerate(model["paths"]):
            charge = np.minimum(
                arrays["electrical_shared_charge_e"][index], path["input_linear_ceiling_e"]
            )
            charge = charge + arrays["electrical_read_noise_e"][index, path_index]
            adc = path["adc"]
            slope = (
                path["conversion_gain_v_per_e"]
                * path["analog_gain"]
                * (2 ** adc["bit_depth"] - 1)
                / adc["full_scale_v"]
            )
            expected = np.rint(adc["black_level_dn"] + charge * slope)
            np.testing.assert_array_equal(arrays["raw_groups"][index, path_index], expected)
        joint = arrays["usable_mask"][index].all(axis=0)
        assert joint.any()
        # Independent scalar quadratic minimization: (R_LL-R_HL)/(R_HH+R_LL-2R_HL).
        np.testing.assert_allclose(arrays["weights"][index, 0][joint], 8 / 11)
        np.testing.assert_allclose(arrays["weights"][index, 1][joint], 3 / 11)
        expected_read_var = (8 / 11) ** 2 * 4 + 2 * (8 / 11) * (3 / 11) + (3 / 11) ** 2 * 9
        np.testing.assert_allclose(arrays["read_variance_e2"][index][joint], expected_read_var)


def test_small_crop_budget_includes_full_native_fixed_patterns(request_data, monkeypatch):
    import camera_platform.dcg_runtime as runtime

    for knot in request_data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[1024, 1024], crop_origin=[11, 13])
    monkeypatch.setattr(runtime, "MAX_WORKING_ARRAY_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(
        runtime.TemporalSceneRuntime,
        "_optical_rates",
        lambda *args: pytest.fail("Optics ran before full-sensor refusal"),
    )
    with pytest.raises(RuntimeRequestError, match="array memory"):
        execute(request_data)


def test_known_dark_law_overflow_is_refused_during_preflight_before_optics(
    request_data, monkeypatch
):
    import camera_platform.dcg_runtime as runtime

    model = request_data["capture_program"]["electrical_model"]
    model.update(reference_temperature_c=0, dark_current_doubling_c=0.001, dark_current_e_per_s=1)
    model["read_noise"]["temperature_range_c"] = [0, 25]
    request_data["temperature_c"] = 25
    monkeypatch.setattr(
        runtime.TemporalSceneRuntime,
        "_optical_rates",
        lambda *args: pytest.fail("Known impossible dark law reached optics"),
    )
    issues = SharedChargeDCGRuntime().validate_request(request_data)
    assert issues and "Dark-current temperature scaling" in issues[0]["message"]
    with pytest.raises(RuntimeRequestError, match="Dark-current temperature scaling"):
        execute(request_data)

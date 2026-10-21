"""Direct source scatter oracles for supplied field PSFs; no physical optics claim."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest
import scipy

from camera_platform.runtime import LIGHT_M_S, PLANCK_J_S, RuntimeRequestError
from camera_platform.spatial import (
    SpatialSceneRuntime,
    photoelectron_rate,
    request_template,
    validate_psf,
    validate_psf_coverage,
)
from camera_platform.temporal import TemporalSceneRuntime
from camera_platform.temporal import request_template as temporal_template


def field_request():
    data = request_template()
    data.update(frames=1, noise_enabled=False)
    data["scene"]["spectral_irradiance"] = np.zeros((8, 10, 5))
    data["scene"]["sampling"].update(full_sensor_shape=[9, 11], crop_origin=[0, 0])
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    kernels = np.zeros((5, 3, 3, 3, 5))
    for band in range(5):
        for y in range(3):
            for x in range(3):
                kernels[band, y, x, y, (x + band) % 5] = 1
    data["psf"].update(
        schema="field_intensity_psf_v1", spatial_model="field_grid",
        field_coordinates="sensor_center_yx", interpolation="bilinear_source_position",
        field_y_mm=[-.016, 0, .012], field_x_mm=[-.020, 0, .016], values=kernels,
    )
    return data


def direct_scatter(data):
    """Sum each source sample's interpolated receiver distribution with explicit loops."""
    image, psf = np.asarray(data["scene"]["spectral_irradiance"]), data["psf"]
    sampling, pitch = data["scene"]["sampling"], data["pixel_pitch_um"]
    kernels = np.asarray(psf["values"])
    rows, cols, bands = image.shape
    ky, kx = kernels.shape[-2:]
    full = np.zeros((rows + ky - 1, cols + kx - 1, bands))
    for y in range(rows):
        for x in range(cols):
            coordinates = [(index + offset - (size - 1) / 2) * pitch / 1000
                           for index, offset, size in zip(
                               (y, x), sampling["crop_origin"], sampling["full_sensor_shape"],
                               strict=True)]
            contributions = []
            for coordinate, axis in zip(coordinates, [psf["field_y_mm"], psf["field_x_mm"]],
                                        strict=True):
                for low in range(len(axis) - 1):
                    if axis[low] <= coordinate <= axis[low + 1]:
                        right = (coordinate - axis[low]) / (axis[low + 1] - axis[low])
                        contributions.append([(low, 1 - right), (low + 1, right)])
                        break
            assert len(contributions) == 2
            for band in range(bands):
                for yn, yw in contributions[0]:
                    for xn, xw in contributions[1]:
                        full[y:y + ky, x:x + kx, band] += (
                            image[y, x, band] * yw * xw * kernels[band, yn, xn])
    return full[ky // 2:ky // 2 + rows, kx // 2:kx // 2 + cols], full


def test_asymmetric_field_scatter_matches_independent_oracle_and_flux():
    data = field_request()
    data["scene"]["spectral_irradiance"] = np.random.default_rng(371).uniform(
        0, 1e-4, (8, 10, 5))
    expected, full = direct_scatter(data)
    output = SpatialSceneRuntime().execute(data)
    np.testing.assert_allclose(output["arrays"]["blurred_spectral_irradiance"], expected,
                               rtol=3e-14, atol=1e-19)
    flux = output["summary"]["flux_diagnostics"]
    area = (data["pixel_pitch_um"] * 1e-6) ** 2
    np.testing.assert_allclose(flux["full_convolution_spectral_flux"], full.sum((0, 1)) * area)
    np.testing.assert_allclose(flux["full_convolution_spectral_flux"], flux["input_spectral_flux"])
    np.testing.assert_allclose(flux["retained_crop_spectral_flux"], expected.sum((0, 1)) * area)
    np.testing.assert_allclose(flux["outside_crop_spectral_flux"],
                               (full.sum((0, 1)) - expected.sum((0, 1))) * area)
    assert max(flux["fft_negative_roundoff_trimmed_spectral_flux"]) >= 0


@pytest.mark.parametrize("location", [(4, 5), (0, 0), (7, 9), (2, 3)])
def test_node_and_intermediate_impulses_use_source_position_and_convolution_orientation(location):
    data = field_request()
    data["scene"]["spectral_irradiance"][location] = np.arange(1, 6) * 1e-5
    expected, full = direct_scatter(data)
    result = SpatialSceneRuntime().execute(data)
    np.testing.assert_allclose(result["arrays"]["blurred_spectral_irradiance"], expected,
                               rtol=2e-14, atol=1e-19)
    if location == (4, 5):
        # The center node has a different, asymmetric horizontal offset per band.
        for band in range(5):
            assert np.unravel_index(expected[..., band].argmax(), expected.shape[:2]) == (
                4, 5 + (1 + band) % 5 - 2)
    assert np.all(full.sum((0, 1)) > 0)


def test_source_blending_is_not_receiver_blending():
    from scipy.signal import convolve2d

    data = field_request()
    data["scene"]["spectral_irradiance"][:] = 1e-4
    expected, _ = direct_scatter(data)
    # A deliberately wrong receiver-weighted implementation loses/reassigns flux.
    wrong = np.zeros_like(expected)
    for y in range(8):
        for x in range(10):
            y_weights = [np.interp((y - 4) * .004, data["psf"]["field_y_mm"], np.eye(3)[n])
                         for n in range(3)]
            x_weights = [np.interp((x - 5) * .004, data["psf"]["field_x_mm"], np.eye(3)[n])
                         for n in range(3)]
            for yn, yw in enumerate(y_weights):
                for xn, xw in enumerate(x_weights):
                    for band in range(5):
                        wrong[y, x, band] += yw * xw * convolve2d(
                            data["scene"]["spectral_irradiance"][..., band],
                            data["psf"]["values"][band, yn, xn], mode="same")[y, x]
    assert np.max(np.abs(expected - wrong)) > 1e-5
    np.testing.assert_allclose(
        SpatialSceneRuntime().execute(data)["arrays"]["blurred_spectral_irradiance"],
        expected, atol=1e-19)


def test_native_crop_coordinates_are_full_sensor_relative_and_artifacts_are_explicit():
    data = field_request()
    data["scene"]["sampling"].update(full_sensor_shape=[21, 25], crop_origin=[3, 5])
    data["psf"].update(field_y_mm=[-.028, -.010, 0], field_x_mm=[-.028, -.012, .008])
    data["scene"]["spectral_irradiance"][4, 5] = 1e-4
    expected, _ = direct_scatter(data)
    output = SpatialSceneRuntime().execute(data)
    np.testing.assert_allclose(output["arrays"]["blurred_spectral_irradiance"], expected,
                               atol=1e-19)
    np.testing.assert_array_equal(output["arrays"]["psf_source_y_mm"],
                                  (np.arange(8) - 7) * 4 / 1000)
    np.testing.assert_array_equal(output["arrays"]["psf_source_x_mm"],
                                  (np.arange(10) - 7) * 4 / 1000)
    summary = output["summary"]
    assert summary["psf"]["field_axis_order"] == [
        "wavelength", "field_y", "field_x", "kernel_y", "kernel_x"]
    assert summary["psf"]["interpolation"] == "bilinear_source_position"
    assert summary["psf"]["source_position_bounds_mm"] == [[-.028, 0.0], [-.028, .008]]
    assert summary["psf_source"] == data["psf"]["source"]
    assert set(summary["array_units"]) == set(output["arrays"])
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert any("separable approximation" in s for s in summary["limitations"])
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("noise", [False, True])
def test_rate_static_temporal_and_prepared_capture_agree_with_field_psf(noise):
    spatial = field_request()
    spatial.update(frames=2, noise_enabled=noise)
    spatial["scene"]["spectral_irradiance"] = np.random.default_rng(822).uniform(
        0, 1e-4, (8, 10, 5))
    timed = temporal_template()
    timed.update({key: value for key, value in spatial.items()
                  if key not in {"scene", "frames", "exposure_s"}})
    for knot in timed["scene_sequence"]["knots"]:
        knot["scene"] = deepcopy(spatial["scene"])
    static = SpatialSceneRuntime().execute(spatial)
    temporal = TemporalSceneRuntime().execute(timed)
    rate = photoelectron_rate({key: value for key, value in spatial.items()
                               if key not in {"frames", "exposure_s"}})
    np.testing.assert_allclose(rate["arrays"]["channel_photoelectron_rate"] * .004,
                               static["arrays"]["channel_electrons"], rtol=2e-15)
    for name in ("raw_frames", "rgb_frames"):
        np.testing.assert_array_equal(static["arrays"][name], temporal["arrays"][name])
    session = TemporalSceneRuntime().prepare_capture(timed)
    frame = session.capture_frame(timed["control_sequence"]["frames"][1], seed_index=1)
    np.testing.assert_array_equal(frame["arrays"]["raw_frames"][0],
                                  temporal["arrays"]["raw_frames"][1])
    for output in (static, rate, temporal, frame):
        assert output["summary"]["psf"]["schema"] == "field_intensity_psf_v1"
        np.testing.assert_array_equal(output["arrays"]["psf_intensity"], spatial["psf"]["values"])
        assert set(output["arrays"]) == set(output["summary"]["array_units"])
        np.testing.assert_array_equal(output["arrays"]["psf_field_x_mm"],
                                      spatial["psf"]["field_x_mm"])


@pytest.mark.parametrize("patch", [
    {"interpolation": "bilinear_receiver_position"},
    {"field_coordinates": "crop_top_left"},
    {"spatial_model": "locally_shift_invariant"},
    {"field_y_mm": [False, .1, .2]},
    {"field_x_mm": [-.020, True, 2]},
    {"field_y_mm": [-.1, float("nan"), .1]},
    {"field_x_mm": [-.1, 0, float("inf")]},
    {"field_x_mm": [-1.7e308, 1.7e308]},
    {"field_y_mm": [0, -.1, .1]},
    {"field_x_mm": [0, 0, .1]},
    {"field_y_mm": [[-.1, 0, .1]]},
    {"field_y_mm": ["-.1", "0", ".1"]},
    {"field_x_mm": [-.1, 0j, .1]},
    {"field_y_mm": [0]},
    {"field_y_mm": list(range(10))},
    {"field_x_mm": [-.1, .1]},
    {"values": np.ones((5, 3, 3, 2, 3)) / 6},
    {"values": np.ones((5, 3, 3, 1, 1)) * 1.001},
    {"values": np.zeros((5, 3, 3, 1, 1))},
    {"values": np.ones((5, 3, 3, 1, 1), dtype=complex)},
    {"values": [[[[[True]]] * 3] * 3] * 5},
    {"sample_pitch_um": True},
    {"extra": 1},
])
def test_invalid_field_contract_refused_standalone_and_runtime(patch):
    data = field_request()
    data["psf"].update(patch)
    with pytest.raises((ValueError, TypeError)):
        validate_psf(data["psf"])
    assert SpatialSceneRuntime().validate_request(data)


def test_field_node_budget_and_detached_normalization():
    data = field_request()
    original = data["psf"]
    normalized = validate_psf(original)
    json.dumps(normalized, allow_nan=False)
    original["values"][:] = 0
    original["field_y_mm"][0] = -100
    assert normalized["field_y_mm"][0] == -.016
    np.testing.assert_allclose(np.asarray(normalized["values"]).sum((-1, -2)), 1)
    normalized.update(field_y_mm=np.linspace(-.02, .02, 6),
                       field_x_mm=np.linspace(-.02, .02, 5), values=np.ones((5, 6, 5, 1, 1)))
    with pytest.raises(ValueError, match="25 field nodes"):
        validate_psf(normalized)


@pytest.mark.parametrize("key,value", [
    ("field_y_mm", [-.015999999999999, 0, .012]),
    ("field_y_mm", [-.016, 0, .011999999999999]),
    ("field_x_mm", [-.019999999999999, 0, .016]),
    ("field_x_mm", [-.020, 0, .015999999999999]),
])
def test_any_extrapolation_refused_before_convolution(key, value):
    data = field_request()
    data["psf"][key] = value
    assert validate_psf(data["psf"])
    with pytest.raises(ValueError, match="no extrapolation"):
        validate_psf_coverage(data["psf"], [9, 11], [0, 0], [8, 10], 4)
    with pytest.raises(RuntimeRequestError, match="no extrapolation"):
        SpatialSceneRuntime().execute(data)


def test_field_work_is_bounded_before_computation_and_multiplies_unique_scenes(monkeypatch):
    data = field_request()
    estimate = SpatialSceneRuntime().estimate_resources(data)
    assert estimate["field_psf_node_convolutions"] == 45
    assert estimate["field_psf_fft_work_units"] > 0
    data["scene"]["spectral_irradiance"] = np.zeros((512, 512, 5))
    data["scene"]["sampling"].update(full_sensor_shape=[512, 512], crop_origin=[0, 0])
    data["psf"].update(field_x_mm=[-2, 0, 2], field_y_mm=[-2, 0, 2])
    with pytest.raises(RuntimeRequestError, match="FFT work-unit budget"):
        SpatialSceneRuntime().estimate_resources(data)
    small = field_request()
    timed = temporal_template()
    timed.update({key: value for key, value in small.items()
                  if key not in {"scene", "frames", "exposure_s"}})
    for index, knot in enumerate(timed["scene_sequence"]["knots"]):
        knot["scene"] = deepcopy(small["scene"])
        knot["scene"]["spectral_irradiance"][:] = index * 1e-5
    estimate = TemporalSceneRuntime().estimate_resources(timed)
    assert estimate["field_psf_node_convolutions"] == 90
    monkeypatch.setattr("camera_platform.temporal.MAX_FIELD_FFT_WORK",
                        estimate["field_psf_fft_work_units"] - 1)
    with pytest.raises(RuntimeRequestError, match="across unique scenes"):
        TemporalSceneRuntime().estimate_resources(timed)


def test_v1_frozen_float_and_seeded_raw_rgb_outputs_are_unchanged():
    # Captured before the field implementation using the existing v1 operator.
    # The dev7 archive independently reproduces both qualified FFT byte results;
    # see reports/camera_platform/install-v016/regression-diagnosis/. No numerical
    # tolerance is relaxed, and seeded RAW/RGB must remain identical in both pairs.
    data = request_template()
    data["scene"]["spectral_irradiance"] = np.arange(8 * 10 * 5).reshape(8, 10, 5) * 1e-7
    data["scene"]["sampling"]["full_sensor_shape"] = [8, 10]
    data.update(frames=2, noise_enabled=True)
    arrays = SpatialSceneRuntime().execute(data)["arrays"]
    qualified_float_hashes = {
        ("2.4.2", "1.17.1"): (
            "0cf08ff940b9fe33cce8374384283988b17e38b57b04ab4cf57442d517c7fbdb",
            "ca5f2f2ca6ad7b780374fcc569e19937d2048971d9b1a4a9b78ba9373f8cb36e",
        ),
        ("2.5.3", "1.18.1"): (
            "320afecc6ce9ed676f654eb2ebc22e1724ae8f8c4beff997a75e4133e38c0611",
            "c45590b6188494bb63331daca06b8cb625dfa3debfd83f5c0eeaf8beb4d6519b",
        ),
    }
    versions = (np.__version__, scipy.__version__)
    assert versions in qualified_float_hashes, (
        f"NumPy/SciPy {versions} requires independent historical v1 FFT qualification"
    )
    blurred_hash, electrons_hash = qualified_float_hashes[versions]
    expected = {
        "blurred_spectral_irradiance": blurred_hash,
        "mean_electrons": electrons_hash,
        "raw_frames": "897478ed4a49e075aeeeac93b76c7480b7fbe97c320276b7e522c11cc5f1085c",
        "rgb_frames": "bd66608a4a2974775a02850777d2c5fe5c040a7740e8b644a8d38b53713cf321",
    }
    assert {key: hashlib.sha256(arrays[key].tobytes()).hexdigest() for key in expected} == expected


def physical_radial_request():
    data = field_request()
    data["psf"]["values"] = np.ones((5, 3, 3, 1, 1))
    data["scene"]["spectral_irradiance"][:] = 1e-4
    data["lens"]["field_reference_radius_mm"] = 3.6
    data["lens"]["relative_illumination"] = [[1, .5]] * 5
    data["sensor_angular_response"]["relative_response"] = [
        [[1, .3]] * 5, [[1, .4]] * 5, [[1, .8]] * 5]
    return data


def test_physical_radial_response_preserves_same_mm_across_two_sensor_sizes_and_crops():
    first = physical_radial_request()
    second = deepcopy(first)
    second["scene"]["sampling"].update(full_sensor_shape=[25, 27], crop_origin=[8, 8])
    a, b = (SpatialSceneRuntime().execute(request) for request in (first, second))
    for key in ("sensor_radius_mm", "response_radius_map", "relative_illumination_map",
                "channel_electrons", "raw_frames"):
        np.testing.assert_array_equal(a["arrays"][key], b["arrays"][key])
    assert not np.array_equal(a["arrays"]["radius_map"], b["arrays"]["radius_map"])
    # Full sensor corner is only ~0.0256 mm from center, not the parent's 3.6 mm node.
    y, x = -.016, -.020
    response_radius = np.hypot(y, x) / 3.6
    expected_ri = 1 - .5 * response_radius
    assert a["arrays"]["relative_illumination_map"][0, 0, 0] == pytest.approx(expected_ri)
    assert a["arrays"]["radius_map"][0, 0] == 1
    assert a["arrays"]["response_radius_map"][0, 0] < .01
    photons = 1e-4 * 1e-9 * (650**2 - 450**2) / 2 / (PLANCK_J_S * LIGHT_M_S)
    for channel, endpoint in enumerate((.3, .4, .8)):
        expected = (photons * expected_ri * (1 + (endpoint - 1) * response_radius)
                    * first["qe"][channel][0] * (4e-6)**2 * .004)
        assert a["arrays"]["channel_electrons"][0, 0, channel] == pytest.approx(expected, rel=2e-14)
    assert a["summary"]["radial_response_sampling"]["field_reference_radius_mm"] == 3.6


def test_physical_radial_rate_temporal_and_prepared_capture_artifacts_preserve_semantics():
    spatial = physical_radial_request()
    timed = temporal_template()
    timed.update({key: value for key, value in spatial.items()
                  if key not in {"scene", "frames", "exposure_s"}})
    for knot in timed["scene_sequence"]["knots"]:
        knot["scene"] = deepcopy(spatial["scene"])
    timed["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 3
    temporal = TemporalSceneRuntime().execute(timed)
    rate = photoelectron_rate({key: value for key, value in spatial.items()
                               if key not in {"frames", "exposure_s"}})
    # Declared ramp increases 1 -> 3 in 20 ms; first 4 ms integrates to 1.2 * exposure.
    np.testing.assert_allclose(temporal["arrays"]["channel_electrons"][0],
                               rate["arrays"]["channel_photoelectron_rate"] * .004 * 1.2,
                               rtol=3e-15)
    session = TemporalSceneRuntime().prepare_capture(timed)
    frame = session.capture_frame(timed["control_sequence"]["frames"][1], seed_index=1)
    np.testing.assert_array_equal(frame["arrays"]["raw_frames"][0],
                                  temporal["arrays"]["raw_frames"][1])
    for output in (rate, temporal, frame):
        assert output["summary"]["radial_response_sampling"]["field_reference_radius_mm"] == 3.6
        assert output["summary"]["array_units"]["sensor_radius_mm"] == "mm"
        assert output["summary"]["array_units"]["response_radius_map"] == "1"
        assert set(output["arrays"]) == set(output["summary"]["array_units"])
        np.testing.assert_allclose(output["arrays"]["response_radius_map"],
                                   output["arrays"]["sensor_radius_mm"] / 3.6)


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "3.6", 3j])
def test_invalid_physical_radial_scale_rejected(value):
    data = physical_radial_request()
    data["lens"]["field_reference_radius_mm"] = value
    with pytest.raises(RuntimeRequestError):
        SpatialSceneRuntime().execute(data)


def test_physical_radial_coverage_does_not_extrapolate_even_when_psf_covers_crop():
    data = physical_radial_request()
    boundary = float(np.hypot(.016, .020))
    data["lens"]["field_reference_radius_mm"] = boundary
    SpatialSceneRuntime().estimate_resources(data)
    data["lens"]["field_reference_radius_mm"] = float(np.nextafter(boundary, 0))
    with pytest.raises(RuntimeRequestError, match="physical radial response"):
        SpatialSceneRuntime().estimate_resources(data)


def test_closed_loop_keeps_physical_radius_and_field_axes_shared_across_frames():
    from camera_platform.closed_loop import ClosedLoopSceneRuntime
    from camera_platform.closed_loop import request_template as control_template
    from camera_platform.controls import default_policy

    spatial = physical_radial_request()
    controlled = control_template()
    controlled.update({key: value for key, value in spatial.items()
                       if key not in {"scene", "frames", "exposure_s"}})
    for knot in controlled["scene_sequence"]["knots"]:
        knot["scene"] = deepcopy(spatial["scene"])
    program = controlled["control_program"]
    program["policy"] = default_policy([8, 10])
    program["policy"]["ae"]["enabled"] = False
    program["policy"]["awb"]["enabled"] = False
    recorded = {key: value for key, value in controlled.items() if key != "control_program"}
    recorded["control_sequence"] = {
        "schema": "recorded_control_sequence_v1", "clock": program["clock"],
        "mode": program["mode"], "frames": [
            {**frame, "applied": {**program["initial"], "command_id": None,
                                   "applied_at_ns": frame["frame_start_ns"]}}
            for frame in program["frames"]],
    }
    actual = ClosedLoopSceneRuntime().execute(controlled)
    expected = TemporalSceneRuntime().execute(recorded)
    for name in ("sensor_radius_mm", "response_radius_map", "relative_illumination_map",
                 "psf_field_x_mm", "psf_field_y_mm", "psf_source_x_mm", "psf_source_y_mm",
                 "raw_frames", "rgb_frames"):
        np.testing.assert_array_equal(actual["arrays"][name], expected["arrays"][name])
    assert actual["arrays"]["sensor_radius_mm"].shape == (8, 10)
    assert actual["arrays"]["response_radius_map"].shape == (8, 10)
    assert actual["summary"]["radial_response_sampling"] == (
        expected["summary"]["radial_response_sampling"])
    assert set(actual["summary"]["array_units"]) == set(actual["arrays"])

"""Analytical RAW replay invariants, CFA geometry, and actual legacy ISP equivalence."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.isp import ISPError, default_profile, process_raw, validate_profile


def raw_format():
    return {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": 64,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }


def geometry(origin=(0, 0)):
    return {"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": list(origin)}


def constant_raw(values=(464, 864, 1264), origin=(0, 0), size=(8, 10)):
    channels = np.array([[0, 1], [1, 2]])
    yy = np.arange(size[0])[:, None] + origin[0]
    xx = np.arange(size[1])[None, :] + origin[1]
    return np.asarray(values, dtype=np.uint16)[channels[yy % 2, xx % 2]]


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_all_native_crop_phases_preserve_constant_rgb_including_boundaries(origin):
    raw = constant_raw(origin=origin)
    profile = default_profile()
    profile["tone"]["curve"] = "linear"
    result = process_raw(raw, raw_format(), profile, geometry=geometry(origin))
    expected = np.broadcast_to(np.array([400, 800, 1200]) / 4031, (1, 8, 10, 3))
    np.testing.assert_allclose(result["arrays"]["rgb_frames"], expected, rtol=1e-15)
    assert result["summary"]["geometry"]["crop_origin"] == list(origin)
    assert result["summary"]["black_subtractions"] == 1
    assert result["summary"]["frame_max_normalization"] is False


def test_independent_impulse_bilinear_weights_and_native_samples():
    raw = np.full((8, 10), 64, dtype=np.uint16)
    raw[2, 2] = 1064  # R impulse away from reflected boundaries.
    result = process_raw(raw, raw_format(), default_profile(), geometry=geometry())
    red = result["arrays"]["demosaiced_rgb_frames"][0, ..., 0]
    signal = 1000 / 4031
    assert red[2, 2] == signal
    assert red[2, 3] == signal / 2
    assert red[3, 3] == signal / 4
    assert red[4, 4] == 0
    np.testing.assert_array_equal(result["arrays"]["demosaiced_rgb_frames"][..., 1:], 0)


def test_nonidentity_wb_then_ccm_has_exact_channel_order_and_retains_stages():
    profile = default_profile()
    profile["white_balance"]["gains_rgb"] = [2, 0.5, 1]
    profile["ccm"]["matrix_rgb"] = [[0, 1, 0], [1, 0, -0.25], [0.2, 0.3, 0.5]]
    profile["tone"]["curve"] = "linear"
    result = process_raw(constant_raw(), raw_format(), profile, geometry=geometry())
    arrays = result["arrays"]
    rgb = np.array([400, 800, 1200]) / 4031
    balanced = np.array([rgb[0] * 2, rgb[1] / 2, rgb[2]])
    expected = np.array(
        [
            balanced[1],
            balanced[0] - balanced[2] / 4,
            balanced[0] / 5 + balanced[1] * 0.3 + balanced[2] / 2,
        ]
    )
    np.testing.assert_allclose(arrays["demosaiced_rgb_frames"][0, 3, 3], rgb, rtol=1e-15)
    np.testing.assert_allclose(arrays["white_balanced_rgb_frames"][0, 3, 3], balanced, rtol=1e-15)
    np.testing.assert_allclose(arrays["ccm_rgb_frames"][0, 3, 3], expected, rtol=1e-15)
    np.testing.assert_allclose(arrays["rgb"][3, 3], expected, rtol=1e-15)
    baseline = process_raw(constant_raw(), raw_format(), default_profile(), geometry=geometry())
    assert not np.array_equal(arrays["rgb"], baseline["arrays"]["rgb"])
    assert result["summary"]["profile_sha256"] != baseline["summary"]["profile_sha256"]


def test_negative_linear_sign_and_upper_range_survive_until_explicit_tone_clip():
    raw = constant_raw((32, 3000, 2000))
    profile = default_profile()
    profile["white_balance"]["gains_rgb"] = [2, 2, 2]
    profile["tone"]["curve"] = "linear"
    result = process_raw(raw, raw_format(), profile, geometry=geometry())
    arrays = result["arrays"]
    expected = (np.array([32, 3000, 2000]) - 64) / 4031 * 2
    np.testing.assert_allclose(arrays["ccm_rgb_frames"][0, 3, 3], expected)
    np.testing.assert_allclose(arrays["rgb_frames"][0, 3, 3], [0, 1, expected[2]])
    assert arrays["black_subtracted_normalized_frames"].min() < 0
    assert arrays["demosaiced_rgb_frames"][..., 0].max() < 0
    assert arrays["white_balanced_rgb_frames"][..., 1].min() > 1
    diagnostic = result["summary"]["frame_diagnostics"][0]
    assert diagnostic["below_black_sample_count"] == 20
    assert diagnostic["tone_low_clipped_channel_samples"] == 80
    assert diagnostic["tone_high_clipped_channel_samples"] == 80


def test_negative_ccm_coefficients_are_not_early_clipped():
    profile = default_profile()
    profile["ccm"]["matrix_rgb"][0] = [1, -2, 0]
    result = process_raw(constant_raw(), raw_format(), profile, geometry=geometry())
    assert np.all(result["arrays"]["ccm_rgb_frames"][..., 0] < 0)
    assert np.all(result["arrays"]["rgb_frames"][..., 0] == 0)


def test_srgb_transfer_matches_independent_piecewise_formula():
    raw = constant_raw((65, 80, 2600))
    result = process_raw(raw, raw_format(), default_profile(), geometry=geometry())
    linear = (np.array([65, 80, 2600]) - 64) / 4031
    expected = np.array(
        [
            12.92 * linear[0],
            1.055 * linear[1] ** (1 / 2.4) - 0.055,
            1.055 * linear[2] ** (1 / 2.4) - 0.055,
        ]
    )
    np.testing.assert_allclose(result["arrays"]["rgb"][0, 0], expected, rtol=1e-14)


def test_readonly_raw_and_metadata_stay_unchanged_all_frames_deterministic():
    raw = np.stack([constant_raw(), constant_raw((864, 1264, 1664))])
    raw.flags.writeable = False
    before = raw.tobytes()
    fmt, geom, profile = raw_format(), geometry(), default_profile()
    arguments = deepcopy((fmt, geom, profile))
    first = process_raw(raw, fmt, profile, geometry=geom)
    second = process_raw(raw, fmt, profile, geometry=geom)
    assert raw.tobytes() == before
    assert (fmt, geom, profile) == arguments
    assert first["summary"] == second["summary"]
    assert first["summary"]["input_array_bytes_sha256"] == hashlib.sha256(before).hexdigest()
    assert first["arrays"]["rgb_frames"].shape == (2, 8, 10, 3)
    assert not np.array_equal(first["arrays"]["rgb_frames"][0], first["arrays"]["rgb_frames"][1])
    for name, value in first["arrays"].items():
        assert value.dtype == np.float64
        assert not np.shares_memory(value, raw)
        np.testing.assert_array_equal(value, second["arrays"][name])
    first["arrays"]["rgb"][0, 0] = 0
    assert not np.array_equal(first["arrays"]["rgb"], first["arrays"]["rgb_frames"][0])
    assert first["summary"]["physical_validation"] == "not_established"
    assert all(len(h) == 64 for h in first["summary"]["implementation"]["source_sha256"].values())
    json.dumps(first["summary"], allow_nan=False)


def test_local_crop_does_not_claim_full_frame_boundary_equivalence():
    raw = np.arange(16 * 20, dtype=np.uint16).reshape(16, 20) + 64
    full = process_raw(raw, raw_format(), default_profile(), geometry=geometry())
    crop = raw[1:9, 1:11]
    cut = process_raw(crop, raw_format(), default_profile(), geometry=geometry((1, 1)))
    # Interior bilinear support is identical; reflected crop boundaries are not.
    np.testing.assert_allclose(
        cut["arrays"]["rgb"][1:-1, 1:-1], full["arrays"]["rgb"][2:8, 2:10], rtol=1e-14
    )
    assert not np.array_equal(cut["arrays"]["rgb"][0], full["arrays"]["rgb"][1, 1:11])
    assert any("crop boundary" in s for s in cut["summary"]["limitations"])


def test_representative_geometry_remains_labeled_and_does_not_resize():
    geom = {
        "kind": "representative_normalized_field_raster",
        "raster_shape": [8, 10],
        "mapping": "center_to_corner_radius_v1",
    }
    result = process_raw(constant_raw(), raw_format(), default_profile(), geometry=geom)
    assert result["summary"]["geometry"] == geom
    assert result["arrays"]["rgb"].shape == (8, 10, 3)
    geom["raster_shape"] = [16, 20]
    with pytest.raises(ISPError, match="match RAW"):
        process_raw(constant_raw(), raw_format(), default_profile(), geometry=geom)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(exposure_s=0.008),
        lambda p: p.update(graph=list(reversed(p["graph"]))),
        lambda p: p.update(precision="uint16"),
        lambda p: p["black_level"].update(negative_policy="clip"),
        lambda p: p["demosaic"].update(method="adaptive"),
        lambda p: p["white_balance"].update(mode="auto"),
        lambda p: p["white_balance"].update(gains_rgb=[1, float("nan"), 1]),
        lambda p: p["white_balance"].update(gains_rgb=[1, 10**400, 1]),
        lambda p: p["white_balance"].update(gains_rgb=[1, True, 1]),
        lambda p: p["white_balance"].update(gains_rgb=[1, 17, 1]),
        lambda p: p["white_balance"].update(gains_rgb=[1, 0, 1]),
        lambda p: p["ccm"].update(matrix_rgb=[[1, 0], [0, 1]]),
        lambda p: p["ccm"].update(matrix_rgb=[[float("inf"), 0, 0]] * 3),
        lambda p: p["ccm"].update(matrix_rgb=[[8, 8, 8]] * 3),
        lambda p: p["ccm"].update(orientation="input_rows_output_columns"),
        lambda p: p["tone"].update(clip_input=[False, True]),
        lambda p: p["tone"].update(curve="adaptive"),
        lambda p: p.update(disabled_effects=[]),
    ],
)
def test_malformed_unbounded_or_unimplemented_tuning_refused(mutate):
    profile = default_profile()
    mutate(profile)
    with pytest.raises(ISPError) as raised:
        validate_profile(profile)
    assert raised.value.issues[0]["severity"] == "error"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.update(cfa="BGGR"),
        lambda f: f.update(packing="mipi_raw12"),
        lambda f: f.update(bit_depth=12.0),
        lambda f: f.update(black_level_dn=float("nan")),
        lambda f: f.update(black_level_dn=10**400),
        lambda f: f.update(white_level_dn=4096),
        lambda f: f.update(black_level=65),
        lambda f: f.update(hdr_streams=True),
        lambda f: f.update(gain=2),
        lambda f: f.pop("response_type"),
    ],
)
def test_undeclared_format_and_acquisition_changes_refused(mutate):
    fmt = raw_format()
    mutate(fmt)
    with pytest.raises(ISPError):
        process_raw(constant_raw(), fmt, default_profile(), geometry=geometry())


@pytest.mark.parametrize(
    "raw",
    [
        np.zeros((8, 10), dtype=float),
        np.zeros((8, 10), dtype=bool),
        np.zeros((8, 10), dtype=object),
        np.zeros((8, 10), dtype=np.uint64),
        np.full((8, 10), -1, dtype=np.int16),
        np.full((8, 10), 4096, dtype=np.uint16),
        np.zeros((0, 8, 10), dtype=np.uint16),
        np.zeros((8, 9), dtype=np.uint16),
        np.broadcast_to(np.uint16(64), (65, 2, 2)),
        np.broadcast_to(np.uint16(64), (2, 1024, 1024)),
    ],
)
def test_malformed_pixels_and_workload_refused_before_processing(raw):
    with pytest.raises(ISPError):
        process_raw(raw, raw_format(), default_profile(), geometry=geometry())


@pytest.mark.parametrize(
    "geom",
    [
        None,
        {"kind": "native_sensor", "full_sensor_shape": [8, 10], "crop_origin": [1, 1]},
        {"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": [1.0, 0]},
        {
            "kind": "native_sensor",
            "full_sensor_shape": [20, 24],
            "crop_origin": [0, 0],
            "flip": True,
        },
    ],
)
def test_missing_geometry_unknown_transforms_and_invalid_crop_refused(geom):
    with pytest.raises(ISPError):
        process_raw(constant_raw(), raw_format(), default_profile(), geometry=geom)


def test_profile_boundary_values_normalize_without_aliasing():
    profile = default_profile()
    profile["white_balance"]["gains_rgb"] = [1 / 16, 1, 16]
    profile["ccm"]["matrix_rgb"] = [[8, -8, 0], [0, 0, 0], [0, -8, 8]]
    parsed = validate_profile(profile)
    assert parsed == profile
    parsed["white_balance"]["gains_rgb"][0] = 1
    assert profile["white_balance"]["gains_rgb"][0] == 1 / 16


def test_adc_endpoints_are_diagnosed_without_changing_original_codes():
    raw = constant_raw((0, 4095, 64))
    before = raw.copy()
    result = process_raw(raw, raw_format(), default_profile(), geometry=geometry())
    np.testing.assert_array_equal(raw, before)
    diagnostic = result["summary"]["frame_diagnostics"][0]
    assert diagnostic["raw_zero_code_count"] == 20
    assert diagnostic["raw_white_code_count"] == 40
    np.testing.assert_allclose(result["arrays"]["rgb"][0, 0], [0, 1, 0], rtol=0, atol=2e-16)
    assert result["summary"]["uncertainty"] == {"kind": "unknown"}


def test_24_bit_codes_have_exact_black_subtraction_before_normalization():
    raw = np.full((8, 10), 2**24 - 2, dtype=np.uint32)
    fmt = {**raw_format(), "bit_depth": 24, "black_level_dn": 2**24 - 2}
    result = process_raw(raw, fmt, default_profile(), geometry=geometry())
    np.testing.assert_array_equal(result["arrays"]["black_subtracted_normalized_frames"], 0)
    np.testing.assert_array_equal(result["arrays"]["rgb_frames"], 0)


def test_nonfinite_backend_result_is_refused_without_fallback(monkeypatch):
    import pyisetcam.ip

    monkeypatch.setattr(pyisetcam.ip, "_ie_bilinear", lambda planes, pattern: planes * np.nan)
    with pytest.raises(ISPError) as raised:
        process_raw(constant_raw(), raw_format(), default_profile(), geometry=geometry())
    assert raised.value.issues[0]["code"] == "NUMERICAL_FAILURE"


def test_default_profile_matches_actual_legacy_pyisetcam_pipeline():
    from pyisetcam.ip import ip_compute, ip_create, ip_set
    from pyisetcam.sensor import sensor_create, sensor_set
    from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture

    sensor = sensor_create("bayer-rggb", {"size": [8, 10], "wave": [450, 550, 650]})
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = configure_e2e_sensor(
        sensor,
        readout={
            "full_well_e": 10000,
            "read_noise_e": 0,
            "dark_current_e_per_s": 0,
            "gain": 1,
            "bit_depth": 12,
            "black_level_dn": 64,
        },
    )
    sensor.data["mean_electrons"] = np.arange(80).reshape(8, 10) * 71.0
    capture = resample_sensor_capture(sensor, seed=41)
    processor = ip_create(sensor=capture)
    processor = ip_set(processor, "conversion method sensor", "none")
    processor.fields["render"]["scale"] = False
    legacy = ip_compute(processor, capture)
    result = process_raw(
        capture.data["raw_dn"], raw_format(), default_profile(), geometry=geometry()
    )
    np.testing.assert_array_equal(result["arrays"]["rgb"], legacy.data["srgb"])
    np.testing.assert_array_equal(
        result["arrays"]["demosaiced_rgb_frames"][0], legacy.data["sensorspace"]
    )

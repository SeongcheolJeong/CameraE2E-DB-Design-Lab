"""Native ISP v2 checks with independent interpolation/filter arithmetic."""

import hashlib
import json
import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp
from camera_platform import isp_v2 as v2


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


def geometry(shape=(12, 14), origin=(0, 0)):
    return {"kind": "native_sensor", "full_sensor_shape": list(shape), "crop_origin": list(origin)}


def calibration(shape=(12, 14)):
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, shape[0] - 1],
            "column_nodes": [0, shape[1] - 1],
            "gains": [
                [[1.0, 1.1, 1.4, 1.7], [1.3, 1.8, 1.7, 2.0]],
                [[1.5, 1.3, 1.9, 2.1], [2.0, 2.2, 2.3, 2.4]],
            ],
            "valid_mask": np.ones((2, 2, 4), dtype=bool).tolist(),
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {"row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2},
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5, 0.6, 0.7, 0.8],
            "intercept_dn2": [4.0, 9.0, 16.0, 25.0],
            "signal_interval_dn": [-64, 4031],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


def active_profile(*stages):
    p = v2.default_profile(
        "calibration-fixture" if any(s != "linear_sharpen" for s in stages) else None
    )
    modes = {
        "spatial_lsc": "native_four_site_grid_v1",
        "raw_denoise": "calibrated_same_site_filter_v1",
        "linear_sharpen": "bounded_detail_v1",
    }
    for name in stages:
        p[name]["mode"] = modes[name]
    return p


def execute(raw, profile=None, cal=None, geom=None):
    return v2.process_raw(
        raw,
        raw_format(),
        profile or v2.default_profile(),
        geometry=geom or geometry(),
        calibration=cal,
    )


def site_at(y, x):
    return 2 * (y % 2) + x % 2


def reflect(index, length):
    if length == 1:
        return 0
    while not 0 <= index < length:
        index = -index if index < 0 else 2 * length - 2 - index
    return index


def filter_oracle(value, hint, valid, strength, floor):
    result = value.copy()
    for frame, y, x in np.ndindex(value.shape):
        if not valid[frame, y, x]:
            continue
        total, denominator = 0.0, 0.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy = 2 * reflect(y // 2 + dy, value.shape[1] // 2) + y % 2
                xx = 2 * reflect(x // 2 + dx, value.shape[2] // 2) + x % 2
                if not valid[frame, yy, xx]:
                    continue
                spatial = (2 if dy == 0 else 1) * (2 if dx == 0 else 1)
                delta = value[frame, yy, xx] - value[frame, y, x]
                denominator_hint = max(hint[frame, yy, xx] + hint[frame, y, x], floor)
                weight = spatial * math.exp(-delta * delta / (2 * denominator_hint))
                total += weight * value[frame, yy, xx]
                denominator += weight
        result[frame, y, x] += strength * (total / denominator - value[frame, y, x])
    return result


@pytest.mark.parametrize("origin", [(0, 0), (0, 1), (1, 0), (1, 1)])
@pytest.mark.parametrize("curve", ["linear", "srgb"])
def test_all_bypass_matches_every_v1_common_array_bytes_and_preserves_signed_raw(origin, curve):
    raw = np.random.default_rng(4).integers(0, 200, (2, 8, 10), dtype=np.int32)
    raw[0, 0, 0], raw[1, 0, 0] = 0, 4095
    raw.setflags(write=False)
    before = hashlib.sha256(raw.tobytes()).hexdigest()
    profile = v2.default_profile()
    profile["tone"]["curve"] = curve
    profile["white_balance"]["gains_rgb"] = [1.2, 0.9, 1.1]
    profile["ccm"]["matrix_rgb"] = [[1.1, -0.1, 0], [-0.2, 1.2, 0], [0, -0.1, 1.1]]
    legacy = isp.default_profile()
    for key in ("tone", "white_balance", "ccm"):
        legacy[key] = profile[key]
    expected = isp.process_raw(raw, raw_format(), legacy, geometry=geometry(origin=origin))
    actual = execute(raw, profile, geom=geometry(origin=origin))
    for key, array in expected["arrays"].items():
        assert actual["arrays"][key].dtype == array.dtype
        assert actual["arrays"][key].tobytes() == array.tobytes()
    assert (
        actual["arrays"]["baseline_rgb_frames"].tobytes()
        == expected["arrays"]["rgb_frames"].tobytes()
    )
    assert actual["arrays"]["black_subtracted_normalized_frames"].min() < 0
    assert actual["summary"]["profile_sha256"] != expected["summary"]["profile_sha256"]
    assert actual["summary"]["input_array_bytes_sha256"] == before
    assert hashlib.sha256(raw.tobytes()).hexdigest() == before
    assert set(actual["arrays"]) == set(actual["summary"]["array_units"])
    assert actual["summary"]["uncertainty"] == {"kind": "unknown"}
    json.dumps(actual["summary"], allow_nan=False)


@pytest.mark.parametrize("origin", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_asymmetric_four_site_lsc_uses_absolute_native_centers_once(origin):
    raw = np.full((8, 10), 164, dtype=np.uint16)
    raw[0, 0] = 32
    cal = calibration()
    before = deepcopy(cal)
    result = execute(raw, active_profile("spatial_lsc"), cal, geometry(origin=origin))
    expected = np.empty(raw.shape)
    nodes = cal["lsc"]["gains"]
    for y, x in np.ndindex(raw.shape):
        yy, xx = y + origin[0], x + origin[1]
        fy, fx = yy / 11, xx / 13
        site = site_at(yy, xx)
        expected[y, x] = (1 - fy) * ((1 - fx) * nodes[0][0][site] + fx * nodes[0][1][site]) + fy * (
            (1 - fx) * nodes[1][0][site] + fx * nodes[1][1][site]
        )
        assert result["arrays"]["native_site_index"][y, x] == site
    np.testing.assert_allclose(result["arrays"]["lsc_gain_map"], expected, rtol=1e-15)
    np.testing.assert_allclose(
        result["arrays"]["lsc_normalized_frames"][0],
        (raw.astype(float) - 64) / 4031 * expected,
        rtol=1e-15,
    )
    assert result["arrays"]["lsc_normalized_frames"][0, 0, 0] < 0
    assert cal == before


def test_zero_weight_invalid_grid_nodes_do_not_poison_valid_node_but_interiors_bypass():
    cal = calibration((8, 8))
    cal["lsc"]["valid_mask"] = np.zeros((2, 2, 4), dtype=bool).tolist()
    cal["lsc"]["valid_mask"][0][0] = [True] * 4
    cal["lsc"]["gains"][0][0] = [2.0] * 4
    result = execute(
        np.full((8, 8), 164, dtype=np.uint16), active_profile("spatial_lsc"), cal, geometry((8, 8))
    )["arrays"]
    assert result["lsc_gain_map"][0, 0] == 2
    assert result["lsc_calibration_valid_mask"][0, 0]
    assert not result["lsc_calibration_valid_mask"][2, 2]
    assert result["lsc_gain_map"][2, 2] == 1
    assert (
        result["lsc_normalized_frames"][0, 2, 2]
        == result["black_subtracted_normalized_frames"][0, 2, 2]
    )


def test_denoise_matches_scalar_same_site_filter_with_invalid_neighbors_and_lsc_noise_units():
    raw = np.random.default_rng(22).integers(40, 180, (2, 8, 8), dtype=np.uint16)
    raw[0, 2, 2] = 0
    raw[1, 3, 3] = 4095
    cal = calibration((8, 8))
    cal["noise"]["signal_interval_dn"] = [-10, 100]
    profile = active_profile("spatial_lsc", "raw_denoise")
    profile["raw_denoise"].update(strength=0.8, range_floor_dn2=3.0)
    result = execute(raw, profile, cal, geometry((8, 8)))
    arrays = result["arrays"]
    signal = raw.astype(float) - 64
    site = np.fromfunction(lambda y, x: 2 * (y % 2) + x % 2, (8, 8), dtype=int)
    valid = (raw > 0) & (raw < 4095) & (signal >= -10) & (signal <= 100)
    variance_dn = np.asarray(cal["noise"]["slope_dn"])[site] * np.maximum(signal, 0)
    variance_dn += np.asarray(cal["noise"]["intercept_dn2"])[site]
    variance_dn = np.where(valid, variance_dn, 0)
    variance = variance_dn * arrays["lsc_gain_map"] ** 2 / 4031**2
    np.testing.assert_array_equal(arrays["noise_hint_valid_mask_frames"], valid)
    np.testing.assert_allclose(arrays["noise_variance_hint_dn2_frames"], variance_dn)
    np.testing.assert_allclose(arrays["noise_variance_hint_normalized_frames"], variance)
    expected = filter_oracle(arrays["lsc_normalized_frames"], variance, valid, 0.8, 3 / 4031**2)
    np.testing.assert_allclose(
        arrays["denoised_normalized_frames"], expected, rtol=1e-14, atol=1e-17
    )
    np.testing.assert_array_equal(
        arrays["denoised_normalized_frames"][~valid], arrays["lsc_normalized_frames"][~valid]
    )
    assert result["summary"]["noise_hint"]["uncertainty"] == {"kind": "unknown"}
    assert "post-LSC" in result["summary"]["noise_hint"]["range_floor_unit"]


def test_zero_strength_is_exact_identity_and_unequal_greens_do_not_mix():
    site = np.fromfunction(lambda y, x: 2 * (y % 2) + x % 2, (8, 8), dtype=int)
    raw = np.take([100, 200, 400, 800], site).astype(np.uint16)
    profile, cal = active_profile("raw_denoise"), calibration((8, 8))
    cal["noise"]["slope_dn"] = [0] * 4
    cal["noise"]["intercept_dn2"] = [1e6] * 4
    profile["raw_denoise"]["strength"] = 1
    arrays = execute(raw, profile, cal, geometry((8, 8)))["arrays"]
    np.testing.assert_allclose(
        arrays["denoised_normalized_frames"],
        arrays["black_subtracted_normalized_frames"],
        atol=1e-16,
    )
    raw[2, 2] += 50
    profile["raw_denoise"]["strength"] = 0
    cal["noise"]["signal_interval_dn"] = [0, 1]  # Unused hints do not invalidate identity.
    result = execute(raw, profile, cal, geometry((8, 8)))
    assert (
        result["arrays"]["denoised_normalized_frames"].tobytes()
        == result["arrays"]["black_subtracted_normalized_frames"].tobytes()
    )
    assert result["arrays"]["denoise_valid_mask_frames"].all()
    assert not result["summary"]["stage_outputs"]["raw_denoise"]["applied"]


def test_noise_hint_changes_impulse_and_edge_response_without_cross_frame_mixing():
    raw = np.full((2, 8, 8), 164, dtype=np.uint16)
    raw[0, 2, 2], raw[0, :, 4:] = 600, 400
    profile, cal = active_profile("raw_denoise"), calibration((8, 8))
    profile["raw_denoise"]["strength"] = 1
    cal["noise"]["slope_dn"] = [0] * 4
    cal["noise"]["intercept_dn2"] = [0] * 4
    low = execute(raw, profile, cal, geometry((8, 8)))["arrays"]
    cal["noise"]["intercept_dn2"] = [1e6] * 4
    high = execute(raw, profile, cal, geometry((8, 8)))["arrays"]
    assert high["denoised_normalized_frames"][0, 2, 2] < low["denoised_normalized_frames"][0, 2, 2]
    assert high["denoised_normalized_frames"][0, 4, 4] < low["denoised_normalized_frames"][0, 4, 4]
    np.testing.assert_allclose(high["denoised_normalized_frames"][1], (164 - 64) / 4031, atol=1e-17)


def test_sharpen_scalar_bound_signed_values_threshold_and_invalid_stencil_bypass():
    raw = np.full((10, 10), 40, dtype=np.uint16)
    raw[:, 5:] = 3900
    raw[5, 5] = 4095
    profile = active_profile("linear_sharpen")
    profile["linear_sharpen"].update(amount=0.7, threshold=0.002, max_delta=0.03)
    arrays = execute(raw, profile, geom=geometry((10, 10)))["arrays"]
    source = arrays["ccm_rgb_frames"]
    guide = source.mean(axis=-1)
    expected = np.zeros(guide.shape)
    for frame, y, x in np.ndindex(guide.shape):
        if not arrays["sharpen_stencil_valid_mask_frames"][frame, y, x]:
            continue
        blur = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                weight = (2 if dy == 0 else 1) * (2 if dx == 0 else 1) / 16
                blur += weight * guide[frame, reflect(y + dy, 10), reflect(x + dx, 10)]
        detail = guide[frame, y, x] - blur
        expected[frame, y, x] = math.copysign(min(0.03, 0.7 * max(abs(detail) - 0.002, 0)), detail)
    np.testing.assert_allclose(arrays["sharpen_delta_frames"], expected, atol=1e-15)
    np.testing.assert_allclose(
        arrays["sharpened_rgb_frames"], source + expected[..., None], atol=1e-15
    )
    assert np.max(np.abs(arrays["sharpen_delta_frames"])) <= 0.03
    assert arrays["sharpen_delta_frames"].min() < 0 < arrays["sharpen_delta_frames"].max()
    assert arrays["sharpened_rgb_frames"].min() < 0
    assert not arrays["sharpen_applied_mask_frames"][0, 5, 5]
    assert arrays["rgb_frames"].min() >= 0
    assert arrays["rgb_frames"].max() <= 1


def test_full_native_vs_odd_crop_matches_inside_declared_four_pixel_halo():
    raw = np.random.default_rng(31).integers(80, 400, (24, 28), dtype=np.uint16)
    profile, cal = (
        active_profile("spatial_lsc", "raw_denoise", "linear_sharpen"),
        calibration(raw.shape),
    )
    full = execute(raw, profile, cal, geometry(raw.shape))
    crop = execute(raw[3:19, 5:23].copy(), profile, cal, geometry(raw.shape, (3, 5)))
    assert crop["summary"]["crop_full_frame_comparison_halo_native_pixels"] == 4
    np.testing.assert_allclose(
        crop["arrays"]["rgb_frames"][:, 4:-4, 4:-4],
        full["arrays"]["rgb_frames"][:, 7:15, 9:19],
        atol=1e-15,
    )


@pytest.mark.parametrize("shape", [(2, 2), (2, 4), (4, 2)])
def test_small_native_sublattices_reflect_without_fabricating_other_sites(shape):
    raw = np.full(shape, 100, dtype=np.uint16)
    result = execute(raw, active_profile("raw_denoise"), calibration(shape), geometry(shape))
    np.testing.assert_allclose(result["arrays"]["denoised_normalized_frames"], (100 - 64) / 4031)


def test_all_invalid_denoise_stencil_preserves_original_values_and_false_masks():
    raw = np.zeros((8, 8), dtype=np.uint16)
    result = execute(
        raw, active_profile("raw_denoise", "linear_sharpen"), calibration((8, 8)), geometry((8, 8))
    )
    arrays = result["arrays"]
    assert not arrays["denoise_valid_mask_frames"].any()
    assert not arrays["rgb_valid_mask_frames"].any()
    assert not arrays["sharpen_applied_mask_frames"].any()
    np.testing.assert_array_equal(
        arrays["denoised_normalized_frames"], arrays["black_subtracted_normalized_frames"]
    )
    assert all(np.isfinite(array).all() for array in arrays.values())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema=isp.SCHEMA),
        lambda p: p["graph"].insert(2, "spatial_lsc"),
        lambda p: p.update(extra={}),
        lambda p: p["raw_denoise"].update(strength=True),
        lambda p: p["raw_denoise"].update(range_floor_dn2=0),
        lambda p: p["linear_sharpen"].update(amount=1.01),
        lambda p: p["linear_sharpen"].update(max_delta=-1),
        lambda p: p["spatial_lsc"].update(mode="automatic_fit"),
        lambda p: p["spatial_lsc"].update(mode="native_four_site_grid_v1"),
    ],
)
def test_invalid_profile_and_duplicate_stage_are_refused(mutation):
    profile = v2.default_profile()
    mutation(profile)
    with pytest.raises(isp.ISPError):
        v2.validate_profile(profile)


def test_preflight_rejects_missing_domain_bad_units_paired_raw_and_resource_before_stages(
    monkeypatch,
):
    raw = np.full((8, 8), 100, dtype=np.uint16)
    profile, cal = active_profile("spatial_lsc"), calibration((8, 8))
    with pytest.raises(ValueError, match="missing"):
        execute(raw, profile, geom=geometry((8, 8)))
    bad = deepcopy(cal)
    bad["lsc"]["row_nodes"] = [1, 7]
    bad["lsc"]["reference_roi"].update(row_start=1, row_end=2)
    with pytest.raises(ValueError, match="cover"):
        v2.validate_inputs(raw, raw_format(), profile, geometry=geometry((8, 8)), calibration=bad)
    bad = deepcopy(cal)
    bad["noise"]["reference"] = "electrons"
    with pytest.raises(ValueError, match="pre-LSC"):
        execute(raw, profile, bad, geometry((8, 8)))
    with pytest.raises(ValueError, match="native"):
        execute(
            raw,
            geom={
                "kind": "representative_normalized_field_raster",
                "raster_shape": [8, 8],
                "mapping": "center_to_corner_radius_v1",
            },
        )
    with pytest.raises(ValueError, match="integer"):
        execute(raw[None, None])
    monkeypatch.setattr(v2, "MAX_WORKING_ARRAY_BYTES", 1)
    monkeypatch.setattr(v2, "_lsc_map", lambda *args: pytest.fail("stage ran before preflight"))
    with pytest.raises(isp.ISPError, match="bounds"):
        execute(raw, profile, cal, geometry((8, 8)))


def test_output_budget_and_floor_underflow_are_preflight_failures(monkeypatch):
    raw = np.full((8, 8), 100, dtype=np.uint16)
    profile = active_profile("raw_denoise")
    profile["raw_denoise"]["range_floor_dn2"] = 5e-324
    with pytest.raises(ValueError, match="numerical"):
        v2.validate_inputs(
            raw, raw_format(), profile, geometry=geometry((8, 8)), calibration=calibration((8, 8))
        )
    monkeypatch.setattr(v2, "MAX_OUTPUT_ARRAY_BYTES", 1)
    with pytest.raises(isp.ISPError, match="bounds"):
        execute(raw, geom=geometry((8, 8)))


def test_profile_and_numeric_calibration_are_detached_and_raw_bytes_are_unchanged():
    profile, cal = active_profile("spatial_lsc", "raw_denoise"), calibration((8, 8))
    original_profile, original_cal = deepcopy(profile), deepcopy(cal)
    raw = np.full((8, 8), 120, dtype=np.uint16)
    before = raw.tobytes()
    result = execute(raw, profile, cal, geometry((8, 8)))
    result["summary"]["profile"]["raw_denoise"]["strength"] = 0.1
    assert profile == original_profile and cal == original_cal
    assert raw.tobytes() == before
    assert all(np.isfinite(a).all() for a in result["arrays"].values())
    for stage in result["summary"]["stage_outputs"].values():
        assert stage["array"] in result["arrays"] and stage["validity"] in result["arrays"]

"""Independent charge/filter oracles and old-preview invariants for ISP v3."""

import hashlib
import json
import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp, isp_v3, nonlinear_isp, nonlinear_readout


def model(*, linear=False, capacity=2000, read_variance=0):
    return {
        "schema": "camera_electrical_readout_v2",
        "full_well_e": capacity,
        "dark_current_e_per_s": 0,
        "reference_temperature_c": 25,
        "dark_current_doubling_c": None,
        "digital_gain": 1,
        "adc": {"bit_depth": 12, "full_scale_v": 0.4095, "black_level_dn": 64},
        "fixed_pattern": {"prnu_sigma": 0, "dsnu_e_per_s": 0, "seed": 7},
        "charge_voltage_response": {
            "schema": "camera_charge_voltage_response_v1",
            "units": {"charge": "electron", "voltage": "V"},
            "charge_e": [0, 1000, 2000],
            "signal_voltage_v": [0, 0.1, 0.2 if linear else 0.15],
            "valid_domain": {"temperature_c": [20, 30], "analog_gain": [1, 4]},
            "signed_inverse": {"minimum_voltage_v": -0.02, "method": "origin_tangent"},
            "source": {"kind": "estimated", "description": "Authored monotone fixture"},
        },
        "read_noise": {
            "schema": "camera_voltage_read_noise_v1",
            "plane": "conversion_output_before_analog_gain",
            "variance_v2": read_variance,
            "temperature_range_c": [20, 30],
            "analog_gain_range": [1, 4],
            "includes": "all_temporal_read_components",
            "source": {"kind": "estimated", "description": "Declared read variance"},
        },
        "source": {"kind": "estimated", "description": "Authored nonlinear fixture"},
    }


def geometry(origin=(0, 0), full=(20, 24)):
    return {"kind": "native_sensor", "full_sensor_shape": list(full), "crop_origin": list(origin)}


def calibration(shape=(8, 10), origin=(0, 0), dark=0, noise=False, **options):
    geom = geometry(origin)
    return nonlinear_readout.make_calibration(
        model(**options),
        analog_gain=1,
        temperature_c=25,
        exposure_s=1,
        geometry={key: geom[key] for key in ("full_sensor_shape", "crop_origin")},
        dark_e=np.full(shape, float(dark)),
        noise_enabled=noise,
    )


def profile(*stages, curve="linear"):
    base = isp.default_profile()
    base["tone"]["curve"] = curve
    result = isp_v3.default_profile(base, "fixture-grid" if "spatial_lsc" in stages else None)
    for stage in stages:
        result[stage]["mode"] = {
            "spatial_lsc": "native_four_site_grid_v1",
            "raw_denoise": "local_charge_variance_filter_v1",
            "linear_sharpen": "bounded_detail_v1",
        }[stage]
    return result


def grid(gains=(2, 3, 4, 5)):
    return {
        "schema": "camera_charge_isp_calibration_numeric_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, 19],
            "column_nodes": [0, 23],
            "gains": [[list(gains), list(gains)], [list(gains), list(gains)]],
            "valid_mask": np.ones((2, 2, 4), dtype=bool).tolist(),
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {"row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2},
        },
    }


def execute(raw, *, p=None, cals=None, origin=(0, 0), norm=2000, numeric=None):
    cals = cals if cals is not None else [calibration(raw.shape[-2:], origin) for _ in raw]
    return isp_v3.process_raw(
        raw,
        cals,
        profile() if p is None else p,
        geometry=geometry(origin),
        normalization_e=norm,
        calibration=numeric,
    )


def reflect(index, size):
    if size == 1:
        return 0
    while index < 0 or index >= size:
        index = -index if index < 0 else 2 * size - 2 - index
    return index


def filter_oracle(value, variance, valid, strength, floor):
    result = value.copy()
    for frame, y, x in np.ndindex(value.shape):
        if not valid[frame, y, x]:
            continue
        numerator, denominator = 0.0, 0.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy = 2 * reflect(y // 2 + dy, value.shape[1] // 2) + y % 2
                xx = 2 * reflect(x // 2 + dx, value.shape[2] // 2) + x % 2
                if not valid[frame, yy, xx]:
                    continue
                spatial = (2 if dy == 0 else 1) * (2 if dx == 0 else 1)
                delta = value[frame, yy, xx] - value[frame, y, x]
                denom = max(variance[frame, yy, xx] + variance[frame, y, x], floor)
                weight = spatial * math.exp(-delta * delta / (2 * denom))
                numerator += weight * value[frame, yy, xx]
                denominator += weight
        result[frame, y, x] += strength * (numerator / denominator - value[frame, y, x])
    return result


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
@pytest.mark.parametrize("curve", ["linear", "srgb"])
def test_all_bypass_exact_existing_nonlinear_preview_for_each_native_phase(origin, curve):
    raw = np.random.default_rng(4).integers(30, 1450, (2, 8, 10), dtype=np.uint16)
    raw[0, 3, 4], raw[1, 0, 0] = 0, 4095
    p = profile(curve=curve)
    p["white_balance"]["gains_rgb"] = [2, 0.5, 1]
    p["ccm"]["matrix_rgb"] = [[0, 1, 0], [1, 0, -0.25], [0.2, 0.3, 0.5]]
    cals = [calibration(origin=origin, dark=10), calibration(origin=origin, dark=20)]
    got = execute(raw, p=p, cals=cals, origin=origin)["arrays"]
    base = isp.default_profile()
    for key in ("white_balance", "ccm", "tone"):
        base[key] = p[key]
    for i in range(len(raw)):
        expected = nonlinear_isp.process_raw(raw[i], cals[i], base, geometry=geometry(origin))
        for key in (
            "black_subtracted_normalized_frames",
            "demosaiced_rgb_frames",
            "white_balanced_rgb_frames",
            "ccm_rgb_frames",
            "rgb_frames",
            "raw_valid_mask_frames",
            "linear_valid_mask_frames",
        ):
            np.testing.assert_array_equal(got[key][i], expected["arrays"][key][0], strict=True)
    np.testing.assert_array_equal(got["rgb_frames"], got["baseline_rgb_frames"], strict=True)
    np.testing.assert_array_equal(
        got["ccm_rgb_frames"], got["baseline_ccm_rgb_frames"], strict=True
    )


def test_linear_curve_equivalent_charge_and_fixed_shared_normalization_not_path_ceiling():
    raw = np.full((2, 8, 10), 564, np.uint16)  # independent linear oracle: 500 electrons
    cals = [calibration(linear=True, capacity=700), calibration(linear=True, capacity=1800)]
    got = execute(raw, cals=cals, norm=2000)
    np.testing.assert_allclose(got["arrays"]["rgb_frames"], 0.25, rtol=1e-14)
    np.testing.assert_allclose(got["arrays"]["calibrated_photocharge_e_frames"], 500, rtol=1e-14)
    assert got["summary"]["normalization_e"] == 2000
    assert got["summary"]["baseline"]["same_decode_and_normalization"]
    # The explicit normalization never expands either path's validity domain.
    raw[0] = 864
    masked = execute(raw, cals=cals, norm=2000)["arrays"]
    assert not masked["raw_valid_mask_frames"][0].any()
    assert masked["raw_valid_mask_frames"][1].all()


@pytest.mark.parametrize("charge", [500, 1500])
def test_distinct_linear_nonlinear_codes_recover_same_charge_not_pseudo_dn(charge):
    linear_code = 64 + charge
    nonlinear_code = 64 + (charge if charge <= 1000 else 1000 + (charge - 1000) // 2)
    outputs = []
    for linear, code in ((True, linear_code), (False, nonlinear_code)):
        raw = np.full((1, 8, 10), code, np.uint16)
        arrays = execute(raw, cals=[calibration(linear=linear)])["arrays"]
        np.testing.assert_allclose(arrays["calibrated_photocharge_e_frames"], charge, rtol=1e-14)
        outputs.append(arrays["rgb_frames"])
    np.testing.assert_allclose(outputs[0], outputs[1], rtol=1e-14)
    np.testing.assert_allclose(outputs[1], charge / 2000, rtol=1e-14)


def test_active_full_frame_and_odd_crop_agree_inside_declared_halo():
    raw = np.random.default_rng(28).integers(100, 900, (1, 20, 24), dtype=np.uint16)
    p = profile("spatial_lsc", "raw_denoise", "linear_sharpen")
    p["linear_sharpen"].update(threshold=0, max_delta=0.04)
    full = execute(raw, p=p, cals=[calibration((20, 24), dark=20, noise=True)], numeric=grid())
    cropped_raw = raw[:, 1:17, 3:21]
    crop = execute(
        cropped_raw,
        p=p,
        origin=(1, 3),
        numeric=grid(),
        cals=[calibration((16, 18), (1, 3), dark=20, noise=True)],
    )
    assert crop["summary"]["crop_full_frame_comparison_halo_native_pixels"] == 4
    for key in (
        "denoised_normalized_frames",
        "ccm_rgb_frames",
        "sharpened_rgb_frames",
        "rgb_frames",
    ):
        np.testing.assert_allclose(
            full["arrays"][key][:, 5:13, 7:17],
            crop["arrays"][key][:, 4:-4, 4:-4],
            rtol=1e-14,
            atol=1e-15,
        )


def test_nonlinear_knots_signed_dark_and_noise_keep_charge_meaning():
    raw = np.full((1, 8, 10), 1314, np.uint16)  # 1500 e, local inverse slope 20000 e/V
    raw[0, 0, 0], raw[0, 0, 1] = 54, 1064  # -10 e; interval crosses 1000 e knot
    got = execute(raw, cals=[calibration(dark=25, noise=True, read_variance=1e-8)])
    a = got["arrays"]
    assert a["calibrated_photocharge_e_frames"][0, 0, 0] == pytest.approx(-35)
    assert a["decoded_shot_variance_estimate_e2_frames"][0, 0, 0] == 0
    assert a["decoded_read_variance_e2_frames"][0, 3, 3] == pytest.approx(4)
    assert a["decoded_shot_variance_estimate_e2_frames"][0, 3, 3] == pytest.approx(1500)
    assert a["decoded_stochastic_variance_estimate_e2_frames"][0, 3, 3] == pytest.approx(1504)
    assert a["black_subtracted_normalized_frames"][0, 3, 3] == pytest.approx(1475 / 2000)
    assert a["quantization_knot_crossing_mask_frames"][0, 0, 1]
    assert a["photocharge_low_normalized_frames"][0, 0, 1] < 975 / 2000
    assert a["photocharge_high_normalized_frames"][0, 0, 1] > 975 / 2000
    assert got["summary"]["noise_hint"]["quantization_variance"] == "not_included"
    assert got["summary"]["array_units"]["decoded_read_variance_e2_frames"] == "electron^2"


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_lsc_native_sites_scale_signal_variance_and_bound_once(origin):
    raw = np.full((1, 8, 10), 564, np.uint16)
    cals = [calibration(origin=origin, dark=20, noise=True, read_variance=1e-8)]
    got = execute(raw, p=profile("spatial_lsc"), cals=cals, numeric=grid(), origin=origin)
    a = got["arrays"]
    yy, xx = np.indices(raw.shape[-2:])
    gain = np.asarray([2, 3, 4, 5])[2 * ((yy + origin[0]) % 2) + ((xx + origin[1]) % 2)]
    np.testing.assert_allclose(a["lsc_gain_map"], gain)
    np.testing.assert_allclose(a["lsc_normalized_frames"], (480 / 2000 * gain)[None])
    np.testing.assert_allclose(a["noise_variance_hint_e2_frames"], (501 * gain**2)[None])
    np.testing.assert_allclose(
        a["noise_variance_hint_normalized_frames"], (501 * gain**2 / 2000**2)[None]
    )
    np.testing.assert_allclose(
        a["lsc_quantization_bound_normalized_frames"],
        a["quantization_bound_normalized_frames"] * gain,
    )
    np.testing.assert_allclose(a["baseline_rgb_frames"], 480 / 2000)


def test_invalid_neighbor_does_not_silently_disable_denoise_scalar_oracle():
    raw = np.full((2, 6, 6), 164, np.uint16)
    raw[0, 0, 0], raw[0, 2, 2] = 184, 0
    p = profile("raw_denoise")
    p["raw_denoise"].update(strength=0.75, range_floor_e2=4)
    got = execute(raw, p=p, cals=[calibration((6, 6), noise=True) for _ in raw])
    a = got["arrays"]
    expected = filter_oracle(
        a["lsc_normalized_frames"],
        a["noise_variance_hint_normalized_frames"],
        a["denoise_valid_mask_frames"],
        0.75,
        4 / 2000**2,
    )
    np.testing.assert_allclose(
        a["denoised_normalized_frames"], expected, rtol=1e-14, equal_nan=True
    )
    assert a["denoised_normalized_frames"][0, 0, 0] < a["lsc_normalized_frames"][0, 0, 0]
    assert np.isnan(a["denoised_normalized_frames"][0, 2, 2])
    alone = execute(raw[1:], p=p, cals=[calibration((6, 6), noise=True)])["arrays"]
    np.testing.assert_array_equal(
        a["denoised_normalized_frames"][1], alone["denoised_normalized_frames"][0]
    )
    for site in ((0, 1), (1, 0), (1, 1)):
        np.testing.assert_allclose(
            a["denoised_normalized_frames"][0, site[0] :: 2, site[1] :: 2],
            a["lsc_normalized_frames"][0, site[0] :: 2, site[1] :: 2],
            rtol=1e-15,
        )


def test_noise_disabled_has_zero_variance_but_nonzero_quantization_and_positive_floor():
    raw = np.full((1, 4, 4), 564, np.uint16)
    got = execute(raw, p=profile("raw_denoise"), cals=[calibration((4, 4))])["arrays"]
    np.testing.assert_array_equal(got["decoded_stochastic_variance_estimate_e2_frames"], 0)
    assert (got["decoded_quantization_bound_e_frames"] > 0).all()
    np.testing.assert_array_equal(got["denoised_normalized_frames"], got["lsc_normalized_frames"])


def test_zero_strength_identity_and_sharpen_scalar_bounds_with_masked_hole():
    raw = np.full((1, 10, 12), 564, np.uint16)
    raw[:, 4:6, 4:6] = 764
    raw[:, 0, 0] = 0
    p = profile("raw_denoise", "linear_sharpen")
    p["raw_denoise"]["strength"] = 0
    p["linear_sharpen"].update(amount=0.7, threshold=0.005, max_delta=0.03)
    got = execute(raw, p=p)["arrays"]
    np.testing.assert_array_equal(got["denoised_normalized_frames"], got["lsc_normalized_frames"])
    for f, y, x in np.ndindex(raw.shape):
        if not got["rgb_valid_mask_frames"][f, y, x]:
            assert np.isnan(got["sharpen_delta_frames"][f, y, x])
            continue
        if not got["sharpen_applied_mask_frames"][f, y, x]:
            assert got["sharpen_delta_frames"][f, y, x] == 0
            continue
        center = got["ccm_rgb_frames"][f, y, x].mean()
        blur = 0.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy, xx = reflect(y + dy, raw.shape[1]), reflect(x + dx, raw.shape[2])
                blur += (
                    got["ccm_rgb_frames"][f, yy, xx].mean()
                    * (2 if dy == 0 else 1)
                    * (2 if dx == 0 else 1)
                    / 16
                )
        detail = center - blur
        expected = math.copysign(min(0.03, 0.7 * max(abs(detail) - 0.005, 0)), detail)
        assert got["sharpen_delta_frames"][f, y, x] == pytest.approx(expected, abs=1e-15)
    assert np.nanmax(np.abs(got["sharpen_delta_frames"])) <= 0.03
    assert np.nanmax(np.abs(got["sharpen_delta_frames"])) > 0


def test_all_invalid_physical_nan_display_black_and_grid_holes_never_repaired():
    for raw, numeric in (
        (np.zeros((1, 4, 4), np.uint16), grid()),
        (np.full((1, 4, 4), 564, np.uint16), grid()),
    ):
        numeric["lsc"]["valid_mask"] = np.zeros((2, 2, 4), bool).tolist()
        got = execute(
            raw, p=profile("spatial_lsc", "raw_denoise", "linear_sharpen"), numeric=numeric
        )["arrays"]
        for name in (
            "lsc_normalized_frames",
            "denoised_normalized_frames",
            "ccm_rgb_frames",
            "sharpened_rgb_frames",
            "noise_variance_hint_normalized_frames",
        ):
            assert np.isnan(got[name]).all()
        assert not got["rgb_valid_mask_frames"].any()
        np.testing.assert_array_equal(got["rgb_frames"], 0)


def test_exact_output_descriptors_and_json_native_summary_and_all_inputs_immutable():
    raw = np.full((2, 8, 10), 564, np.uint16)
    raw.setflags(write=False)
    cals, p, numeric, geom = (
        [calibration(dark=10), calibration(dark=20)],
        profile("spatial_lsc"),
        grid(),
        geometry(),
    )
    prior = deepcopy((cals, p, numeric, geom))
    before = hashlib.sha256(raw.tobytes()).hexdigest()
    parsed = isp_v3.validate_inputs(
        raw, cals, p, geometry=geom, normalization_e=2000, calibration=numeric
    )
    got = isp_v3.process_raw(raw, cals, p, geometry=geom, normalization_e=2000, calibration=numeric)
    resources = parsed["resources"]
    assert resources["output_array_payload_bytes"] == sum(a.nbytes for a in got["arrays"].values())
    for item in resources["output_array_descriptors"]:
        array = got["arrays"][item["name"]]
        assert list(array.shape) == item["shape"] and array.dtype.str == item["dtype"]
    assert resources["output_array_descriptors"] == isp_v3.output_array_descriptors(raw.shape)
    assert hashlib.sha256(raw.tobytes()).hexdigest() == before
    assert (p, numeric, geom) == prior[1:]
    for cal, original in zip(cals, prior[0], strict=True):
        np.testing.assert_array_equal(cal["dark_e"], original["dark_e"])
        assert {k: v for k, v in cal.items() if k != "dark_e"} == {
            k: v for k, v in original.items() if k != "dark_e"
        }
    from camera_platform.evidence_objects import pack, unpack

    packed = pack(got["summary"])
    assert unpack(packed["root"], packed["objects"].__getitem__) == got["summary"]
    json.dumps(got["summary"], allow_nan=False)
    got["arrays"]["rgb"][...] = 0.1
    assert not np.all(got["arrays"]["rgb_frames"][0] == 0.1)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 1e-300, 1e14])
def test_bad_fixed_normalization_is_rejected_before_decoder(monkeypatch, value):
    monkeypatch.setattr(nonlinear_readout, "decode_raw", lambda *a, **k: pytest.fail("decoder ran"))
    with pytest.raises(ValueError):
        execute(np.full((1, 4, 4), 100, np.uint16), norm=value)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema="camera_isp_profile_v2"),
        lambda p: p["graph"].reverse(),
        lambda p: p["raw_denoise"].update(mode="calibrated_same_site_filter_v1"),
        lambda p: p["raw_denoise"].update(range_floor_e2=0),
        lambda p: p["raw_denoise"].update(strength=True),
        lambda p: p["charge_decode"].update(schema="wrong"),
        lambda p: p["charge_decode"].update(guard_sigma=float("nan")),
        lambda p: p["spatial_lsc"].update(mode="native_four_site_grid_v1"),
        lambda p: p.update(black_level={"source": "raw_format"}),
    ],
)
def test_strict_profile_refuses_old_units_unknown_graph_and_unbound_lsc(change):
    p = profile()
    change(p)
    with pytest.raises(ValueError):
        isp_v3.validate_profile(p)


@pytest.mark.parametrize(
    "kind", ["count", "dark_bytes", "dark_shape", "geometry", "model_hash", "adc"]
)
def test_malformed_bound_calibration_and_code_domain_refused_before_decoder(monkeypatch, kind):
    raw, cals = np.full((1, 8, 10), 564, np.uint16), [calibration()]
    if kind == "count":
        cals = []
    elif kind == "dark_bytes":
        cals[0]["dark_e"][0, 0] += 1
    elif kind == "dark_shape":
        cals[0]["dark_e_shape"] = [8.0, 10]
    elif kind == "geometry":
        cals = [calibration(origin=(1, 1))]
    elif kind == "model_hash":
        cals[0]["model_sha256"] = "0" * 64
    else:
        raw[0, 0, 0] = 5000
    monkeypatch.setattr(nonlinear_readout, "decode_raw", lambda *a, **k: pytest.fail("decoder ran"))
    with pytest.raises(ValueError):
        execute(raw, cals=cals)


@pytest.mark.parametrize("kind", ["unbound", "missing", "noise", "schema", "coverage", "bool_gain"])
def test_charge_lsc_calibration_exact_contract(kind):
    p, numeric = profile("spatial_lsc"), grid()
    if kind == "unbound":
        p = profile()
    elif kind == "missing":
        numeric = None
    elif kind == "noise":
        numeric["noise"] = None
    elif kind == "schema":
        numeric["schema"] = "camera_isp_calibration_v1"
    elif kind == "coverage":
        numeric["lsc"]["row_nodes"] = [2, 19]
        numeric["lsc"]["reference_roi"].update(row_start=2, row_end=3)
    else:
        numeric["lsc"]["gains"][0][0][0] = True
    with pytest.raises(ValueError):
        execute(np.full((1, 4, 4), 100, np.uint16), p=p, numeric=numeric)


@pytest.mark.parametrize(
    "raw",
    [
        np.zeros((4, 4), np.uint16),
        np.zeros((1, 2, 4, 4), np.uint16),
        np.zeros((1, 4, 4), float),
        np.zeros((1, 3, 4), np.uint16),
        np.zeros((0, 4, 4), np.uint16),
        np.zeros((1, 4, 4), np.uint64),
    ],
)
def test_raw_shape_no_implicit_stream_flattening(raw):
    with pytest.raises(ValueError):
        isp_v3.validate_inputs(raw, [], profile(), geometry=geometry(), normalization_e=2000)


@pytest.mark.parametrize("limit", ["MAX_OUTPUT_ARRAY_BYTES", "MAX_WORKING_ARRAY_BYTES"])
def test_resource_preflight_before_calibration_copy_or_decoder(monkeypatch, limit):
    monkeypatch.setattr(isp_v3, limit, 1)
    monkeypatch.setattr(
        nonlinear_readout, "validate_calibration", lambda *a: pytest.fail("copied calibration")
    )
    with pytest.raises(isp.ISPError, match="bounds"):
        isp_v3.validate_inputs(
            np.zeros((1, 4, 4), np.uint16),
            [{}],
            profile(),
            geometry=geometry(),
            normalization_e=2000,
        )

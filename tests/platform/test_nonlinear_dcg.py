"""Independent scalar and covariance checks; synthetic numerical evidence only."""

import json
from copy import deepcopy

import numpy as np
import pytest
from test_nonlinear_readout import model as single_model

from camera_platform import dcg_electrical, nonlinear_readout
from camera_platform import nonlinear_dcg as nd
from camera_platform.registry import digest


def model():
    single = single_model()
    paths = []
    for stream, factor, ceiling in [("HCG", 2, 1200), ("LCG", 1, 2000)]:
        curve = deepcopy(single["charge_voltage_response"])
        curve["signal_voltage_v"] = [v * factor for v in curve["signal_voltage_v"]]
        paths.append(
            {
                "stream_id": stream,
                "charge_voltage_response": curve,
                "analog_gain": 1.0,
                "input_linear_ceiling_e": ceiling,
                "digital_gain": 1.0,
                "adc": deepcopy(single["adc"]),
            }
        )
    return {
        "schema": nd.SCHEMA,
        "topology": dcg_electrical.TOPOLOGY,
        "stream_ids": ["HCG", "LCG"],
        "shared_charge_capacity_e": 2000.0,
        **{key: single[key] for key in nd.COMMON_KEYS},
        "paths": paths,
        "read_noise": {
            "schema": nd.NOISE_SCHEMA,
            "stream_ids": ["HCG", "LCG"],
            "plane": nonlinear_readout.NOISE_PLANE,
            "includes": nonlinear_readout.NOISE_INCLUDES,
            "covariance_v2": [[4e-8, 1e-8], [1e-8, 9e-8]],
            "temperature_range_c": [20.0, 30.0],
            "source": single["source"],
        },
        "source": single["source"],
    }


def capture(
    photo=500.0,
    m=None,
    *,
    shape=(4, 6),
    noise=False,
    exposure=0.5,
    seed=7933,
    temperature=25.0,
    geometry=None,
):
    photo = (
        np.full(shape, photo, dtype=float) if np.isscalar(photo) else np.asarray(photo, dtype=float)
    )
    return nd.capture_shared_electrons(
        photo,
        exposure,
        m or model(),
        temperature_c=temperature,
        geometry=geometry or {"full_sensor_shape": list(photo.shape), "crop_origin": [0, 0]},
        noise_enabled=noise,
        seed=seed,
    )


def scalar_voltage(charge, path):
    curve = path["charge_voltage_response"]
    q, v = curve["charge_e"], curve["signal_voltage_v"]
    index = next((i for i in range(len(q) - 1) if charge <= q[i + 1]), len(q) - 2)
    return v[index] + (charge - q[index]) * (v[index + 1] - v[index]) / (q[index + 1] - q[index])


def scalar_code(charge, m, path):
    charge = min(charge, m["shared_charge_capacity_e"], path["input_linear_ceiling_e"])
    adc = path["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    first = min(
        white,
        max(
            0,
            round(
                black
                + scalar_voltage(charge, path) * path["analog_gain"] * white / adc["full_scale_v"]
            ),
        ),
    )
    final = min(white, max(0, round(black + path["digital_gain"] * (first - black))))
    return first, final


def raw_for(charges, m, shape=(4, 6)):
    return np.stack(
        [
            np.full(shape, scalar_code(q, m, p)[1], dtype=np.int32)
            for q, p in zip(charges, m["paths"], strict=True)
        ]
    )


def merge(raw, cal, **overrides):
    return nd.merge_shared_charge_raw(raw, cal, {**nd.default_profile(), **overrides})


def test_normalized_model_detaches_and_keeps_voltage_plane_and_source():
    m = model()
    before = deepcopy(m)
    checked = nd.validate_model(m)
    assert checked == m and m == before
    checked["paths"][0]["adc"]["black_level_dn"] = 4
    checked["paths"][0]["charge_voltage_response"]["charge_e"][1] = 500
    checked["fixed_pattern"]["seed"] = 3
    assert m == before
    assert checked["read_noise"]["plane"] == "conversion_output_before_analog_gain"
    json.dumps(m, allow_nan=False)


@pytest.mark.parametrize("factor", [1.0, 0.5])
def test_hcg_name_requires_higher_origin_conversion_slope_before_analog_gain(factor):
    m = model()
    m["paths"][0]["charge_voltage_response"]["signal_voltage_v"] = [0, 0.1 * factor, 0.15]
    m["paths"][0]["analog_gain"] = 4  # Analog gain cannot relabel lower conversion gain.
    with pytest.raises(ValueError, match="origin conversion slope"):
        nd.validate_model(m)


@pytest.mark.parametrize("analog,digital", [(0.5, 0.3), (1.0, 1.0), (2.0, 1.7), (4.0, 4.0)])
def test_noiseless_forward_has_response_before_analog_and_two_actual_quantizers(analog, digital):
    m = model()
    m["dark_current_e_per_s"] = 10.0
    for path in m["paths"]:
        path.update(analog_gain=analog, digital_gain=digital)
    photo = np.array([[0.0, 499.0, 999.0, 1195.0], [1499.0, 1995.0, 2999.0, 7.25]])
    original = photo.copy()
    result = capture(photo, m)
    for index, path in enumerate(m["paths"]):
        expected = np.array([[scalar_code(q + 5, m, path) for q in row] for row in photo])
        np.testing.assert_array_equal(result["arrays"]["adc_dn"][index], expected[..., 0])
        np.testing.assert_array_equal(result["arrays"]["raw_dn"][index], expected[..., 1])
    np.testing.assert_array_equal(photo, original)
    np.testing.assert_array_equal(result["arrays"]["read_noise_v"], 0)
    assert set(result["arrays"]) == set(result["summary"]["array_units"])
    assert result["summary"]["simulation_oracle"]["used_by_fusion"] is False


def test_calibration_shared_dark_is_stored_once_and_every_path_is_bound():
    result = capture()
    c = result["calibration"]
    assert c["schema"] == nd.CALIBRATION_SCHEMA
    assert all("dark_e" not in path for path in c["paths"])
    assert c["model_sha256"] == digest(c["model"])
    for index in range(2):
        path = nd.calibration_for_path(c, index)
        assert nonlinear_readout.validate_calibration(path, (4, 6))["model"] == path["model"]
        np.testing.assert_array_equal(path["dark_e"], c["dark_e"])
        path["dark_e"][:] = 11
        assert not c["dark_e"].any()
    c["dark_e"][0, 0] = 1
    with pytest.raises(ValueError, match="binding|hash"):
        nd.calibration_for_path(c, 0)


@pytest.mark.parametrize(
    "field", ["top-model", "top-settings", "path-model", "path-settings", "path-source"]
)
def test_calibration_rejects_rehashed_partial_model_or_setting_tamper(field):
    cal = capture()["calibration"]
    if field == "top-model":
        cal["model"]["paths"][1]["digital_gain"] = 2
        cal["model_sha256"] = digest(cal["model"])
    elif field == "top-settings":
        cal["acquisition"]["exposure_s"] = 1
        cal["acquisition_sha256"] = digest(cal["acquisition"])
    elif field == "path-settings":
        cal["paths"][1]["acquisition"]["temperature_c"] = 26
        cal["paths"][1]["acquisition_sha256"] = digest(cal["paths"][1]["acquisition"])
    else:
        target = cal["paths"][1]["model"]
        if field == "path-model":
            target["charge_voltage_response"]["signal_voltage_v"][1] *= 0.9
        else:
            target["source"]["description"] = "Different author"
        cal["paths"][1]["model_sha256"] = digest(target)
    with pytest.raises(ValueError, match="binding"):
        nd.calibration_for_path(cal, 0)  # Even the other path cannot hide inconsistent pairing.


@pytest.mark.parametrize(
    "fault", ["temperature", "analog", "coverage", "asymmetric", "non-PSD", "plane", "oracle"]
)
def test_model_and_condition_refusals_happen_before_capture_arrays(fault, monkeypatch):
    m = model()
    temperature = 25
    if fault == "temperature":
        temperature = 31
    elif fault == "analog":
        m["paths"][0]["analog_gain"] = 5
    elif fault == "coverage":
        m["paths"][1]["charge_voltage_response"]["charge_e"][-1] = 1800
    elif fault == "asymmetric":
        m["read_noise"]["covariance_v2"][0][1] = 0
    elif fault == "non-PSD":
        m["read_noise"]["covariance_v2"] = [[1.0, 1.0], [1.0, float(np.nextafter(1.0, 0.0))]]
    elif fault == "plane":
        m["read_noise"]["plane"] = "after_analog_gain"
    else:
        m["noiseless_charge_e"] = 500
    monkeypatch.setattr(
        nd.electrical,
        "_patterns",
        lambda *a: pytest.fail("Rejected scalar model allocated native patterns"),
    )
    with pytest.raises(ValueError):
        capture(m=m, temperature=temperature)


def test_dark_temperature_law_overflow_refuses_before_allocation(monkeypatch):
    m = model()
    m.update(reference_temperature_c=0.0, dark_current_doubling_c=0.001, dark_current_e_per_s=1.0)
    monkeypatch.setattr(nd.electrical, "_patterns", lambda *a: pytest.fail("Native allocation"))
    with pytest.raises(ValueError, match="temperature scaling"):
        capture(m=m)


def test_hidden_path_clip_uses_lcg_and_both_clipped_is_nan_with_zero_weights():
    m = model()
    photo = np.array([[500.0, 1500.0, 2000.0, 3000.0]] * 2)
    result = capture(photo, m)
    raw = result["arrays"]["raw_dn"]
    before = raw.copy()
    output = merge(raw, result["calibration"], read_noise_guard_sigma=0)
    a = output["arrays"]
    assert a["usable_mask"][:, :, 0].all()
    assert not a["usable_mask"][0, :, 1:].any()
    assert a["usable_mask"][1, :, 1].all()
    np.testing.assert_array_equal(a["weights"][1, :, 1], 1)
    assert not a["valid_mask"][:, 2:].any()
    assert np.isnan(a["merged_rate"][:, 2:]).all()
    np.testing.assert_array_equal(a["weights"][:, :, 2:], 0)
    assert np.all(abs(a["merged_rate"][:, 1] - 3000) <= a["rounding_bound_rate"][:, 1])
    np.testing.assert_array_equal(raw, before)
    assert raw.max() < 65535  # Hidden path/shared-charge clipping precedes ADC white.
    assert set(a) == set(output["summary"]["array_units"])
    assert not output["summary"]["simulation_oracle_used"]
    json.dumps(output["summary"], allow_nan=False)


def test_independent_inverse_and_scalar_covariance_weights_count_shot_once():
    m = model()
    m["dark_current_e_per_s"] = 10
    cal = capture(m=m, noise=True)["calibration"]
    raw = raw_for([500, 504], m)
    a = merge(raw, cal, read_noise_guard_sigma=0)["arrays"]
    slopes = np.array([5000.0, 10000.0])
    z = (raw[:, 0, 0].astype(float) - 1000) / 65535 * slopes
    covariance = np.diag(slopes) @ np.array(m["read_noise"]["covariance_v2"]) @ np.diag(slopes)
    denominator = covariance[0, 0] + covariance[1, 1] - 2 * covariance[0, 1]
    w = np.array(
        [
            (covariance[1, 1] - covariance[0, 1]) / denominator,
            (covariance[0, 0] - covariance[0, 1]) / denominator,
        ]
    )
    expected_total = w @ z
    np.testing.assert_allclose(a["weights"][:, 0, 0], w, rtol=1e-15)
    np.testing.assert_allclose(a["local_read_covariance_e2"][:, :, 0, 0], covariance)
    np.testing.assert_allclose(a["read_variance_e2"], w @ covariance @ w)
    np.testing.assert_allclose(a["merged_rate"], (expected_total - 5) / 0.5)
    np.testing.assert_allclose(a["common_shot_variance_estimate_e2"], expected_total)
    np.testing.assert_allclose(
        a["shared_shot_read_variance_estimate_e2"], expected_total + w @ covariance @ w
    )
    expected_bound = w @ (slopes / 65535 / 2)
    # The exact-arithmetic quantizer bound is widened by the decoder's declared
    # float64 voltage/charge allowances, separate from stochastic variance.
    machine_allowance = 16 * np.finfo(float).eps * (slopes + 2000)
    assert np.all(a["rounding_bound_e"] >= expected_bound)
    np.testing.assert_allclose(
        a["rounding_bound_e"],
        expected_bound + w @ machine_allowance,
        rtol=0,
        atol=8 * np.spacing(z.max()),
    )
    expected_threshold = 5 * np.sqrt(denominator) + sum(slopes / 65535 / 2 + machine_allowance)
    np.testing.assert_allclose(a["gain_path_agreement_threshold_e"], expected_threshold)


def test_weights_follow_local_inverse_slopes_instead_of_a_single_constant_gain():
    m = model()
    m["paths"][0]["input_linear_ceiling_e"] = 2000
    m["paths"][0]["charge_voltage_response"].update(
        charge_e=[0, 800, 2000], signal_voltage_v=[0, 0.16, 0.22]
    )
    cal = capture(m=m, noise=True)["calibration"]
    low = merge(raw_for([400, 400], m), cal)["arrays"]
    high = merge(raw_for([1500, 1500], m), cal)["arrays"]
    assert abs(low["weights"][0, 0, 0] - high["weights"][0, 0, 0]) > 0.1
    for a, derivatives in [(low, [5000.0, 10000.0]), (high, [20000.0, 20000.0])]:
        cov = (
            np.diag(derivatives) @ np.array(m["read_noise"]["covariance_v2"]) @ np.diag(derivatives)
        )
        w = (cov[1, 1] - cov[0, 1]) / (cov[0, 0] + cov[1, 1] - 2 * cov[0, 1])
        np.testing.assert_allclose(a["weights"][0], w, rtol=1e-14)


@pytest.mark.parametrize(
    "covariance,weights,variance",
    [
        ([[0, 0], [0, 0]], [1, 0], 0),
        ([[4, 2], [2, 1]], [1, 0], 1e8),  # Equal input-referred factor, exact flat objective.
        ([[1, -2], [-2, 4]], [0.8, 0.2], 0),
        ([[0, 0], [0, 9]], [1, 0], 0),
    ],
)
def test_singular_negative_covariance_and_zero_read_tie_need_no_jitter(
    covariance, weights, variance
):
    m = model()
    m["read_noise"]["covariance_v2"] = covariance
    cal = capture(m=m, noise=True)["calibration"]
    a = merge(raw_for([500, 500], m), cal, read_noise_guard_sigma=0)["arrays"]
    np.testing.assert_allclose(a["weights"][:, 0, 0], weights, atol=1e-15, rtol=0)
    np.testing.assert_allclose(a["read_variance_e2"], variance, atol=1e-22, rtol=1e-15)


def test_read_disagreement_is_diagnostic_and_does_not_change_weights():
    m = model()
    cal = capture(m=m, noise=True)["calibration"]
    agree = merge(raw_for([500, 500], m), cal)["arrays"]
    disagree = merge(raw_for([500, 800], m), cal)["arrays"]
    assert agree["gain_path_agreement_mask"].all()
    assert not disagree["gain_path_agreement_mask"].any()
    assert not disagree["gain_path_agreement_unknown_mask"].any()
    np.testing.assert_array_equal(agree["weights"], disagree["weights"])
    assert disagree["valid_mask"].all()


def test_shared_poisson_variance_is_not_halved_by_two_raw_reads():
    m = model()
    m["read_noise"]["covariance_v2"] = [[0, 0], [0, 0]]
    output = capture(300.0, m, shape=(200, 300), noise=True)
    a = merge(output["arrays"]["raw_dn"], output["calibration"])["arrays"]
    charge = a["merged_rate"] * 0.5
    assert a["valid_mask"].all()
    assert abs(charge.mean() - 300) < 0.3
    assert 285 < charge.var() < 315
    np.testing.assert_array_equal(a["common_shot_variance_estimate_e2"], charge)
    np.testing.assert_array_equal(a["shared_shot_read_variance_estimate_e2"], charge)
    assert np.corrcoef(a["path_rate"].reshape(2, -1))[0, 1] > 0.9999


def test_paired_noise_is_in_volts_after_response_and_is_drawn_jointly():
    m = model()
    expected = np.array([[4e-8, -2e-8], [-2e-8, 9e-8]])
    m["read_noise"]["covariance_v2"] = expected.tolist()
    result = capture(400.0, m, shape=(200, 300), noise=True)
    measured = np.cov(result["arrays"]["read_noise_v"].reshape(2, -1))
    np.testing.assert_allclose(measured, expected, rtol=0.05, atol=0)
    assert result["summary"]["shared_charge_sample_count_per_pixel"] == 1
    assert result["summary"]["array_units"]["read_noise_v"] == "V"
    charge = result["arrays"]["shared_charge_e"]
    for index in range(2):
        # All samples are below the first knot in this fixed statistical fixture.
        voltage = (
            charge * (0.0002 if index == 0 else 0.0001) + result["arrays"]["read_noise_v"][index]
        )
        expected_raw = np.rint(1000 + voltage * 65535).astype(np.int32)
        np.testing.assert_array_equal(result["arrays"]["raw_dn"][index], expected_raw)


def test_native_fixed_patterns_persist_across_crops_and_noise_off():
    m = model()
    m["fixed_pattern"].update(prnu_sigma=0.02, dsnu_e_per_s=1.0, seed=42)
    m["dark_current_e_per_s"] = 10.0
    full = capture(m=m, shape=(8, 10))
    crop = capture(
        m=m, shape=(4, 6), geometry={"full_sensor_shape": [8, 10], "crop_origin": [1, 3]}
    )
    for key in ("photo_response_map", "dark_current_e_per_s_map", "mean_total_charge_e"):
        np.testing.assert_array_equal(crop["arrays"][key], full["arrays"][key][1:5, 3:9])
    np.testing.assert_array_equal(crop["arrays"]["raw_dn"], full["arrays"]["raw_dn"][:, 1:5, 3:9])


def test_noise_disabled_variance_zero_quantization_bound_retained_and_profile_no_v1_upgrade():
    result = capture()
    a = merge(result["arrays"]["raw_dn"], result["calibration"])["arrays"]
    np.testing.assert_array_equal(a["shared_shot_read_variance_estimate_e2"], 0)
    assert np.all(a["rounding_bound_e"] > 0)
    np.testing.assert_array_equal(a["weights"][0], 1)
    with pytest.raises(ValueError):
        nd.validate_profile({**nd.default_profile(), "schema": "camera_dcg_merge_v1"})
    with pytest.raises(ValueError):
        nd.validate_profile({**nd.default_profile(), "tie_break_stream": "LCG"})


@pytest.mark.parametrize("kind", ["readout", "decode", "isp", "hdr", "reference_hdr", "dcg"])
def test_nonzero_noise_summaries_roundtrip_strict_evidence_codec(kind):
    # json.dumps accepts np.float64 and therefore did not expose the actual
    # large Worker-result failure. Exercise the strict production graph codec
    # directly regardless of this small fixture's serialized byte threshold.
    from camera_platform import isp, nonlinear_hdr, nonlinear_isp, reference_hdr
    from camera_platform.evidence_objects import pack, unpack

    m = single_model()
    m["read_noise"]["variance_v2"] = 4e-8
    geometry = {"full_sensor_shape": [4, 6], "crop_origin": [0, 0]}
    native_geometry = {"kind": "native_sensor", **geometry}
    captures = [
        nonlinear_readout.capture_electrons(
            np.full((4, 6), charge),
            exposure,
            m,
            analog_gain=1.0,
            temperature_c=25.0,
            geometry=geometry,
            noise_enabled=True,
            seed=31 + index,
        )
        for index, (charge, exposure) in enumerate(((500.0, 0.5), (1000.0, 1.0)))
    ]
    first = captures[0]
    raw = np.stack([item["arrays"]["raw_dn"] for item in captures])
    calibrations = [item["calibration"] for item in captures]
    if kind == "readout":
        summary = first["summary"]
    elif kind == "decode":
        summary = nonlinear_readout.decode_raw(raw[0], calibrations[0])["summary"]
        for key in (
            "guard_voltage_v",
            "numerical_voltage_allowance_v",
            "numerical_charge_allowance_e",
        ):
            assert type(summary[key]) is float and summary[key] > 0
    elif kind == "isp":
        summary = nonlinear_isp.process_raw(
            raw[0],
            calibrations[0],
            isp.default_profile(),
            geometry=native_geometry,
        )["summary"]
    elif kind == "hdr":
        summary = nonlinear_hdr.merge_raw_bracket(
            raw,
            [0.5, 1.0],
            calibrations,
            geometry=native_geometry,
            profile=nonlinear_hdr.default_profile(),
        )["summary"]
    elif kind == "reference_hdr":
        summary = reference_hdr.merge_reference_raw_bracket(
            raw,
            [0.5, 1.0],
            calibrations,
            geometry=native_geometry,
            profile=reference_hdr.default_profile_v2(),
        )["summary"]
    else:
        paired = capture(noise=True)
        summary = merge(paired["arrays"]["raw_dn"], paired["calibration"])["summary"]
    packed = pack(summary)
    assert unpack(packed["root"], packed["objects"].__getitem__) == summary

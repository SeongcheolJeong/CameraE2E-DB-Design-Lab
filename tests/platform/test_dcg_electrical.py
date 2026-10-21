"""Shared-charge scalar transfer, correlated noise and native-coordinate checks.

Numerical behavior is tested using synthetic inputs, not physical sensor data.
Statistical sample sizes and tolerances below are fixed before the draws.
"""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import dcg_electrical as dcg
from camera_platform.electrical import capture_electrons, default_electrical_model

LEGACY = {
    "full_well_e": 10000.0,
    "read_noise_e": 4.0,
    "dark_current_e_per_s": 10.0,
    "gain": 1.0,
    "bit_depth": 12,
    "black_level_dn": 64.0,
}


@pytest.fixture
def model():
    return dcg.default_model(LEGACY)


def capture(
    model,
    photo=100.0,
    *,
    shape=(16, 16),
    exposure=0.01,
    noise=False,
    temperature=25.0,
    seed=731,
    geometry=None,
):
    photo = np.full(shape, photo, dtype=float) if np.isscalar(photo) else photo
    return dcg.capture_shared_electrons(
        photo,
        exposure,
        model,
        temperature_c=temperature,
        noise_enabled=noise,
        seed=seed,
        geometry=geometry or {"full_sensor_shape": list(photo.shape), "crop_origin": [0, 0]},
    )


def scalar_codes(photo, dark, exposure, model, path):
    """Independent scalar conversion using the documented physical stages."""
    shared = min(photo + dark * exposure, model["shared_charge_capacity_e"])
    charge = min(shared, path["input_linear_ceiling_e"])
    adc = path["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    voltage = charge * path["conversion_gain_v_per_e"] * path["analog_gain"]
    code = max(0, min(white, round(black + voltage * white / adc["full_scale_v"])))
    raw = max(0, min(white, round(black + path["digital_gain"] * (code - black))))
    return code, raw


def linear_model(model):
    model.update(shared_charge_capacity_e=100000.0, dark_current_e_per_s=0.0)
    for index, path in enumerate(model["paths"]):
        path.update(
            conversion_gain_v_per_e=2.0 if index == 0 else 1.0,
            analog_gain=1.0,
            digital_gain=1.0,
            input_linear_ceiling_e=100000.0,
        )
        path["adc"].update(bit_depth=20, full_scale_v=2**20 - 1, black_level_dn=1024.0)
    # Keep full-scale within the declared 1e6 V bound, without changing slope.
    for path in model["paths"]:
        path["adc"].update(bit_depth=19, full_scale_v=2**19 - 1)
    return model


def test_default_is_explicit_estimated_two_path_assumption_and_detached(model):
    assert model["source"]["kind"] == model["read_noise"]["source"]["kind"] == "estimated"
    assert "not inferred or measured" in model["read_noise"]["source"]["description"]
    assert model["read_noise"]["temperature_range_c"] == [25, 25]
    assert model["read_noise"]["covariance_e2"] == [[4, 0], [0, 16]]
    original = deepcopy(model)
    checked = dcg.validate_model(model)
    checked["paths"][0]["adc"]["black_level_dn"] = 1
    checked["read_noise"]["covariance_e2"][0][0] = 100
    assert model == original


def test_scalar_preflight_catches_dark_law_overflow_without_native_allocation(model, monkeypatch):
    monkeypatch.setattr(
        dcg, "_patterns", lambda *args: pytest.fail("Allocated in scalar preflight")
    )
    assert dcg.validate_capture_conditions(model, 0.01, 25) == {
        "exposure_s": 0.01,
        "temperature_c": 25.0,
        "dark_current_temperature_scale": 1.0,
    }
    model.update(reference_temperature_c=0, dark_current_doubling_c=0.001, dark_current_e_per_s=1)
    model["read_noise"]["temperature_range_c"] = [0, 25]
    with pytest.raises(ValueError, match="temperature scaling"):
        dcg.validate_capture_conditions(model, 0.01, 25)


@pytest.mark.parametrize("analog,digital", [(0.5, 0.3), (1, 1), (2, 1.7), (5, 4)])
def test_noise_off_matches_independent_scalar_adc_and_digital_stages(model, analog, digital):
    for index, path in enumerate(model["paths"]):
        path["analog_gain"] = analog * (index + 1)
        path["digital_gain"] = digital
        path["adc"]["black_level_dn"] += 3 * index
    photo = np.array([[0, 1.49, 30.7, 311], [1200, 2300, 7000, 15000]])
    original = photo.copy()
    output = capture(model, photo, exposure=0.5)
    for stream, path in enumerate(model["paths"]):
        expected = np.array([[scalar_codes(v, 10, 0.5, model, path) for v in row] for row in photo])
        np.testing.assert_array_equal(output["arrays"]["adc_dn"][stream], expected[..., 0])
        np.testing.assert_array_equal(output["arrays"]["raw_dn"][stream], expected[..., 1])
    np.testing.assert_array_equal(photo, original)
    np.testing.assert_array_equal(output["arrays"]["mean_total_charge_e"], photo + 5)
    assert output["calibration"]["read_noise_covariance_e2"] == [[0, 0], [0, 0]]
    assert not output["arrays"]["read_noise_e"].any()
    assert output["summary"]["model"]["read_noise"]["covariance_e2"] == [[4, 0], [0, 16]]
    assert output["summary"]["physical_validation"] == "not_established"


def test_shared_capacity_and_hcg_path_clip_are_separate_and_hidden_below_adc_white(model):
    model = linear_model(model)
    model["shared_charge_capacity_e"] = 1000
    model["paths"][0]["input_linear_ceiling_e"] = 200
    photo = np.array([[100.0, 300.0, 1000.0, 1500.0]])
    result = capture(model, photo)
    a, c = result["arrays"], result["calibration"]
    np.testing.assert_array_equal(a["shared_charge_e"], [[100, 300, 1000, 1000]])
    np.testing.assert_array_equal(a["well_saturated_mask"], [[False, False, True, True]])
    np.testing.assert_array_equal(a["path_saturated_mask"][0], [[False, True, True, True]])
    assert not a["path_saturated_mask"][1].any()
    np.testing.assert_array_equal(a["raw_dn"][0], [[1224, 1424, 1424, 1424]])
    np.testing.assert_array_equal(a["raw_dn"][1], [[1124, 1324, 2024, 2024]])
    assert not a["adc_high_mask"].any()
    assert [p["linear_ceiling_dn"] for p in c["paths"]] == [1424, 2024]


def test_shot_is_drawn_once_then_both_paths_share_the_same_charge(model, monkeypatch):
    model = linear_model(model)
    model["read_noise"]["covariance_e2"] = [[0, 0], [0, 0]]
    rng = np.random.default_rng
    poisson_shapes = []

    class TrackedGenerator:
        def __init__(self, generator):
            self.generator = generator

        def __getattr__(self, name):
            return getattr(self.generator, name)

        def poisson(self, mean):
            poisson_shapes.append(mean.shape)
            return self.generator.poisson(mean)

    monkeypatch.setattr(np.random, "default_rng", lambda seed: TrackedGenerator(rng(seed)))
    result = capture(model, 100, noise=True, shape=(32, 48))
    assert poisson_shapes == [(32, 48)]
    a, paths = result["arrays"], result["calibration"]["paths"]
    for stream, path in enumerate(paths):
        recovered = (a["raw_dn"][stream] - path["black_level_dn"]) / path["dn_per_e"]
        np.testing.assert_array_equal(recovered, a["shared_charge_e"])
    assert result["summary"]["shared_charge_sample_count_per_pixel"] == 1


@pytest.mark.parametrize("cross", [6.0, -12.0, 24.0, -24.0])
def test_paired_noise_and_shared_shot_covariance_match_independent_statistics(model, cross):
    model = linear_model(model)
    covariance = np.array([[16.0, cross], [cross, 36.0]])
    model["read_noise"]["covariance_e2"] = covariance.tolist()
    result = capture(model, 200.0, shape=(512, 512), noise=True)
    a = result["arrays"]
    read = a["read_noise_e"].reshape(2, -1)
    common = a["shared_charge_e"].ravel()
    # 262144 independent native pixels, uniform means and no fixed patterns.
    np.testing.assert_allclose(np.cov(read), covariance, rtol=0.025, atol=0.12)
    paired = common[None, :] + read
    np.testing.assert_allclose(np.cov(paired), covariance + 200.0, rtol=0.015, atol=0.15)
    difference_variance = 16 + 36 - 2 * cross
    assert np.var(paired[0] - paired[1], ddof=1) == pytest.approx(difference_variance, rel=0.025)
    weight = np.array([0.5, 0.5])
    average_variance = 200 + weight @ covariance @ weight
    assert np.var(paired.mean(axis=0), ddof=1) == pytest.approx(average_variance, rel=0.015)
    assert np.var(paired.mean(axis=0), ddof=1) > 190  # Common shot never halves to 100.
    if abs(cross) == 24:
        np.testing.assert_allclose(read[1], np.sign(cross) * 1.5 * read[0], atol=2e-14)


@pytest.mark.parametrize(
    "covariance", [[[0, 0], [0, 0]], [[0, 0], [0, 9]], [[4, 0], [0, 0]], [[4, 4], [4, 4]]]
)
def test_zero_and_degenerate_covariance_does_not_invent_jitter(model, covariance):
    model["read_noise"]["covariance_e2"] = covariance
    result = capture(model, 0.0, noise=True, shape=(64, 64))
    noise = result["arrays"]["read_noise_e"]
    for stream in range(2):
        if covariance[stream][stream] == 0:
            assert not noise[stream].any()
    if covariance == [[4, 4], [4, 4]]:
        np.testing.assert_array_equal(noise[0], noise[1])
    assert result["calibration"]["read_noise_covariance_e2"] == covariance


@pytest.mark.parametrize("origin", [[0, 0], [0, 1], [1, 0], [1, 1]])
def test_native_fpn_crop_phase_and_temperature_match_existing_electrical_patterns(model, origin):
    model["fixed_pattern"].update(prnu_sigma=0.2, dsnu_e_per_s=5, seed=853)
    model["dark_current_doubling_c"] = 5
    model["read_noise"]["temperature_range_c"] = [25, 35]
    full = capture(model, shape=(18, 20), exposure=0.01)
    crop = capture(
        model,
        shape=(8, 10),
        exposure=0.02,
        temperature=35,
        seed=37,
        geometry={"full_sensor_shape": [18, 20], "crop_origin": origin},
    )
    region = tuple(slice(start, start + size) for start, size in zip(origin, (8, 10), strict=True))
    np.testing.assert_array_equal(
        crop["arrays"]["photo_response_map"], full["arrays"]["photo_response_map"][region]
    )
    np.testing.assert_array_equal(
        crop["arrays"]["dark_current_e_per_s_map"],
        full["arrays"]["dark_current_e_per_s_map"][region] * 4,
    )
    np.testing.assert_array_equal(
        crop["calibration"]["dark_e"], full["calibration"]["dark_e"][region] * 8
    )
    legacy = default_electrical_model(LEGACY)
    legacy["fixed_pattern"] = deepcopy(model["fixed_pattern"])
    old = capture_electrons(
        np.full((18, 20), 100.0),
        0.01,
        legacy,
        analog_gain=1,
        temperature_c=25,
        geometry={"full_sensor_shape": [18, 20], "crop_origin": [0, 0]},
        noise_enabled=False,
        seed=731,
    )
    for key in ("photo_response_map", "dark_current_e_per_s_map", "mean_total_charge_e"):
        np.testing.assert_array_equal(full["arrays"][key], old["arrays"][key])


@pytest.mark.parametrize("digital", [0.01, 0.5, 1, 2.5])
def test_calibration_retains_adc_lower_floor_after_black_relative_digital_gain(model, digital):
    model = linear_model(model)
    model["read_noise"]["covariance_e2"] = [[1e6, 0], [0, 1e6]]
    for path in model["paths"]:
        path["digital_gain"] = digital
    result = capture(model, 0.0, shape=(128, 128), noise=True)
    for stream, calibration in enumerate(result["calibration"]["paths"]):
        mask = result["arrays"]["adc_low_mask"][stream]
        assert mask.sum() > 100
        expected_floor = max(0.0, 1024 * (1 - digital))
        assert calibration["linear_floor_dn"] == expected_floor
        assert np.all(result["arrays"]["raw_dn"][stream][mask] == np.rint(expected_floor))


@pytest.mark.parametrize("digital", [0.1, 0.75, 1, 1.7, 4])
def test_two_stage_quantization_error_stays_inside_deterministic_electron_bound(model, digital):
    model = linear_model(model)
    model["paths"][0]["analog_gain"] = 0.37
    model["paths"][1]["analog_gain"] = 0.81
    for path in model["paths"]:
        path["digital_gain"] = digital
        path["adc"]["black_level_dn"] = 1000.3
    photo = np.linspace(0, 220, 5001)[None, :]
    result = capture(model, photo)
    for stream, path in enumerate(result["calibration"]["paths"]):
        recovered = (result["arrays"]["raw_dn"][stream] - path["black_level_dn"]) / path["dn_per_e"]
        error = abs(recovered - photo)
        assert error.max() <= path["rounding_bound_e"] + 1e-10
        assert path["rounding_bound_e"] > 0
    assert "quantization_variance_e2" not in result["calibration"]


def test_calibration_has_no_scene_mean_or_latent_noise_or_saturation_truth(model):
    low, high = capture(model, 5), capture(model, 20000)
    assert set(low["calibration"]) == {
        "schema",
        "stream_ids",
        "exposure_s",
        "paths",
        "read_noise_covariance_e2",
        "dark_e",
        "geometry",
    }
    for key in low["calibration"]:
        if key == "dark_e":
            np.testing.assert_array_equal(low["calibration"][key], high["calibration"][key])
        else:
            assert low["calibration"][key] == high["calibration"][key]
    assert set(low["calibration"]["paths"][0]) == {
        "stream_id",
        "dn_per_e",
        "black_level_dn",
        "white_level_dn",
        "linear_floor_dn",
        "linear_ceiling_dn",
        "rounding_bound_e",
    }


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "stream_order",
        "path_order",
        "topology",
        "conversion_order",
        "bits",
        "cov_negative",
        "cov_asymmetric",
        "cov_near_indefinite",
        "cov_bool",
        "cov_shape",
        "cov_missing",
        "cov_extra_noise",
        "source",
        "temperature_order",
        "gain_bool",
        "nan",
        "black_white",
        "unrepresentable_span",
        "fixed_seed_bool",
    ],
)
def test_invalid_model_refused_before_native_map_allocation(model, change, monkeypatch):
    if change == "extra":
        model["read_noise_e"] = 2
    elif change == "stream_order":
        model["stream_ids"] = ["LCG", "HCG"]
    elif change == "path_order":
        model["paths"].reverse()
    elif change == "topology":
        model["topology"] = "independent_reset_per_path"
    elif change == "conversion_order":
        model["paths"][0]["conversion_gain_v_per_e"] = 1e-5
    elif change == "bits":
        model["paths"][1]["adc"]["bit_depth"] = 10
    elif change == "cov_negative":
        model["read_noise"]["covariance_e2"] = [[-1, 0], [0, 1]]
    elif change == "cov_asymmetric":
        model["read_noise"]["covariance_e2"] = [[4, 2], [2.0000000000000004, 4]]
    elif change == "cov_near_indefinite":
        b = float(np.nextafter(1.0, 2.0))
        model["read_noise"]["covariance_e2"] = [[1, b], [b, 1]]
    elif change == "cov_bool":
        model["read_noise"]["covariance_e2"][0][0] = True
    elif change == "cov_shape":
        model["read_noise"]["covariance_e2"] = [[1, 2]]
    elif change == "cov_missing":
        model["read_noise"].pop("covariance_e2")
    elif change == "cov_extra_noise":
        model["read_noise"]["post_gain_noise_v"] = 1
    elif change == "source":
        model["read_noise"]["source"]["description"] = "   "
    elif change == "temperature_order":
        model["read_noise"]["temperature_range_c"] = [30, 20]
    elif change == "gain_bool":
        model["paths"][0]["analog_gain"] = True
    elif change == "nan":
        model["shared_charge_capacity_e"] = float("nan")
    elif change == "black_white":
        model["paths"][0]["adc"]["black_level_dn"] = 4095
    elif change == "unrepresentable_span":
        model["paths"][0].update(
            conversion_gain_v_per_e=1e-15,
            analog_gain=1e-6,
            digital_gain=1e-6,
            input_linear_ceiling_e=1e-6,
        )
    else:
        model["fixed_pattern"]["seed"] = True
    monkeypatch.setattr(dcg, "_patterns", lambda *args: pytest.fail("Allocated before validation"))
    with pytest.raises(ValueError):
        capture(model)


@pytest.mark.parametrize(
    "change",
    [
        "photo_bool",
        "photo_negative",
        "photo_nan",
        "photo_max",
        "photo_size",
        "temperature",
        "temperature_overflow",
        "dark_overflow",
        "zero_exposure",
        "noise_bool",
        "seed_bool",
        "native_size",
        "crop",
    ],
)
def test_invalid_capture_domains_refused_before_full_native_allocation(model, change, monkeypatch):
    photo = np.ones((2, 2))
    args = {}
    if change == "photo_bool":
        photo = np.ones((2, 2), dtype=bool)
    elif change == "photo_negative":
        photo[0, 0] = -1
    elif change == "photo_nan":
        photo[0, 0] = float("nan")
    elif change == "photo_max":
        photo[0, 0] = dcg.MAX_CHARGE_E * 2
    elif change == "photo_size":
        photo = np.broadcast_to(1.0, (1000001, 1))
    elif change == "temperature":
        args["temperature"] = 26
    elif change == "temperature_overflow":
        model["dark_current_doubling_c"] = 1e-6
        model["read_noise"]["temperature_range_c"] = [0, 100]
        args["temperature"] = 35
    elif change == "dark_overflow":
        model["dark_current_e_per_s"] = 1e12
        args["exposure"] = 3600
    elif change == "zero_exposure":
        args["exposure"] = 0
    elif change == "noise_bool":
        args["noise"] = 1
    elif change == "seed_bool":
        args["seed"] = True
    elif change == "native_size":
        args["geometry"] = {"full_sensor_shape": [8192, 8192], "crop_origin": [0, 0]}
    else:
        args["geometry"] = {"full_sensor_shape": [2, 2], "crop_origin": [1, 1]}
    monkeypatch.setattr(dcg, "_patterns", lambda *args: pytest.fail("Allocated before validation"))
    with pytest.raises(ValueError):
        capture(model, photo, **args)

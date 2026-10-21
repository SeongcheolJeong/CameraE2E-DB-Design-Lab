"""Equivalent native green sites must use the common Bayer reconstruction."""

import numpy as np
import pytest

from pyisetcam.ip import (
    _equivalent_physical_bayer,
    _normalized_cfa_interpolation,
    _sensor_space_from_data,
    ip_compute,
    ip_create,
)
from pyisetcam.physical_estimate import apply_physical_estimate, image_sensor_physical_model
from pyisetcam.sensor import sensor_create
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.utils import tile_pattern


@pytest.fixture
def matched_bayer_sensors(asset_store):
    model = image_sensor_physical_model("DEF-2011-804")
    sensor = sensor_create("default", {"size": (32, 32)}, asset_store=asset_store)
    four = apply_physical_estimate(
        sensor,
        model,
        {
            "directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1}],
        },
    )
    four = configure_e2e_sensor(four)
    three = four.clone()
    three.fields.pop("physical_estimate")
    three.fields.pop("physical_illumination")
    three.fields["pattern"] = np.array([[1, 2], [2, 3]])
    three.fields["filter_names"] = ["red", "green", "blue"]
    three.fields["filter_spectra"] = four.fields["filter_spectra"][:, [0, 1, 3]].copy()
    return four, three


@pytest.mark.parametrize("method", ["bilinear", "nearest neighbor", "adaptive laplacian"])
def test_same_raw_has_identical_ordinary_bayer_reconstruction(matched_bayer_sensors, method):
    four, three = matched_bayer_sensors
    rng = np.random.default_rng(97)
    raw = rng.uniform(0.1, 0.8, size=(32, 32))
    before = four.clone()
    native = _sensor_space_from_data(four, raw, method)
    ordinary = _sensor_space_from_data(three, raw, method)
    np.testing.assert_array_equal(native, ordinary[..., [0, 1, 1, 2]])
    np.testing.assert_array_equal(four.fields["pattern"], before.fields["pattern"])
    assert four.fields["filter_names"] == ["R", "Gr", "Gb", "B"]


def test_noisy_measured_green_samples_are_preserved_without_extra_smoothing(
    matched_bayer_sensors,
):
    four, three = matched_bayer_sensors
    raw = np.random.default_rng(201).normal(size=(128, 128))
    sites = tile_pattern(four.fields["pattern"], 128, 128)
    green = (sites == 2) | (sites == 3)
    actual = _sensor_space_from_data(four, raw, "bilinear")
    ordinary = _sensor_space_from_data(three, raw, "bilinear")
    np.testing.assert_array_equal(actual[..., 1][green], raw[green])
    np.testing.assert_array_equal(actual[..., 2][green], raw[green])
    np.testing.assert_array_equal(actual[..., 1], ordinary[..., 1])
    assert np.var(actual[4:-4, 4:-4, 1]) == pytest.approx(0.625, rel=0.04)


def test_same_noisy_raw_has_equivalent_fixed_d65_isp_output_and_truthful_metadata(
    matched_bayer_sensors,
    asset_store,
):
    four, three = matched_bayer_sensors
    raw = np.random.default_rng(814).integers(500, 1500, size=(32, 32), dtype=np.int32)
    outputs = []
    for sensor in (four, three):
        sensor.data["raw_dn"] = raw.copy()
        sensor.data["dv"] = raw.copy()
        outputs.append(
            ip_compute(ip_create(asset_store=asset_store), sensor, asset_store=asset_store)
        )
    for name in ("xyz", "result"):
        np.testing.assert_allclose(outputs[0].data[name], outputs[1].data[name], atol=2e-14)
    metadata = outputs[0].metadata["sensor_input"]["demosaic"]
    assert metadata["applied"] == "bilinear"
    assert metadata["native_raw_sites"] == ["R", "Gr", "Gb", "B"]
    assert metadata["output_channel_mapping"] == ["R", "G", "G", "B"]
    assert metadata["post_interpolation_green_average"] is False
    assert metadata["measured_samples_preserved"] is True
    np.testing.assert_array_equal(four.data["raw_dn"], raw)


@pytest.mark.parametrize("case", ["fdtd", "distinct_green", "other_pattern", "other_names"])
def test_independent_native_sites_are_not_merged(matched_bayer_sensors, case):
    four, _ = matched_bayer_sensors
    if case == "fdtd":
        four.fields.pop("physical_estimate")
        four.fields["fdtd_estimate"] = {"kind": "fdtd_estimate_v1"}
    elif case == "distinct_green":
        four.fields["filter_spectra"] = four.fields["filter_spectra"].copy()
        four.fields["filter_spectra"][:, 2] *= 0.9
    elif case == "other_pattern":
        four.fields["pattern"] = np.array([[4, 2], [3, 1]])
    else:
        four.fields["filter_names"] = ["R", "G", "W", "B"]
    assert not _equivalent_physical_bayer(four)
    raw = np.random.default_rng(791).normal(size=(32, 32))
    tiled = tile_pattern(four.fields["pattern"], 32, 32)
    planes = np.stack([np.where(tiled == i, raw, 0) for i in (1, 2, 3, 4)], axis=2)
    expected = _normalized_cfa_interpolation(planes, four.fields["pattern"])
    actual = _sensor_space_from_data(four, raw)
    np.testing.assert_array_equal(actual, expected)
    assert not np.array_equal(actual[..., 1], actual[..., 2])

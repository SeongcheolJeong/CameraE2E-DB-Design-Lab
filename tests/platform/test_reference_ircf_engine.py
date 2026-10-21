"""Numerical seams between IRCF spectra, pupil integration, RAW and fixed ISP."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import reference_ircf_engine as engine
from camera_platform import reference_ircf_spectra as spectra
from camera_platform import reference_workshop_optics as optics
from camera_platform.reference_sensor_modes import process_electrons

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def assets():
    return spectra.load_assets(WORKSPACE)


@pytest.fixture(scope="module")
def pixel():
    return spectra.build_pixel_response(WORKSPACE)


def _solution(assets, size=16):
    wave = assets["wavelength_nm"]
    fields = [
        {
            "id": key,
            "normalized_radius": radius,
            "azimuth_deg": azimuth,
            "status": "succeeded",
            "issues": [],
            "geometry": {
                "input_shape_row_col": [size, size],
                "crop_bounds_row_col": [0, size, 0, size],
            },
        }
        for key, radius, azimuth in (("center", 0.0, 0), ("outer-ne", 0.85, 45))
    ]
    directions = np.empty((len(wave), 3, 2, 3))
    for wi in range(len(wave)):
        for ri in range(3):
            angles = np.deg2rad([5 + wi / 6 + 5 * ri, 20 + wi / 6 + 5 * ri])
            directions[wi, ri] = np.stack((np.sin(angles), np.zeros(2), np.cos(angles)), axis=-1)
    return {
        "result": {"schema": optics.IRCF_SCHEMA, "revision": "spectral-unit", "fields": fields},
        "arrays": {
            "wavelength_nm": wave.copy(),
            "spectral_bin_width_nm": assets["wavelength_bin_width_nm"].copy(),
            "conditional_weights": np.broadcast_to([0.4, 0.6], (len(wave), 3, 2)).copy(),
            # Deliberately incompatible legacy directions catch accidental use
            # in calibration, which must agree with the actual spectral rays.
            "directions_xyz": np.tile([0.0, 0.0, 1.0], (3, 2, 1)),
            "directions_xyz_per_wavelength": directions,
            "irradiance_per_radiance_sr": np.broadcast_to(
                [0.02, 0.015, 0.01], (len(wave), 3)
            ).copy(),
            "psf": np.ones((len(wave), 2, 1, 1)),
        },
    }


def test_spectral_calibration_uses_identical_pupil_angles_weights_and_units(assets, pixel):
    solution = _solution(assets)
    field = solution["result"]["fields"][1]
    density = assets["illuminants"]["day"]["photons_per_m2_sr_s_nm"]
    photons = np.broadcast_to(density, (16, 16, len(density)))
    for filter_id in ("baseline", "nir_leak_1pct"):
        rate_per_density, transmission = engine.spectral_response(
            solution, field, assets, pixel["response"], filter_id, "pupil", 1.8
        )
        actual = optics.apply_field(
            solution,
            field["id"],
            photons,
            angular_response=engine.callback(assets, pixel["response"], filter_id, "pupil", 1.8),
        )
        np.testing.assert_allclose(
            actual["center_electron_rate_per_band"], rate_per_density * density[:, None], rtol=1e-12
        )
        np.testing.assert_allclose(
            actual["planes_electron_rate"].mean(axis=(0, 1)),
            (rate_per_density * density[:, None]).sum(axis=0),
            rtol=1e-12,
        )
        assert transmission.shape == density.shape
        assert actual["metadata"]["pixel_aperture_applications"] == 1


def test_visible_nir_partition_sums_to_full_response(assets, pixel):
    solution = _solution(assets)
    wave = assets["wavelength_nm"]
    photons = np.broadcast_to(
        assets["illuminants"]["ill_a"]["photons_per_m2_sr_s_nm"], (16, 16, len(wave))
    )
    result = optics.apply_field(
        solution,
        "center",
        photons,
        angular_response=engine.callback(assets, pixel["response"], "nir_leak_1pct", "pupil", 1.8),
        spectral_groups={"visible": wave <= 700, "nir": wave > 700},
    )
    groups = result["group_planes_electron_rate"]
    np.testing.assert_allclose(
        groups["visible"] + groups["nir"], result["planes_electron_rate"], rtol=1e-12
    )
    assert groups["nir"].min() > 0


def test_calibration_is_white_constrained_and_does_not_read_macbeth_or_nir(assets, pixel):
    solution = _solution(assets)
    fields = solution["result"]["fields"]
    filters = ["baseline", "cutoff_minus20"]
    matrices, lsc, metadata = engine.calibrations(
        solution, assets, pixel["response"], fields, filters, 1.8
    )
    changed = copy.deepcopy(assets)
    changed["macbeth_reflectance"] *= 0.5
    changed["illuminants"]["ill_a"]["photons_per_m2_sr_s_nm"] *= 10
    response = copy.deepcopy(pixel["response"])
    qe = np.asarray(response["eqe"])
    qe[:, assets["wavelength_nm"] > 700] *= 0.1
    response["eqe"] = qe.tolist()
    other, _, _ = engine.calibrations(solution, changed, response, fields, filters, 1.8)
    for key in filters:
        np.testing.assert_array_equal(matrices[key], other[key])
        record = metadata["calibrations"][key]
        assert record["white_error"] < 1e-12
        assert record["evaluation_used_for_fit"] is False
        assert record["nir_aware_fit"] is False
    np.testing.assert_array_equal(lsc["center"], np.ones((256, 256)))


def test_frozen_lsc_restores_uniform_visible_white_once(assets, pixel):
    solution = _solution(assets)
    fields = solution["result"]["fields"]
    matrices, lsc, _ = engine.calibrations(
        solution, assets, pixel["response"], fields, ["baseline"], 1.8
    )
    light = assets["illuminants"]["d65_visible"]["photons_per_m2_sr_s_nm"]
    corrected = []
    for field in fields:
        basis, _ = engine.spectral_response(
            solution, field, assets, pixel["response"], "baseline", "pupil", 1.8
        )
        site_e = (basis * light[:, None]).sum(axis=0) * engine.EXPOSURE
        mosaic = engine.mosaic(np.broadcast_to(site_e, (256, 256, 4)))
        output = process_electrons(
            mosaic, matrices["baseline"], lsc_gain=lsc[field["id"]], variance_e2=np.ones((256, 256))
        )
        corrected.append(output["linear"][8:-8, 8:-8].mean(axis=(0, 1)))
        np.testing.assert_array_equal(
            output["lsc_mosaic_variance_e2_per_contributor"], lsc[field["id"]] ** 2
        )
    np.testing.assert_allclose(corrected[0], corrected[1], rtol=1e-11, atol=1e-12)


def test_chart_coordinates_extend_consistently_through_input_halo(assets):
    field = {
        "geometry": {"input_shape_row_col": [288, 288], "crop_bounds_row_col": [16, 272, 16, 272]}
    }
    pattern = engine.chart_pattern(field["geometry"])
    assert pattern.shape == (288, 288)
    assert pattern[0, 0] == pattern[16, 16] == 0
    assert pattern[-1, -1] == pattern[271, 271] == 23
    for i, (r0, r1, c0, c1) in enumerate(engine.patch_regions()):
        assert np.all(pattern[16 + r0 : 16 + r1, 16 + c0 : 16 + c1] == i)
    image = engine.scene(assets, field, "day", "color")
    density = assets["illuminants"]["day"]["photons_per_m2_sr_s_nm"]
    np.testing.assert_array_equal(image[32, 32], assets["macbeth_reflectance"][:, 0] * density)


def test_capture_independent_raw_and_same_raw_ccm_reprocessing(pixel, assets):
    rate = np.full((256, 256), 100 / engine.EXPOSURE)
    rate[:, -4:] = 20000 / engine.EXPOSURE
    captured = engine.acquire(rate, pixel["cell"]["electrical_model"], 71)
    assert captured["raw_frames"].dtype.kind in "iu"
    assert captured["raw_frames"].shape == (8, 256, 256)
    assert captured["seed_sequence"] == list(range(71, 79))
    assert not np.array_equal(captured["raw_frames"][0], captured["raw_frames"][1])
    assert captured["well_saturated_frames"][:, :, -4:].all()
    calibration = captured["calibration"]
    np.testing.assert_array_equal(
        captured["electron_frames"],
        (captured["raw_frames"].astype(float) - calibration["black_level_dn"])
        / calibration["dn_per_e"]
        - calibration["dark_e"],
    )
    expected_variance = (
        100
        + calibration["dark_e"]
        + calibration["read_noise_variance_e2"]
        + calibration["quantization_variance_e2"]
    )
    np.testing.assert_allclose(captured["variance_e2"][:, :-4], expected_variance[:, :-4])
    measured = np.var(captured["electron_frames"][:, 8:-8, 8:-8], axis=0, ddof=1).mean()
    assert measured == pytest.approx(expected_variance.mean(), rel=0.02)
    raw_copy = captured["raw_frames"].copy()
    electrons_copy = captured["electron_frames"].copy()
    spec = {"isp": {"display_white_level": 1, "tone_gamma": 2.2}}
    first = engine.processed_metrics(
        captured, np.eye(3) / 100, np.ones((256, 256)), spec, assets, "day", "uniform"
    )
    second = engine.processed_metrics(
        captured, np.eye(3) / 200, np.ones((256, 256)), spec, assets, "day", "uniform"
    )
    np.testing.assert_array_equal(captured["raw_frames"], raw_copy)
    np.testing.assert_array_equal(captured["electron_frames"], electrons_copy)
    np.testing.assert_allclose(second["linear"], first["linear"] / 2)
    assert first["metrics"]["temporal_noise_e"] == second["metrics"]["temporal_noise_e"]

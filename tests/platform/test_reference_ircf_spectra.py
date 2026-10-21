"""The IRCF demo must preserve spectral support and expose its NIR assumption."""

from pathlib import Path

import numpy as np
import pytest
from scipy.io import loadmat

from camera_platform import reference_ircf_spectra as spectra
from camera_platform.offline_assets import offline_asset_store
from pyisetcam.physical_estimate import physical_optical_response

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def assets():
    return spectra.load_assets(WORKSPACE)


@pytest.fixture(scope="module")
def pixel():
    return spectra.build_pixel_response(WORKSPACE)


def test_quadrature_and_density_units(assets):
    wave = assets["wavelength_nm"]
    widths = assets["wavelength_bin_width_nm"]
    assert wave.shape == (61,)
    np.testing.assert_array_equal(wave, np.arange(400, 1001, 10))
    assert widths.sum() == 600
    assert widths[0] == widths[-1] == 5
    for light in assets["illuminants"].values():
        photons = light["photons_per_m2_sr_s_nm"]
        np.testing.assert_allclose(light["photon_radiance_per_bin"], photons * widths)
        np.testing.assert_allclose(photons @ assets["xyz_per_photon"], light["white_xyz"] * 1000)
        assert light["luminance_cd_m2"] == pytest.approx(1000)
    assert spectra.wavelength_grid(5).size == 121
    for bad in (True, 0, 1, 20, float("nan")):
        with pytest.raises(ValueError):
            spectra.wavelength_grid(bad)


def test_munsell_visible_fit_has_no_invented_nir(assets):
    visible = assets["munsell_visible"]
    assert visible["reflectance"].shape == (31, 64)
    assert visible["wavelength_nm"][-1] == 700
    assert assets["macbeth_reflectance"].shape == (61, 24)
    assert np.all(assets["macbeth_reflectance"][-1] > 0)
    assert assets["metadata"]["munsell_nir_available"] is False
    assert assets["metadata"]["macbeth_used_in_fit"] is False
    assert "not CIE D65" in assets["metadata"]["day_label"]
    d65 = assets["illuminants"]["d65_visible"]["photons_per_m2_sr_s_nm"]
    assert np.all(d65[assets["wavelength_nm"] > 700] == 0)
    store = offline_asset_store(workspace=WORKSPACE)
    original = loadmat(store.resolve(spectra._FILES["munsell"]), simplify_cells=True)
    np.testing.assert_array_equal(visible["reflectance"], original["data"])


def test_cmf_does_not_extend_730nm_tail_as_constant(assets):
    wave, cmf = assets["wavelength_nm"], assets["cmf_xyz"]
    assert cmf[wave == 740, 1].item() > 0
    assert cmf[wave == 830, 1].item() == pytest.approx(0.00000045181)
    assert np.all(cmf[wave > 830] == 0)
    assert assets["metadata"]["cmf"]["license"] == "CC BY-SA 4.0"
    assert len(assets["metadata"]["cmf"]["source_sha256"]) == 64


def test_filter_baseline_preserves_loaded_transmission_at_normal_incidence(assets):
    wave = assets["wavelength_nm"]
    table = assets["filters"]["baseline_filter"]
    expected = np.interp(wave, table["wavelength_nm"], table["transmission"])
    np.testing.assert_allclose(spectra.filter_transmission(assets, "baseline", wave, 0), expected)
    np.testing.assert_allclose(
        spectra.filter_transmission(assets, "baseline", wave, 60, angular_mode="independent"),
        expected,
    )


def test_red_edge_shift_and_leak_do_not_change_blue_edge(assets):
    low = np.arange(400, 601, 5)
    baseline = spectra.filter_transmission(assets, "baseline", low, 0)
    for key in spectra.filter_profiles():
        np.testing.assert_array_equal(spectra.filter_transmission(assets, key, low, 50), baseline)
    for shift, key in ((-20, "cutoff_minus20"), (20, "cutoff_plus20")):
        np.testing.assert_allclose(
            spectra.filter_transmission(assets, key, 680, 0),
            spectra.filter_transmission(assets, "baseline", 680 - shift, 0),
        )
    leak = spectra.filter_transmission(assets, "nir_leak_1pct", [700, 750, 850, 1000], 0)
    assert leak[0] == spectra.filter_transmission(assets, "baseline", 700, 0)
    assert np.all(leak[1:] >= 0.01)


def test_angular_blue_shift_and_broadcast(assets):
    theta = np.array([[0], [30], [60]])
    wave = np.array([640, 660, 680])
    values = spectra.filter_transmission(assets, "baseline", wave, theta)
    assert values.shape == (3, 3)
    assert values[0, 1] > values[1, 1] > values[2, 1]
    assert np.all((values >= 0) & (values <= 1))
    with pytest.raises(ValueError, match="no extrapolation"):
        spectra.filter_transmission(assets, "baseline", 1000, 60, n_eff=1.1)
    with pytest.raises(ValueError):
        spectra.filter_transmission(assets, "baseline", 1001, 0)


def test_visible_pixel_is_the_existing_model_without_extrapolation(pixel):
    response, cell = pixel["response"], pixel["cell"]
    wave = np.asarray(response["wavelength_nm"])
    visible = wave <= 700
    theta = np.asarray(response["theta_deg"])
    direct = physical_optical_response(
        cell["physical_model"], wave[None, visible], theta_deg=theta[:, None], phi_deg=0
    )["eqe"]
    np.testing.assert_array_equal(np.asarray(response["eqe"])[:, visible], direct)
    assert cell["physical_model"]["wavelength_nm"][-1] == 650
    with pytest.raises(ValueError):
        physical_optical_response(cell["physical_model"], 710)


def test_nir_positive_declared_anchors_and_frozen_angle_ratio(pixel):
    response = pixel["response"]
    np.testing.assert_allclose(spectra.absolute_response(response, 0, 0, 800), [0.2] * 4)
    np.testing.assert_allclose(spectra.absolute_response(response, 0, 0, 900), [0.12] * 4)
    np.testing.assert_allclose(spectra.absolute_response(response, 0, 0, 1000), [0.02] * 4)
    ratio700 = spectra.absolute_response(response, 40, 0, 700) / spectra.absolute_response(
        response, 0, 0, 700
    )
    ratio900 = spectra.absolute_response(response, 40, 0, 900) / spectra.absolute_response(
        response, 0, 0, 900
    )
    np.testing.assert_allclose(ratio700, ratio900)
    assert pixel["metadata"]["nir_material_extrapolation"] is False
    assert pixel["metadata"]["product_measured"] is False
    assert "not IMX462 product QE" in pixel["metadata"]["nir_model"]


def test_pixel_scale_changes_only_nir_branch(pixel):
    reduced = spectra.build_pixel_response(WORKSPACE, nir_scale=0.5)
    normal = np.asarray(pixel["response"]["eqe"])
    changed = np.asarray(reduced["response"]["eqe"])
    wave = np.asarray(pixel["response"]["wavelength_nm"])
    np.testing.assert_array_equal(normal[:, wave <= 700], changed[:, wave <= 700])
    np.testing.assert_allclose(changed[:, wave >= 800], normal[:, wave >= 800] * 0.5)
    assert reduced["response"]["table_sha256"] != pixel["response"]["table_sha256"]


def test_pixel_query_domain_and_azimuth(pixel):
    response = pixel["response"]
    values = spectra.absolute_response(response, np.array([[0], [22.5]]), 91, [500, 805])
    assert values.shape == (2, 2, 4)
    assert np.all((values >= 0) & (values <= 1))
    np.testing.assert_array_equal(
        values, spectra.absolute_response(response, np.array([[0], [22.5]]), 0, [500, 805])
    )
    for theta, phi, wave in ((61, 0, 800), (0, 0, 1001), (0, float("nan"), 800)):
        with pytest.raises(ValueError):
            spectra.absolute_response(response, theta, phi, wave)

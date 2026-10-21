"""Numerical/ownership checks for the genuine CFA layer sensitivity loop."""

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.design_workshop_sensor import (
    absolute_angular_response,
    build_cell,
    build_response,
    interpolate_qe,
    photoelectron_rate_density,
    relative_angular_response,
)

WORKSPACE = Path(__file__).resolve().parents[2]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def cells():
    if not (WORKSPACE / "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json").exists():
        pytest.skip("Local canonical sensor material DB is needed for this integration test")
    return {scale: build_cell(WORKSPACE, scale) for scale in [0.9, 1.0, 1.1]}


@pytest.fixture(scope="module")
def responses(cells):
    return {scale: build_response(cell) for scale, cell in cells.items()}


def test_research_pitch_and_stack_are_explicit_with_only_cfa_passivation_changes(cells):
    baseline = cells[1.0]
    assert baseline["product_model"] is False
    assert baseline["pixel_pitch_um"] == 3.45
    assert baseline["provenance"]["source_pixel_pitch_um"] == 2.9
    for scale, cell in cells.items():
        geometry = cell["geometry_um"]
        assert geometry["cfa_thickness"] == pytest.approx(0.5 * scale)
        assert geometry["cfa_thickness"] + geometry["passivation_thickness"] == pytest.approx(1.65)
        assert cell["cross_section"]["top_stack_height_um"] == pytest.approx(2.5)
        for key, value in baseline["geometry_um"].items():
            if key not in {"cfa_thickness", "passivation_thickness"}:
                assert geometry[key] == value
        assert cell["readout"] == baseline["readout"]
        assert cell["electrical_model"] == baseline["electrical_model"]
        assert (
            cell["provenance"]["source_model_sha256"]
            == baseline["provenance"]["source_model_sha256"]
        )
    assert len({cell["cell_sha256"] for cell in cells.values()}) == 3


def test_cell_rebuild_preserves_source_files_and_is_hash_stable(cells):
    source_files = cells[1.0]["provenance"]["source_files"]
    before = {key: _sha(path) for key, path in source_files.items()}
    again = build_cell(WORKSPACE, 1.0)
    assert again == cells[1.0]
    assert before == {key: _sha(path) for key, path in source_files.items()}
    assert before["optical_model"] == again["provenance"]["source_optical_model_sha256"]
    assert before["generated_stack"] == again["provenance"]["source_generated_stack_sha256"]
    assert again["provenance"]["database_modified"] is False


def test_mutated_cell_is_rejected_instead_of_reusing_its_revision(cells):
    mutated = copy.deepcopy(cells[1.0])
    mutated["physical_model"]["geometry_um"]["cfa_thickness"] *= 1.1
    with pytest.raises(ValueError, match="hash mismatch"):
        build_response(mutated)


def test_cfa_thickness_changes_absolute_qe_and_spectral_leakage(responses):
    thin = interpolate_qe(responses[0.9], 0, 550)
    thick = interpolate_qe(responses[1.1], 0, 550)
    assert np.all(thin > thick)
    # A thicker green-pass layer sacrifices green signal but rejects R/B leakage
    # more strongly; this is an actual material/thickness calculation.
    assert thick[0] / thick[1] < thin[0] / thin[1]
    assert thick[3] / thick[1] < thin[3] / thin[1]
    assert thin[1] == pytest.approx(thin[2])
    assert thick[1] == pytest.approx(thick[2])
    assert np.max(np.abs(thick - thin)) > 0.01


def test_optical_energy_and_charge_budget_do_not_double_count_qe(responses):
    response = responses[1.0]
    layers = {key: np.asarray(val) for key, val in response["layer_absorption"].items()}
    reflection = np.asarray(response["reflection"])
    transmitted = np.asarray(response["uncollected_deep_transmission"])
    qe = np.asarray(response["eqe"])
    collection_loss = np.asarray(response["collection_loss"])
    assert qe.shape == (13, 21, 4)
    assert qe == pytest.approx(layers["silicon"] * 0.75)
    assert reflection + transmitted + sum(layers.values()) == pytest.approx(np.ones_like(qe))
    counted = reflection + transmitted + qe + collection_loss
    for key in ("lens", "cfa", "passivation"):
        counted += layers[key]
    assert counted == pytest.approx(np.ones_like(qe))
    assert np.max(np.abs(response["energy_residual"])) < 1e-12
    json.dumps(response, allow_nan=False)


def test_bilinear_interpolation_broadcast_azimuth_and_absolute_contract(responses):
    response = responses[1.0]
    values = np.asarray(response["eqe"])
    assert interpolate_qe(response, 2.5, 455) == pytest.approx(values[:2, :2].mean(axis=(0, 1)))
    theta = np.array([[0, 10], [20, 40]])
    absolute = absolute_angular_response(response, theta, 123, 550)
    assert absolute.shape == (2, 2, 4)
    assert absolute == pytest.approx(absolute_angular_response(response, theta, -67, 550))
    relative = relative_angular_response(response, theta, 0, 550)
    assert relative * interpolate_qe(response, 0, 550) == pytest.approx(absolute)
    assert relative[0, 0] == pytest.approx(np.ones(4))
    assert absolute[1, 1, 1] < absolute[0, 0, 1]


@pytest.mark.parametrize("theta,wave", [(-1, 550), (61, 550), (0, 449), (0, 651), (np.nan, 550)])
def test_no_out_of_domain_fallback(responses, theta, wave):
    with pytest.raises(ValueError, match="domain"):
        interpolate_qe(responses[1.0], theta, wave)


def test_photon_density_uses_absolute_qe_and_only_spectral_integral(responses):
    response = responses[1.0]
    photons = np.full((2, 3, 21), 2e12)
    theta = np.array([[0, 20, 40], [40, 20, 0]])
    rate = photoelectron_rate_density(photons, response, theta)
    wave = np.asarray(response["wavelength_nm"])
    qe = interpolate_qe(response, theta[..., None], wave)
    expected = np.trapezoid(photons[..., None] * qe, wave, axis=-2)
    assert rate.shape == (2, 3, 4)
    assert rate == pytest.approx(expected)
    assert rate.min() > 1e12  # density, not electron count after pixel-area multiplication
    assert photoelectron_rate_density(3 * photons, response, theta) == pytest.approx(3 * rate)
    explicit = photoelectron_rate_density(
        photons, response, theta, wavelength_bin_width_nm=np.full(21, 10)
    )
    assert explicit == pytest.approx((photons[..., None] * qe * 10).sum(axis=-2))


def test_single_wavelength_table_and_invalid_configuration(cells):
    response = build_response(cells[1.0], wavelength_nm=[550], theta_deg=[0])
    assert interpolate_qe(response, 0, 550) == pytest.approx(response["normal_qe"][0])
    rate = photoelectron_rate_density([10.0], response, wavelength_bin_width_nm=[2])
    assert rate == pytest.approx(np.asarray(response["normal_qe"][0]) * 20)
    with pytest.raises(ValueError, match="bin width"):
        photoelectron_rate_density([10.0], response)
    with pytest.raises(ValueError, match="one of"):
        build_cell(WORKSPACE, 1.05)
    with pytest.raises(ValueError, match="theta_deg"):
        build_response(cells[1.0], theta_deg=[5, 10])

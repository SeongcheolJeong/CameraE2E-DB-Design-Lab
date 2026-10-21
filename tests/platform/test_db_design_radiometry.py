"""Dense aperture adapter keeps the original measure and rejection criteria."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import db_design_optics as db
from camera_platform import db_design_radiometry as dense
from camera_platform import lens_factory
from camera_platform import reference_workshop_optics as optics

ROOT = Path(__file__).resolve().parents[2]


def _prescription():
    context = db._source(db.aperture_key(11))
    data = optics._request(
        ROOT, db.template(db.aperture_key(11)), [450.0, 550.0], source_context=context
    )["prescription"]
    data.update(image_height_mm=[0.0, 11.0], field_radius=[0.0, 1.0])
    data["sampling"].update(context["reciprocal_sampling"])
    return data


def test_dense_budget_is_explicit_and_does_not_modify_public_factory():
    before = (lens_factory.MAX_RAYS, lens_factory.MAX_INTERSECTIONS, lens_factory.MAX_SECONDS)
    request = dense.request(_prescription())
    data, rays, visits = dense._validate(request)
    assert rays == 1_966_080 and visits == 72_744_960
    assert request["prescription"]["sampling"]["polar_rings"] == 28
    assert before == (65_536, 1_000_000, 45.0)
    assert before == (
        lens_factory.MAX_RAYS,
        lens_factory.MAX_INTERSECTIONS,
        lens_factory.MAX_SECONDS,
    )
    larger = copy.deepcopy(data)
    larger["sampling"].update(polar_rings=256, azimuth_samples=256)
    with pytest.raises(ValueError):
        lens_factory.validate_request(larger)
    request["budget"]["max_rays"] *= 2
    with pytest.raises(ValueError, match="fixed"):
        dense._validate(request)


def _pass_result():
    return {
        "irradiance_per_radiance_sr": np.array([[0.8, 0.4], [0.9, 0.3]]),
        "relative_illumination": np.array([[1.0, 0.5], [1.0, 1 / 3]]),
        "angular_weights": np.tile([0.1, 0.2, 0.7], (2, 2, 1)),
        "failure_counts": np.zeros((2, 2, 4), dtype=int),
    }


@pytest.mark.parametrize("failure", ["integral", "ri", "cdf", "geometry", "zero", "nan"])
def test_dense_adapter_does_not_relax_factory_acceptance(failure):
    data = _prescription()
    coarse, fine = _pass_result(), _pass_result()
    assert dense._agreement(data, coarse, fine)["observed_two_level_agreement"]
    if failure == "integral":
        coarse["irradiance_per_radiance_sr"][0, 1] *= 1.031
    elif failure == "ri":
        coarse["relative_illumination"][0, 1] += 0.031
    elif failure == "cdf":
        coarse["angular_weights"][0, 1] = [0.151, 0.2, 0.649]
    elif failure == "geometry":
        coarse["failure_counts"][0, 1, 3] = 1
    elif failure == "zero":
        fine["irradiance_per_radiance_sr"][0, 1] = 0
    else:
        fine["irradiance_per_radiance_sr"][0, 1] = np.nan
    assert not dense._agreement(data, coarse, fine)["observed_two_level_agreement"]


def test_actual_direction_weights_conserve_all_transmitted_flux_and_spectral_mask():
    weights = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    accepted = np.array([[[1, 0, 1], [0, 1, 1]], [[0, 1, 1], [1, 0, 1]]], dtype=bool)
    transmission = np.array([[[0.5, 0, 1], [0, 0.7, 0.3]], [[0, 0.2, 0.8], [1, 0, 0.6]]])
    power = weights[None] * accepted * transmission
    fine = {
        "quadrature_weights_sr": weights,
        "accepted_rays": accepted,
        "stop_power_transmittance": transmission,
        "irradiance_per_radiance_sr": power.sum(-1),
    }
    actual = dense._conditional(fine)
    np.testing.assert_allclose(actual * power.sum(-1)[..., None], power, rtol=1e-14)
    np.testing.assert_allclose(actual.sum(-1), 1)
    assert np.all(actual[~accepted] == 0)
    broken = copy.deepcopy(fine)
    broken["irradiance_per_radiance_sr"][0, 0] *= 2
    with pytest.raises(ValueError, match="conserve"):
        dense._conditional(broken)


def test_new_source_requests_dense_profile_and_historical_sources_do_not():
    assert db._source(db.aperture_key(11))["reciprocal_profile"] == dense.PROFILE
    for key in ("reference", "canon_p0248", "canon_p0248_open"):
        assert "reciprocal_profile" not in db._source(key)
    assert "dense_radiometry_adapter_sha256" in optics._backend()


def test_mixed_grid_spectral_padding_preserves_actual_angles_and_photon_weights():
    rays = {
        450.0: {
            "conditional": np.array([[0.25, 0.75], [0.5, 0.5]]),
            "directions": np.tile([[0.6, 0, 0.8], [0, 0, 1]], (2, 1, 1)),
        },
        550.0: {
            "conditional": np.array([[0, 0.3, 0.7], [0.5, 0, 0.5]]),
            "directions": np.tile([[0, 0, 1], [0, 0.8, 0.6], [0, 0, 1]], (2, 1, 1)),
        },
    }
    weights, directions = dense.spectral_directions(rays, [450.0, 550.0])
    assert weights.shape == (2, 2, 3)
    for wi, wave in enumerate((450.0, 550.0)):
        n = rays[wave]["conditional"].shape[-1]
        np.testing.assert_array_equal(weights[wi, :, :n], rays[wave]["conditional"])
        np.testing.assert_array_equal(directions[wi, :, :n], rays[wave]["directions"])
    np.testing.assert_allclose(np.linalg.norm(directions, axis=-1), 1)
    np.testing.assert_array_equal(weights[0, :, 2], 0)
    solution = {
        "result": {
            "schema": optics.SCHEMA,
            "fields": [
                {
                    "id": "center",
                    "status": "succeeded",
                    "pupil_node_index": 0,
                    "normalized_radius": 0.0,
                    "azimuth_deg": 0,
                }
            ],
        },
        "arrays": {
            "conditional_weights": weights,
            "directions_xyz_per_wavelength": directions,
            "directions_xyz": directions[1],
        },
    }
    theta, _, power = optics.pupil_samples(solution, "center", 0)
    np.testing.assert_allclose(theta, [np.degrees(np.arccos(0.8)), 0])
    np.testing.assert_array_equal(power, [0.25, 0.75])

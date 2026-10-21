"""Analytic limits and capture contracts for an explicitly unmeasured proxy."""

import copy
import json

import numpy as np
import pytest

from pyisetcam.physical_estimate import (
    apply_physical_estimate,
    image_sensor_physical_model,
    incoherent_stack_response,
    physical_estimate_current_density,
    physical_optical_response,
    physical_response,
)
from pyisetcam.sensor import _ELEMENTARY_CHARGE_C, sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage


def _rays(theta=0, phi=0, weight=1):
    return {"directions": [{"theta_deg": theta, "phi_deg": phi, "weight": weight}]}


@pytest.fixture(scope="module")
def physical_model():
    return image_sensor_physical_model("DEF-2011-804")


def _sensor_oi(model, illumination, asset_store):
    sensor = sensor_create("default", {"size": (8, 8)}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.01)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = apply_physical_estimate(sensor, model, illumination)
    sensor = configure_e2e_sensor(sensor, readout={"dark_current_e_per_s": 0, "bit_depth": 24})
    wave = np.arange(400.0, 701.0, 10.0)
    oi = OpticalImage(
        fields={"wave": wave, "width_m": 8 * model["pixel_pitch_um"] * 1e-6},
        data={"photons": np.full((8, 8, wave.size), 1e14)},
    )
    return sensor, oi


@pytest.mark.parametrize("angle", [0, 30, 60])
def test_index_matched_absorber_is_beer_lambert_with_snell_path(angle):
    wave = np.array([450.0, 550.0, 650.0])
    result = incoherent_stack_response(
        wave, [{"n": 1.0, "k": 0.03, "thickness_um": 2.0}], angle_deg=angle
    )
    expected = np.exp(-4 * np.pi * 0.03 * 2 / (wave / 1000 * np.cos(np.deg2rad(angle))))
    np.testing.assert_allclose(result["T"], expected, rtol=1e-14)
    np.testing.assert_allclose(result["A"], 1 - expected, rtol=1e-14)
    np.testing.assert_array_equal(result["R"], 0)


def test_silicon_continuation_has_no_artificial_back_reflection_or_fringes():
    wave = np.arange(400.0, 701.0)
    thin = incoherent_stack_response(wave, [{"n": 4.0, "k": 0.0, "thickness_um": 0.5}])
    thick = incoherent_stack_response(wave, [{"n": 4.0, "k": 0.0, "thickness_um": 50.0}])
    np.testing.assert_allclose(thin["R"], ((4 - 1) / (4 + 1)) ** 2)
    np.testing.assert_allclose(thin["T"], 1 - thin["R"])
    np.testing.assert_array_equal(thin["A"], 0)
    np.testing.assert_array_equal(thin["R"], thick["R"])
    np.testing.assert_array_equal(thin["T"], thick["T"])


def test_zero_thickness_filter_is_removed_and_zero_active_si_absorbs_nothing():
    result = incoherent_stack_response(
        550.0,
        [{"n": 1.5, "k": 0.1, "thickness_um": 0}, {"n": 4.0, "k": 0.03, "thickness_um": 1.0}],
    )
    bare = incoherent_stack_response(550.0, [{"n": 4.0, "k": 0.03, "thickness_um": 1.0}])
    np.testing.assert_allclose(result["R"], bare["R"])
    np.testing.assert_allclose(result["T"], bare["T"])
    assert result["layer_absorption"][0] == 0
    empty = incoherent_stack_response(550.0, [{"n": 4.0, "k": 0.03, "thickness_um": 0}])
    assert empty["A"] == 0
    assert empty["R"] == pytest.approx(0.36)


def test_invalid_readout_is_rejected_at_model_construction():
    with pytest.raises(ValueError, match="full_well_e"):
        image_sensor_physical_model("DEF-2011-804", readout={"full_well_e": -1})


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
def test_multiple_reflection_power_budget_and_opaque_limit(polarization):
    result = incoherent_stack_response(
        np.array([450.0, 550.0, 650.0]),
        [
            {"n": 1.6, "k": 0.1, "thickness_um": 0.5},
            {"n": 1.46, "k": 0, "thickness_um": 1.0},
            {"n": 4.0, "k": 2.0, "thickness_um": 100.0},
        ],
        angle_deg=40,
        polarization=polarization,
    )
    np.testing.assert_allclose(result["R"] + result["T"] + result["A"], 1, atol=5e-15)
    np.testing.assert_array_equal(result["layer_absorption"][:, 1], 0)
    assert np.all(result["layer_absorption"] >= 0)
    assert np.all(result["T"] < 1e-100)


def test_real_n_single_interface_brewster_limit():
    brewster = np.degrees(np.arctan(1.5))
    result = incoherent_stack_response(
        550.0, [{"n": 1.5, "k": 0, "thickness_um": 1}], angle_deg=brewster, polarization="p"
    )
    assert float(result["R"]) < 1e-25
    assert float(result["T"]) == pytest.approx(1.0)


def test_planar_budget_is_separate_from_routing_and_collection(physical_model):
    result = physical_optical_response(physical_model, np.arange(400.0, 701.0, 10.0), theta_deg=35)
    np.testing.assert_allclose(
        result["R"] + result["T"] + sum(result["layer_absorption"].values()), 1, atol=1e-14
    )
    np.testing.assert_array_equal(result["layer_absorption"]["lens"], 0)
    np.testing.assert_array_equal(result["layer_absorption"]["passivation"], 0)
    np.testing.assert_allclose(
        result["accepted_silicon_absorption"] + result["routing_loss"],
        result["layer_absorption"]["silicon"],
    )
    np.testing.assert_allclose(result["eqe"], 0.75 * result["accepted_silicon_absorption"])
    np.testing.assert_array_equal(result["routing_loss"], 0)
    np.testing.assert_array_equal(result["applied_routing_acceptance"], 1)
    assert np.all((result["eqe"] >= 0) & (result["eqe"] <= 0.75))
    assert np.all((result["microlens_acceptance"] > 0) & (result["microlens_acceptance"] < 1))
    np.testing.assert_array_equal(result["eqe"][:, 1], result["eqe"][:, 2])


def test_model_is_json_safe_and_uncalibrated_with_explicit_inputs(physical_model):
    json.dumps(physical_model, allow_nan=False)
    assert physical_model["estimate_only"]
    assert physical_model["provenance"]["measured_accuracy_verified"] is False
    assert "nonreflecting" in physical_model["provenance"]["silicon_back_boundary"]
    assert "not_measured" in physical_model["optical_assumptions"]["acceptance_source"]
    assert len(physical_model["provenance"]["model_assumptions_sha256"]) == 64
    assert "validation" not in physical_model


def test_collection_and_weights_apply_once(physical_model):
    half = copy.deepcopy(physical_model)
    half["collection_efficiency"] /= 2
    wave = [450.0, 550.0, 650.0]
    first = physical_response(physical_model, wave, _rays(20, 45, 1))
    np.testing.assert_allclose(physical_response(physical_model, wave, _rays(20, 45, 70)), first)
    np.testing.assert_allclose(physical_response(half, wave, _rays(20, 45)), first / 2)
    mixed = {"directions": [*_rays(0, 0, 1)["directions"], *_rays(20, 45, 3)["directions"]]}
    np.testing.assert_allclose(
        physical_response(physical_model, wave, mixed),
        0.25 * physical_response(physical_model, wave, _rays()) + 0.75 * first,
    )


def test_cfa_absorption_changes_only_requested_material_channel(physical_model):
    model = image_sensor_physical_model(
        "DEF-2011-804",
        optical_assumptions={
            "cfa_k_scale": {"red": 1.2, "green": 1.0, "blue": 1.0},
        },
    )
    before, after = np.asarray(physical_model["eqe"]), np.asarray(model["eqe"])
    assert np.all(after[:, 0] <= before[:, 0])
    assert np.any(after[:, 0] < 0.95 * before[:, 0])
    np.testing.assert_array_equal(after[:, 1:], before[:, 1:])


def test_explicit_lower_bound_retains_microlens_shape_shift_diffraction_and_azimuth():
    scenario = {"routing_mode": "own_site_lower_bound"}
    physical_model = image_sensor_physical_model("DEF-2011-804", optical_assumptions=scenario)
    wave = [450.0, 550.0, 650.0]
    normal = physical_response(physical_model, wave, _rays())
    oblique = physical_response(physical_model, wave, _rays(35))
    assert np.all(oblique < normal)
    assert not np.allclose(oblique / normal, (oblique / normal)[0, 0])
    axis = physical_response(physical_model, wave, _rays(35, 0))
    diagonal = physical_response(physical_model, wave, _rays(35, 45))
    assert not np.allclose(axis, diagonal)
    np.testing.assert_allclose(axis, physical_response(physical_model, wave, _rays(35, 90)))
    for settings in (
        {"lens_height_scale": 1.1},
        {"diffraction_scale": 1.2},
        {"acceptance_width_fraction": 0.8},
    ):
        model = image_sensor_physical_model(
            "DEF-2011-804", optical_assumptions={**scenario, **settings}
        )
        assert not np.allclose(physical_response(model, wave, _rays(35)), oblique)
    shifted = image_sensor_physical_model(
        "DEF-2011-804", optical_assumptions={**scenario, "lens_shift_um": [0.1, 0]}
    )
    plus, minus = [physical_response(shifted, wave, _rays(35, phi)) for phi in (0, 180)]
    assert not np.allclose(plus, minus)


def test_disabling_microlens_diagnostic_does_not_raise_nominal_qe(physical_model):
    model = image_sensor_physical_model(
        "DEF-2011-804", optical_assumptions={"microlens_enabled": False}
    )
    result = physical_optical_response(model, [450.0, 550.0, 650.0], theta_deg=35)
    np.testing.assert_array_equal(result["microlens_acceptance"], 1)
    np.testing.assert_allclose(result["eqe"], 0.75 * result["layer_absorption"]["silicon"])
    np.testing.assert_allclose(
        result["eqe"], physical_response(physical_model, [450.0, 550.0, 650.0], _rays(35))
    )


def test_full_active_aperture_cannot_turn_own_site_displacement_into_nominal_loss():
    settings = {"acceptance_width_fraction": 1.0}
    nominal = image_sensor_physical_model("DEF-2011-804", optical_assumptions=settings)
    result = physical_optical_response(nominal, [450.0, 550.0, 650.0], theta_deg=35, phi_deg=0)
    # The Gaussian own-site overlap still loses light off-site; the periodic
    # fully active array can recollect it. That is not justified net shading.
    assert np.any(result["own_site_acceptance"] < 0.8)
    np.testing.assert_array_equal(result["applied_routing_acceptance"], 1)
    np.testing.assert_allclose(result["eqe"], 0.75 * result["layer_absorption"]["silicon"])
    lower = image_sensor_physical_model(
        "DEF-2011-804",
        optical_assumptions={
            **settings,
            "routing_mode": "own_site_lower_bound",
        },
    )
    pessimistic = physical_optical_response(lower, [450.0, 550.0, 650.0], theta_deg=35)
    np.testing.assert_allclose(pessimistic["eqe"], result["own_site_eqe_lower_bound"])
    np.testing.assert_allclose(
        pessimistic["routing_loss"],
        result["own_site_routing_loss_bound"],
    )
    assert np.all(pessimistic["eqe"] < result["eqe"])
    assert nominal["provenance"]["microlens_overlap_applied_to_qe"] is False
    assert lower["provenance"]["microlens_overlap_applied_to_qe"] is True


def test_nominal_retains_planar_angle_response_without_claiming_microlens_color_shading(
    physical_model,
):
    wave = [450.0, 550.0, 650.0]
    center = physical_optical_response(physical_model, wave, theta_deg=0)
    edge_x = physical_optical_response(physical_model, wave, theta_deg=35, phi_deg=0)
    edge_xy = physical_optical_response(physical_model, wave, theta_deg=35, phi_deg=45)
    assert not np.allclose(center["eqe"], edge_x["eqe"])
    np.testing.assert_array_equal(edge_x["eqe"], edge_xy["eqe"])
    assert not np.allclose(edge_x["own_site_acceptance"], edge_xy["own_site_acceptance"])
    shifted = image_sensor_physical_model(
        "DEF-2011-804",
        optical_assumptions={
            "lens_shift_um": [0.1, 0],
            "diffraction_scale": 1.2,
        },
    )
    different_diagnostic = physical_optical_response(shifted, wave, theta_deg=35)
    np.testing.assert_array_equal(different_diagnostic["eqe"], edge_x["eqe"])
    assert not np.allclose(
        different_diagnostic["own_site_acceptance"],
        edge_x["own_site_acceptance"],
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": 1},
        {"cfa_k_scale": 0},
        {"cfa_n_offset": -2},
        {"acceptance_width_fraction": 1.1},
        {"lens_shift_um": [10, 0]},
        {"microlens_enabled": "yes"},
        {"routing_mode": "discard_neighbor_photons_silently"},
        {"lens_height_scale": np.nan},
    ],
)
def test_invalid_optical_assumptions_are_not_silently_replaced(settings):
    with pytest.raises(ValueError):
        image_sensor_physical_model("DEF-2011-804", optical_assumptions=settings)


@pytest.mark.parametrize("wave,theta", [([399.0], 0), ([701.0], 0), ([550.0], 61), ([550.0], -1)])
def test_no_spectral_or_angular_extrapolation(physical_model, wave, theta):
    with pytest.raises(ValueError):
        physical_response(physical_model, wave, _rays(theta))


def test_capture_uses_spectral_angle_response_and_fixed_normal_reference(
    physical_model, asset_store
):
    sensor, oi = _sensor_oi(physical_model, _rays(20, 45), asset_store)
    response = physical_response(physical_model, oi.fields["wave"], _rays(20, 45))
    expected_current = response.sum(axis=0) * 10 * 1e14 * _ELEMENTARY_CHARGE_C
    np.testing.assert_allclose(
        physical_estimate_current_density(oi, sensor),
        np.broadcast_to(expected_current, (8, 8, 4)),
        rtol=1e-13,
    )
    captured = sensor_compute(sensor, oi)
    expected_by_site = (
        expected_current
        / _ELEMENTARY_CHARGE_C
        * 0.01
        * (physical_model["pixel_pitch_um"] * 1e-6) ** 2
    )
    for row, col, site in ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)):
        np.testing.assert_allclose(
            captured.data["mean_electrons"][row::2, col::2], expected_by_site[site], rtol=1e-12
        )
    assert captured.data["raw_dn"].dtype == np.int32
    assert "material_bundle" not in captured.metadata["sensor_estimate"]
    np.testing.assert_array_equal(captured.fields["filter_spectra"], physical_model["eqe"])
    oi.fields["fov_deg"] = 120
    np.testing.assert_array_equal(
        sensor_compute(sensor, oi).data["mean_electrons"], captured.data["mean_electrons"]
    )


def test_exit_pupil_path_and_weighted_rays_agree_at_center(physical_model, asset_store):
    from pyisetcam.fdtd_estimate import _illumination_config, _incident_directions

    config = _illumination_config(
        {
            "exit_pupil_distance_m": 0.01,
            "image_side_f_number": 2,
            "radial_samples": 2,
            "azimuth_samples": 4,
        }
    )
    directions = _incident_directions(config, np.zeros((1, 1)), np.zeros((1, 1)))
    rays = {
        "directions": [
            {
                "theta_deg": float(theta[0, 0]),
                "phi_deg": float(phi[0, 0]),
                "weight": float(weight[0, 0]),
            }
            for theta, phi, weight in directions
        ]
    }
    np.testing.assert_allclose(
        physical_response(physical_model, [450, 550, 650], config),
        physical_response(physical_model, [450, 550, 650], rays),
    )
    sensor, oi = _sensor_oi(physical_model, config, asset_store)
    current = physical_estimate_current_density(oi, sensor)
    assert np.all(np.isfinite(current)) and np.all(current > 0)
    np.testing.assert_allclose(current, current[::-1, ::-1], rtol=1e-12)


@pytest.mark.parametrize(
    "field,value", [("pixel_qe", 0.5), ("vignetting", 1), ("filter_spectra", 0.5)]
)
def test_runtime_rejects_double_counted_or_overridden_optics(
    physical_model, asset_store, field, value
):
    sensor, oi = _sensor_oi(physical_model, _rays(), asset_store)
    sensor.fields[field] = value
    with pytest.raises(ValueError, match="compound"):
        physical_estimate_current_density(oi, sensor)


def test_switching_back_to_db_estimate_clears_physical_runtime(physical_model, asset_store):
    from pyisetcam.sensor_estimate import apply_sensor_estimate, image_sensor_db_model

    sensor, _ = _sensor_oi(physical_model, _rays(), asset_store)
    changed = apply_sensor_estimate(sensor, image_sensor_db_model("DEF-2011-804"))
    assert "physical_estimate" not in changed.fields
    assert "physical_illumination" not in changed.fields
    assert changed.metadata["sensor_estimate"]["kind"] == "db_estimate_v1"


def test_replacement_clears_previous_captures_and_preserves_readout_profile(
    physical_model, asset_store
):
    model = copy.deepcopy(physical_model)
    model["readout_profile"] = {"operating_mode": "synthetic_profile_metadata_only"}
    sensor, oi = _sensor_oi(model, _rays(), asset_store)
    captured = sensor_compute(sensor, oi)
    captured.fields["e2e_readout_profile"] = {"name": "stale_previous_sensor_profile"}
    captured.metadata["sensor_readout_profile"] = {"name": "stale_previous_sensor_profile"}
    assert captured.data
    changed = apply_physical_estimate(captured, model, _rays(20))
    assert not changed.data
    assert "sensor_capture" not in changed.metadata
    assert "sensor_readout_profile" not in changed.metadata
    assert "e2e_readout_profile" not in changed.fields
    assert changed.metadata["sensor_estimate"]["readout_profile"] == model["readout_profile"]


def test_direct_db_and_fdtd_replacements_clear_stale_readout_profile(physical_model, asset_store):
    from pyisetcam.fdtd_estimate import apply_fdtd_estimate
    from pyisetcam.sensor_estimate import apply_sensor_estimate, image_sensor_db_model

    sensor, _ = _sensor_oi(physical_model, _rays(), asset_store)
    sensor.fields["e2e_readout_profile"] = {"name": "stale_previous_sensor_profile"}
    sensor.metadata["sensor_readout_profile"] = {"name": "stale_previous_sensor_profile"}
    db = apply_sensor_estimate(sensor, image_sensor_db_model("DEF-2011-804"))
    # The apply-only boundary needs no real LUT: this in-memory synthetic stub
    # deliberately tests profile invalidation, never numerical FDTD validation.
    stub = copy.deepcopy(physical_model)
    stub.update(kind="fdtd_estimate_v1", validation={"status": "passed"})
    fdtd = apply_fdtd_estimate(sensor, stub, _rays())
    for result in (db, fdtd):
        assert "e2e_readout_profile" not in result.fields
        assert "sensor_readout_profile" not in result.metadata

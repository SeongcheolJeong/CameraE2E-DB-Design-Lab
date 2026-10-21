"""Native source routing, strict optical boundary and shot-noise statistics."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import charge_collection as collection
from camera_platform import electrical
from camera_platform import native_pixel_response as pixel


def model():
    return {
        "schema": "camera_charge_collection_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 2,
        "wavelength_nm": [450, 650],
        "destination_offsets_rc": [[0, 0], [0, 1]],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": [[[0.75, 0.25] for _ in range(4)] for _ in range(2)],
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": {"kind": "estimated", "description": "Authored carrier routing fixture"},
    }


def generation_response():
    return {
        "schema": pixel.GENERATION_SCHEMA,
        "site_names": list(pixel.SITE_NAMES),
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 2,
        "reference_plane": pixel.REFERENCE_PLANE,
        "direction_convention": pixel.DIRECTION_CONVENTION,
        "polarization": "unpolarized",
        "response_basis": pixel.GENERATION_RESPONSE_BASIS,
        "wavelength_nm": [450, 650],
        "theta_deg": [0],
        "phi_deg": [0],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": [[[[0.2, 0.4, 0.6, 0.8]]], [[[0.4, 0.6, 0.8, 1.0]]]],
        "source": {"kind": "estimated", "description": "Optical generation only"},
    }


def test_contract_normalization_is_detached_json_and_strictly_idempotent():
    data = model()
    original = deepcopy(data)
    normalized = collection.validate_collection(data)
    assert normalized == original
    assert collection.validate_collection(normalized) == normalized
    assert json.loads(json.dumps(normalized, allow_nan=False)) == normalized
    normalized["probabilities"][0][0][0] = 0
    normalized["site_index_tile"][0][0] = 3
    normalized["source"]["description"] = "changed"
    normalized["destination_offsets_rc"][0][0] = 4
    normalized["wavelength_nm"][0] = 460
    assert data == original


def test_generation_schema_retains_math_but_never_converts_collected_qe():
    optical, transport = collection.validate_pair(generation_response(), model())
    assert optical["schema"] == pixel.GENERATION_SCHEMA
    assert transport["schema"] == collection.SCHEMA
    np.testing.assert_allclose(pixel.evaluate(optical, 550, 0, -17), [0.3, 0.5, 0.7, 0.9])
    collected = deepcopy(optical)
    collected.update(schema=pixel.SCHEMA, response_basis=pixel.RESPONSE_BASIS)
    assert pixel.validate_response(collected) == collected
    np.testing.assert_array_equal(pixel.evaluate(optical, 575, 0, 31),
                                  pixel.evaluate(collected, 575, 0, 31))
    with pytest.raises(ValueError, match="not collected EQE"):
        collection.validate_pair(collected, model())
    for response in (optical, collected):
        forged = deepcopy(response)
        forged["response_basis"] = (pixel.RESPONSE_BASIS if response is optical
                                    else pixel.GENERATION_RESPONSE_BASIS)
        with pytest.raises(ValueError, match="response_basis"):
            pixel.validate_response(forged)
    with pytest.raises(ValueError):
        collection.validate_pair(optical, None)


@pytest.mark.parametrize("field,value", [
    ("wavelength_nm", [450, 651]), ("pixel_pitch_um", 2.01),
    ("site_names", ["R", "Gb", "Gr", "B"]), ("site_index_tile", [[0, 2], [1, 3]]),
])
def test_pair_refuses_axis_and_native_geometry_mismatches(field, value):
    data = model()
    data[field] = value
    with pytest.raises(ValueError):
        collection.validate_pair(generation_response(), data)


def test_asymmetric_source_sites_and_absorbing_edge_have_exact_charge_ledger():
    data = model()
    data["destination_offsets_rc"] = [[0, 0], [0, 1], [1, 1]]
    data["probabilities"] = [
        [[.1, .5, .1], [.2, .4, .2], [.3, .3, .1], [.4, .2, .1]] for _ in range(2)
    ]
    generated = np.array([[10., 20.], [30., 40.]])
    before = generated.copy()
    result = collection.transport(generated, 550, data)
    arrays, diagnostics = result["arrays"], result["diagnostics"]
    np.testing.assert_array_equal(generated, before)
    np.testing.assert_allclose(arrays["collected"], [[1, 9], [9, 26]])
    np.testing.assert_allclose(arrays["intrinsic_loss"], [[3, 4], [9, 12]])
    np.testing.assert_allclose(arrays["off_sensor_loss"], [[0, 12], [3, 12]])
    assert diagnostics["generated_total"] == 100
    assert diagnostics["collected_total"] == pytest.approx(45)
    assert diagnostics["intrinsic_loss_total"] == pytest.approx(28)
    assert diagnostics["off_sensor_loss_total"] == pytest.approx(27)
    assert diagnostics["conservation_residual"] == pytest.approx(0, abs=1e-12)
    assert diagnostics["loss_map_coordinates"] == "source_pixel"
    arrays["generated"][0, 0] = 200
    np.testing.assert_array_equal(generated, before)


def test_wavelength_interpolates_each_destination_and_intrinsic_loss():
    data = model()
    data["probabilities"] = [[[1., 0.] for _ in range(4)], [[0., .5] for _ in range(4)]]
    source = np.zeros((5, 6))
    source[2, 2] = 100
    result = collection.transport(source, 550, data)["arrays"]
    assert result["collected"][2, 2] == 50
    assert result["collected"][2, 3] == 25
    assert result["intrinsic_loss"].sum() == 25
    assert result["off_sensor_loss"].sum() == 0
    assert collection.transport(source, 450, data)["arrays"]["collected"][2, 2] == 100
    assert collection.transport(source, 650, data)["arrays"]["collected"][2, 3] == 50


@pytest.mark.parametrize("offset,expected", [
    ([-1, 0], (1, 2)), ([1, 0], (3, 2)), ([0, -1], (2, 1)),
    ([0, 1], (2, 3)), ([-1, -1], (1, 1)), ([1, 1], (3, 3)),
])
def test_signed_offsets_scatter_from_source_without_axis_reversal(offset, expected):
    data = model()
    data.update(destination_offsets_rc=[offset], probabilities=[[[1.]] * 4] * 2)
    source = np.zeros((5, 5))
    source[2, 2] = 12
    result = collection.transport(source, 500, data)
    assert result["arrays"]["collected"][expected] == 12
    assert np.count_nonzero(result["arrays"]["collected"]) == 1
    assert result["diagnostics"]["conservation_residual"] == 0


def test_tiny_sensor_all_off_sensor_or_all_intrinsic_loss_remains_accounted():
    data = model()
    data.update(destination_offsets_rc=[[4, -4]], probabilities=[[[1.]] * 4] * 2)
    source = np.array([[10.]])
    result = collection.transport(source, 500, data)["arrays"]
    assert result["off_sensor_loss"][0, 0] == 10
    assert result["collected"][0, 0] == result["intrinsic_loss"][0, 0] == 0
    data["probabilities"] = [[[0.]] * 4] * 2
    result = collection.transport(source, 500, data)["arrays"]
    assert result["intrinsic_loss"][0, 0] == 10
    assert result["collected"][0, 0] == result["off_sensor_loss"][0, 0] == 0


@pytest.mark.parametrize("change", [
    {"schema": "camera_charge_collection_v2"}, {"extra": 1},
    {"site_index_tile": [[False, 1], [2, 3]]},
    {"pixel_pitch_um": True}, {"pixel_pitch_um": np.float64(2)},
    {"pixel_pitch_um": 10**1000}, {"pixel_pitch_um": float("nan")},
    {"wavelength_nm": [450]}, {"wavelength_nm": [450, True]},
    {"wavelength_nm": [650, 450]}, {"wavelength_nm": [450, 450]},
    {"wavelength_nm": [450, float("inf")]},
    {"destination_offsets_rc": []}, {"destination_offsets_rc": [[0, 0], [0, 0]]},
    {"destination_offsets_rc": [[0, 5]]}, {"destination_offsets_rc": [[-5, 0]]},
    {"destination_offsets_rc": [[0, False]]}, {"destination_offsets_rc": [[0, 1.0]]},
    {"destination_offsets_rc": [[0, 0]] * 82},
    {"probabilities": []}, {"probabilities": [[], []]},
    {"probabilities": [[[.5]] * 4] * 2},
    {"probabilities": [[[True, 0]] * 4] * 2},
    {"probabilities": [[[float("nan"), 0]] * 4] * 2},
    {"probabilities": [[[-.1, .5]] * 4] * 2},
    {"probabilities": [[[.5, .5000000000000002]] * 4] * 2},
    {"boundary": "reflect"}, {"statistics": "convolve_noisy_samples"},
    {"temporal_model": "delayed"}, {"regime": "saturation_dependent"},
    {"angular_assumption": "unrestricted_transport"},
    {"source": {"kind": "verified", "description": "not valid"}},
    {"source": {"kind": "estimated", "description": " "}},
    {"source": {"kind": "estimated", "description": "x", "approved": True}},
])
def test_malformed_or_unsupported_collection_is_rejected_without_coercion(change):
    data = model()
    data.update(change)
    with pytest.raises(ValueError):
        collection.validate_collection(data)


@pytest.mark.parametrize("generated", [
    [[1]], np.ones((2, 2), dtype=bool), np.ones((2, 2), dtype=complex),
    np.ones((2, 2), dtype=object), np.ones((2,)), np.ones((2, 2, 1)),
    np.ones((0, 2)), np.full((2, 2), -1.), np.full((2, 2), float("inf")),
    np.full((2, 2), float("nan")), np.full((2, 2), 1e13 + 1),
])
def test_transport_refuses_invalid_or_non_mean_arrays(generated):
    with pytest.raises(ValueError):
        collection.transport(generated, 500, model())


@pytest.mark.parametrize("wave", [True, np.bool_(True), 449.999, 650.001,
                                  float("nan"), float("inf"), "500", [500], 10**1000])
def test_transport_refuses_extrapolation_nonfinite_and_boolean_wavelength(wave):
    with pytest.raises(ValueError):
        collection.transport(np.ones((2, 2)), wave, model())


def test_resource_bound_is_checked_before_image_work_and_output_is_deterministic(monkeypatch):
    monkeypatch.setattr(collection, "MAX_PIXELS", 3)
    with pytest.raises(ValueError, match="at most"):
        collection.transport(np.ones((2, 2)), 500, model())
    monkeypatch.setattr(collection, "MAX_PIXELS", 4)
    first = collection.transport(np.ones((2, 2)), 500, model())
    second = collection.transport(np.ones((2, 2)), 500, model())
    assert first["diagnostics"] == second["diagnostics"]
    for key in first["arrays"]:
        np.testing.assert_array_equal(first["arrays"][key], second["arrays"][key])


def test_routed_poisson_readout_keeps_marginal_variance_without_false_spatial_covariance():
    # Isolated R source pairs provide independent repetitions of one carrier
    # stream split 3:1. Routing sampled source charge instead would give variances
    # 56.25 and 6.25, plus covariance 18.75, instead of 75, 25 and zero.
    data = model()
    source = np.zeros((64, 64))
    source[::2, ::2] = 100
    means = collection.transport(source, 550, data)["arrays"]["collected"]
    electrical_model = electrical.default_electrical_model({
        "full_well_e": 65535., "read_noise_e": 0., "dark_current_e_per_s": 0.,
        "gain": 1., "bit_depth": 16, "black_level_dn": 0.,
    })
    geometry = {"full_sensor_shape": [64, 64], "crop_origin": [0, 0]}
    pairs = []
    for seed in range(48):
        raw = electrical.capture_electrons(
            means, 1., electrical_model, analog_gain=1., temperature_c=25.,
            geometry=geometry, noise_enabled=True, seed=seed,
        )["arrays"]["raw_dn"]
        pairs.append(np.column_stack((raw[::2, ::2].ravel(), raw[::2, 1::2].ravel())))
    pairs = np.concatenate(pairs)
    np.testing.assert_allclose(pairs.mean(axis=0), [75, 25], atol=.2, rtol=0)
    np.testing.assert_allclose(pairs.var(axis=0, ddof=1), [75, 25], rtol=.025)
    assert abs(np.cov(pairs, rowvar=False)[0, 1]) < .7

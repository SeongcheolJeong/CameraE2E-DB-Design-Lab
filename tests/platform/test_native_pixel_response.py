"""Analytic four-site response, periodic angles, native crops and strict bounds."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import native_pixel_response as pixel


def triangle(phi):
    p = np.mod(phi, 360)
    return np.where(p < 90, p / 90, np.where(p < 270, 2 - p / 90, p / 90 - 4))


def analytic(wave, theta, phi):
    wave, theta, phi = np.broadcast_arrays(wave, theta, phi)
    normal = np.array([0.15, 0.25, 0.45, 0.55]) + (
        (wave[..., None] - 500) / 100 * np.array([0.1, 0.05, -0.1, 0.1])
    )
    return normal * (1 + theta[..., None] / 40 * 0.2 * triangle(phi)[..., None])


def response():
    wave, theta, phi = [500, 550, 600], [0, 20, 40], [0, 90, 180, 270]
    return {
        "schema": "camera_native_pixel_response_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 2.0,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": wave,
        "theta_deg": theta,
        "phi_deg": phi,
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": analytic(
            np.array(wave)[:, None, None],
            np.array(theta)[None, :, None],
            np.array(phi)[None, None, :],
        ).tolist(),
        "source": {"kind": "simulated", "description": "독립 사이트 합성 시험; 실측 아님"},
    }


def test_normalization_is_detached_finite_json_and_preserves_four_distinct_sites():
    source = response()
    original = deepcopy(source)
    result = pixel.validate_response(source)
    assert source == original and result == original
    assert type(result["wavelength_nm"][0]) is float
    assert type(result["values"][0][0][0][0]) is float
    assert pixel.validate_response(result) == result
    assert json.loads(json.dumps(result, allow_nan=False, ensure_ascii=False)) == result
    result["site_names"][0] = "changed"
    result["values"][0][0][0][0] = 0
    result["source"]["description"] = "changed"
    assert source == original


def test_broadcast_trilinear_response_matches_closed_form_with_all_cross_terms():
    wave = np.array([501, 536, 577, 600])[:, None, None]
    theta = np.array([0, 7, 23, 40])[None, :, None]
    phi = np.array([-721, -1, 0, 19, 90, 171, 220, 300, 359, 361])[None, None, :]
    actual = pixel.evaluate(response(), wave, theta, phi)
    assert actual.shape == (4, 4, 10, 4) and actual.dtype == np.float64
    np.testing.assert_allclose(actual, analytic(wave, theta, phi), rtol=2e-15, atol=1e-16)
    assert np.max(abs(actual[..., 1] - actual[..., 2])) > 0.1


def test_periodic_seam_normal_incidence_and_large_integer_angles():
    data = response()
    angles = np.array([359, -1, 719, -361, 2**53 + 1], dtype=np.int64)
    actual = pixel.evaluate(data, 550, 40, angles)
    np.testing.assert_allclose(actual[:4], np.broadcast_to(actual[0], (4, 4)), atol=1e-16)
    np.testing.assert_allclose(actual[-1], analytic(550, 40, (2**53 + 1) % 360), atol=1e-16)
    normal = pixel.evaluate(data, 550, 0, [-359, 33, 270, 10000])
    np.testing.assert_allclose(normal, np.broadcast_to(analytic(550, 0, 0), (4, 4)), atol=1e-16)
    np.testing.assert_allclose(
        pixel.evaluate(data, 550, 40, 0),
        (pixel.evaluate(data, 550, 40, 1) + pixel.evaluate(data, 550, 40, 359)) / 2,
        atol=1e-16,
    )


def test_zero_normal_response_is_valid_without_dividing_by_normal_qe():
    data = response()
    for plane in data["values"]:
        for sites in plane[0]:
            sites[0] = 0.0
    assert pixel.evaluate(data, 550, 0, 90)[0] == 0
    assert pixel.evaluate(data, 550, 10, 90)[0] == pytest.approx(
        pixel.evaluate(data, 550, 20, 90)[0] / 2
    )


def test_normal_only_model_is_phi_invariant_and_refuses_any_oblique_query():
    data = response()
    data.update(theta_deg=[0], phi_deg=[0], values=[[[row[0][0]]] for row in data["values"]])
    assert np.shape(data["values"]) == (3, 1, 1, 4)
    np.testing.assert_allclose(
        pixel.evaluate(data, 575, 0, [-180, 11, 999]),
        np.broadcast_to(analytic(575, 0, 0), (3, 4)),
        atol=1e-16,
    )
    with pytest.raises(ValueError, match="no extrapolation"):
        pixel.evaluate(data, 575, 1e-12, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "camera_native_pixel_response_v2"},
        {"response_basis": "whole_2x2_incident_cell"},
        {"response_basis": "relative_to_normal_qe"},
        {"reference_plane": "silicon_after_microlens"},
        {"direction_convention": "toward_exit_pupil"},
        {"polarization": "s"},
        {"extra": 1},
        {"site_names": ["R", "G", "G", "B"]},
        {"site_index_tile": [[0, 1], [1, 2]]},
        {"site_index_tile": [[False, 1], [2, 3]]},
        {"pixel_pitch_um": True},
        {"pixel_pitch_um": 0},
        {"pixel_pitch_um": 10**1000},
        {"wavelength_nm": [500]},
        {"wavelength_nm": [500, 500, 600]},
        {"wavelength_nm": [500, True, 600]},
        {"wavelength_nm": [500, 550, float("nan")]},
        {"theta_deg": [1, 20, 40]},
        {"theta_deg": [0, 20, 90]},
        {"phi_deg": [0, 90, 180, 360]},
        {"phi_deg": [0, 45, 90, 180]},
        {"phi_deg": [0]},
        {"phi_deg": [0, 180]},
        {"interpolation": "nearest"},
        {"values_axis_order": ["site", "wavelength", "theta", "phi"]},
        {"source": {"kind": "simulated", "description": " "}},
        {"source": {"kind": "validated", "description": "claim"}},
        {"source": {"kind": "simulated", "description": "test", "approved": True}},
    ],
)
def test_unsupported_contracts_fail_before_response_evaluation(change):
    data = response()
    data.update(change)
    with pytest.raises(ValueError):
        pixel.validate_response(data)


@pytest.mark.parametrize("value", [-0.01, 1.01, True, float("inf"), "0.1", None])
def test_absolute_site_response_has_strict_passive_bounds(value):
    data = response()
    data["values"][0][1][1][0] = value
    with pytest.raises(ValueError):
        pixel.validate_response(data)


def test_malformed_shape_normal_phi_dependence_and_oversize_lut_are_rejected():
    data = response()
    data["values"][0][0][0].append(0.2)
    with pytest.raises(ValueError, match="exact shape"):
        pixel.validate_response(data)
    data = response()
    data["values"][0][0][1][1] += 1e-10
    with pytest.raises(ValueError, match="Normal-incidence"):
        pixel.validate_response(data)
    data = response()
    # The cap is checked using axes before attempting to inspect/allocate values.
    data.update(
        wavelength_nm=np.linspace(400, 700, 256).tolist(),
        theta_deg=np.linspace(0, 60, 91).tolist(),
        phi_deg=np.arange(0, 360, 2.5).tolist(),
        values=None,
    )
    with pytest.raises(ValueError, match="262144 scalar"):
        pixel.validate_response(data)


@pytest.mark.parametrize(
    "wave,theta,phi",
    [
        (499.999, 0, 0),
        (600.001, 0, 0),
        (550, -0.001, 0),
        (550, 40.001, 0),
        (True, 0, 0),
        ([550, True], 0, 0),
        (550, [0, False], 0),
        (550, 0, float("nan")),
        (550, 0, complex(1, 0)),
        ("550", 0, 0),
        (np.array([550], dtype=object), 0, 0),
    ],
)
def test_query_domain_and_types_refuse_silent_coercion_or_extrapolation(wave, theta, phi):
    with pytest.raises(ValueError):
        pixel.evaluate(response(), wave, theta, phi)


def test_broadcast_budget_precedes_output_allocation_and_mismatched_shapes_refuse(monkeypatch):
    def no_zeros(*args, **kwargs):
        pytest.fail("Oversized query reached interpolation allocation")

    monkeypatch.setattr(pixel.np, "zeros", no_zeros)
    with pytest.raises(ValueError, match="Broadcast query exceeds"):
        pixel.evaluate(
            response(), np.full((513, 1), 550), np.zeros_like(np.arange(513))[None, :], 0
        )
    with pytest.raises(ValueError, match="cannot broadcast"):
        pixel.evaluate(response(), [500, 550], [0, 10, 20], 0)


@pytest.mark.parametrize("origin", [(0, 0), (0, 1), (1, 0), (1, 1), (9, 11)])
def test_native_site_mapping_matches_full_sensor_crops_for_every_bayer_phase(origin):
    full = pixel.site_index((20, 20), (0, 0))
    actual = pixel.site_index((5, 7), origin)
    np.testing.assert_array_equal(
        actual, full[origin[0] : origin[0] + 5, origin[1] : origin[1] + 7]
    )
    assert actual.dtype == np.uint8 and set(actual.ravel()) == {0, 1, 2, 3}
    qe = pixel.evaluate(response(), 550, 20, 90)
    # Native Gr and Gb remain distinct when selecting the scalar rate/charge.
    charge = 1000 * qe[actual]
    assert charge[actual == 1].mean() != charge[actual == 2].mean()


def test_large_native_origin_preserves_integer_parity_without_large_sensor_allocation():
    origin = (pixel.MAX_NATIVE_COORDINATE, pixel.MAX_NATIVE_COORDINATE - 1)
    np.testing.assert_array_equal(pixel.site_index((2, 2), origin), [[2, 3], [0, 1]])


@pytest.mark.parametrize(
    "shape,origin",
    [
        ((0, 1), (0, 0)),
        ((2.0, 2), (0, 0)),
        ((True, 2), (0, 0)),
        ((2, 2), (-1, 0)),
        ((2, 2), (0, False)),
        ((2, 2), (0, 0.5)),
        ((2, 2), (2**53, 0)),
        ((1025, 1024), (0, 0)),
    ],
)
def test_invalid_native_geometry_fails_before_site_allocation(shape, origin, monkeypatch):
    monkeypatch.setattr(
        pixel.np, "arange", lambda *a, **k: pytest.fail("Invalid geometry allocated")
    )
    with pytest.raises(ValueError):
        pixel.site_index(shape, origin)

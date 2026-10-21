"""Finite object PSF qualification retains the established scalar validity gates."""

import time
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import finite_conjugate, prescription_psf
from camera_platform.pupil_transmission import digest
from camera_platform.spatial import validate_psf


def wavefront(request, *, x=0.3, y=0.4, wi=0, count=32):
    return prescription_psf._wavefront(
        request, prescription_psf._model(request, wi),
        request["prescription"]["wavelength_nm"][wi], x, y, count, time.monotonic() + 30,
    )


@pytest.fixture(scope="module")
def characterized():
    request = prescription_psf.request_template(5)
    return request, prescription_psf.characterize(request)


def test_finite_fixture_qualifies_with_all_existing_unrelaxed_gates(characterized):
    request, result = characterized
    original = deepcopy(request)
    previous = prescription_psf.request_template(4)
    for name in (
        "max_kernel_l1_change", "minimum_retained_energy",
        "maximum_exit_pupil_mapping_fraction", "maximum_ray_cone_deg",
        "pupil_samples", "kernel_size", "subpixel_samples",
    ):
        assert request[name] == previous[name]
    assert request == original
    summary = result["summary"]
    assert summary["status"] == "PROPOSED"
    assert summary["numerical_agreement"] and summary["approximation_domain_checks"]
    assert summary["schema"] == "prescription_field_psf_characterization_v5"
    assert summary["factory_id"] == summary["method_and_version"] == (
        prescription_psf.FINITE_FACTORY_ID
    )
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert summary["prescription_sha256"] == digest(
        prescription_psf.validate_request(request)["prescription"]
    )
    assert summary["object_conjugate"] == request["prescription"]["object_conjugate"]
    assert summary["field_mapping"]["reference_focal_length_role"] == (
        "reference_f_number_only_not_field_mapping"
    )
    assert summary["maximum_object_origin_residual_mm_observed"] <= 1e-9
    assert summary["maximum_neighbor_opd_waves_observed"] <= 0.25
    assert "additional downstream defocus" in " ".join(summary["limitations"])
    candidate = validate_psf(result["model_candidate"])
    np.testing.assert_allclose(np.sum(candidate["values"], axis=(-1, -2)), 1, atol=1e-14)
    assert result["arrays"]["fine_opd_nm"].shape == (2, 3, 3, 64, 64)


@pytest.mark.parametrize("mutate", ["wrong_outer", "wrong_inner", "no_conjugate", "positive_m"])
def test_finite_and_infinite_contracts_cannot_be_mixed(mutate):
    request = prescription_psf.request_template(5)
    if mutate == "wrong_outer":
        request["object_conjugate"] = "infinity_plane_wave"
    elif mutate == "wrong_inner":
        request["prescription"]["schema"] = "lens_prescription_v4"
    elif mutate == "no_conjugate":
        del request["prescription"]["object_conjugate"]
    else:
        request["prescription"]["object_conjugate"]["reference_magnification"] *= -1
    with pytest.raises(ValueError):
        prescription_psf.validate_request(request)


def test_all_wavelengths_share_one_physical_object_chart_without_color_recentering(characterized):
    request, result = characterized
    arrays = result["arrays"]
    yy, xx = np.meshgrid(request["field_y_mm"], request["field_x_mm"], indexing="ij")
    expected = finite_conjugate.object_xy_mm(
        np.stack([xx, yy], axis=-1), request["prescription"]["object_conjugate"]
    )
    for label in ("coarse", "fine"):
        np.testing.assert_array_equal(arrays[label + "_object_origin_xy_mm"][0], expected)
        np.testing.assert_array_equal(arrays[label + "_object_origin_xy_mm"][1], expected)
        # The fixed chart is intentionally distinct from actual wavelength-dependent landings.
        assert np.max(np.abs(np.diff(arrays[label + "_chief_sensor_xy_mm"], axis=0))) > 1e-4
        np.testing.assert_array_equal(
            arrays[label + "_reference_image_xy_mm"][0], np.stack([xx, yy], axis=-1)
        )


def test_reference_focal_length_never_substitutes_for_finite_object_mapping():
    request = prescription_psf.request_template(5)
    changed = deepcopy(request)
    changed["reference_focal_length_mm"] *= 2
    a, b = wavefront(request), wavefront(changed)
    assert a.keys() == b.keys()
    for key in a:
        if isinstance(a[key], np.ndarray):
            np.testing.assert_array_equal(a[key], b[key])
        else:
            assert a[key] == b[key]


def test_changing_object_distance_at_fixed_sensor_changes_traced_opd_rays_and_psf():
    request = prescription_psf.request_template(5)
    changed = deepcopy(request)
    changed["prescription"]["object_conjugate"]["distance_mm"] = 700
    a, b = wavefront(request), wavefront(changed)
    assert request["prescription"]["surfaces"] == changed["prescription"]["surfaces"]
    np.testing.assert_array_equal(a["object_origin_xy_mm"], b["object_origin_xy_mm"])
    assert np.max(np.abs(a["ray_sensor_xy_mm"] - b["ray_sensor_xy_mm"])) > 0.05
    common = a["admitted"] & b["admitted"]
    assert np.max(np.abs(a["opd_nm"][common] - b["opd_nm"][common])) > 100
    ka = prescription_psf._propagate(request, a, 500, 7)
    kb = prescription_psf._propagate(changed, b, 500, 7)
    assert np.abs(ka / ka.sum() - kb / kb.sum()).sum() > 0.1


def test_strong_defocus_refuses_support_and_sampling_without_autofocus():
    request = prescription_psf.request_template(5)
    request.update(field_x_mm=[0, 0.02], field_y_mm=[0, 0.02])
    request["prescription"]["object_conjugate"]["distance_mm"] = 100
    before = deepcopy(request)
    result = prescription_psf.characterize(request)
    assert request == before
    assert result["summary"]["status"] == "REFUSED"
    assert result["model_candidate"] is None
    assert not result["summary"]["numerical_agreement"]
    assert result["summary"]["minimum_retained_energy_observed"] < 0.95
    assert result["summary"]["maximum_neighbor_opd_waves_observed"] > 0.25


@pytest.mark.parametrize("distance,gap,magnification", [
    (100., 23.545291501119127, -0.24074267560313928),
    (2000., 18.97761852542692, -0.009985645482108917),
])
def test_near_and_far_authored_sensor_planes_can_qualify(distance, gap, magnification):
    # Distinct authored fixtures from independent surface ABCD; no autofocus
    # calculation or tolerance change occurs inside the production factory.
    request = prescription_psf.request_template(5)
    request.update(field_x_mm=[0, 0.3], field_y_mm=[0, 0.4])
    request["prescription"]["surfaces"][-1]["thickness_mm"] = gap
    request["prescription"]["object_conjugate"].update(
        distance_mm=distance, reference_magnification=magnification
    )
    before = deepcopy(request)
    result = prescription_psf.characterize(request)
    assert request == before
    assert result["summary"]["status"] == "PROPOSED"
    assert result["summary"]["maximum_object_origin_residual_mm_observed"] <= 1e-9


def test_finite_even_asphere_changes_real_trace_without_altering_paraxial_power():
    request = prescription_psf.request_template(5)
    changed = deepcopy(request)
    front = prescription_psf.request_template(4)["prescription"]["surfaces"][1]
    changed["prescription"]["surfaces"][1] = deepcopy(front)
    prescription_psf.validate_request(changed)
    a, b = wavefront(request), wavefront(changed)
    common = a["admitted"] & b["admitted"]
    assert np.max(np.abs(a["opd_nm"][common] - b["opd_nm"][common])) > 10
    assert prescription_psf._model(request, 0)["ar"]["parax_data"].fod.efl == (
        prescription_psf._model(changed, 0)["ar"]["parax_data"].fod.efl
    )


@pytest.mark.parametrize("corruption", ["origin", "stop", "backwards"])
def test_invalid_finite_chief_never_becomes_opd_evidence(monkeypatch, corruption):
    request = prescription_psf.request_template(5)
    model = prescription_psf._model(request, 0)
    original = prescription_psf._finite_reference

    def corrupt(*args, **kwargs):
        field, reference, chief = original(*args, **kwargs)
        if corruption == "origin":
            chief[0].ray[0][0][1] += 0.01
        elif corruption == "stop":
            chief[0].ray[model.seq_model.stop_surface][0][0] += 0.01
        else:
            chief[0].ray[0] = chief[0].ray[0]._replace(dst=-1.)
        return field, reference, chief

    monkeypatch.setattr(prescription_psf, "_finite_reference", corrupt)
    with pytest.raises(ValueError):
        prescription_psf._wavefront(request, model, 500, 0, 0, 16, time.monotonic() + 20)

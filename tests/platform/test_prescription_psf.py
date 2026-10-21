"""Independent optical path, geometric sign and continuous diffraction evidence."""

import time
from copy import deepcopy

import numpy as np
import pytest
from numpy.polynomial.legendre import leggauss
from scipy.special import j1

from camera_platform import prescription_psf as factory


def deadline():
    return time.monotonic() + 20


@pytest.fixture(scope="module")
def characterized():
    request = factory.request_template()
    return request, factory.characterize(request)


def test_real_prescription_produces_field_payload_and_preserves_unverified_status(characterized):
    request, result = characterized
    summary, arrays, candidate = result["summary"], result["arrays"], result["model_candidate"]
    assert summary["status"] == "PROPOSED"
    assert candidate["schema"] == "field_intensity_psf_v1"
    assert np.shape(candidate["values"]) == (2, 3, 3, 41, 41)
    np.testing.assert_allclose(np.sum(candidate["values"], axis=(-1, -2)), 1, atol=1e-14)
    assert arrays["fine_opd_nm"].shape == (2, 3, 3, 64, 64)
    assert arrays["fine_admitted"].dtype == bool
    assert np.max(np.abs(arrays["fine_opd_nm"])) > 1
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert summary["reference_wavelength_nm"] == 600
    assert summary["geometric_reference_f_number"] == pytest.approx(
        request["reference_focal_length_mm"]
    )
    assert "transport is not solved" in " ".join(summary["assumptions"])
    assert set(candidate["source"]) == {"kind", "description"}


def test_unmodified_template_passes_both_factories_with_same_physical_prescription(characterized):
    from camera_platform import lens_factory

    request, psf = characterized
    original = deepcopy(request)
    radiometry = lens_factory.characterize(request["prescription"])
    assert request == original
    assert radiometry["summary"]["status"] == psf["summary"]["status"] == "PROPOSED"
    assert (
        radiometry["summary"]["normalized_request"]
        == psf["summary"]["normalized_request"]["prescription"]
    )
    convergence = radiometry["summary"]["convergence"]
    assert np.max(convergence["relative_illumination_absolute_change"]) < 0.005
    assert np.max(convergence["angular_cdf_max_absolute_change"]) < 0.008
    # PSF corner source radius must fit the shared RI's physical field coverage.
    corner = np.hypot(
        max(abs(v) for v in request["field_x_mm"]), max(abs(v) for v in request["field_y_mm"])
    )
    assert corner < request["prescription"]["image_height_mm"][-1]


def test_opd_matches_direct_physical_path_to_reference_sphere():
    """OPL sum and analytic sphere intersections, independent of Hopkins EIC OPD."""
    from rayoptics.raytr import trace, waveabr
    from rayoptics.raytr.opticalspec import Field

    request = factory.request_template()
    model = factory._model(request, 0)
    field = Field(y=0.1, fov=model.optical_spec["fov"])
    sphere, chief = trace.setup_pupil_coords(model, field, 500, 0.0, image_pt=[0.0, 0.04])
    surfaces = request["prescription"]["surfaces"]
    center = np.array([0.0, 0.04, surfaces[-1]["thickness_mm"]])
    indexes = [surface["refractive_index_after"][0] for surface in surfaces]

    def physical_path(ray):
        point, direction = ray[-2][:2]
        v = point - center
        # Sphere's near-side intersection; negative travel back-projects the
        # exiting wave in homogeneous air and is not a new material traversal.
        distance = -direction @ v - np.sqrt((direction @ v) ** 2 - v @ v + sphere[2] ** 2)
        incident_plane_phase = ray[0][1] @ ray[1][0]
        return (
            incident_plane_phase
            + sum(indexes[i] * ray[1 + i][2] for i in range(len(indexes) - 1))
            + distance
        )

    for pupil in ([0.0, 0.0], [0.4, 0.3], [0.0, 0.8], [-0.4, 0.3]):
        ray = trace.trace_safe(model, pupil, field, 500, None, "full", check_apertures=True).pkg
        expected_nm = (physical_path(chief[0].ray) - physical_path(ray[0])) * 1e6
        actual_nm = (
            waveabr.wave_abr_full_calc(
                model["ar"]["parax_data"].fod, field, 500, 0.0, ray, chief, sphere
            )
            * 1e6
        )
        assert actual_nm == pytest.approx(expected_nm, abs=0.002)


def centroid_um(mass, request):
    coordinate = (np.arange(request["kernel_size"]) - request["kernel_size"] // 2) * request[
        "pixel_pitch_um"
    ]
    return (
        np.array([(mass.sum(0) * coordinate).sum(), (mass.sum(1) * coordinate).sum()]) / mass.sum()
    )


def test_signed_psf_displacement_matches_geometric_rays_and_rotates_fields():
    request = factory.request_template()
    request["reference_focal_length_mm"] = 21.0
    model = factory._model(request, 0)
    xfield = factory._wavefront(request, model, 500, 0.2, 0.0, 64, deadline())
    yfield = factory._wavefront(request, model, 500, 0.0, 0.2, 64, deadline())
    mx = factory._propagate(request, xfield, 500, 7)
    my = factory._propagate(request, yfield, 500, 7)
    geometric = (xfield["ray_sensor_xy_mm"][xfield["admitted"]].mean(0) - [0.2, 0]) * 1000
    assert geometric[0] < -10  # Explicit mismatch in ideal versus actual focal map.
    np.testing.assert_allclose(centroid_um(mx, request), geometric, atol=0.25)
    np.testing.assert_allclose(mx, my.T, atol=2e-8)
    assert abs(centroid_um(my, request)[0]) < 1e-6
    # Recentring each chief would destroy this displacement.
    assert abs(centroid_um(my, request)[1]) > 10


def test_zero_phase_and_known_linear_phase_match_continuous_airy_bin_integrals():
    """Analytic Bessel intensity integrated with independent Gauss quadrature."""
    request = factory.request_template()
    n = 256
    coordinate = (np.arange(n) - n // 2) * 1.2 / n
    x, y = np.meshgrid(coordinate, coordinate)
    aperture = x * x + y * y <= 0.5**2
    nodes, weights = leggauss(24)
    centers = (np.arange(41) - 20) * 3.0
    image = centers[:, None] + nodes[None, :] * 1.5
    area_weights = weights[None, None, :, None] * weights[None, None, None, :] * 1.5**2
    for shift in (0.0, 9.0):
        wf = {
            "admitted": aperture,
            "opd_nm": -x * (shift * 1e-3) / 20.0 * 1e6,
            "exit_pupil_radius_mm": 0.5,
            "reference_sphere_radius_mm": 20.0,
        }
        mass = factory._propagate(request, wf, 500, 7)
        r = np.hypot(image[:, None, :, None], image[None, :, None, :] - shift)
        z = np.pi * r / 10.0  # lambda * F = 0.5 um * 20.
        amplitude = np.divide(2 * j1(z), z, out=np.ones_like(z), where=z != 0)
        exact = (amplitude**2 * area_weights).sum(axis=(-1, -2)) * np.pi / 400.0
        assert np.abs(mass - exact).sum() < 0.002
        assert abs(mass.sum() - exact.sum()) < 0.0003
        assert abs(centroid_um(mass, request)[0] - centroid_um(exact, request)[0]) < 0.02


def test_off_axis_general_azimuth_rotation_and_opposite_field_reflection():
    request = factory.request_template()
    model = factory._model(request, 0)
    results = []
    for x, y in ((0.3, 0.4), (0.4, 0.3), (-0.3, -0.4)):
        wf = factory._wavefront(request, model, 500, x, y, 64, deadline())
        psf = factory._propagate(request, wf, 500, 7)
        geometric = (wf["ray_sensor_xy_mm"][wf["admitted"]].mean(0) - [x, y]) * 1000
        np.testing.assert_allclose(centroid_um(psf, request), geometric, atol=0.01)
        results.append(psf)
    np.testing.assert_allclose(results[0], results[1].T, atol=1e-12)
    np.testing.assert_allclose(results[0], results[2][::-1, ::-1], atol=1e-12)


def test_axial_symmetry_and_physical_surface_and_spectral_changes():
    request = factory.request_template()
    model = factory._model(request, 0)
    original = factory._wavefront(request, model, 500, 0.0, 0.0, 32, deadline())
    np.testing.assert_allclose(original["opd_nm"], original["opd_nm"].T, atol=1e-8)
    np.testing.assert_array_equal(original["admitted"], original["admitted"].T)
    altered = deepcopy(request)
    altered["prescription"]["surfaces"][-1]["thickness_mm"] += 0.5
    moved = factory._wavefront(altered, factory._model(altered, 0), 500, 0.0, 0.0, 32, deadline())
    spectral = factory._wavefront(
        request, factory._model(request, 1), 600, 0.0, 0.0, 32, deadline()
    )
    assert np.max(np.abs(original["opd_nm"] - moved["opd_nm"])) > 100
    assert np.max(np.abs(original["opd_nm"] - spectral["opd_nm"])) > 10
    assert model.seq_model.gaps[-1].thi == request["prescription"]["surfaces"][-1]["thickness_mm"]
    assert [s.max_aperture for s in model.seq_model.ifcs[1:-1]] == [0.5, 0.8, 0.8]


def test_physical_rear_aperture_clips_the_wavefront_without_resizing():
    request = factory.request_template()
    ordinary = factory._wavefront(
        request, factory._model(request, 0), 500, 0.0, 0.0, 32, deadline()
    )
    request["prescription"]["surfaces"][-1]["semi_diameter_mm"] = 0.25
    model = factory._model(request, 0)
    clipped = factory._wavefront(request, model, 500, 0.0, 0.0, 32, deadline())
    assert clipped["admitted"].sum() < 0.4 * ordinary["admitted"].sum()
    assert (
        clipped["failure_counts"]["TraceRayBlockedError"]
        > ordinary["failure_counts"]["TraceRayBlockedError"]
    )
    assert model.seq_model.ifcs[-2].max_aperture == 0.25
    assert np.all(clipped["opd_nm"][~clipped["admitted"]] == 0)


@pytest.mark.parametrize(
    "change",
    [
        {"object_conjugate": "finite"},
        {"pupil_samples": 17},
        {"field_x_mm": [False, 0.1]},
        {"field_y_mm": [0.0, float("nan")]},
        {"field_x_mm": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]},
        {"maximum_ray_cone_deg": 11},
        {"maximum_exit_pupil_mapping_fraction": 0.1},
    ],
)
def test_unsupported_or_unbounded_requests_rejected(change):
    request = factory.request_template()
    request.update(change)
    with pytest.raises(ValueError):
        factory.validate_request(request)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_kernel_l1_change", 1e-6),
        ("maximum_exit_pupil_mapping_fraction", 1e-6),
        ("kernel_size", 7),
    ],
)
def test_failed_numeric_transport_domain_or_tail_gate_withholds_candidate(key, value):
    request = factory.request_template()
    request.update(field_x_mm=[0.0, 0.02], field_y_mm=[0.0, 0.02])
    request[key] = value
    result = factory.characterize(request)
    assert result["summary"]["status"] == "REFUSED"
    assert result["model_candidate"] is None
    assert np.isfinite(result["arrays"]["psf_bin_energy"]).all()


def test_zero_received_energy_refused_without_nan(monkeypatch):
    request = factory.request_template()
    request.update(field_x_mm=[0.0, 0.02], field_y_mm=[0.0, 0.02], pupil_samples=16)
    monkeypatch.setattr(
        factory, "_propagate", lambda data, *args: np.zeros((data["kernel_size"],) * 2)
    )
    result = factory.characterize(request)
    assert result["model_candidate"] is None
    assert result["summary"]["minimum_retained_energy_observed"] == 0
    assert np.isfinite(result["arrays"]["psf_kernel"]).all()

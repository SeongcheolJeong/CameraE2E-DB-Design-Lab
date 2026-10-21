"""Independent Airy energy integrals and phase-preserving declared-pupil PSF checks."""

from copy import deepcopy

import numpy as np
import pytest
from numpy.polynomial.legendre import leggauss
from scipy.special import j0, j1

from camera_platform.psf_factory import characterize, request_template, validate_request


def airy_request():
    request = request_template()
    request.update(
        wavelength_nm=[500, 600],
        pixel_pitch_um=2.0,
        subpixel_samples=3,
        opd_nm={"defocus": 0.0, "astigmatism": 0.0, "coma_x": 0.0},
    )
    return request


def airy_pixel_integrals(request, order=24):
    """Continuous unit-energy Airy intensity integrated by Gauss-Legendre quadrature.

    Independent of Prysm, discrete pupils, Fourier transforms, and factory binning.
    I(r)=pi/(4*(lambda*F)^2)*(2*J1(z)/z)^2, z=pi*r/(lambda*F).
    Its integral over the infinite receiving plane is one.
    """
    size, pitch = request["kernel_size"], request["pixel_pitch_um"]
    nodes, weights = leggauss(order)
    centers = (np.arange(size) - size // 2) * pitch
    coordinate = centers[:, None] + nodes[None, :] * pitch / 2
    radius = np.hypot(coordinate[:, None, :, None], coordinate[None, :, None, :])
    area_weights = weights[None, None, :, None] * weights[None, None, None, :] * (pitch / 2) ** 2
    integrals = []
    for wavelength in request["wavelength_nm"]:
        scale = wavelength * 1e-3 * request["f_number"]
        z = np.pi * radius / scale
        amplitude = np.ones_like(z)
        np.divide(2 * j1(z), z, out=amplitude, where=z != 0)
        integrals.append((amplitude**2 * area_weights).sum(axis=(-1, -2)) * np.pi / (4 * scale**2))
    return np.asarray(integrals)


@pytest.fixture(scope="module")
def diffraction():
    request = airy_request()
    return request, characterize(request), airy_pixel_integrals(request)


def test_zero_opd_psf_matches_independent_absolute_airy_pixel_energy(diffraction):
    request, result, exact = diffraction
    assert result["summary"]["status"] == "PROPOSED"
    assert result["model_candidate"] is not None
    mass = result["arrays"]["psf_bin_energy"][-1]
    # Absolute bin fractions expose missing pixel area or transform normalization.
    assert np.max(np.abs(mass.sum(axis=(1, 2)) - exact.sum(axis=(1, 2)))) < 5e-5
    assert np.max(np.abs(mass - exact).sum(axis=(1, 2))) < 0.0023
    exact_kernel = exact / exact.sum(axis=(1, 2), keepdims=True)
    assert np.max(np.abs(result["arrays"]["psf_kernel"] - exact_kernel).sum(axis=(1, 2))) < 0.0024
    assert request["pixel_pitch_um"] == result["model_candidate"]["sample_pitch_um"]


def test_airy_quadrature_and_finite_square_window_have_independent_energy_bounds(diffraction):
    request, result, exact = diffraction
    np.testing.assert_allclose(airy_pixel_integrals(request, order=36), exact, atol=2e-15, rtol=0)
    half_width = request["kernel_size"] * request["pixel_pitch_um"] / 2
    for index, wavelength in enumerate(request["wavelength_nm"]):
        scale = wavelength * 1e-3 * request["f_number"]
        # A square contains its inscribed disk and lies within its circumscribed disk.
        z = np.pi * np.array([half_width, half_width * np.sqrt(2)]) / scale
        encircled = 1 - j0(z) ** 2 - j1(z) ** 2
        assert encircled[0] < exact[index].sum() < encircled[1] < 1
        assert (
            abs(result["arrays"]["retained_energy_fraction"][-1, index] - exact[index].sum()) < 5e-5
        )


def test_independent_pupil_and_receiver_refinements_reduce_analytic_error(diffraction):
    _, result, exact = diffraction
    mass = result["arrays"]["psf_bin_energy"]
    error = np.abs(mass - exact[None]).sum(axis=(-1, -2))
    assert np.all(error[1] < error[0])  # Pupil only.
    assert np.all(error[2] < error[0])  # Receiving quadrature only.
    assert np.all(error[3] < error[1])
    assert np.all(error[3] < error[2])
    assert result["summary"]["configurations"] == [
        {"pupil_samples": 128, "subpixel_samples": 3},
        {"pupil_samples": 256, "subpixel_samples": 3},
        {"pupil_samples": 128, "subpixel_samples": 7},
        {"pupil_samples": 256, "subpixel_samples": 7},
    ]
    assert result["summary"]["uncertainty"] == {"kind": "unknown"}


def test_unconverged_zero_opd_default_pitch_is_refused_instead_of_force_normalized():
    request = airy_request()
    request["pixel_pitch_um"] = 4.0
    result = characterize(request)
    assert result["summary"]["status"] == "REFUSED"
    assert result["summary"]["max_kernel_l1_change_observed"] > request["max_kernel_l1_change"]
    assert result["model_candidate"] is None
    assert np.isfinite(result["arrays"]["psf_bin_energy"]).all()
    # Explicitly improving quadrature is a new request; no acceptance-tolerance relaxation.
    request["subpixel_samples"] = 5
    refined = characterize(request)
    assert refined["summary"]["status"] == "PROPOSED"
    assert refined["summary"]["max_kernel_l1_change_observed"] <= request["max_kernel_l1_change"]


def test_too_small_window_and_tight_numeric_tolerance_withhold_candidate(diffraction):
    request, accepted, _ = diffraction
    small = {**request, "kernel_size": 7}
    result = characterize(small)
    assert result["summary"]["status"] == "REFUSED"
    assert result["arrays"]["retained_energy_fraction"].min() < small["minimum_retained_energy"]
    assert result["model_candidate"] is None
    # Restricting a larger result to the same physical bins retains the absolute flux scale.
    c = request["kernel_size"] // 2
    np.testing.assert_allclose(
        result["arrays"]["psf_bin_energy"],
        accepted["arrays"]["psf_bin_energy"][..., c - 3 : c + 4, c - 3 : c + 4],
        atol=2e-16,
        rtol=1e-14,
    )
    tight = characterize({**request, "max_kernel_l1_change": 1e-6})
    assert tight["model_candidate"] is None
    assert tight["summary"]["status"] == "REFUSED"


def test_kernel_is_explicitly_finite_support_normalized_not_an_absolute_throughput(diffraction):
    _, result, _ = diffraction
    mass, kernel = result["arrays"]["psf_bin_energy"][-1], result["arrays"]["psf_kernel"]
    retained = result["arrays"]["retained_energy_fraction"][-1]
    assert np.all((retained > 0.97) & (retained < 1))
    np.testing.assert_allclose(mass, kernel * retained[:, None, None], atol=3e-17, rtol=0)
    np.testing.assert_allclose(kernel.sum(axis=(-1, -2)), 1, atol=5e-16, rtol=0)
    candidate = result["model_candidate"]
    assert candidate["sampling_model"] == "pixel_integrated_optical_impulse"
    assert candidate["normalization"] == "unit_sum"
    assert "finite support renormalized" in candidate["source"]["description"].lower()
    assert result["summary"]["included_effects"] == ["lens_psf", "pixel_aperture"]


def test_psf_depends_on_f_number_not_pupil_diameter_alone(diffraction):
    request, baseline, _ = diffraction
    changed = characterize({**request, "pupil_diameter_mm": 8.0})
    np.testing.assert_array_equal(changed["arrays"]["psf_kernel"], baseline["arrays"]["psf_kernel"])
    slower = characterize({**request, "f_number": 12.0, "kernel_size": 41})
    center = slower["arrays"]["psf_kernel"].shape[-1] // 2
    assert np.all(
        slower["arrays"]["psf_kernel"][:, center, center]
        < baseline["arrays"]["psf_kernel"][:, 15, 15]
    )


def test_defocus_lowers_central_concentration_without_inventing_throughput_loss(diffraction):
    request, baseline, _ = diffraction
    changed = deepcopy(request)
    changed["opd_nm"]["defocus"] = 120
    defocused = characterize(changed)
    assert defocused["summary"]["status"] == "PROPOSED"
    assert np.all(
        defocused["arrays"]["psf_kernel"][:, 15, 15]
        < baseline["arrays"]["psf_kernel"][:, 15, 15] * 0.7
    )
    np.testing.assert_allclose(defocused["arrays"]["psf_kernel"].sum(axis=(-1, -2)), 1)
    assert defocused["summary"]["physical_validation"] == "not_established"
    assert defocused["summary"]["approval_status"] == "unapproved"


def test_astigmatism_sign_rotates_axes_at_fixed_defocus():
    request = airy_request()
    request.update(kernel_size=41, opd_nm={"defocus": 120, "astigmatism": 100, "coma_x": 0})
    first = characterize(request)
    request["opd_nm"]["astigmatism"] = -100
    other = characterize(request)
    a, b = first["arrays"]["psf_kernel"], other["arrays"]["psf_kernel"]
    assert np.abs(a - a.swapaxes(-1, -2)).sum() > 0.1
    np.testing.assert_allclose(a.swapaxes(-1, -2), b, atol=2e-16, rtol=1e-12)


def test_coma_reflection_and_complex_otf_keep_asymmetric_phase():
    request = airy_request()
    request["opd_nm"]["coma_x"] = 100
    positive = characterize(request)
    request["opd_nm"]["coma_x"] = -100
    negative = characterize(request)
    a, b = positive["arrays"], negative["arrays"]
    np.testing.assert_allclose(a["psf_kernel"][..., ::-1], b["psf_kernel"], atol=2e-16, rtol=1e-12)
    assert np.abs(a["psf_kernel"] - b["psf_kernel"]).sum() > 0.1
    assert np.max(np.abs(a["kernel_otf_imag"])) > 0.05
    np.testing.assert_allclose(a["kernel_otf_imag"], -b["kernel_otf_imag"], atol=2e-16, rtol=1e-12)
    # Independent direct sum at one nonzero frequency catches centering/sign errors.
    kernel = a["psf_kernel"][0]
    size = kernel.shape[0]
    coordinate = np.arange(size) - size // 2
    ky, kx = 2, 3
    expected = np.sum(
        kernel * np.exp(-2j * np.pi * (ky * coordinate[:, None] + kx * coordinate[None, :]) / size)
    )
    actual = (a["kernel_otf_real"] + 1j * a["kernel_otf_imag"])[0, size // 2 + ky, size // 2 + kx]
    assert abs(actual - expected) < 3e-16


def test_complex_otf_dc_reconstruction_and_determinism_preserve_original_request(diffraction):
    request, result, _ = diffraction
    before = deepcopy(request)
    again = characterize(request)
    assert request == before
    assert result["summary"] == again["summary"]
    for name, value in result["arrays"].items():
        assert np.isfinite(value).all()
        np.testing.assert_array_equal(value, again["arrays"][name])
    otf = result["arrays"]["kernel_otf_real"] + 1j * result["arrays"]["kernel_otf_imag"]
    np.testing.assert_allclose(otf[:, 15, 15], 1, atol=2e-16, rtol=0)
    assert np.max(np.abs(otf.imag)) < 1e-15
    recovered = np.fft.fftshift(np.fft.ifft2(np.fft.ifftshift(otf, axes=(-2, -1))), axes=(-2, -1))
    np.testing.assert_allclose(recovered.real, result["arrays"]["psf_kernel"], atol=8e-17, rtol=0)
    assert np.max(np.abs(recovered.imag)) < 1e-17


def test_actual_aberrated_template_remains_an_unapproved_optical_candidate():
    result = characterize(request_template())
    assert result["summary"]["status"] == "PROPOSED"
    assert result["model_candidate"]["schema"] == "intensity_psf_v1"
    assert result["summary"]["physical_validation"] == "not_established"
    assert result["summary"]["approval_status"] == "unapproved"
    assert "not_rayoptics_wavefront" in result["summary"]["request"]["application_basis"]


@pytest.mark.parametrize(
    "changes",
    [
        {"wavelength_nm": [500, 500]},
        {"wavelength_nm": [600, 500]},
        {"wavelength_nm": [500]},
        {"wavelength_nm": [500, float("nan")]},
        {"wavelength_nm": [500, 1200]},
        {"wavelength_nm": ["500", "600"]},
        {"pixel_pitch_um": float("nan")},
        {"f_number": True},
        {"f_number": 1},
        {"pupil_samples": 127},
        {"pupil_samples": 256},
        {"subpixel_samples": 4},
        {"kernel_size": 32},
        {"kernel_size": 67},
        {"max_kernel_l1_change": 0.1},
        {"minimum_retained_energy": 0.5},
        {"source_kind": "measured"},
        {"application_basis": "rayoptics_wavefront"},
        {"opd_nm": {"defocus": 0, "astigmatism": 0}},
        {"opd_nm": {"defocus": float("inf"), "astigmatism": 0, "coma_x": 0}},
        {"opd_nm": {"defocus": 0, "astigmatism": 501, "coma_x": 0}},
    ],
)
def test_units_axes_unphysical_provenance_and_bad_parameters_refused(changes):
    request = airy_request()
    request.update(changes)
    with pytest.raises(ValueError):
        validate_request(request)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"pixel_pitch_um": 20}, "Nyquist"),
        ({"pupil_samples": 32, "kernel_size": 65}, "periodic"),
        ({"kernel_size": 65, "subpixel_samples": 9}, "DFT budget"),
    ],
)
def test_sampling_replica_and_work_budget_guards(changes, message):
    request = airy_request()
    request.update(changes)
    with pytest.raises(ValueError, match=message):
        validate_request(request)


def test_nonfinite_solver_output_cannot_create_a_psf_candidate(monkeypatch):
    import camera_platform.psf_factory as factory

    monkeypatch.setattr(
        factory,
        "_propagate",
        lambda *args: (np.full((31, 31), np.nan), np.ones((32, 32)), np.zeros((32, 32))),
    )
    with pytest.raises(ValueError, match="invalid optical energy"):
        characterize(airy_request())


@pytest.mark.parametrize("field", ["pixel_pitch_um", "opd_nm"])
def test_extreme_integer_overflow_is_a_preflight_value_error(field):
    request = airy_request()
    if field == "opd_nm":
        request[field]["defocus"] = 10**400
    else:
        request[field] = 10**400
    with pytest.raises(ValueError):
        validate_request(request)


@pytest.fixture
def composition(diffraction):
    request, result, _ = diffraction
    job = {
        "id": "run:synthetic-psf",
        "status": "succeeded",
        "plugin_id": "prysm_psf_v1",
        "asset_id": "asset:synthetic-lens",
        "request": deepcopy(request),
        "result": {"model_candidate": deepcopy(result["model_candidate"])},
    }
    base = {
        "id": "lens:parent-revision",
        "asset_id": job["asset_id"],
        "kind": "lens",
        "name": "Synthetic parent",
        "revision": "1",
        "source_kind": "estimated",
        "source_uri": "fixture://declared-lens",
        "license": "MIT",
        "parents": [],
        "method_and_version": "authored-response-v1",
        "reference_plane": "sensor_surface",
        "included_effects": ["lens_relative_illumination", "lens_incident_angle_distribution"],
        "valid_domain": {
            "temperature_c": {"min": 20.0, "max": 30.0, "unit": "degC"},
            "exposure_s": {"min": 0.001, "max": 0.02, "unit": "s"},
            "gain": {"min": 1.0, "max": 1.0, "unit": "1"},
        },
        "spectral_range": {"min": 500.0, "max": 600.0, "unit": "nm"},
        "properties": {},
        "factory_application_conditions": None,
        "payload": {
            "field_radius": [0, 1],
            "theta_deg": [0, 10],
            "angular_weights": [[1, 0], [0, 1]],
            "relative_illumination": [1, 0.8],
        },
        "uncertainty": {"kind": "unknown"},
        "calibration_dataset_ids": ["training:fixture-1"],
    }
    conditions = {
        "temperature_c": {"value": 25.0, "unit": "degC"},
        "exposure_s": {"value": 0.004, "unit": "s"},
        "gain": {"value": 1.0, "unit": "1"},
        "light_id": {"value": "fixture-light", "unit": "1"},
    }
    return job, base, conditions


def test_psf_composition_preserves_parent_tables_lineage_and_fixed_domain(composition):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    before = deepcopy((job, base, conditions))
    composed = psf_candidate_fields(job, base, conditions)
    assert (job, base, conditions) == before
    assert composed["parents"] == [base["id"]]
    assert composed["calibration_dataset_ids"] == ["training:fixture-1"]
    assert composed["source_kind"] == "estimated"
    assert composed["uncertainty"] == {"kind": "unknown"}
    assert composed["psf_factory_job_id"] == job["id"]
    assert composed["spectral_range"] == base["spectral_range"]
    for name, table in base["payload"].items():
        assert composed["payload"][name] == table
    assert composed["payload"]["psf"] == job["result"]["model_candidate"]
    assert composed["valid_domain"]["temperature_c"] == {"min": 25.0, "max": 25.0, "unit": "degC"}
    conditions["temperature_c"]["value"] = 50
    assert composed["factory_application_conditions"]["temperature_c"]["value"] == 25


def test_optical_scope_preserves_parent_exposure_and_gain_bounds(composition):
    from camera_platform.factory import psf_candidate_fields
    from camera_platform.model_conditions import fixed_factory_conditions
    from camera_platform.validation import check_domain

    job, base, conditions = composition
    candidate = psf_candidate_fields(job, base, conditions, "optical_response")
    legacy = psf_candidate_fields(job, base, conditions)
    changed = deepcopy(conditions)
    changed["exposure_s"]["value"] = 0.008
    assert not check_domain(changed, candidate["valid_domain"])
    assert check_domain(changed, legacy["valid_domain"])
    assert candidate["valid_domain"]["gain"] == base["valid_domain"]["gain"]
    assert candidate["factory_application_conditions"] == conditions
    assert set(fixed_factory_conditions(candidate)) == {"temperature_c", "light_id"}
    for key, value in [("exposure_s", 0.03), ("gain", 2), ("temperature_c", 26)]:
        changed = deepcopy(conditions)
        changed[key]["value"] = value
        assert check_domain(changed, candidate["valid_domain"])
    # A fixed legacy parent stays fixed even when a child opts into optical scope.
    legacy.update(id="legacy-fixed-parent")
    child = psf_candidate_fields(job, legacy, conditions, "optical_response")
    assert child["valid_domain"]["exposure_s"] == legacy["valid_domain"]["exposure_s"]


def test_composer_checks_psf_pupil_even_when_original_conditions_omit_aperture(composition):
    from camera_platform.composer import check_assembly
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    candidate = psf_candidate_fields(job, base, conditions, "optical_response")
    candidate["id"] = "lens:composite"
    assert candidate["properties"]["psf_pupil_f_number"]["value"] == job["request"]["f_number"]
    for value, mismatch in [(job["request"]["f_number"], False), (4, True)]:
        report = check_assembly(
            {
                "model_ids": [candidate["id"]],
                "conditions": conditions | {"f_number": {"value": value, "unit": "1"}},
            },
            [candidate],
        )
        assert (
            any(i["code"] == "psf_pupil_condition_mismatch" for i in report["issues"]) == mismatch
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda b, c: c["temperature_c"].update(value=50),
        lambda b, c: c["exposure_s"].update(value=0.03),
        lambda b, c: c.pop("gain"),
        lambda b, c: c["temperature_c"].update(unit="K"),
        lambda b, c: c["temperature_c"].update(value="warm"),
        lambda b, c: b.update(valid_domain={}),
        lambda b, c: b["valid_domain"]["temperature_c"].update(min=True),
    ],
)
def test_psf_composition_cannot_widen_or_drop_parent_applicability(composition, change):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    change(base, conditions)
    with pytest.raises(ValueError, match="parent Lens domain"):
        psf_candidate_fields(job, base, conditions)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["light_id"].update(value="different-light"),
        lambda c: c["light_id"].update(unit="label"),
        lambda c: c.pop("light_id"),
    ],
)
def test_fixed_parent_categorical_application_conditions_must_remain_exact(composition, change):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    base["factory_application_conditions"] = deepcopy(conditions)
    change(conditions)
    with pytest.raises(ValueError, match="fixed application conditions"):
        psf_candidate_fields(job, base, conditions)


@pytest.mark.parametrize(
    "spectral",
    [
        {"min": 450, "max": 600, "unit": "nm"},
        {"min": 500, "max": 650, "unit": "nm"},
        {"min": 525, "max": 575, "unit": "nm"},
        {"min": 0.5, "max": 0.6, "unit": "um"},
        {"min": 500, "max": float("nan"), "unit": "nm"},
    ],
)
def test_psf_publication_has_no_implicit_spectral_narrowing_or_extrapolation(composition, spectral):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    base["spectral_range"] = spectral
    with pytest.raises(ValueError, match="declared nm coverage"):
        psf_candidate_fields(job, base, conditions)


def test_spectral_parent_requires_exact_psf_grid_not_only_equal_endpoints(composition):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    payload = base["payload"]
    old_weights, old_ri = payload["angular_weights"], payload["relative_illumination"]
    payload.update(
        wavelength_nm=[500, 550, 600],
        angular_weights=[old_weights] * 3,
        relative_illumination=[old_ri] * 3,
    )
    with pytest.raises(ValueError, match="coordinates must match exactly"):
        psf_candidate_fields(job, base, conditions)
    payload.update(
        wavelength_nm=[500, 600],
        angular_weights=[old_weights] * 2,
        relative_illumination=[old_ri] * 2,
    )
    composed = psf_candidate_fields(job, base, conditions)
    assert composed["payload"]["wavelength_nm"] == [500, 600]
    assert composed["payload"]["angular_weights"] == [old_weights] * 2


@pytest.mark.parametrize(
    "declared",
    [
        {"value": 2, "unit": "1"},
        {"value": 8, "unit": "mm"},
        {"value": "8", "unit": "1"},
    ],
)
def test_explicit_f_number_application_cannot_conflict_with_declared_pupil(composition, declared):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    conditions["f_number"] = declared
    with pytest.raises(ValueError, match="Explicit f_number"):
        psf_candidate_fields(job, base, conditions)


def test_parent_f_number_property_must_match_and_matching_condition_is_frozen(composition):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    base["properties"]["f_number"] = {"value": 4, "unit": "1"}
    with pytest.raises(ValueError, match="parent properties"):
        psf_candidate_fields(job, base, conditions)
    base["properties"]["f_number"]["value"] = 8
    conditions["f_number"] = {"value": 8, "unit": "1"}
    composed = psf_candidate_fields(job, base, conditions)
    assert composed["valid_domain"]["f_number"] == {"min": 8, "max": 8, "unit": "1"}


def test_psf_composition_rejects_extreme_condition_integer_before_any_publication(composition):
    from camera_platform.factory import psf_candidate_fields

    job, base, conditions = composition
    conditions["temperature_c"]["value"] = 10**400
    with pytest.raises(ValueError, match="Invalid fixed application condition"):
        psf_candidate_fields(job, base, conditions)

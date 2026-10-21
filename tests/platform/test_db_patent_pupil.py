"""The patent operating iris is physical, wavelength-invariant, and traceable."""

import copy
import hashlib
from pathlib import Path

import numpy as np
import pytest

from camera_platform import db_design_optics as db
from camera_platform import prescription_psf
from camera_platform import reference_workshop_optics as reference

ROOT = Path(__file__).resolve().parents[2]
CSV = ROOT / (
    "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents/"
    "reviewed_prescriptions/canon/US20200174235A1_p248_numerical-example-1/"
    "configured_surfaces/wide-angle.csv"
)


def _source_context():
    context = db._source("canon_p0248")
    context.pop("operating_f_number")
    return context


def _stop(request):
    return next(surface for surface in request["prescription"]["surfaces"] if surface["is_stop"])


def _request(waves, *, original=False):
    return reference._request(
        ROOT,
        db.template("canon_p0248"),
        waves,
        source_context=_source_context() if original else db._source("canon_p0248"),
    )


def _paraxial(request, index):
    model = prescription_psf._model(request, index)
    return model["analysis_results"]["parax_data"].fod


def test_patent_iris_matches_d_line_without_changing_source_or_lens_geometry():
    before_bytes = CSV.read_bytes()
    original = _request([550.0, 587.6], original=True)
    original_copy = copy.deepcopy(original)
    applied = _request([550.0, 587.6])
    source_values = _paraxial(original, 1)
    applied_values = _paraxial(applied, 1)

    assert _stop(original)["semi_diameter_mm"] == 9.98
    assert source_values.efl / (2 * source_values.enp_radius) == pytest.approx(2.7476834051250587)
    assert _stop(applied)["semi_diameter_mm"] == pytest.approx(6.655796209501962)
    assert applied_values.efl / (2 * applied_values.enp_radius) == pytest.approx(4.12, abs=1e-10)
    assert applied_values.efl == pytest.approx(source_values.efl, abs=1e-10)
    assert applied_values.enp_dist == pytest.approx(source_values.enp_dist, abs=1e-10)
    assert source_values.efl == pytest.approx(24.72, abs=0.005)
    assert source_values.enp_dist == pytest.approx(32.23, abs=0.005)

    # The CSV's "image" classification is not a license to drop surface 36.
    surfaces = applied["prescription"]["surfaces"]
    assert len(surfaces) == 36
    assert surfaces[-1]["radius_mm"] == -56.995
    assert surfaces[-1]["thickness_mm"] == 13.50
    for old, new in zip(original["prescription"]["surfaces"], surfaces, strict=True):
        allowed = {"semi_diameter_mm"} if old["is_stop"] else set()
        assert {key: value for key, value in old.items() if key not in allowed} == {
            key: value for key, value in new.items() if key not in allowed
        }
    assert original == original_copy
    assert CSV.read_bytes() == before_bytes
    assert hashlib.sha256(before_bytes).hexdigest() == db.SOURCES["canon_p0248"]["csv_sha256"]


def test_disjoint_wavelength_batches_use_one_iris_not_independent_f_number_targets():
    requests = [
        _request(waves) for waves in ([450.0, 460.0], [550.0, 587.6], [640.0, 650.0])
    ]
    assert len({_stop(request)["semi_diameter_mm"] for request in requests}) == 1
    at_550 = _paraxial(requests[1], 0)
    assert at_550.efl / (2 * at_550.enp_radius) == pytest.approx(4.12508391444129)
    assert not np.isclose(at_550.efl / (2 * at_550.enp_radius), 4.12, atol=1e-5)


def test_source_stop_and_operating_stop_cannot_share_optical_cache():
    spec = db.template("canon_p0248")
    original = reference.cache_key(ROOT, spec, source_context=_source_context())
    applied = reference.cache_key(ROOT, spec, source_context=db._source("canon_p0248"))
    assert original != applied
    metadata = db.source_metadata(ROOT, "canon_p0248")
    condition = metadata["pupil_condition"]
    assert condition["reference_wavelength_nm"] == 587.6
    assert condition["source_stop_semi_diameter_mm"] == 9.98
    assert condition["applied_stop_semi_diameter_mm"] == pytest.approx(6.655796209501962)
    assert condition["physical_validation"] == "not_established"
    assert metadata["applied_prescription_sha256"] != metadata["prescription_sha256"]


def test_solve_passes_same_physical_stop_to_forward_and_reciprocal_backends(monkeypatch, tmp_path):
    """Check real solve dispatch, without treating a mocked solve as optical evidence."""
    forward_stops = []
    reciprocal_stops = []

    def record_forward(request, wavelength_index, radii, samples):
        forward_stops.append(_stop(request)["semi_diameter_mm"])
        # Only transport the request to the reciprocal dispatch. Optical
        # validity is established separately by the actual paraxial tests.
        return [
            {
                "points": np.array([[0.0, radius]]),
                "directions": np.array([[0.0, 0.0, 1.0]]),
                "chief": np.array([0.0, radius]),
                "chief_direction": np.array([0.0, 0.0, 1.0]),
            }
            for radius in radii
        ]

    class ReachedReciprocalError(Exception):
        pass

    def record_reciprocal(directory, label, request, backend, operation, progress):
        assert label == "reciprocal-radiometry"
        assert operation is reference.lens_factory.characterize
        assert request["sampling"]["polar_rings"] == 28
        assert request["sampling"]["azimuth_samples"] == 48
        assert request["sampling"]["ri_tolerance_absolute"] == 0.03
        assert request["sampling"]["angular_cdf_tolerance_absolute"] == 0.05
        assert request["sampling"]["irradiance_tolerance_relative"] == 0.03
        reference.lens_factory.validate_request(request)  # Original resource guards.
        stop = next(s for s in request["surfaces"] if s["is_stop"])
        reciprocal_stops.append(stop["semi_diameter_mm"])
        raise ReachedReciprocalError

    monkeypatch.setattr(reference, "_rays", record_forward)
    monkeypatch.setattr(reference, "_wavefront_diagnostic", lambda *args: {"status": "not_run"})
    monkeypatch.setattr(reference.shared, "_batch", record_reciprocal)
    with pytest.raises(ReachedReciprocalError):
        reference.solve(
            ROOT,
            db.template("canon_p0248"),
            tmp_path,
            source_context=db._source("canon_p0248"),
        )
    assert len(forward_stops) == 21
    assert len(reciprocal_stops) == 1
    assert set(forward_stops) == set(reciprocal_stops)
    assert forward_stops[0] == pytest.approx(6.655796209501962)

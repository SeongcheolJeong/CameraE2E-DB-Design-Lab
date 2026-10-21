"""Pinned DB-to-wavefront translation; numerical refusal is preserved."""

import csv
import hashlib
import io
from pathlib import Path

import pytest

from camera_platform import lens_db_wavefront_bridge as bridge
from camera_platform import lens_factory, optics_surfaces
from camera_platform.lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    LensDBWavefrontError,
    db_wavefront_preflight,
)
from camera_platform.lens_patent_source_review import reviewed_source_semantics

ROOT = Path(__file__).resolve().parents[2] / "camerae2e_db"
CANON_14 = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CANON_14_CSV = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"
CANON_349 = "1c346187-4529-5290-8e08-f769f8def387"
CANON_349_CSV = "ba7994d1585fda31e387d2f3191d988d30e03d359363171ec1d83307bc617c77"


def inspect(revision=CANON_14, csv_sha=CANON_14_CSV, **overrides):
    return db_wavefront_preflight(
        revision,
        catalog_root=ROOT,
        reviewed_csv_sha256=csv_sha,
        aperture_hypothesis=APERTURE_HYPOTHESIS,
        **overrides,
    )


def test_reviewed_28_row_a10_lens_separates_opd_and_direction_factory_domains():
    result = inspect()
    assert result["schema"] == "camerae2e_db_lens_wavefront_research_v1"
    assert result["status"] == "research_request_ready"
    assert result["psf_request"] is not None
    assert result["psf_request"]["pupil_samples"] == 16
    assert result["psf_request"]["kernel_size"] == 5
    assert result["psf_request"]["subpixel_samples"] == 5
    assert result["source_surface_count"] == result["sequential_surface_count"] == 28
    audit = result["translation_audit"]
    assert audit["converted_surface_count"] == 28
    assert audit["nonzero_profile_orders"] == [4, 6, 8, 10]
    assert audit["a10_surface_orders"] == [4, 5, 19, 24]
    assert audit["retained_numbered_image_surface_orders"] == [28]
    assert audit["stop_surface_orders"] == [18]
    assert result["source"]["revision_id"] == CANON_14
    assert result["source"]["source_database_sha256"] == (
        "07d4141c67fd08cc5094b36cc4bdab9ee0e784aa1d8e585c93627e3a1c373bab"
    )
    assert result["source"]["reviewed_csv_sha256"] == CANON_14_CSV
    assert result["source"]["prescription_sha256"] == (
        "6a7f9651ed05d4c341f8211b1544b0d9b9a4730b7c5d2fafe8eb0190370fc483"
    )
    assert "aperture_convention_unverified" in result["source_validation_warning_codes"]
    review = result["source_semantics_review"]
    assert review["archived_patent_html_sha256"] == (
        "648c9cd8828095f60526d6c7caede8c7f5349842fbf31cb48cff98d29ab46f27"
    )
    assert review["effective_aperture_field_meaning"] == "patent_declares_effective_diameter_mm"
    assert review["terminal_numbered_row"] == {
        "surface_order": 28,
        "radius": "plane",
        "thickness_to_image_plane_mm": 0.5,
        "effective_diameter_mm": 50.0,
        "separate_image_plane_caption": True,
        "optical_role": "last_plane_interface_before_image_plane",
    }
    assert review["physical_clear_aperture_verified"] is False
    assert "physical clear aperture" in result["source_warning_resolution"][
        "aperture_convention_unverified"
    ]
    assert "material_identity_unknown" in result["source_validation_warning_codes"]
    assert result["physical_validation"] == "not_established"
    assert result["product_ranking_allowed"] is False
    assert result["blockers"] == []
    direction = result["directional_radiometry_preflight"]
    assert direction == {
        "status": "input_domain_supported_uncharacterized",
        "scope": "directional_radiometry_only_not_opd",
    }
    prescription = lens_factory.validate_request(result["psf_request"]["prescription"])
    _, _, _, distance = lens_factory._sampling(prescription, 1)
    support = lens_factory._angular_support(prescription, distance)
    certificate = support["certificate"]
    assert certificate["limiting_surface_id"] == "db-surface-24-24"
    assert certificate["full_rear_disk_maximum_angle_deg"] == pytest.approx(
        88.87021056766181
    )
    assert certificate["maximum_admitted_sensor_angle_deg"] < 80
    assert certificate["sampling_maximum_angle_deg"] < 80
    assert certificate["physical_validation"] == "not_established"


def test_paraxial_translation_matches_efl_but_not_pupil_spec():
    diagnostic = inspect()["paraxial_translation_diagnostic"]
    assert diagnostic["status"] == "computed_paraxial_only"
    assert diagnostic["not_diffraction"] is True
    assert diagnostic["physical_validation"] == "not_established"
    assert diagnostic["wavelength_nm"] == 550.0
    assert diagnostic["effective_focal_length_mm"] == pytest.approx(35.178465940618935)
    comparison = diagnostic["comparison_to_saved_geometric_psf"]
    assert comparison["status"] == "compared_paraxial_only"
    assert comparison["focal_length_delta_mm"] == pytest.approx(0.0, abs=1e-12)
    assert comparison["saved_entrance_pupil_radius_mm"] == pytest.approx(4.872363723312298)
    assert diagnostic["entrance_pupil_radius_mm"] == pytest.approx(4.949545322338922)
    assert comparison["entrance_pupil_radius_relative_delta"] == pytest.approx(0.015840689121245477)
    assert "Equal EFL does not establish equal pupil support" in comparison["pupil_spec_difference"]


def test_csv_hash_and_aperture_hypothesis_are_mandatory():
    with pytest.raises(LensDBWavefrontError, match="Explicitly select") as exc:
        db_wavefront_preflight(
            CANON_14,
            catalog_root=ROOT,
            reviewed_csv_sha256=CANON_14_CSV,
            aperture_hypothesis="validated_diameter",
        )
    assert exc.value.code == "aperture_hypothesis_required"
    with pytest.raises(LensDBWavefrontError, match="bytes changed") as exc:
        inspect(csv_sha="0" * 64)
    assert exc.value.code == "csv_hash_mismatch"


def test_cartesian_corner_cannot_leave_reviewed_radial_field():
    with pytest.raises(LensDBWavefrontError, match="corner exceeds") as exc:
        inspect(field_x_mm=(-10.0, 10.0), field_y_mm=(-10.0, 10.0))
    assert exc.value.code == "field_outside_source"


def test_shorter_reviewed_lens_missing_apertures_is_blocked_without_partial_request():
    result = inspect(CANON_349, CANON_349_CSV)
    assert result["status"] == "blocked"
    assert result["sequential_surface_count"] == 20
    assert result["psf_request"] is None
    assert result["paraxial_translation_diagnostic"] is None
    assert len(result["blockers"]) == 20
    assert {item["code"] for item in result["blockers"]} == {"aperture_unknown"}


def test_surface_budget_refuses_before_direct_backend_diagnostic(monkeypatch):
    monkeypatch.setattr(optics_surfaces, "MAX_SURFACES", 27)
    result = inspect()
    assert result["status"] == "blocked"
    assert result["psf_request"] is None
    assert result["paraxial_translation_diagnostic"] is None
    assert result["blockers"][0]["code"] == "surface_budget_exceeded"


def test_source_review_refuses_to_transfer_to_other_revision_or_modified_terminal_row():
    rows = [
        {"surface_order": index, "surface_kind": "surface", "radius_mm": 1.0,
         "thickness_mm": 1.0, "effective_aperture_mm": 20.0}
        for index in range(1, 28)
    ]
    rows.append({"surface_order": 28, "surface_kind": "image", "radius_mm": 0.0,
                 "thickness_mm": 0.5, "effective_aperture_mm": 50.0})
    assert reviewed_source_semantics("other-revision", CANON_14_CSV, rows) is None
    assert reviewed_source_semantics(CANON_14, "0" * 64, rows) is None
    rows[-1]["thickness_mm"] = 0.6
    with pytest.raises(ValueError, match="terminal row differs"):
        reviewed_source_semantics(CANON_14, CANON_14_CSV, rows)


def test_wavefront_numerical_controls_are_explicit_and_bounded():
    result = inspect(pupil_samples=32, kernel_size=9, subpixel_samples=5)
    assert result["status"] == "research_request_ready"
    request = result["psf_request"]
    assert (request["pupil_samples"], request["kernel_size"], request["subpixel_samples"]) == (
        32, 9, 5
    )
    refused = inspect(pupil_samples=15)
    assert refused["status"] == "blocked"
    assert refused["psf_request"] is None
    assert refused["blockers"][0]["code"] == "wavefront_contract_rejected"


@pytest.fixture
def object_row_source(monkeypatch):
    """Inject matching reviewed CSV/DB rows without changing either saved asset."""
    ordinary = inspect()
    csv_path = next(ROOT.glob("lens_db/*/data/lens_patents")) / ordinary["source"][
        "reviewed_csv_relative_path"
    ]
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))

    def supply(distance, position=0):
        object_row = dict.fromkeys(rows[0], "")
        object_row.update(
            surface_order="0", surface_label="Object", surface_key="OBJECT",
            surface_kind="object", radius="Infinity", thickness=distance, is_aspheric="0",
        )
        changed = list(rows)
        changed.insert(position, object_row)
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(changed)
        content = stream.getvalue().encode()
        sha = hashlib.sha256(content).hexdigest()
        monkeypatch.setattr(bridge, "_reviewed_csv", lambda *a: (csv_path, sha, content))
        monkeypatch.setattr(bridge, "lens_patent_surfaces", lambda *a, **kw: changed)
        return sha

    return ordinary, supply


@pytest.mark.parametrize("distance", ["400.0", "800.0", "", "unknown", "(Variable)"])
def test_finite_or_unknown_object_cannot_be_silently_replaced_by_infinity(
    object_row_source, monkeypatch, distance,
):
    _, supply = object_row_source
    sha = supply(distance)

    def no_backend(*args, **kwargs):
        pytest.fail("Unsupported object conjugate must fail before paraxial/OPD work")

    monkeypatch.setattr(bridge, "_paraxial_diagnostic", no_backend)
    with pytest.raises(LensDBWavefrontError, match="explicit infinite distance") as error:
        inspect(csv_sha=sha)
    assert error.value.code == "object_conjugate_unsupported"


def test_explicit_leading_infinity_preserves_request_but_misplaced_object_is_refused(
    object_row_source, monkeypatch,
):
    ordinary, supply = object_row_source
    # A new source fingerprint deliberately cannot inherit the saved geometric
    # asset comparison; this test exercises the object-admission boundary only.
    monkeypatch.setattr(bridge, "_paraxial_diagnostic", lambda *a: None)
    result = inspect(csv_sha=supply("Infinity"))
    assert result["status"] == "research_request_ready"
    assert result["source_surface_count"] == 29
    assert result["sequential_surface_count"] == 28
    assert result["psf_request"] == ordinary["psf_request"]
    with pytest.raises(LensDBWavefrontError, match="misplaced"):
        inspect(csv_sha=supply("Infinity", position=1))

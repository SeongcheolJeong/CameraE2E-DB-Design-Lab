"""New patent binding shares the established optical transport unchanged."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import db_design_optics as db
from camera_platform import reference_workshop_optics as reference

ROOT = Path(__file__).resolve().parents[2]


def test_new_patent_requires_explicit_source_context_and_preserves_old_contract():
    old = reference.template()
    before = copy.deepcopy(old)
    assert reference.normalize(old) == before
    candidate = db.template("canon_p0248")
    with pytest.raises(ValueError, match="pinned"):
        reference.normalize(candidate)
    assert candidate["reference_revision_id"] != old["reference_revision_id"]
    assert old == before
    with pytest.raises(ValueError, match="pinned"):
        db.template("unknown")


def test_pinned_prescriptions_retain_distinct_sources_and_report_aperture_assumptions():
    rows = {x["key"]: x for x in db.candidates(ROOT)}
    baseline, candidate = rows["reference"], rows["canon_p0248"]
    assert baseline["source"]["prescription_sha256"] != candidate["source"]["prescription_sha256"]
    assert candidate["source"]["reviewed_csv_sha256"] == db.SOURCES["canon_p0248"]["csv_sha256"]
    assert len(candidate["source"]["surfaces"]) == 36
    params = {x["parameter"]: x for x in candidate["parameter_sources"]}
    assert params["reported_f_number"]["value"] == 4.12
    assert params["reported_f_number"]["used_as_pupil_control"] is True
    assert params["derived_stop_pupil_f_number"]["value"] == pytest.approx(2.7510739360)
    assert params["derived_stop_pupil_f_number"]["used_as_pupil_control"] is False
    assert params["applied_f_number"]["value"] == pytest.approx(4.12)
    assert params["physical_clear_aperture"]["origin"] == "explicit_assumption"
    assert candidate["physical_validation"] == "not_established"


def test_shared_native_pitch_retains_different_angular_roi_widths():
    spec = db.template("canon_p0248")
    old = reference.geometry(reference.template())
    new = reference.geometry(spec, source_context=db.SOURCES["canon_p0248"])
    assert old["native_shape_row_col"] == new["native_shape_row_col"] == [256, 256]
    assert old["pixel_pitch_um"] == new["pixel_pitch_um"] == 2.9
    assert old["horizontal_fov_deg"] < new["horizontal_fov_deg"]


def test_aperture_pair_keeps_one_prescription_and_changes_only_the_operating_stop():
    opened = db.source_metadata(ROOT, "canon_p0248_open")
    stopped = db.source_metadata(ROOT, "canon_p0248")
    assert opened["reviewed_csv_sha256"] == stopped["reviewed_csv_sha256"]
    assert opened["paraxial"]["effective_f_number"] == pytest.approx(2.7510739360)
    assert stopped["pupil_condition"]["effective_f_number"] == pytest.approx(4.12)
    for key in ("canon_p0248_open", "canon_p0248"):
        source = db._source(key)
        source["name"] = "mutated copy"
        assert db.SOURCES[key]["name"] != "mutated copy"


def test_user_apertures_keep_patent_geometry_and_distinguish_design_values():
    source = db.source_metadata(ROOT, db.aperture_key(8))
    patent = db.source_metadata(ROOT, "canon_p0248")
    condition = source["pupil_condition"]
    assert condition["mode"] == "user_f_number_at_reference_wavelength"
    assert condition["source_url"] is None
    assert condition["effective_f_number"] == pytest.approx(8)
    assert condition["applied_stop_semi_diameter_mm"] == pytest.approx(3.427735047893511)
    assert source["reviewed_csv_sha256"] == patent["reviewed_csv_sha256"]
    for new, old in zip(source["surfaces"], patent["surfaces"], strict=True):
        assert {k: v for k, v in new.items() if k != "semi_diameter_mm"} == {
            k: v for k, v in old.items() if k != "semi_diameter_mm"
        }
        if not old["is_stop"]:
            assert new["semi_diameter_mm"] == old["semi_diameter_mm"]
    params = {p["parameter"]: p for p in source["parameter_sources"]}
    assert params["reported_f_number"]["value"] == 4.12
    assert params["reported_f_number"]["used_as_pupil_control"] is False
    assert params["requested_f_number"]["origin"] == "user_design_setting"
    assert "operating_stop_derived_from_patent_f_number" not in source["assumptions"]


def test_aperture_keys_preserve_historical_cache_contract_and_canonical_values():
    assert db.aperture_key(2.75) == "canon_p0248_open"
    assert db.aperture_key(4.12) == "canon_p0248"
    assert db.aperture_key(8) == db.aperture_key(8.0) == "canon_p0248_f_8"
    with pytest.raises(ValueError):
        db._source("canon_p0248_f_4p12")
    with pytest.raises(ValueError):
        db._source("canon_p0248_f_nan")
    with pytest.raises(ValueError):
        db._source("canon_p0248_f_12")
    for key in ("canon_p0248_open", "canon_p0248"):
        assert db.cache_key(
            ROOT, key, optical_profile="exit_pupil_diffraction"
        ) == reference.cache_key(
            ROOT,
            db.template(key, optical_profile="exit_pupil_diffraction"),
            source_context=db.SOURCES[key],
        )


def test_actual_candidate_rays_keep_shared_reference_object_directions():
    source = db._source("canon_p0248")
    request = reference._request(
        ROOT, db.template("canon_p0248"), [450.0, 550.0, 650.0], source_context=source
    )
    radius = 0.85 * reference.MAX_HEIGHT_MM
    rows = reference._rays(request, 1, [0.0, radius], 24)
    assert len(rows[1]["points"]) > 100
    assert np.linalg.norm(rows[1]["chief"]) == pytest.approx(7.98287496, abs=1e-6)
    assert rows[1]["effective_focal_length_mm"] == pytest.approx(24.7328804)
    assert rows[1]["stop_aim_residual_mm"] < 1e-5


def test_two_node_solution_indexes_outer_pupil_without_legacy_middle_field():
    spec = db.template("canon_p0248")
    field = next(f for f in reference.field_layout() if f["id"] == "outer-ne")
    field.update(
        status="succeeded", issues=[], pupil_node_index=1, geometry=reference.geometry(spec)
    )
    psf = np.zeros((21, 1, 3, 3))
    psf[..., 1, 1] = 1
    solution = {
        "result": {"fields": [field], "revision": "unit"},
        "arrays": {
            "psf": psf,
            "wavelength_nm": reference.WAVE,
            "irradiance_per_radiance_sr": np.tile([0.01, 0.02], (21, 1)),
            "conditional_weights": np.ones((21, 2, 1)),
            "directions_xyz": np.tile([[0.0, 0.0, 1.0]], (2, 1, 1)),
        },
    }
    result = reference.apply_field(
        solution,
        "outer-ne",
        np.ones((384, 384, 21)) * 1e12,
        angular_response=lambda theta, phi, wave: np.full((len(theta), 4), 0.25),
    )
    assert result["mean_electron_rate"].shape == (256, 256)
    np.testing.assert_allclose(
        result["mean_electron_rate"], 1e12 * 0.02 * 210 * 0.25 * (2.9e-6) ** 2
    )

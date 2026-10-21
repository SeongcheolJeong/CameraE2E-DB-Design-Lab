"""Source semantics, reproducibility and optical impact of reviewed structures."""

import json

import numpy as np
import pytest

from pyisetcam import sensor_techinsights_evidence
from pyisetcam.cfa_materials import image_sensor_cfa_materials
from pyisetcam.physical_estimate import image_sensor_physical_model, physical_optical_response


@pytest.mark.parametrize("code", ["DEF-1807-801", "DEF-2011-804", "DEF-2206-805"])
def test_facts_have_page_provenance_without_claiming_measured_response(code):
    evidence = sensor_techinsights_evidence(code)
    assert evidence["available"]
    assert not evidence["measured_response_available"]
    assert not evidence["measured_accuracy_verified"]
    assert all(s["status"] == "not_checked" for s in evidence["source_checks"].values())
    for fact in evidence["selected_facts"].values():
        source = evidence["sources"][fact["source_id"]]
        assert 1 <= fact["pdf_page_1_based"] <= source["page_count"]
        assert len(source["sha256"]) == 64
    json.dumps(evidence, allow_nan=False)


def test_absent_and_changed_originals_are_not_freshly_verified(tmp_path):
    code = "DEF-2011-804"
    missing = sensor_techinsights_evidence(code, source_root=tmp_path)
    assert all(s["status"] == "unavailable" for s in missing["source_checks"].values())
    key, source = next(iter(missing["sources"].items()))
    path = tmp_path / source["relative_path"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Different source contents")
    changed = sensor_techinsights_evidence(code, source_root=tmp_path)
    assert changed["source_checks"][key]["status"] == "mismatch"


def test_dti_scope_and_dimensional_ranges_do_not_become_optical_measurements():
    older = sensor_techinsights_evidence("DEF-1807-801")
    nir = sensor_techinsights_evidence("DEF-2011-804")
    latest = sensor_techinsights_evidence("DEF-2206-805")
    assert older["active_pixel_dti"] == "unconfirmed"
    assert nir["active_pixel_dti"] == "partial_depth_back_dti"
    assert latest["active_pixel_dti"] == "full_depth_front_dti"
    assert all(e["rejected_legacy_optical_fields"] for e in (nir, latest))
    assert "red_cfa_thickness_um" not in latest["selected_facts"]
    assert "blue_cfa_thickness_um" not in latest["selected_facts"]


def test_reported_aperture_area_is_converted_to_width_and_only_diagnostic():
    for code, fraction in [("DEF-2011-804", 0.88), ("DEF-2206-805", 0.854)]:
        model = image_sensor_physical_model(code)
        assert model["optical_assumptions"]["acceptance_width_fraction"] ** 2 == pytest.approx(
            fraction
        )
        assert "not_measured_photodiode" in model["optical_assumptions"]["acceptance_source"]
        explicit = image_sensor_physical_model(
            code, optical_assumptions={"acceptance_width_fraction": 0.5}
        )
        np.testing.assert_array_equal(model["eqe"], explicit["eqe"])
        assert explicit["optical_assumptions"]["acceptance_source"] == (
            "explicit_scenario_assumption"
        )
        left = physical_optical_response(model, [450, 550, 650], theta_deg=20)
        right = physical_optical_response(explicit, [450, 550, 650], theta_deg=20)
        assert np.all(left["own_site_acceptance"] > right["own_site_acceptance"])


def test_imx662_applies_identified_green_thickness_and_direct_curvature():
    before = image_sensor_physical_model(
        "DEF-2206-805", optical_assumptions={"geometry_source": "canonical_db"}
    )
    after = image_sensor_physical_model("DEF-2206-805")
    geometry = after["geometry_um"]
    assert geometry["lens_height"] == 1.3
    assert geometry["lens_vertex_radius_um"] == 1.6
    assert geometry["cfa_thickness_by_channel"] == {"red": 0.475, "green": 0.5, "blue": 0.475}
    assert geometry["passivation_thickness_by_channel"]["green"] == 0.8
    for channel in ("red", "green", "blue"):
        assert (
            geometry["lens_height"]
            + geometry["cfa_thickness_by_channel"][channel]
            + geometry["passivation_thickness_by_channel"][channel]
        ) == pytest.approx(2.6)
    a, b = np.asarray(before["eqe"]), np.asarray(after["eqe"])
    np.testing.assert_array_equal(a[:, [0, 3]], b[:, [0, 3]])
    assert np.all(b[:, 1] <= a[:, 1])
    assert np.any(b[:, 1] < a[:, 1])
    np.testing.assert_array_equal(b[:, 1], b[:, 2])
    assert all(not c["product_measured"] for c in after["material_bundle"]["channels"].values())
    assert after["provenance"]["structural_evidence"]["unmodeled_structure"]
    assert after["provenance"]["canonical_geometry_sha256"] == (
        before["provenance"]["canonical_geometry_sha256"]
    )
    provenance = after["material_bundle"]["provenance"]
    assert provenance["canonical_passivation_thickness_um"] == 1.241
    assert provenance["reviewed_passivation_thickness_um"] == .825


def test_canonical_material_export_and_saved_physics_are_preserved():
    canonical = image_sensor_cfa_materials("DEF-2206-805")
    model = image_sensor_physical_model(
        "DEF-2206-805", optical_assumptions={"geometry_source": "canonical_db"}
    )
    assert canonical["geometry_um"]["lens_height"] == 0.884
    assert canonical["geometry_um"]["cfa_thickness"] == 0.475
    assert model["material_bundle"] == canonical
    assert model["provenance"]["reviewed_evidence_sha256"] is None
    # This pre-overlay 550-nm green EQE is independent of the new source facts.
    assert np.asarray(model["eqe"])[15, 1] == pytest.approx(0.48408414551257656, abs=1e-14)


def test_measured_curvature_sensitivity_and_per_color_energy_budget():
    model = image_sensor_physical_model("DEF-2206-805")
    changed = image_sensor_physical_model(
        "DEF-2206-805", optical_assumptions={"lens_height_scale": 1.2}
    )
    assert changed["geometry_um"]["lens_vertex_radius_um"] == pytest.approx(1.6 / 1.2)
    wave = np.arange(400.0, 701.0, 10.0)[:, None]
    angles = np.arange(0.0, 61.0, 10.0)[None, :]
    for item in (model, changed):
        result = physical_optical_response(item, wave, theta_deg=angles)
        np.testing.assert_allclose(result["R"] + result["T"] + result["A"], 1, atol=1e-14)
        assert np.all((result["eqe"] >= 0) & (result["eqe"] <= 0.75))
    a = physical_optical_response(model, [450.0, 550.0, 650.0], theta_deg=20)
    b = physical_optical_response(changed, [450.0, 550.0, 650.0], theta_deg=20)
    assert not np.allclose(a["own_site_acceptance"], b["own_site_acceptance"])

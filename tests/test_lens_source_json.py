"""Optical invariants of the conservative DeepLens JSON adapter."""

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from pyisetcam.lens_source_json import (
    _CATALOG_HASHES,
    _MATERIALS,
    DEEPLENS_COMMIT,
    parse_deeplens_file,
    parse_deeplens_json,
    parse_deeplens_text,
)
from pyisetcam.lens_source_parser import parse_lens_configurations, parse_lens_text
from pyisetcam.lens_validation import validate_lens_prescription


@pytest.fixture
def prescription():
    return {
        "info": "test refractive model",
        "foclen": 40.0,
        "fnum": 4.0,
        "r_sensor": 10.0,
        "d_sensor": 43.0,
        "surfaces": [
            {"type": "Aperture", "r": 5.0, "d": 0.0, "d_next": 1.0},
            {
                "type": "Spheric",
                "r": 6.0,
                "c": 0.025,
                "d": 1.0,
                "d_next": 2.0,
                "mat1": "air",
                "mat2": "1.5/50",
            },
            {
                "type": "Spheric",
                "r": 6.0,
                "roc": -40.0,
                "d": 3.0,
                "d_next": 40.0,
                "mat1": "1.5/50",
                "mat2": "air",
            },
        ],
    }


def errors(parsed):
    return {i["code"] for i in parsed["issues"] if i["severity"] == "error"}


def test_radius_aperture_gap_and_transmitted_medium_are_distinct(prescription):
    parsed = parse_deeplens_json(prescription)
    assert parsed["status"] == "parsed"
    assert validate_lens_prescription(parsed["surfaces"])["valid"]
    obj, stop, front, rear, image = parsed["surfaces"]
    assert obj["thickness_mm"] is None and obj["thickness_state"] == "unknown"
    assert stop["surface_kind"] == "stop" and stop["thickness_mm"] == 1.0
    assert front["radius_mm"] == 40.0
    assert front["semidiameter_mm"] == 6.0 and front["effective_aperture_mm"] == 12.0
    assert front["nd"] == 1.5 and front["vd"] == 50.0
    assert rear["nd"] == 1.0 and rear["vd"] is None
    assert rear["axial_position_mm"] == 3.0
    assert image["axial_position_mm"] == 43.0
    assert "image_circle_mm" not in parsed["metadata"]


@pytest.mark.parametrize(
    "flag,terms", [(True, {"a2": 0.001, "a4": 2e-5}), (False, {"a4": 0.001, "a6": 2e-5})]
)
def test_explicit_asphere_order_and_sag(prescription, flag, terms):
    source = prescription["surfaces"][1]
    source.update(type="Aspheric", k=-1.0, ai=[0.001, 2e-5], use_ai2=flag)
    parsed = parse_deeplens_json(prescription)
    assert not errors(parsed)
    row = parsed["surfaces"][2]
    assert row["coefficients"] == terms
    r, c, k = 2.0, 1 / row["radius_mm"], row["conic"]
    sag = c * r**2 / (1 + math.sqrt(1 - (1 + k) * c * c * r * r))
    sag += sum(a * r ** int(key[1:]) for key, a in row["coefficients"].items())
    expected = 0.05 + (0.004 + 0.00032 if flag else 0.016 + 0.00128)
    assert sag == pytest.approx(expected)


def test_legacy_asphere_requires_corroborating_named_terms(prescription):
    s = prescription["surfaces"][1]
    s.update(type="Aspheric", k=0.0, ai=[0.001, 2e-5])
    assert "ambiguous_asphere_order" in errors(parse_deeplens_json(prescription))
    s.update({"(ai2)": 0.001, "(ai4)": 2e-5})
    parsed = parse_deeplens_json(prescription)
    assert parsed["status"] == "parsed"
    assert parsed["surfaces"][2]["coefficients"] == {"a2": 0.001, "a4": 2e-5}
    s["(ai4)"] = -2e-5
    assert "ambiguous_asphere_order" in errors(parse_deeplens_json(prescription))


def test_explicit_coefficient_mismatch_blocks(prescription):
    prescription["surfaces"][1].update(type="Aspheric", k=0, use_ai2=False, ai=[0.001], ai4=0.002)
    assert "conflicting_asphere_coefficients" in errors(parse_deeplens_json(prescription))


@pytest.mark.parametrize("invalid_flag", [None, 0, 1, "false", [], {}])
def test_present_nonboolean_asphere_flag_blocks_even_with_named_a2(prescription, invalid_flag):
    prescription["surfaces"][1].update(
        type="Aspheric", k=0, use_ai2=invalid_flag, ai=[0.001], ai2=0.001
    )
    parsed = parse_deeplens_json(prescription)
    assert "invalid_surface_values" in errors(parsed)
    assert parsed["status"] == "blocked"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ({"roc": 50.0}, "conflicting_curvature"),
        ({"d": 2.0}, "conflicting_axial_position"),
        ({"mat1": "1.6/40"}, "conflicting_incident_medium"),
        ({"d_next": -1.0}, "negative_thickness"),
        ({"pos_xy": [1, 0]}, "unsupported_decenter_or_tilt"),
        ({"vec_local": [0, 0, -1]}, "unsupported_decenter_or_tilt"),
        ({"is_square": True}, "unsupported_square_aperture"),
        ({"diffraction": True}, "unsupported_diffraction"),
        ({"type": "ThinLens"}, "unsupported_surface_type"),
        ({"type": "Mirror"}, "unsupported_surface_type"),
        ({"unknown_pickup": 2}, "unsupported_surface_records"),
        ({"mat2": "generic_plastic"}, "unresolved_material"),
        ({"mat2": "hk51"}, "conflicting_source_material"),
        ({"mat2": "1.5/0"}, "invalid_inline_material"),
        ({"c": float("nan")}, "invalid_surface_values"),
        ({"is_aperture": "false"}, "invalid_aperture_flag"),
    ],
)
def test_optical_ambiguities_and_unsupported_operations_block(prescription, mutation, code):
    prescription["surfaces"][1].update(mutation)
    parsed = parse_deeplens_json(prescription)
    assert parsed["status"] == "blocked"
    assert code in errors(parsed)
    json.dumps(parsed, allow_nan=False)


def test_global_doe_and_sensor_conflicts_block(prescription):
    prescription["DOE"] = {"phase_model": "binary"}
    assert "unsupported_global_records" in errors(parse_deeplens_json(prescription))
    del prescription["DOE"]
    prescription["d_sensor"] = 42.5
    assert "conflicting_sensor_position" in errors(parse_deeplens_json(prescription))


def test_informational_parenthesized_curvature_does_not_override_roc(prescription):
    s = prescription["surfaces"][2]
    s["(c)"] = -0.02  # A rounded informational field, unlike active c.
    result = parse_deeplens_json(prescription)
    assert result["status"] == "parsed"
    assert result["surfaces"][3]["radius_mm"] == -40
    assert result["surfaces"][3]["raw"]["(c)"] == -0.02


def test_source_object_and_wavelength_units(prescription):
    prescription.update(obj_depth=-1000.0, primary_wvln=0.5876, wvln_rgb=[0.6563, 0.5876, 0.4861])
    parsed = parse_deeplens_json(prescription)
    assert parsed["surfaces"][0]["thickness_mm"] == 1000.0
    assert parsed["metadata"]["primary_wavelength_nm"] == pytest.approx(587.6)
    assert parsed["metadata"]["wavelengths_nm"] == pytest.approx([656.3, 587.6, 486.1])
    prescription["wvln_rgb"] = [0.5, 0.6]
    assert "invalid_wavelength" in errors(parse_deeplens_json(prescription))


def test_exact_pinned_polymer_is_not_a_placeholder(prescription):
    prescription["surfaces"][1]["mat2"] = "COC"
    prescription["surfaces"][2]["mat1"] = "coc"
    parsed = parse_deeplens_json(prescription)
    assert parsed["status"] == "parsed"
    row = parsed["surfaces"][2]
    assert row["nd"] == 1.533732 and row["vd"] == 56.227932
    assert row["material_model"] == "deeplens_exact_catalog"
    dispersion = row["material_resolution"]["dispersion"]
    assert dispersion["formula"] == "schott" and len(dispersion["coefficients"]) == 6
    assert dispersion["commit"] == DEEPLENS_COMMIT
    prescription["surfaces"][1]["mat2"] = "c-o-c"
    assert "unresolved_material" in errors(parse_deeplens_json(prescription))


def test_all_pinned_catalogue_indices_agree_with_coefficients():
    for _, nd, vd, mode, a in _MATERIALS.values():

        def index(w, mode=mode, a=a):
            w2 = w * w
            if mode == 2:
                square = 1 + sum(a[i] * w2 / (w2 - a[i + 1]) for i in (0, 2, 4))
            else:
                square = a[0] + a[1] * w2 + (a[2] + (a[3] + (a[4] + a[5] / w2) / w2) / w2) / w2
            return math.sqrt(square)

        calculated_nd = index(0.5875618)
        calculated_vd = (calculated_nd - 1) / (index(0.4861327) - index(0.6562725))
        assert calculated_nd == pytest.approx(nd, abs=5e-7)
        assert calculated_vd == pytest.approx(vd, abs=0.005)


@pytest.mark.parametrize("content", ["", "[]", '{"surfaces":[],"surfaces":[1]}', "{bad"])
def test_malformed_or_duplicate_json_blocks(content):
    result = parse_deeplens_text(content)
    assert result["status"] == "blocked"


def test_parser_preserves_input_and_file_provenance(prescription, tmp_path):
    original = copy.deepcopy(prescription)
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(prescription))
    result = parse_deeplens_file(path)
    result["metadata"]["raw_source"]["surfaces"][0]["r"] = 999
    assert prescription == original
    assert result["metadata"]["source_filename"] == path.name


def test_dispatch_preserves_explicit_format_and_single_configuration(prescription, tmp_path):
    content = json.dumps(prescription)
    result = parse_lens_text(content, source_format="deeplens_json")
    assert result["format"] == "deeplens_json" and result["status"] == "parsed"
    invalid = parse_lens_text(content, source_format="deeplens_json", configuration_index=1)
    assert invalid["status"] == "blocked"
    path = tmp_path / "model.json"
    path.write_text(content)
    assert len(parse_lens_configurations(path, source_format="deeplens_json")) == 1


def test_acquired_catalogue_evidence_matches_pinned_hashes_if_present():
    root = (
        Path(__file__).resolve().parents[1]
        / "camerae2e_db/sources/deeplens/metadata/deeplens/material"
    )
    if not root.exists():
        pytest.skip("Acquired source evidence not available in this checkout")
    for name, expected in _CATALOG_HASHES.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected

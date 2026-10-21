from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisetcam.lens_source_parser import (
    parse_lens_configurations,
    parse_lens_file,
    parse_lens_text,
)
from pyisetcam.lens_validation import normalize_lens_surfaces


def _zmx(*, unit: str = "MM", extra: str = "", curvature: str = "0.1") -> str:
    return f"""VERS 140418
MODE SEQ
NAME "Test imaging lens"
UNIT {unit}
FNUM 2
WAVM 1 0.4861327 0.5
WAVM 2 0.5875618 1
PWAV 2
SURF 0
TYPE STANDARD
CURV 0
DISZ INFINITY
DIAM 0 0 0
SURF 1
STOP
TYPE EVENASPH
CURV {curvature} 0 0 0 0 ""
PARM 1 2D-3
PARM 2 -3.5e-6
PARM 8 1e-17
CONI -1
DISZ 2
GLAS ___BLANK 1 0 1.5168 64.17 0
DIAM 2 1 0 0 1 ""
{extra}
SURF 2
TYPE STANDARD
CURV -0.1
DISZ 8
DIAM 2
SURF 3
TYPE STANDARD
CURV 0
DISZ 0
DIAM 3
"""


def _p2p(*, constants: str = "", configs: str = "", asphere: str = "") -> str:
    return f"""[descriptive data]
title\tA test lens
[constants]
{constants}
[variable distances]
Focal Length\t10
F-Number\t2
d0\tInfinity
Bf\t8
{configs}
[lens data]
1\t10\t2\t1.5168\t4\t64.17\tN-BK7\tSCHOTT
2\tAS\t0\t\t4\t
3\t-10\tBf\t\t4\t
[aspherical data]
{asphere}
[figure]
source\tfigure.png
[figure]
source\tfigure2.png
[group data]
G1\t1\t3\t10
"""


def _errors(result: dict) -> set[str]:
    return {i["code"] for i in result["issues"] if i["severity"] == "error"}


def test_zmx_curvature_plane_object_stop_image_and_aperture_conventions() -> None:
    result = parse_lens_text(_zmx())
    assert result["status"] == "parsed", result["issues"]
    obj, front, rear, img = result["surfaces"]
    assert [s["surface_kind"] for s in result["surfaces"]] == ["object", "stop", "surface", "image"]
    assert obj["radius_mm"] == 0
    assert obj["thickness_mm"] is None
    assert obj["thickness_state"] == "infinite"
    assert front["radius_mm"] == 10
    assert rear["radius_mm"] == -10
    assert front["effective_aperture_mm"] == 4
    assert front["semidiameter_mm"] == 2
    assert front["source_aperture_convention"] == "semidiameter"
    assert front["coefficients"] == {"a2": 0.002, "a4": -3.5e-6, "a16": 1e-17}
    assert front["nd"] == 1.5168
    assert rear["material"] == "AIR"
    assert img["radius_mm"] == 0
    meta = result["metadata"]
    assert meta["wavelengths_nm"] == pytest.approx([486.1327, 587.5618])
    assert meta["wavelength_records"][0]["weight"] == 0.5
    assert meta["primary_wavelength_index"] == 2
    assert meta["object_distance_state"] == "infinite"
    assert "focal_length_mm" not in meta
    json.dumps(result, allow_nan=False)
    normalized = normalize_lens_surfaces(result["surfaces"])
    assert normalized[0]["thickness_state"] == "infinite"
    assert normalized[1]["coefficients"]["a2"] == 0.002


@pytest.mark.parametrize("unit,scale", [("MM", 1), ("CM", 10), ("IN", 25.4), ("M", 1000)])
def test_zmx_units_rescale_radius_gaps_apertures_and_each_aspheric_power(unit, scale) -> None:
    result = parse_lens_text(_zmx(unit=unit))
    front = result["surfaces"][1]
    assert result["status"] == "parsed", result["issues"]
    assert front["radius_mm"] == pytest.approx(10 * scale)
    assert front["thickness_mm"] == 2 * scale
    assert front["effective_aperture_mm"] == 4 * scale
    assert front["coefficients"]["a2"] == pytest.approx(0.002 / scale)
    assert front["coefficients"]["a4"] == pytest.approx(-3.5e-6 / scale**3)
    assert front["coefficients"]["a16"] == pytest.approx(1e-17 / scale**15)
    assert front["conic"] == -1
    assert result["metadata"]["wavelengths_nm"][0] == pytest.approx(486.1327)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "1e309", "1,25", "--0.1", "0.1junk"])
def test_invalid_curvature_never_becomes_an_explicit_plane(token) -> None:
    result = parse_lens_text(_zmx(curvature=token))
    assert result["status"] == "blocked"
    assert result["surfaces"][1]["radius_mm"] is None
    assert "invalid_record" in _errors(result)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "extra,code",
    [
        ("PKUP 1 0 1 0", "unsupported_solve"),
        ("SOLV 1", "unsupported_solve"),
        ("VPAR 2", "unsupported_solve"),
        ("COORDBRK 1 0 0", "unsupported_command"),
        ("UNKNOWN 1", "unsupported_command"),
        ("CONF 2 0", "unsupported_multiconfiguration"),
        ("MCON 2 1 0", "unsupported_multiconfiguration"),
        ("THIC 1 2 3", "unsupported_multiconfiguration"),
        ("OBDC 1 0", "unsupported_optical_control"),
        ("GLAS MIRROR", "unsupported_reflection"),
        ("GLAS ___BLANK 1 0 1.5 60 .02", "unsupported_material_dispersion"),
        ("DIAM 2 4 0", "unsupported_aperture"),
        ("DIAM 2 1 0 2 1", "unsupported_aperture"),
        ("DIAM -2 0", "invalid_record"),
        ("CURV .2 1", "unsupported_solve"),
        ("DISZ 2 1", "invalid_record"),
    ],
)
def test_unsupported_controls_are_retained_and_blocked(extra, code) -> None:
    result = parse_lens_text(_zmx(extra=extra))
    assert result["status"] == "blocked"
    assert code in _errors(result)
    assert any(r["raw"] == extra for r in result["metadata"]["raw_records"])


@pytest.mark.parametrize(
    "surface_type", ["COORDBRK", "XASPHERE", "XOSPHERE", "TOROIDAL", "DGRATING"]
)
def test_unsupported_surface_type_is_not_flattened(surface_type) -> None:
    result = parse_lens_text(_zmx().replace("TYPE EVENASPH", f"TYPE {surface_type}"))
    assert "unsupported_surface_type" in _errors(result)
    assert result["surfaces"][1]["surface_type"] == surface_type
    assert result["surfaces"][1]["coefficients"] == {}


def test_catalog_placeholder_nd_vd_not_silently_used_as_real_glass() -> None:
    result = parse_lens_text(_zmx().replace("___BLANK 1 0 1.5168 64.17", "N-BK7 0 0 1.5 40"))
    front = result["surfaces"][1]
    assert front["nd"] is None and front["vd"] is None
    assert front["material"] == "N-BK7"
    assert front["material_model"] == "catalog_required"
    assert any(i["code"] == "catalog_material_unresolved" for i in result["issues"])


def test_missing_or_unknown_units_block_and_do_not_claim_a_scale() -> None:
    for source in (_zmx(unit="FURLONG"), _zmx().replace("UNIT MM\n", "")):
        result = parse_lens_text(source)
        assert result["status"] == "blocked"
        assert result["metadata"]["source_to_mm"] is None


def test_zmx_effl_merit_operand_is_not_reported_as_a_focal_length() -> None:
    result = parse_lens_text(_zmx() + "EFFL 0 2 0 0 0 0 50 2\n")
    assert "focal_length_mm" not in result["metadata"]


def test_zmx_free_text_quotes_are_not_parsed_as_optical_operands() -> None:
    result = parse_lens_text(_zmx() + "NOTE 0 5Mpix, 1/3.6\" format\nCOMM Maker's lens\n")
    assert result["status"] == "parsed", result["issues"]


@pytest.mark.parametrize("clear_radius", [1.5, 3])
def test_zmx_physical_circular_aperture_is_distinct_from_computed_semidiameter(
    clear_radius,
) -> None:
    result = parse_lens_text(_zmx(extra=f"CLAP 0 {clear_radius} 0"))
    assert result["status"] == "parsed", result["issues"]
    row = result["surfaces"][1]
    assert row["computed_semidiameter_mm"] == 2
    assert row["clear_aperture_radius_mm"] == clear_radius
    assert row["effective_aperture_mm"] == clear_radius * 2
    assert row["semidiameter_mm"] == clear_radius


def test_zmx_central_obscuration_is_not_silently_erased() -> None:
    result = parse_lens_text(_zmx(extra="CLAP 1 2 0"))
    assert "unsupported_aperture" in _errors(result)


def test_ftyp_active_wavelength_count_omits_padded_entries_but_preserves_raw() -> None:
    source = _zmx().replace("PWAV 2", "WAVM 3 0.55 1\nPWAV 2\nFTYP 0 0 1 2 0 0 0")
    result = parse_lens_text(source)
    assert result["status"] == "parsed"
    assert len(result["metadata"]["wavelengths_nm"]) == 2
    assert len(result["metadata"]["wavelength_records"]) == 3


def test_unit_conversion_overflow_stays_json_safe_and_blocks() -> None:
    source = _zmx().replace("WAVM 1 0.4861327 0.5", "WAVM 1 1e308 0.5")
    result = parse_lens_text(source)
    assert "numeric_conversion_overflow" in _errors(result)
    json.dumps(result, allow_nan=False)


def test_explicit_zero_curvature_is_a_plane_and_negative_gaps_are_blocked() -> None:
    result = parse_lens_text(_zmx(curvature="-0"))
    assert result["surfaces"][1]["radius_mm"] == 0
    negative = parse_lens_text(_zmx().replace("DISZ 8", "DISZ -8"))
    assert "negative_thickness" in _errors(negative)
    assert negative["surfaces"][2]["thickness_mm"] == -8


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "cp1252"])
def test_supported_file_encodings(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "lens.zmx"
    path.write_bytes(_zmx().replace("Test imaging", "Tést imaging").encode(encoding))
    result = parse_lens_file(path)
    assert result["status"] == "parsed", result["issues"]
    assert result["metadata"]["title"] == "Tést imaging lens"


def test_p2p_full_diameter_numeric_material_and_explicit_variables() -> None:
    result = parse_lens_text(_p2p(asphere="1\t10\t-1\t1e-5\t2e-7\t\t"))
    assert result["status"] == "parsed", result["issues"]
    obj, front, stop, rear, img = result["surfaces"]
    assert front["effective_aperture_mm"] == 4 and front["semidiameter_mm"] == 2
    assert front["source_aperture_convention"] == "diameter"
    assert front["coefficients"] == {"a4": 1e-5, "a6": 2e-7}
    assert front["nd"] == 1.5168 and front["vd"] == 64.17
    assert front["material_catalog"] == "SCHOTT"
    assert stop["surface_kind"] == "stop" and stop["radius_mm"] == 0
    assert rear["thickness_mm"] == 8
    assert rear["raw"]["source_thickness"] == "Bf"
    assert obj["thickness_state"] == "infinite"
    assert img["surface_kind"] == "image"
    assert result["metadata"]["focal_length_mm"] == 10
    assert "wavelengths_nm" not in result["metadata"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "constant,expected",
    [
        ("AsphericalA2\t1", {"a2": 1e-5, "a4": 2e-7}),
        ("AsphericalOddCount\t2", {"a3": 1e-5, "a4": 2e-7}),
    ],
)
def test_p2p_asphere_order_flags_are_not_confused(constant, expected) -> None:
    result = parse_lens_text(_p2p(constants=constant, asphere="1\t10\t-1\t1e-5\t2e-7"))
    assert result["status"] == "parsed", result["issues"]
    assert result["surfaces"][1]["coefficients"] == expected


def test_p2p_multiple_configurations_expanded_by_explicit_column(tmp_path: Path) -> None:
    text = (
        _p2p()
        .replace("Focal Length\t10", "Focal Length\t10\t20")
        .replace("Bf\t8", "Bf\t8\t18")
        .replace("d0\tInfinity", "d0\tInfinity\t100")
    )
    blocked = parse_lens_text(text)
    assert "unsupported_multiconfiguration" in _errors(blocked)
    assert blocked["surfaces"][3]["thickness_mm"] is None
    path = tmp_path / "lens.txt"
    path.write_text(text)
    results = parse_lens_configurations(path)
    assert len(results) == 2
    assert [r["status"] for r in results] == ["parsed", "parsed"]
    assert [r["metadata"]["configuration_index"] for r in results] == [0, 1]
    assert [r["metadata"]["focal_length_mm"] for r in results] == [10, 20]
    assert [r["surfaces"][3]["thickness_mm"] for r in results] == [8, 18]
    assert results[1]["metadata"]["object_distance_mm"] == 100
    assert results[1]["metadata"]["variable_distances"]["Bf"] == ["8", "18"]


def test_p2p_trailing_tsv_padding_does_not_create_phantom_configuration(tmp_path: Path) -> None:
    text = (
        _p2p()
        .replace("Focal Length\t10", "Focal Length\t10\t20")
        .replace("Bf\t8", "Bf\t8\t18\t")
        .replace("d0\tInfinity", "d0\tInfinity\t100")
    )
    path = tmp_path / "lens.txt"
    path.write_text(text)
    results = parse_lens_configurations(path)
    assert len(results) == 2
    assert all(r["status"] == "parsed" for r in results)
    assert results[1]["surfaces"][3]["thickness_mm"] == 18
    assert results[1]["metadata"]["variable_distances"]["Bf"] == ["8", "18", ""]
    path.write_text(text.replace("Bf\t8\t18\t", "Bf\t8\t\t"))
    missing = parse_lens_configurations(path)
    assert len(missing) == 2
    assert missing[1]["status"] == "blocked"
    assert missing[1]["surfaces"][3]["thickness_mm"] is None


def test_p2p_configuration_aperture_overrides_only_aperture_stop(tmp_path: Path) -> None:
    text = (
        _p2p()
        .replace("Focal Length\t10", "Focal Length\t10\t20")
        .replace("[variable distances]", "[variable distances]\nAperture Diameter\t4\t6")
    )
    path = tmp_path / "lens.txt"
    path.write_text(text)
    results = parse_lens_configurations(path)
    assert [r["surfaces"][2]["effective_aperture_mm"] for r in results] == [4, 6]
    assert [r["surfaces"][2]["semidiameter_mm"] for r in results] == [2, 3]
    assert results[1]["surfaces"][1]["effective_aperture_mm"] == 4
    assert results[1]["surfaces"][2]["raw"]["lens_data_diameter"] == "4"
    path.write_text(text.replace("\tAS\t", "\tFS\t"))
    field_stop = parse_lens_configurations(path)
    assert [r["surfaces"][2]["effective_aperture_mm"] for r in field_stop] == [4, 4]


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda t: t.replace("Bf\t8", "Bf\tmissing"), "unresolved_thickness"),
        (lambda t: t.replace("Bf\t8", "Bf\td1\nd1\tBf"), "unresolved_thickness"),
        (lambda t: t + "[diffractive data]\n1\t550\t1\t0.01\n", "unsupported_section"),
        (lambda t: t + "[aspherical data(p)]\n1\t10\t-1\t.01\n", "unsupported_section"),
        (
            lambda t: t.replace("[constants]", "[constants]\nUnknownEquation\t1"),
            "unsupported_constant",
        ),
        (lambda t: t.replace("1\t10\t2", "1\tundefined\t2"), "unresolved_radius"),
        (lambda t: t.replace("1\t10\t2", "1\t10\tundefined"), "unresolved_thickness"),
    ],
)
def test_p2p_unsupported_or_unresolved_source_data_is_never_filled_with_zero(change, code) -> None:
    result = parse_lens_text(change(_p2p()))
    assert result["status"] == "blocked"
    assert code in _errors(result)
    json.dumps(result, allow_nan=False)


def test_p2p_asphere_conflict_and_internal_blank_are_blocked() -> None:
    conflict = parse_lens_text(_p2p(asphere="1\t11\t0\t1e-5"))
    assert "asphere_radius_conflict" in _errors(conflict)
    assert conflict["surfaces"][1]["radius_mm"] == 10
    missing = parse_lens_text(_p2p(asphere="1\t10\t0\t\t1e-5"))
    assert "missing_asphere_coefficient" in _errors(missing)


def test_unknown_object_distance_not_promoted_to_infinity() -> None:
    result = parse_lens_text(_p2p().replace("d0\tInfinity\n", ""))
    assert result["metadata"]["object_distance_state"] == "unknown"
    assert result["metadata"]["object_distance_mm"] is None
    assert any(i["code"] == "object_distance_missing" for i in result["issues"])


def test_empty_or_non_prescription_input_is_blocked() -> None:
    for text in ("", "<html>Please log in</html>", "[descriptive data]\ntitle\tEmpty"):
        assert parse_lens_text(text)["status"] == "blocked"

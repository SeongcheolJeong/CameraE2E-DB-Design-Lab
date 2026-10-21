from copy import deepcopy

import pytest

from pyisetcam.lens_source_identity import optical_fingerprint
from pyisetcam.lens_source_parser import parse_lens_text

ZMX = """VERS 190513 19 0
MODE SEQ
NAME Test singlet
UNIT MM
ENPD 5
FTYP 0 0 3 1 0 0 0
XFLN 0 0 0
YFLN 0 10 20
FWGN 1 1 1
WAVM 1 0.5875618 1
SURF 0
 TYPE STANDARD
 CURV 0
 DISZ INFINITY
SURF 1
 TYPE STANDARD
 STOP
 CURV 0.02
 DISZ 3
 GLAS ___BLANK 1 0 1.5168 64.17
 DIAM 2.5
SURF 2
 TYPE STANDARD
 CURV -0.02
 DISZ 48
 DIAM 2.5
SURF 3
 TYPE STANDARD
 CURV 0
 DISZ 0
 DIAM 5
"""

P2P = """[descriptive data]
title\tOriginal test
[constants]
[variable distances]
Focal Length\t50
F-Number\t2.8
Angle of View\t40
d0\tInfinity
[lens data]
1\t50\t3\t1.5\t5\t60
2\t-50\t48\t\t5\t
[figure]
source\tData/old.png
[notes]
Attribution text.
"""


def test_documentary_and_exact_numeric_spellings_are_not_optical_changes():
    first = parse_lens_text(ZMX)
    second = parse_lens_text(
        ZMX.replace("VERS 190513 19 0", "VERS 220726 155 123")
        .replace("NAME Test singlet", "NAME Renamed\nNOTE 0 New attribution")
        .replace("CURV 0.02", " CURV 2.000e-2")
        .replace("DISZ 3\n", "DISZ 3.000D0\n")
    )
    second["metadata"].update(
        source_filename="renamed.zmx", source_path="elsewhere", encoding="utf-16"
    )
    first["metadata"].update(source_filename="original.zmx", source_path="here", encoding="utf-8")
    assert first["status"] == second["status"] == "parsed"
    assert optical_fingerprint(first) == optical_fingerprint(second)


@pytest.mark.parametrize(
    "before,after",
    [
        ("ENPD 5", "ENPD 4"),
        ("FTYP 0 0 3 1 0 0 0", "FTYP 1 0 3 1 0 0 0"),
        ("YFLN 0 10 20", "YFLN 0 10 21"),
        ("XFLN 0 0 0", "XFLN 0 1 0"),
        ("FWGN 1 1 1", "FWGN 1 0.5 1"),
        ("WAVM 1 0.5875618 1", "WAVM 1 0.55 1"),
        ("DIAM 2.5", "DIAM 2.4"),
        ("CURV 0.02", "CURV 0.0200000000000000000000000000000000001"),
    ],
)
def test_optical_settings_and_even_sub_float_source_differences_remain(before, after):
    assert optical_fingerprint(parse_lens_text(ZMX)) != optical_fingerprint(
        parse_lens_text(ZMX.replace(before, after))
    )


def test_unknown_optical_controls_are_retained_without_line_number_noise():
    first = parse_lens_text(ZMX + "FUTURE_CONTROL 1 2\n")
    same = parse_lens_text("\n" + ZMX + "FUTURE_CONTROL 1.0 2e0\n")
    different = parse_lens_text(ZMX + "FUTURE_CONTROL 1 3\n")
    assert first["status"] == "blocked"
    assert optical_fingerprint(first) == optical_fingerprint(same)
    assert optical_fingerprint(first) != optical_fingerprint(different)


def test_environment_numeric_spelling_is_exact_and_quoted_unknown_text_is_preserved():
    first = parse_lens_text(ZMX + "ENVD 20 1 0\n")
    same = parse_lens_text(ZMX + "ENVD 2e1 1.0 0\n")
    different = parse_lens_text(ZMX + "ENVD 30 1 0\n")
    assert optical_fingerprint(first) == optical_fingerprint(same)
    assert optical_fingerprint(first) != optical_fingerprint(different)
    first = parse_lens_text(ZMX + 'FUTURE_CONTROL " name "\n')
    different = parse_lens_text(ZMX + 'FUTURE_CONTROL "name"\n')
    assert optical_fingerprint(first) != optical_fingerprint(different)


def test_p2p_titles_figures_notes_are_documentary_but_field_and_config_are_not():
    first = parse_lens_text(P2P, source_format="p2p")
    second = parse_lens_text(
        P2P.replace("Original test", "Renamed")
        .replace("Data/old.png", "Data/new.png")
        .replace("Attribution text.", "Different notes.")
        .replace("50\t3\t1.5", "5e1\t3.0\t1.50"),
        source_format="p2p",
    )
    assert optical_fingerprint(first) == optical_fingerprint(second)
    field = parse_lens_text(P2P.replace("View\t40", "View\t41"), source_format="p2p")
    assert optical_fingerprint(first) != optical_fingerprint(field)
    config = deepcopy(first)
    config["metadata"]["configuration_index"] = 1
    assert optical_fingerprint(first) != optical_fingerprint(config)


def test_unknown_sections_and_properties_and_format_are_not_discarded():
    first = parse_lens_text(P2P + "[future optics]\nsetting\t1\n", source_format="p2p")
    second = parse_lens_text(P2P + "[future optics]\nsetting\t2\n", source_format="p2p")
    assert optical_fingerprint(first) != optical_fingerprint(second)
    different_format = deepcopy(first)
    different_format["format"] = "zmx"
    assert optical_fingerprint(first) != optical_fingerprint(different_format)
    different_property = deepcopy(first)
    different_property["metadata"]["future_pupil_setting"] = 2
    assert optical_fingerprint(first) != optical_fingerprint(different_property)


def test_fingerprint_is_pure_and_deterministic():
    parsed = parse_lens_text(ZMX)
    before = deepcopy(parsed)
    assert optical_fingerprint(parsed) == optical_fingerprint(deepcopy(parsed))
    assert parsed == before

"""Prevent wider source sampling from silently tracing a different optical model."""

import copy
import importlib.util
from pathlib import Path

import pytest

from pyisetcam.lens_source_parser import parse_lens_text

pytest.importorskip("rayoptics")
_SPEC = importlib.util.spec_from_file_location(
    "public_lens_raytrace",
    Path(__file__).resolve().parents[2] / "tools/verify_public_lens_raytrace.py",
)
_TOOL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TOOL)

_SOURCE = """VERS 190513 19 0
MODE SEQ
UNIT MM
FNUM 4
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
 DIAM 5
SURF 2
 TYPE STANDARD
 CURV -0.02
 DISZ 48
 DIAM 5
SURF 3
 TYPE STANDARD
 CURV 0
 DISZ 0
"""


def test_supported_reference_constructs_with_source_stop():
    parsed = parse_lens_text(_SOURCE)
    model, _ = _TOOL.build_normalized_model(parsed)
    assert model.seq_model.stop_surface == 1
    assert model.seq_model.ifcs[1].profile.cv == pytest.approx(0.02)


@pytest.mark.parametrize(
    "change,match",
    [
        ("odd", "Odd-order"),
        ("no_stop", "explicit aperture stop"),
        ("object_glass", "air object medium"),
    ],
)
def test_unsupported_trace_semantics_are_rejected(change, match):
    parsed = copy.deepcopy(parse_lens_text(_SOURCE))
    if change == "odd":
        parsed["surfaces"][1]["is_aspheric"] = True
        parsed["surfaces"][1]["coefficients"] = {"a3": 1e-5}
    elif change == "no_stop":
        parsed["surfaces"][1]["surface_kind"] = "surface"
    else:
        parsed["surfaces"][0]["nd"] = 1.33
    with pytest.raises(ValueError, match=match):
        _TOOL.build_normalized_model(parsed)

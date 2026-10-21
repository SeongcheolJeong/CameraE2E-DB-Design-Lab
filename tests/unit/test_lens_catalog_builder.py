import importlib.util
import sys
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "tools/build_lens_patent_simulation_db.py"
spec = importlib.util.spec_from_file_location("lens_catalog_builder_test", path)
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


def test_plane_exit_interface_refracts_paraxial_slope():
    rows = [
        {"radius": "50", "thickness": "5", "nd": "1.5", "vd": "60"},
        {"radius": "Infinity", "thickness": "95", "nd": ""},
    ]
    # Plano-convex lens in air: f=R/(n-1), independent of center thickness.
    assert builder.paraxial_effective_focal_length_mm(rows) == pytest.approx(100.0)


def test_quadratic_sag_term_contributes_to_paraxial_curvature():
    rows = [
        {"radius": "Infinity", "a2": ".01", "thickness": "5", "nd": "1.5"},
        {"radius": "Infinity", "thickness": "95", "nd": ""},
    ]
    assert builder.paraxial_effective_focal_length_mm(rows) == pytest.approx(100.0)


@pytest.mark.parametrize("thickness", ["(Variable)", "", "Infinity"])
def test_unresolved_gap_is_not_silently_a_zero_gap(thickness):
    rows = [
        {"radius": "50", "thickness": thickness, "nd": "1.5"},
        {"radius": "-50", "thickness": "50", "nd": ""},
    ]
    assert builder.paraxial_effective_focal_length_mm(rows) is None


def test_explicit_image_context_does_not_require_a_following_gap():
    rows = [
        {"radius": "50", "thickness": "5", "nd": "1.5", "vd": "60"},
        {"radius": "Infinity", "thickness": "95", "nd": ""},
        {"surface_kind": "image", "radius": "Infinity", "thickness": "", "nd": ""},
    ]
    assert builder.paraxial_effective_focal_length_mm(rows) == pytest.approx(100.0)

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("rayoptics")

from simulations.rayoptics.backend.app.service import RayOpticsStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB = (
    ROOT
    / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents"
    / "lens_patent_simulation_v9.sqlite"
)


def _store() -> RayOpticsStore:
    # These construction helpers do not need workbench runtime directories.
    return RayOpticsStore.__new__(RayOpticsStore)


def _model_row() -> dict:
    return {
        "configuration": "base",
        "company": "Test",
        "publication_number": "test",
        "example_label": "test",
        "focal_length_mm": 10.0,
        "f_number": 4.0,
        "image_height_mm": 1.0,
        "half_field_deg": None,
    }


def _surfaces() -> list[dict]:
    records = [
        ("1", "surface", "10", "2", "1.5", "60"),
        ("ST", "stop", "Infinity", "0.1", "", ""),
        ("3", "surface", "-10", "8", "", ""),
        ("IM", "image", "Infinity", "", "", ""),
    ]
    return _store()._normalized_surface_csv_rows(
        [
            {
                "surface_order": str(i),
                "surface_label": label,
                "surface_key": label,
                "surface_kind": kind,
                "radius": radius,
                "thickness": thickness,
                "nd": nd,
                "vd": vd,
                "effective_aperture": "",
                "is_aspheric": "0",
            }
            for i, (label, kind, radius, thickness, nd, vd) in enumerate(records, 1)
        ]
    )


@pytest.mark.parametrize("simulation_id", ["p0247:base", "p0371:base"])
def test_backend_rejects_corrupted_source_before_constructing_model(simulation_id: str) -> None:
    if not DB.is_file():
        pytest.skip("Bundled regression DB unavailable")
    with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM simulation_results WHERE simulation_id=?", (simulation_id,)
        ).fetchone()
        surfaces = con.execute(
            "SELECT * FROM lens_surfaces WHERE lens_id=? AND configuration=? "
            "ORDER BY surface_order",
            (row["lens_id"], row["configuration"]),
        ).fetchall()
    with pytest.raises(ValueError, match="failed structural validation"):
        _store()._build_lens_patent_model(row, surfaces, [])


def test_backend_adapter_preserves_plane_and_unresolved_tokens() -> None:
    surfaces = _surfaces()
    assert json.loads(surfaces[1]["raw_json"])["radius"] == "Infinity"
    surfaces[1]["thickness_mm"] = None
    raw = json.loads(surfaces[1]["raw_json"])
    raw["thickness"] = "Variable"
    surfaces[1]["raw_json"] = json.dumps(raw)
    with pytest.raises(ValueError, match="unresolved_thickness"):
        _store()._build_lens_patent_model(_model_row(), surfaces, [])


def test_backend_applies_coefficients_without_flag_and_records_proxy_assumptions() -> None:
    store = _store()
    surfaces = _surfaces()
    surfaces[0]["coefficients_json"] = json.dumps({"k": -1, "a4": 1.2e-6})
    surfaces[0]["is_aspheric"] = 0
    warnings: list[str] = []
    model = store._build_lens_patent_model(_model_row(), surfaces, warnings)
    assert model.seq_model.ifcs[1].profile.coefs[1] == pytest.approx(1.2e-6)
    assert model.seq_model.ifcs[1].profile.cc == -1
    provenance = model["analysis_results"]["patent_optical_assumptions"]
    assert provenance["fallback_aperture_used"]
    assert not provenance["image_quality_validated"]
    assert any("asphere_flag_conflict" in text for text in warnings)


def test_backend_wavelength_selection_preserves_patent_reference_geometry() -> None:
    store = _store()
    model = store._build_lens_patent_model(_model_row(), _surfaces(), [])
    model.update_model()
    original_efl = store._first_order_values(model)["efl"]
    store._select_analysis_wavelength(model, 0)
    model.update_model()
    assert model.optical_spec.spectral_region.central_wvl == 550.0
    assert store._first_order_values(model)["efl"] == pytest.approx(original_efl)
    assert model["analysis_results"]["patent_analysis_wavelength"]["selected_nm"] == 450.0

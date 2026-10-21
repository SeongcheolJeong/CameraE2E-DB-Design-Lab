from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from pyisetcam.lens_patents import (
    lens_patent_default_db_path,
    lens_patent_downsample_psf,
    lens_patent_optics,
    lens_patent_raytrace_optics,
    lens_patent_search,
    lens_patent_validation,
)


@pytest.fixture
def checked_asset(tmp_path):
    """A small declared-state asset, independent of optional patent packages."""
    db = tmp_path / "lens.sqlite"
    surfaces = [
        {"surface_kind": "stop", "radius": "Infinity", "thickness": "0", "nd": ""},
        {"surface_kind": "surface", "radius": "50", "thickness": "5", "nd": "1.5"},
        {"surface_kind": "surface", "radius": "-50", "thickness": "47.46", "nd": ""},
        {"surface_kind": "image", "radius": "Infinity", "thickness": "0", "nd": ""},
    ]
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE simulation_results(simulation_id TEXT, lens_id TEXT, configuration TEXT, "
            "focal_length_mm REAL, f_number REAL, optics_json TEXT, simulation_model TEXT, "
            "simulation_status TEXT);"
            "CREATE TABLE lens_surfaces(lens_id TEXT,configuration TEXT,surface_order INTEGER,"
            "radius_mm REAL,thickness_mm REAL,nd REAL,vd REAL,effective_aperture_mm REAL,"
            "is_aspheric INTEGER,coefficients_json TEXT,raw_json TEXT);"
        )
        con.execute(
            "INSERT INTO simulation_results VALUES (?,?,?,?,?,?,?,?)",
            ("demo:base", "demo", "base", 50.8474576, 4.,
             json.dumps({"focal_length_m": .0508474576, "f_number": 4.}),
             "thin_lens_proxy", "camerae2e_ready"),
        )
        for i, raw in enumerate(surfaces, 1):
            raw.update(surface_order=i, surface_key=str(i), is_aspheric="0",
                       effective_aperture="15", vd="60" if raw["nd"] else "")
            con.execute(
                "INSERT INTO lens_surfaces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("demo", "base", i, None if raw["radius"] == "Infinity" else float(raw["radius"]),
                 float(raw["thickness"]), float(raw["nd"]) if raw["nd"] else None,
                 60. if raw["nd"] else None, 15., 0, "{}", json.dumps(raw)),
            )
    validation = lens_patent_validation("demo:base", db_path=db)
    assert validation["valid"], validation
    raytrace = {
        "contract_version": 2, "physics_policy": "strict",
        "coordinate_convention": "pixel_centers", "psf_reference_axis": "+x",
        "row_axis": "y_up", "radiometry_status": "unknown",
        "source_prescription_sha256": validation["prescription_sha256"],
        "object_distance_m": float("inf"), "effective_focal_length_m": .0508474576,
        "effective_f_number": 4., "max_fov_deg": 10.,
    }
    metadata = {
        "model": "raytrace", "focal_length_m": .0508474576, "f_number": 4.,
        "raytrace": raytrace, "transmittance": {"wave": [500., 600.], "scale": [1., 1.]},
        "lens_patent_raytrace_psf": {"simulation_id": "demo:base"},
    }
    arrays = {
        "psf_function": np.ones((4, 4, 3, 2)) / 16,
        "sample_spacing_mm": np.array([.001, .001]),
        "field_height_mm": np.array([0., 1., 2.]),
        "wavelength_nm": np.array([500., 600.]),
        "geometry_function": np.array([[0., 0.], [.9, .91], [1.8, 1.82]]),
        "relative_illumination_function": np.ones((3, 2)),
        "optics_json": np.array(json.dumps(metadata)),
    }
    np.savez_compressed(tmp_path / "demo.npz", **arrays)
    (tmp_path / "manifest.json").write_text(json.dumps({"rows": [{
        "simulation_id": "demo:base", "status": "generated", "file": "demo.npz",
        "contract_version": 2,
    }]}))
    return db, tmp_path, arrays


def test_shipped_corrupt_prescription_is_not_an_approved_input():
    validation = lens_patent_validation("p0247:base")
    assert not validation["valid"]
    with pytest.raises(ValueError, match="Invalid lens prescription"):
        lens_patent_optics("p0247:base")
    rows = lens_patent_search(company="Ricoh", require_camerae2e=True)
    assert "p0247:base" not in {row["simulation_id"] for row in rows}


def test_legacy_asset_requires_explicit_opt_in():
    old = lens_patent_default_db_path().parent / "raytrace_psf"
    with pytest.raises(ValueError, match="Legacy PSF"):
        lens_patent_raytrace_optics("p0014:intermediate", psf_dir=old)


def test_unknown_radiometry_is_explicit_and_recorded(checked_asset):
    db, root, _ = checked_asset
    with pytest.raises(ValueError, match="no validated relative illumination"):
        lens_patent_raytrace_optics("demo:base", psf_dir=root, db_path=db)
    optics = lens_patent_raytrace_optics(
        "demo:base", psf_dir=root, db_path=db, allow_approximate_radiometry=True
    )
    assert optics["fidelity"] == "geometric"
    assert optics["raytrace"]["radiometry_assumption_accepted"]
    assert len(optics["raytrace"]["asset_sha256"]) == 64


def test_changed_prescription_invalidates_saved_psf(checked_asset):
    db, root, _ = checked_asset
    with sqlite3.connect(db) as con:
        con.execute("UPDATE lens_surfaces SET radius_mm=51 WHERE surface_order=2")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        lens_patent_raytrace_optics(
            "demo:base", psf_dir=root, db_path=db, allow_approximate_radiometry=True
        )


def test_swapped_asset_identity_is_rejected(checked_asset):
    db, root, arrays = checked_asset
    metadata = json.loads(str(arrays["optics_json"]))
    metadata["lens_patent_raytrace_psf"]["simulation_id"] = "different:base"
    arrays["optics_json"] = np.array(json.dumps(metadata))
    np.savez_compressed(root / "demo.npz", **arrays)
    with pytest.raises(ValueError, match="identity mismatch"):
        lens_patent_raytrace_optics("demo:base", psf_dir=root, db_path=db)


def test_psf_downsample_integrates_area_without_invented_blur():
    psf = np.zeros((4, 4, 1, 1))
    psf[0, 0, 0, 0], psf[3, 3, 0, 0] = .25, .75
    actual = lens_patent_downsample_psf(psf, 2)[:, :, 0, 0]
    np.testing.assert_array_equal(actual, [[.25, 0], [0, .75]])
    np.testing.assert_allclose(lens_patent_downsample_psf(psf, 3).sum(), 1., atol=1e-15)
    with pytest.raises(ValueError, match="positive energy"):
        lens_patent_downsample_psf(np.zeros_like(psf), 2)

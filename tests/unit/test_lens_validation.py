from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from pyisetcam.lens_validation import validate_lens_prescription

ROOT = Path(__file__).resolve().parents[2]
DB = (
    ROOT
    / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents"
    / "lens_patent_simulation_v9.sqlite"
)


def _prescription() -> list[dict[str, str]]:
    return [
        {
            "surface_order": "1",
            "surface_key": "1",
            "surface_kind": "surface",
            "radius": "10",
            "thickness": "2",
            "nd": "1.5",
            "vd": "60",
        },
        {
            "surface_order": "2",
            "surface_key": "ST",
            "surface_kind": "stop",
            "radius": "Infinity",
            "thickness": "0",
        },
        {
            "surface_order": "3",
            "surface_key": "3",
            "surface_kind": "surface",
            "radius": "-10",
            "thickness": "8",
            "nd": "",
            "vd": "",
        },
        {
            "surface_order": "4",
            "surface_key": "IM",
            "surface_kind": "image",
            "radius": "Infinity",
            "thickness": "",
        },
    ]


def _codes(result: dict, severity: str = "error") -> set[str]:
    return {issue["code"] for issue in result["issues"] if issue["severity"] == severity}


def test_explicit_infinity_and_air_gaps_are_not_missing_geometry() -> None:
    result = validate_lens_prescription(_prescription(), focal_length_mm=10, f_number=2)
    assert result["valid"]
    assert not result["image_quality_validated"]
    assert "material_identity_unknown" in _codes(result, "warning")
    missing = _prescription()
    missing[0]["radius"] = ""
    assert "unresolved_radius" in _codes(validate_lens_prescription(missing))


def test_unknown_stop_radius_requires_explicit_assumption_but_gap_must_resolve() -> None:
    rows = _prescription()
    rows[1]["radius"] = ""
    assert "plane_radius_inferred" in _codes(validate_lens_prescription(rows), "warning")
    rows[1]["thickness"] = "Variable"
    result = validate_lens_prescription(rows)
    assert not result["valid"]
    assert "unresolved_thickness" in _codes(result)


def test_db_and_csv_semantic_fingerprints_match_and_change_with_geometry() -> None:
    rows = _prescription()
    rows[0].update({"is_aspheric": "0", "k": "-1", "a4": "1.2e-6", "a6": "0"})
    db_rows = []
    for row in rows:
        db_rows.append(
            {
                "surface_order": int(row["surface_order"]),
                "surface_key": row["surface_key"],
                "radius_mm": None if row["radius"] == "Infinity" else float(row["radius"]),
                "thickness_mm": float(row["thickness"]) if row["thickness"] else None,
                "raw_json": json.dumps(row),
                "coefficients_json": json.dumps(
                    {key: float(row[key]) for key in ("k", "a4", "a6") if key in row}
                ),
            }
        )
    csv_result = validate_lens_prescription(
        rows, focal_length_mm=10, f_number=2, configuration="base"
    )
    db_result = validate_lens_prescription(
        db_rows, focal_length_mm=10.0, f_number=2.0, configuration="base"
    )
    assert csv_result["prescription_sha256"] == db_result["prescription_sha256"]
    assert "asphere_flag_conflict" in _codes(db_result, "warning")
    db_rows[0]["radius_mm"] = 11
    assert (
        validate_lens_prescription(db_rows, focal_length_mm=10, f_number=2, configuration="base")[
            "prescription_sha256"
        ]
        != csv_result["prescription_sha256"]
    )


def test_odd_and_second_order_terms_have_case_independent_db_csv_hashes() -> None:
    csv_rows = _prescription()
    csv_rows[0].update({"K": "-1", "A2": "1e-5", "a3": "2e-6", "A7": "0", "A9": ""})
    db_rows = [dict(row) for row in csv_rows]
    for key in ("K", "A2", "a3", "A7", "A9"):
        db_rows[0].pop(key)
    db_rows[0]["coefficients_json"] = json.dumps(
        {
            "k": -1.0,
            "a2": 1e-5,
            "A3": 2e-6,
            "a7": 0.0,
        }
    )
    original = validate_lens_prescription(csv_rows)
    assert (
        original["prescription_sha256"]
        == validate_lens_prescription(db_rows)["prescription_sha256"]
    )
    assert "asphere_flag_conflict" in _codes(original, "warning")
    csv_rows[0]["a3"] = "3e-6"
    assert (
        original["prescription_sha256"]
        != validate_lens_prescription(csv_rows)["prescription_sha256"]
    )
    csv_rows[0]["a3"] = "2e-6"
    csv_rows[0].pop("A9")
    assert (
        original["prescription_sha256"]
        == validate_lens_prescription(csv_rows)["prescription_sha256"]
    )


@pytest.mark.parametrize("key", ["a2", "A3", "a21"])
def test_all_polynomial_orders_require_finite_numeric_values(key: str) -> None:
    rows = _prescription()
    rows[0][key] = "NaN"
    assert "invalid_asphere_coefficient" in _codes(validate_lens_prescription(rows))
    rows[0][key] = "Infinity"
    assert "invalid_asphere_coefficient" in _codes(validate_lens_prescription(rows))


def test_original_builder_does_not_promote_plane_only_prescription() -> None:
    from tools.build_lens_patent_simulation_db import build_simulation_profile

    rows = _prescription()
    for row in rows:
        row["radius"] = "Infinity"
    profile = build_simulation_profile(
        rows, {"focal_length": 2.6, "f_number": 2.15}, "ready_staging"
    )
    assert profile.simulation_status == "invalid_prescription"
    assert any("no_powered_surface" in note for note in profile.notes)


def test_plane_base_quadratic_has_paraxial_power_but_higher_orders_alone_do_not() -> None:
    rows = _prescription()
    for row in rows:
        row["radius"] = "Infinity"
    rows[0]["a4"] = "1e-5"
    assert "no_powered_surface" in _codes(validate_lens_prescription(rows))
    rows[0]["A2"] = "0.05"
    assert validate_lens_prescription(rows)["valid"]
    rows[0]["radius"] = "10"
    rows[0]["A2"] = "-0.05"
    assert "no_powered_surface" in _codes(validate_lens_prescription(rows))


@pytest.mark.parametrize(
    "nd,vd,code",
    [
        ("0", "0", "invalid_refractive_index"),
        ("1.5", "0", "invalid_abbe_number"),
        ("NaN", "50", "invalid_refractive_index"),
    ],
)
def test_invalid_optical_material_values_block_geometry(nd: str, vd: str, code: str) -> None:
    rows = _prescription()
    rows[0].update({"nd": nd, "vd": vd})
    assert code in _codes(validate_lens_prescription(rows))


def test_spurious_coefficient_table_and_merged_rows_block_geometry() -> None:
    rows = _prescription()
    rows.append(
        {
            "surface_order": "5",
            "surface_key": "1",
            "surface_kind": "surface",
            "radius": "17",
            "thickness": "K",
            "nd": "0",
            "vd": "0",
            "raw_surface": "16 17 K 0 0 A4 -2.1e-6",
        }
    )
    result = validate_lens_prescription(rows)
    assert {"spurious_asphere_surface", "duplicate_surface_key", "unresolved_thickness"} <= _codes(
        result
    )
    rows = _prescription()
    rows[0]["raw_surface"] = "1 10 2 1.5 60 11* (Positive rear group) 25.81 3.21"
    assert "merged_surface_rows" in _codes(validate_lens_prescription(rows))


@pytest.mark.parametrize(
    "simulation_id,expected",
    [
        ("p0247:base", {"no_powered_surface", "merged_surface_rows"}),
        (
            "p0371:base",
            {"spurious_asphere_surface", "unresolved_thickness", "invalid_refractive_index"},
        ),
    ],
)
def test_real_corrupted_recommended_prescriptions_are_rejected(
    simulation_id: str, expected: set[str]
) -> None:
    if not DB.is_file():
        pytest.skip("Bundled v9 regression database is unavailable")
    with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM simulation_results WHERE simulation_id=?", (simulation_id,)
        ).fetchone()
        surfaces = [
            dict(surface)
            for surface in con.execute(
                "SELECT * FROM lens_surfaces WHERE lens_id=? AND configuration=? "
                "ORDER BY surface_order",
                (row["lens_id"], row["configuration"]),
            )
        ]
    result = validate_lens_prescription(
        surfaces,
        focal_length_mm=row["focal_length_mm"],
        f_number=row["f_number"],
        configuration=row["configuration"],
    )
    assert not result["valid"]
    assert expected <= _codes(result)


def test_audit_is_read_only_by_default_and_never_promotes_on_write(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "validate_lens_patent_db", ROOT / "tools/validate_lens_patent_db.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = tmp_path / "lens.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE simulation_results (
                simulation_id TEXT PRIMARY KEY, lens_id TEXT, configuration TEXT,
                company TEXT, focal_length_mm REAL, f_number REAL
            );
            CREATE TABLE lens_surfaces (
                lens_id TEXT, configuration TEXT, surface_order INTEGER, raw_json TEXT
            );
            CREATE TABLE camerae2e_quality (
                simulation_id TEXT PRIMARY KEY, recommended_for_camerae2e INTEGER
            );
            INSERT INTO simulation_results VALUES
                ('good:base', 'good', 'base', 'Test', 10, 2),
                ('bad:base', 'bad', 'base', 'Test', 10, 2);
            INSERT INTO camerae2e_quality VALUES ('good:base', 0), ('bad:base', 1);
        """)
        for lens_id in ("good", "bad"):
            for row in _prescription():
                if lens_id == "bad":
                    row["radius"] = "Infinity"
                con.execute(
                    "INSERT INTO lens_surfaces VALUES (?, 'base', ?, ?)",
                    (lens_id, int(row["surface_order"]), json.dumps(row)),
                )
    before = db.read_bytes()
    report = module.audit_database(db)
    assert db.read_bytes() == before
    assert report["summary"]["invalid_structure"] == 1
    written = module.audit_database(db, write_db=True)
    assert written["summary"]["recommendations_cleared"] == 1
    with sqlite3.connect(db) as con:
        assert (
            con.execute("SELECT SUM(recommended_for_camerae2e) FROM camerae2e_quality").fetchone()[
                0
            ]
            == 0
        )
        assert con.execute("SELECT COUNT(*) FROM prescription_validation").fetchone()[0] == 2
    assert not written["image_quality_validated"]

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "tools/repair_lens_patent_configurations.py"
spec = importlib.util.spec_from_file_location("lens_gap_repair", path)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def source_rows():
    return [
        {"surface_order": "17", "surface_key": "17", "surface_kind": "surface", "thickness": "0.8"},
        {
            "surface_order": "18",
            "surface_key": "STOP",
            "surface_kind": "stop",
            "thickness": "(Variable)",
        },
        {"surface_order": "19", "surface_key": "19", "surface_kind": "surface", "thickness": "1"},
    ]


def test_stop_gap_uses_named_variable_and_correct_zoom_state():
    variables = [
        {"parameter": "d18", "configuration": "intermediate", "value": "6.39"},
        {"parameter": "d18", "configuration": "wide_angle", "value": "3.00"},
    ]
    result = repair.plan_stop_gap_repairs(source_rows(), variables, "wide-angle")
    assert len(result) == 1
    assert result[0]["parameter"] == "d18"
    assert result[0]["value_mm"] == 3.0
    assert result[0]["source_variable_line"] == 3
    assert source_rows()[1]["thickness"] == "(Variable)"


def test_ambiguous_neighbors_or_variable_rows_are_never_guessed():
    rows = source_rows()
    variables = [{"parameter": "d18", "configuration": "base", "value": "3.0"}]
    rows[2]["surface_key"] = "20"
    assert repair.plan_stop_gap_repairs(rows, variables, "base") == []
    assert repair.plan_stop_gap_repairs(source_rows(), variables * 2, "base") == []
    assert repair.plan_stop_gap_repairs(source_rows(), variables, "telephoto") == []


def test_numeric_gap_and_missing_values_are_preserved():
    rows = source_rows()
    variables = [{"parameter": "d18", "configuration": "base", "value": "3.0"}]
    rows[1]["thickness"] = "0"
    assert repair.plan_stop_gap_repairs(rows, variables, "base") == []
    rows[1]["thickness"] = ""
    assert repair.plan_stop_gap_repairs(rows, variables, "base") == []


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path, *, two_stops=False):
    export = tmp_path / "export"
    source = export / "lens/base.csv"
    variables = export / "lens/variables.csv"
    rows = source_rows()
    if two_stops:
        rows.extend(
            [
                {
                    "surface_order": "20",
                    "surface_key": "ST20",
                    "surface_kind": "stop",
                    "thickness": "Variable",
                },
                {
                    "surface_order": "21",
                    "surface_key": "21",
                    "surface_kind": "surface",
                    "thickness": "1",
                },
            ]
        )
    _write_csv(source, rows)
    _write_csv(variables, [{"parameter": "d18", "configuration": "base", "value": "3.0"}])
    db = tmp_path / "package/lenses.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE lenses (lens_id TEXT, source_variable_distances_csv TEXT);
            CREATE TABLE simulation_results (
                simulation_id TEXT, lens_id TEXT, configuration TEXT, source_surfaces_csv TEXT
            );
            CREATE TABLE lens_surfaces (
                lens_id TEXT, configuration TEXT, surface_order INTEGER,
                thickness_mm REAL, raw_json TEXT
            );
            INSERT INTO lenses VALUES ('p0001', 'lens/variables.csv');
            INSERT INTO simulation_results VALUES ('p0001:base', 'p0001', 'base', 'lens/base.csv');
        """)
        for row in rows:
            thickness = None if row["surface_kind"] == "stop" else float(row["thickness"])
            con.execute(
                "INSERT INTO lens_surfaces VALUES ('p0001', 'base', ?, ?, ?)",
                (int(row["surface_order"]), thickness, json.dumps(row)),
            )
    return db, export, source, variables


def test_apply_preserves_source_and_is_idempotent(tmp_path):
    db, export, source, variables = _fixture(tmp_path)
    originals = source.read_bytes(), variables.read_bytes(), db.read_bytes()
    dry_run = repair.repair_database(db, export)
    assert dry_run["repaired_gaps"] == 1
    assert (source.read_bytes(), variables.read_bytes(), db.read_bytes()) == originals
    result = repair.repair_database(db, export, apply=True)
    overlay = Path(result["evidence"][0]["reviewed_surfaces_csv"])
    assert repair.read_csv(overlay)[1]["thickness"] == "3.0"
    assert (source.read_bytes(), variables.read_bytes()) == originals[:2]
    applied = db.read_bytes(), overlay.read_bytes()
    assert repair.repair_database(db, export, apply=True)["repaired_gaps"] == 0
    assert (db.read_bytes(), overlay.read_bytes()) == applied


@pytest.mark.parametrize("unsafe", ["absolute", "traversal", "symlink"])
def test_source_paths_cannot_escape_declared_roots(tmp_path, unsafe):
    db, export, source, _ = _fixture(tmp_path)
    if unsafe == "absolute":
        relative = str(source)
    elif unsafe == "traversal":
        relative = "../export/lens/base.csv"
    else:
        (export / "escape").symlink_to(tmp_path / "package", target_is_directory=True)
        relative = "escape/lenses.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("UPDATE simulation_results SET source_surfaces_csv=?", (relative,))
    before = source.read_bytes(), db.read_bytes()
    with pytest.raises(ValueError, match="relative|escapes"):
        repair.repair_database(db, export, apply=True)
    assert (source.read_bytes(), db.read_bytes()) == before


def test_numeric_db_conflict_is_refused_without_mutation(tmp_path):
    db, export, source, _ = _fixture(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE lens_surfaces SET thickness_mm=9 WHERE surface_order=18")
    before = source.read_bytes(), db.read_bytes()
    with pytest.raises(ValueError, match="numeric DB gap"):
        repair.repair_database(db, export, apply=True)
    assert (source.read_bytes(), db.read_bytes()) == before


def test_conflict_arriving_after_planning_is_rechecked_under_write_lock(tmp_path, monkeypatch):
    db, export, _, _ = _fixture(tmp_path)
    connect = sqlite3.connect

    def concurrent_change(database, *args, **kwargs):
        if database == db:
            with connect(db) as con:
                con.execute("UPDATE lens_surfaces SET thickness_mm=7 WHERE surface_order=18")
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(repair.sqlite3, "connect", concurrent_change)
    with pytest.raises(ValueError, match="numeric DB gap"):
        repair.repair_database(db, export, apply=True)
    with connect(db) as con:
        assert (
            con.execute("SELECT thickness_mm FROM lens_surfaces WHERE surface_order=18").fetchone()[
                0
            ]
            == 7
        )
    assert not (db.parent / "reviewed_prescriptions/lens/base.csv").exists()


def test_incremental_repair_keeps_prior_overlay_values(tmp_path):
    db, export, source, variables = _fixture(tmp_path, two_stops=True)
    first = repair.repair_database(db, export, apply=True)
    overlay = Path(first["evidence"][0]["reviewed_surfaces_csv"])
    _write_csv(
        variables,
        [
            {"parameter": "d18", "configuration": "base", "value": "3.0"},
            {"parameter": "d20", "configuration": "base", "value": "4.0"},
        ],
    )
    original_source = source.read_bytes()
    second = repair.repair_database(db, export, apply=True)
    assert second["repaired_gaps"] == 1
    overlay_rows = repair.read_csv(overlay)
    assert overlay_rows[1]["thickness"] == "3.0"
    assert overlay_rows[3]["thickness"] == "4.0"
    assert source.read_bytes() == original_source

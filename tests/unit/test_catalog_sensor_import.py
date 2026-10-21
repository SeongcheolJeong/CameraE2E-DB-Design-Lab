"""Sensor migration preserves report identity, provenance and frozen run history."""

import copy
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from pyisetcam._catalog_sensor import import_sensors
from pyisetcam._catalog_store import CatalogWriter, connect, file_hash


@pytest.fixture
def writer(tmp_path):
    root = tmp_path / "out"
    conn = connect(root / "catalog.sqlite", write=True)
    try:
        yield CatalogWriter(conn, root)
    finally:
        conn.close()


def catalog(tmp_path, records=None):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    stack = source / "legacy_sensor_name.json"
    stack.write_text(
        json.dumps(
            {
                "geometry_um": {"pitch": 2.0},
                "calibration_status": {"is_measured": False, "mode": "proxy"},
                "layers": [{"name": "passivation", "material": "SiO2", "thickness_um": 0.2}],
            }
        )
    )
    record = {
        "code": "DEF-2601-801",
        "provider": "TechInsights",
        "metadata": {"manufacturer": "Vendor", "device_name": "Same Model"},
        "derived_specs": {"pixel_pitch_um": 2.0, "resolution_x": 4000, "resolution_y": 3000},
        "derived_evidence": {
            "pixel_pitch_um": [{"source": "pdf_design_snippet", "text": "2.0 um"}]
        },
        "generated_files": {"stack_config": str(stack)},
    }
    path = source / "sensor_catalog.json"
    path.write_text(json.dumps({"records": records if records is not None else [record]}))
    return path


def simulations(tmp_path, statuses=("simulated", "failed", "excluded")):
    path = tmp_path / "simulations" / "sensor_simulations.sqlite"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,report_code TEXT,sensor_id TEXT,
          device_name TEXT,manufacturer TEXT,model TEXT,status TEXT,reason TEXT,
          created_at TEXT,config_sha256 TEXT,result_path TEXT,result_sha256 TEXT,
          arrays_path TEXT,arrays_sha256 TEXT,summary_json TEXT);
        CREATE TABLE sensors(report_code TEXT PRIMARY KEY,run_id TEXT);
    """)
    for i, status in enumerate(statuses):
        folder = path.parent / f"run_{i}"
        folder.mkdir()
        metadata = {
            "report_code": "DEF-2601-801",
            "sensor_id": "legacy_sensor_name",
            "status": status,
            "simulation_settings": {"seed": i, "theta_deg": [0, 10], "exposure_s": [0.004]},
            "checks": {"numerical_passed": status == "simulated"},
        }
        if status == "simulated":
            metadata.update(
                model_snapshot={
                    "kind": "physical_estimate_v1",
                    "pixel_pitch_um": 1.5,
                    "filter_names": ["R", "G", "B"],
                    "estimate_only": True,
                },
                readout_profile={
                    "operating_point": "assumed_reference",
                    "readout": {"bit_depth": 12},
                    "provenance": {
                        "conditions": {"temperature_c": None, "conversion_mode": "unspecified"},
                        "fields": {"read_noise_e": {"value": 3.0, "classification": "assumption"}},
                    },
                },
                optical_capabilities={"spectral_axes": ["wavelength", "native_filter"]},
                array_units={"spectral_eqe": "collected electrons per incident photon"},
            )
        arrays = folder / "arrays.npz"
        np.savez(arrays, spectral_eqe=np.ones((2, 3)), wave_nm=[500, 600])
        result = folder / "result.json"
        result.write_text(
            json.dumps(
                {
                    "run_id": f"run_{i}",
                    "metadata": metadata,
                    "array_manifest": {"spectral_eqe": {"shape": [2, 3], "dtype": "float64"}},
                }
            )
        )
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"run_{i}",
                "DEF-2601-801",
                "legacy_sensor_name",
                "Same Model",
                "Vendor",
                "physical_estimate_v1",
                status,
                None if status == "simulated" else "historical failure",
                f"2026-09-06T0{i}:00:00Z",
                f"config_{i}",
                str(result.relative_to(path.parent)),
                file_hash(result),
                str(arrays.relative_to(path.parent)),
                file_hash(arrays),
                json.dumps({"checks": metadata["checks"], "estimate_only": True}),
            ),
        )
    conn.execute("INSERT INTO sensors VALUES (?,?)", ("DEF-2601-801", f"run_{len(statuses) - 1}"))
    conn.commit()
    conn.close()
    return path


def rows(writer, table):
    return [dict(row) for row in writer.conn.execute(f"SELECT * FROM {table}")]


def test_report_identity_ignores_file_name_and_separates_reports(writer, tmp_path):
    path = catalog(tmp_path)
    document = json.loads(path.read_text())
    second = copy.deepcopy(document["records"][0])
    second["code"] = "DEP-2601-802"
    document["records"].append(second)
    path.write_text(json.dumps(document))
    assert import_sensors(writer, path)["components"] == 2
    ids = {row["component_id"] for row in rows(writer, "components")}
    revisions = len(rows(writer, "model_revisions"))
    original = Path(document["records"][0]["generated_files"]["stack_config"])
    renamed = original.with_name("renamed.json")
    original.rename(renamed)
    for record in document["records"]:
        record["generated_files"]["stack_config"] = str(renamed)
    path.write_text(json.dumps(document))
    import_sensors(writer, path)
    assert {row["component_id"] for row in rows(writer, "components")} == ids
    assert len(rows(writer, "model_revisions")) == revisions
    assert {"legacy_sensor_name", "renamed"} <= {r["alias"] for r in rows(writer, "aliases")}


def test_extracted_evidence_and_proxy_models_remain_distinct(writer, tmp_path):
    path = catalog(tmp_path)
    import_sensors(writer, path)
    observations = rows(writer, "spec_observations")
    pitch = next(row for row in observations if row["property_name"] == "pixel_pitch_um")
    assert pitch["origin_kind"] == "extracted"
    assert json.loads(pitch["source_locator_json"])["source_location_status"] == "missing"
    unknown = next(row for row in observations if row["property_name"] == "cfa_pattern")
    assert json.loads(unknown["value_json"]) is None
    assert unknown["missing_reason"] == "not_reported"
    extracted = next(
        r for r in rows(writer, "model_revisions") if r["model_kind"] == "sensor_report_extraction"
    )
    geometry = next(
        r for r in rows(writer, "sensor_geometry") if r["revision_id"] == extracted["revision_id"]
    )
    assert (geometry["width_px"], geometry["height_px"]) == (4000, 3000)
    assert geometry["active_width_mm"] is None
    layer = rows(writer, "sensor_layers")[0]
    assert json.loads(layer["metadata_json"])["origin_kind"] == "assumed"
    assert all(r["status"] == "unknown" for r in rows(writer, "validations"))


def test_all_runs_frozen_models_and_array_metadata_import_read_only(writer, tmp_path):
    path, db = catalog(tmp_path), simulations(tmp_path)
    original_hash = file_hash(db)
    report = import_sensors(writer, path, db)
    assert report["runs"] == 3
    assert {r["status"] for r in rows(writer, "simulation_runs")} == {
        "simulated",
        "failed",
        "excluded",
    }
    assert not rows(writer, "component_preferences")
    simulated = next(r for r in rows(writer, "simulation_runs") if r["status"] == "simulated")
    geom = next(
        r for r in rows(writer, "sensor_geometry") if r["revision_id"] == simulated["revision_id"]
    )
    assert geom["pixel_pitch_um"] == 1.5  # Historical snapshot, not the current 2 um catalog.
    readout = next(
        r
        for r in rows(writer, "sensor_readout_modes")
        if r["operating_point_id"] == simulated["operating_point_id"]
    )
    assert readout["bit_depth"] == 12
    noise = next(
        r for r in rows(writer, "spec_observations") if r["property_name"] == "read_noise_e"
    )
    assert noise["origin_kind"] == "assumed"
    arrays = [
        json.loads(r["metadata_json"])
        for r in rows(writer, "artifacts")
        if r["kind"] == "sensor_simulation_arrays"
    ]
    assert any(
        r["axis_metadata"].get("spectral_axes") == ["wavelength", "native_filter"] for r in arrays
    )
    assert file_hash(db) == original_hash
    counts = {
        table: len(rows(writer, table))
        for table in (
            "simulation_runs",
            "model_revisions",
            "spec_observations",
            "artifacts",
            "artifact_links",
        )
    }
    import_sensors(writer, path, db)
    assert counts == {table: len(rows(writer, table)) for table in counts}


@pytest.mark.parametrize("corruption", ["missing", "hash_mismatch"])
def test_bad_snapshot_retains_run_without_rebuilding_current_model(writer, tmp_path, corruption):
    path, db = catalog(tmp_path), simulations(tmp_path, ("simulated",))
    result = db.parent / "run_0/result.json"
    if corruption == "missing":
        result.unlink()
    else:
        result.write_text('{"metadata":{"model_snapshot":{"pixel_pitch_um":999}}}')
    (db.parent / "run_0/arrays.npz").unlink()
    report = import_sensors(writer, path, db)
    assert report["runs"] == 1
    assert any(corruption in message for message in report["warnings"])
    run = rows(writer, "simulation_runs")[0]
    revision = next(
        r for r in rows(writer, "model_revisions") if r["revision_id"] == run["revision_id"]
    )
    snapshot = json.loads(revision["snapshot_json"])
    assert snapshot["model_snapshot_missing"] is True
    assert snapshot["model_snapshot"] == {}
    assert any(r["availability"] == "missing" for r in rows(writer, "artifacts"))
    assert any(
        r["gate"] == "frozen_snapshot_integrity" and r["status"] == "failed"
        for r in rows(writer, "validations")
    )


def test_source_edit_adds_revision_without_rewriting_frozen_history(writer, tmp_path):
    path, db = catalog(tmp_path), simulations(tmp_path, ("simulated",))
    import_sensors(writer, path, db)
    runs_before = rows(writer, "simulation_runs")
    revisions_before = len(rows(writer, "model_revisions"))
    document = json.loads(path.read_text())
    document["records"][0]["derived_specs"]["pixel_pitch_um"] = 4.0
    path.write_text(json.dumps(document))
    import_sensors(writer, path, db)
    assert len(rows(writer, "components")) == 1
    assert len(rows(writer, "model_revisions")) == revisions_before + 1
    assert rows(writer, "simulation_runs") == runs_before
    observed = {
        r["numeric_value"]
        for r in rows(writer, "spec_observations")
        if r["property_name"] == "pixel_pitch_um"
    }
    assert observed == {2.0, 4.0}


@pytest.mark.parametrize("document", [{}, [], {"records": {}}, {"records": None}])
def test_invalid_sensor_catalog_is_not_an_empty_success(writer, tmp_path, document):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="records list"):
        import_sensors(writer, path)
    assert rows(writer, "components") == []


@pytest.mark.parametrize("selected_material_available", [False, True])
def test_material_resolution_uses_recorded_original_config_as_fallback(
    writer, tmp_path, selected_material_available
):
    path = catalog(tmp_path)
    original = tmp_path / "original/sensor_db/generated_stack_configs/model.json"
    original.parent.mkdir(parents=True)
    original.write_text(json.dumps({"geometry_um": {"pitch": 99}}))
    relocated = path.parent / "generated_stack_configs/model.json"
    relocated.parent.mkdir()
    relocated.write_text(
        json.dumps(
            {
                "geometry_um": {"pitch": 2.0},
                "materials": {"silicon": {"nk_table": "../../materials/Si.csv"}},
            }
        )
    )
    original_material = original.parent.parent.parent / "materials/Si.csv"
    original_material.parent.mkdir()
    original_material.write_text("wavelength_nm,n,k\n500,4.1,0.1\n")
    selected_material = path.parent.parent / "materials/Si.csv"
    if selected_material_available:
        selected_material.parent.mkdir()
        selected_material.write_text("wavelength_nm,n,k\n500,4.2,0.2\n")
    document = json.loads(path.read_text())
    document["records"][0]["generated_files"]["stack_config"] = str(original)
    path.write_text(json.dumps(document))
    report = import_sensors(writer, path)
    assert report["warnings"] == []
    material = next(
        row for row in rows(writer, "artifacts") if row["kind"] == "sensor_material_table"
    )
    expected = selected_material if selected_material_available else original_material
    assert material["sha256"] == file_hash(expected)
    assert material["expected_sha256"] == file_hash(expected)
    assert material["availability"] == "available"
    assert (writer.root / material["path"]).read_bytes() == expected.read_bytes()
    metadata = json.loads(material["metadata_json"])
    assert metadata["source_origin_path"] == str(expected)
    assert metadata["source_origin_sha256"] == file_hash(expected)
    assert metadata["resolution_basis"] == (
        "selected_config_relative" if selected_material_available else "original_config_relative"
    )
    generated = next(
        row
        for row in rows(writer, "model_revisions")
        if row["model_kind"] == "sensor_generated_stack_config"
    )
    snapshot = json.loads(generated["snapshot_json"])
    assert snapshot["config"]["geometry_um"]["pitch"] == 2.0
    assert snapshot["material_dependencies"]["silicon"]["sha256"] == file_hash(expected)

"""Behavioral tests for the persistent component catalog's public workflows."""

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from pyisetcam import component_catalog as catalog
from pyisetcam._catalog_store import CatalogWriter, connect, digest, file_hash, json_text


@pytest.fixture
def populated(tmp_path):
    root = tmp_path / "catalog"
    conn = connect(root / "catalog.sqlite", write=True)
    writer = CatalogWriter(conn, root)
    source = writer.source(
        "datasheet",
        "fixture-doc",
        uri="https://example.test/spec",
        metadata={"page": 4, "source_status": "reported"},
    )
    lens = writer.component("lens", "test-lens", "OpticsCo", "Prime", "patent_design")
    sensor = writer.component("sensor", "test-sensor", "SensorCo", "Chip", "sensor_report")
    writer.alias(lens, "test", "lens-alias")
    writer.alias(sensor, "test", "sensor-alias")
    lens_revision = writer.revision(lens, "prescription", {"focal_length_mm": 10}, source_id=source)
    sensor_revision = writer.revision(
        sensor,
        "sensor_stack",
        {"pitch_um": 1.0, "width_mm": 6, "height_mm": 8},
        source_id=source,
    )
    lens_point = writer.operating_point(lens_revision, "infinity", {"object_distance": "infinity"})
    sensor_point = writer.operating_point(sensor_revision, "normal", {"gain": 1})
    writer.put(
        "lens_configurations",
        operating_point_id=lens_point,
        focal_length_mm=10,
        f_number=2,
        image_height_mm=5,
        image_circle_mm=None,
        metadata_json=json_text({"circle_unknown_reason": "not_reported"}),
    )
    writer.put(
        "sensor_geometry",
        revision_id=sensor_revision,
        pixel_pitch_um=1,
        width_px=6000,
        height_px=8000,
        active_width_mm=6,
        active_height_mm=8,
        cfa_pattern="RGGB",
        metadata_json="{}",
    )
    writer.observation(
        lens,
        "focal_length_mm",
        10.0,
        unit="mm",
        source_id=source,
        revision_id=lens_revision,
        operating_point_id=lens_point,
        source_locator={"page": 4, "table": "optical specifications"},
        extraction_method="manual_transcription",
    )
    run = writer.run(
        lens_revision,
        lens_point,
        "fixture-run",
        "failed",
        solver="fixture-solver",
        settings={"wavelength_nm": [550]},
        source_id=source,
        input_sha256=digest({"focal_length_mm": 10}),
        metadata={"failure_reason": "fixture"},
    )
    payload = root / "input.bin"
    payload.write_bytes(b"sample artifact")
    artifact = writer.artifact(
        payload,
        "psf",
        revision_id=lens_revision,
        run_id=run,
        expected_sha256=file_hash(payload),
        metadata={"axes": ["x", "y"]},
    )
    writer.validation(
        "artifact",
        artifact,
        "measurement_accuracy",
        "unknown",
        {"reason": "no_matched_measurement"},
    )
    conn.commit()
    yield dict(
        root=root,
        conn=conn,
        writer=writer,
        source=source,
        lens=lens,
        sensor=sensor,
        lens_revision=lens_revision,
        sensor_revision=sensor_revision,
        lens_point=lens_point,
        sensor_point=sensor_point,
        artifact=artifact,
        payload=payload,
    )
    conn.close()


def test_read_only_queries_on_missing_catalog_do_not_create_files(tmp_path):
    root = tmp_path / "missing"
    assert catalog.component_catalog_search(root=root) == []
    assert catalog.component_catalog_summary(root=root)["status"] == "missing"
    assert catalog.component_catalog_validate(root=root)["status"] == "failed"
    with pytest.raises(FileNotFoundError):
        catalog.component_catalog_get("anything", root=root)
    with pytest.raises(FileNotFoundError):
        catalog.camera_configuration_get("anything", root=root)
    assert not root.exists()


def test_export_missing_catalog_creates_neither_database_nor_empty_export(tmp_path):
    root = tmp_path / "missing"
    output = tmp_path / "export"
    with pytest.raises(FileNotFoundError):
        catalog.component_catalog_export(root=root, output_dir=output)
    assert not root.exists()
    assert not output.exists()
    with pytest.raises(FileNotFoundError):
        catalog.component_catalog_export(root=root)
    assert not root.exists()


def test_explicit_root_does_not_fall_back_to_unrelated_workspace(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated"
    candidate = unrelated / "lens_db/package/data/lens_patents/lens_patent_simulation_v99.sqlite"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"must not inspect this database")
    monkeypatch.setenv("PYISETCAM_CAMERA_DB_ROOT", str(unrelated))
    target = tmp_path / "empty-root"
    with pytest.raises(FileNotFoundError, match="No lens or sensor source"):
        catalog.build_component_catalog(root=target)
    assert not target.exists()
    with pytest.raises(FileNotFoundError, match="Explicit lens_db"):
        catalog.build_component_catalog(root=target, lens_db=tmp_path / "missing.sqlite")
    assert not target.exists()


def test_alias_ambiguity_requires_canonical_identity(populated):
    f = populated
    for component in (f["lens"], f["sensor"]):
        f["writer"].alias(component, "legacy", "shared-id")
    f["conn"].commit()
    assert len(catalog.component_catalog_search("SHARED-ID", root=f["root"])) == 2
    with pytest.raises(ValueError, match="Ambiguous component alias"):
        catalog.component_catalog_get("shared-id", root=f["root"])
    result = catalog.component_catalog_get(f["lens"], root=f["root"])
    assert result["component"]["family"] == "lens"
    assert result["runs"][0]["status"] == "failed"
    assert result["observations"][0]["source_locator"]["page"] == 4
    assert result["validations"][0]["status"] == "unknown"


def test_selection_is_owned_persistent_and_unchanged_by_new_revision(populated):
    f = populated
    with pytest.raises(ValueError, match="different component"):
        catalog.component_catalog_select_revision(
            "lens-alias", f["sensor_revision"], root=f["root"]
        )
    catalog.component_catalog_select_revision("LENS-ALIAS", f["lens_revision"], root=f["root"])
    old = (
        f["conn"]
        .execute(
            "SELECT snapshot_path FROM model_revisions WHERE revision_id=?", (f["lens_revision"],)
        )
        .fetchone()[0]
    )
    before = (f["root"] / old).read_bytes()
    new_revision = f["writer"].revision(f["lens"], "prescription", {"focal_length_mm": 12})
    f["conn"].commit()
    result = catalog.component_catalog_get(f["lens"], root=f["root"])
    assert result["selected_revision_id"] == f["lens_revision"]
    assert {r["revision_id"] for r in result["revisions"]} == {f["lens_revision"], new_revision}
    assert (f["root"] / old).read_bytes() == before
    assert (
        catalog.component_catalog_search(family="lens", root=f["root"])[0]["selected_revision_id"]
        == f["lens_revision"]
    )


def test_camera_combination_rejects_wrong_family_and_foreign_operating_point(populated):
    f = populated
    with pytest.raises(ValueError, match="Expected lens revision"):
        catalog.camera_configuration_save(
            "wrong", f["sensor_revision"], f["lens_revision"], root=f["root"]
        )
    with pytest.raises(ValueError, match="Operating point must belong"):
        catalog.camera_configuration_save(
            "wrong point",
            f["lens_revision"],
            f["sensor_revision"],
            root=f["root"],
            lens_operating_point_id=f["sensor_point"],
        )
    assert f["conn"].execute("SELECT count(*) FROM camera_configurations").fetchone()[0] == 0


def test_camera_coverage_distinguishes_unknown_and_insufficient_and_pins_result(populated):
    f = populated
    unknown = catalog.camera_configuration_save(
        "unknown", f["lens_revision"], f["sensor_revision"], root=f["root"]
    )
    assert unknown["compatibility"]["coverage"]["status"] == "unknown"
    assert unknown["compatibility"]["status"] == "unknown"
    # Store a new complete lens model rather than rewriting the original one.
    rev = f["writer"].revision(
        f["lens"], "prescription", {"focal_length_mm": 10, "image_circle_mm": 9}
    )
    point = f["writer"].operating_point(rev, "infinity", {})
    f["writer"].put(
        "lens_configurations", operating_point_id=point, image_circle_mm=9, metadata_json="{}"
    )
    f["conn"].commit()
    failed = catalog.camera_configuration_save(
        "too small", rev, f["sensor_revision"], root=f["root"]
    )
    assert failed["compatibility"]["coverage"]["active_diagonal_mm"] == 10
    assert failed["compatibility"]["coverage"]["status"] == "failed"
    assert failed["compatibility"]["status"] == "failed"
    assert catalog.camera_configuration_get(unknown["configuration_id"], root=f["root"]) == unknown


@pytest.mark.parametrize(
    ("circle", "width", "height"),
    [
        (0, 6, 8),
        (float("inf"), 6, 8),
        (10, -1, 8),
        (10, float("inf"), 8),
        (10, 6, 0),
        (10, 6, float("inf")),
    ],
)
def test_camera_coverage_requires_finite_positive_dimensions(populated, circle, width, height):
    f = populated
    # Emulate invalid values admitted by a historical source adapter.
    f["conn"].execute("UPDATE lens_configurations SET image_circle_mm=?", (circle,))
    f["conn"].execute(
        "UPDATE sensor_geometry SET active_width_mm=?,active_height_mm=?", (width, height)
    )
    f["conn"].commit()
    result = catalog.camera_configuration_save(
        "invalid geometry", f["lens_revision"], f["sensor_revision"], root=f["root"]
    )
    assert result["compatibility"]["coverage"]["status"] == "unknown"
    assert "finite positive" in result["compatibility"]["coverage"]["reason"]
    assert result["compatibility"]["status"] == "unknown"


def test_release_requires_selection_pins_explicit_set_and_is_idempotent(populated):
    f = populated
    with pytest.raises(ValueError, match="Select model revisions"):
        catalog.component_catalog_release("empty", root=f["root"])
    catalog.component_catalog_select_revision(f["sensor"], f["sensor_revision"], root=f["root"])
    result = catalog.component_catalog_release(
        "lens-only", revision_ids=[f["lens_revision"]], root=f["root"]
    )
    again = catalog.component_catalog_release(
        "lens-only", revision_ids=[f["lens_revision"], f["lens_revision"]], root=f["root"]
    )
    assert result == again
    assert [r["revision_id"] for r in result["manifest"]["models"]] == [f["lens_revision"]]
    assert result["manifest"]["artifacts"][0]["sha256"] == file_hash(f["payload"])
    assert json.loads(Path(result["path"]).read_text()) == result["manifest"]
    selected = catalog.component_catalog_release("selected", root=f["root"])
    assert [r["revision_id"] for r in selected["manifest"]["models"]] == [f["sensor_revision"]]
    assert f["conn"].execute("SELECT count(*) FROM releases").fetchone()[0] == 2


def test_manufacturer_export_keeps_evidence_and_run_history(populated, tmp_path):
    f = populated
    result = catalog.component_catalog_export(
        root=f["root"], output_dir=tmp_path / "export", manufacturer="opticsco"
    )
    assert result["count"] == 1
    exported = json.loads(Path(result["json_path"]).read_text())["components"]
    assert exported[0]["component"]["component_id"] == f["lens"]
    assert exported[0]["sources"][0]["uri"] == "https://example.test/spec"
    assert exported[0]["observations"][0]["source_locator"]["page"] == 4
    assert exported[0]["runs"][0]["status"] == "failed"
    assert exported[0]["validations"][0]["status"] == "unknown"
    with Path(result["csv_path"]).open(newline="") as stream:
        assert [r["component_id"] for r in csv.DictReader(stream)] == [f["lens"]]


def test_validation_finds_changed_artifact_and_foreign_key_corruption(populated):
    f = populated
    assert (
        catalog.component_catalog_validate(root=f["root"], verify_files=True)["status"] == "passed"
    )
    f["payload"].write_bytes(b"tampered artifact")
    bad_file = catalog.component_catalog_validate(root=f["root"], verify_files=True)
    assert bad_file["status"] == "failed"
    assert any("Artifact unavailable/corrupt" in error for error in bad_file["errors"])
    with sqlite3.connect(f["root"] / "catalog.sqlite") as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DELETE FROM components WHERE component_id=?", (f["sensor"],))
    corrupt = catalog.component_catalog_validate(root=f["root"])
    assert corrupt["status"] == "failed"
    assert any("Foreign key violation" in error for error in corrupt["errors"])


def test_validation_rejects_changed_snapshot_and_schema_version(populated):
    f = populated
    with sqlite3.connect(f["root"] / "catalog.sqlite") as con:
        con.execute(
            "UPDATE model_revisions SET snapshot_json=? WHERE revision_id=?",
            (json_text({"tampered": True}), f["lens_revision"]),
        )
    result = catalog.component_catalog_validate(root=f["root"])
    assert result["status"] == "failed"
    assert any("Model content hash mismatch" in error for error in result["errors"])
    with sqlite3.connect(f["root"] / "catalog.sqlite") as con:
        con.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="Unsupported component catalog schema"):
        catalog.component_catalog_summary(root=f["root"])
    assert catalog.component_catalog_validate(root=f["root"])["status"] == "failed"


def test_invalid_sqlite_bytes_report_validation_failure(tmp_path):
    (tmp_path / "catalog.sqlite").write_bytes(b"not a sqlite database")
    result = catalog.component_catalog_validate(root=tmp_path)
    assert result["status"] == "failed"
    assert result["errors"]


def test_failed_import_rolls_back_rows_and_does_not_touch_source(tmp_path, monkeypatch):
    import pyisetcam._catalog_lens as importer

    source = tmp_path / "source.sqlite"
    source.write_bytes(b"opaque source: patched importer owns decoding")
    expected = file_hash(source)

    def fail(writer, path):
        assert path == source
        component = writer.component("lens", "should-rollback", "Test", "Failure", "patent_design")
        writer.revision(component, "prescription", {"focal_length_mm": 1})
        raise RuntimeError("fixture import failure")

    monkeypatch.setattr(importer, "import_lenses", fail)
    target = tmp_path / "output"
    with pytest.raises(RuntimeError, match="fixture import failure"):
        catalog.build_component_catalog(root=target, lens_db=source)
    assert file_hash(source) == expected
    result = catalog.component_catalog_summary(root=target)
    assert result["counts"]["components"] == result["counts"]["model_revisions"] == 0
    with connect(target / "catalog.sqlite") as con:
        assert con.execute("SELECT count(*) FROM import_batches").fetchone()[0] == 0


def test_runtime_registry_uses_env_root_and_only_reads_catalog(populated, tmp_path, monkeypatch):
    from pyisetcam.db_catalog import _component_catalog_entries

    def unexpected_import(**kwargs):
        pytest.fail("Registry inspection must not import component sources")

    monkeypatch.setattr(catalog, "build_component_catalog", unexpected_import)
    missing = tmp_path / "registry-missing"
    monkeypatch.setenv("PYISETCAM_CAMERA_DB_ROOT", str(missing))
    entry = _component_catalog_entries()[0]
    assert entry.name == "component_catalog"
    assert entry.status == "missing"
    assert entry.path == missing / "catalog.sqlite"
    assert not missing.exists()

    root = populated["root"]
    before = file_hash(root / "catalog.sqlite")
    monkeypatch.setenv("PYISETCAM_CAMERA_DB_ROOT", str(root))
    entry = _component_catalog_entries()[0]
    assert entry.status == "available"
    assert entry.parameters["root"] == str(root)
    assert entry.metadata["summary"]["counts"]["by_family"] == {"lens": 1, "sensor": 1}
    assert file_hash(root / "catalog.sqlite") == before

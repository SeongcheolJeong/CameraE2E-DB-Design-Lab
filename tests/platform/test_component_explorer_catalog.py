"""Catalog browsing must not merge reports or mutate original design assets."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from camera_platform import component_explorer_catalog as catalog


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "camerae2e_db"
    root.mkdir()
    db = root / "catalog.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("""
        CREATE TABLE components(component_id TEXT PRIMARY KEY,family TEXT,
          manufacturer TEXT,model TEXT);
        CREATE TABLE model_revisions(revision_id TEXT PRIMARY KEY,component_id TEXT,
          model_kind TEXT,content_sha256 TEXT,snapshot_json TEXT,source_id TEXT);
        CREATE TABLE sources(source_id TEXT PRIMARY KEY,sha256 TEXT);
        CREATE TABLE operating_points(operating_point_id TEXT PRIMARY KEY,
          revision_id TEXT,label TEXT);
        CREATE TABLE lens_configurations(operating_point_id TEXT,focal_length_mm REAL,
          f_number REAL,image_height_mm REAL,image_circle_mm REAL);
        """)
        con.executemany(
            "INSERT INTO components VALUES(?,?,?,?)",
            [
                ("lens", "lens", "Canon", "Published design"),
                ("sensor-a", "sensor", "Sony", "Same sensor name"),
                ("sensor-b", "sensor", "Sony", "Same sensor name"),
            ],
        )
        con.execute("INSERT INTO sources VALUES('source',?)", ("a" * 64,))
        lens_snapshot = {
            "identity": {"publication_number": "US123B2"},
            "specifications": {
                "focal_length_source": "reported",
                "f_number_source": "proxy_estimate",
            },
            "prescription_conventions": {"aperture_radius_or_diameter": "unknown"},
            "surfaces": [
                {
                    "surface_order": 1,
                    "radius_mm": 10,
                    "thickness_mm": 1.1,
                    "effective_aperture_mm": 4,
                    "material": "N-BK7",
                    "is_aspheric": True,
                    "coefficients": {"a4": 0.0001},
                    "raw": {"report_body": "PRIVATE REPORT", "source_file": "/secret/source.txt"},
                }
            ],
        }
        rows = [
            (
                "lens-r1",
                "lens",
                "patent_prescription",
                "b" * 64,
                json.dumps(lens_snapshot),
                "source",
            ),
            (
                "lens-r2",
                "lens",
                "patent_prescription",
                "c" * 64,
                json.dumps(lens_snapshot),
                "source",
            ),
        ]
        reports = []
        for key, code in (("a", "DEF-ONE"), ("b", "DEF-TWO")):
            specs = {
                "pixel_pitch_um": 2.9,
                "optical_stack_height_um": 2.5,
                "active_si_thickness_um": 6.2,
                "cfa_pattern": "bayer",
                "pixel_architecture": "stacked",
                "dti_depth_um": 2,
                "private_path": "/secret/private/report.pdf",
            }
            snapshot = {
                "metadata": {"code": code, "device_name": "Same sensor name"},
                "derived_specs": specs,
                "derived_evidence": {
                    "pixel_pitch_um": [
                        {"text": "PRIVATE REPORT BODY", "source_file": "/secret/report.pdf"}
                    ]
                },
            }
            rows.append(
                (
                    f"sensor-{key}-r1",
                    f"sensor-{key}",
                    "sensor_report_extraction",
                    key * 64,
                    json.dumps(snapshot),
                    "source",
                )
            )
            reports.append(
                {
                    "code": code,
                    "derived_specs": specs,
                    "source_files": {"pdf": ["/secret/report.pdf"]},
                }
            )
        con.executemany("INSERT INTO model_revisions VALUES(?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO operating_points VALUES(?,?,?)",
            [("config1", "lens-r1", "wide"), ("config2", "lens-r2", "tele")],
        )
        con.executemany(
            "INSERT INTO lens_configurations VALUES(?,?,?,?,?)",
            [("config1", 4, 1.8, None, None), ("config2", None, 2.4, None, None)],
        )
    report_path = root / "fdtd_tcad/image_sensor_db/sensor_catalog.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({"records": reports}))
    return tmp_path


def test_counts_distinguish_components_revisions_reports_and_names(workspace):
    view = catalog.index(workspace)
    assert view["counts"]["total_assets"] == 4
    assert view["counts"]["lens_components"] == 1
    assert view["counts"]["lens_prescription_revisions"] == 2
    assert view["counts"]["lens_configurations"] == 2
    assert view["counts"]["sensor_reports"] == 2
    assert view["counts"]["sensor_model_labels"] == 1
    sensors = [item for item in view["items"] if item["family"] == "sensor"]
    assert sensors[0]["id"] != sensors[1]["id"]


def test_missing_dimensions_stay_null_and_hashes_keep_source_identity(workspace):
    items = catalog.index(workspace)["items"]
    unknown = next(item for item in items if item["revision_id"] == "lens-r2")
    assert unknown["parameters"]["focal_length_mm"] is None
    assert unknown["parameters"]["image_circle_mm"] is None
    assert unknown["content_sha256"] == "c" * 64
    assert unknown["source_sha256"] == "a" * 64


def test_reads_are_nonmutating_and_index_copy_cannot_poison_cache(workspace):
    db, reports = catalog._paths(workspace)
    before = [
        (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in (db, reports)
    ]
    view = catalog.index(workspace)
    original_id = view["items"][0]["id"]
    view["items"][0]["parameters"]["focal_length_mm"] = 999
    catalog.detail(workspace, original_id)
    assert catalog.index(workspace)["items"][0]["parameters"]["focal_length_mm"] != 999
    after = [
        (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in (db, reports)
    ]
    assert before == after


def test_metadata_exports_have_no_paths_raw_commands_or_report_bodies(workspace):
    view = catalog.index(workspace)
    values = [view] + [catalog.detail(workspace, item["id"]) for item in view["items"]]
    encoded = json.dumps(values)
    assert "/secret/" not in encoded
    assert "PRIVATE REPORT" not in encoded
    assert "source_files" not in encoded


def test_structure_does_not_infer_aperture_or_add_overlapping_sensor_dimensions(workspace):
    view = catalog.index(workspace)
    lens = catalog.detail(
        workspace, next(item["id"] for item in view["items"] if item["family"] == "lens")
    )
    assert lens["structure"]["elements"][0]["aperture_convention"] == "unknown"
    assert any("aspheric" in note for note in lens["structure"]["notes"])
    assert any(item["source_class"] == "model_assumption" for item in lens["source_evidence"])
    sensor = catalog.detail(
        workspace, next(item["id"] for item in view["items"] if item["family"] == "sensor")
    )
    assert sensor["structure"]["status"] == "reported_dimensions"
    assert all(item["kind"] == "dimension" for item in sensor["structure"]["elements"])
    assert any("not additive" in note for note in sensor["structure"]["notes"])


def test_current_json_conflicts_are_visible_without_overwriting_fixed_revision(workspace):
    first = catalog.index(workspace)
    db, reports = catalog._paths(workspace)
    content = json.loads(reports.read_text())
    content["records"][0]["derived_specs"]["pixel_pitch_um"] = 1.1
    reports.write_text(json.dumps(content))
    second = catalog.index(workspace)
    assert first["snapshot_id"] != second["snapshot_id"]
    item = next(item for item in second["items"] if item["report_code"] == "DEF-ONE")
    assert item["parameters"]["pixel_pitch_um"] == 2.9
    detail = catalog.detail(workspace, item["id"])
    assert any(
        evidence["source_class"] == "conflicting" and evidence["value"] == 1.1
        for evidence in detail["source_evidence"]
    )
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE lens_configurations SET focal_length_mm=5 WHERE operating_point_id='config1'"
        )
    third = catalog.index(workspace)
    assert third["snapshot_id"] != second["snapshot_id"]
    assert (
        next(item for item in third["items"] if item["revision_id"] == "lens-r1")["parameters"][
            "focal_length_mm"
        ]
        == 5
    )


def test_unknown_asset_fails_without_trying_paths(workspace):
    with pytest.raises(KeyError, match="Unknown design asset"):
        catalog.detail(workspace, "../../secret")


def test_api_detail_uses_real_catalog_dto(workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from camera_platform import component_explorer as explorer
    from camera_platform.api import create_app

    monkeypatch.setattr(explorer.evidence, "bindings", lambda service, items: {})
    asset = next(item for item in catalog.index(workspace)["items"] if item["family"] == "lens")
    app = create_app(workspace / "state", workspace, start_worker=False, seed_demo=False)
    with TestClient(app) as http:
        response = http.get(f"/api/v1/component-explorer/assets/{asset['id']}")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["item"]["id"] == asset["id"]
    assert result["item"]["revision_id"] == asset["revision_id"]
    assert result["structure"]["kind"] == "lens"
    assert result["structure"]["elements"]
    assert result["source_evidence"]
    assert result["results"] == []


def test_real_repository_snapshot_counts_and_source_sanitization():
    root = Path(__file__).resolve().parents[2]
    if not (root / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Full catalog is optional in a portable installation")
    view = catalog.index(root)
    assert view["counts"]["lens_components"] >= 877
    assert view["counts"]["sensor_reports"] >= 224
    sensor = next(item for item in view["items"] if item.get("report_code") == "DEF-2011-804")
    assert sensor["parameters"]["pixel_pitch_um"] == 2.9
    encoded = json.dumps(catalog.detail(root, sensor["id"]))
    assert "/Users/" not in encoded
    assert "subscription" not in encoded

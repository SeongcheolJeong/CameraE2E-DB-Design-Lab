"""Registered active-mode declarations use artifact IDs at the API boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_active_mode_asset import (
    preflight_registered_fullfield_study,
    register_active_mode_declaration,
)
from camera_platform.lens_sensor_fullfield_study_preflight import MODE_SCHEMA
from camera_platform.registry import SQLiteRegistry
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import inspect_sensor_revision

WORKSPACE = Path(__file__).resolve().parents[2]
DB_ROOT = WORKSPACE / "camerae2e_db"
LENS_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
SENSOR_CODE = "DEF-2512-801"
SENSOR_RUN = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"


@pytest.fixture(scope="module")
def active_mode() -> dict:
    if not (DB_ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB unavailable")
    revision = inspect_sensor_revision(
        SENSOR_CODE, root=DB_ROOT, run_id=SENSOR_RUN
    )["revision"]
    return {
        "schema": MODE_SCHEMA,
        "mode_id": "illustrative-registered-mode",
        "sensor_run_id": revision["run_id"],
        "sensor_report_code": revision["report_code"],
        "sensor_result_sha256": revision["result_sha256"],
        "active_rows": 1000,
        "active_cols": 1200,
        "pixel_pitch_um": 0.61,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "sensor_orientation_deg_clockwise": 0,
        "cfa_origin_row_col": [0, 0],
        "image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane",
            "axial_offset_um": 0.0,
        },
    }


def _check(registry: SQLiteRegistry, declaration_id: str) -> dict:
    return preflight_registered_fullfield_study(
        registry, LENS_REVISION, SENSOR_CODE,
        sensor_run_id=SENSOR_RUN, declaration_id=declaration_id, root=DB_ROOT,
    )


def test_registered_declaration_uses_immutable_blob_without_exposing_local_path(
    tmp_path, active_mode
):
    registry = SQLiteRegistry(tmp_path / "state")
    record = register_active_mode_declaration(
        registry, SENSOR_CODE, sensor_run_id=SENSOR_RUN,
        active_mode=active_mode, source_kind="engineering_declaration", root=DB_ROOT,
    )
    assert record["sensor_run_id"] == SENSOR_RUN
    assert record["physical_mode_verified"] is False
    binding = registry.artifact_binding(
        record["declaration_artifact_sha256"],
        owner_kind="active_mode_declarations", owner_id=record["id"],
    )
    assert binding["descriptor"] == record["declaration_artifact"]
    result = _check(registry, record["id"])
    assert result["status"] == "planning_only"
    assert result["planning_ready"] is True
    assert result["execution_qualified"] is False
    assert result["physical_mtf_allowed"] is False
    assert result["product_qualification_allowed"] is False
    assert result["source_validation"]["file_hash_verified"] is True
    assert result["registered_declaration"]["artifact_sha256"] == record[
        "declaration_artifact_sha256"
    ]
    assert str(tmp_path) not in json.dumps(result)
    assert "registry-artifact://sha256/" in json.dumps(result)


def test_unregistered_or_changed_blob_fails_closed(tmp_path, active_mode):
    registry = SQLiteRegistry(tmp_path / "state")
    missing = _check(registry, "active-mode-declaration-does-not-exist")
    assert missing["status"] == "blocked"
    assert missing["reason"] == "registered_active_mode_artifact_unavailable_or_invalid"
    record = register_active_mode_declaration(
        registry, SENSOR_CODE, sensor_run_id=SENSOR_RUN,
        active_mode=active_mode, source_kind="engineering_declaration", root=DB_ROOT,
    )
    blob = registry.blob_path(record["declaration_artifact_sha256"])
    blob.write_bytes(blob.read_bytes() + b"\n")
    changed = _check(registry, record["id"])
    assert changed["status"] == "blocked"
    assert changed["reason"] == "registered_active_mode_artifact_unavailable_or_invalid"
    assert changed["planning_ready"] is False
    assert changed["physical_mtf_allowed"] is False
    blob.unlink()
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"private":"do not read"}', encoding="utf-8")
    blob.symlink_to(unrelated)
    symlinked = _check(registry, record["id"])
    assert symlinked["status"] == "blocked"
    assert "do not read" not in json.dumps(symlinked)


def test_submission_rejects_caller_path_and_unpinned_sensor(tmp_path, active_mode):
    registry = SQLiteRegistry(tmp_path / "state")
    with pytest.raises(ValueError, match="without a local source path"):
        register_active_mode_declaration(
            registry, SENSOR_CODE, sensor_run_id=SENSOR_RUN,
            active_mode={**active_mode, "source": {"path": "/etc/passwd"}},
            source_kind="engineering_declaration", root=DB_ROOT,
        )
    with pytest.raises(ValueError, match="unverified declaration"):
        register_active_mode_declaration(
            registry, SENSOR_CODE, sensor_run_id=SENSOR_RUN,
            active_mode=active_mode,
            source_kind="external_reference_unverified", root=DB_ROOT,
        )
    with pytest.raises(ValueError, match="saved sensor revision"):
        register_active_mode_declaration(
            registry, SENSOR_CODE, sensor_run_id="missing-run",
            active_mode=active_mode, source_kind="engineering_declaration", root=DB_ROOT,
        )
    with pytest.raises(ValueError, match="1..128"):
        register_active_mode_declaration(
            registry, SENSOR_CODE, sensor_run_id=SENSOR_RUN,
            active_mode={**active_mode, "mode_id": "x" * 129},
            source_kind="engineering_declaration", root=DB_ROOT,
        )


def test_sdk_can_register_inspect_and_download_pinned_declaration(tmp_path, active_mode):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        record = sdk.register_lens_sensor_active_mode(
            SENSOR_CODE, SENSOR_RUN, active_mode,
        )
        assert record == sdk.lens_sensor_active_mode(record["id"])
        source = transport.get(record["declaration_artifact"]["url"])
        assert source.status_code == 200
        assert json.loads(source.content)["active_mode"] == active_mode
        result = sdk.inspect_registered_lens_sensor_fov(
            LENS_REVISION, SENSOR_CODE, SENSOR_RUN, record["id"],
        )
        assert result["status"] == "planning_only"
        assert result["physical_mtf_allowed"] is False
        assert result["registered_declaration"]["artifact_sha256"] == record[
            "declaration_artifact_sha256"
        ]
        assert str(tmp_path) not in json.dumps(result)
        bad = transport.post("/api/v1/lens-sensor-active-modes", json={
            "sensor_identifier": SENSOR_CODE,
            "sensor_run_id": SENSOR_RUN,
            "active_mode": {**active_mode, "source": {"path": "/etc/passwd"}},
            "source_kind": "engineering_declaration",
        })
        assert bad.status_code == 422
        oversized = transport.post(
            "/api/v1/lens-sensor-active-modes",
            content=b" " * (1024 * 1024 + 4097),
            headers={"Content-Type": "application/json"},
        )
        assert oversized.status_code == 422
        assert "1 MiB policy" in oversized.text

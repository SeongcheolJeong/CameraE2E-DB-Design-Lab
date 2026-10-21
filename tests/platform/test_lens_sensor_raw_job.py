"""One explicit Lens DB × saved Sensor DB path produces attributable integer RAW."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_catalog_bridge import geometric_lens_preview
from camera_platform.lens_sensor_raw_bridge import (
    bounded_psf_resource_preflight,
    central_psf_resource_preflight,
    preview_lens_sensor_raw,
)
from camera_platform.lens_sensor_raw_job import default_spec, specification
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]
SLOW_P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
FAST_P0248_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def test_central_psf_budget_preserves_ready_lens_and_blocks_known_timeout_before_queue(
    tmp_path, monkeypatch
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    root = WORKSPACE / "camerae2e_db"
    slow = geometric_lens_preview(SLOW_P0014_REVISION, catalog_root=root)
    fast = geometric_lens_preview(FAST_P0248_REVISION, catalog_root=root)
    assert slow["model"]["psf_samples_row_col"] == [512, 512]
    slow_cost = central_psf_resource_preflight(slow["model"], 0.61, [8, 8])
    fast_cost = central_psf_resource_preflight(fast["model"], 0.61, [8, 8])
    assert slow_cost["status"] == "resource_blocked"
    assert slow_cost["resampled_kernel_samples_row_col"] == [1711, 1711]
    assert slow_cost["estimated_float32_stack_gib"] > 6.0
    assert slow_cost["minimum_stack_plus_float64_copy_gib"] > 18.0
    assert slow_cost["suggested_distinct_virtual_sensor_size"] == [12, 12]
    assert fast_cost["status"] == "within_development_budget"
    assert fast_cost["estimated_float32_stack_gib"] < 3.0
    assert central_psf_resource_preflight(slow["model"], 0.61, [12, 12])[
        "status"
    ] == "within_development_budget"

    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = transport.get("/api/v1/lens-sensor-raw/template").json()
        assert template["resource_contract"]["lens_preview_ready_meaning"] == (
            "validated_stored_psf_asset_only"
        )
        assert next(
            row for row in template["lens_catalog"]["revisions"]
            if row["revision_id"] == SLOW_P0014_REVISION
        )["preview_status"] == "ready"
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEF-2512-801"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=SLOW_P0014_REVISION,
            sensor_revision={
                "identifier": sensor["report_code"],
                "run_id": sensor["run_id"],
            },
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        rejected = transport.post("/api/v1/lens-sensor-raw", json=spec)
        assert rejected.status_code == 422
        assert "resource_blocked" in rejected.text
        assert "6.05 GiB" in rejected.text
        assert "local_field_psf" in rejected.text
        assert "sensor_size=[12, 12]" in rejected.text
        assert transport.get("/api/v1/lens-sensor-raw").json() == []

        local = transport.get(
            "/api/v1/lens-sensor-raw/template", params={"scope": "local_field_psf"}
        ).json()["specification"]
        local.update(
            lens_revision_id=SLOW_P0014_REVISION,
            sensor_revision=spec["sensor_revision"],
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        queued = transport.post("/api/v1/lens-sensor-raw", json=local)
        assert queued.status_code == 202
        assert queued.json()["resource_preflight"] is None

    from camera_platform import lens_sensor_raw_bridge as bridge

    monkeypatch.setattr(
        bridge, "offline_asset_store", lambda **_kwargs: pytest.fail("optical work started")
    )
    with pytest.raises(ValueError, match="resource_blocked"):
        preview_lens_sensor_raw(
            SLOW_P0014_REVISION,
            sensor["report_code"],
            sensor_run_id=sensor["run_id"],
            root=root,
            workspace=WORKSPACE,
            sensor_size=(8, 8),
            accept_approximate_lens_radiometry=True,
        )


def test_bounded_central_scope_admits_p0014_with_distinct_frozen_contract(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    root = WORKSPACE / "camerae2e_db"
    lens = geometric_lens_preview(SLOW_P0014_REVISION, catalog_root=root)
    budget = bounded_psf_resource_preflight(lens["model"], 0.61, [8, 8])
    assert budget["status"] == "within_development_budget"
    assert budget["scope"] == "central_bounded_strict_raytrace"
    assert budget["legacy_complete_float32_stack_gib"] > 6
    assert budget["estimated_bounded_working_gib"] < 3
    assert budget["execution_guaranteed"] is False

    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_raw_template(scope="central_bounded")
        assert template["specification"]["schema"].endswith("study_v3")
        assert template["resource_contract"]["central_bounded_uses_heuristic_working_budget"]
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEF-2512-801"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=SLOW_P0014_REVISION,
            sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        queued = client.submit_lens_sensor_raw(spec)
        assert queued["status"] == "queued"
        assert queued["plugin_id"] == spec["schema"]
        assert queued["resource_preflight"] == budget
        assert "execution strategy" in queued["limitation"]


def test_contract_requires_explicit_approximation_and_bounds():
    spec = default_spec()
    spec["lens_revision_id"] = "lens-id"
    spec["sensor_revision"] = {"identifier": "sensor-id", "run_id": "run-id"}
    with pytest.raises(ValueError, match="acceptance"):
        specification(spec)
    spec["accept_approximate_lens_radiometry"] = True
    assert specification(spec)["scene_kind"] == "slanted_edge"
    with pytest.raises(ValueError, match="acceptance"):
        preview_lens_sensor_raw(
            "lens-id", "sensor-id", sensor_run_id="run-id", root=WORKSPACE,
            workspace=WORKSPACE,
        )
    with pytest.raises(ValueError, match="sensor_size"):
        specification(dict(spec, sensor_size=[0, 32]))
    with pytest.raises(ValueError, match="finite"):
        specification(dict(spec, luminance_cd_m2=float("nan")))
    with pytest.raises(ValueError, match="finite"):
        specification(dict(spec, exposure_s=10**1000))


def test_real_saved_lens_sensor_raw_through_api_sdk_worker(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_raw_template()
        ready = [
            row for row in template["lens_catalog"]["revisions"]
            if row["preview_status"] == "ready"
        ]
        sensors = {
            row["report_code"]: row for row in template["sensor_catalog"]
        }
        if not ready or "DEP-2209-801" not in sensors:
            pytest.skip("Required pinned example revisions are unavailable")
        spec = template["specification"]
        spec.update(
            lens_revision_id=ready[0]["revision_id"],
            sensor_revision={
                "identifier": "DEP-2209-801",
                "run_id": sensors["DEP-2209-801"]["run_id"],
            },
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        queued = client.submit_lens_sensor_raw(spec)
        assert client.job_status(queued["id"], expected_kind="lens_sensor_raw")[
            "detail_path"
        ] == f"lens-sensor-raw/{queued['id']}"
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        report = summary["preview"]
        assert summary["source_identities"]["lens"] == report["lens_identity"]
        assert summary["source_identities"]["sensor"] == report["sensor_revision"]
        assert report["scope"] == "approximate_geometric_central_field_raw_preview"
        assert report["lens_model"]["absolute_radiometry"].startswith("unknown_")
        assert report["sensor_model"]["saved_snapshot_used"] is True
        assert report["raw"]["dtype"] == "int32"
        assert report["raw"]["shape"] == [8, 8]
        assert report["mean_electrons"]["mean"] > 0
        assert report["physical_validation"] == "not_established"
        content = transport.get(report["raw_artifact"]["url"]).content
        original = np.load(io.BytesIO(content), allow_pickle=False)
        assert original.dtype == np.dtype("int32")
        assert original.tolist() == report["raw"]["values"]
        assert client.lens_sensor_raw(queued["id"])["result"]["summary"] == summary
        assert len(client.list_lens_sensor_raw()) == 1


def test_physical_saved_sensor_is_refused_before_submission(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = transport.get("/api/v1/lens-sensor-raw/template").json()
        ready = next(
            row for row in template["lens_catalog"]["revisions"]
            if row["preview_status"] == "ready"
        )
        physical = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEF-1801-802"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=ready["revision_id"],
            sensor_revision={"identifier": "DEF-1801-802", "run_id": physical["run_id"]},
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        response = transport.post("/api/v1/lens-sensor-raw", json=spec)
        assert response.status_code == 422
        assert "matched lens pupil" in response.text


def test_local_field_psf_preview_crosses_job_api_and_keeps_scope(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_raw_template(scope="local_field_psf")
        lens = next(
            row for row in template["lens_catalog"]["revisions"]
            if row["preview_status"] == "ready"
        )
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEP-2209-801"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=lens["revision_id"],
            sensor_revision={"identifier": "DEP-2209-801", "run_id": sensor["run_id"]},
            sensor_size=[8, 8],
            field_psf_index=2,
            field_azimuth_deg=90,
            accept_approximate_lens_radiometry=True,
        )
        queued = client.submit_lens_sensor_raw(spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="lens_sensor_raw")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["schema"] == spec["schema"]
        preview = summary["preview"]
        assert preview["scope"] == "approximate_geometric_local_field_psf_raw_preview"
        assert preview["lens_model"]["field_psf_selection"]["stored_field_index"] == 2
        assert preview["lens_model"]["field_psf_selection"]["azimuth_deg_from_positive_x"] == 90
        assert preview["raw"]["shape"] == [8, 8]
        assert preview["physical_validation"] == "not_established"
        assert "PSF" in summary["limitation"]
        unsupported = dict(spec, field_psf_index=64)
        response = transport.post("/api/v1/lens-sensor-raw", json=unsupported)
        assert response.status_code == 422
        assert "stored PSF fields" in response.text

"""The cohort diagnostic has a typed API, Worker lifecycle and sealed export."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform import lens_sensor_isp_perception_cohort_job as cohort_job
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, canonical
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute as execute_worker

WORKSPACE = Path(__file__).resolve().parents[2]


def _spec():
    spec = cohort_job.default_spec()
    spec.update(
        evaluation_job_ids=["frame-a", "frame-b"],
        acknowledge_diagnostic_only=True,
    )
    return spec


def _assessment():
    cases = [
        {
            "evaluation_job_id": identity,
            "scene_family": scene,
            "single_frame_result_sha256": marker * 64,
            "annotation_artifact_sha256": marker * 64,
            "prediction_artifact_sha256": marker * 64,
            "model": {"checkpoint_sha256": "f" * 64},
        }
        for identity, scene, marker in (
            ("frame-a", "uniform_d65", "a"),
            ("frame-b", "slanted_edge_d65", "b"),
        )
    ]
    return {
        "schema": "camerae2e_lens_sensor_isp_prediction_cohort_v1",
        "cases": cases,
        "diagnostic_map": {"available": True, "value": {"map": 0.5}},
        "product_map": {"available": False, "reasons": ["Synthetic fixtures only."]},
    }


def test_spec_requires_explicit_bounded_cohort_and_unambiguous_thresholds():
    with pytest.raises(ValueError, match="acknowledgement"):
        cohort_job.specification(cohort_job.default_spec())
    with pytest.raises(ValueError, match="distinct"):
        cohort_job.specification({**_spec(), "evaluation_job_ids": ["frame-a", "frame-a"]})
    with pytest.raises(ValueError, match="exactly 2"):
        cohort_job.specification({
            **_spec(), "evaluation_job_ids": ["frame-a", "frame-b", "frame-c"]
        })
    with pytest.raises(ValueError, match="distinct percent keys"):
        cohort_job.specification({**_spec(), "iou_thresholds": [0.501, 0.504]})


def test_sdk_api_worker_readback_and_export_keep_diagnostic_scope(tmp_path, monkeypatch):
    report = _assessment()
    monkeypatch.setattr(cohort_job, "_assessment", lambda _service, _spec: deepcopy(report))
    state = tmp_path / "state"
    app = create_app(state, WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        assert client.lens_sensor_isp_perception_cohort_template()["specification"]["schema"] == (
            cohort_job.SCHEMA
        )
        queued = client.submit_lens_sensor_isp_perception_cohort(_spec())
        assert queued["status"] == "queued"
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute_worker(app.state.service, queued["id"])
        finished = client.wait(
            queued["id"], timeout=5,
            expected_kind="lens_sensor_isp_perception_cohort",
        )
        assert finished["status"] == "succeeded", finished.get("error")
        assert finished["result"]["summary"]["assessment"]["product_map"]["available"] is False
        assert finished["result"]["summary"]["assessment"]["diagnostic_map"]["value"]["map"] == 0.5
        assert client.lens_sensor_isp_perception_cohort(queued["id"])["id"] == queued["id"]
        assert any(
            row["id"] == queued["id"]
            for row in client.list_lens_sensor_isp_perception_cohorts()
        )
        exported = client.export_lens_sensor_isp_perception_cohort(queued["id"])
        assert canonical(exported) == canonical(finished)
        assert (
            app.state.service.registry.blob_path(finished["artifacts"][0]["sha256"])
            .read_bytes() == canonical(finished["result"]["summary"]).encode()
        )
        report["cases"][0]["model"]["checkpoint_sha256"] = "0" * 64
        with pytest.raises(IntegrityError, match="frozen sources"):
            cohort_job.get(app.state.service, queued["id"])
        assert transport.get(
            f"/api/v1/lens-sensor-isp-perception-cohorts/{queued['id']}/export"
        ).status_code == 409


def test_worker_refuses_source_change_after_submit(tmp_path, monkeypatch):
    report = _assessment()
    monkeypatch.setattr(cohort_job, "_assessment", lambda _service, _spec: deepcopy(report))
    service = PlatformService(tmp_path / "state", WORKSPACE)
    queued = cohort_job.submit(service, _spec())
    assert service.registry.claim()["id"] == queued["id"]
    report["cases"][0]["annotation_artifact_sha256"] = "0" * 64
    execute_worker(service, queued["id"])
    failed = service.registry.get_run(queued["id"])
    assert failed["status"] == "failed"
    assert "Frozen cohort sources" in failed["error"]
    assert not failed["artifacts"]

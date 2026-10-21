"""Retained RAW datasets copy source bytes and keep sensor-run lineage."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.registry import canonical, digest
from camera_platform.retained_raw_dataset_job import (
    default_spec,
    get,
    prepare,
    specification,
    submit,
)
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute
from pyisetcam.dataset import camerae2e_dataset_validate

WORKSPACE = Path(__file__).resolve().parents[2]


def _quality_source(service: PlatformService, *, npz_hash_override: str | None = None):
    source_raw = np.arange(36, dtype=np.int32).reshape(6, 6)
    source_npz = io.BytesIO()
    np.savez_compressed(source_npz, case_000_raw_dn_frame=source_raw)
    arrays = service.registry.store_blob(
        source_npz.getvalue(), "source-quality.npz", "application/octet-stream"
    )
    revision = {
        "run_id": "saved-run-1",
        "report_code": "SENSOR-1",
        "result_sha256": "a" * 64,
        "config_sha256": "b" * 64,
    }
    source_report = {
        "schema": "camerae2e_saved_sensor_quality_v1",
        "status": "completed",
        "revision": revision,
        "upstream_assets": {"digest": "upstream-1"},
        "cases": [
            {
                "scene": "uniform",
                "array_prefix": "case_000",
                "frame_seeds": [11, 12],
                "native_size": [6, 6],
            }
        ],
        "artifacts": {
            "storage": "local_output_dir",
            "npz_name": "saved_quality_arrays.npz",
            "npz_sha256": npz_hash_override or arrays["sha256"],
        },
    }
    report = service.registry.store_blob(
        (json.dumps(source_report, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        "source-quality.json",
        "application/json",
    )
    request = {
        "name": "completed quality source",
        "job_kind": "saved_sensor_quality",
        "specification": {"source": "synthetic-fixture"},
        "source_identities": [revision],
        "upstream_assets": {"digest": "upstream-1"},
    }
    queued = service.registry.submit(request, digest(request))
    assert service.registry.claim()["id"] == queued["id"]
    summary = {
        "schema": "camerae2e_saved_sensor_quality_study_v1",
        "status": "completed",
        "specification": request["specification"],
        "source_identities": request["source_identities"],
        "upstream_assets": request["upstream_assets"],
        "sensor_reports": [
            {key: value for key, value in source_report.items() if key != "artifacts"}
            | {"artifacts": {"json": report, "npz": arrays}}
        ],
    }
    result = service.registry.store_blob(
        canonical(summary).encode(), "saved-sensor-quality-result.json", "application/json"
    )
    service.registry.finish(
        queued["id"], result={"summary": summary, "artifacts": [report, arrays, result]}
    )
    return queued["id"], source_raw


def test_retained_raw_job_exports_exact_source_and_validates_bundle(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source_id, source_raw = _quality_source(service)
    spec = default_spec()
    spec.update(source_quality_job_id=source_id, acknowledge_simulation_limits=True)
    assert prepare(service, spec)["source_case"]["frame_seed"] == 11
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    finished = get(service, queued["id"])
    assert finished["status"] == "succeeded", finished.get("error")
    summary = finished["result"]["summary"]
    assert summary["raw_values_byte_equal"] is True
    assert summary["dataset_validation"] == {"ok": True, "issue_count": 0, "warning_count": 0}
    assert summary["raw_training_index"] == {"row_count": 1, "warning_count": 0}
    assert summary["source_binding"]["source_quality_job_id"] == source_id
    assert summary["source_binding"]["saved_sensor_revision"]["run_id"] == "saved-run-1"
    assert summary["source_binding"]["source_frame_seed"] == 11
    assert summary["perception_metric"]["available"] is False
    bundle_bytes = service.registry.blob_path(summary["bundle"]["sha256"]).read_bytes()
    assert hashlib.sha256(bundle_bytes).hexdigest() == summary["bundle"]["sha256"]
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with ZipFile(io.BytesIO(bundle_bytes)) as archive:
        archive.extractall(extracted)
    with np.load(extracted / "raw/retained_0000.npz", allow_pickle=False) as arrays:
        raw = arrays["raw"]
        assert raw.dtype == np.int32
        assert raw.tobytes(order="C") == source_raw.tobytes(order="C")
        assert arrays["sensor_digital"].size == 0
        assert arrays["seed"].tolist() == [11]
    assert (
        json.loads((extracted / "manifest.json").read_text())["source_binding"]
        == (summary["source_binding"])
    )
    monkeypatch.chdir(extracted)
    assert camerae2e_dataset_validate("manifest.json")["ok"]


def test_retained_raw_requires_completed_hash_bound_source(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source_id, _ = _quality_source(service, npz_hash_override="0" * 64)
    spec = default_spec()
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec.update(source_quality_job_id=source_id, acknowledge_simulation_limits=True)
    with pytest.raises(ValueError, match="unlabeled"):
        specification(dict(spec, labels="generated"))
    with pytest.raises(ValueError, match="split"):
        specification(dict(spec, split="unsafe"))
    with pytest.raises(ValueError, match="NPZ hash differs"):
        submit(service, spec)
    assert len(service.registry.list_runs()) == 1


def test_retained_raw_api_sdk_worker_and_zip_export(tmp_path):
    state = tmp_path / "state"
    service = PlatformService(state, WORKSPACE)
    source_id, source_raw = _quality_source(service)
    app = create_app(state, WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.retained_raw_dataset_template()
        assert any(item["id"] == source_id for item in template["source_quality_jobs"])
        spec = template["specification"]
        spec.update(source_quality_job_id=source_id, acknowledge_simulation_limits=True)
        queued = client.submit_retained_raw_dataset(spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="retained_raw_dataset")
        assert finished["status"] == "succeeded", finished.get("error")
        assert client.retained_raw_dataset(queued["id"])["status"] == "succeeded"
        assert any(item["id"] == queued["id"] for item in client.list_retained_raw_datasets())
        response = transport.get(f"/api/v1/retained-raw-datasets/{queued['id']}/export")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with ZipFile(io.BytesIO(response.content)) as archive:
            raw_npz = io.BytesIO(archive.read("raw/retained_0000.npz"))
            with np.load(raw_npz, allow_pickle=False) as raw:
                assert raw["raw"].tobytes(order="C") == source_raw.tobytes(order="C")

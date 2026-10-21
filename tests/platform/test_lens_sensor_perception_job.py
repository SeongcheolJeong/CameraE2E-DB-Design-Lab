"""Saved Lens RAW -> one retained dataset ZIP -> provenance-only perception job."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform.lens_sensor_perception import inspect_retained_lens_sensor_bundle
from camera_platform.lens_sensor_perception_job import (
    default_spec,
    install_routes,
)
from camera_platform.lens_sensor_perception_job import (
    execute as perception_execute,
)
from camera_platform.lens_sensor_perception_job import (
    get as perception_get,
)
from camera_platform.lens_sensor_perception_job import (
    submit as perception_submit,
)
from camera_platform.registry import canonical, digest
from camera_platform.retained_raw_dataset_job import (
    default_lens_spec,
)
from camera_platform.retained_raw_dataset_job import (
    execute as dataset_execute,
)
from camera_platform.retained_raw_dataset_job import (
    get as dataset_get,
)
from camera_platform.retained_raw_dataset_job import (
    submit as dataset_submit,
)
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]


def _saved_lens_raw(service: PlatformService) -> tuple[str, np.ndarray]:
    raw = np.arange(64, dtype=np.int32).reshape(8, 8)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_descriptor = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-original-raw.npy", "application/octet-stream"
    )
    raw_descriptor.update(dtype="int32", shape=[8, 8], unit="DN", source_kind="simulated")
    specification = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "seed": 7, "accept_approximate_lens_radiometry": True,
    }
    resource_preflight = {"scope": "central_full_raytrace"}
    identities = {
        "lens": {"revision_id": "lens-r1", "content_hash": "a" * 64},
        "sensor": {"run_id": "sensor-r1", "result_sha256": "b" * 64},
    }
    upstream = {"digest": "source-fixture"}
    request = {
        "name": "saved Lens RAW fixture",
        "job_kind": "lens_sensor_raw",
        "specification": specification,
        "source_identities": identities,
        "upstream_assets": upstream,
        "resource_preflight": resource_preflight,
    }
    queued = service.registry.submit(request, digest(request))
    assert service.registry.claim()["id"] == queued["id"]
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": specification,
        "source_identities": identities,
        "upstream_assets": upstream,
        "resource_preflight": resource_preflight,
        "preview": {
            "resource_preflight": resource_preflight,
            "raw_artifact": raw_descriptor,
            "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
            "physical_validation": "not_established",
            "capture": {"seed": 7, "sensor_size": [8, 8]},
            "lens_identity": identities["lens"],
            "sensor_revision": identities["sensor"],
            "scope": "approximate_geometric_central_field_raw_preview",
            "lens_model": {"field_psf_selection": None},
            "scene": {"kind": "slanted_edge"},
        },
    }
    result_descriptor = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-raw-preview-result.json", "application/json"
    )
    service.registry.finish(
        queued["id"],
        result={"summary": summary, "artifacts": [raw_descriptor, result_descriptor]},
    )
    return queued["id"], raw


def _dataset(service: PlatformService, source_id: str, *, split: str = "train") -> dict:
    spec = default_lens_spec()
    spec.update(
        source_job_id=source_id,
        split=split,
        acknowledge_simulation_limits=True,
    )
    queued = dataset_submit(service, spec)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    service.registry.finish(queued["id"], result=dataset_execute(service, claimed))
    return dataset_get(service, queued["id"])


def test_perception_job_reuses_dataset_bundle_and_keeps_metrics_unavailable(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source_id, source_raw = _saved_lens_raw(service)
    dataset = _dataset(service, source_id)
    bundle = dataset["result"]["summary"]["bundle"]
    before_blobs = {path.name for path in service.registry.blobs.iterdir()}
    spec = default_spec()
    spec.update(source_dataset_job_id=dataset["id"], acknowledge_simulation_limits=True)
    queued = perception_submit(service, spec)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    service.registry.finish(queued["id"], result=perception_execute(service, claimed))
    finished = perception_get(service, queued["id"])
    summary = finished["result"]["summary"]
    assert summary["dataset_bundle"] == bundle
    assert summary["source_lens_sensor_job_id"] == source_id
    assert summary["raw_content_sha256"] == dataset["result"]["summary"]["raw_content_sha256"]
    assert summary["split_isolation"]["matching_dataset_job_ids"] == [dataset["id"]]
    assert summary["split_isolation"]["holdout_independence_claimed"] is False
    assert summary["annotation_provenance"]["ground_truth_available"] is False
    assert summary["perception_metric"]["available"] is False
    assert summary["detector_evaluation_performed"] is False
    assert summary["portable_raw_index"]["pyisetcam_native_index"] is False
    after_blobs = {path.name for path in service.registry.blobs.iterdir()}
    assert len(after_blobs - before_blobs) == 2  # index and report, never a second RAW bundle
    with ZipFile(service.registry.blob_path(bundle["sha256"])) as archive:
        original = archive.read("raw/retained_0000.npz")
    with np.load(io.BytesIO(original), allow_pickle=False) as arrays:
        assert arrays["raw"].tobytes() == source_raw.tobytes()
    index = json.loads(
        service.registry.blob_path(summary["portable_raw_index"]["artifact"]["sha256"]).read_text()
    )
    assert index["raw"] == "raw/retained_0000.npz"
    assert index["bundle_sha256"] == bundle["sha256"]
    assert index["objects"] == []

    app = FastAPI()
    install_routes(app, service)
    with TestClient(app) as client:
        template = client.get("/api/v1/lens-sensor-perception/template").json()
        assert template["source_dataset_jobs"][0]["id"] == dataset["id"]
        assert client.get(f"/api/v1/lens-sensor-perception/{queued['id']}").status_code == 200
        exported = client.get(f"/api/v1/lens-sensor-perception/{queued['id']}/export")
        assert exported.status_code == 200
        assert exported.json()["perception_metric"]["available"] is False


def test_perception_job_rejects_reused_raw_across_registry_splits(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source_id, _ = _saved_lens_raw(service)
    first = _dataset(service, source_id, split="train")
    second = _dataset(service, source_id, split="test")
    assert first["id"] != second["id"]
    spec = default_spec()
    spec.update(source_dataset_job_id=first["id"], acknowledge_simulation_limits=True)
    with pytest.raises(ValueError, match="another completed retained dataset"):
        perception_submit(service, spec)


def test_pure_bundle_inspector_refuses_unlabeled_metadata_invention(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source_id, _ = _saved_lens_raw(service)
    dataset = _dataset(service, source_id)
    lens = service.registry.get_run(source_id)
    bundle = dataset["result"]["summary"]["bundle"]
    original = service.registry.blob_path(bundle["sha256"])
    inspected = inspect_retained_lens_sensor_bundle(dataset, lens, bundle_path=original)
    assert inspected["annotation_provenance"]["ground_truth_available"] is False

    altered = tmp_path / "altered.zip"
    with ZipFile(original) as source, ZipFile(altered, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "labels/retained_0000.json":
                labels = json.loads(content)
                labels["labels"]["objects"] = [{"label": "invented"}]
                content = canonical(labels).encode()
            target.writestr(name, content)
    dataset["result"]["summary"]["bundle"] = dict(
        bundle,
        sha256=hashlib.sha256(altered.read_bytes()).hexdigest(),
        bytes=altered.stat().st_size,
    )
    dataset["artifacts"] = [dataset["result"]["summary"]["bundle"]]
    with pytest.raises(ValueError, match="annotation metadata"):
        inspect_retained_lens_sensor_bundle(dataset, lens, bundle_path=altered)

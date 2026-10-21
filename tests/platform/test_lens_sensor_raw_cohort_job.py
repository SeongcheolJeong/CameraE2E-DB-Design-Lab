"""Registry cohort job exports multiple completed Lens RAW sources without recapture."""

from __future__ import annotations

import hashlib
import io
import json
import os
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_raw_cohort_job import (
    ANNOTATED_SCHEMA,
    _source_entry,
    default_annotated_spec,
    default_spec,
    execute,
    get,
    prepare,
    specification,
    submit,
)
from camera_platform.registry import canonical, digest
from camera_platform.retained_raw_dataset import raw_annotation_template
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from pyisetcam.dataset import camerae2e_dataset_validate

WORKSPACE = Path(__file__).resolve().parents[2]


def _saved_lens_job(
    service: PlatformService, *, offset: int, seed: int = 7,
    scene_kind: str = "slanted_edge",
) -> tuple[str, np.ndarray]:
    raw = np.arange(64, dtype=np.int32).reshape(8, 8) + offset
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_descriptor = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-original-raw.npy", "application/octet-stream"
    )
    raw_descriptor.update(dtype="int32", shape=[8, 8], unit="DN", source_kind="simulated")
    spec = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "seed": seed, "accept_approximate_lens_radiometry": True,
        "scene_kind": scene_kind,
    }
    resource_preflight = {"scope": "central_full_raytrace"}
    source_identities = {
        "lens": {"revision_id": f"lens-{offset}", "asset_sha256": "a" * 64},
        "sensor": {"run_id": f"sensor-{offset}", "result_sha256": "b" * 64},
    }
    request = {
        "name": f"completed Lens RAW {offset}",
        "job_kind": "lens_sensor_raw",
        "plugin_id": "camerae2e_lens_sensor_raw_preview_study_v1",
        "specification": spec,
        "source_identities": source_identities,
        "upstream_assets": {"digest": "offline-assets-1"},
        "resource_preflight": resource_preflight,
    }
    queued = service.registry.submit(request, digest(request))
    assert service.registry.claim()["id"] == queued["id"]
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": spec,
        "source_identities": source_identities,
        "upstream_assets": request["upstream_assets"],
        "resource_preflight": resource_preflight,
        "preview": {
            "resource_preflight": resource_preflight,
            "raw_artifact": raw_descriptor,
            "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
            "physical_validation": "not_established",
            "capture": {"seed": seed, "sensor_size": [8, 8]},
            "lens_identity": source_identities["lens"],
            "sensor_revision": source_identities["sensor"],
            "scope": "approximate_geometric_central_field_raw_preview",
            "lens_model": {"field_psf_selection": None},
            "scene": {"kind": f"{scene_kind}_d65"},
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


def _spec(*pairs: tuple[str, str]) -> dict:
    spec = default_spec()
    spec.update(
        source_jobs=[{"source_job_id": identity, "split": split} for identity, split in pairs],
        acknowledge_simulation_limits=True,
    )
    return spec


def test_cohort_job_preserves_multiple_registry_raw_frames_and_manifest(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    first_id, first_raw = _saved_lens_job(service, offset=0)
    second_id, second_raw = _saved_lens_job(service, offset=100)
    spec = _spec((first_id, "train"), (second_id, "test"))
    frozen = prepare(service, spec)
    assert [entry["source_job_id"] for entry in frozen["source_entries"]] == [
        first_id, second_id
    ]
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    result = execute(service, service.registry.get_run(queued["id"]))
    service.registry.finish(queued["id"], result=result)
    finished = get(service, queued["id"])
    assert finished["status"] == "succeeded"
    summary = finished["result"]["summary"]
    assert summary["source_job_count"] == summary["case_count"] == 2
    assert summary["dataset_validation"]["ok"] is True
    assert summary["perception_metric"]["available"] is False
    assert summary["split_policy"]["identical_raw_across_splits_rejected"] is True
    assert summary["split_policy"]["holdout_independence_claimed"] is False
    assert len(finished["artifacts"]) == 3
    bundle = service.registry.blob_path(summary["bundle"]["sha256"])
    manifest_bytes = service.registry.blob_path(summary["manifest"]["sha256"]).read_bytes()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with ZipFile(bundle) as archive:
        assert archive.read("manifest.json") == manifest_bytes
        archive.extractall(extracted)
    manifest = json.loads(manifest_bytes)
    originals = {first_id: first_raw, second_id: second_raw}
    for record in manifest["records"]:
        job_id = record["source_binding"]["source_lens_sensor_job_id"]
        with np.load(extracted / record["raw"], allow_pickle=False) as arrays:
            assert arrays["raw"].tobytes() == originals[job_id].tobytes()
    monkeypatch.chdir(extracted)
    assert camerae2e_dataset_validate("manifest.json")["ok"]


def test_cohort_job_rejects_same_raw_across_splits_and_frozen_tamper(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    first_id, _ = _saved_lens_job(service, offset=0)
    second_id, _ = _saved_lens_job(service, offset=0, seed=8)
    with pytest.raises(ValueError, match="cannot be reused across cohort splits"):
        submit(service, _spec((first_id, "train"), (second_id, "test")))
    assert len(service.registry.list_runs()) == 2
    spec = _spec((first_id, "train"), (second_id, "train"))
    queued = submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    forged = deepcopy(service.registry.get_run(queued["id"]))
    forged["source_entries"][0]["source_binding"]["source_frame_seed"] = 99
    with pytest.raises(Exception, match="Frozen Lens RAW cohort sources"):
        execute(service, forged)
    result = execute(service, service.registry.get_run(queued["id"]))
    service.registry.finish(queued["id"], result=result)
    assert get(service, queued["id"])["result"]["summary"]["case_count"] == 1


def test_cohort_spec_requires_bounded_distinct_completed_sources():
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(default_spec())
    with pytest.raises(ValueError, match="2..64"):
        specification(_spec(("one", "train")))
    with pytest.raises(ValueError, match="distinct"):
        specification(_spec(("one", "train"), ("one", "test")))
    with pytest.raises(ValueError, match="unlabeled"):
        specification({**_spec(("one", "train"), ("two", "test")), "labels": "generated"})
    with pytest.raises(ValueError, match="split"):
        specification(_spec(("one", "train"), ("two", "unknown")))


def test_cohort_api_runs_two_saved_raw_sources_in_child_worker(tmp_path):
    state = tmp_path / "state"
    service = PlatformService(state, WORKSPACE)
    first_id, first_raw = _saved_lens_job(service, offset=0)
    second_id, second_raw = _saved_lens_job(service, offset=100)
    app = create_app(state, WORKSPACE, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        worker = app.state.worker
        assert worker is not None and worker.poll() is None and worker.pid != os.getpid()
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        template = client.lens_sensor_raw_cohort_template()
        assert {first_id, second_id}.issubset(
            {item["id"] for item in template["source_jobs"]}
        )
        queued = client.submit_lens_sensor_raw_cohort(
            _spec((first_id, "train"), (second_id, "test"))
        )
        finished = client.wait(queued["id"], timeout=30, expected_kind="lens_sensor_raw_cohort")
        assert finished["status"] == "succeeded", finished.get("error")
        assert client.lens_sensor_raw_cohort(queued["id"])["status"] == "succeeded"
        assert any(item["id"] == queued["id"] for item in client.list_lens_sensor_raw_cohorts())
        assert worker.poll() is None
        exported = transport.get(f"/api/v1/lens-sensor-raw-cohorts/{queued['id']}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        with ZipFile(io.BytesIO(exported.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["case_count"] == 2
            originals = {first_id: first_raw, second_id: second_raw}
            for record in manifest["records"]:
                source_id = record["source_binding"]["source_lens_sensor_job_id"]
                with np.load(io.BytesIO(archive.read(record["raw"])), allow_pickle=False) as raw:
                    assert raw["raw"].tobytes() == originals[source_id].tobytes()


def test_annotated_cohort_api_merges_imported_raw_boxes_without_metric(tmp_path):
    state = tmp_path / "state"
    service = PlatformService(state, WORKSPACE)
    first_id, first_raw = _saved_lens_job(service, offset=0, scene_kind="uniform")
    second_id, second_raw = _saved_lens_job(service, offset=100, scene_kind="slanted_edge")
    app = create_app(state, WORKSPACE, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        client = object.__new__(PlatformClient)
        client.http = transport
        template = client.lens_sensor_raw_cohort_template()
        assert template["annotated_specification"]["schema"] == ANNOTATED_SCHEMA
        annotation_ids = []
        for source_id, label in ((first_id, "pedestrian"), (second_id, "vehicle")):
            source_template = client.retained_raw_dataset_template(
                source_kind="lens_sensor_raw_labeled", source_job_id=source_id
            )
            annotation = source_template["annotation_template"]
            annotation["provenance"].update(
                provider_id="independent-reviewer-declared",
                annotation_set_id=f"set-{source_id}",
            )
            annotation["objects"] = [
                {"label": label, "bbox_xyxy": [1, 1, 5, 6]}
            ]
            imported = client.import_retained_raw_annotation(annotation)
            annotation_ids.append(imported["artifact"]["sha256"])
        spec = template["annotated_specification"]
        spec.update(
            source_jobs=[
                {"source_job_id": first_id, "split": "train",
                 "annotation_artifact_sha256": annotation_ids[0]},
                {"source_job_id": second_id, "split": "test",
                 "annotation_artifact_sha256": annotation_ids[1]},
            ],
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_lens_sensor_raw_cohort(spec)
        finished = client.wait(
            queued["id"], timeout=30, expected_kind="lens_sensor_raw_cohort"
        )
        assert finished["status"] == "succeeded", finished.get("error")
        result = client.lens_sensor_raw_cohort(queued["id"])["result"]["summary"]
        assert result["schema"] == ANNOTATED_SCHEMA
        assert result["labels"] == "user_provided_unverified"
        assert result["class_map"] == {"pedestrian": 0, "vehicle": 1}
        assert result["perception_metric"]["available"] is False
        assert result["annotation_provenance"]["independence_verified"] is False
        exported = transport.get(
            f"/api/v1/lens-sensor-raw-cohorts/{queued['id']}/export"
        )
        assert exported.status_code == 200
        with ZipFile(io.BytesIO(exported.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["splits"] == {"train": 1, "test": 1}
            originals = {first_id: first_raw, second_id: second_raw}
            for record in manifest["records"]:
                source_id = record["source_binding"]["source_lens_sensor_job_id"]
                with np.load(io.BytesIO(archive.read(record["raw"])), allow_pickle=False) as arrays:
                    assert arrays["raw"].tobytes() == originals[source_id].tobytes()
                annotation_bytes = archive.read(record["source_annotation"])
                assert hashlib.sha256(annotation_bytes).hexdigest() in annotation_ids
                labels = json.loads(archive.read(record["labels"]))["labels"]
                assert labels["source"] == "user_provided_unverified"
                assert labels["source_annotation_sha256"] in annotation_ids


def test_annotated_cohort_job_rejects_scene_family_leak_before_queue(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    first_id, _ = _saved_lens_job(service, offset=0)
    second_id, _ = _saved_lens_job(service, offset=100)
    selections = []
    for source_id, split in ((first_id, "train"), (second_id, "test")):
        _, source = _source_entry(
            service, {"source_job_id": source_id, "split": split}, annotated=False
        )
        from camera_platform.lens_sensor_perception import _source as checked_source

        verified, _ = checked_source(
            source["job"], source["source_report_path"], source["source_raw_path"]
        )
        annotation = raw_annotation_template(verified["binding"], [8, 8])
        annotation["provenance"].update(provider_id="reviewer", annotation_set_id=source_id)
        annotation["objects"] = [{"label": "car", "bbox_xyxy": [1, 1, 6, 6]}]
        artifact = service.registry.store_blob(
            canonical(annotation).encode(),
            "retained-raw-source-annotation.json",
            "application/json",
        )
        selections.append({
            "source_job_id": source_id, "split": split,
            "annotation_artifact_sha256": artifact["sha256"],
        })
    spec = default_annotated_spec()
    spec.update(source_jobs=selections, acknowledge_simulation_limits=True)
    with pytest.raises(ValueError, match="synthetic scene family"):
        submit(service, spec)
    assert len(service.registry.list_runs()) == 2

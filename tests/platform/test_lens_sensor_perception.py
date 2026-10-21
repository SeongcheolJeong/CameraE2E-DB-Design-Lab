"""A saved geometric Lens × Sensor RAW has one exact, unlabeled handoff."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.lens_sensor_perception import export_lens_sensor_perception_handoff
from camera_platform.registry import canonical
from pyisetcam.dataset import camerae2e_dataset_validate


def _saved_source(tmp_path, *, identity="run-raw-1", offset=0):
    raw = np.arange(64, dtype=np.int32).reshape(8, 8) + offset
    raw_bytes = io.BytesIO()
    np.save(raw_bytes, raw, allow_pickle=False)
    raw_content = raw_bytes.getvalue()
    raw_descriptor = {
        "name": "lens-sensor-original-raw.npy",
        "sha256": hashlib.sha256(raw_content).hexdigest(),
        "bytes": len(raw_content),
        "dtype": "int32",
        "shape": [8, 8],
        "unit": "DN",
        "source_kind": "simulated",
    }
    spec = {"seed": 7, "accept_approximate_lens_radiometry": True}
    sources = {
        "lens": {"revision_id": "lens-rev-1", "asset_sha256": "a" * 64},
        "sensor": {"run_id": "sensor-run-1", "result_sha256": "b" * 64},
    }
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": spec,
        "source_identities": sources,
        "upstream_assets": {"digest": "offline-assets-1"},
        "preview": {
            "raw_artifact": raw_descriptor,
            "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
            "physical_validation": "not_established",
            "capture": {"seed": 7},
            "lens_identity": sources["lens"],
            "sensor_revision": sources["sensor"],
            "scope": "approximate_geometric_central_field_raw_preview",
            "lens_model": {"field_psf_selection": None},
            "scene": {"kind": "slanted_edge_d65"},
        },
    }
    result_content = canonical(summary).encode()
    result_descriptor = {
        "name": "lens-sensor-raw-preview-result.json",
        "sha256": hashlib.sha256(result_content).hexdigest(),
        "bytes": len(result_content),
    }
    raw_path = tmp_path / f"{identity}-source.npy"
    report_path = tmp_path / f"{identity}-source.json"
    raw_path.write_bytes(raw_content)
    report_path.write_bytes(result_content)
    artifacts = [raw_descriptor, result_descriptor]
    job = {
        "id": identity,
        "job_kind": "lens_sensor_raw",
        "status": "succeeded",
        "specification": spec,
        "source_identities": sources,
        "upstream_assets": summary["upstream_assets"],
        "result": {"summary": summary, "artifacts": artifacts},
        "artifacts": artifacts,
    }
    return job, report_path, raw_path, raw


def _export(job, report_path, raw_path, output_dir, *, split="train", manifests=()):
    return export_lens_sensor_perception_handoff(
        job,
        source_report_path=report_path,
        source_raw_path=raw_path,
        output_dir=output_dir,
        split=split,
        existing_manifests=manifests,
    )


def test_exact_raw_handoff_is_unlabeled_and_has_no_perception_metric(tmp_path):
    job, report_path, raw_path, original = _saved_source(tmp_path)
    output = tmp_path / "dataset"
    handoff = _export(job, report_path, raw_path, output)
    assert handoff["source_binding"]["source_lens_sensor_job_id"] == job["id"]
    assert handoff["source_binding"]["lens_identity"] == job["source_identities"]["lens"]
    assert handoff["source_binding"]["saved_sensor_revision"] == job["source_identities"]["sensor"]
    assert handoff["annotation_provenance"]["ground_truth_available"] is False
    assert handoff["perception_metric"]["available"] is False
    assert handoff["detector_evaluation_performed"] is False
    assert handoff["split_isolation"]["holdout_independence_claimed"] is False
    assert handoff["dataset_manifest_sha256"] == hashlib.sha256(
        (output / "manifest.json").read_bytes()
    ).hexdigest()
    assert handoff["raw_perception_index_sha256"] == hashlib.sha256(
        (output / "perception_index/raw_manifest.jsonl").read_bytes()
    ).hexdigest()
    with np.load(output / "raw/retained_0000.npz", allow_pickle=False) as retained:
        assert retained["raw"].dtype == np.int32
        assert retained["raw"].tobytes() == original.tobytes()
    labels = json.loads((output / "labels/retained_0000.json").read_text())
    assert labels["labels"] == {"objects": [], "masks": [], "source": "unlabeled"}
    assert camerae2e_dataset_validate(output / "manifest.json")["ok"]
    index = json.loads((output / "perception_index/raw_manifest.jsonl").read_text())
    assert index["objects"] == []
    assert index["raw_dtype"] == "int32"
    written = json.loads((output / "perception-handoff.json").read_text())
    assert written["source_binding"] == handoff["source_binding"]


def test_source_artifact_or_frozen_identity_mismatch_refuses_export(tmp_path):
    job, report_path, raw_path, _ = _saved_source(tmp_path)
    output = tmp_path / "dataset"
    raw_path.write_bytes(raw_path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="artifact bytes"):
        _export(job, report_path, raw_path, output)
    assert not output.exists()
    job, report_path, raw_path, _ = _saved_source(tmp_path, identity="run-raw-2")
    altered = deepcopy(job)
    altered["source_identities"] = deepcopy(job["source_identities"])
    altered["source_identities"]["lens"]["revision_id"] = "other-lens"
    with pytest.raises(ValueError, match="frozen source"):
        _export(altered, report_path, raw_path, output)
    assert not output.exists()


def test_declared_cohort_rejects_duplicate_raw_across_splits(tmp_path):
    job, report_path, raw_path, _ = _saved_source(tmp_path)
    first = tmp_path / "train"
    _export(job, report_path, raw_path, first, split="train")
    with pytest.raises(ValueError, match="already occurs"):
        _export(
            job, report_path, raw_path, tmp_path / "test", split="test",
            manifests=[first / "manifest.json"],
        )
    assert not (tmp_path / "test").exists()
    other, other_report, other_raw, _ = _saved_source(
        tmp_path, identity="run-raw-other", offset=100
    )
    second = _export(
        other, other_report, other_raw, tmp_path / "test", split="test",
        manifests=[first / "manifest.json"],
    )
    assert len(second["split_isolation"]["checked_manifests"]) == 1
    assert second["split_isolation"]["global_cross_export_exclusivity_enforced"] is False

"""A cohort of saved Lens x Sensor RAW jobs retains bytes and split provenance."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from zipfile import ZipFile

import numpy as np
import pytest

from camera_platform.lens_sensor_perception import _source
from camera_platform.lens_sensor_raw_cohort import export_lens_sensor_raw_cohort
from camera_platform.registry import canonical
from camera_platform.retained_raw_dataset import raw_annotation_template
from pyisetcam.dataset import camerae2e_dataset_validate


def _saved_source(tmp_path, *, job_id: str, offset: int = 0, seed: int = 7):
    raw = np.arange(64, dtype=np.int32).reshape(8, 8) + offset
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_bytes = stream.getvalue()
    raw_descriptor = {
        "name": "lens-sensor-original-raw.npy",
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "bytes": len(raw_bytes),
        "dtype": "int32",
        "shape": [8, 8],
        "unit": "DN",
        "source_kind": "simulated",
    }
    spec = {"seed": seed, "accept_approximate_lens_radiometry": True}
    identities = {
        "lens": {"revision_id": f"lens-{job_id}", "asset_sha256": "a" * 64},
        "sensor": {"run_id": f"sensor-{job_id}", "result_sha256": "b" * 64},
    }
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": spec,
        "source_identities": identities,
        "upstream_assets": {"digest": "offline-assets-1"},
        "preview": {
            "raw_artifact": raw_descriptor,
            "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
            "physical_validation": "not_established",
            "capture": {"seed": seed},
            "lens_identity": identities["lens"],
            "sensor_revision": identities["sensor"],
            "scope": "approximate_geometric_central_field_raw_preview",
            "lens_model": {"field_psf_selection": None},
            "scene": {"kind": "slanted_edge_d65"},
        },
    }
    result_bytes = canonical(summary).encode()
    result_descriptor = {
        "name": "lens-sensor-raw-preview-result.json",
        "sha256": hashlib.sha256(result_bytes).hexdigest(),
        "bytes": len(result_bytes),
    }
    raw_path = tmp_path / f"{job_id}.npy"
    report_path = tmp_path / f"{job_id}.json"
    raw_path.write_bytes(raw_bytes)
    report_path.write_bytes(result_bytes)
    artifacts = [raw_descriptor, result_descriptor]
    job = {
        "id": job_id,
        "job_kind": "lens_sensor_raw",
        "status": "succeeded",
        "specification": spec,
        "source_identities": identities,
        "upstream_assets": summary["upstream_assets"],
        "result": {"summary": summary, "artifacts": artifacts},
        "artifacts": artifacts,
    }
    return job, report_path, raw_path, raw


def _input(source, split):
    job, report_path, raw_path, _ = source
    return {
        "job": job,
        "source_report_path": report_path,
        "source_raw_path": raw_path,
        "split": split,
    }


def _annotated_input(source, split, *, label, provider):
    selected = _input(source, split)
    verified, _ = _source(
        selected["job"], selected["source_report_path"], selected["source_raw_path"]
    )
    annotation = raw_annotation_template(verified["binding"], list(verified["raw"].shape))
    annotation["provenance"].update(
        provider_id=provider, annotation_set_id=f"{provider}-set",
    )
    annotation["objects"] = [{"label": label, "bbox_xyxy": [1, 1, 5, 6]}]
    return {
        **selected,
        "annotation_document": annotation,
        "annotation_artifact_sha256": hashlib.sha256(canonical(annotation).encode()).hexdigest(),
    }


def test_three_splits_preserve_distinct_original_raw_in_portable_zip(tmp_path, monkeypatch):
    source_a = _saved_source(tmp_path, job_id="raw-a", offset=0)
    source_b = _saved_source(tmp_path, job_id="raw-b", offset=100)
    source_c = _saved_source(tmp_path, job_id="raw-c", offset=200)
    destination = tmp_path / "cohort"
    result = export_lens_sensor_raw_cohort(
        [_input(source_a, "train"), _input(source_b, "validation"), _input(source_c, "test")],
        output_dir=destination,
    )
    assert result["status"] == "completed"
    assert result["source_job_count"] == result["case_count"] == 3
    assert result["dataset_validation"]["ok"] is True
    assert result["raw_training_index"]["row_count"] == 3
    assert result["annotation_provenance"]["ground_truth_available"] is False
    assert result["perception_metric"]["available"] is False
    assert result["detector_evaluation_performed"] is False
    assert result["split_policy"]["holdout_independence_claimed"] is False
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["splits"] == {"train": 1, "validation": 1, "test": 1}
    assert manifest["case_count"] == 3
    originals = {source[0]["id"]: source[3] for source in (source_a, source_b, source_c)}
    for record in manifest["records"]:
        assert len(record["source_bindings"]) == 1
        source_id = record["source_binding"]["source_lens_sensor_job_id"]
        assert source_id in originals
        with np.load(destination / record["raw"], allow_pickle=False) as arrays:
            assert arrays["raw"].dtype == np.int32
            assert arrays["raw"].tobytes() == originals[source_id].tobytes()
            assert arrays["sensor_digital"].size == 0
        labels = json.loads((destination / record["labels"]).read_text())
        assert labels["labels"] == {"objects": [], "masks": [], "source": "unlabeled"}
    assert camerae2e_dataset_validate(destination / "manifest.json")["ok"]
    assert (
        hashlib.sha256((destination / "lens-sensor-raw-cohort.zip").read_bytes()).hexdigest()
        == result["integrity"]["zip_sha256"]
    )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with ZipFile(destination / "lens-sensor-raw-cohort.zip") as archive:
        assert len(archive.namelist()) == 3 + 2 * 3
        archive.extractall(extracted)
    monkeypatch.chdir(extracted)
    assert camerae2e_dataset_validate("manifest.json")["ok"]


def test_same_split_duplicate_raw_is_stored_once_with_both_job_bindings(tmp_path):
    source_a = _saved_source(tmp_path, job_id="raw-a", seed=7)
    source_b = _saved_source(tmp_path, job_id="raw-b", seed=8)
    result = export_lens_sensor_raw_cohort(
        [_input(source_b, "train"), _input(source_a, "train")],
        output_dir=tmp_path / "cohort",
    )
    assert result["source_job_count"] == 2
    assert result["case_count"] == 1
    assert result["split_policy"]["same_split_duplicate_source_jobs_coalesced"] == 1
    manifest = json.loads((tmp_path / "cohort/manifest.json").read_text())
    record = manifest["records"][0]
    assert [
        item["source_lens_sensor_job_id"] for item in record["source_bindings"]
    ] == ["raw-a", "raw-b"]
    assert record["seed"] == 7
    with ZipFile(tmp_path / "cohort/lens-sensor-raw-cohort.zip") as archive:
        assert len([name for name in archive.namelist() if name.startswith("raw/")]) == 1


def test_identical_raw_across_splits_rejected_before_output(tmp_path):
    source_a = _saved_source(tmp_path, job_id="raw-a")
    source_b = _saved_source(tmp_path, job_id="raw-b")
    destination = tmp_path / "cohort"
    with pytest.raises(ValueError, match="cannot be reused across cohort splits"):
        export_lens_sensor_raw_cohort(
            [_input(source_a, "train"), _input(source_b, "test")],
            output_dir=destination,
        )
    assert not destination.exists()


def test_declared_prior_manifest_and_tampered_source_fail_closed(tmp_path):
    first = _saved_source(tmp_path, job_id="raw-a", offset=0)
    other = _saved_source(tmp_path, job_id="raw-b", offset=100)
    third = _saved_source(tmp_path, job_id="raw-c", offset=200)
    export_lens_sensor_raw_cohort(
        [_input(first, "train"), _input(other, "validation")],
        output_dir=tmp_path / "first",
    )
    with pytest.raises(ValueError, match="already occurs"):
        export_lens_sensor_raw_cohort(
            [_input(first, "test"), _input(third, "train")],
            output_dir=tmp_path / "second",
            existing_manifests=[tmp_path / "first/manifest.json"],
        )
    assert not (tmp_path / "second").exists()
    tampered = _saved_source(tmp_path, job_id="raw-d", offset=300)
    tampered[2].write_bytes(tampered[2].read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="artifact bytes"):
        export_lens_sensor_raw_cohort(
            [_input(other, "train"), _input(tampered, "test")],
            output_dir=tmp_path / "tampered",
        )
    assert not (tmp_path / "tampered").exists()


def test_unsucceeded_or_duplicate_job_is_rejected(tmp_path):
    source_a = _saved_source(tmp_path, job_id="raw-a")
    source_b = _saved_source(tmp_path, job_id="raw-b", offset=100)
    failed = deepcopy(source_b[0])
    failed["status"] = "failed"
    with pytest.raises(ValueError, match="succeeded"):
        export_lens_sensor_raw_cohort(
            [_input(source_a, "train"), {**_input(source_b, "test"), "job": failed}],
            output_dir=tmp_path / "failed",
        )
    with pytest.raises(ValueError, match="distinct"):
        export_lens_sensor_raw_cohort(
            [_input(source_a, "train"), _input(source_a, "train")],
            output_dir=tmp_path / "duplicate",
        )


def test_annotated_cohort_preserves_source_raw_and_boxes_with_global_classes(
    tmp_path, monkeypatch
):
    first = _saved_source(tmp_path, job_id="raw-a", offset=0)
    second = _saved_source(tmp_path, job_id="raw-b", offset=100)
    inputs = [
        _annotated_input(first, "train", label="vehicle", provider="reviewer-a"),
        _annotated_input(second, "train", label="pedestrian", provider="reviewer-b"),
    ]
    output = tmp_path / "annotated"
    result = export_lens_sensor_raw_cohort(
        inputs, output_dir=output, labels="user_provided_unverified"
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["class_map"] == {"pedestrian": 0, "vehicle": 1}
    assert manifest["labels"] == "user_provided_unverified"
    assert manifest["split_policy"]["holdout_independence_claimed"] is False
    assert result["perception_metric"]["available"] is False
    assert result["annotation_provenance"]["independence_verified"] is False
    assert result["dataset_validation"]["ok"] is True
    assert result["raw_training_index"]["row_count"] == 2
    for record in manifest["records"]:
        binding = record["source_binding"]
        source = next(
            item for item in inputs
            if item["job"]["id"] == binding["source_lens_sensor_job_id"]
        )
        # The RAW NPZ payload and source annotation remain separately hash bound.
        with np.load(output / record["raw"], allow_pickle=False) as arrays:
            expected = first[3] if source["job"]["id"] == "raw-a" else second[3]
            assert arrays["raw"].tobytes() == expected.tobytes()
        annotation_bytes = (output / record["source_annotation"]).read_bytes()
        assert hashlib.sha256(annotation_bytes).hexdigest() == source[
            "annotation_artifact_sha256"
        ]
        labels = json.loads((output / record["labels"]).read_text())["labels"]
        source_object = source["annotation_document"]["objects"][0]
        assert labels["objects"] == [{
            **source_object, "class_id": manifest["class_map"][source_object["label"]]
        }]
        assert labels["source"] == "user_provided_unverified"
    with ZipFile(output / "lens-sensor-raw-cohort.zip") as archive:
        assert len(archive.namelist()) == 3 + 3 * 2
        assert all(
            record["source_annotation"] in archive.namelist()
            for record in manifest["records"]
        )
        extracted = tmp_path / "annotated-extracted"
        extracted.mkdir()
        archive.extractall(extracted)
    monkeypatch.chdir(extracted)
    assert camerae2e_dataset_validate("manifest.json")["ok"]


def test_annotated_cohort_rejects_scene_family_leak_and_duplicate_raw(tmp_path):
    first = _saved_source(tmp_path, job_id="raw-a", offset=0)
    second = _saved_source(tmp_path, job_id="raw-b", offset=100)
    third = _saved_source(tmp_path, job_id="raw-c", offset=0, seed=8)
    a = _annotated_input(first, "train", label="vehicle", provider="reviewer-a")
    b = _annotated_input(second, "test", label="vehicle", provider="reviewer-b")
    with pytest.raises(ValueError, match="synthetic scene family"):
        export_lens_sensor_raw_cohort(
            [a, b], output_dir=tmp_path / "scene-leak", labels="user_provided_unverified"
        )
    assert not (tmp_path / "scene-leak").exists()
    c = _annotated_input(third, "train", label="vehicle", provider="reviewer-c")
    with pytest.raises(ValueError, match="distinct RAW"):
        export_lens_sensor_raw_cohort(
            [a, c], output_dir=tmp_path / "duplicate-labeled",
            labels="user_provided_unverified",
        )
    assert not (tmp_path / "duplicate-labeled").exists()
    tampered = {**a, "annotation_artifact_sha256": "0" * 64}
    with pytest.raises(ValueError, match="annotation canonical SHA"):
        export_lens_sensor_raw_cohort(
            [tampered, _annotated_input(second, "train", label="person", provider="r2")],
            output_dir=tmp_path / "tampered-annotation",
            labels="user_provided_unverified",
        )
    assert not (tmp_path / "tampered-annotation").exists()


def test_annotated_cohort_checks_scene_family_in_declared_prior_manifest(tmp_path):
    first = _saved_source(tmp_path, job_id="raw-a", offset=0)
    second = _saved_source(tmp_path, job_id="raw-b", offset=100)
    export_lens_sensor_raw_cohort(
        [_input(first, "train"), _input(second, "train")],
        output_dir=tmp_path / "prior",
    )
    third = _saved_source(tmp_path, job_id="raw-c", offset=200)
    fourth = _saved_source(tmp_path, job_id="raw-d", offset=300)
    with pytest.raises(ValueError, match="declared existing cohort split"):
        export_lens_sensor_raw_cohort(
            [
                _annotated_input(third, "test", label="vehicle", provider="reviewer-c"),
                _annotated_input(fourth, "test", label="person", provider="reviewer-d"),
            ],
            output_dir=tmp_path / "cross-export",
            existing_manifests=[tmp_path / "prior/manifest.json"],
            labels="user_provided_unverified",
        )
    assert not (tmp_path / "cross-export").exists()

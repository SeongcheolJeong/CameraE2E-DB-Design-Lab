"""File-level handoff of one saved Lens × Sensor RAW to a RAW-aware dataset.

This adapter reads a completed, registry-verified job record and its two stored
artifacts. It never runs optics, sensor, ISP, annotation, or detector code. A
caller must supply every existing manifest in its intended cohort to check
duplicate RAW across that cohort; no global split registry is implied.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import numpy as np

from pyisetcam.dataset import _sha256_array, camerae2e_dataset_validate

from .registry import canonical, digest
from .retained_raw_dataset import (
    export_retained_raw_array,
    load_retained_lens_sensor_raw,
)

SCHEMA = "camerae2e_lens_sensor_raw_perception_handoff_v1"
BUNDLE_SCHEMA = "camerae2e_lens_sensor_retained_bundle_handoff_v1"
_RESULT_NAME = "lens-sensor-raw-preview-result.json"
_RAW_NAME = "lens-sensor-original-raw.npy"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(job: Mapping[str, Any], report_path: Path, raw_path: Path) -> tuple[dict, dict]:
    if (
        not isinstance(job, Mapping)
        or job.get("job_kind") != "lens_sensor_raw"
        or job.get("status") != "succeeded"
        or not isinstance(job.get("id"), str)
        or not job["id"].strip()
    ):
        raise ValueError("Select one succeeded Lens × Sensor RAW job.")
    result = job.get("result") or {}
    if not isinstance(result, Mapping):
        raise ValueError("Saved Lens × Sensor job has no completed result.")
    summary = result.get("summary")
    artifacts = job.get("artifacts")
    if (
        not isinstance(summary, dict)
        or not isinstance(summary.get("preview"), dict)
        or not isinstance(summary.get("source_identities"), dict)
        or not isinstance(job.get("source_identities"), dict)
        or not isinstance(artifacts, list)
        or len(artifacts) != 2
        or result.get("artifacts") != artifacts
    ):
        raise ValueError("Saved Lens × Sensor job lacks its two completed artifacts.")
    by_name = {item.get("name"): item for item in artifacts if isinstance(item, dict)}
    if set(by_name) != {_RESULT_NAME, _RAW_NAME}:
        raise ValueError("Saved Lens × Sensor artifact names are not the expected pair.")
    report_bytes = report_path.read_bytes()
    if (
        report_bytes != canonical(summary).encode()
        or hashlib.sha256(report_bytes).hexdigest() != by_name[_RESULT_NAME].get("sha256")
        or len(report_bytes) != by_name[_RESULT_NAME].get("bytes")
        or _sha256(raw_path) != by_name[_RAW_NAME].get("sha256")
        or raw_path.stat().st_size != by_name[_RAW_NAME].get("bytes")
        or summary["preview"].get("raw_artifact") != by_name[_RAW_NAME]
    ):
        raise ValueError("Saved Lens × Sensor artifact bytes differ from the job result.")
    if (
        summary.get("source_identities") != job.get("source_identities")
        or summary.get("specification") != job.get("specification")
        or summary.get("upstream_assets") != job.get("upstream_assets")
        or summary["preview"].get("lens_identity") != job["source_identities"].get("lens")
        or summary["preview"].get("sensor_revision") != job["source_identities"].get("sensor")
    ):
        raise ValueError("Saved Lens × Sensor result lost its frozen source identities.")
    report, raw, binding = load_retained_lens_sensor_raw(
        report_path, raw_path, source_lens_sensor_job_id=job["id"]
    )
    if report != summary:
        raise ValueError("Saved Lens × Sensor report differs from the job summary.")
    return {"raw": raw, "binding": binding}, summary


def _check_cohort(
    raw_content_sha256: str, existing_manifests: Sequence[str | Path]
) -> list[dict[str, str]]:
    if isinstance(existing_manifests, (str, Path)) or len(existing_manifests) > 256:
        raise ValueError("Pass a bounded sequence of existing cohort manifests.")
    checked = []
    for item in existing_manifests:
        path = Path(item).expanduser().resolve()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "camerae2e_dataset_manifest_v1":
            raise ValueError("Existing cohort manifest has an unsupported schema.")
        validation = camerae2e_dataset_validate(path)
        if not validation["ok"]:
            raise ValueError("Existing cohort manifest or its data failed validation.")
        for record in manifest["records"]:
            if record.get("raw_content_sha256") == raw_content_sha256:
                raise ValueError(
                    "This exact RAW already occurs in the declared cohort; "
                    "a source frame cannot cross or repeat splits."
                )
        checked.append({"manifest_sha256": _sha256(path), "path": str(path)})
    return checked


def export_lens_sensor_perception_handoff(
    job: Mapping[str, Any],
    *,
    source_report_path: str | Path,
    source_raw_path: str | Path,
    output_dir: str | Path,
    split: str,
    existing_manifests: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Copy one original RAW to the existing dataset and RAW perception index.

    `existing_manifests` is the caller's declared cohort, not a global registry.
    External annotations are intentionally not accepted by this version.
    """
    report_path = Path(source_report_path).expanduser().resolve()
    raw_path = Path(source_raw_path).expanduser().resolve()
    source, _ = _source(job, report_path, raw_path)
    binding = source["binding"]
    checked = _check_cohort(binding["source_raw_content_sha256"], existing_manifests)
    exported = export_retained_raw_array(
        source["raw"],
        source_binding=binding,
        seed=binding["source_frame_seed"],
        scene=binding["source_scene"],
        split=split,
        output_dir=output_dir,
    )
    if (
        exported["source_binding"] != binding
        or exported["raw_content_sha256"] != binding["source_raw_content_sha256"]
        or exported["labels"] != "unlabeled"
        or exported["perception_metric"]["available"] is not False
        or _sha256(report_path) != binding["source_report_sha256"]
        or _sha256(raw_path) != binding["source_raw_artifact_sha256"]
    ):
        raise ValueError("Lens × Sensor source or unlabeled dataset boundary changed.")
    label_path = Path(exported["files"]["labels"])
    label = json.loads(label_path.read_text(encoding="utf-8"))
    if label.get("labels") != {"objects": [], "masks": [], "source": "unlabeled"}:
        raise ValueError("Lens × Sensor dataset unexpectedly contains annotations.")
    handoff = {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": binding,
        "source_job_record_verification": (
            "caller_supplied_registry_verified_record_required; "
            "this adapter checks artifact bytes and frozen fields, not the registry audit"
        ),
        "dataset_manifest": "manifest.json",
        "dataset_manifest_sha256": _sha256(Path(exported["files"]["manifest"])),
        "raw_perception_index": "perception_index/raw_manifest.jsonl",
        "raw_perception_index_sha256": _sha256(Path(exported["files"]["raw_manifest"])),
        "raw_content_sha256": exported["raw_content_sha256"],
        "split": split,
        "split_isolation": {
            "scope": "declared_cohort_manifests_only",
            "checked_manifests": checked,
            "duplicate_source_raw_in_checked_cohort": False,
            "global_cross_export_exclusivity_enforced": False,
            "holdout_independence_claimed": False,
        },
        "annotation_provenance": {
            "status": "absent",
            "labels_sha256": _sha256(label_path),
            "ground_truth_available": False,
            "source_raw_content_sha256": exported["raw_content_sha256"],
        },
        "perception_metric": {
            "available": False,
            "reason": "No ground-truth annotation or detector model was supplied or run.",
        },
        "detector_evaluation_performed": False,
        "physical_validation": "not_established",
    }
    destination = Path(output_dir).expanduser().resolve()
    handoff_path = destination / "perception-handoff.json"
    handoff_path.write_text(canonical(handoff) + "\n", encoding="utf-8")
    return {
        **handoff,
        "path": str(handoff_path),
        "dataset_validation": exported["dataset_validation"],
    }


def _zip_member(archive: ZipFile, name: str, limit: int) -> bytes:
    member = archive.getinfo(name)
    if member.file_size > limit or member.file_size < 1:
        raise ValueError(f"Retained dataset member has an invalid size: {name}")
    data = archive.read(member)
    if len(data) != member.file_size:
        raise ValueError(f"Retained dataset member size changed: {name}")
    return data


def inspect_retained_lens_sensor_bundle(
    dataset_job: Mapping[str, Any],
    lens_job: Mapping[str, Any],
    *,
    bundle_path: str | Path,
) -> dict[str, Any]:
    """Validate a saved dataset ZIP and describe a portable RAW perception view.

    This consumes the existing retained dataset bundle without exporting RAW again.
    The caller supplies registry-verified jobs; this function checks their frozen
    identities and the artifact bytes, including unlabeled annotation evidence.
    """
    from .retained_raw_dataset_job import LENS_SCHEMA

    if (
        dataset_job.get("job_kind") != "retained_raw_dataset"
        or dataset_job.get("status") != "succeeded"
        or dataset_job.get("specification", {}).get("schema") != LENS_SCHEMA
        or lens_job.get("job_kind") != "lens_sensor_raw"
        or lens_job.get("status") != "succeeded"
    ):
        raise ValueError("Select succeeded Lens RAW and Lens retained dataset jobs.")
    summary = dataset_job.get("result", {}).get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Retained dataset result is missing.")
    binding = summary.get("source_binding")
    bundle = summary.get("bundle")
    if (
        summary.get("schema") != LENS_SCHEMA
        or summary.get("status") != "completed"
        or summary.get("source_kind") != "lens_sensor_raw"
        or summary.get("specification") != dataset_job.get("specification")
        or not isinstance(binding, dict)
        or binding.get("source_lens_sensor_job_id") != lens_job.get("id")
        or binding.get("source_raw_content_sha256") != summary.get("raw_content_sha256")
        or summary.get("source_capture_result_sha256") != digest(lens_job.get("result"))
        or not isinstance(bundle, dict)
        or bundle not in dataset_job.get("artifacts", [])
        or summary.get("raw_values_byte_equal") is not True
        or summary.get("raw_dtype") != "int32"
        or summary.get("labels") != "unlabeled"
        or summary.get("perception_metric", {}).get("available") is not False
        or summary.get("raw_training_index", {}).get("row_count") != 1
    ):
        raise ValueError("Retained dataset lost its Lens RAW or unlabeled source binding.")
    lens_summary = lens_job["result"]["summary"]
    if (
        binding.get("lens_identity") != lens_summary["source_identities"]["lens"]
        or binding.get("saved_sensor_revision") != lens_summary["source_identities"]["sensor"]
        or binding.get("source_report_sha256")
        != next(item["sha256"] for item in lens_job["artifacts"] if item["name"] == _RESULT_NAME)
        or binding.get("source_raw_artifact_sha256")
        != lens_summary["preview"]["raw_artifact"]["sha256"]
    ):
        raise ValueError("Retained dataset source identities differ from the original RAW job.")
    path = Path(bundle_path).expanduser().resolve()
    if (
        path.stat().st_size != bundle.get("bytes")
        or path.stat().st_size > 512 * 1024
        or _sha256(path) != bundle.get("sha256")
    ):
        raise ValueError("Retained dataset bundle differs from its saved artifact.")
    with ZipFile(path) as archive:
        expected = {
            "README.txt",
            "labels/retained_0000.json",
            "manifest.json",
            "metadata.jsonl",
            "raw/retained_0000.npz",
        }
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError("Retained dataset bundle members are incomplete or duplicated.")
        manifest_bytes = _zip_member(archive, "manifest.json", 128 * 1024)
        manifest = json.loads(manifest_bytes)
        metadata = _zip_member(archive, "metadata.jsonl", 128 * 1024)
        label_bytes = _zip_member(archive, "labels/retained_0000.json", 128 * 1024)
        raw_bytes = _zip_member(archive, "raw/retained_0000.npz", 64 * 1024)
    records = manifest.get("records")
    if (
        manifest.get("schema_version") != "camerae2e_dataset_manifest_v1"
        or manifest.get("dataset_root") != "."
        or manifest.get("case_count") != 1
        or manifest.get("source_binding") != binding
        or not isinstance(records, list)
        or len(records) != 1
        or manifest.get("splits") != {dataset_job["specification"]["split"]: 1}
        or manifest.get("split_policy", {}).get("cross_export_exclusivity_enforced") is not False
        or manifest.get("split_policy", {}).get("holdout_independence_claimed") is not False
    ):
        raise ValueError("Retained dataset manifest changed its source or split contract.")
    record = records[0]
    labels = json.loads(label_bytes)
    if (
        json.loads(metadata) != record
        or record.get("case_id") != "retained_0000"
        or record.get("raw") != "raw/retained_0000.npz"
        or record.get("labels") != "labels/retained_0000.json"
        or record.get("split") != dataset_job["specification"]["split"]
        or record.get("source_binding") != binding
        or record.get("raw_content_sha256") != summary["raw_content_sha256"]
        or record.get("raw_sha256") != f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
        or record.get("labels_sha256") != f"sha256:{hashlib.sha256(label_bytes).hexdigest()}"
        or record.get("label_summary")
        != {
            "source": "unlabeled",
            "object_count": 0,
            "mask_count": 0,
        }
        or labels.get("schema_version") != "camerae2e_dataset_labels_v1"
        or labels.get("case_id") != "retained_0000"
        or labels.get("labels") != {"objects": [], "masks": [], "source": "unlabeled"}
    ):
        raise ValueError("Retained dataset contains altered RAW or annotation metadata.")
    with ZipFile(io.BytesIO(raw_bytes)) as npz:
        sizes = {member.filename: member.file_size for member in npz.infolist()}
        if (
            set(sizes) != {"raw.npy", "sensor_digital.npy", "seed.npy"}
            or len(npz.infolist()) != 3
            or any(size > 16 * 1024 for size in sizes.values())
        ):
            raise ValueError("Retained RAW NPZ contents are outside the bounded contract.")
    with np.load(io.BytesIO(raw_bytes), allow_pickle=False) as arrays:
        raw = arrays["raw"]
        digital = arrays["sensor_digital"]
        seed = arrays["seed"]
    if (
        raw.dtype != np.dtype("int32")
        or list(raw.shape) != summary["raw_shape"]
        or list(raw.shape) != record.get("raw_shape")
        or _sha256_array(raw) != summary["raw_content_sha256"]
        or digital.dtype != np.dtype("int32")
        or digital.size != 0
        or seed.tolist() != [binding["source_frame_seed"]]
        or record.get("seed") != binding["source_frame_seed"]
    ):
        raise ValueError("Retained dataset RAW values, shape, or seed differ from source.")
    index = {
        "schema": "camerae2e_lens_sensor_portable_raw_index_v1",
        "case_id": "retained_0000",
        "split": record["split"],
        "bundle_sha256": bundle["sha256"],
        "raw": record["raw"],
        "raw_sha256": record["raw_sha256"],
        "raw_content_sha256": summary["raw_content_sha256"],
        "raw_shape": summary["raw_shape"],
        "raw_dtype": "int32",
        "labels": record["labels"],
        "labels_sha256": record["labels_sha256"],
        "annotation_source": "unlabeled",
        "ground_truth_available": False,
        "objects": [],
    }
    return {
        "schema": BUNDLE_SCHEMA,
        "source_binding": binding,
        "dataset_bundle": bundle,
        "dataset_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "raw_content_sha256": summary["raw_content_sha256"],
        "raw_shape": summary["raw_shape"],
        "split": record["split"],
        "portable_raw_index": index,
        "annotation_provenance": {
            "status": "absent",
            "labels_sha256": hashlib.sha256(label_bytes).hexdigest(),
            "source_raw_content_sha256": summary["raw_content_sha256"],
            "ground_truth_available": False,
        },
        "perception_metric": {
            "available": False,
            "reason": "No independent ground-truth annotation or detector evaluation was supplied.",
        },
        "detector_evaluation_performed": False,
        "physical_validation": "not_established",
    }

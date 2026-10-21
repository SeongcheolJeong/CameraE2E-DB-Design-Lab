"""Durable export of multiple saved Lens x Sensor RAW jobs as one cohort ZIP."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import numpy as np

from pyisetcam.dataset import _sha256_array

from .lens_sensor_perception import _source
from .lens_sensor_raw_cohort import export_lens_sensor_raw_cohort
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_raw_cohort_study_v1"
ANNOTATED_SCHEMA = "camerae2e_lens_sensor_raw_cohort_study_v2"
JOB_KIND = "lens_sensor_raw_cohort"
LIMITATION = (
    "Exports exact original int32 RAW artifacts from multiple completed Lens x "
    "Sensor jobs into one unlabeled portable cohort ZIP. Content-identical RAW "
    "is stored once within one split and rejected across splits. There is no "
    "new capture, ISP, annotation, detector, perception metric, calibrated Lens "
    "radiometry, global split registry or physical validation."
)
ANNOTATED_LIMITATION = (
    "Exports exact saved Lens x Sensor int32 RAW and hash-pinned, user-supplied "
    "RAW-pixel boxes as one portable cohort. Annotation truth, completeness, "
    "origin and independence are unverified. The same synthetic scene family "
    "cannot cross splits in this export, but a held-out dataset is not thereby "
    "established. No capture, ISP, detector, perception metric, calibrated lens "
    "radiometry, global split registry or physical validation is provided."
)
ANNOTATED_RADIANCE_LIMITATION = (
    "Exports exact saved Lens x Sensor int32 RAW and hash-pinned, user-supplied "
    "RAW-pixel boxes as one portable cohort. Annotation truth, completeness, "
    "origin and independence are unverified. The same original radiance NPY "
    "identity cannot cross splits in this export, but a held-out dataset is "
    "not thereby established. No capture, ISP, detector, perception metric, "
    "calibrated lens radiometry, global split registry or physical validation "
    "is provided."
)
_SPLITS = frozenset({"train", "validation", "test"})
_RESULT_NAME = "lens-sensor-raw-preview-result.json"
_RAW_NAME = "lens-sensor-original-raw.npy"
_BUNDLE_NAME = "lens-sensor-raw-cohort.zip"
_MANIFEST_NAME = "lens-sensor-raw-cohort-manifest.json"
_SUMMARY_NAME = "lens-sensor-raw-cohort-result.json"


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 보존 RAW cohort",
        "source_jobs": [],
        "labels": "unlabeled",
        "acknowledge_simulation_limits": False,
    }


def default_annotated_spec() -> dict[str, Any]:
    return {
        **default_spec(),
        "schema": ANNOTATED_SCHEMA,
        "name": "렌즈 × 센서 사용자 라벨 RAW cohort",
        "labels": "user_provided_unverified",
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    annotated = isinstance(payload, dict) and payload.get("schema") == ANNOTATED_SCHEMA
    if not isinstance(payload, dict) or set(payload) != set(
        default_annotated_spec() if annotated else default_spec()
    ):
        raise ValueError("Declare exactly the Lens x Sensor RAW cohort fields.")
    if payload["schema"] not in {SCHEMA, ANNOTATED_SCHEMA} or payload[
        "acknowledge_simulation_limits"
    ] is not True:
        raise ValueError("Explicit cohort simulation-limit acknowledgement required.")
    expected_labels = "user_provided_unverified" if annotated else "unlabeled"
    if payload["labels"] != expected_labels:
        raise ValueError(f"Cohort export requires explicit {expected_labels} status.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Cohort name requires 1..160 characters.")
    sources = payload["source_jobs"]
    if not isinstance(sources, list) or not 2 <= len(sources) <= 64:
        raise ValueError("Choose 2..64 completed Lens x Sensor RAW jobs.")
    selected = []
    seen = set()
    for source in sources:
        required = {"source_job_id", "split"}
        if annotated:
            required.add("annotation_artifact_sha256")
        if not isinstance(source, dict) or set(source) != required:
            raise ValueError(
                "Each cohort source needs source_job_id, split and an annotation "
                "SHA-256 in annotated mode."
            )
        identity = source["source_job_id"]
        split = source["split"]
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity.strip()) <= 128
            or identity.strip() in seen
        ):
            raise ValueError("Each cohort source_job_id must be distinct and nonempty.")
        if not isinstance(split, str) or split not in _SPLITS:
            raise ValueError("split must be train, validation or test.")
        identity = identity.strip()
        seen.add(identity)
        entry = {"source_job_id": identity, "split": split}
        if annotated:
            annotation_sha = source["annotation_artifact_sha256"]
            if (
                not isinstance(annotation_sha, str)
                or len(annotation_sha) != 64
                or any(character not in "0123456789abcdef" for character in annotation_sha)
            ):
                raise ValueError(
                    "annotation_artifact_sha256 needs a lowercase SHA-256 artifact ID."
                )
            entry["annotation_artifact_sha256"] = annotation_sha
        selected.append(entry)
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_jobs"] = selected
    return result


def _source_entry(
    service: Any, selected: dict[str, str], *, annotated: bool
) -> tuple[dict, dict]:
    from .lens_sensor_raw_job import get as lens_get

    source_job_id, split = selected["source_job_id"], selected["split"]
    job = lens_get(service, source_job_id)
    if job["status"] != "succeeded":
        raise ValueError("Every cohort Lens x Sensor RAW source must have succeeded.")
    artifacts = {item["name"]: item for item in job["artifacts"]}
    if len(job["artifacts"]) != 2 or set(artifacts) != {_RESULT_NAME, _RAW_NAME}:
        raise IntegrityError("Completed Lens RAW source lacks its exact artifact pair.")
    report_path = service.registry.blob_path(artifacts[_RESULT_NAME]["sha256"])
    raw_path = service.registry.blob_path(artifacts[_RAW_NAME]["sha256"])
    verified, _ = _source(job, report_path, raw_path)
    binding = verified["binding"]
    entry = {
        "source_job_id": source_job_id,
        "split": split,
        "source_capture_result_sha256": digest(job["result"]),
        "source_artifacts": {
            "result": artifacts[_RESULT_NAME],
            "raw": artifacts[_RAW_NAME],
        },
        "source_binding": binding,
    }
    source_input = {
        "job": job,
        "source_report_path": report_path,
        "source_raw_path": raw_path,
        "split": split,
    }
    if annotated:
        from .retained_raw_dataset_job import _annotation_source

        document, annotation_binding = _annotation_source(
            service,
            {"annotation_artifact_sha256": selected["annotation_artifact_sha256"]},
            {"source_binding": binding, "source_raw_shape": list(verified["raw"].shape)},
        )
        entry["annotation_binding"] = annotation_binding
        source_input["annotation_document"] = document
        source_input["annotation_artifact_sha256"] = selected[
            "annotation_artifact_sha256"
        ]
    return entry, source_input


def _sources(service: Any, spec: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    entries, inputs = [], []
    raw_splits: dict[str, str] = {}
    scene_splits: dict[str, str] = {}
    annotated = spec["schema"] == ANNOTATED_SCHEMA
    for selected in spec["source_jobs"]:
        entry, source_input = _source_entry(service, selected, annotated=annotated)
        raw_hash = entry["source_binding"]["source_raw_content_sha256"]
        duplicate_raw = raw_hash in raw_splits
        previous_split = raw_splits.setdefault(raw_hash, entry["split"])
        if previous_split != entry["split"]:
            raise ValueError("The exact RAW cannot be reused across cohort splits.")
        if annotated:
            if duplicate_raw:
                raise ValueError("Annotated cohort requires distinct RAW per source.")
            scene = entry["source_binding"]["source_scene"]
            previous_scene_split = scene_splits.setdefault(scene, entry["split"])
            if previous_scene_split != entry["split"]:
                raise ValueError(
                    "The same source radiance NPY identity cannot cross annotated cohort splits."
                    if scene.startswith("object_radiance_sha256:") else
                    "The same synthetic scene family cannot cross annotated cohort splits."
                )
        entries.append(entry)
        inputs.append(source_input)
    return entries, inputs


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    entries, _ = _sources(service, spec)
    includes_radiance = any(
        entry["source_binding"]["source_scene"].startswith("object_radiance_sha256:")
        for entry in entries
    )
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "source_entries": entries,
        "environment": _current(service),
        "limitation": (
            ANNOTATED_RADIANCE_LIMITATION if includes_radiance else ANNOTATED_LIMITATION
        ) if spec["schema"] == ANNOTATED_SCHEMA else LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens RAW cohort export is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    for field, value in fresh.items():
        if canonical(value) != canonical(job.get(field)):
            raise IntegrityError(
                f"Frozen Lens RAW cohort sources or executable changed: {field}."
            )
    spec = fresh["specification"]
    _, inputs = _sources(service, spec)
    with tempfile.TemporaryDirectory(prefix="camera-lens-raw-cohort-") as temporary:
        directory = Path(temporary) / "dataset"
        exported = export_lens_sensor_raw_cohort(
            inputs, output_dir=directory, labels=spec["labels"]
        )
        bundle_bytes = Path(exported["files"]["zip"]).read_bytes()
        with ZipFile(io.BytesIO(bundle_bytes)) as archive:
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
        _check_manifest_sources(
            manifest, fresh["source_entries"], labels=spec["labels"]
        )
        if (
            hashlib.sha256(bundle_bytes).hexdigest() != exported["integrity"]["zip_sha256"]
            or exported["perception_metric"]["available"] is not False
            or exported["dataset_validation"]["ok"] is not True
            or canonical(prepare(service, spec)) != canonical(fresh)
        ):
            raise IntegrityError("Cohort bundle, source binding or validation changed.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Lens RAW cohort export was cancelled during execution.")
        bundle_descriptor = service.registry.store_blob(
            bundle_bytes, _BUNDLE_NAME, "application/zip"
        )
        manifest_descriptor = service.registry.store_blob(
            manifest_bytes, _MANIFEST_NAME, "application/json"
        )
    summary = {
        "schema": spec["schema"],
        "status": "completed",
        "specification": spec,
        "source_entries": fresh["source_entries"],
        "source_job_count": exported["source_job_count"],
        "case_count": exported["case_count"],
        "split_policy": exported["split_policy"],
        "raw_content_sha256": exported["integrity"]["distinct_raw_content_sha256"],
        "dataset_validation": {
            "ok": exported["dataset_validation"]["ok"],
            "issue_count": exported["dataset_validation"]["issue_count"],
            "warning_count": exported["dataset_validation"]["warning_count"],
        },
        "raw_training_index": exported["raw_training_index"],
        "labels": spec["labels"],
        "class_map": manifest["class_map"],
        "annotation_provenance": exported["annotation_provenance"],
        "perception_metric": exported["perception_metric"],
        "bundle": bundle_descriptor,
        "manifest": manifest_descriptor,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": fresh["limitation"],
    }
    result_descriptor = service.registry.store_blob(
        canonical(summary).encode(), _SUMMARY_NAME, "application/json"
    )
    return {
        "summary": summary,
        "artifacts": [bundle_descriptor, manifest_descriptor, result_descriptor],
    }


def _check_manifest_sources(
    manifest: dict, source_entries: list[dict], *, labels: str = "unlabeled"
) -> None:
    if (
        manifest.get("schema_version") != "camerae2e_dataset_manifest_v1"
        or manifest.get("dataset_root") != "."
        or manifest.get("source_job_count") != len(source_entries)
        or manifest.get("case_count") != len(manifest.get("records", []))
        or manifest.get("split_policy", {}).get("identical_raw_across_splits_rejected") is not True
        or manifest.get("split_policy", {}).get("holdout_independence_claimed") is not False
        or manifest.get("labels", "unlabeled") != labels
    ):
        raise IntegrityError("Portable cohort manifest lost its dataset or split contract.")
    expected = {
        entry["source_job_id"]: (entry["split"], entry["source_binding"])
        for entry in source_entries
    }
    annotation_by_job = {
        entry["source_job_id"]: entry["annotation_binding"]["annotation_artifact_sha256"]
        for entry in source_entries if "annotation_binding" in entry
    }
    found = {}
    raw_splits = {}
    case_ids = set()
    for record in manifest["records"]:
        raw_hash = record["raw_content_sha256"]
        split = record["split"]
        if raw_hash in raw_splits or split not in _SPLITS:
            raise IntegrityError("Portable cohort has duplicate RAW records or invalid splits.")
        raw_splits[raw_hash] = split
        case_id = record["case_id"]
        if (
            case_id in case_ids
            or record["raw"] != f"raw/{case_id}.npz"
            or record["labels"] != f"labels/{case_id}.json"
        ):
            raise IntegrityError("Portable cohort case paths or IDs are not unique.")
        case_ids.add(case_id)
        if labels == "unlabeled":
            if record["labels"] is None or record["label_summary"] != {
                "source": "unlabeled", "object_count": 0, "mask_count": 0
            }:
                raise IntegrityError("Portable cohort unexpectedly gained labels.")
        else:
            if (
                record["label_summary"].get("source") != "user_provided_unverified"
                or record.get("source_annotation")
                != f"annotations/{case_id}-source.json"
                or len(record["source_bindings"]) != 1
            ):
                raise IntegrityError("Annotated cohort lost its source annotation path.")
        for binding in record["source_bindings"]:
            identity = binding["source_lens_sensor_job_id"]
            if (
                identity in found
                or identity not in expected
                or expected[identity] != (split, binding)
            ):
                raise IntegrityError("Portable cohort source binding differs from frozen job.")
            if binding["source_raw_content_sha256"] != raw_hash:
                raise IntegrityError("Portable cohort RAW hash differs from source binding.")
            if labels == "user_provided_unverified" and record.get(
                "source_annotation_sha256"
            ) != f"sha256:{annotation_by_job.get(identity)}":
                raise IntegrityError("Annotated cohort source SHA differs from frozen job.")
            found[identity] = binding
    if set(found) != set(expected):
        raise IntegrityError("Portable cohort omitted a frozen Lens RAW source.")
    split_counts = {
        split: sum(value == split for value in raw_splits.values())
        for split in set(raw_splits.values())
    }
    if manifest.get("splits") != split_counts:
        raise IntegrityError("Portable cohort split counts differ from its RAW records.")
    if labels == "user_provided_unverified":
        scene_splits = {}
        includes_radiance = any(
            entry["source_binding"]["source_scene"].startswith("object_radiance_sha256:")
            for entry in source_entries
        )
        for record in manifest["records"]:
            scene = record["source_binding"]["source_scene"]
            if scene_splits.setdefault(scene, record["split"]) != record["split"]:
                raise IntegrityError("Annotated cohort source scene identity crosses splits.")
        if (
            manifest.get("split_policy", {}).get(
                "source_scene_identity_across_splits_rejected" if includes_radiance else
                "synthetic_scene_family_across_splits_rejected"
            ) is not True
            or manifest.get("split_policy", {}).get("holdout_independence_claimed") is not False
            or len(annotation_by_job) != len(source_entries)
        ):
            raise IntegrityError("Annotated cohort split policy changed.")


def _check_annotated_zip_case(
    archive: ZipFile, record: dict, labels: dict, class_map: dict[str, int]
) -> set[str]:
    from .retained_raw_dataset import validate_raw_annotation

    if (
        not isinstance(class_map, dict)
        or class_map != {
            label: index for index, label in enumerate(sorted(class_map))
        }
    ):
        raise IntegrityError("Annotated cohort class map is not canonical.")
    annotation_bytes = archive.read(record["source_annotation"])
    annotation_sha = hashlib.sha256(annotation_bytes).hexdigest()
    if (
        record["source_annotation_sha256"] != f"sha256:{annotation_sha}"
        or labels.get("schema_version") != "camerae2e_dataset_labels_v1"
    ):
        raise IntegrityError("Annotated cohort source bytes or label schema differ.")
    try:
        annotation = json.loads(annotation_bytes)
        normalized = validate_raw_annotation(
            annotation,
            source_binding=record["source_binding"],
            raw_shape=record["raw_shape"],
        )
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("Annotated cohort source annotation is invalid.") from exc
    if canonical(normalized).encode() != annotation_bytes:
        raise IntegrityError("Annotated cohort source annotation lost canonical bytes.")
    try:
        objects = [
            {**item, "class_id": class_map[item["label"]]}
            for item in annotation["objects"]
        ]
    except KeyError as exc:
        raise IntegrityError("Annotated cohort class map omits a source label.") from exc
    expected_labels = {
        "objects": objects,
        "masks": [],
        "source": "user_provided_unverified",
        "image_size_rc": record["raw_shape"],
        "coordinate_frame": annotation["coordinate_frame"],
        "provenance": annotation["provenance"],
        "source_annotation_sha256": annotation_sha,
    }
    if (
        labels.get("labels") != expected_labels
        or record["label_summary"] != {
            "source": "user_provided_unverified",
            "object_count": len(objects),
            "mask_count": 0,
        }
    ):
        raise IntegrityError("Annotated cohort labels differ from source annotation.")
    return {item["label"] for item in annotation["objects"]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens RAW cohort export.")
    summary = (job.get("result") or {}).get("summary")
    if summary is None:
        return job
    if (
        canonical(summary["specification"]) != canonical(job["specification"])
        or canonical(summary["source_entries"]) != canonical(job["source_entries"])
        or summary.get("schema") != job["specification"]["schema"]
        or summary.get("labels") != job["specification"]["labels"]
    ):
        raise IntegrityError("Cohort result differs from frozen request or source entries.")
    if job["status"] == "succeeded":
        current_sources, _ = _sources(service, job["specification"])
        if canonical(current_sources) != canonical(job["source_entries"]):
            raise IntegrityError("Cohort source Registry jobs changed after export.")
        artifacts = {item["name"]: item for item in job["artifacts"]}
        if (
            len(job["artifacts"]) != 3
            or set(artifacts) != {_BUNDLE_NAME, _MANIFEST_NAME, _SUMMARY_NAME}
            or summary["bundle"] != artifacts[_BUNDLE_NAME]
            or summary["manifest"] != artifacts[_MANIFEST_NAME]
        ):
            raise IntegrityError("Cohort artifact set differs from completed result.")
        if service.registry.blob_path(artifacts[_SUMMARY_NAME]["sha256"]).read_bytes() != canonical(
            summary
        ).encode():
            raise IntegrityError("Cohort result JSON artifact differs from summary.")
        manifest_bytes = service.registry.blob_path(
            artifacts[_MANIFEST_NAME]["sha256"]
        ).read_bytes()
        bundle_path = service.registry.blob_path(artifacts[_BUNDLE_NAME]["sha256"])
        with ZipFile(bundle_path) as archive:
            if archive.read("manifest.json") != manifest_bytes:
                raise IntegrityError("Cohort ZIP manifest differs from separate manifest artifact.")
            manifest = json.loads(manifest_bytes)
            _check_manifest_sources(
                manifest, job["source_entries"], labels=summary["labels"]
            )
            if (
                manifest["case_count"] != summary["case_count"]
                or [record["raw_content_sha256"] for record in manifest["records"]]
                != summary["raw_content_sha256"]
                or manifest.get("class_map", {}) != summary.get("class_map", {})
                or manifest.get("split_policy") != summary.get("split_policy")
                or (
                    "annotation_provenance" in summary
                    and manifest.get("annotation_provenance")
                    != summary["annotation_provenance"]
                )
            ):
                raise IntegrityError("Cohort result case count or RAW hashes changed.")
            expected_members = {"README.txt", "manifest.json", "metadata.jsonl"}
            expected_members.update(record["raw"] for record in manifest["records"])
            expected_members.update(record["labels"] for record in manifest["records"])
            expected_members.update(
                record["source_annotation"] for record in manifest["records"]
                if record.get("source_annotation") is not None
            )
            if len(archive.namelist()) != len(expected_members) or set(archive.namelist()) != (
                expected_members
            ):
                raise IntegrityError("Cohort ZIP member set differs from manifest records.")
            metadata_rows = [
                json.loads(line)
                for line in archive.read("metadata.jsonl").decode().splitlines()
                if line.strip()
            ]
            if metadata_rows != manifest["records"]:
                raise IntegrityError("Cohort ZIP metadata differs from manifest records.")
            observed_labels: set[str] = set()
            for record in manifest["records"]:
                raw_bytes = archive.read(record["raw"])
                if f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}" != record["raw_sha256"]:
                    raise IntegrityError("Cohort RAW NPZ bytes differ from manifest hash.")
                with np.load(io.BytesIO(raw_bytes), allow_pickle=False) as arrays:
                    if (
                        arrays["raw"].dtype != np.dtype("int32")
                        or _sha256_array(arrays["raw"]) != record["raw_content_sha256"]
                        or arrays["sensor_digital"].size != 0
                    ):
                        raise IntegrityError("Cohort ZIP RAW content differs from source hash.")
                label_bytes = archive.read(record["labels"])
                labels = json.loads(label_bytes)
                if (
                    f"sha256:{hashlib.sha256(label_bytes).hexdigest()}"
                    != record["labels_sha256"]
                    or labels.get("case_id") != record["case_id"]
                ):
                    raise IntegrityError("Cohort ZIP labels differ from manifest.")
                if summary["labels"] == "unlabeled":
                    if labels.get("labels") != {
                        "objects": [], "masks": [], "source": "unlabeled"
                    }:
                        raise IntegrityError("Cohort ZIP unexpectedly contains annotations.")
                else:
                    observed_labels.update(_check_annotated_zip_case(
                        archive, record, labels, manifest["class_map"]
                    ))
            if summary["labels"] == "user_provided_unverified" and (
                observed_labels != set(manifest["class_map"])
            ):
                raise IntegrityError("Annotated cohort class map has unused labels.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-raw-cohorts/template", tags=["lens_sensor_raw_cohort"])
    def template():
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
        ]
        return {
            "specification": default_spec(),
            "annotated_specification": default_annotated_spec(),
            "source_jobs": sources,
            "annotation_template_endpoint": (
                "/api/v1/retained-raw-datasets/template"
                "?source_kind=lens_sensor_raw_labeled&source_job_id={id}"
            ),
            "annotation_import_endpoint": "/api/v1/retained-raw-datasets/annotations",
            "limitation": LIMITATION,
            "annotated_limitation": ANNOTATED_LIMITATION,
        }

    @app.post("/api/v1/lens-sensor-raw-cohorts", status_code=202, tags=["lens_sensor_raw_cohort"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-raw-cohorts", tags=["lens_sensor_raw_cohort"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-raw-cohorts/{identity}", tags=["lens_sensor_raw_cohort"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-raw-cohorts/{identity}/cancel", tags=["lens_sensor_raw_cohort"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-raw-cohorts/{identity}/export", tags=["lens_sensor_raw_cohort"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens RAW cohort ZIP is not ready.")
        content = service.registry.blob_path(
            job["result"]["summary"]["bundle"]["sha256"]
        ).read_bytes()
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-raw-cohort.zip"'},
        )

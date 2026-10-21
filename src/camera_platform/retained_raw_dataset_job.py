"""Durable job that exports an existing quality capture as an unlabeled RAW dataset."""

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
from fastapi import HTTPException, Request

from pyisetcam.dataset import _sha256_array

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_retained_raw_dataset_study_v1"
LENS_SCHEMA = "camerae2e_retained_raw_dataset_study_v2"
LENS_LABELED_SCHEMA = "camerae2e_retained_raw_dataset_study_v3"
JOB_KIND = "retained_raw_dataset"
_MAX_ANNOTATION_BODY_BYTES = 512 * 1024
_MAX_ANNOTATION_JSON_BYTES = 256 * 1024
LIMITATION = (
    "Copies one previously captured int32 RAW frame from a hash-pinned saved "
    "sensor-quality job into the existing CameraE2E NPZ dataset format. The "
    "original RAW values and seed are retained; no capture, ISP, labeling, "
    "model inference, or perception scoring is run. The dataset is explicitly "
    "unlabeled and is not a standards-compliant DNG or physical validation. "
    "The split is a per-export annotation with no cross-export holdout guarantee."
)
LENS_LIMITATION = (
    "Copies one exact integer RAW artifact from a completed approximate Lens DB × "
    "saved Sensor DB capture into the existing CameraE2E dataset format. This "
    "retains the original Lens and Sensor revisions, RAW values and seed; it does "
    "not recompute capture, infer labels or score perception. Lens transmission "
    "and relative illumination were assumed unity in the source, so this is not "
    "a calibrated camera dataset. Split is a per-export annotation only."
)
LENS_LABELED_LIMITATION = (
    "Copies one exact approximate Lens DB x saved Sensor DB integer RAW frame and "
    "user-supplied, hash-pinned boxes expressed in the source RAW mosaic pixel grid. "
    "The annotation provider, completeness and independence are declarations, not "
    "verified truth. No ISP RGB, detector, perception metric, calibrated radiometry, "
    "physical validation or cross-export holdout independence is established."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "보존 RAW 데이터셋 내보내기",
        "source_quality_job_id": "",
        "sensor_report_index": 0,
        "case_index": 0,
        "split": "train",
        "labels": "unlabeled",
        "acknowledge_simulation_limits": False,
    }


def default_lens_spec() -> dict[str, Any]:
    return {
        "schema": LENS_SCHEMA,
        "name": "렌즈 × 센서 보존 RAW 데이터셋",
        "source_kind": "lens_sensor_raw",
        "source_job_id": "",
        "split": "train",
        "labels": "unlabeled",
        "acknowledge_simulation_limits": False,
    }


def default_lens_labeled_spec() -> dict[str, Any]:
    return {
        **default_lens_spec(),
        "schema": LENS_LABELED_SCHEMA,
        "name": "렌즈 × 센서 사용자 라벨 RAW 데이터셋",
        "labels": "user_provided_unverified",
        "annotation_artifact_sha256": "",
    }


def _artifact_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("annotation_artifact_sha256 requires a lowercase SHA-256 artifact ID.")
    return value


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict) and payload.get("schema") in {LENS_SCHEMA, LENS_LABELED_SCHEMA}:
        labeled = payload["schema"] == LENS_LABELED_SCHEMA
        if set(payload) != set(default_lens_labeled_spec() if labeled else default_lens_spec()):
            raise ValueError("Declare exactly the Lens × Sensor retained dataset fields.")
        if payload["acknowledge_simulation_limits"] is not True:
            raise ValueError("Explicit retained-RAW simulation-limit acknowledgement required.")
        if payload["source_kind"] != "lens_sensor_raw":
            raise ValueError("The Lens source_kind must be lens_sensor_raw.")
        if (
            not isinstance(payload["source_job_id"], str)
            or not 1 <= len(payload["source_job_id"].strip()) <= 128
        ):
            raise ValueError("Select one completed Lens × Sensor source_job_id.")
        if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
            raise ValueError("Dataset name requires 1..160 characters.")
        if not isinstance(payload["split"], str) or payload["split"] not in {
            "train", "validation", "test"
        }:
            raise ValueError("split must be train, validation, or test.")
        if payload["labels"] != ("user_provided_unverified" if labeled else "unlabeled"):
            raise ValueError(
                "This bounded export requires explicit user_provided_unverified status."
                if labeled else "This bounded export requires explicit unlabeled status."
            )
        if labeled:
            _artifact_id(payload["annotation_artifact_sha256"])
        result = deepcopy(payload)
        result["name"] = result["name"].strip()
        result["source_job_id"] = result["source_job_id"].strip()
        return result
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the retained RAW dataset fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit retained-RAW simulation-limit acknowledgement required.")
    name = payload["name"]
    identity = payload["source_quality_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Dataset name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_quality_job_id.")
    for key in ("sensor_report_index", "case_index"):
        if type(payload[key]) is not int or not 0 <= payload[key] <= 2:
            raise ValueError(f"{key} must be an integer index from 0 to 2.")
    if payload["split"] not in {"train", "validation", "test"}:
        raise ValueError("split must be train, validation, or test.")
    if payload["labels"] != "unlabeled":
        raise ValueError("This bounded export requires explicit unlabeled status.")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_quality_job_id"] = identity.strip()
    return result


def _source(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_quality_job import get as quality_get

    quality = quality_get(service, spec["source_quality_job_id"])
    if quality["status"] != "succeeded":
        raise ValueError("Selected saved sensor quality job has not succeeded.")
    source_result = quality["result"]
    reports = source_result["summary"]["sensor_reports"]
    if spec["sensor_report_index"] >= len(reports):
        raise ValueError("Selected source sensor_report_index does not exist.")
    report = reports[spec["sensor_report_index"]]
    if spec["case_index"] >= len(report["cases"]):
        raise ValueError("Selected source case_index does not exist.")
    artifacts = report["artifacts"]
    json_path = service.registry.blob_path(artifacts["json"]["sha256"])
    npz_path = service.registry.blob_path(artifacts["npz"]["sha256"])
    original = json.loads(json_path.read_text(encoding="utf-8"))
    without_artifacts = {key: value for key, value in original.items() if key != "artifacts"}
    expected = {key: value for key, value in report.items() if key != "artifacts"}
    if canonical(without_artifacts) != canonical(expected):
        raise IntegrityError("Source quality JSON differs from its completed job summary.")
    if original["artifacts"]["npz_sha256"] != artifacts["npz"]["sha256"]:
        raise IntegrityError("Source quality NPZ hash differs from its report.")
    return {
        "source_quality_result_sha256": digest(source_result),
        "source_revision": report["revision"],
        "source_artifacts": artifacts,
        "source_case": {
            "scene": report["cases"][spec["case_index"]]["scene"],
            "array_prefix": report["cases"][spec["case_index"]]["array_prefix"],
            "frame_seed": report["cases"][spec["case_index"]]["frame_seeds"][0],
        },
        "source_upstream_assets": source_result["summary"]["upstream_assets"],
        "source_npz_bytes": npz_path.stat().st_size,
    }


def _lens_source(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_raw_job import get as lens_get
    from .retained_raw_dataset import load_retained_lens_sensor_raw

    source = lens_get(service, spec["source_job_id"])
    if source["status"] != "succeeded":
        raise ValueError("Selected Lens × Sensor RAW job has not succeeded.")
    source_result = source["result"]
    summary = source_result["summary"]
    result_descriptor = next(
        (
            item for item in source["artifacts"]
            if item["name"] == "lens-sensor-raw-preview-result.json"
        ),
        None,
    )
    if result_descriptor is None:
        raise IntegrityError("Lens × Sensor result artifact is missing.")
    raw_descriptor = summary["preview"]["raw_artifact"]
    result_path = service.registry.blob_path(result_descriptor["sha256"])
    raw_path = service.registry.blob_path(raw_descriptor["sha256"])
    _, raw, binding = load_retained_lens_sensor_raw(
        result_path, raw_path, source_lens_sensor_job_id=spec["source_job_id"]
    )
    if (
        binding["lens_identity"] != summary["source_identities"]["lens"]
        or binding["saved_sensor_revision"] != summary["source_identities"]["sensor"]
        or list(raw.shape) != summary["preview"]["capture"]["sensor_size"]
    ):
        raise IntegrityError("Lens × Sensor RAW source binding differs from the completed job.")
    return {
        "source_capture_result_sha256": digest(source_result),
        "source_artifacts": {"json": result_descriptor, "raw": raw_descriptor},
        "source_binding": binding,
        "source_raw_shape": list(raw.shape),
        "source_npz_bytes": raw_path.stat().st_size,
    }


def _annotation_source(
    service: Any, spec: dict[str, Any], lens_source: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .retained_raw_dataset import validate_raw_annotation

    artifact_sha256 = spec["annotation_artifact_sha256"]
    path = service.registry.blob_path(artifact_sha256)
    content = path.read_bytes()
    if len(content) > _MAX_ANNOTATION_JSON_BYTES:
        raise ValueError("Registered annotation artifact exceeds 256 KiB.")
    try:
        document = json.loads(content)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Registered annotation artifact is not JSON.") from exc
    try:
        canonical_content = canonical(document).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Registered annotation artifact is not canonical JSON.") from exc
    if not isinstance(document, dict) or canonical_content != content:
        raise ValueError("Registered annotation artifact is not canonical JSON.")
    validated = validate_raw_annotation(
        document,
        source_binding=lens_source["source_binding"],
        raw_shape=lens_source["source_raw_shape"],
    )
    return validated, {
        "annotation_artifact_sha256": artifact_sha256,
        "source_raw_content_sha256": lens_source["source_binding"]["source_raw_content_sha256"],
        "source_raw_shape": lens_source["source_raw_shape"],
        "coordinate_frame": validated["coordinate_frame"],
        "provenance": validated["provenance"],
        "object_count": len(validated["objects"]),
    }


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    lens_source = spec["schema"] in {LENS_SCHEMA, LENS_LABELED_SCHEMA}
    source = _lens_source(service, spec) if lens_source else _source(service, spec)
    annotation = (
        _annotation_source(service, spec, source)[1]
        if spec["schema"] == LENS_LABELED_SCHEMA else None
    )
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        **source,
        **({"annotation_binding": annotation} if annotation is not None else {}),
        "environment": _current(service),
        "limitation": (
            LENS_LABELED_LIMITATION if annotation is not None else
            LENS_LIMITATION if lens_source else LIMITATION
        ),
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .retained_raw_dataset import (
        export_retained_quality_raw,
        export_retained_raw_array,
        load_retained_lens_sensor_raw,
        make_portable_bundle,
    )

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Retained RAW dataset job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen quality source, executable or request changed.")
    spec = frozen["specification"]
    lens_source = spec["schema"] in {LENS_SCHEMA, LENS_LABELED_SCHEMA}
    labeled = spec["schema"] == LENS_LABELED_SCHEMA
    with tempfile.TemporaryDirectory(prefix="camera-retained-raw-") as temporary:
        temporary_root = Path(temporary)
        if lens_source:
            _, raw, source_binding = load_retained_lens_sensor_raw(
                service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
                service.registry.blob_path(frozen["source_artifacts"]["raw"]["sha256"]),
                source_lens_sensor_job_id=spec["source_job_id"],
            )
            annotation_document = None
            if labeled:
                annotation_document, annotation_binding = _annotation_source(
                    service, spec, frozen
                )
                if annotation_binding != frozen["annotation_binding"]:
                    raise IntegrityError("Annotation source changed after submission.")
            report = export_retained_raw_array(
                raw,
                source_binding=source_binding,
                seed=source_binding["source_frame_seed"],
                scene=source_binding["source_scene"],
                split=spec["split"],
                output_dir=temporary_root / "dataset",
                annotation_document=annotation_document,
                annotation_artifact_sha256=(
                    spec["annotation_artifact_sha256"] if labeled else None
                ),
            )
        else:
            report = export_retained_quality_raw(
                service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
                service.registry.blob_path(frozen["source_artifacts"]["npz"]["sha256"]),
                source_quality_job_id=spec["source_quality_job_id"],
                case_index=spec["case_index"],
                split=spec["split"],
                output_dir=temporary_root / "dataset",
            )
        binding = report["source_binding"]
        if lens_source:
            if (
                binding != frozen["source_binding"]
                or report["raw_shape"] != frozen["source_raw_shape"]
            ):
                raise IntegrityError("Exported RAW lost its Lens × Sensor source binding.")
            if (
                labeled
                and report["annotation_artifact_sha256"]
                != spec["annotation_artifact_sha256"]
            ):
                raise IntegrityError("Exported annotation lost its registered artifact hash.")
        elif (
            binding["saved_sensor_revision"] != frozen["source_revision"]
            or binding["source_report_sha256"] != frozen["source_artifacts"]["json"]["sha256"]
            or binding["source_arrays_sha256"] != frozen["source_artifacts"]["npz"]["sha256"]
            or binding["source_scene"] != frozen["source_case"]["scene"]
            or binding["source_frame_seed"] != frozen["source_case"]["frame_seed"]
        ):
            raise IntegrityError("Exported RAW lost its saved quality source binding.")
        archive = make_portable_bundle(temporary_root / "dataset", temporary_root / "dataset.zip")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Saved quality source or executable changed during export.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Retained RAW dataset job was cancelled during export.")
        bundle_bytes = Path(archive["path"]).read_bytes()
        if hashlib.sha256(bundle_bytes).hexdigest() != archive["sha256"]:
            raise IntegrityError("Portable RAW dataset bundle bytes changed.")
        bundle_descriptor = service.registry.store_blob(
            bundle_bytes, "retained-raw-dataset.zip", "application/zip"
        )
    summary = {
        "schema": spec["schema"],
        "status": "completed",
        "specification": spec,
        **(
            {"source_capture_result_sha256": frozen["source_capture_result_sha256"]}
            if lens_source else
            {"source_quality_result_sha256": frozen["source_quality_result_sha256"]}
        ),
        "source_kind": "lens_sensor_raw" if lens_source else "saved_sensor_quality",
        "source_binding": binding,
        "raw_values_byte_equal": report["raw_values_byte_equal"],
        "raw_content_sha256": report["raw_content_sha256"],
        "raw_shape": report["raw_shape"],
        "raw_dtype": report["raw_dtype"],
        "dataset_validation": {
            "ok": report["dataset_validation"]["ok"],
            "issue_count": report["dataset_validation"]["issue_count"],
            "warning_count": report["dataset_validation"]["warning_count"],
        },
        "raw_training_index": report["raw_training_index"],
        "labels": report["labels"],
        **({"annotation_binding": frozen["annotation_binding"]} if labeled else {}),
        "perception_metric": report["perception_metric"],
        "bundle": bundle_descriptor,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": (
            LENS_LABELED_LIMITATION if labeled else
            LENS_LIMITATION if lens_source else LIMITATION
        ),
    }
    summary_descriptor = service.registry.store_blob(
        canonical(summary).encode(), "retained-raw-dataset-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [bundle_descriptor, summary_descriptor]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a retained RAW dataset export.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if canonical(summary["specification"]) != canonical(job["specification"]):
            raise IntegrityError("Retained RAW dataset result differs from frozen request.")
        labeled = job["specification"]["schema"] == LENS_LABELED_SCHEMA
        source_key = (
            "source_capture_result_sha256"
            if job["specification"]["schema"] in {LENS_SCHEMA, LENS_LABELED_SCHEMA}
            else "source_quality_result_sha256"
        )
        if summary[source_key] != job[source_key]:
            raise IntegrityError("Retained RAW dataset source result hash changed.")
        if labeled:
            source = _lens_source(service, job["specification"])
            _, annotation_binding = _annotation_source(
                service, job["specification"], source
            )
            if (
                annotation_binding != job.get("annotation_binding")
                or summary.get("annotation_binding") != annotation_binding
                or summary.get("labels") != "user_provided_unverified"
                or summary.get("source_binding") != source["source_binding"]
            ):
                raise IntegrityError("Retained RAW annotation or source binding changed.")
        if job["status"] == "succeeded":
            if summary["bundle"] not in job["artifacts"] or len(job["artifacts"]) != 2:
                raise IntegrityError("Retained RAW dataset bundle artifact is missing.")
            for descriptor in job["artifacts"]:
                service.registry.blob_path(descriptor["sha256"])
            result_descriptor = next(
                (
                    item
                    for item in job["artifacts"]
                    if item["name"] == "retained-raw-dataset-result.json"
                ),
                None,
            )
            if result_descriptor is None or (
                service.registry.blob_path(result_descriptor["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Retained RAW dataset result artifact differs.")
            with ZipFile(service.registry.blob_path(summary["bundle"]["sha256"])) as archive:
                with np.load(
                    io.BytesIO(archive.read("raw/retained_0000.npz")), allow_pickle=False
                ) as raw:
                    if _sha256_array(raw["raw"]) != summary["raw_content_sha256"]:
                        raise IntegrityError(
                            "Retained RAW bundle content differs from source hash."
                        )
                manifest = json.loads(archive.read("manifest.json"))
                if canonical(manifest["source_binding"]) != canonical(summary["source_binding"]):
                    raise IntegrityError("Retained RAW bundle source binding differs.")
                if labeled:
                    annotation_bytes = archive.read("annotations/retained_0000-source.json")
                    annotation_sha256 = hashlib.sha256(annotation_bytes).hexdigest()
                    if (
                        annotation_sha256 != job["specification"]["annotation_artifact_sha256"]
                        or manifest.get("annotation_artifact_sha256") != annotation_sha256
                    ):
                        raise IntegrityError("Bundled annotation source hash differs.")
                    annotation = json.loads(annotation_bytes)
                    labels_bytes = archive.read("labels/retained_0000.json")
                    labels = json.loads(labels_bytes)["labels"]
                    record = manifest["records"][0]
                    class_map = {
                        label: index for index, label in enumerate(sorted({
                            item["label"] for item in annotation["objects"]
                        }))
                    }
                    if (
                        manifest.get("class_map") != class_map
                        or labels.get("objects") != [
                            {**item, "class_id": class_map[item["label"]]}
                            for item in annotation["objects"]
                        ]
                        or labels.get("provenance") != annotation.get("provenance")
                        or labels.get("source_annotation_sha256") != annotation_sha256
                        or labels.get("coordinate_frame") != annotation.get("coordinate_frame")
                        or labels.get("image_size_rc") != annotation.get("raw_shape")
                        or labels.get("source") != "user_provided_unverified"
                        or record.get("labels_sha256")
                        != f"sha256:{hashlib.sha256(labels_bytes).hexdigest()}"
                        or record.get("source_annotation_sha256") != f"sha256:{annotation_sha256}"
                    ):
                        raise IntegrityError(
                            "Bundled labels differ from the registered annotation."
                        )
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    from .retained_raw_dataset import (
        RAW_ANNOTATION_SCHEMA,
        raw_annotation_template,
        validate_raw_annotation,
    )

    @app.post(
        "/api/v1/retained-raw-datasets/annotations",
        status_code=201,
        tags=["retained_raw_dataset"],
    )
    async def import_annotation(request: Request):
        blocks: list[bytes] = []
        total = 0
        async for block in request.stream():
            total += len(block)
            if total > _MAX_ANNOTATION_BODY_BYTES:
                raise HTTPException(413, "Annotation request exceeds 512 KiB.")
            blocks.append(block)
        try:
            payload = json.loads(b"".join(blocks))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise HTTPException(422, "Annotation import request must be JSON.") from exc
        if not isinstance(payload, dict) or set(payload) not in (
            {"document"}, {"document", "expected_sha256"}
        ):
            raise HTTPException(422, "Declare document and optional expected_sha256 exactly.")
        document = payload["document"]
        if not isinstance(document, dict) or document.get("schema") != RAW_ANNOTATION_SCHEMA:
            raise HTTPException(422, "Annotation document schema differs.")
        binding = document.get("source_binding")
        if not isinstance(binding, dict):
            raise HTTPException(422, "Annotation requires exact source RAW binding.")
        source_id = binding.get("source_lens_sensor_job_id")
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 128:
            raise HTTPException(422, "Annotation requires a Lens RAW source job ID.")
        source = _lens_source(service, {"source_job_id": source_id})
        try:
            normalized = validate_raw_annotation(
                document,
                source_binding=source["source_binding"],
                raw_shape=source["source_raw_shape"],
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            encoded = canonical(normalized).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise HTTPException(422, "Annotation is not canonical finite JSON.") from exc
        if len(encoded) > _MAX_ANNOTATION_JSON_BYTES:
            raise HTTPException(413, "Canonical annotation exceeds 256 KiB.")
        computed_sha256 = hashlib.sha256(encoded).hexdigest()
        if (
            "expected_sha256" in payload
            and computed_sha256 != payload["expected_sha256"]
        ):
            raise HTTPException(422, "Annotation SHA differs from canonical JSON bytes.")
        artifact = service.registry.store_blob(
            encoded, "retained-raw-source-annotation.json", "application/json"
        )
        return {
            "schema": RAW_ANNOTATION_SCHEMA,
            "artifact": artifact,
            "computed_sha256": computed_sha256,
            "expected_sha256_checked": "expected_sha256" in payload,
            "source_job_id": source_id,
            "coordinate_frame": normalized["coordinate_frame"],
            "verification": "user_supplied_annotation_contract_only",
            "canonicalization": (
                "Server source_binding and raw_shape normalized before UTF-8 JSON "
                "hashing; sorted keys, compact separators, finite numbers"
            ),
            "expected_sha256_scope": "server_normalized_canonical_annotation_bytes",
        }

    @app.get("/api/v1/retained-raw-datasets/template", tags=["retained_raw_dataset"])
    def template(source_kind: str = "saved_sensor_quality", source_job_id: str = ""):
        if source_kind in {"lens_sensor_raw", "lens_sensor_raw_labeled"}:
            labeled = source_kind == "lens_sensor_raw_labeled"
            sources = [
                {"id": item["id"], "name": item["name"], "status": item["status"]}
                for item in service.registry.list_runs()
                if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
            ]
            result = {
                "specification": default_lens_labeled_spec() if labeled else default_lens_spec(),
                "source_jobs": sources,
                "limitation": LENS_LABELED_LIMITATION if labeled else LENS_LIMITATION,
            }
            if labeled and source_job_id:
                source = _lens_source(service, {"source_job_id": source_job_id})
                result["annotation_template"] = raw_annotation_template(
                    source["source_binding"], source["source_raw_shape"]
                )
            return result
        if source_kind != "saved_sensor_quality":
            raise ValueError(
                "source_kind must be saved_sensor_quality, lens_sensor_raw, "
                "or lens_sensor_raw_labeled."
            )
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "saved_sensor_quality" and item["status"] == "succeeded"
        ]
        return {
            "specification": default_spec(),
            "source_quality_jobs": sources,
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/retained-raw-datasets", status_code=202, tags=["retained_raw_dataset"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/retained-raw-datasets", tags=["retained_raw_dataset"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/retained-raw-datasets/{identity}", tags=["retained_raw_dataset"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/retained-raw-datasets/{identity}/cancel", tags=["retained_raw_dataset"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/retained-raw-datasets/{identity}/export", tags=["retained_raw_dataset"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Retained RAW dataset bundle is not ready.")
        content = service.registry.blob_path(
            job["result"]["summary"]["bundle"]["sha256"]
        ).read_bytes()
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="retained-raw-dataset.zip"'},
        )

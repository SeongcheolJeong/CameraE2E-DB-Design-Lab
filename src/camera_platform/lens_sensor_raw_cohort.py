"""Export a declared cohort of saved Lens x Sensor RAW jobs without recapture.

The caller supplies registry-verified completed job records and artifact paths.
This file adapter verifies their frozen fields and artifact bytes, then writes
an unlabeled or explicitly user-annotated CameraE2E RAW dataset. The latter
reuses the single-frame RAW annotation contract and remains unverified truth.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import numpy as np

from pyisetcam.dataset import (
    _sha256_array,
    _write_npz_deterministic,
    camerae2e_dataset_export_perception_index,
    camerae2e_dataset_validate,
)

from .lens_sensor_perception import _source
from .registry import canonical
from .retained_raw_dataset import validate_raw_annotation

SCHEMA = "camerae2e_lens_sensor_raw_cohort_v1"
_SPLITS = frozenset({"train", "validation", "test"})
_SOURCE_KEYS = frozenset({"job", "source_report_path", "source_raw_path", "split"})
_ANNOTATED_SOURCE_KEYS = _SOURCE_KEYS | frozenset({
    "annotation_document", "annotation_artifact_sha256"
})
_LABEL_MODES = frozenset({"unlabeled", "user_provided_unverified"})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checked_existing_manifests(
    manifests: Sequence[str | Path], source_hashes: set[str],
    *, scene_splits: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    if (
        not isinstance(manifests, Sequence)
        or isinstance(manifests, (str, bytes, Path))
        or len(manifests) > 256
    ):
        raise ValueError("Pass at most 256 declared existing cohort manifests.")
    checked = []
    for item in manifests:
        path = Path(item).expanduser().resolve()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "camerae2e_dataset_manifest_v1":
            raise ValueError("Existing cohort manifest has an unsupported schema.")
        # A portable ZIP's dataset_root is ".". Interpret it beside the
        # manifest even when the caller has a different working directory.
        validation = camerae2e_dataset_validate({**manifest, "dataset_root": str(path.parent)})
        if not validation["ok"] or validation["issue_count"]:
            raise ValueError("Existing cohort manifest or RAW data failed validation.")
        for record in manifest["records"]:
            if record["raw_content_sha256"] in source_hashes:
                raise ValueError("The exact RAW already occurs in a declared cohort manifest.")
            if scene_splits is not None:
                binding = record.get("source_binding")
                if not isinstance(binding, dict) or not isinstance(
                    binding.get("source_scene"), str
                ):
                    raise ValueError(
                        "Existing cohort lacks a declared source scene family; "
                        "annotated split isolation cannot be checked."
                    )
                scene = binding["source_scene"]
                if scene in scene_splits and record.get("split") != scene_splits[scene]:
                    raise ValueError(
                        "The same source scene identity crosses a declared "
                        "existing cohort split." if scene.startswith("object_radiance_sha256:")
                        else "The same synthetic scene family crosses a declared "
                        "existing cohort split."
                    )
        checked.append({"path": str(path), "manifest_sha256": _sha256(path)})
    return checked


def _source_groups(
    sources: Sequence[Mapping[str, Any]], *, labels: str
) -> list[dict[str, Any]]:
    if (
        not isinstance(sources, Sequence)
        or isinstance(sources, (str, bytes, Mapping))
        or not 2 <= len(sources) <= 256
    ):
        raise ValueError("Provide 2..256 completed Lens x Sensor RAW sources.")
    groups: dict[str, dict[str, Any]] = {}
    job_ids: set[str] = set()
    scene_splits: dict[str, str] = {}
    annotated = labels == "user_provided_unverified"
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != (
            _ANNOTATED_SOURCE_KEYS if annotated else _SOURCE_KEYS
        ):
            raise ValueError(
                "Each source needs its exact job, artifact paths, split and "
                "annotation fields when labels are supplied."
            )
        split = source["split"]
        if not isinstance(split, str) or split not in _SPLITS:
            raise ValueError("split must be train, validation or test.")
        job = source["job"]
        if (
            not isinstance(job, Mapping)
            or not isinstance(job.get("id"), str)
            or job["id"] in job_ids
        ):
            raise ValueError("Each source must be a distinct saved Lens x Sensor RAW job.")
        verified, _ = _source(
            job,
            Path(source["source_report_path"]).expanduser().resolve(),
            Path(source["source_raw_path"]).expanduser().resolve(),
        )
        raw, binding = verified["raw"], verified["binding"]
        if raw.dtype != np.dtype("int32") or raw.ndim != 2 or any(
            not 8 <= dimension <= 32 for dimension in raw.shape
        ):
            raise ValueError("Saved Lens x Sensor source is not a bounded int32 RAW frame.")
        raw_hash = binding["source_raw_content_sha256"]
        if raw_hash != _sha256_array(raw):
            raise ValueError("Source RAW content hash changed before cohort export.")
        annotation = None
        annotation_sha = None
        if annotated:
            annotation = validate_raw_annotation(
                source["annotation_document"],
                source_binding=binding,
                raw_shape=list(raw.shape),
            )
            annotation_sha = source["annotation_artifact_sha256"]
            if (
                not isinstance(annotation_sha, str)
                or len(annotation_sha) != 64
                or any(character not in "0123456789abcdef" for character in annotation_sha)
                or hashlib.sha256(canonical(annotation).encode()).hexdigest()
                != annotation_sha
            ):
                raise ValueError("Source annotation canonical SHA-256 differs.")
            # Legacy built-in scenes are generator families. Registered object
            # radiance uses the original NPY SHA as a source-scene identity.
            # Neither establishes an independent holdout across camera changes.
            scene = binding["source_scene"]
            prior_scene_split = scene_splits.setdefault(scene, split)
            if prior_scene_split != split:
                raise ValueError(
                    "The same source scene identity cannot cross annotated cohort splits."
                    if scene.startswith("object_radiance_sha256:") else
                    "The same synthetic scene family cannot cross annotated cohort splits."
                )
        job_ids.add(job["id"])
        group = groups.get(raw_hash)
        if group is None:
            groups[raw_hash] = {
                "raw": raw,
                "split": split,
                "source_bindings": [binding],
                **({"annotation": annotation, "annotation_sha256": annotation_sha}
                   if annotated else {}),
            }
        else:
            if annotated:
                raise ValueError(
                    "Annotated cohort requires distinct RAW per source; duplicate "
                    "RAW may carry conflicting labels."
                )
            if group["split"] != split:
                raise ValueError("The exact RAW cannot be reused across cohort splits.")
            if not np.array_equal(group["raw"], raw):
                raise ValueError("RAW content hash collision or corrupted source.")
            group["source_bindings"].append(binding)
    for raw_hash, group in groups.items():
        group["raw_content_sha256"] = raw_hash
        group["source_bindings"].sort(key=lambda item: item["source_lens_sensor_job_id"])
    return [groups[raw_hash] for raw_hash in sorted(groups)]


def _record(
    destination: Path, case_id: str, group: dict[str, Any],
    *, class_map: dict[str, int],
) -> dict[str, Any]:
    raw = group["raw"]
    binding = group["source_bindings"][0]
    raw_relative = f"raw/{case_id}.npz"
    labels_relative = f"labels/{case_id}.json"
    raw_path = destination / raw_relative
    labels_path = destination / labels_relative
    annotation = group.get("annotation")
    annotated = annotation is not None
    digital_unavailable = np.empty((0,), dtype=np.int32)
    _write_npz_deterministic(
        raw_path,
        raw=raw,
        sensor_digital=digital_unavailable,
        seed=np.asarray([binding["source_frame_seed"]], dtype=np.int64),
    )
    with np.load(raw_path, allow_pickle=False) as saved:
        exported = np.asarray(saved["raw"])
        if (
            exported.dtype != np.dtype("int32")
            or exported.shape != raw.shape
            or exported.tobytes(order="C") != raw.tobytes(order="C")
            or _sha256_array(exported) != group["raw_content_sha256"]
        ):
            raise ValueError("Cohort export changed the original int32 RAW values.")
    annotation_relative = f"annotations/{case_id}-source.json" if annotated else None
    if annotated:
        annotation_bytes = canonical(annotation).encode()
        (destination / annotation_relative).write_bytes(annotation_bytes)
        if hashlib.sha256(annotation_bytes).hexdigest() != group["annotation_sha256"]:
            raise ValueError("Source annotation changed during cohort export.")
    labels = (
        {
            "objects": [
                {**item, "class_id": class_map[item["label"]]}
                for item in annotation["objects"]
            ],
            "masks": [],
            "source": "user_provided_unverified",
            "image_size_rc": list(raw.shape),
            "coordinate_frame": annotation["coordinate_frame"],
            "provenance": annotation["provenance"],
            "source_annotation_sha256": group["annotation_sha256"],
        }
        if annotated else
        {"objects": [], "masks": [], "source": "unlabeled"}
    )
    labels_path.write_text(canonical({
        "schema_version": "camerae2e_dataset_labels_v1",
        "case_id": case_id,
        "labels": labels,
        "note": (
            "User-supplied RAW-pixel boxes; truth, origin, completeness and "
            "independence are not independently verified."
            if annotated else "No independent annotation or ground truth was supplied."
        ),
    }) + "\n", encoding="utf-8")
    record = {
        "case_id": case_id,
        "split": group["split"],
        "seed": binding["source_frame_seed"],
        "scenario_name": binding["source_scene"],
        "raw": raw_relative,
        "rgb": None,
        "raw_tiff": None,
        "stage_outputs": None,
        "container": None,
        "uncertainty": None,
        "labels": labels_relative,
        "raw_shape": list(raw.shape),
        "raw_dtype": "int32",
        "sensor_digital_shape": [0],
        "sensor_digital_dtype": "int32",
        "raw_sha256": f"sha256:{_sha256(raw_path)}",
        "raw_content_sha256": group["raw_content_sha256"],
        "sensor_digital_content_sha256": _sha256_array(digital_unavailable),
        "rgb_sha256": None,
        "raw_tiff_sha256": None,
        "stage_outputs_sha256": None,
        "container_sha256": None,
        "uncertainty_sha256": None,
        "labels_sha256": f"sha256:{_sha256(labels_path)}",
        "source_binding": binding,
        "source_bindings": group["source_bindings"],
        "label_summary": {
            "source": "user_provided_unverified" if annotated else "unlabeled",
            "object_count": len(labels["objects"]), "mask_count": 0,
        },
        "faca_metrics": {},
        "parameter_lineage": [],
    }
    if annotated:
        record["source_annotation"] = annotation_relative
        record["source_annotation_sha256"] = f"sha256:{group['annotation_sha256']}"
    return record


def _portable_zip(destination: Path, records: list[dict[str, Any]]) -> Path:
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["dataset_root"] = "."
    members = {
        "manifest.json": (canonical(manifest) + "\n").encode(),
        "metadata.jsonl": (destination / "metadata.jsonl").read_bytes(),
        "README.txt": (
            (
                b"Exact retained Lens x Sensor RAW with user-supplied, unverified "
                b"RAW-pixel annotations. "
                if manifest.get("labels") == "user_provided_unverified" else
                b"Unlabeled exact retained Lens x Sensor RAW frames. "
            )
            + b"Extract, change into this directory, and validate manifest.json "
            b"with pyisetcam.dataset. No new capture, detector, metric, "
            b"calibrated radiometry or physical validation is provided.\n"
        ),
    }
    for record in records:
        for key in ("raw", "labels", "source_annotation"):
            if record.get(key) is None:
                continue
            name = record[key]
            members[name] = (destination / name).read_bytes()
    archive_path = destination / "lens-sensor-raw-cohort.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return archive_path


def export_lens_sensor_raw_cohort(
    sources: Sequence[Mapping[str, Any]],
    *,
    output_dir: str | Path,
    existing_manifests: Sequence[str | Path] = (),
    labels: str = "unlabeled",
) -> dict[str, Any]:
    """Write one portable exact-RAW cohort with explicit label limitations.

    Source Job records must come from a registry-verifying getter. Cross-export
    isolation covers only manifests explicitly supplied by the caller.
    """
    if labels not in _LABEL_MODES:
        raise ValueError("labels must be unlabeled or user_provided_unverified.")
    groups = _source_groups(sources, labels=labels)
    checked = _checked_existing_manifests(
        existing_manifests,
        {group["raw_content_sha256"] for group in groups},
        scene_splits=(
            {group["source_bindings"][0]["source_scene"]: group["split"]
             for group in groups}
            if labels == "user_provided_unverified" else None
        ),
    )
    requested = Path(output_dir).expanduser()
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("Cohort output_dir must be a new path.")
    destination = requested.resolve()
    (destination / "raw").mkdir(parents=True)
    (destination / "labels").mkdir()
    if labels == "user_provided_unverified":
        (destination / "annotations").mkdir()
    class_map = (
        {label: index for index, label in enumerate(sorted({
            item["label"]
            for group in groups for item in group["annotation"]["objects"]
        }))}
        if labels == "user_provided_unverified" else {}
    )
    records = [
        _record(destination, f"retained_{index:04d}", group, class_map=class_map)
        for index, group in enumerate(groups)
    ]
    metadata_path = destination / "metadata.jsonl"
    metadata_path.write_text(
        "".join(canonical(record) + "\n" for record in records), encoding="utf-8"
    )
    split_counts = {
        split: sum(record["split"] == split for record in records)
        for split in sorted({record["split"] for record in records})
    }
    includes_registered_radiance = any(
        binding["source_scene"].startswith("object_radiance_sha256:")
        for group in groups for binding in group["source_bindings"]
    )
    split_policy = {
        "assignment_unit": (
            ("source_scene_identity_and_distinct_raw_content_within_this_export"
             if includes_registered_radiance else
             "synthetic_scene_family_and_distinct_raw_content_within_this_export")
            if labels == "user_provided_unverified" else
            "distinct_raw_content_within_this_export"
        ),
        "identical_raw_across_splits_rejected": True,
        "same_split_duplicate_source_jobs_coalesced": len(sources) - len(records),
        **({
            **({"source_scene_identity_across_splits_rejected": True}
               if includes_registered_radiance else
               {"synthetic_scene_family_across_splits_rejected": True}),
            "scene_family_field": "source_binding.source_scene",
            "scene_family_is_independent_capture_identity": False,
        } if labels == "user_provided_unverified" else {}),
        "checked_existing_manifests": checked,
        "cross_export_exclusivity_enforced": False,
        "holdout_independence_claimed": False,
    }
    annotation_provenance = (
        {
            "status": "user_provided_unverified",
            "ground_truth_available": False,
            "source_artifact_sha256": [
                group["annotation_sha256"] for group in groups
            ],
            "full_frame_reviewed_declared_count": sum(
                group["annotation"]["provenance"]["coverage"]
                == "full_frame_reviewed" for group in groups
            ),
            "independence_verified": False,
        }
        if labels == "user_provided_unverified" else
        {"status": "absent", "ground_truth_available": False}
    )
    manifest = {
        "schema_version": "camerae2e_dataset_manifest_v1",
        "dataset_root": str(destination),
        "case_count": len(records),
        "source_job_count": len(sources),
        "labels": labels,
        "class_map": class_map,
        "annotation_provenance": annotation_provenance,
        "source_job_record_verification": (
            "Caller supplies registry-verified completed records; this adapter "
            "checks frozen fields and artifact bytes, not the registry audit."
        ),
        "splits": split_counts,
        "split_policy": split_policy,
        "format": {
            "raw": "deterministic compressed NumPy NPZ with exact original int32 values",
            "sensor_digital": "empty: separate stage was not retained",
            "rgb": "not exported",
            "labels": (
                "user-provided source RAW mosaic pixel boxes, unverified"
                if labels == "user_provided_unverified" else "explicitly unlabeled"
            ),
            "dng": "not emitted",
        },
        "truth_boundary": (
            "Completed saved Lens x Sensor RAW artifacts only; no new capture or ISP. "
            + (
                "User annotations are hash-bound to source RAW but their truth, "
                "origin, completeness and independence are unverified. "
                + ("A source radiance NPY identity cannot cross splits in this export. "
                   if includes_registered_radiance else
                   "Synthetic scene families cannot cross splits in this export. ")
                if labels == "user_provided_unverified" else
                "No annotation or ground truth was supplied. "
            )
            + "No detector, perception metric, calibrated radiometry or physical "
            "validation. Holdout independence and global cross-export "
            "exclusivity are not established."
        ),
        "records": records,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(canonical(manifest) + "\n", encoding="utf-8")
    validation = camerae2e_dataset_validate(manifest_path)
    if not validation["ok"] or validation["issue_count"]:
        raise ValueError(f"CameraE2E dataset validation failed: {validation['issues']}")
    index = camerae2e_dataset_export_perception_index(
        manifest_path,
        output_dir=destination / "perception_index",
        formats=("raw_manifest",),
        class_map=class_map,
    )
    archive_path = _portable_zip(destination, records)
    return {
        "schema": SCHEMA,
        "status": "completed",
        "source_job_record_verification": manifest["source_job_record_verification"],
        "source_job_count": len(sources),
        "case_count": len(records),
        "split_policy": split_policy,
        "dataset_validation": validation,
        "raw_training_index": {
            "row_count": index["outputs"]["raw_manifest"]["row_count"],
            "warning_count": index["warning_count"],
        },
        "annotation_provenance": annotation_provenance,
        "perception_metric": {
            "available": False,
            "reason": (
                "User-supplied annotations are unverified and no detector "
                "predictions were supplied."
                if labels == "user_provided_unverified" else
                "No independent ground-truth annotation or detector was supplied."
            ),
        },
        "detector_evaluation_performed": False,
        "physical_validation": "not_established",
        "files": {
            "manifest": str(manifest_path),
            "metadata": str(metadata_path),
            "raw_manifest": index["outputs"]["raw_manifest"]["path"],
            "zip": str(archive_path),
        },
        "integrity": {
            "manifest_sha256": _sha256(manifest_path),
            "zip_sha256": _sha256(archive_path),
            "distinct_raw_content_sha256": [record["raw_content_sha256"] for record in records],
        },
    }

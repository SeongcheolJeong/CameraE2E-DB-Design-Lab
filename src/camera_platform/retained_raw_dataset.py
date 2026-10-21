"""Export one retained RAW frame from a saved sensor-quality job.

The source quality job already performed the capture. This adapter copies the
captured integer array into the existing CameraE2E dataset format; it never
invokes scene, optics, sensor, readout, or ISP computation. The generic dataset
factory cannot be used here because that factory takes scenarios and captures
new RAW. Its validator and RAW-aware indexer can safely consume these files.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
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

from .registry import canonical

SCHEMA = "camerae2e_retained_raw_dataset_v1"
_SOURCE_SCHEMA = "camerae2e_saved_sensor_quality_v1"
_SPLITS = frozenset({"train", "validation", "test"})
RAW_ANNOTATION_SCHEMA = "camerae2e_retained_raw_annotations_v1"
RAW_ANNOTATION_COORDINATES = "raw_mosaic_pixel_xyxy_half_open"
_MAX_ANNOTATION_OBJECTS = 2048


def raw_annotation_template(
    source_binding: dict[str, Any], raw_shape: list[int]
) -> dict[str, Any]:
    """Describe an annotation of the source RAW mosaic, never an ISP RGB image."""
    return {
        "schema": RAW_ANNOTATION_SCHEMA,
        "source_binding": deepcopy(source_binding),
        "raw_shape": list(raw_shape),
        "coordinate_frame": RAW_ANNOTATION_COORDINATES,
        "provenance": {
            "provider_id": "",
            "annotation_set_id": "",
            "method": "human_review",
            "coverage": "full_frame_reviewed",
        },
        "objects": [],
    }


def _json_equal_allow_numeric_roundtrip(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (
            (not isinstance(left, float) or math.isfinite(left))
            and (not isinstance(right, float) or math.isfinite(right))
            and left == right
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal_allow_numeric_roundtrip(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal_allow_numeric_roundtrip(a, b)
            for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def validate_raw_annotation(
    document: Any,
    *,
    source_binding: dict[str, Any],
    raw_shape: list[int],
) -> dict[str, Any]:
    """Require an exact source RAW and bounded, half-open RAW-pixel boxes."""
    expected = raw_annotation_template(source_binding, raw_shape)
    if not isinstance(document, dict) or set(document) != set(expected):
        raise ValueError("Declare exactly the retained RAW annotation fields.")
    if document["schema"] != RAW_ANNOTATION_SCHEMA:
        raise ValueError("Retained RAW annotation schema differs.")
    # A browser can round-trip integral JSON floats as integers. Numeric value
    # equality is enough for the supplied binding; normalize back to the exact
    # server-side source binding before hashing and retaining the document.
    if not _json_equal_allow_numeric_roundtrip(document["source_binding"], source_binding):
        raise ValueError("Annotation source binding differs from the exact Lens RAW.")
    if (
        not isinstance(raw_shape, list)
        or len(raw_shape) != 2
        or any(type(value) is not int or value <= 0 for value in raw_shape)
        or not isinstance(document["raw_shape"], list)
        or len(document["raw_shape"]) != 2
        or any(type(value) is not int for value in document["raw_shape"])
        or document["raw_shape"] != raw_shape
    ):
        raise ValueError("Annotation RAW shape differs from the source frame.")
    if document["coordinate_frame"] != RAW_ANNOTATION_COORDINATES:
        raise ValueError("Annotation boxes must use half-open source RAW mosaic pixels.")
    provenance = document["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != set(expected["provenance"]):
        raise ValueError("Declare exactly the annotation provenance fields.")
    for key in ("provider_id", "annotation_set_id"):
        value = provenance[key]
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
            raise ValueError(f"Annotation provenance {key} requires 1..160 characters.")
    if not isinstance(provenance["method"], str) or provenance["method"] not in {
        "human_review", "external_reference", "synthetic_author"
    }:
        raise ValueError("Annotation provenance method is unsupported.")
    if not isinstance(provenance["coverage"], str) or provenance["coverage"] not in {
        "full_frame_reviewed", "partial"
    }:
        raise ValueError("Annotation coverage must be explicitly declared.")
    objects = document["objects"]
    if not isinstance(objects, list) or len(objects) > _MAX_ANNOTATION_OBJECTS:
        raise ValueError("Annotation objects must be a bounded list.")
    height, width = raw_shape
    for index, item in enumerate(objects):
        if not isinstance(item, dict) or set(item) != {"label", "bbox_xyxy"}:
            raise ValueError(f"Annotation object {index} must declare label and bbox_xyxy.")
        label = item["label"]
        box = item["bbox_xyxy"]
        if (
            not isinstance(label, str)
            or not 1 <= len(label.strip()) <= 128
            or any(ord(character) < 32 for character in label)
            or not isinstance(box, list)
            or len(box) != 4
            or any(
                type(value) is not int
                and (type(value) is not float or not math.isfinite(value))
                for value in box
            )
        ):
            raise ValueError(f"Annotation object {index} has invalid label or box.")
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError(f"Annotation object {index} is outside the source RAW frame.")
    if not objects and provenance["coverage"] != "full_frame_reviewed":
        raise ValueError("An empty annotation requires declared full-frame review.")
    normalized = deepcopy(document)
    normalized["source_binding"] = deepcopy(source_binding)
    normalized["raw_shape"] = list(raw_shape)
    return normalized


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


def load_retained_quality_frame(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
) -> tuple[dict[str, Any], dict[str, Any], np.ndarray, dict[str, Any]]:
    """Read one original RAW array and its hash-bound quality-job lineage."""
    if not isinstance(source_quality_job_id, str) or not source_quality_job_id.strip():
        raise ValueError("An explicit source_quality_job_id is required.")
    if type(case_index) is not int or case_index < 0:
        raise ValueError("case_index must be a nonnegative integer.")
    source_report_path = Path(source_report_path).expanduser().resolve()
    source_arrays_path = Path(source_arrays_path).expanduser().resolve()
    report = json.loads(source_report_path.read_bytes())
    if report.get("schema") != _SOURCE_SCHEMA or report.get("status") != "completed":
        raise ValueError("Source is not a completed saved sensor-quality report.")
    cases = report.get("cases")
    if not isinstance(cases, list) or case_index >= len(cases):
        raise ValueError("Selected quality case_index does not exist.")
    case = cases[case_index]
    arrays_sha256 = _sha256(source_arrays_path)
    if report.get("artifacts", {}).get("npz_sha256") != arrays_sha256:
        raise ValueError("Source quality NPZ bytes differ from the report hash.")
    prefix = case.get("array_prefix")
    seeds = case.get("frame_seeds")
    if not isinstance(prefix, str) or not prefix or not isinstance(seeds, list) or not seeds:
        raise ValueError("Source quality case lacks retained RAW identity and seed.")
    raw_key = f"{prefix}_raw_dn_frame"
    with np.load(source_arrays_path, allow_pickle=False) as source_arrays:
        if raw_key not in source_arrays.files:
            raise ValueError("Source quality NPZ lacks the selected retained RAW frame.")
        raw = np.asarray(source_arrays[raw_key])
    if raw.ndim != 2 or raw.dtype != np.dtype("int32") or raw.shape != tuple(case["native_size"]):
        raise ValueError("Selected source RAW is not the declared native int32 frame.")
    raw = np.ascontiguousarray(raw)
    source_binding = {
        "source_quality_job_id": source_quality_job_id,
        "saved_sensor_revision": report["revision"],
        "source_report_sha256": _sha256(source_report_path),
        "source_arrays_sha256": arrays_sha256,
        "source_case_index": case_index,
        "source_scene": case["scene"],
        "source_raw_array_key": raw_key,
        "source_raw_content_sha256": _sha256_array(raw),
        "source_frame_index": 0,
        "source_frame_seed": seeds[0],
    }
    return report, case, raw, source_binding


def load_retained_lens_sensor_raw(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
) -> tuple[dict[str, Any], np.ndarray, dict[str, Any]]:
    """Read the exact RAW artifact of a pinned geometric Lens × Sensor job."""
    if not isinstance(source_lens_sensor_job_id, str) or not source_lens_sensor_job_id.strip():
        raise ValueError("An explicit source_lens_sensor_job_id is required.")
    report_path = Path(source_report_path).expanduser().resolve()
    raw_path = Path(source_raw_path).expanduser().resolve()
    report = json.loads(report_path.read_bytes())
    if (
        report.get("schema") not in {
            "camerae2e_lens_sensor_raw_preview_study_v1",
            "camerae2e_lens_sensor_raw_preview_study_v2",
            "camerae2e_lens_sensor_raw_preview_study_v3",
            "camerae2e_lens_sensor_raw_preview_study_v4",
            "camerae2e_lens_sensor_raw_preview_study_v5",
            "camerae2e_lens_sensor_raw_preview_study_v6",
        }
        or report.get("status") != "completed"
    ):
        raise ValueError("Source is not a completed Lens × Sensor RAW report.")
    preview = report.get("preview") or {}
    spec = report.get("specification") or {}
    if report["schema"] in {
        "camerae2e_lens_sensor_raw_preview_study_v3",
        "camerae2e_lens_sensor_raw_preview_study_v4",
        "camerae2e_lens_sensor_raw_preview_study_v5",
        "camerae2e_lens_sensor_raw_preview_study_v6",
    }:
        resource = report.get("resource_preflight")
        executed = preview.get("resource_preflight")
        psf = (preview.get("lens_model") or {}).get("psf_execution")
        selection = (preview.get("lens_model") or {}).get("field_psf_selection")
        if (
            not isinstance(resource, dict)
            or resource != executed
            or spec.get("schema") != report["schema"]
            or resource.get("status") != "within_development_budget"
            or resource.get("scope") != "central_bounded_strict_raytrace"
            or preview.get("scope") != "approximate_geometric_central_field_raw_preview"
            or not isinstance(psf, dict)
            or psf.get("mode") != "strict_bounded_streaming"
            or psf.get("complete_precomputed_stack") is not False
            or type(psf.get("peak_cached_kernels")) is not int
            or not 0 <= psf["peak_cached_kernels"] <= 4
            or not isinstance(selection, dict)
            or selection.get("psf_compute_mode") != "bounded_streaming"
        ):
            raise ValueError(
                "Bounded Lens × Sensor RAW execution contract differs from the report."
            )
        if report["schema"] in {
            "camerae2e_lens_sensor_raw_preview_study_v4",
            "camerae2e_lens_sensor_raw_preview_study_v5",
            "camerae2e_lens_sensor_raw_preview_study_v6",
        }:
            scene = preview.get("scene") or {}
            grid = scene.get("angular_grid_contract") or {}
            output = (preview.get("capture") or {}).get("native_output_angular_grid") or {}
            if (
                not isinstance(grid, dict)
                or not isinstance(output, dict)
                or grid.get("mode") != "explicit_common_input_scene"
                or grid.get("native_output_grid_alignment") != "not_established"
                or scene.get("horizontal_fov_deg") != (
                    (report.get("source_identities") or {}).get("radiance_scene", {}).get(
                        "horizontal_fov_deg"
                    ) if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v6"
                    else spec.get("scene_horizontal_fov_deg")
                )
                or grid.get("horizontal_fov_deg") != scene.get("horizontal_fov_deg")
                or output.get("status")
                != "paraxial_center_only_full_distortion_map_not_established"
                or output.get("sensor_resize") is not False
                or not isinstance(grid.get("source_shape"), list)
                or len(grid["source_shape"]) != 2
                or any(type(value) is not int or value <= 0 for value in grid["source_shape"])
                or not isinstance(grid.get("center_angular_step_deg_row_col"), list)
                or len(grid["center_angular_step_deg_row_col"]) != 2
                or any(
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0
                    for value in grid["center_angular_step_deg_row_col"]
                )
                or any(
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0
                    for value in (
                        grid.get("vertical_fov_deg"), output.get("image_distance_m"),
                        output.get("horizontal_fov_deg"), output.get("vertical_fov_deg"),
                    )
                )
                or any(
                    not isinstance(output.get(key), list) or len(output[key]) != 2
                    or any(
                        not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value <= 0
                        for value in output[key]
                    )
                    for key in ("pixel_pitch_m_row_col", "center_angular_step_deg_row_col")
                )
            ):
                raise ValueError("Common input scene or native output angular evidence differs.")
            size = spec.get("sensor_size")
            pitch_um = (preview.get("sensor_model") or {}).get("pixel_pitch_um")
            if (
                not isinstance(size, list) or len(size) != 2
                or any(type(value) is not int or not 8 <= value <= 32 for value in size)
                or grid["source_shape"] != (
                    spec.get("scene_samples_row_col")
                    if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v5"
                    else (report.get("source_identities") or {}).get("radiance_scene", {}).get(
                        "shape", []
                    )[:2]
                    if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v6"
                    else [max(32, 2 * value) for value in size]
                )
                or not isinstance(pitch_um, (int, float)) or isinstance(pitch_um, bool)
                or not math.isfinite(pitch_um) or pitch_um <= 0
                or any(
                    not math.isclose(value, pitch_um * 1e-6, rel_tol=1e-8, abs_tol=1e-15)
                    for value in output["pixel_pitch_m_row_col"]
                )
            ):
                raise ValueError("Common scene source shape or native pixel pitch differs.")
            distance = output["image_distance_m"]
            pitch_row, pitch_col = output["pixel_pitch_m_row_col"]
            expected_source_steps = [
                math.degrees(math.atan(
                    2 * math.tan(math.radians(fov) / 2) / samples
                ))
                for fov, samples in zip(
                    (grid["vertical_fov_deg"], grid["horizontal_fov_deg"]),
                    grid["source_shape"], strict=True,
                )
            ]
            expected_steps = [
                math.degrees(math.atan(pitch / distance)) for pitch in (pitch_row, pitch_col)
            ]
            expected_fovs = [
                math.degrees(2 * math.atan(samples * pitch / (2 * distance)))
                for samples, pitch in ((size[1], pitch_col), (size[0], pitch_row))
            ]
            if (
                any(
                    not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
                    for actual, expected in zip(
                        grid["center_angular_step_deg_row_col"], expected_source_steps,
                        strict=True,
                    )
                )
                or
                any(
                    not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
                    for actual, expected in zip(
                        output["center_angular_step_deg_row_col"], expected_steps, strict=True
                    )
                )
                or any(
                    not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
                    for actual, expected in zip(
                        (output["horizontal_fov_deg"], output["vertical_fov_deg"]),
                        expected_fovs, strict=True,
                    )
                )
            ):
                raise ValueError("Native output angular geometry is internally inconsistent.")
            if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v6":
                source = (report.get("source_identities") or {}).get("radiance_scene") or {}
                source_hash = source.get("source_sha256")
                if (
                    not isinstance(source_hash, str) or len(source_hash) != 64
                    or any(char not in "0123456789abcdef" for char in source_hash)
                    or spec.get("radiance_scene_id") != source.get("id")
                    or scene.get("kind") != "registered_object_radiance"
                    or scene.get("source_asset") != source
                    or scene.get("source_unit") != "W/m^2/sr/nm"
                    or scene.get("source_reference_plane")
                    != "object_directional_radiance_before_lens"
                    or scene.get("source_wavelength_nm") != source.get("wavelength_nm")
                    or scene.get("luminance_cd_m2") is not None
                    or scene.get("spectral_conversion")
                    != "linear_energy_density_then_energy_to_quanta_once"
                ):
                    raise ValueError("Registered object-radiance source evidence is missing.")
    descriptor = preview.get("raw_artifact") or {}
    if descriptor.get("sha256") != _sha256(raw_path):
        raise ValueError("Lens × Sensor RAW artifact hash differs from the source report.")
    with raw_path.open("rb") as stream:
        raw = np.asarray(np.load(stream, allow_pickle=False))
    declared = preview.get("raw") or {}
    if (
        raw.dtype != np.dtype("int32")
        or raw.ndim != 2
        or list(raw.shape) != descriptor.get("shape")
        or list(raw.shape) != declared.get("shape")
        or declared.get("dtype") != "int32"
        or not np.array_equal(raw, np.asarray(declared.get("values"), dtype=np.int32))
    ):
        raise ValueError("Lens × Sensor RAW values differ from the saved result.")
    raw = np.ascontiguousarray(raw)
    if (
        spec.get("accept_approximate_lens_radiometry") is not True
        or preview.get("physical_validation") != "not_established"
        or spec.get("seed") != preview.get("capture", {}).get("seed")
    ):
        raise ValueError("Lens × Sensor RAW approximation or seed contract differs.")
    binding = {
        "source_lens_sensor_job_id": source_lens_sensor_job_id,
        "source_report_sha256": _sha256(report_path),
        "source_raw_artifact_sha256": descriptor["sha256"],
        "source_raw_content_sha256": _sha256_array(raw),
        "lens_identity": preview["lens_identity"],
        "lens_scope": preview["scope"],
        "lens_field_psf_selection": preview["lens_model"].get("field_psf_selection"),
        "saved_sensor_revision": preview["sensor_revision"],
        "source_scene": (
            "object_radiance_sha256:" + report["source_identities"]["radiance_scene"][
                "source_sha256"
            ] if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v6"
            else preview["scene"]["kind"]
        ),
        "source_frame_seed": spec["seed"],
        "lens_radiometry": "assumed_unity_not_validated",
        "physical_validation": "not_established",
    }
    return report, raw, binding


def export_retained_quality_raw(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
    split: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Copy the exact first RAW frame of a pinned quality case to a dataset.

    The selected source case must have a stored ``raw_dn_frame``. This bounded
    export does not infer labels, recompute captures, or equate a new seed with
    the retained capture. It emits one unlabeled case with its original seed.
    """
    _, case, raw, source_binding = load_retained_quality_frame(
        source_report_path,
        source_arrays_path,
        source_quality_job_id=source_quality_job_id,
        case_index=case_index,
    )
    return export_retained_raw_array(
        raw,
        source_binding=source_binding,
        seed=case["frame_seeds"][0],
        scene=case["scene"],
        split=split,
        output_dir=output_dir,
    )


def export_retained_raw_array(
    raw: np.ndarray,
    *,
    source_binding: dict[str, Any],
    seed: int,
    scene: str,
    split: str,
    output_dir: str | Path,
    annotation_document: dict[str, Any] | None = None,
    annotation_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Export one already captured integer mosaic with its immutable lineage."""
    if split not in _SPLITS:
        raise ValueError(f"split must be one of {sorted(_SPLITS)}.")
    if (
        not isinstance(raw, np.ndarray)
        or raw.ndim != 2
        or raw.dtype != np.dtype("int32")
        or not isinstance(source_binding, dict)
        or source_binding.get("source_raw_content_sha256") != _sha256_array(raw)
        or type(seed) is not int
        or not 0 <= seed < 2**32
        or not isinstance(scene, str)
        or not scene
    ):
        raise ValueError("Retained RAW, seed, scene or source hash is invalid.")
    raw = np.ascontiguousarray(raw)
    original_content_hash = source_binding["source_raw_content_sha256"]
    annotated = annotation_document is not None
    if annotated:
        annotation_document = validate_raw_annotation(
            annotation_document, source_binding=source_binding, raw_shape=list(raw.shape)
        )
        annotation_bytes = canonical(annotation_document).encode("utf-8")
        if hashlib.sha256(annotation_bytes).hexdigest() != annotation_artifact_sha256:
            raise ValueError("Annotation artifact SHA differs from canonical source bytes.")
    elif annotation_artifact_sha256 is not None:
        raise ValueError("Annotation artifact SHA requires a supplied annotation document.")
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Retained RAW dataset output_dir is not empty: {destination}")

    case_id = "retained_0000"
    raw_relative = f"raw/{case_id}.npz"
    labels_relative = f"labels/{case_id}.json"
    annotation_relative = f"annotations/{case_id}-source.json" if annotated else None
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "raw").mkdir()
    (destination / "labels").mkdir()
    if annotated:
        (destination / "annotations").mkdir()
    raw_path = destination / raw_relative
    # The quality artifact stores no separate sensor_digital stage. An empty
    # array is truthful and keeps the established dataset reader contract.
    digital_unavailable = np.empty((0,), dtype=np.int32)
    _write_npz_deterministic(
        raw_path,
        raw=raw,
        sensor_digital=digital_unavailable,
        seed=np.asarray([seed], dtype=np.int64),
    )
    with np.load(raw_path, allow_pickle=False) as exported:
        exported_raw = np.asarray(exported["raw"])
        if (
            exported_raw.dtype != raw.dtype
            or exported_raw.shape != raw.shape
            or exported_raw.tobytes(order="C") != raw.tobytes(order="C")
        ):
            raise ValueError("Exported RAW differs from the retained source bytes.")
    exported_content_hash = _sha256_array(exported_raw)
    if exported_content_hash != original_content_hash:
        raise ValueError("Exported RAW content hash differs from the retained source.")
    label_path = destination / labels_relative
    if annotated:
        (destination / annotation_relative).write_bytes(annotation_bytes)
    class_map = (
        {label: index for index, label in enumerate(sorted({
            item["label"] for item in annotation_document["objects"]
        }))}
        if annotated else {}
    )
    labels = (
        {
            "objects": [
                {**item, "class_id": class_map[item["label"]]}
                for item in annotation_document["objects"]
            ],
            "masks": [],
            "source": "user_provided_unverified",
            "image_size_rc": list(raw.shape),
            "coordinate_frame": RAW_ANNOTATION_COORDINATES,
            "provenance": annotation_document["provenance"],
            "source_annotation_sha256": annotation_artifact_sha256,
        }
        if annotated else
        {"objects": [], "masks": [], "source": "unlabeled"}
    )
    _write_json(
        label_path,
        {
            "schema_version": "camerae2e_dataset_labels_v1",
            "case_id": case_id,
            "labels": labels,
            "note": (
                "User-supplied RAW-pixel annotations; origin, completeness and independence "
                "are unverified. No detector score or product performance is claimed."
                if annotated else
                "No ground truth or model weights were supplied; no perception score is claimed."
            ),
        },
    )
    record = {
        "case_id": case_id,
        "split": split,
        "seed": seed,
        "scenario_name": scene,
        "raw": raw_relative,
        "rgb": None,
        "raw_tiff": None,
        "stage_outputs": None,
        "container": None,
        "uncertainty": None,
        "labels": labels_relative,
        "raw_shape": list(raw.shape),
        "raw_dtype": str(raw.dtype),
        "sensor_digital_shape": [0],
        "sensor_digital_dtype": "int32",
        "raw_sha256": f"sha256:{_sha256(raw_path)}",
        "raw_content_sha256": exported_content_hash,
        "sensor_digital_content_sha256": _sha256_array(digital_unavailable),
        "rgb_sha256": None,
        "raw_tiff_sha256": None,
        "stage_outputs_sha256": None,
        "container_sha256": None,
        "uncertainty_sha256": None,
        "labels_sha256": f"sha256:{_sha256(label_path)}",
        "source_binding": source_binding,
        "label_summary": {
            "source": "user_provided_unverified" if annotated else "unlabeled",
            "object_count": len(labels["objects"]),
            "mask_count": 0,
        },
        "faca_metrics": {},
        "parameter_lineage": [],
    }
    if annotated:
        record["source_annotation"] = annotation_relative
        record["source_annotation_sha256"] = f"sha256:{annotation_artifact_sha256}"
    metadata_path = destination / "metadata.jsonl"
    metadata_path.write_text(
        json.dumps(record, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": "camerae2e_dataset_manifest_v1",
        "dataset_root": str(destination),
        "case_count": 1,
        "splits": {split: 1},
        "split_policy": {
            "assignment_unit": "this_export_only",
            "requested": split,
            "cross_export_exclusivity_enforced": False,
            "holdout_independence_claimed": False,
        },
        "format": {
            "raw": "deterministic compressed NumPy NPZ with original int32 raw values",
            "sensor_digital": "empty: a separate stage was not retained by the quality job",
            "rgb": "not exported",
            "labels": (
                "user-provided source RAW mosaic pixel boxes, unverified"
                if annotated else "explicitly unlabeled"
            ),
            "dng": "not emitted",
        },
        "truth_boundary": (
            "One exact retained RAW frame, not a new capture or a new ISP result. "
            + (
                "User-provided boxes are bound to source RAW pixels and bytes, but their "
                "truth, completeness, origin and independence are not verified. "
                if annotated else "No ground truth or labels were supplied. "
            )
            + "No detector, perception metric, physical validation or cross-export "
            "holdout independence is established. Split is a per-export annotation only."
        ),
        "source_binding": source_binding,
        "integrity": {
            "hash": "sha256",
            "source_raw_content_sha256": original_content_hash,
            "exported_raw_content_sha256": exported_content_hash,
            "raw_values_byte_equal": True,
        },
        "records": [record],
    }
    if annotated:
        manifest["annotation_artifact_sha256"] = annotation_artifact_sha256
        manifest["annotation_provenance"] = annotation_document["provenance"]
        manifest["class_map"] = class_map
    manifest_path = destination / "manifest.json"
    _write_json(manifest_path, manifest)
    validation = camerae2e_dataset_validate(manifest_path)
    if not validation["ok"] or validation["issue_count"]:
        raise ValueError(f"Existing CameraE2E dataset validator failed: {validation['issues']}")
    perception_index = camerae2e_dataset_export_perception_index(
        manifest_path,
        output_dir=destination / "perception_index",
        formats=("raw_manifest",),
        class_map=class_map,
    )
    return {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": source_binding,
        "raw_values_byte_equal": True,
        "raw_content_sha256": exported_content_hash,
        "raw_shape": list(raw.shape),
        "raw_dtype": str(raw.dtype),
        "split": split,
        "labels": "user_provided_unverified" if annotated else "unlabeled",
        "annotation_artifact_sha256": annotation_artifact_sha256,
        "class_map": class_map,
        "perception_metric": {
            "available": False,
            "reason": (
                "User-supplied annotation truth is unverified; no predictions or detector weights."
                if annotated else "No ground-truth labels or detector model weights."
            ),
        },
        "dataset_validation": validation,
        "raw_training_index": {
            "row_count": perception_index["outputs"]["raw_manifest"]["row_count"],
            "warning_count": perception_index["warning_count"],
        },
        "files": {
            "manifest": str(manifest_path),
            "metadata": str(metadata_path),
            "raw": str(raw_path),
            "labels": str(label_path),
            "source_annotation": (
                str(destination / annotation_relative) if annotated else None
            ),
            "raw_manifest": perception_index["outputs"]["raw_manifest"]["path"],
        },
    }


def make_portable_bundle(dataset_dir: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Package a validated dataset with relative paths for offline extraction."""
    source = Path(dataset_dir).expanduser().resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["dataset_root"] = "."
    source_description = (
        "approximate geometric Lens x Sensor job"
        if "source_lens_sensor_job_id" in manifest["source_binding"]
        else "saved sensor quality job"
    )
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    members = {
        "manifest.json": (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
        "metadata.jsonl": (source / "metadata.jsonl").read_bytes(),
        "raw/retained_0000.npz": (source / "raw/retained_0000.npz").read_bytes(),
        "labels/retained_0000.json": (source / "labels/retained_0000.json").read_bytes(),
        "README.txt": (
            f"This is one exact retained CameraE2E RAW frame from a {source_description}. "
            + (
                "It includes user-supplied RAW-pixel boxes, whose truth, origin and "
                "independence were not independently verified. "
                if manifest.get("annotation_artifact_sha256") else
                "It is unlabeled. "
            )
            + "There is no detector score or new capture. Extract "
            "the ZIP, change into the extracted directory, then call "
            "pyisetcam.camerae2e_dataset_validate('manifest.json') or "
            "camerae2e_dataset_export_perception_index('manifest.json', "
            "formats=('raw_manifest',)).\n"
        ).encode(),
    }
    if manifest.get("annotation_artifact_sha256"):
        member = "annotations/retained_0000-source.json"
        content = (source / member).read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["annotation_artifact_sha256"]:
            raise ValueError("Source annotation bytes differ from the manifest artifact SHA.")
        members[member] = content
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return {"path": str(target), "sha256": _sha256(target), "member_names": sorted(members)}

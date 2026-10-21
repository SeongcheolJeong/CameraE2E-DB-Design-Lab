"""Image-aware aggregation of saved Lens × Sensor ISP prediction evidence.

The current Lens RAW jobs offer only tiny built-in synthetic scene families.
This adapter can calculate a bounded numerical diagnostic over distinct saved
families, but it cannot turn those fixtures into a representative benchmark.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from pyisetcam.task_perception import (
    dataset_mean_average_precision,
    task_perception_config,
)

from .lens_sensor_isp_perception import (
    _document,
    evaluate_lens_sensor_isp_predictions,
)
from .registry import IntegrityError, digest
from .saved_sensor_perception import _boxes, _label_set

SCHEMA = "camerae2e_lens_sensor_isp_prediction_cohort_v1"
_MAX_CASES = 2


def _verified_case(service: Any, identity: str) -> dict[str, Any]:
    """Recheck a persisted single-frame Job and every image/box artifact."""
    from .lens_sensor_isp_perception_job import (
        SCHEMA as SINGLE_SCHEMA,
    )
    from .lens_sensor_isp_perception_job import (
        _inputs,
        _source_job,
        get,
    )

    job = get(service, identity)
    if (
        job.get("status") != "succeeded"
        or job.get("specification", {}).get("schema") != SINGLE_SCHEMA
    ):
        raise ValueError("Cohort inputs must be completed single-frame ISP prediction Jobs.")
    spec = job["specification"]
    raw_job, isp_job, paths = _source_job(service, spec)
    evidence_paths = _inputs(service, spec)
    evaluation = evaluate_lens_sensor_isp_predictions(
        lens_raw_job=raw_job,
        lens_isp_job=isp_job,
        **paths,
        **evidence_paths,
        iou_threshold=spec["iou_threshold"],
    )
    summary = job["result"]["summary"]
    if (
        digest(evaluation) != job["evaluation_sha256"]
        or summary.get("evaluation") != evaluation
    ):
        raise IntegrityError("Saved single-frame evaluation differs from source artifacts.")
    annotations = _document(evidence_paths["annotation_path"])
    predictions = _document(evidence_paths["prediction_path"])
    model = _document(evidence_paths["model_manifest_path"])
    binding = evaluation["image_binding"]
    if (
        annotations.get("image_binding") != binding
        or predictions.get("image_binding") != binding
        or hashlib.sha256(evidence_paths["annotation_path"].read_bytes()).hexdigest()
        != evaluation["annotation_provenance"]["artifact_sha256"]
        or hashlib.sha256(evidence_paths["prediction_path"].read_bytes()).hexdigest()
        != evaluation["prediction_provenance"]["artifact_sha256"]
        or hashlib.sha256(evidence_paths["model_manifest_path"].read_bytes()).hexdigest()
        != evaluation["model_provenance"]["manifest_sha256"]
    ):
        raise IntegrityError("Cohort boxes or model do not bind to the saved RGB frame.")
    shape = tuple(binding["shape"][:2])
    labels = _label_set(annotations["label_set"])
    if (
        _label_set(model.get("label_set")) != labels
        or model.get("checkpoint_sha256")
        != evaluation["model_provenance"]["checkpoint_sha256"]
    ):
        raise IntegrityError("Cohort model manifest differs from saved label contract.")
    truth = _boxes(annotations["objects"], set(labels["labels"]), shape, predictions=False)
    detections = _boxes(
        predictions["detections"], set(labels["labels"]), shape, predictions=True
    )
    scene = raw_job["result"]["summary"]["preview"]["scene"]
    if (
        not isinstance(scene, dict)
        or scene.get("kind") not in {"uniform_d65", "slanted_edge_d65"}
    ):
        raise ValueError("The saved Lens RAW scene family is not a supported cohort source.")
    return {
        "evaluation_job_id": identity,
        "lens_raw_job_id": raw_job["id"],
        "lens_isp_job_id": isp_job["id"],
        "raw_content_sha256": binding["source_raw_content_sha256"],
        "rgb_array_sha256": binding["array_sha256"],
        "rgb_shape": binding["shape"],
        "scene_family": scene["kind"],
        "scene_asset_identity": None,
        "annotation_artifact_sha256": evaluation["annotation_provenance"]["artifact_sha256"],
        "prediction_artifact_sha256": evaluation["prediction_provenance"]["artifact_sha256"],
        "annotation_source": evaluation["annotation_provenance"]["ground_truth_source"],
        "model": {
            "backend": evaluation["model_provenance"]["backend"],
            "checkpoint_sha256": evaluation["model_provenance"]["checkpoint_sha256"],
            "label_set": labels,
            "model_execution_verified": evaluation["prediction_provenance"][
                "model_execution_verified"
            ],
        },
        "truth": truth,
        "detections": detections,
        "single_frame_result_sha256": digest(job["result"]),
    }


def assess_saved_lens_sensor_isp_perception_cohort(
    service: Any,
    evaluation_job_ids: Sequence[str],
    *,
    iou_thresholds: tuple[float, ...] = (0.5, 0.75),
) -> dict[str, Any]:
    """Calculate an image-aware diagnostic from fully revalidated saved Jobs.

    A built-in scene family may appear once. This rejects camera variations of
    the same synthetic input as extra independent images. A model checkpoint
    and label-set contract must match across all cases. Numeric AP remains a
    supplier-label diagnostic because all current source scenes are 8–32 px
    built-in patterns, their labels are unverified, and inference is external.
    """
    if (
        not isinstance(evaluation_job_ids, Sequence)
        or isinstance(evaluation_job_ids, (str, bytes))
        or len(evaluation_job_ids) != _MAX_CASES
        or any(not isinstance(identity, str) or not identity.strip()
               for identity in evaluation_job_ids)
        or len(set(evaluation_job_ids)) != len(evaluation_job_ids)
    ):
        raise ValueError("Choose exactly 2 distinct completed ISP prediction Job IDs.")
    cases = [_verified_case(service, identity) for identity in evaluation_job_ids]
    if len({case["scene_family"] for case in cases}) != len(cases):
        raise ValueError(
            "A synthetic scene family cannot count as multiple independent cohort images."
        )
    for key in (
        "raw_content_sha256", "rgb_array_sha256", "annotation_artifact_sha256",
        "prediction_artifact_sha256",
    ):
        if len({case[key] for case in cases}) != len(cases):
            raise ValueError(f"Cohort cases reuse exact {key} evidence.")
    model = cases[0]["model"]
    for case in cases[1:]:
        if case["model"] != model:
            raise ValueError("Cohort cases require one model checkpoint and label set.")
    predictions = {
        case["evaluation_job_id"]: case["detections"] for case in cases
    }
    truth = {case["evaluation_job_id"]: case["truth"] for case in cases}
    metric = dataset_mean_average_precision(
        predictions,
        truth,
        task_perception_config(score_threshold=0.0, max_detections=256),
        label_set=model["label_set"]["labels"],
        iou_thresholds=iou_thresholds,
    )
    public_cases = [
        {key: value for key, value in case.items() if key not in {"truth", "detections"}}
        | {
            "ground_truth_box_count": len(case["truth"]),
            "prediction_box_count": len(case["detections"]),
        }
        for case in cases
    ]
    fixture_used = any(
        case["annotation_source"]["source_kind"] == "synthetic_contract_fixture"
        for case in cases
    )
    product_reasons = [
        "Built-in 8–32 pixel synthetic scenes lack independent scene asset identities.",
        "Annotation correctness and independence are supplier declarations.",
        "Prediction-to-model execution is not verified for supplied artifacts.",
        "No independent held-out capture or representative negative-image set exists.",
        "Lens radiometry and physical camera accuracy are not validated.",
    ]
    if fixture_used:
        product_reasons.append(
            "Synthetic contract fixture boxes have no verified semantic object truth."
        )
    return {
        "schema": SCHEMA,
        "status": "completed",
        "evaluation_kind": "image_aware_supplied_prediction_diagnostic",
        "cases": public_cases,
        "diagnostic_map": {"available": True, "value": metric},
        "product_map": {
            "available": False,
            "reasons": product_reasons,
        },
        "verification": {
            "saved_raw_isp_prediction_artifacts": "revalidated_by_hash_and_array_binding",
            "image_id_matching": "enforced_by_pyisetcam_dataset_mean_average_precision",
            "ground_truth_independence": (
                "semantic_ground_truth_absent_synthetic_contract_fixture"
                if fixture_used else "supplier_declared_not_independently_verified"
            ),
            "model_execution": "not_performed_by_this_evaluation",
            "physical_validation": "not_established",
        },
    }

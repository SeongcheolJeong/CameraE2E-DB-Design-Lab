"""A saved-image cohort cannot cross-match boxes or claim product mAP."""

from __future__ import annotations

from copy import deepcopy

import pytest

from camera_platform import lens_sensor_isp_perception_cohort as cohort


def _case(identity: str, scene: str, *, offset: int) -> dict:
    box = {"bbox": [1.0, 1.0, 4.0, 4.0], "label": "target"}
    return {
        "evaluation_job_id": identity,
        "lens_raw_job_id": f"raw-{identity}",
        "lens_isp_job_id": f"isp-{identity}",
        "raw_content_sha256": str(offset) * 64,
        "rgb_array_sha256": str(offset + 2) * 64,
        "rgb_shape": [8, 8, 3],
        "scene_family": scene,
        "scene_asset_identity": None,
        "annotation_artifact_sha256": str(offset + 4) * 64,
        "prediction_artifact_sha256": str(offset + 6) * 64,
        "annotation_source": {
            "source_kind": "human_annotation",
            "source_dataset_id": f"fixture-{identity}",
        },
        "model": {
            "backend": "external_predictions",
            "checkpoint_sha256": "a" * 64,
            "label_set": {"id": "target-set", "version": "1", "labels": ["target"]},
            "model_execution_verified": False,
        },
        "truth": [box],
        "detections": [{**box, "score": 0.9}],
        "single_frame_result_sha256": str(offset + 8) * 64,
    }


def test_aggregator_uses_distinct_image_ids_and_keeps_product_map_blocked(monkeypatch):
    cases = {
        "a": _case("a", "uniform_d65", offset=1),
        "b": _case("b", "slanted_edge_d65", offset=2),
    }
    monkeypatch.setattr(cohort, "_verified_case", lambda _service, identity: cases[identity])
    report = cohort.assess_saved_lens_sensor_isp_perception_cohort(
        object(), ["a", "b"], iou_thresholds=(0.5,)
    )
    assert report["diagnostic_map"]["value"]["map"] == 1.0
    assert report["diagnostic_map"]["value"]["image_id_matching_enforced"] is True
    assert report["product_map"]["available"] is False
    assert report["verification"]["physical_validation"] == "not_established"
    assert all("truth" not in case and "detections" not in case for case in report["cases"])


def test_aggregator_marks_contract_fixture_as_absent_semantic_truth(monkeypatch):
    cases = {
        "a": _case("a", "uniform_d65", offset=1),
        "b": _case("b", "slanted_edge_d65", offset=2),
    }
    cases["a"]["annotation_source"]["source_kind"] = "synthetic_contract_fixture"
    monkeypatch.setattr(cohort, "_verified_case", lambda _service, identity: cases[identity])

    report = cohort.assess_saved_lens_sensor_isp_perception_cohort(object(), ["a", "b"])
    assert report["diagnostic_map"]["available"] is True
    assert report["product_map"]["available"] is False
    assert report["verification"]["ground_truth_independence"] == (
        "semantic_ground_truth_absent_synthetic_contract_fixture"
    )
    assert any(
        "no verified semantic object truth" in reason
        for reason in report["product_map"]["reasons"]
    )
    assert report["cases"][0]["annotation_source"]["source_kind"] == (
        "synthetic_contract_fixture"
    )


def test_aggregator_rejects_same_synthetic_scene_and_reused_evidence(monkeypatch):
    cases = {
        "a": _case("a", "uniform_d65", offset=1),
        "b": _case("b", "uniform_d65", offset=2),
    }
    monkeypatch.setattr(cohort, "_verified_case", lambda _service, identity: cases[identity])
    with pytest.raises(ValueError, match="exactly 2"):
        cohort.assess_saved_lens_sensor_isp_perception_cohort(
            object(), ["a", "b", "c"]
        )
    with pytest.raises(ValueError, match="synthetic scene family"):
        cohort.assess_saved_lens_sensor_isp_perception_cohort(object(), ["a", "b"])
    cases["b"]["scene_family"] = "slanted_edge_d65"
    cases["b"]["raw_content_sha256"] = cases["a"]["raw_content_sha256"]
    with pytest.raises(ValueError, match="raw_content_sha256"):
        cohort.assess_saved_lens_sensor_isp_perception_cohort(object(), ["a", "b"])


def test_aggregator_requires_one_model_and_evaluable_class_coverage(monkeypatch):
    cases = {
        "a": _case("a", "uniform_d65", offset=1),
        "b": _case("b", "slanted_edge_d65", offset=2),
    }
    monkeypatch.setattr(cohort, "_verified_case", lambda _service, identity: cases[identity])
    cases["b"]["model"] = deepcopy(cases["b"]["model"])
    cases["b"]["model"]["checkpoint_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="one model checkpoint"):
        cohort.assess_saved_lens_sensor_isp_perception_cohort(object(), ["a", "b"])
    cases["b"]["model"] = deepcopy(cases["a"]["model"])
    cases["a"]["model"]["label_set"]["labels"] = ["target", "absent"]
    cases["b"]["model"] = deepcopy(cases["a"]["model"])
    with pytest.raises(ValueError, match="No ground-truth objects"):
        cohort.assess_saved_lens_sensor_isp_perception_cohort(object(), ["a", "b"])

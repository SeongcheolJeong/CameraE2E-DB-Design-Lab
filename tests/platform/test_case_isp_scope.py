"""Case boundary tests over real RAW acquisition; Study execution is isolated.

The completed Study reader is stubbed only to exercise case ownership without
duplicating the separate ISP Study integration suite. Camera RAW generation and
ordinary capture import/observation verification use their actual product paths.
"""

from copy import deepcopy

import pytest
import test_isp_calibration as calibration_tests

from camera_platform import imaging, isp_studies, observations
from camera_platform.case_reviews import create_case_review, get_case_review
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.registry import IntegrityError

source = calibration_tests.source


def acquisition(source, *, exposure_ms=4):
    service, original = source
    assembly_id = original["source_snapshot"]["assembly_id"]
    fixture = imaging.fixture_template(service, assembly_id)
    fixture.update(shape=[16, 16], pattern="flat")
    scene = imaging.create_fixture(service, fixture)
    test = service.create_test(imaging.spatial_test_template(service, assembly_id, scene["id"]))
    queued = service.submit_run(
        {
            "assembly_id": assembly_id,
            "test_id": test["id"],
            "scene_id": scene["id"],
            "plugin_id": "spatial_scene_v1",
            "parameters": {"frames": 4, "seed": 275, "exposure_ms": exposure_ms},
        }
    )
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    return service, imaging.replay_source(service, "run", run["id"])


def case_for_source(service, raw, monkeypatch):
    original = raw["source_snapshot"]
    study = service.registry.put(
        "isp_studies",
        {
            "specification": {"calibration_id": "scope-fixture-calibration"},
            "datasets": [
                {
                    "key": "flat",
                    "assembly_id": original["assembly_id"],
                    "test_id": original["test_id"],
                    "source": deepcopy(raw),
                }
            ],
        },
    )
    monkeypatch.setattr(
        isp_studies._EvidenceReader,
        "export",
        lambda _reader, identity: {
            "study": service.registry.get("isp_studies", identity),
            "analysis": {"status": "complete"},
        },
    )
    draft = {
        "request_id": "case-isp-scope-fixture",
        "name": "Same fixed RAW question",
        "question": "Does this exact acquisition answer the fixed requirement?",
        "requirements": [
            {
                "key": "noise",
                "description": "Noise in the original conditions",
                "test_id": original["test_id"],
            }
        ],
        "assembly_ids": [original["assembly_id"]],
        "evidence": [
            {
                "kind": "isp_studies",
                "id": study["id"],
                "requirement_keys": ["noise"],
                "dataset_requirements": {"flat": ["noise"]},
            }
        ],
        "decision": {"status": "investigate", "rationale": "Numerical evidence only"},
    }
    return draft


def test_actual_spatial_exposure_override_cannot_be_wrapped_as_original_test_evidence(
    source, monkeypatch
):
    service, raw = acquisition(source, exposure_ms=8)
    registered = service.registry.get("tests", raw["source_snapshot"]["test_id"])
    assert registered["conditions"]["exposure_s"]["value"] == 0.004
    assert raw["conditions"]["exposure_s"]["value"] == 0.008
    assert raw["raw_shape"] == [4, 16, 16]
    draft = case_for_source(service, raw, monkeypatch)
    with pytest.raises(ValueError, match="conditions differ"):
        create_case_review(service, draft)
    assert not service.registry.list("case_reviews")


def test_matching_actual_run_remains_valid_case_evidence(source, monkeypatch):
    service, raw = acquisition(source)
    draft = case_for_source(service, raw, monkeypatch)
    review = create_case_review(service, draft)
    assert get_case_review(service, review["id"]) == review
    assert review["snapshots"]["evidence"][0]["dataset_requirements"] == {"flat": ["noise"]}


def test_run_snapshot_cannot_be_replaced_with_same_test_id(source, monkeypatch):
    service, raw = acquisition(source)
    altered = deepcopy(raw)
    altered["source_snapshot"]["name"] = "Different source contents"
    draft = case_for_source(service, altered, monkeypatch)
    with pytest.raises(IntegrityError, match="exact frozen snapshot"):
        create_case_review(service, draft)


def captured_source(source):
    service, raw = acquisition(source)
    original = raw["source_snapshot"]
    base = next(t for t in service.registry.list("tests") if t["metric_id"] == "raw.channel_ratio")
    payload = {k: deepcopy(v) for k, v in base.items() if k in SpecificationContract.model_fields}
    payload.update(
        name="Actual native RAW observation scope",
        revision="scope",
        conditions=deepcopy(raw["conditions"]),
        included_effects=original["test_snapshot"]["included_effects"],
    )
    test = service.create_test(payload)
    metadata = {
        "name": "Explicit simulated native capture",
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "fixture://case-isp-scope-capture",
        "license": "MIT",
        "method_and_version": "scope-import:1",
        "physical_instance": None,
        "parents": [],
        "test_id": test["id"],
        "assembly_id": original["assembly_id"],
        "dataset_ids": ["case-scope-native-acquisition"],
        "instrument": {},
        "observation": {
            "raw_format": raw["raw_format"],
            "geometry": raw["geometry"],
            "conditions": raw["conditions"],
            "included_effects": test["included_effects"],
            "rois": [{"id": "center", "field_radius": 0.0, "box": [4, 4, 8, 8]}],
        },
    }
    imported = observations.import_capture(
        service, metadata, service.registry.blob_path(raw["raw_sha256"]).read_bytes()
    )
    return service, imaging.replay_source(service, "capture", imported["capture"]["id"])


def test_actual_capture_reuses_preserved_observation_contract(source, monkeypatch):
    service, raw = captured_source(source)
    draft = case_for_source(service, raw, monkeypatch)
    review = create_case_review(service, draft)
    assert get_case_review(service, review["id"]) == review


def test_capture_cannot_relabel_test_conditions_despite_matching_snapshot(source, monkeypatch):
    service, raw = captured_source(source)
    capture = raw["source_snapshot"]
    fixed = service.registry.get("tests", capture["test_id"])
    changed = {
        k: deepcopy(v) for k, v in fixed.items() if k not in {"id", "created_at", "content_hash"}
    }
    changed["conditions"]["exposure_s"]["value"] = 0.008
    changed_test = service.registry.put("tests", changed)
    # Deliberately malformed registry fixture tests the case boundary, not import.
    changed_capture = {
        k: deepcopy(v) for k, v in capture.items() if k not in {"id", "created_at", "content_hash"}
    }
    changed_capture["test_id"] = changed_test["id"]
    changed_capture["measurement_contract"]["test_id"] = changed_test["id"]
    relabeled = service.registry.put("captures", changed_capture)
    altered = imaging.replay_source(service, "capture", relabeled["id"])
    draft = case_for_source(service, altered, monkeypatch)
    with pytest.raises(ValueError, match="fixed case Test"):
        create_case_review(service, draft)

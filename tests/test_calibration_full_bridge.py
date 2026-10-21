"""All E-SFR edges survive legacy admission and immutable export upgrades."""
from copy import deepcopy

import pytest

from camera_platform import calibration_lab as lab
from camera_platform import calibration_lab_contracts as contracts
from camera_platform import esfr_bridge, esfr_lab
from camera_platform.registry import IntegrityError, canonical, digest
from test_esfr_bridge import fixture_run


def full_chart_run(tmp_path):
    service, first_id, captures = fixture_run(tmp_path)
    old = service.registry.get_run(first_id)
    observations = deepcopy(old["result"]["summary"]["observations"])
    for observation in observations:
        template = observation["rois"][0]
        observation["rois"] = [
            {**deepcopy(template), "id": f"edge-{index:03d}",
             "box": [index // 14, index % 14, 32, 40]}
            for index in range(112)
        ]
    job = service.registry.submit(
        {"job_kind": esfr_lab.KIND, "plugin_id": "camera_esfr_validation_v1",
         "name": "112 native edge fixture", "specification": old["specification"],
         "capture_snapshots": captures, "source_context": None},
        "full-edge-fixture", force=True,
    )
    assert service.registry.claim()["id"] == job["id"]
    summary = {"run_id": job["id"], "observations": observations}
    artifact = service.registry.store_blob(canonical(summary).encode(), "result.json", "application/json")
    service.registry.finish(job["id"], result={"summary": summary, "artifacts": [artifact]})
    return service, job["id"]


def test_112_edge_export_and_api_training_validation_submission(tmp_path, monkeypatch):
    service, owner = full_chart_run(tmp_path)
    exported = esfr_bridge.export(service, owner)
    assert exported["status"] == "ready_for_mtf_study"
    assert exported["field_position_count"] == 112
    assert len(exported["training_ids"]) == len(exported["validation_ids"]) == 112
    assert len(exported["measurement_ids"]) == 224
    assert exported["validation_request"] == {"measurement_ids": exported["validation_ids"]}
    records = [lab._measurement(service, identity) for identity in exported["measurement_ids"]]
    assert all(m["analysis"]["rois"] == [m["provenance"]["native_roi"]] for m in records)
    assert len({tuple(m["observations"]["field_xy"]) for m in records[:112]}) == 112
    # These are admission tests; isolate source fingerprints from concurrent
    # implementation edits without weakening the production source guard.
    service.loaded_environment = {"fixture": "full-edge-api"}
    monkeypatch.setattr(lab, "_source_code", lambda: service.loaded_environment)
    training = lab.submit(service, {**exported["study_request"], "fit_method": "analytic_curve"})
    assert len(training["measurement_snapshots"]) == 112
    assert training["measurement_resource_budget"] == exported["resource_budget"]["training"]
    # Freeze a complete candidate to exercise the real validation admission and
    # lineage guard, without duplicating engine numerical tests in this API test.
    assert service.registry.claim()["id"] == training["id"]
    profile = training["base_profile"]
    result_summary = {"candidate_profile": profile, "candidate_sha256": digest(profile)}
    artifact = service.registry.store_blob(canonical(result_summary).encode(), "calibration-result.json", "application/json")
    service.registry.finish(training["id"], result={"summary": result_summary, "artifacts": [artifact]})
    validation = lab.validate(service, training["id"], exported["validation_request"])
    assert len(validation["measurement_snapshots"]) == 112
    assert validation["measurement_resource_budget"] == exported["resource_budget"]["validation"]
    assert not set(validation["validation_lineage"]) & set(training["development_lineage"])
    assert validation["candidate_sha256"] == digest(profile)
    assert lab.validate(service, training["id"], exported["validation_request"])["id"] == validation["id"]


def test_legacy_blocked_export_refresh_is_append_only_and_retains_all_curve_ids(tmp_path):
    service, owner = full_chart_run(tmp_path)
    original = esfr_bridge.export(service, owner)
    old_document = {k: deepcopy(v) for k, v in original.items()
                    if k not in {"id", "created_at", "content_hash", "readiness_version", "resource_budget", "field_position_count", "validation_request"}}
    old_document.update(status="export_only_limits", study_request=None,
                        study_blockers=["Legacy supports 64 observations / 25 positions"])
    legacy = service.registry.put(esfr_bridge.EXPORTS, old_document)
    prior_records = service.registry.list(lab.MEASUREMENTS)
    refreshed = esfr_bridge.export(service, owner)
    assert refreshed["id"] != legacy["id"]
    assert refreshed["supersedes_export_id"] == legacy["id"]
    assert refreshed["supersedes_export_sha256"] == legacy["content_hash"]
    assert refreshed["measurement_ids"] == legacy["measurement_ids"]
    assert refreshed["campaign_id"] == legacy["campaign_id"]
    assert refreshed["status"] == "ready_for_mtf_study"
    assert service.registry.get(esfr_bridge.EXPORTS, legacy["id"]) == legacy
    assert service.registry.list(lab.MEASUREMENTS) == prior_records
    assert len(service.registry.list(lab.CAMPAIGNS)) == 1
    assert esfr_bridge.export(service, owner) == refreshed


def test_api_uses_resource_budget_not_edge_count(tmp_path, monkeypatch):
    service, owner = full_chart_run(tmp_path)
    exported = esfr_bridge.export(service, owner)
    campaign = service.registry.get(lab.CAMPAIGNS, exported["campaign_id"])
    monkeypatch.setattr(contracts, "MAX_MEASUREMENT_BYTES", 1)
    with pytest.raises(ValueError, match="byte|MiB|budget"):
        lab._resolve_measurements(service, campaign, exported["training_ids"], {"training"})


def test_refresh_refuses_original_byte_tampering(tmp_path):
    service, owner = full_chart_run(tmp_path)
    exported = esfr_bridge.export(service, owner)
    record = service.registry.get(lab.MEASUREMENTS, exported["training_ids"][0])
    artifact = next(a for a in record["artifacts"] if a["name"] == "esfr-gr-sfr.csv")
    binding = service.registry.artifact_binding(artifact["sha256"], owner_kind=lab.MEASUREMENTS, owner_id=record["id"])
    binding["path"].write_bytes(b"changed curve")
    with pytest.raises(IntegrityError):
        esfr_bridge.export(service, owner)

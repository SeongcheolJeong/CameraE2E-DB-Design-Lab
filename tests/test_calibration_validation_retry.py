"""Only repaired-source retries may reuse an exposed frozen candidate holdout."""
from copy import deepcopy

import pytest

from camera_platform import calibration_lab as lab, esfr_bridge
from camera_platform.registry import ConflictError, canonical, digest
from test_esfr_bridge import fixture_run


def complete_candidate(service, exported, *, sigma=None):
    row = lab.submit(service, {**exported["study_request"], "fit_method": "analytic_curve"})
    assert service.registry.claim()["id"] == row["id"]
    profile = deepcopy(row["base_profile"])
    if sigma is not None:
        profile["blur"]["field_nodes"][0].update(sigma_x_px=sigma, sigma_y_px=sigma)
    summary = {"candidate_profile": profile, "candidate_sha256": digest(profile)}
    artifact = service.registry.store_blob(canonical(summary).encode(), "calibration-result.json", "application/json")
    service.registry.finish(row["id"], result={"summary": summary, "artifacts": [artifact]})
    return lab.get(service, row["id"])


def failed_validation(tmp_path, monkeypatch):
    service, source, _ = fixture_run(tmp_path, repeats=2)
    exported = esfr_bridge.export(service, source, aggregate_repeats=False)
    service.loaded_environment = {"revision": "source-before-repair"}
    monkeypatch.setattr(lab, "_source_code", lambda: deepcopy(service.loaded_environment))
    parent = complete_candidate(service, exported)
    validation = lab.validate(service, parent["id"], exported["validation_request"])
    assert service.registry.claim()["id"] == validation["id"]
    service.registry.finish(validation["id"], error="Artifact publication budget exceeded")
    return service, exported, parent, lab.get(service, validation["id"])


def test_failed_same_source_stays_idempotent_and_repaired_source_retries_exact_candidate(tmp_path, monkeypatch):
    service, exported, parent, failed = failed_validation(tmp_path, monkeypatch)
    assert lab.validate(service, parent["id"], exported["validation_request"])["id"] == failed["id"]
    service.loaded_environment = {"revision": "source-after-repair"}
    retry = lab.validate(service, parent["id"], exported["validation_request"])
    assert retry["id"] != failed["id"]
    assert retry["retry_of_validation_ids"] == [failed["id"]]
    for key in ("candidate_sha256", "candidate_profile", "base_profile", "parent_study_id",
                "parent_result_sha256", "measurement_snapshots", "validation_lineage", "development_lineage"):
        assert retry[key] == failed[key]
    assert retry["specification"] == failed["specification"]
    assert retry["source_code"] == service.loaded_environment
    assert lab.get(service, failed["id"]) == failed
    assert lab.validate(service, parent["id"], exported["validation_request"])["id"] == retry["id"]
    assert service.registry.claim()["id"] == retry["id"]
    service.registry.finish(retry["id"], error="Another same-source failure")
    assert lab.validate(service, parent["id"], exported["validation_request"])["id"] == retry["id"]


def test_changed_source_cannot_reuse_holdout_for_different_candidate(tmp_path, monkeypatch):
    service, exported, _, failed = failed_validation(tmp_path, monkeypatch)
    service.loaded_environment = {"revision": "source-after-repair"}
    other = complete_candidate(service, exported, sigma=0.8)
    with pytest.raises(ConflictError, match="already exposed"):
        lab.validate(service, other["id"], exported["validation_request"])
    assert lab.get(service, failed["id"]) == failed


def test_repaired_source_cannot_change_failed_holdout_selection(tmp_path, monkeypatch):
    service, exported, parent, _ = failed_validation(tmp_path, monkeypatch)
    service.loaded_environment = {"revision": "source-after-repair"}
    with pytest.raises(ConflictError, match="already exposed"):
        lab.validate(service, parent["id"], {"measurement_ids": exported["validation_ids"][:1]})


def test_other_candidate_overlap_is_checked_before_idempotent_return(tmp_path, monkeypatch):
    service, exported, parent, failed = failed_validation(tmp_path, monkeypatch)
    conflict = deepcopy(failed)
    conflict.update(id="other-existing-validation", parent_study_id="other-parent")
    # Deliberately place the reusable run first; a return inside the loop would
    # hide the conflicting candidate exposure supplied by an inconsistent store.
    monkeypatch.setattr(service.registry, "list_runs", lambda **kwargs: [failed, conflict])
    with pytest.raises(ConflictError, match="already exposed"):
        lab.validate(service, parent["id"], exported["validation_request"])

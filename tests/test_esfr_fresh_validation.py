"""A new holdout is acquired only after the exact candidate and criteria freeze."""
from copy import deepcopy
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image

from camera_platform import calibration_lab as lab, esfr_bridge as bridge, esfr_lab
from camera_platform.registry import IntegrityError, canonical, digest
from test_esfr_bridge import fixture_run


def finish_esfr(service, row, observations):
    assert service.registry.claim()["id"] == row["id"]
    summary = {"run_id": row["id"], "observations": observations}
    artifact = service.registry.store_blob(canonical(summary).encode(), "esfr-result.json", "application/json")
    service.registry.finish(row["id"], result={"summary": summary, "artifacts": [artifact]})
    return service.registry.get_run(row["id"])


def fixture_contract(tmp_path, monkeypatch):
    service, original, captures = fixture_run(tmp_path, repeats=3)
    service.loaded_environment = {"fixture": "source-frozen"}
    monkeypatch.setattr(lab, "_source_code", lambda: service.loaded_environment)
    old = service.registry.get_run(original)
    observations = old["result"]["summary"]["observations"]
    roi = {k: observations[0]["rois"][0][k] for k in ("id", "kind", "field_id", "box")}
    source = esfr_lab.submit(service, {
        "development_ids": [c["id"] for c in captures[:3]], "validation_ids": [c["id"] for c in captures[3:]],
        "rois": [roi], "chart_profile": old["specification"]["chart_profile"], "fit": True,
        "baseline_sigma_native_px": 1.5, "thresholds": {"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 0.05}})
    source = finish_esfr(service, source, observations)
    exported = bridge.export(service, source["id"])
    parent = lab.submit(service, {**exported["study_request"], "fit_method": "analytic_curve"})
    assert service.registry.claim()["id"] == parent["id"]
    summary = {"candidate_profile": parent["base_profile"], "candidate_sha256": digest(parent["base_profile"]),
               "fit": {"training_measurement_ids": exported["training_ids"]}}
    artifact = service.registry.store_blob(canonical(summary).encode(), "result.json", "application/json")
    service.registry.finish(parent["id"], result={"summary": summary, "artifacts": [artifact]})
    contract = bridge.create_validation_contract(service, parent["id"])
    return service, source, exported, parent, contract


def fresh_source(tmp_path, service, source, contract, *, change_meta=None, change_spec=None):
    template = source["capture_snapshots"][0]["metadata"]
    observations, ids = [], []
    roi = source["result"]["summary"]["observations"][0]["rois"][0]
    for i in range(3):
        metadata = deepcopy(template)
        metadata.update(role="validation", acquisition_id=f"new-acquisition-{i}", session_id="new-session",
                        generated_at=datetime.now(timezone.utc).isoformat(),
                        validation_contract={"id": contract["id"], "content_hash": contract["content_hash"]})
        if change_meta:
            change_meta(metadata)
        rng = np.random.default_rng(600 + i)
        raw = tmp_path / f"new-{i}.raw"; raw.write_bytes(rng.integers(100, 4000, (64, 80), dtype=np.uint16).tobytes())
        isp = tmp_path / f"new-{i}.png"; Image.fromarray(rng.integers(10, 250, (64, 80, 3), dtype=np.uint8)).save(isp)
        capture = esfr_lab.import_capture(service, metadata, raw, isp)
        ids.append(capture["id"])
        new_roi = deepcopy(roi)
        new_roi["raw"]["Gr"]["sfr"][2] = 0.72 + i * 0.005
        observations.append({"capture_id": capture["id"], "role": "validation", "source_kind": "synthetic", "rois": [new_roi]})
    request = {"development_ids": contract["development_capture_ids"], "validation_ids": ids,
               "fit": False, **{k: contract[k] for k in ("rois", "chart_profile", "thresholds", "baseline_sigma_native_px", "source")}}
    if change_spec:
        change_spec(request)
    row = esfr_lab.submit(service, request)
    return finish_esfr(service, row, observations)


def test_fresh_holdout_contract_export_preserves_frozen_candidate_and_same_campaign(tmp_path, monkeypatch):
    service, source, old_export, parent, contract = fixture_contract(tmp_path, monkeypatch)
    assert bridge.create_validation_contract(service, parent["id"]) == contract
    row = fresh_source(tmp_path, service, source, contract)
    result = bridge.export_validation(service, row["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])
    assert result["campaign_id"] == old_export["campaign_id"]
    assert result["training_ids"] == old_export["training_ids"]
    assert set(result["validation_ids"]).isdisjoint(old_export["validation_ids"])
    assert result["candidate_sha256"] == contract["candidate_sha256"]
    assert result["status"] == "ready_for_frozen_validation" and result["study_request"] is None
    assert result == bridge.export_validation(service, row["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])
    verified = bridge.validation_contract_for_source(service, row["id"], parent["id"])
    assert verified["new_validation_capture_ids"] == row["validation_ids"]
    assert verified["candidate_sha256"] == contract["candidate_sha256"]
    validation = lab.validate(service, parent["id"], result["validation_request"])
    assert validation["candidate_sha256"] == contract["candidate_sha256"]
    assert validation["parent_study_id"] == parent["id"]
    assert not set(validation["development_lineage"]) & set(validation["validation_lineage"])


@pytest.mark.parametrize("mutation", ["old_time", "wrong_contract", "threshold", "fit"])
def test_fresh_source_rejects_pre_freeze_or_changed_contract(tmp_path, monkeypatch, mutation):
    service, source, _, parent, contract = fixture_contract(tmp_path, monkeypatch)
    changes = {
        "old_time": dict(change_meta=lambda m: m.update(generated_at="2000-01-01T00:00:00+00:00")),
        "wrong_contract": dict(change_meta=lambda m: m["validation_contract"].update(content_hash="bad")),
        "threshold": dict(change_spec=lambda p: p["thresholds"].update(raw_sfr_rmse=0.09)),
        "fit": dict(change_spec=lambda p: p.update(fit=True)),
    }
    # Mutations must not alter the local frozen contract object by aliasing.
    row = fresh_source(tmp_path, service, source, deepcopy(contract), **changes[mutation])
    with pytest.raises((IntegrityError, ValueError)):
        bridge.export_validation(service, row["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])


def test_interrupted_publication_recovers_same_immutable_measurement(tmp_path, monkeypatch):
    service, source, old_export, parent, contract = fixture_contract(tmp_path, monkeypatch)
    row = fresh_source(tmp_path, service, source, contract)
    original = service.registry.put_verified_record
    interrupted = []
    def publish(kind, *args, **kwargs):
        result = original(kind, *args, **kwargs)
        if kind == lab.MEASUREMENTS and not interrupted:
            interrupted.append(result["id"])
            raise RuntimeError("connection lost after immutable measurement commit")
        return result
    monkeypatch.setattr(service.registry, "put_verified_record", publish)
    with pytest.raises(RuntimeError, match="connection lost"):
        bridge.export_validation(service, row["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])
    result = bridge.export_validation(service, row["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])
    assert result["validation_ids"] == interrupted
    assert len(service.registry.list(lab.MEASUREMENTS)) == len(old_export["measurement_ids"]) + 1

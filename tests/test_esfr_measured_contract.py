"""Contract/export tests use registry fixtures, not claims of physical capture."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from camera_platform import calibration_lab as lab, esfr_bridge as bridge, esfr_lab
from camera_platform.calibration_esfr_replay import acquisition_scope
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical, digest
from test_esfr_bridge import metadata
from test_esfr_fresh_validation import finish_esfr


def capture_fixture(service, index, *, contract=None):
    """Assemble immutable registry records; native decoding is tested elsewhere."""
    meta = metadata(0, "measured")
    meta.update(acquisition_id=f"measured-{index}", session_id="new-session" if contract else "dev-session",
                role="validation" if contract else "development",
                sampling={"binning": [1, 1], "subsampling": [1, 1], "resized": False, "rotation_deg": 0, "mirrored": False},
                raw_processing={"linear_response": True, "demosaiced": False, "denoised": False,
                                "sharpened": False, "companded": False, "geometric_transform": False},
                captured_at=datetime.now(timezone.utc).isoformat())
    meta["isp"]["comparison_mode"] = "diagnostic"
    if contract:
        meta["validation_contract"] = {"id": contract["id"], "content_hash": contract["content_hash"]}
        meta["acquisition_attestation"] = {"operator_id": "unit-operator", "captured_after_contract": True,
            "original_raw_isp_pair": True, "camera_and_chart_unchanged": True}
        bridge.verify_measured_validation_metadata(service, meta)
    files = {name: service.registry.store_blob((name + str(index)).encode(), name, "application/octet-stream")
             for name in ("raw", "isp", "preview")}
    return service.registry.put(esfr_lab.CAPTURES, {
        "schema": "camera_esfr_capture_v1", "metadata": meta, "raw_shape": [64, 80],
        "native_pixel_sha256": sha256(("native" + str(index)).encode()).hexdigest(),
        "isp_pixel_sha256": sha256(("isp-native" + str(index)).encode()).hexdigest(),
        "files": files, "artifacts": list(files.values()), "physical_identity_verified": False})


def measured_source(service, captures, *, contract=None, rois=None, change_spec=None):
    if rois is None:
        rois = [dict(id=f"edge-{i}", kind="edge", field_id=f"edge-{i}",
                     box=[(i // 14) * 6, (i % 14) * 4, 8, 8]) for i in range(112)]
    spec = {"rois": rois, "chart_profile": {"id": "owned-physical-chart"},
            "fit": False, "thresholds": {"raw_sfr_rmse": .03, "isp_sfr_rmse": .05},
            "baseline_sigma_native_px": 1.5, "source": None}
    if contract:
        spec.update({key: deepcopy(contract[key]) for key in
                     ("rois", "chart_profile", "thresholds", "baseline_sigma_native_px", "source")})
    if change_spec:
        change_spec(spec)
    dev = [c for c in captures if c["metadata"]["role"] == "development"]
    val = [c for c in captures if c["metadata"]["role"] == "validation"]
    row = service.registry.submit({
        "job_kind": esfr_lab.KIND, "specification": spec, "source_code": service.loaded_environment,
        "capture_snapshots": captures, "development_ids": [c["id"] for c in dev],
        "validation_ids": [c["id"] for c in val], "source_context": None,
        "development_lineage": sorted(set().union(*(esfr_lab._lineage(c) for c in dev))),
        "validation_lineage": sorted(set().union(*(esfr_lab._lineage(c) for c in val))) if val else [],
        "input_artifacts": [a for c in captures for a in c["artifacts"]]}, "measured-fixture", force=True)
    observations = [{"capture_id": c["id"], "role": c["metadata"]["role"], "source_kind": "measured",
                     "rois": [{**r, "raw": {"Gr": {"status": "ok", "edge_angle_deg": 25,
                         "frequency_direction_deg": 115, "frequency_cy_native_px": [0, .05, .1, .15, .2, .25],
                         "sfr": [1, .94, .79 if contract else .8, .55, .29, .1]}}} for r in rois]}
                    for c in captures]
    return finish_esfr(service, row, observations)


def frozen_parent(tmp_path, monkeypatch):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "state"), loaded_environment={"fixture": "frozen"})
    monkeypatch.setattr(lab, "_source_code", lambda: service.loaded_environment)
    captures = [capture_fixture(service, i) for i in range(3)]
    source = measured_source(service, captures)
    exported = bridge.export(service, source["id"])
    assert exported["status"] == "ready_for_mtf_study" and exported["validation_ids"] == []
    assert exported["validation_request"] is None and len(exported["training_ids"]) == 112
    parent = lab.submit(service, exported["study_request"])
    assert service.registry.claim()["id"] == parent["id"]
    candidate = deepcopy(parent["base_profile"])
    forward = {"source_kind": "measured", "development_capture_ids": source["development_ids"],
               "geometry": {**captures[0]["metadata"]["geometry"], "roi_nuisance": {
                   r["id"]: {"geometry": {"box": r["box"]}} for r in source["specification"]["rois"]}},
               "fields": {r["id"]: {"sigma_x_px": 1.5, "sigma_y_px": 1.5, "theta_deg": 0}
                          for r in source["specification"]["rois"]}}
    candidate["esfr_forward_model"] = {"fit_data_role": "development_only", "profile": forward,
        "profile_sha256": digest(forward), "method": "development_native_bayer_same_observer_gaussian_v1"}
    summary = {"candidate_profile": candidate, "candidate_sha256": digest(candidate),
               "fit": {"training_measurement_ids": exported["training_ids"]}}
    blob = service.registry.store_blob(canonical(summary).encode(), "result.json", "application/json")
    service.registry.finish(parent["id"], result={"summary": summary, "artifacts": [blob]})
    contract = bridge.create_validation_contract(service, parent["id"])
    return service, source, exported, parent, contract


def test_measured_development_only_then_fresh112_export_stays_measured(tmp_path, monkeypatch):
    service, source, initial, parent, contract = frozen_parent(tmp_path, monkeypatch)
    assert contract["source_kind"] == "measured" and contract["physical_identity_verified"] is False
    assert contract["acquisition_evidence"] == "operator_declared_not_authenticated"
    assert contract == bridge.create_validation_contract(service, parent["id"])
    val = [capture_fixture(service, 10 + i, contract=contract) for i in range(3)]
    fresh = measured_source(service, source["capture_snapshots"] + val, contract=contract)
    exported = bridge.export_validation(service, fresh["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])
    assert exported["source_kind"] == "measured" and exported["physical_validation"] == "not_established"
    assert exported["campaign_id"] == initial["campaign_id"] and exported["training_ids"] == initial["training_ids"]
    assert len(exported["validation_ids"]) == 112 and exported["study_request"] is None
    assert all(lab._measurement(service, x)["source_kind"] == "measured" for x in exported["measurement_ids"])
    verified = bridge.validation_contract_for_source(service, fresh["id"], parent["id"])
    assert verified["candidate_sha256"] == contract["candidate_sha256"]
    validation = lab.validate(service, parent["id"], exported["validation_request"])
    assert validation["candidate_sha256"] == contract["candidate_sha256"]
    assert not set(validation["development_lineage"]) & set(validation["validation_lineage"])


@pytest.mark.parametrize("mutation", ["time_old", "time_future", "time_naive", "attestation", "operator", "source_kind", "isp", "sampling", "raw_processing", "contract"])
def test_measured_capture_declarations_must_match_freeze(tmp_path, monkeypatch, mutation):
    service, source, _, _, contract = frozen_parent(tmp_path, monkeypatch)
    meta = deepcopy(source["capture_snapshots"][0]["metadata"])
    meta.update(role="validation", captured_at=datetime.now(timezone.utc).isoformat(),
                validation_contract={"id": contract["id"], "content_hash": contract["content_hash"]},
                acquisition_attestation={"operator_id": "operator", "captured_after_contract": True,
                                        "original_raw_isp_pair": True, "camera_and_chart_unchanged": True})
    if mutation == "time_old": meta["captured_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "time_future": meta["captured_at"] = "2999-01-01T00:00:00Z"
    elif mutation == "time_naive": meta["captured_at"] = "2026-10-18T00:00:00"
    elif mutation == "attestation": meta["acquisition_attestation"]["camera_and_chart_unchanged"] = "true"
    elif mutation == "operator": meta["acquisition_attestation"]["operator_id"] = " "
    elif mutation == "source_kind": meta["source_kind"] = "synthetic"
    elif mutation == "isp": meta["isp"]["comparison_mode"] = "matched_bilinear"
    elif mutation == "sampling": meta["sampling"]["binning"] = [2, 2]
    elif mutation == "raw_processing": meta["raw_processing"]["sharpened"] = True
    else: meta["validation_contract"]["content_hash"] = "changed"
    with pytest.raises((ValueError, IntegrityError)):
        bridge.verify_measured_validation_metadata(service, meta)


@pytest.mark.parametrize("mutation", ["rois", "thresholds", "fit", "source", "code"])
def test_measured_fresh_source_cannot_change_frozen_observation(tmp_path, monkeypatch, mutation):
    service, source, _, parent, contract = frozen_parent(tmp_path, monkeypatch)
    val = [capture_fixture(service, 10 + i, contract=contract) for i in range(3)]
    changes = {"rois": lambda s: s["rois"][0]["box"].__setitem__(0, 1),
               "thresholds": lambda s: s["thresholds"].update(raw_sfr_rmse=.09),
               "fit": lambda s: s.update(fit=True),
               "source": lambda s: s.update(source={"run_id": "different"}), "code": lambda s: None}
    if mutation == "code": service.loaded_environment = {"fixture": "changed"}
    fresh = measured_source(service, source["capture_snapshots"] + val, contract=contract, change_spec=changes[mutation])
    with pytest.raises((ValueError, IntegrityError)):
        bridge.export_validation(service, fresh["id"], parent_study_id=parent["id"], validation_contract_id=contract["id"])


def test_acquisition_labels_distinguish_measured_declaration_from_synthetic_and_replay():
    measured = acquisition_scope({"source_kind": "measured"})
    assert measured["validation_acquisition_status"] == "new_independent_measured_acquisition"
    assert measured["acquisition_evidence"] == "operator_declared_not_authenticated"
    assert measured["physical_identity_verified"] is False and measured["acquisition_time_authenticated"] is False
    assert acquisition_scope({})["validation_acquisition_status"] == "new_independent_synthetic_acquisition"
    assert acquisition_scope(None)["candidate_frozen_before_new_acquisition"] is False


def test_measured_contract_rejects_changed_method_or_incomplete_forward_geometry(tmp_path, monkeypatch):
    service, source, _, submitted, _ = frozen_parent(tmp_path, monkeypatch)
    parent = lab.get(service, submitted["id"])
    for mutation in ("method", "binding_method", "missing_field", "missing_geometry", "already_validated"):
        changed_parent, changed_source = deepcopy(parent), deepcopy(source)
        binding = changed_parent["result"]["summary"]["candidate_profile"]["esfr_forward_model"]
        if mutation == "method": changed_parent["specification"]["fit_method"] = "analytic_curve"
        elif mutation == "binding_method": binding["method"] = "different_operator"
        elif mutation == "missing_field": binding["profile"]["fields"].pop("edge-0")
        elif mutation == "missing_geometry": binding["profile"]["geometry"]["roi_nuisance"].pop("edge-0")
        else: changed_source["validation_ids"] = ["previous-exposure"]
        binding["profile_sha256"] = digest(binding["profile"])
        with pytest.raises((ValueError, IntegrityError)):
            bridge._measured_contract_fields(changed_source, changed_parent)

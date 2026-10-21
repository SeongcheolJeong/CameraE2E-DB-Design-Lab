"""Optical initialization evidence, explicit draft application and immutable source gates."""

import hashlib
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_db_optics as optics
from camera_platform import calibration_lab as lab
from camera_platform import reference_workshop_job
from camera_platform.api import create_app
from camera_platform.calibration_lab_contracts import template
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.sdk import PlatformClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    service = app.state.service
    monkeypatch.setattr(lab, "_source_code", lambda: {"optical_api_test": "frozen"})
    service.loaded_environment = lab._source_code()
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        yield service, http, sdk


@pytest.fixture
def model_source(api, tmp_path, monkeypatch):
    from camera_platform import calibration_db_optics_engine as engine

    service, _, _ = api
    candidate = {"id": "candidate-03", "status": "succeeded", "label": "selected optical model"}
    condition = {"id": "condition-006", "candidates": [candidate]}
    summary = {
        "schema": "camera_db_design_study_result_v1",
        "comparison_mode": "aperture_conditions",
        "aperture_conditions": {"conditions": [condition]},
        "candidates": [candidate],
    }
    original = canonical(summary).encode()
    manifest = canonical(
        {
            "schema": "camera_reference_workshop_files_v1",
            "files": {"result.json": hashlib.sha256(original).hexdigest()},
        }
    ).encode()
    summary["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    run = service.registry.submit(
        {"job_kind": "reference_workshop", "plugin_id": "camera_db_design_study_v1"},
        "optical-source",
    )
    assert service.registry.claim()["id"] == run["id"]
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "source-result.json", "application/json"
    )
    service.registry.finish(run["id"], result={"summary": summary, "artifacts": [artifact]})
    monkeypatch.setattr(
        reference_workshop_job, "get", lambda _s, identity: service.registry.get_run(identity)
    )
    monkeypatch.setattr(reference_workshop_job, "_directory", lambda _s, _id: tmp_path)
    ancestor_prefix = "ancestors/run-" + "b" * 32 + "/"
    npz = b"exact original optical NPZ bytes; numerical parser tested separately"
    ancestor_manifest = canonical(
        {
            "schema": "camera_reference_workshop_files_v1",
            "files": {"optics.npz": hashlib.sha256(npz).hexdigest()},
        }
    ).encode()
    blur = deepcopy(template()["campaign"]["baseline_profile"]["blur"])
    blur.update(
        family="field_effective_psf_v1",
        included_effects=["lens_blur", "pixel_aperture"],
        charge_diffusion_assumption="zero_not_identified",
        field_application="exact_nodes_only",
    )
    blur["field_nodes"][0]["sigma_x_px"] = 0.7
    blur["field_nodes"][0]["sigma_y_px"] = 0.8
    blur["field_nodes"][0].update(
        family="sampled_native_kernel_v1",
        kernel=[[0.0, 0.1, 0.0], [0.1, 0.5, 0.2], [0.0, 0.0, 0.1]],
        kernel_pitch_px=1.0,
        sampling="native_cell_center_pixel_integrated",
    )
    converted = {
        "frozen_files": {
            "manifest.json": manifest,
            "result.json": original,
            ancestor_prefix + "manifest.json": ancestor_manifest,
            ancestor_prefix + "optics.npz": npz,
        },
        "manifest_bindings": {
            "": summary["manifest_sha256"],
            ancestor_prefix: hashlib.sha256(ancestor_manifest).hexdigest(),
        },
        "source_identity": {
            "run_id": run["id"],
            "candidate_id": "candidate-03",
            "condition_id": "condition-006",
            "manifest_sha256": summary["manifest_sha256"],
            "candidate_sha256": digest(candidate),
            "ancestors": [
                {
                    "run_id": "run-" + "b" * 32,
                    "manifest_sha256": hashlib.sha256(ancestor_manifest).hexdigest(),
                }
            ],
        },
        "profile_patch": {
            "blur": blur,
            "conditions": {"aperture_f_number": 4.12, "light_id": "D65"},
        },
        "conditions_mapping": {
            "aperture_f_number": "source applied pupil",
            "light_id": "modeled D65",
        },
        "report": {
            "gates": [{"id": "approximation", "status": "pass", "rmse": 0.01}],
            "limitations": ["modeled optics; zero charge diffusion assumption"],
        },
        "can_apply": True,
        "status": "fitted_model_initialization",
        "derived_files": {"spectral-evidence.json": b'{"kind":"derived_model_operator"}'},
    }
    calls = []

    def convert(
        root,
        supplied,
        identity,
        profile,
        condition_id=None,
        expected_manifest_sha256=None,
        *,
        field_mapping=None,
        field_mapping_mode=None,
    ):
        assert supplied == summary
        assert identity == "candidate-03" and condition_id == "condition-006"
        assert expected_manifest_sha256 == summary["manifest_sha256"]
        calls.append(
            {
                "profile": deepcopy(profile),
                "field_mapping": field_mapping,
                "field_mapping_mode": field_mapping_mode,
            }
        )
        return deepcopy(converted)

    monkeypatch.setattr(engine, "convert_source", convert)
    return {
        "source": {
            "run_id": run["id"],
            "candidate_id": "candidate-03",
            "condition_id": "condition-006",
        },
        "converted": converted,
        "calls": calls,
    }


def convert(api, model_source):
    _, _, sdk = api
    source = model_source["source"]
    profile = deepcopy(template()["campaign"]["baseline_profile"])
    result = sdk.create_calibration_db_optical_profile(
        source["run_id"], source["candidate_id"], profile, condition=source["condition_id"]
    )
    return result, profile


def draft(record):
    value = template()["campaign"]
    value["db_optical_profile_id"] = record["id"]
    value["baseline_profile"]["blur"] = deepcopy(record["profile_patch"]["blur"])
    value["conditions"].update(record["profile_patch"]["conditions"])
    value["baseline_profile"]["conditions"] = deepcopy(value["conditions"])
    return value


def test_conversion_preserves_original_profile_and_ancestor_bytes_without_activation(
    api, model_source
):
    service, http, sdk = api
    before = lab.create_campaign(service, template()["campaign"])
    record, profile = convert(api, model_source)
    assert record["status"] == "qualified" and record["can_apply"] is True
    assert sdk.calibration_db_optical_profile(record["id"])["id"] == record["id"]
    assert record["physical_validation"] == "not_established"
    assert service.registry.get(lab.CAMPAIGNS, before["id"]) == before
    assert service.registry.list(lab.ACTIVATIONS) == []
    assert len(service.registry.list(lab.CAMPAIGNS)) == 1
    original = http.get(record["files_base"] + "input-profile.json")
    assert original.status_code == 200 and original.content == canonical(profile).encode()
    assert record["input_profile_sha256"] == hashlib.sha256(original.content).hexdigest()
    proposed = http.get(record["files_base"] + "converted-profile.json").json()
    assert proposed["native_shape"] == profile["native_shape"]
    assert proposed["electronics"] == profile["electronics"] and proposed["isp"] == profile["isp"]
    assert proposed["conditions"]["focus_distance_m"] == profile["conditions"]["focus_distance_m"]
    assert proposed["conditions"]["aperture_f_number"] == 4.12
    assert digest(proposed) == record["converted_profile_sha256"]
    for relative, raw in model_source["converted"]["frozen_files"].items():
        descriptor = record["source_files"][relative]
        bound = service.registry.artifact_binding(
            descriptor["sha256"], owner_kind=optics.COLLECTION, owner_id=record["id"]
        )
        assert bound["path"].read_bytes() == raw
    report = http.get(record["report_url"]).json()
    assert report["evidence_kind"] == "model_derived_profile_initialization"
    assert report["can_apply"] is True
    assert http.get(record["files_base"] + "unregistered.json").status_code == 404


def test_manual_new_campaign_application_and_exact_blur_metadata_gates(api, model_source):
    service, http, _ = api
    record, _ = convert(api, model_source)
    value = draft(record)
    response = http.post(lab.ROUTE + "/campaigns", json=value)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["db_optical_profile_sha256"] == record["content_hash"]
    assert created["source"] == model_source["source"]
    assert lab.campaign_detail(service, created["id"])["db_optical_profile"]["id"] == record["id"]
    for mutation in ("blur", "pitch", "shape", "conditions", "source"):
        bad = deepcopy(value)
        if mutation == "blur":
            bad["baseline_profile"]["blur"]["field_nodes"][0]["sigma_x_px"] += 0.1
        elif mutation == "pitch":
            bad["pixel_pitch_um"] = bad["baseline_profile"]["pixel_pitch_um"] = 2.9
        elif mutation == "shape":
            bad["native_shape"] = bad["baseline_profile"]["native_shape"] = [256, 256]
        elif mutation == "conditions":
            bad["conditions"]["light_id"] = bad["baseline_profile"]["conditions"]["light_id"] = (
                "D50"
            )
        else:
            bad["source"] = {**model_source["source"], "condition_id": "condition-007"}
        response = http.post(lab.ROUTE + "/campaigns", json=bad)
        assert response.status_code == 422, (mutation, response.text)


def test_rejected_approximation_retained_but_cannot_initialize_campaign(api, model_source):
    _, http, _ = api
    model_source["converted"].update(can_apply=False, status="rejected_approximation")
    model_source["converted"]["report"]["gates"] = [
        {"id": "approximation", "status": "fail", "rmse": 0.5}
    ]
    record, _ = convert(api, model_source)
    assert record["status"] == "rejected" and record["can_apply"] is False
    assert "converted-profile.json" not in record["output_files"]
    assert http.get(record["report_url"]).json()["gates"][0]["status"] == "fail"
    response = http.post(lab.ROUTE + "/campaigns", json=draft(record))
    assert response.status_code == 422 and "Rejected" in response.text


def test_optical_artifact_corruption_and_unbound_ancestor_rejected(api, model_source):
    service, http, _ = api
    record, _ = convert(api, model_source)
    checksum = record["output_files"]["conversion-report.json"]["sha256"]
    service.registry.blob_path(checksum).write_bytes(b"changed report")
    with pytest.raises(IntegrityError):
        optics.get_record(service, record["id"])
    model_source["converted"]["frozen_files"]["ancestors/run-" + "b" * 32 + "/extra.npz"] = (
        b"unbound"
    )
    response = http.post(
        optics.ROUTE,
        json={
            "source": model_source["source"],
            "profile": template()["campaign"]["baseline_profile"],
        },
    )
    assert response.status_code == 409 and "manifest" in response.text


def test_file_download_verifies_only_selected_typed_binding(api, model_source, monkeypatch):
    service, http, _ = api
    record, _ = convert(api, model_source)
    original_binding = service.registry.artifact_binding
    selected = record["output_files"]["conversion-report.json"]
    calls = []

    def observe(checksum, *, owner_kind, owner_id):
        calls.append((checksum, owner_kind, owner_id))
        return original_binding(checksum, owner_kind=owner_kind, owner_id=owner_id)

    monkeypatch.setattr(service.registry, "artifact_binding", observe)
    response = http.get(record["report_url"])
    assert response.status_code == 200
    assert len(response.content) == selected["bytes"]
    assert hashlib.sha256(response.content).hexdigest() == selected["sha256"]
    assert calls == [(selected["sha256"], optics.COLLECTION, record["id"])]


@pytest.mark.parametrize("same_size", [False, True])
def test_file_download_refuses_corrupted_selected_blob(api, model_source, same_size):
    service, http, _ = api
    record, _ = convert(api, model_source)
    selected = record["output_files"]["conversion-report.json"]
    path = service.registry.blob_path(selected["sha256"])
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:] if same_size else b"corrupted")
    with pytest.raises(IntegrityError):
        optics._file(service, record["id"], "conversion-report.json")
    assert http.get(record["report_url"]).status_code == 409


def test_mapping_and_request_admission_stale_api_and_source_condition(api, model_source):
    service, http, _ = api
    valid = {
        "source": model_source["source"],
        "profile": template()["campaign"]["baseline_profile"],
    }
    for change in (
        {"profile": None},
        {"unexpected": True},
        {"field_mapping": []},
        {"field_mapping_mode": "automatic_sensor"},
        {"field_mapping_mode": []},
        {
            "field_mapping_mode": "reference_model",
            "field_mapping": [{"source_field_id": "center", "field_xy": [0, 0]}],
        },
        {"field_mapping": [{"source_field_id": "outer-ne", "field_xy": [2, 0]}]},
    ):
        assert http.post(optics.ROUTE, json={**valid, **change}).status_code == 422
    missing = deepcopy(valid)
    del missing["source"]["condition_id"]
    assert http.post(optics.ROUTE, json=missing).status_code == 422
    mapping = [{"source_field_id": "center", "field_xy": [0, 0]}]
    response = http.post(optics.ROUTE, json={**valid, "field_mapping": mapping})
    assert response.status_code == 201, response.text
    assert model_source["calls"][-1]["field_mapping"] == mapping
    source = model_source["source"]
    record = api[2].create_calibration_db_optical_profile(
        source["run_id"],
        source["candidate_id"],
        valid["profile"],
        condition=source["condition_id"],
        field_mapping_mode="reference_model",
    )
    assert record["field_mapping_mode"] == "reference_model"
    assert model_source["calls"][-1]["field_mapping_mode"] == "reference_model"
    assert record["resolved_field_mapping"][0]["field_xy"] == [0, 0]
    service.loaded_environment = {"stale": True}
    assert http.post(optics.ROUTE, json=valid).status_code == 409

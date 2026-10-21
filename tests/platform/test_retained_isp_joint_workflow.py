"""Registered-source Joint Study: API, Worker, provenance and cold evidence."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile

import httpx
import numpy as np
import pytest
import test_scene_render
from fastapi.testclient import TestClient
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as hdr_workflow  # noqa: F401 - pytest fixture dependency
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_workflow import payload as replay_payload

from camera_platform import capture_programs, factory, scene_render, sequences
from camera_platform import retained_isp_joint_study as engine
from camera_platform import retained_isp_joint_workflow as joint
from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService


def _spec(repeats, dynamic, model, test):
    selector = {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"}
    return {
        "schema": joint.SCHEMA,
        "name": "Registered noise and time-signal Joint Study",
        "isp_model_id": model["id"],
        "stationary_sources": [
            {"source_id": run["id"], "selector": selector} for run in repeats
        ],
        "dynamic_source": {"source_id": dynamic["id"], "selector": selector},
        "temporal_test_id": test["id"],
        "axes": [{"parameter": "temporal_filter.previous_weight", "values": [0, 0.5]}],
        "noise_roi": {"row_start": 1, "row_end": 15,
                      "column_start": 1, "column_end": 15},
        "objectives": [
            {"metric": engine.NOISE, "direction": "minimize"},
            {"metric": "signal_change_rms_error", "direction": "minimize"},
        ],
        "constraints": [
            {"metric": "truth_code_mae", "operator": "<=", "value": 1,
             "unit": "normalized_code_rgb"},
            {"metric": "signal_amplitude_error", "operator": "<=", "value": 1,
             "unit": "1"},
            {"metric": "signal_change_rms_error", "operator": "<=", "value": 0.2,
             "unit": "1"},
        ],
    }


def test_saved_npy_loader_requires_exact_native_array(tmp_path):
    service = PlatformService(tmp_path / "joint-array-loader")
    pixels = np.arange(2 * 4 * 4 * 3, dtype=np.float64).reshape(2, 4, 4, 3) / 100

    def artifact(array, suffix=b""):
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        return service.registry.store_blob(
            stream.getvalue() + suffix, "candidate-000-dynamic-pre-tone.npy",
            "application/x-npy",
        )

    saved = artifact(pixels)
    np.testing.assert_array_equal(
        joint._load_saved_array(service, saved, pixels.shape, np.dtype("float64")), pixels,
    )
    with pytest.raises(IntegrityError, match="different native values/type"):
        joint._load_saved_array(service, artifact(pixels, b"x"), pixels.shape,
                                np.dtype("float64"))
    with pytest.raises(IntegrityError, match="shape/byte contract"):
        joint._load_saved_array(service, artifact(pixels.astype(np.float32)), pixels.shape,
                                np.dtype("float64"))
    with pytest.raises(IntegrityError, match="different native values/type"):
        joint._load_saved_array(service, artifact(np.asfortranarray(pixels)), pixels.shape,
                                np.dtype("float64"))


def test_registered_joint_study_api_worker_cold_bundle_and_forgery(
    workflow, tmp_path, monkeypatch,
):
    service, sdk, assembly, _, _, _, _, static_request = workflow
    repeats = [finish(service, service.submit_run({
        **static_request,
        "parameters": {"seed": seed, "noise_enabled": True},
    })) for seed in (601, 602, 603, 604)]

    dynamic_scene = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "moving_spot"},
    )
    dynamic_control = sdk.create_control_sequence(
        sequences.control_template(service, assembly["id"], dynamic_scene["id"])
    )
    program_draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="retained_charge",
        control_sequence_id=dynamic_control["id"],
    )
    program_draft["payload"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    dynamic_program = sdk.create_capture_program(program_draft)
    dynamic_test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"],
        scene_sequence_id=dynamic_scene["id"],
        control_sequence_id=dynamic_control["id"],
        capture_program_id=dynamic_program["id"],
    ))
    dynamic_run = finish(service, service.submit_run({
        **static_request,
        "scene_sequence_id": dynamic_scene["id"],
        "control_sequence_id": dynamic_control["id"],
        "capture_program_id": dynamic_program["id"],
        "test_id": dynamic_test["id"],
        "parameters": {"seed": 699, "noise_enabled": False},
    }))
    replay = replay_payload(service, sdk, dynamic_run, enabled=True)
    model = service.registry.get("models", replay["isp_model_id"])
    metadata = sdk.temporal_signal_reference_template(
        dynamic_run["id"], "single", model["id"],
    )
    count, rows, columns = metadata["source_binding"]["raw_shape"]
    truth = np.full((count, rows, columns, 3), 0.2, dtype=np.float64)
    truth[-1] = 0.8
    metadata.update(
        name="Independent authored dynamic step",
        source_uri="synthetic://registered-joint-study-step-v1",
        method_and_version="analytic-step-independent-of-isp:1",
        license="test-fixture",
        description="Analytic time step declared independently of candidate ISP outputs.",
        roi={"row_start": 0, "row_end": rows,
             "column_start": 0, "column_end": columns},
        step={"onset_frame": count - 1, "steady_start_frame": count - 1,
              "onset_fraction": 0.5, "settling_fraction": 0.2},
    )
    truth_path = tmp_path / "joint-truth.npy"
    np.save(truth_path, truth, allow_pickle=False)
    reference = sdk.import_temporal_signal_reference(metadata, truth_path)
    temporal_test = sdk.post(
        "tests", sdk.temporal_signal_test_template(reference["id"]),
    )
    spec = _spec(repeats, dynamic_run, model, temporal_test)

    planned = joint.plan(service, spec)
    assert sdk.plan_retained_isp_joint_study(spec) == planned
    assert planned["cost"]["processor_invocations"] == 10
    assert planned["cost"]["schema"] == "camera_retained_isp_joint_budget_v3"
    assert planned["cost"]["output_artifact_count"] == 30
    outside = {key: copy.deepcopy(model[key]) for key in ModelCreate.model_fields}
    outside["name"] = "Out-of-domain Joint Study ISP"
    outside["revision"] = "outside-exposure"
    outside["valid_domain"]["exposure_s"] = {
        "min": 0, "max": 1e-9, "unit": "s",
    }
    outside_model = service.create_model(outside)
    with pytest.raises(httpx.HTTPStatusError):
        sdk.plan_retained_isp_joint_study({**spec, "isp_model_id": outside_model["id"]})
    bad = copy.deepcopy(spec)
    bad["axes"][0]["values"] = [0, 0.75]
    with pytest.raises(httpx.HTTPStatusError):
        sdk.post("retained-isp/joint-studies", {
            "specification": bad, "plan_hash": planned["plan_hash"],
        })
    queued = sdk.submit_retained_isp_joint_study(planned)
    completed = finish(service, queued)
    assert completed["status"] == "succeeded", completed.get("error")
    summary = completed["result"]["summary"]
    assert summary["schema"] == engine.RESULT_SCHEMA
    assert summary["plan_hash"] == planned["plan_hash"]
    assert summary["specification_sha256"] == digest(summary["specification"])
    assert summary["truth_role"] == "evaluation_only_never_processor_input"
    assert summary["physical_validation"] == "not_established"
    assert summary["automatic_selection"] is False
    assert summary["ranking"]["selected_candidate_id"] is None
    assert len(summary["cases"]) == 2
    assert all(case["noise_support_status"] == "ok" for case in summary["cases"])
    assert all(len(case["repeat_processing"]) == 4 for case in summary["cases"])
    assert all(case["temporal_support_status"] == "ok" for case in summary["cases"])
    assert len(completed["result"]["artifacts"]) == 31
    assert all(record["pre_tone_artifact"]["name"].endswith("-pre-tone.npy")
               and record["temporal_gate_artifact"]["name"].endswith("-temporal-gate.npy")
               and record["display_rgb_artifact"]["name"].endswith("-display-rgb.npy")
               for case in summary["cases"] for record in [
                   *case["repeat_processing"], case["dynamic_processing"],
               ])
    assert joint.get(service, completed["id"])["id"] == completed["id"]
    assert sdk.submit_retained_isp_joint_study(planned)["id"] == completed["id"]
    assert sdk.retained_isp_joint_study(completed["id"])["id"] == completed["id"]
    assert any(item["id"] == completed["id"] for item in sdk.retained_isp_joint_studies())
    assert not any(item["id"] == completed["id"] for item in sdk.get("runs"))

    def no_processor_rerun(*_args, **_kwargs):
        raise AssertionError("Cold verification must use retained arrays")

    monkeypatch.setattr(engine, "process_raw", no_processor_rerun)
    cold = PlatformService(service.registry.directory)
    assert joint.get(cold, completed["id"])["id"] == completed["id"]
    legacy_v2_frozen, _ = joint._prepared(service, spec, budget_version=2)
    legacy_v2_summary = copy.deepcopy(summary)
    legacy_v2_summary["schema"] = engine.RESULT_SCHEMA_V2
    legacy_v2_summary["plan_hash"] = digest(legacy_v2_frozen)
    legacy_v2_summary["cost"] = legacy_v2_frozen["cost"]
    legacy_v2_summary["noise_meaning"] = engine.LEGACY_NOISE_MEANING
    for case in legacy_v2_summary["cases"]:
        for record in [*case["repeat_processing"], case["dynamic_processing"]]:
            record.pop("display_rgb_artifact")
            record.pop("display_rgb_sha256")
    legacy_v2_artifact = service.registry.store_blob(
        canonical(legacy_v2_summary).encode(), "summary.json", "application/json",
    )
    legacy_v2_outputs = [artifact for artifact in completed["result"]["artifacts"]
                         if artifact["name"].endswith(("-pre-tone.npy", "-temporal-gate.npy"))]
    legacy_v2_job = {
        **legacy_v2_frozen, "plan_hash": digest(legacy_v2_frozen),
        "result": {"summary": legacy_v2_summary,
                   "artifacts": [legacy_v2_artifact, *legacy_v2_outputs],
                   "physical_validation": "not_established"},
    }
    assert joint.verify(cold, legacy_v2_job)["result"]["summary"]["schema"].endswith("_v2")
    legacy_frozen, _ = joint._prepared(service, spec, budget_version=1)
    legacy_summary = copy.deepcopy(summary)
    legacy_summary["schema"] = "camera_retained_isp_joint_study_result_v1"
    legacy_summary["plan_hash"] = digest(legacy_frozen)
    legacy_summary["cost"] = legacy_frozen["cost"]
    legacy_summary["noise_meaning"] = engine.LEGACY_NOISE_MEANING
    for case in legacy_summary["cases"]:
        for record in case["repeat_processing"]:
            record.pop("temporal_gate_sha256")
        for record in [*case["repeat_processing"], case["dynamic_processing"]]:
            record.pop("pre_tone_artifact")
            record.pop("temporal_gate_artifact")
            record.pop("display_rgb_artifact")
            record.pop("display_rgb_sha256")
    legacy_artifact = service.registry.store_blob(
        canonical(legacy_summary).encode(), "summary.json", "application/json",
    )
    legacy_job = {
        **legacy_frozen, "plan_hash": digest(legacy_frozen),
        "result": {"summary": legacy_summary, "artifacts": [legacy_artifact],
                   "physical_validation": "not_established"},
    }
    assert joint.verify(cold, legacy_job)["result"]["summary"]["schema"].endswith("_v1")
    altered_case = copy.deepcopy(completed)
    altered_case["result"]["summary"]["cases"][0]["profile_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="candidate"):
        joint.verify(service, altered_case)
    altered_rank = copy.deepcopy(completed)
    altered_rank["result"]["summary"]["ranking"]["selected_candidate_id"] = "candidate-000"
    with pytest.raises(IntegrityError, match="ranking"):
        joint.verify(service, altered_rank)
    altered_metric = copy.deepcopy(completed)
    altered_metric["result"]["summary"]["cases"][0]["metrics"][engine.NOISE]["value"] = float(
        "inf"
    )
    with pytest.raises(IntegrityError, match="finite"):
        joint.verify(service, altered_metric)
    altered_finite_metric = copy.deepcopy(completed)
    altered_finite_metric["result"]["summary"]["cases"][0]["metrics"][engine.NOISE][
        "value"] += 0.01
    altered_finite_metric["result"]["summary"]["ranking"] = engine._rank(
        altered_finite_metric["result"]["summary"]["cases"],
        altered_finite_metric["engine_specification"],
    )
    with pytest.raises(IntegrityError, match="saved metrics differ from retained arrays"):
        joint.verify(service, altered_finite_metric)
    for field, forged_value in (
        ("noise_definition", "a different noise statistic"),
        ("noise_meaning", "measured sensor noise"),
        ("uncertainty", {"kind": "measured", "value": 0.0}),
    ):
        forged = copy.deepcopy(completed)
        forged["result"]["summary"][field] = forged_value
        new_summary = service.registry.store_blob(
            canonical(forged["result"]["summary"]).encode(),
            "summary.json", "application/json",
        )
        forged["result"]["artifacts"] = [
            new_summary if item["name"] == "summary.json" else item
            for item in forged["result"]["artifacts"]
        ]
        with pytest.raises(IntegrityError, match="lost frozen source, plan or authority"):
            joint.verify(service, forged)
    forged_wrapper = copy.deepcopy(completed)
    forged_wrapper["result"]["physical_validation"] = "established"
    with pytest.raises(IntegrityError, match="wrapper changed physical validation"):
        joint.verify(service, forged_wrapper)
    altered_alias = copy.deepcopy(completed)
    record = altered_alias["result"]["summary"]["cases"][0]["repeat_processing"][0]
    original_name = record["pre_tone_artifact"]["name"]
    swapped_name = "candidate-001-stationary-00-pre-tone.npy"
    record["pre_tone_artifact"]["name"] = swapped_name
    assert original_name != swapped_name
    # The Worker result keeps one descriptor object in its summary and artifact
    # list, so this forged semantic alias updates both references at once.
    assert any(item["name"] == swapped_name
               for item in altered_alias["result"]["artifacts"])
    with pytest.raises(IntegrityError, match="array/source alias differs"):
        joint.verify(service, altered_alias)

    manifest = sdk.export_retained_isp_joint_study(completed["id"])
    assert manifest["schema"] == "camera_retained_isp_joint_export_v3"
    assert manifest["study"]["temporal_test_snapshot"] == temporal_test
    assert len([item for item in manifest["artifacts"]
                if item["name"].startswith("candidate-")]) == 30
    assert joint.export_bundle(service, completed["id"]).exists()
    bundle = sdk.retained_isp_joint_study_bundle(completed["id"])
    assert len(bundle) <= planned["cost"]["archive_bytes_bound"]
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        saved = json.loads(archive.read("manifest.json"))
        assert saved == manifest
        for artifact in manifest["artifacts"]:
            data = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]


def test_request_requires_exact_registered_sources():
    with pytest.raises(ValueError, match="exactly"):
        joint.validate_specification({"schema": joint.SCHEMA, "source_snapshot": {}})


@pytest.fixture
def scene_workflow(tmp_path, monkeypatch):
    return test_scene_render.scene_workflow.__wrapped__(tmp_path, monkeypatch)


def test_published_manual_3d_static_sources_reach_registered_joint_study(
    scene_workflow, tmp_path,
):
    service, original, render_request = scene_workflow
    old_lens = next(
        service.registry.get("models", identity)
        for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    old_sensor = next(
        service.registry.get("models", identity)
        for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    lens = factory.publish(service, {
        "job_id": old_lens["psf_factory_job_id"],
        "base_lens_model_id": old_lens["parents"][0],
        "name": "3D static retained optical response", "revision": "joint-static-1",
        "conditions": original["conditions"], "condition_scope": "optical_response",
    })
    sensor_draft = {key: copy.deepcopy(old_sensor[key]) for key in ModelCreate.model_fields}
    sensor_draft.update(
        name="16 pixel full-native 3D static sensor", revision="joint-static-16",
        parents=[old_sensor["id"]],
    )
    pitch = old_sensor["payload"]["pixel_pitch_um"]
    for key in ("active_width", "active_height"):
        sensor_draft["properties"][key] = {"value": pitch * 16 / 1000, "unit": "mm"}
    sensor = service.create_model(sensor_draft)
    assembly_draft = {
        key: copy.deepcopy(original[key]) for key in AssemblyCreate.model_fields
    }
    assembly_draft.update(
        name="Published 3D static Joint ISP camera", revision="joint-static-16",
        model_ids=[
            lens["id"] if identity == old_lens["id"] else
            sensor["id"] if identity == old_sensor["id"] else identity
            for identity in original["model_ids"]
        ],
    )
    assembly = service.create_assembly(assembly_draft)
    assert assembly["compatibility"]["status"] == "PASS"

    render_request.update(
        assembly_id=assembly["id"], shape=[16, 16],
        crop_origin=[0, 0], samples_per_pixel=2,
    )
    rendered = finish(service, scene_render.submit(service, render_request))
    image = scene_render.publish(service, {
        "job_id": rendered["id"], "name": "Published 3D Joint Study static source",
    })
    assert image["render_job_snapshot"]["id"] == rendered["id"]
    assert image["sampling"]["full_sensor_shape"] == [16, 16]
    timeline = sequences.create_sequence(service, "scene_sequences", {
        **PROVENANCE,
        "name": "Manually registered published 3D static scene", "revision": "joint-3d-1",
        "source_uri": "scene-render://joint-study-static-3d",
        "method_and_version": "published_scene_render_static_timeline:1",
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": {
                "clock_id": "joint-3d-static-clock", "unit": "ns", "reference": "relative",
                "origin": "Authored simulation time zero; no hardware acquisition clock",
            },
            "interpolation": "linear_irradiance",
            "knots": [
                {"time_ns": 0, "spectral_image_id": image["id"]},
                {"time_ns": 80_000_000, "spectral_image_id": image["id"]},
            ],
        },
    })
    assert timeline["fixture_specification"] is None
    assert timeline["image_snapshots"][0]["render_job_snapshot"]["id"] == rendered["id"]
    control = sequences.create_sequence(
        service, "control_sequences",
        sequences.control_template(service, assembly["id"], timeline["id"]),
    )
    program_draft = capture_programs.template(
        service, assembly["id"], mode="retained_charge", control_sequence_id=control["id"],
    )
    program_draft["payload"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    program = capture_programs.create(service, program_draft)
    static_test = service.create_test(sequences.test_template(
        service, assembly["id"], timeline["id"], control["id"], program["id"],
    ))
    static_request = {
        "assembly_id": assembly["id"], "test_id": static_test["id"],
        "scene_sequence_id": timeline["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "retained_charge_v1",
    }
    repeats = [finish(service, service.submit_run({
        **static_request, "parameters": {"seed": seed, "noise_enabled": True},
    })) for seed in (701, 702, 703, 704)]
    assert all(run["scene_sequence_snapshot"]["id"] == timeline["id"] for run in repeats)

    moving = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "moving_spot"},
    )
    moving_control = sequences.create_sequence(
        service, "control_sequences",
        sequences.control_template(service, assembly["id"], moving["id"]),
    )
    moving_program_draft = capture_programs.template(
        service, assembly["id"], mode="retained_charge",
        control_sequence_id=moving_control["id"],
    )
    moving_program_draft["payload"]["retained_charge_model"][
        "transfer_efficiency"] = [0.5] * 4
    moving_program = capture_programs.create(service, moving_program_draft)
    moving_test = service.create_test(sequences.test_template(
        service, assembly["id"], moving["id"], moving_control["id"], moving_program["id"],
    ))
    moving_run = finish(service, service.submit_run({
        **static_request, "test_id": moving_test["id"],
        "scene_sequence_id": moving["id"], "control_sequence_id": moving_control["id"],
        "capture_program_id": moving_program["id"],
        "parameters": {"seed": 799, "noise_enabled": False},
    }))

    app = create_app(service.registry.directory, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = client
        active = app.state.service
        replay = replay_payload(active, sdk, moving_run, enabled=True)
        model = active.registry.get("models", replay["isp_model_id"])
        metadata = sdk.temporal_signal_reference_template(
            moving_run["id"], "single", model["id"],
        )
        count, rows, columns = metadata["source_binding"]["raw_shape"]
        truth = np.full((count, rows, columns, 3), 0.2, dtype=np.float64)
        truth[-1] = 0.8
        metadata.update(
            name="3D static study independent moving truth",
            source_uri="synthetic://joint-3d-moving-truth-v1",
            method_and_version="analytic-step-independent-of-isp:1",
            license="test-fixture",
            description="Separate authored time truth, never derived from candidate output.",
            roi={"row_start": 0, "row_end": rows,
                 "column_start": 0, "column_end": columns},
            step={"onset_frame": count - 1, "steady_start_frame": count - 1,
                  "onset_fraction": 0.5, "settling_fraction": 0.2},
        )
        path = tmp_path / "published-3d-joint-truth.npy"
        np.save(path, truth, allow_pickle=False)
        reference = sdk.import_temporal_signal_reference(metadata, path)
        temporal_test = sdk.post(
            "tests", sdk.temporal_signal_test_template(reference["id"]),
        )
        spec = _spec(repeats, moving_run, model, temporal_test)
        planned = sdk.plan_retained_isp_joint_study(spec)
        assert planned["frozen_request"]["stationary_source_snapshots"][0][
            "source_snapshot"]["scene_sequence_snapshot"]["id"] == timeline["id"]
        result = finish(active, sdk.submit_retained_isp_joint_study(planned))
        assert result["status"] == "succeeded", result.get("error")
        summary = result["result"]["summary"]
        assert summary["ranking"]["selected_candidate_id"] is None
        assert all(case["noise_support_status"] == "ok" for case in summary["cases"])
        assert all(case["temporal_support_status"] == "ok" for case in summary["cases"])
        assert sdk.retained_isp_joint_study(result["id"])["id"] == result["id"]
        assert joint.get(PlatformService(active.registry.directory), result["id"])["id"] == (
            result["id"]
        )

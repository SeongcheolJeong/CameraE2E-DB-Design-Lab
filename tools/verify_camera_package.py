#!/usr/bin/env python3
"""Verify wheel contents and installed execution outside this checkout, reusing dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / "reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl"

CHILD = r"""
import sys, json, io, hashlib, zipfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import camera_platform, pyisetcam, numpy as np
from camera_platform.api import create_app
from camera_platform import factory, imaging, sequences, control_service
from camera_platform.contracts import AssemblyCreate, ModelCreate
from fastapi.testclient import TestClient
from camera_platform.service import environment_fingerprint

assert Path(camera_platform.__file__).is_relative_to(Path(sys.argv[1]))
assert Path(pyisetcam.__file__).is_relative_to(Path(sys.argv[1]))
app = create_app(Path.cwd() / "state", workspace=Path.cwd(), start_worker=False)
s = app.state.service
with TestClient(app) as http:
    assert http.get("/openapi.json").json()["info"]["version"] == camera_platform.__version__
    for file in (Path(camera_platform.__file__).parent / "static").iterdir():
        if not file.is_file():
            continue
        name = file.name
        assert (
            http.get("/" + name).content
            == (Path(camera_platform.__file__).parent / "static" / name).read_bytes()
        )

    def finish(q):
        j = s.registry.claim()
        assert j["id"] == q["id"]
        return s.registry.finish(j["id"], result=s.execute_run(j))

    jobs = {}
    for plugin in ["rayoptics_lens_v1", "meep_planar_stack_v1", "prysm_psf_v1"]:
        j = finish(factory.submit(s, factory.template(s, plugin)))
        assert j["status"] == "succeeded", j
        jobs[plugin] = j
    assert jobs["meep_planar_stack_v1"]["result"]["model_candidate"] is None
    a = s.registry.list("assemblies")[0]
    original_for_bounds = a
    test_for_bounds = s.registry.list("tests")[0]
    base = next(
        s.registry.get("models", i)
        for i in a["model_ids"]
        if s.registry.get("models", i)["kind"] == "lens"
    )
    l = factory.publish(
        s,
        {
            "job_id": jobs["prysm_psf_v1"]["id"],
            "base_lens_model_id": base["id"],
            "name": "Installed PSF",
            "revision": "1",
            "conditions": a["conditions"],
            "condition_scope": "optical_response",
        },
    )
    p = {k: v for k, v in a.items() if k in AssemblyCreate.model_fields}
    p["name"] = "Installed camera"
    p["model_ids"] = [l["id"] if i == base["id"] else i for i in p["model_ids"]]
    a = s.create_assembly(p)
    scene = imaging.create_fixture(s, imaging.fixture_template(s, a["id"]))
    t = s.create_test(imaging.spatial_test_template(s, a["id"], scene["id"]))
    run = finish(
        s.submit_run(
            {
                "assembly_id": a["id"],
                "test_id": t["id"],
                "scene_id": scene["id"],
                "plugin_id": "spatial_scene_v1",
            }
        )
    )
    p = imaging.isp_template(s, a["id"])
    p["payload"]["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
    isp = s.create_model(p)
    r = finish(
        imaging.submit_replay(
            s,
            {
                "name": "Installed replay",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        )
    )

    def artifact(record, name):
        a = next(a for a in record["artifacts"] if a["name"] == name)
        content = http.get(a["url"]).content
        assert hashlib.sha256(content).hexdigest() == a["sha256"]
        return content

    assert artifact(run, "raw_frames.npy") == artifact(r, "raw_frames.npy")
    assert artifact(run, "rgb_frames.npy") != artifact(r, "rgb_frames.npy")
    sequence = sequences.create_fixture(
        s, sequences.fixture_template(s, a["id"]) | {"shape": [16, 16], "knot_count": 17}
    )
    control_data = sequences.control_template(s, a["id"], sequence["id"])
    control_data["payload"]["frames"][1]["applied"]["exposure_ns"] = 8000000
    control = sequences.create_sequence(s, "control_sequences", control_data)
    test = s.create_test(sequences.test_template(s, a["id"], sequence["id"], control["id"]))
    temporal = finish(
        s.submit_run(
            {
                "assembly_id": a["id"],
                "test_id": test["id"],
                "plugin_id": "temporal_scene_v1",
                "scene_sequence_id": sequence["id"],
                "control_sequence_id": control["id"],
            }
        )
    )
    replay = finish(
        imaging.submit_replay(
            s,
            {
                "name": "Installed temporal replay",
                "source_kind": "run",
                "source_id": temporal["id"],
                "isp_model_id": isp["id"],
            },
        )
    )
    assert artifact(temporal, "raw_frames.npy") == artifact(replay, "raw_frames.npy")
    assert artifact(temporal, "rgb_frames.npy") != artifact(replay, "rgb_frames.npy")
    assert temporal["result"]["frame_conditions"] == replay["result"]["frame_conditions"]
    assert temporal["result"]["clock"] == replay["result"]["clock"]
    # Public installed API registers immutable policy/program and generates actual controls.
    http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
    feedback_scene = sequences.create_fixture(
        s,
        sequences.fixture_template(s, a["id"])
        | {
            "shape": [16, 16],
            "knot_count": 3,
            "pattern": "static",
            "duration_ns": 320_000_000,
            "irradiance_scale": 0.002,
        },
    )
    policy_data = control_service.policy_template(s, feedback_scene["id"])
    # Respect the existing gain=1 domain, rather than widening a supplied model.
    policy_data["payload"]["ae"]["readout_gain"]["maximum"] = 1.0
    policy_response = http.post("/api/v1/control_policies", json=policy_data)
    assert policy_response.status_code == 201, policy_response.text
    policy = policy_response.json()
    program_data = http.get(
        "/api/v1/templates/control-program",
        params={
            "assembly_id": a["id"],
            "scene_sequence_id": feedback_scene["id"],
            "control_policy_id": policy["id"],
            "frame_count": 32,
        },
    ).json()
    checked = http.post("/api/v1/control_programs/check", json=program_data)
    assert checked.status_code == 200, checked.text
    raw_program_text = " \r\n" + json.dumps(program_data, indent=3) + "\r\n"
    program_response = http.post(
        "/api/v1/control_programs/import", json={"document_text": raw_program_text}
    )
    assert program_response.status_code == 201, program_response.text
    program = program_response.json()
    source_artifact = next(
        x for x in program["artifacts"] if x["name"] == "control-program-source.json"
    )
    assert http.get(source_artifact["url"]).content == raw_program_text.encode()
    feedback_test = http.post(
        "/api/v1/tests",
        json=http.get(
            "/api/v1/templates/closed-loop-test", params={"control_program_id": program["id"]}
        ).json(),
    ).json()
    response = http.post(
        "/api/v1/runs",
        json={
            "assembly_id": a["id"],
            "test_id": feedback_test["id"],
            "plugin_id": "closed_loop_scene_v1",
            "control_program_id": program["id"],
            "parameters": {"seed": 509},
        },
    )
    assert response.status_code == 202, response.text
    feedback = finish(response.json())
    feedback_summary = feedback["result"]["summary"]
    assert feedback_summary["frame_count"] == 32
    assert feedback_summary["control_trace"]["commands"]
    assert len({f["applied"]["exposure_ns"] for f in feedback_summary["frames"]}) > 1
    assert feedback_summary["control_program_sha256"] == program["expanded_program_sha256"]
    assert feedback_test["frame_conditions"] is None
    feedback_raw = np.load(io.BytesIO(artifact(feedback, "raw_frames.npy")), allow_pickle=False)
    assert feedback_raw.shape == (32, 16, 16)
    assert not np.array_equal(feedback_raw[0], feedback_raw[-1])
    feedback_replay = finish(
        imaging.submit_replay(
            s,
            {
                "name": "Installed feedback RAW replay",
                "source_kind": "run",
                "source_id": feedback["id"],
                "isp_model_id": isp["id"],
            },
        )
    )
    assert artifact(feedback, "raw_frames.npy") == artifact(feedback_replay, "raw_frames.npy")
    assert artifact(feedback, "rgb_frames.npy") != artifact(feedback_replay, "rgb_frames.npy")
    assert (
        feedback_replay["result"]["summary"]["control_trace"] == feedback_summary["control_trace"]
    )
    from camera_platform.validation import compare

    compared = compare(
        feedback_summary["prediction"], feedback_summary["prediction"], feedback_test
    )
    assert compared["status"] == "INCONCLUSIVE"
    from camera_platform import control_response_service as response_service, capture_analysis

    response_test = s.create_test(
        response_service.test_template(s, {"control_program_id": program["id"]})
    )
    response_run = finish(
        s.submit_run(
            {
                "assembly_id": a["id"],
                "test_id": response_test["id"],
                "plugin_id": "closed_loop_scene_v1",
                "control_program_id": program["id"],
                "parameters": {"seed": 123},
            }
        )
    )
    observed = response_run["result"]["response_observation"]
    metadata = {
        "name": "Installed synthetic RAW acquisition",
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "run://" + response_run["id"],
        "license": "MIT",
        "method_and_version": "qa_export:1",
        "physical_instance": None,
        "parents": [],
        "test_id": response_test["id"],
        "assembly_id": a["id"],
        "dataset_ids": ["installed-response-export"],
        "instrument": {},
        "conditions": response_test["conditions"],
        "acquisition": observed["acquisition"],
        "uncertainty": {"kind": "unknown", "unit": "1", "reason": "No total bound"},
    }
    original_metadata = (json.dumps(metadata, indent=3).replace("\n", "\r\n") + "\r\n").encode()
    reply = http.post(
        "/api/v1/captures/control-response/import",
        files={
            "raw": (
                "raw.npy",
                artifact(response_run, "raw_frames.npy"),
                "application/octet-stream",
            ),
            "metadata_file": ("metadata.json", original_metadata, "application/json"),
        },
    )
    assert reply.status_code == 201, reply.text
    capture_result = reply.json()
    capture = capture_result["capture"]
    assert capture["capture_schema"] == "camera_response_acquisition_v1"
    assert artifact(capture, "acquisition-metadata.json") == original_metadata
    response_comparison = s.validate(
        {
            "run_id": response_run["id"],
            "test_id": response_test["id"],
            "measurement_id": capture_result["measurement"]["id"],
        }
    )
    assert (
        response_comparison["status"] == "INCONCLUSIVE"
        and not response_comparison["physical_validation_pass"]
    )
    assert all(v == 0 for v in response_comparison["residuals"]["values"])
    exposure_test = s.create_test(
        response_service.test_template(
            s, {"control_program_id": program["id"], "observable": "exposure_ns"}
        )
    )
    analysis_request = {
        "capture_id": capture["id"],
        "test_id": exposure_test["id"],
        "name": "Installed exposure reanalysis",
        "uncertainty": {"kind": "unknown", "unit": "ns", "reason": "No total bound"},
        "parent_analysis_id": capture_result["analysis"]["id"],
    }
    second = capture_analysis.create(s, analysis_request)
    assert capture_analysis.create(s, analysis_request) == second
    assert (
        second["capture"] == capture
        and second["measurement"]["dataset_ids"] == metadata["dataset_ids"]
    )
    assert len(s.registry.list("captures")) == 1 and len(s.registry.list("capture_analyses")) == 2
    from camera_platform.observations import verify_capture_measurement

    verify_capture_measurement(s, second["measurement"])
    original = original_for_bounds
    sensor = next(
        s.registry.get("models", i)
        for i in original["model_ids"]
        if s.registry.get("models", i)["kind"] == "sensor"
    )
    bounded_payload = {k: v for k, v in sensor.items() if k in ModelCreate.model_fields}
    bounded_payload.update(
        revision="installed-bounds",
        parents=[sensor["id"]],
        parameter_uncertainty={
            "qe": {
                "kind": "absolute_bound",
                "unit": "electron/photon",
                "value": 0.005,
                "source_kind": "estimated",
                "source_uri": "fixture://installed-bound",
                "method_and_version": "authored-assumption/1",
            }
        },
    )
    bounded_sensor = s.create_model(bounded_payload)
    composition = {k: v for k, v in original.items() if k in AssemblyCreate.model_fields}
    composition.update(
        revision="installed-bounds",
        model_ids=[bounded_sensor["id"] if i == sensor["id"] else i for i in original["model_ids"]],
    )
    bounded_assembly = s.create_assembly(composition)
    bounded = finish(
        s.submit_run({"assembly_id": bounded_assembly["id"], "test_id": test_for_bounds["id"]})
    )
    budget = bounded["result"]["prediction"]["uncertainty_budget"]
    assert budget["status"] == "partial" and budget["total_uncertainty"] == {"kind": "unknown"}
    assert budget["sources"]["qe"]["source_content_hash"] == bounded_sensor["content_hash"]
    assert budget["conditional_intervals"]["channel_ratios"]["defined"][0] == [True, True]
    specification = {
        "name": "Installed parameter study",
        "question": "Exposure and read-noise tradeoff",
        "assembly_ids": [original_for_bounds["id"]],
        "test_id": test_for_bounds["id"],
        "axes": [
            {"parameter": "exposure_s", "unit": "s", "values": [0.002, 0.008]},
            {"parameter": "read_noise_e", "unit": "electron", "values": [1.0, 5.0]},
        ],
        "objectives": [
            {"metric": "min_theoretical_snr_db", "direction": "maximize"},
            {"metric": "exposure_ms", "direction": "minimize"},
        ],
        "frames": 2,
        "output_size": [8, 8],
    }
    plan = s.plan_study(specification)
    study = s.create_study({"specification": specification, "plan_hash": plan["plan_hash"]})
    for run_id in study["run_ids"]:
        finish(s.registry.get_run(run_id))
    analysis = s.analyze_study(study["id"])
    assert analysis["coverage"]["succeeded"] == 5 and len(analysis["sensitivities"]) == 4
    assert analysis["total_uncertainty"] == {"kind": "unknown"}
    repeated_study = s.create_study(
        {"specification": specification, "plan_hash": plan["plan_hash"]}
    )
    assert repeated_study["id"] == study["id"]

    # Installed surrogate learns from actual camera runs, then predicts from JSON coefficients.
    from copy import deepcopy

    references = []
    for label, exposure, noise in [
        ("training", [0.002, 0.004, 0.006, 0.008], [1.0, 2.0, 3.0, 5.0]),
        ("validation", [0.003, 0.005, 0.007], [1.5, 2.5, 4.0]),
    ]:
        payload = deepcopy(specification)
        payload.update(name="Installed surrogate " + label, design="cartesian")
        payload["axes"][0]["values"], payload["axes"][1]["values"] = exposure, noise
        planned = s.plan_study(payload)
        submitted = s.create_study({"specification": payload, "plan_hash": planned["plan_hash"]})
        for identity in submitted["run_ids"]:
            finish(s.registry.get_run(identity))
        references.append(submitted)
    training, validation = references
    surrogate_spec = {
        "name": "Installed metric RBF",
        "purpose": "Numerically verified fixed-camera approximation",
        "training": [{"study_id": training["id"], "run_ids": training["run_ids"]}],
        "validation": [
            {
                "study_id": validation["id"],
                "run_ids": [
                    identity
                    for identity, candidate in zip(validation["run_ids"], validation["candidates"])
                    if not candidate["is_baseline"]
                ],
            }
        ],
        "metrics": [{"metric": "min_theoretical_snr_db", "unit": "dB", "max_absolute_error": 0.3}],
    }
    surrogate_plan = s.plan_surrogate(surrogate_spec)
    surrogate = finish(
        s.create_surrogate(
            {"specification": surrogate_spec, "plan_hash": surrogate_plan["plan_hash"]}
        )
    )
    assert surrogate["result"]["prediction_eligible"]
    predicted = s.predict_surrogate(
        surrogate["id"],
        {
            "points": [{"exposure_s": 0.0055, "read_noise_e": 3.5}],
            "units": {"exposure_s": "s", "read_noise_e": "electron"},
        },
    )
    assert len(predicted["rows"]) == 1 and predicted["total_uncertainty"] == {"kind": "unknown"}
    assert s.registry.get("surrogate_predictions", predicted["id"]) == predicted

    from camera_platform.registry import digest

    full = http.get("/api/v1/bootstrap").json()
    index = http.get("/api/v1/workbench").json()
    assert index["workbench_schema"] == "camera_workbench_v1"
    assert index["counts"] == full["counts"]
    for collection in index["projected_collections"]:
        original = {item["id"]: item for item in full[collection]}
        assert set(original) == {item["id"] for item in index[collection]}
        for item in index[collection]:
            ref = item["_projection"]
            assert ref["record_sha256"] == digest(original[item["id"]])
            detail = http.get("/api/v1" + ref["detail_path"]).json()
            assert detail["record"] == original[item["id"]]
            assert detail["record_sha256"] == ref["record_sha256"]
    session = http.get("/api/v1/session")
    assert session.json()["token"] == full["token"] and len(session.content) < 1024

    from camera_platform import scene_render, scene_templates
    from camera_platform.demo import PROVENANCE

    authored = scene_templates.templates(s)
    environment_light = scene_render.create_asset(s, authored["assets"]["light"])
    scene_data = authored["assets"]["scene"]
    scene_data["payload"].update(instances=[], environment_light_id=environment_light["id"])
    scene3d = scene_render.create_asset(s, scene_data)
    optical_base = next(
        s.registry.get("models", i)
        for i in original_for_bounds["model_ids"]
        if s.registry.get("models", i)["kind"] == "lens"
    )
    declared = json.loads(
        json.dumps({k: v for k, v in optical_base.items() if k in ModelCreate.model_fields})
    )
    declared.update(
        name="Installed declared synthetic projection",
        parents=[optical_base["id"]],
        source_kind="estimated",
        source_uri="fixture://installed-scene-optics",
    )
    declared["properties"].update(
        focal_length={"value": 4.0, "unit": "mm"}, f_number={"value": 8.0, "unit": "1"}
    )
    declared = s.create_model(declared)
    scene_conditions = {
        **original_for_bounds["conditions"],
        "light_id": {"value": scene3d["illumination_id"], "unit": "1"},
        "f_number": {"value": 8.0, "unit": "1"},
    }
    scene_lens = factory.publish(
        s,
        {
            "job_id": jobs["prysm_psf_v1"]["id"],
            "base_lens_model_id": declared["id"],
            "name": "Installed declared scene PSF",
            "revision": "scene-installed-1",
            "conditions": scene_conditions,
        },
    )
    assembly_data = {
        k: v for k, v in original_for_bounds.items() if k in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Installed spectral scene camera",
        conditions=scene_conditions,
        model_ids=[
            scene_lens["id"] if i == optical_base["id"] else i
            for i in original_for_bounds["model_ids"]
        ],
    )
    draft = {
        "scene_asset_id": scene3d["id"],
        "source_assembly_id": original_for_bounds["id"],
        "assembly": assembly_data,
    }
    before_count = len(s.registry.list("assemblies"))
    response = http.post("/api/v1/scene_authoring/camera-preview", json=draft)
    assert response.status_code == 200, response.text
    preparation = response.json()
    assert preparation["status"] == "ready", preparation["issues"]
    assert len(s.registry.list("assemblies")) == before_count
    save_request = {"draft": draft, "preview_sha256": preparation["preview_sha256"]}
    response = http.post("/api/v1/scene_authoring/cameras", json=save_request)
    assert response.status_code == 201, response.text
    prepared = response.json()
    assert http.post("/api/v1/scene_authoring/cameras", json=save_request).json() == prepared
    assert len(s.registry.list("assemblies")) == before_count + 1
    scene_camera = prepared["assembly"]
    assert scene_camera["scene_preparation"]["source_assembly_sha256"] == (
        original_for_bounds["content_hash"]
    )
    assert s.registry.get("assemblies", original_for_bounds["id"]) == original_for_bounds
    request = prepared["render"]["request"]
    assert all(v is None for v in request["projection"]["on_axis_transmittance"]["values"])
    request.update(shape=[8, 8], samples_per_pixel=2)
    request["projection"]["on_axis_transmittance"]["values"] = [0.8] * len(
        request["projection"]["on_axis_transmittance"]["wavelength_nm"]
    )
    rendered3d = finish(scene_render.submit(s, request))
    radiance3d = np.load(
        io.BytesIO(artifact(rendered3d, "spectral-radiance.npy")), allow_pickle=False
    )
    np.testing.assert_allclose(radiance3d, 0.002, rtol=2e-6)
    assert len(rendered3d["result"]["display"]["previews"]) == radiance3d.shape[2] + 1
    image3d = scene_render.publish(
        s, {"job_id": rendered3d["id"], "name": "Installed rendered spectral input"}
    )
    test3d = s.create_test(imaging.spatial_test_template(s, scene_camera["id"], image3d["id"]))
    camera3d = finish(
        s.submit_run(
            {
                "assembly_id": scene_camera["id"],
                "test_id": test3d["id"],
                "scene_id": image3d["id"],
                "plugin_id": "spatial_scene_v1",
                "parameters": {"frames": 2, "seed": 57},
            }
        )
    )
    assert np.load(io.BytesIO(artifact(camera3d, "raw_frames.npy")), allow_pickle=False).shape == (
        2,
        8,
        8,
    )
    assert camera3d["scene_snapshot"]["render_job_snapshot"]["id"] == rendered3d["id"]
    assert http.get("/api/v1/scene_renders/" + rendered3d["id"] + "/bundle").status_code == 200
    assert http.get("/api/v1/scene_authoring/templates").status_code == 200
    # A separately installed projection consumes the original L, not a rerender.
    original_backend = scene_render.backend_identity
    def forbidden_renderer():
        raise AssertionError("Radiance reuse invoked the renderer")
    scene_render.backend_identity = forbidden_renderer
    try:
        response = http.get("/api/v1/scene_authoring/projection-template", params={
            "source_render_id": rendered3d["id"], "assembly_id": scene_camera["id"],
        })
        assert response.status_code == 200, response.text
        projection_draft = response.json()["request"]
        projection_draft["projection"]["on_axis_transmittance"]["values"] = (
            [0.4] * radiance3d.shape[2]
        )
        projection_draft["projection"].update(
            source_uri="fixture://installed-projection-throughput",
            method_and_version="synthetic-throughput-assumption:1",
        )
        response = http.post("/api/v1/scene_projections/preview", json=projection_draft)
        assert response.status_code == 200 and response.json()["status"] == "ready", response.text
        response = http.post("/api/v1/scene_projections", json={
            "draft": projection_draft, "preview_sha256": response.json()["preview_sha256"],
        })
        assert response.status_code == 202, response.text
        projected = finish(response.json())
        projected_e = np.load(io.BytesIO(artifact(projected, "spectral-image.npy")),
                              allow_pickle=False)
        np.testing.assert_allclose(projected_e, radiance3d * np.pi * 0.4 / (4 * 8**2),
                                   rtol=2e-15, atol=0)
        assert artifact(projected, "spectral-radiance.npy") == artifact(
            rendered3d, "spectral-radiance.npy"
        )
        publication = {"job_id": projected["id"], "name": "Installed reused radiance"}
        response = http.post("/api/v1/scene_projection_publications", json=publication)
        assert response.status_code == 201, response.text
        projected_image = response.json()
        assert http.post("/api/v1/scene_projection_publications", json=publication).json() == (
            projected_image
        )
        projected_test = s.create_test(imaging.spatial_test_template(
            s, scene_camera["id"], projected_image["id"]
        ))
        projected_camera = finish(s.submit_run({
            "assembly_id": scene_camera["id"], "test_id": projected_test["id"],
            "scene_id": projected_image["id"], "plugin_id": "spatial_scene_v1",
            "parameters": {"frames": 2, "seed": 57},
        }))
        assert artifact(projected_camera, "raw_frames.npy") != artifact(camera3d, "raw_frames.npy")
        artifact(projected_camera, "rgb_frames.npy")
        assert projected_camera["scene_snapshot"]["projection_job_snapshot"]["id"] == (
            projected["id"]
        )
        response = http.get("/api/v1/scene_projections/" + projected["id"] + "/bundle")
        assert response.status_code == 200
        assert s.registry.get_run(rendered3d["id"]) == rendered3d
    finally:
        scene_render.backend_identity = original_backend

    def verify_prescription_optics():
        from camera_platform import prescription_psf

        def get(path, **kwargs):
            response = http.get("/api/v1/" + path, **kwargs)
            assert response.status_code == 200, response.text
            return response.json()

        def post(path, payload, status=201):
            response = http.post("/api/v1/" + path, json=payload)
            assert response.status_code == status, response.text
            return response.json()

        def fields(record, contract):
            return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}

        source = original_for_bounds
        source_models = {s.registry.get("models", i)["kind"]: s.registry.get("models", i)
                         for i in source["model_ids"]}
        assert all(model["source_kind"] == PROVENANCE["source_kind"]
                   and model["source_uri"] == PROVENANCE["source_uri"]
                   for model in source_models.values())
        psf_request = get("templates/factory", params={"plugin_id": "prescription_psf_v1"})
        # Exercise the shipped default without relaxing optical/numerical gates.
        assert psf_request["request"] == prescription_psf.request_template()
        psf_request.update(name="Installed prescription OPD",
                           asset_id=source_models["lens"]["asset_id"])
        ri_request = get("templates/factory", params={"plugin_id": "rayoptics_lens_v1"})
        ri_request.update(name="Installed prescription radiometry",
                          asset_id=psf_request["asset_id"],
                          request=deepcopy(psf_request["request"]["prescription"]))
        radiometry = finish(post("factory/jobs", ri_request, 202))
        psf = finish(post("factory/jobs", psf_request, 202))
        assert radiometry["request"] == psf["request"]["prescription"]
        for job in (radiometry, psf):
            assert job["status"] == "succeeded"
            assert job["result"]["summary"]["status"] == "PROPOSED"
            assert job["result"]["physical_validation"] == "not_established"
            assert job["result"]["summary"]["uncertainty"] == {"kind": "unknown"}
        summary = psf["result"]["summary"]
        assert summary["propagation_phase"] == "exp(-2*pi*i*opd_nm/wavelength_nm)"
        assert any("transport is not solved" in text for text in summary["assumptions"])
        assert any("Jacobian" in text for text in summary["limitations"])
        opd = np.load(io.BytesIO(artifact(psf, "fine_opd_nm.npy")), allow_pickle=False)
        admitted = np.load(io.BytesIO(artifact(psf, "fine_admitted.npy")), allow_pickle=False)
        kernel = np.load(io.BytesIO(artifact(psf, "psf_kernel.npy")), allow_pickle=False)
        assert opd.shape == admitted.shape and admitted.dtype == bool
        assert np.isfinite(opd).all() and np.max(np.abs(opd)) > 1
        assert kernel.ndim == 5 and kernel.shape[:3] == (2, 3, 3)
        assert np.isfinite(kernel).all() and np.all(kernel >= 0)
        np.testing.assert_allclose(kernel.sum(axis=(-1, -2)), 1, atol=1e-12, rtol=0)

        parent = post("factory/models", {
            "job_id": radiometry["id"], "name": "Installed same-prescription RI",
            "revision": "1", "conditions": source["conditions"],
        })
        conditions = deepcopy(source["conditions"])
        conditions["f_number"] = {"value": summary["geometric_reference_f_number"], "unit": "1"}
        lens = post("factory/models", {
            "job_id": psf["id"], "base_lens_model_id": parent["id"],
            "name": "Installed prescription field Lens", "revision": "1",
            "conditions": conditions, "condition_scope": "optical_response",
        })
        assert lens["parents"] == [parent["id"]] and lens["psf_factory_job_id"] == psf["id"]
        assert parent["factory_job_id"] == radiometry["id"]
        assert lens["payload"]["psf"] == psf["result"]["model_candidate"]
        assert lens["source_kind"] == "simulated" and lens["uncertainty"] == {"kind": "unknown"}
        radius = psf["request"]["prescription"]["image_height_mm"][-1]
        assert lens["payload"]["field_reference_radius_mm"] == radius

        # Restrict an explicitly synthetic sensor to the exact PSF wavelengths;
        # this is a new model and does not change the original seeded camera.
        sensor_data = fields(source_models["sensor"], ModelCreate)
        wavelengths = psf["request"]["prescription"]["wavelength_nm"]
        indices = [sensor_data["payload"]["wavelength_nm"].index(w) for w in wavelengths]
        sensor_data.update(name="Installed prescription sensor fixture", revision="1",
                           parents=[source_models["sensor"]["id"]])
        sensor_data["spectral_range"] = {
            "min": wavelengths[0], "max": wavelengths[-1], "unit": "nm",
        }
        sensor_data["payload"]["wavelength_nm"] = wavelengths
        sensor_data["payload"]["qe"] = [[row[i] for i in indices]
                                         for row in sensor_data["payload"]["qe"]]
        angular = sensor_data["payload"]["sensor_angular_response"]
        angular["relative_response"] = [
            [row[i] for i in indices] for row in angular["relative_response"]
        ]
        for dimension in ("active_height", "active_width"):
            sensor_data["properties"][dimension] = {"value": 0.384, "unit": "mm"}
        sensor = post("models", sensor_data)
        assembly_data = fields(source, AssemblyCreate)
        assembly_data.update(name="Installed prescription optics camera", conditions=conditions,
                             spectral_range=sensor_data["spectral_range"],
                             model_ids=[lens["id"], sensor["id"], source_models["isp"]["id"]])
        assembly = post("assemblies", assembly_data)
        assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
        image = post("spectral_images/fixture", {"assembly_id": assembly["id"]})
        test = post("tests", get("templates/spatial-test", params={
            "assembly_id": assembly["id"], "scene_id": image["id"],
        }))
        camera = finish(post("runs", {
            "assembly_id": assembly["id"], "scene_id": image["id"], "test_id": test["id"],
            "plugin_id": "spatial_scene_v1", "parameters": {"frames": 2, "seed": 41},
        }, 202))
        assert camera["result"]["psf"]["spatial_model"] == "field_grid"
        assert camera["result"]["physical_validation"] == "not_established"
        assert camera["result"]["uncertainty"] == {"kind": "unknown"}
        assert camera["result"]["radial_response_sampling"]["field_reference_radius_mm"] == radius
        np.testing.assert_array_equal(
            np.load(io.BytesIO(artifact(camera, "psf_intensity.npy")), allow_pickle=False), kernel
        )
        raw_bytes = artifact(camera, "raw_frames.npy")
        rgb_bytes = artifact(camera, "rgb_frames.npy")
        raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
        rgb = np.load(io.BytesIO(rgb_bytes), allow_pickle=False)
        assert raw.shape == (2, 64, 64) and raw.dtype.kind in "ui" and raw.std() > 0
        assert rgb.shape == (2, 64, 64, 3) and np.isfinite(rgb).all()
        isp_data = get("templates/isp-profile", params={"assembly_id": assembly["id"]})
        isp_data["payload"]["white_balance"]["gains_rgb"] = [1.4, 1.0, 0.8]
        isp_model = post("models", isp_data)
        replay = finish(post("replays", {
            "name": "Installed prescription RAW replay", "source_kind": "run",
            "source_id": camera["id"], "isp_model_id": isp_model["id"],
        }, 202))
        assert artifact(replay, "raw_frames.npy") == raw_bytes
        replay_rgb = artifact(replay, "rgb_frames.npy")
        assert replay_rgb != rgb_bytes
        assert replay["result"]["conditions"] == camera["result"]["conditions"]

        bundles = {}
        for collection, record in (("runs", camera), ("replays", replay)):
            response = http.get("/api/v1/" + collection + "/" + record["id"] + "/bundle")
            assert response.status_code == 200, response.text
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                keys = (("run", "model_lineage", "factory_jobs") if collection == "runs"
                        else ("replay", "source", "model_lineage", "factory_jobs"))
                assert manifest["package_hash"] == digest({k: manifest[k] for k in keys})
                assert manifest["run" if collection == "runs" else "replay"] == record
                if collection == "replays":
                    assert manifest["source"] == camera
                factory_records = {j["id"]: j for j in manifest["factory_jobs"]}
                for job in (radiometry, psf):
                    assert factory_records.get(job["id"]) == job
                model_records = {m["id"]: m for m in manifest["model_lineage"]}
                for model in (parent, lens, sensor):
                    assert model_records.get(model["id"]) == model
                evidence_records = [record, radiometry, psf]
                if collection == "replays":
                    evidence_records.append(camera)
                entries = {(a["sha256"], a["name"]): a for evidence in evidence_records
                           for a in [*evidence.get("input_artifacts", []), *evidence["artifacts"]]}
                for (checksum, name), descriptor in entries.items():
                    original = http.get(descriptor["url"])
                    assert original.status_code == 200, original.text
                    assert len(original.content) == descriptor["bytes"]
                    assert hashlib.sha256(original.content).hexdigest() == checksum
                    assert archive.read("artifacts/" + checksum + "/" + name) == original.content
                bundles[collection] = {
                    "zip_sha256": hashlib.sha256(response.content).hexdigest(),
                    "package_hash": manifest["package_hash"],
                    "original_artifacts_verified": len(entries),
                }
        assert s.registry.get("assemblies", source["id"]) == source
        for model in source_models.values():
            assert s.registry.get("models", model["id"]) == model
        return {
            "radiometry_job_id": radiometry["id"], "psf_job_id": psf["id"],
            "factory_statuses": {"radiometry": "PROPOSED", "prescription_psf": "PROPOSED"},
            "request_sha256": summary["request_sha256"], "parent_lens_model_id": parent["id"],
            "lens_model_id": lens["id"], "assembly_id": assembly["id"],
            "run_id": camera["id"], "replay_id": replay["id"],
            "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "rgb_sha256": hashlib.sha256(rgb_bytes).hexdigest(),
            "replay_rgb_sha256": hashlib.sha256(replay_rgb).hexdigest(),
            "bundles": bundles, "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
        }

    prescription_proof = verify_prescription_optics()
    assert s.registry.audit()["integrity"] == "verified"
    print(
        json.dumps(
            {
                "status": "passed",
                "platform_version": camera_platform.__version__,
                "environment": environment_fingerprint(),
                "installed_module_paths": {
                    "camera_platform": camera_platform.__file__,
                    "pyisetcam": pyisetcam.__file__,
                },
                "factory_statuses": {k: v["result"]["summary"]["status"] for k, v in jobs.items()},
                "raw_sha256": r["result"]["raw_sha256"],
                "source_and_replay_conditions_equal": run["result"]["conditions"]
                == r["result"]["conditions"],
                "static_http_matches_installed_bytes": True,
                "raw_and_rgb_artifact_http_hashes_verified": True,
                "raw_unchanged_and_tuned_rgb_changed": True,
                "temporal_camera_and_raw_replay_with_applied_conditions_verified": True,
                "input_bound_propagation_with_frozen_source_verified": True,
                "atomic_parameter_study_and_actual_five_run_analysis_verified": True,
                "surrogate_actual_training_holdout_and_persisted_prediction_verified": True,
                "workbench_projection_full_evidence_and_small_session_verified": True,
                "closed_loop_api_program_full_range_and_32_frame_raw_replay_verified": True,
                "closed_loop_control_program_hash": program["expanded_program_sha256"],
                "closed_loop_comparison_keeps_physical_validation_unestablished": True,
                "response_original_file_common_operator_and_reusable_analysis_verified": True,
                "response_capture_schema": capture["capture_schema"],
                "response_analysis_count": len(s.registry.list("capture_analyses")),
                "scene_authoring_api_and_renderer_to_raw_verified": True,
                "scene_camera_preparation_api_idempotent_save_to_raw_verified": True,
                "scene_projection_api_to_raw_without_renderer_verified": True,
                "prescription_optics_api_to_raw_replay_and_parent_bundle_verified": True,
                "prescription_optics": prescription_proof,
                "scene_backend": rendered3d["backend"],
                "scene_radiance_sha256": hashlib.sha256(
                    artifact(rendered3d, "spectral-radiance.npy")
                ).hexdigest(),
                "registry_integrity": "verified",
                "measurement_count": len(s.registry.list("measurements")),
            }
        )
    )
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, default=WHEEL)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "reports/camera_platform/package-verification.json",
    )
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        files = [
            p
            for prefix in ["camera_platform", "pyisetcam"]
            for p in (ROOT / "src" / prefix).rglob("*")
            if p.is_file()
            and (p.suffix in {".py", ".js", ".css", ".html"}
                 or ("data/r1" in p.as_posix()
                     and (p.suffix in {".json", ".npz"} or p.name == "LICENSE")))
            and "__pycache__" not in p.parts
        ]
        for path in files:
            assert archive.read(str(path.relative_to(ROOT / "src"))) == path.read_bytes(), path
    with tempfile.TemporaryDirectory(prefix="camera-platform-installed-") as directory:
        outside = Path(directory).resolve()
        target = outside / "installed"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(target),
                str(wheel),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        script = outside / "verify.py"
        script.write_text(CHILD)
        result = subprocess.run(
            [sys.executable, "-I", str(script), str(target)],
            cwd=outside,
            check=False,
            capture_output=True,
            text=True,
            # Existing end-to-end paths plus the two new bounded optical jobs
            # (radiometry up to 45 s, prescription OPD up to 100 s).
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-6000:] + result.stdout[-2000:])
        report = json.loads(result.stdout)
    report.update(
        wheel=str(wheel),
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        wheel_bytes=wheel.stat().st_size,
        source_static_file_count=len(files),
        all_checked_source_static_bytes_match=True,
        temporary_state_removed=True,
        scope="Installed wheel outside checkout using Python -I and existing dependencies; "
        "not a clean OS/dependency-resolution test or hardware validation",
    )
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "wheel_sha256": report["wheel_sha256"],
                "source_files": len(files),
                "report": str(output),
            }
        )
    )


if __name__ == "__main__":
    main()

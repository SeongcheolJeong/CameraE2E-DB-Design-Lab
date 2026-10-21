"""Field PSFs must survive real model, crop, RAW, control and evidence boundaries."""

import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_closed_loop import recorded_request
from test_scene_camera import setup, snapshots
from test_scene_projection import draft as projection_draft
from test_scene_projection import projection_source, target_camera  # noqa: F401
from test_scene_render import finish, scene_workflow  # noqa: F401

from camera_platform import imaging, scene_camera, scene_projection, scene_render, spatial
from camera_platform.api import create_app
from camera_platform.closed_loop import ClosedLoopSceneRuntime, request_template
from camera_platform.contracts import ModelCreate
from camera_platform.evidence import build_bundle
from camera_platform.service import PlatformService, PreflightError
from camera_platform.temporal import TemporalSceneRuntime


def field_psf(*, extent=0.1):
    psf = deepcopy(spatial.request_template()["psf"])
    values = np.zeros((5, 2, 2, 3, 3))
    values[:, 0, 0, 1, 0] = 1
    values[:, 0, 1, 1, 1] = 1
    values[:, 1, 0, 1, 1] = 1
    values[:, 1, 1, 1, 2] = 1
    psf.update(
        schema="field_intensity_psf_v1",
        spatial_model="field_grid",
        field_y_mm=[-extent, extent],
        field_x_mm=[-extent, extent],
        field_coordinates="sensor_center_yx",
        interpolation="bilinear_source_position",
        sample_pitch_um=3.0,
        values=values.tolist(),
        source={
            "kind": "estimated",
            "description": "Arithmetic field fixture, not optical evidence",
        },
    )
    return psf


def field_setup(service, *, extent=0.1):
    draft, original, source, parent = setup(service)
    fields = {k: deepcopy(v) for k, v in parent.items() if k in ModelCreate.model_fields}
    fields.update(name="Supplied field PSF", parents=[parent["id"]])
    fields["payload"]["psf"] = field_psf(extent=extent)
    model = service.create_model(fields)
    draft["assembly"]["model_ids"] = [
        model["id"] if i == parent["id"] else i for i in draft["assembly"]["model_ids"]
    ]
    return draft, model


def array(service, job, name):
    artifact = next(a for a in job["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def test_http_field_model_to_raw_and_replay_keeps_full_kernel_evidence(tmp_path):
    app = create_app(tmp_path / "state", start_worker=False)
    service = app.state.service
    draft, model = field_setup(service)
    with TestClient(app) as client:
        session = client.get("/api/v1/workbench").json()
        client.headers["X-Platform-Token"] = session["token"]
        projected = next(m for m in session["models"] if m["id"] == model["id"])
        assert projected["display_summary"]["psf_spatial_model"] == "field_grid"
        assert "payload" not in projected
        response = client.post("/api/v1/assemblies", json=draft["assembly"])
        assert response.status_code == 201, response.text
        assembly = response.json()
        response = client.post(
            "/api/v1/spectral_images/fixture", json={"assembly_id": assembly["id"]}
        )
        assert response.status_code == 201, response.text
        image = response.json()
        test = client.get(
            "/api/v1/templates/spatial-test",
            params={
                "assembly_id": assembly["id"],
                "scene_id": image["id"],
            },
        ).json()
        test = client.post("/api/v1/tests", json=test).json()
        response = client.post(
            "/api/v1/runs",
            json={
                "assembly_id": assembly["id"],
                "scene_id": image["id"],
                "test_id": test["id"],
                "plugin_id": "spatial_scene_v1",
                "parameters": {"frames": 2, "seed": 712},
            },
        )
        assert response.status_code == 202, response.text
        run = finish(service, response.json())
        assert run["result"]["psf"]["spatial_model"] == "field_grid"
        assert run["result"]["physical_validation"] == "not_established"
        np.testing.assert_array_equal(
            array(service, run, "psf_intensity"), model["payload"]["psf"]["values"]
        )
        raw = array(service, run, "raw_frames")
        assert raw.shape == (2, 64, 64) and raw.std() > 10
        isp = imaging.isp_template(service, assembly["id"])
        isp["payload"]["white_balance"]["gains_rgb"] = [1.3, 1.0, 0.7]
        isp = service.create_model(isp)
        replay = client.post(
            "/api/v1/replays",
            json={
                "name": "Field result replay",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        )
        assert replay.status_code == 202, replay.text
        replay = finish(service, replay.json())
        np.testing.assert_array_equal(array(service, replay, "raw_frames"), raw)
        assert not np.array_equal(
            array(service, replay, "rgb_frames"), array(service, run, "rgb_frames")
        )
        with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            # A self-contained bundle carries the full PSF values and model
            # snapshot, not only a small workbench display summary.
            assert model["id"] in json.dumps(manifest)
            psf_artifact = next(a for a in run["artifacts"] if a["name"] == "psf_intensity.npy")
            assert any(psf_artifact["sha256"] in name for name in bundle.namelist())


def test_camera_preparation_uses_covered_patch_and_render_checks_actual_crop(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state")
    draft, model = field_setup(service, extent=0.03)
    before = snapshots(service)
    preview = scene_camera.preview(service, draft)
    assert preview["status"] == "ready", preview["issues"]
    assert snapshots(service) == before
    saved = scene_camera.save(
        service, {"draft": draft, "preview_sha256": preview["preview_sha256"]}
    )
    assert any("PSF field grid must cover" in message for message in saved["render"]["issues"])
    render = saved["render"]["request"]
    render["projection"]["on_axis_transmittance"]["values"] = [0.8] * 5

    def forbidden():
        pytest.fail("An uncovered crop must be refused before probing the renderer")

    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    before = snapshots(service)
    with pytest.raises(PreflightError):
        scene_render.submit(service, render)  # Default 32px extends beyond +/-0.03mm.
    assert snapshots(service) == before
    render["shape"] = [8, 8]
    render["crop_origin"] = [0, 0]  # A small crop is still wrong at the sensor corner.
    with pytest.raises(PreflightError):
        scene_render.submit(service, render)
    assert snapshots(service) == before
    full = preview["sampling"]["full_sensor_shape"]
    render["crop_origin"] = [(n - 8) // 2 for n in full]
    monkeypatch.setattr(scene_render, "backend_identity", lambda: {"test_only": "no execution"})
    queued = scene_render.submit(service, render)
    assert queued["status"] == "queued"
    assert queued["request"]["sampling"]["crop_origin"] == render["crop_origin"]


def test_preparation_accepts_exact_native_field_boundary_despite_inverse_rounding(tmp_path):
    service = PlatformService(tmp_path / "state")
    draft, _ = field_setup(service)
    models = {
        m["kind"]: m
        for m in (service.registry.get("models", i) for i in draft["assembly"]["model_ids"])
    }
    psf = models["lens"]["payload"]["psf"]
    psf.update(
        sample_pitch_um=2.2,
        field_x_mm=[-0.0649, -0.04950000000000001],
        field_y_mm=[-0.0649, -0.04950000000000001],
    )
    models["sensor"]["payload"]["pixel_pitch_um"] = 2.2
    # Native indices 2..9 lie on both declared boundaries. Inverting their
    # coordinates and using ceil/floor alone incorrectly removes edge pixels.
    sampling = {"full_sensor_shape": [64, 64], "pixel_pitch_um": 2.2}
    spatial.validate_psf_coverage(spatial.validate_psf(psf), [64, 64], [2, 2], [8, 8], 2.2)
    assert scene_camera._response_preflight(draft["assembly"], models, sampling) == []


def test_reused_radiance_checks_full_original_crop_field_domain(projection_source):  # noqa: F811
    service, assembly, source = projection_source

    # Cover an off-center 8x8 patch (preparation succeeds), but deliberately not
    # the centered source-render crop (actual reuse must fail before submit).
    def replace(data):
        data["payload"]["psf"] = field_psf(extent=0.03)
        data["payload"]["psf"]["field_x_mm"] = [0.06, 0.12]

    target = target_camera(service, assembly, lens_change=replace)
    payload = projection_draft(service, source, target)
    before = snapshots(service)
    plan = scene_projection.preview(service, payload)
    assert plan["status"] == "needs_input"
    assert any(i["code"] == "projection_field_psf_coverage" for i in plan["issues"])
    with pytest.raises(PreflightError):
        scene_projection.submit(
            service, {"draft": payload, "preview_sha256": plan["preview_sha256"]}
        )
    assert snapshots(service) == before


def test_closed_loop_retains_field_axes_once_and_matches_recorded_capture():
    request = request_template()
    request["psf"] = field_psf(extent=10)
    request["psf"]["sample_pitch_um"] = request["pixel_pitch_um"]
    request["noise_enabled"] = False
    request["control_program"]["policy"]["ae"]["enabled"] = False
    request["control_program"]["policy"]["awb"]["enabled"] = False
    output = ClosedLoopSceneRuntime().execute(request)
    recorded = TemporalSceneRuntime().execute(recorded_request(request))
    for name in output["arrays"]:
        np.testing.assert_array_equal(output["arrays"][name], recorded["arrays"][name])
    assert output["summary"]["physical_validation"] == "not_established"

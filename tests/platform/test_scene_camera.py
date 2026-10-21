"""Camera preparation must never repair provenance or missing optics by guessing."""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from camera_platform import scene_camera, scene_render, spatial
from camera_platform.api import create_app
from camera_platform.contracts import ModelCreate
from camera_platform.demo import PROVENANCE, seed
from camera_platform.registry import ConflictError
from camera_platform.service import PlatformService, PreflightError


def setup(service):
    seed(service)
    assembly = service.registry.list("assemblies")[0]
    source = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    draft = {k: copy.deepcopy(v) for k, v in source.items() if k in ModelCreate.model_fields}
    psf = spatial.request_template()["psf"]
    psf["sample_pitch_um"] = 3.0
    draft["payload"]["psf"] = psf
    draft["included_effects"] += ["lens_psf", "pixel_aperture"]
    draft.update(
        parents=[source["id"]],
        source_uri="fixture://declared-preparation-test",
        method_and_version="assumed-camera-preparation:1",
    )
    draft["properties"].update(
        focal_length={"value": 4.0, "unit": "mm"}, f_number={"value": 8.0, "unit": "1"}
    )
    model = service.create_model(draft)
    scene = scene_render.create_asset(
        service,
        {
            **PROVENANCE,
            "name": "Empty authored scene",
            "payload": {"kind": "scene", "coordinate_system": "right_handed_y_up_metres"},
        },
    )
    prepared = scene_camera.template(service, scene["id"], assembly["id"])
    prepared["assembly"]["model_ids"] = [
        model["id"] if i == source["id"] else i for i in assembly["model_ids"]
    ]
    prepared["assembly"]["conditions"]["f_number"] = {"value": 8.0, "unit": "1"}
    return prepared, assembly, source, model


def snapshots(service):
    with service.registry.connection() as conn:
        return {
            table: list(map(tuple, conn.execute("SELECT * FROM " + table)))
            for table in ("records", "jobs", "audit")
        }


def test_readonly_preview_and_idempotent_atomic_save_preserve_originals(tmp_path):
    service = PlatformService(tmp_path / "state")
    draft, original, source, model = setup(service)
    before = snapshots(service)
    preview = scene_camera.preview(service, draft)
    assert snapshots(service) == before
    assert preview["status"] == "ready", preview["issues"]
    assert preview["physical_validation"] == "not_established"
    assert {c["condition"] for c in preview["condition_changes"]} == {"light_id", "f_number"}
    payload = {"draft": draft, "preview_sha256": preview["preview_sha256"]}
    # Concurrent retries produce exactly one immutable assembly and audit event.
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = list(pool.map(lambda _: scene_camera.save(service, payload), range(2)))
    assert saved[0] == saved[1]
    after = snapshots(service)
    assert len(after["records"]) == len(before["records"]) + 1
    assert len(after["audit"]) == len(before["audit"]) + 1
    assert after["jobs"] == before["jobs"]
    assert set(before["records"]) <= set(after["records"])
    assert service.registry.get("assemblies", original["id"]) == original
    assert service.registry.get("models", source["id"]) == source
    assert service.registry.get("models", model["id"]) == model
    a = saved[0]["assembly"]
    assert a["scene_preparation"]["source_assembly_sha256"] == original["content_hash"]
    assert a["model_hashes"][model["id"]] == model["content_hash"]
    assert saved[0]["render"]["request"]["assembly_id"] == a["id"]
    assert all(
        v is None
        for v in saved[0]["render"]["request"]["projection"]["on_axis_transmittance"]["values"]
    )


@pytest.mark.parametrize(
    "fault", ["missing_optics", "f_number", "light", "domain", "psf_pitch", "raw", "roles"]
)
def test_incompatible_preparation_cannot_create_records(tmp_path, fault):
    service = PlatformService(tmp_path / "state")
    draft, original, source, model = setup(service)
    a = draft["assembly"]
    if fault == "missing_optics":
        a["model_ids"] = original["model_ids"]
    if fault == "f_number":
        a["conditions"]["f_number"]["value"] = 4.0
    if fault == "light":
        a["conditions"]["light_id"]["value"] = "unrelated-light"
    if fault == "domain":
        a["conditions"]["temperature_c"]["value"] = 100.0
    if fault == "psf_pitch":
        fields = {k: copy.deepcopy(v) for k, v in model.items() if k in ModelCreate.model_fields}
        fields["payload"]["psf"]["sample_pitch_um"] = 4.0
        bad = service.create_model(fields)
        a["model_ids"] = [bad["id"] if i == model["id"] else i for i in a["model_ids"]]
    if fault == "raw":
        a["raw_format"]["bit_depth"] = 10
    if fault == "roles":
        a["model_ids"] = [model["id"]] * 3
    before = snapshots(service)
    if fault == "roles":
        with pytest.raises(ValueError, match="exactly one"):
            scene_camera.preview(service, draft)
    else:
        result = scene_camera.preview(service, draft)
        assert result["status"] == "needs_input" and result["issues"]
        with pytest.raises(PreflightError):
            scene_camera.save(service, {"draft": draft, "preview_sha256": result["preview_sha256"]})
    assert snapshots(service) == before


def test_changed_draft_or_stale_review_cannot_save(tmp_path):
    service = PlatformService(tmp_path / "state")
    draft, *_ = setup(service)
    result = scene_camera.preview(service, draft)
    before = snapshots(service)
    draft["assembly"]["name"] += " edited after preview"
    with pytest.raises(ConflictError, match="preview"):
        scene_camera.save(service, {"draft": draft, "preview_sha256": result["preview_sha256"]})
    assert snapshots(service) == before


def test_public_api_prepares_a_camera_with_explicit_unknown_transmittance(tmp_path):
    state = tmp_path / "state"
    service = PlatformService(state)
    draft, *_ = setup(service)
    with TestClient(create_app(state, start_worker=False)) as client:
        boot = client.get("/api/v1/workbench").json()
        client.headers["X-Platform-Token"] = boot["token"]
        path = "/api/v1/scene_authoring/"
        result = client.post(path + "camera-preview", json=draft)
        assert result.status_code == 200, result.text
        plan = result.json()
        assert client.get("/api/v1/workbench").json()["counts"] == boot["counts"]
        saved = client.post(
            path + "cameras", json={"draft": draft, "preview_sha256": plan["preview_sha256"]}
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["assembly"]["compatibility"]["status"] == "PASS"
        assert (
            client.post("/api/v1/scene_renders", json=saved.json()["render"]["request"]).status_code
            == 422
        )
        assert (
            client.get(
                path + "camera-template",
                params={
                    "scene_id": draft["scene_asset_id"],
                    "assembly_id": saved.json()["assembly"]["id"],
                },
            ).status_code
            == 200
        )


def test_small_sensor_gets_valid_native_crop_in_render_draft(tmp_path):
    service = PlatformService(tmp_path / "state")
    draft, *_ = setup(service)
    sensor = next(
        service.registry.get("models", i)
        for i in draft["assembly"]["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    fields = {k: copy.deepcopy(v) for k, v in sensor.items() if k in ModelCreate.model_fields}
    fields["properties"]["active_width"] = {"value": 0.03, "unit": "mm"}
    fields["properties"]["active_height"] = {"value": 0.024, "unit": "mm"}
    small = service.create_model(fields)
    draft["assembly"]["model_ids"] = [
        small["id"] if i == sensor["id"] else i for i in draft["assembly"]["model_ids"]
    ]
    result = scene_camera.preview(service, draft)
    assert result["status"] == "ready", result["issues"]
    saved = scene_camera.save(service, {"draft": draft, "preview_sha256": result["preview_sha256"]})
    assert saved["render"]["request"]["shape"] == [8, 10]


def test_factory_fixed_light_cannot_be_relabelled_by_scene_preparation(tmp_path):
    from camera_platform import factory

    service = PlatformService(tmp_path / "state")
    draft, original, _, model = setup(service)
    queued = factory.submit(service, factory.template(service, "prysm_psf_v1"))
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    job = service.registry.finish(job["id"], result=service.execute_run(job))
    # This response was explicitly bound to the old light and exposure conditions.
    fixed = factory.publish(
        service,
        {
            "job_id": job["id"],
            "base_lens_model_id": model["id"],
            "name": "Fixed old illumination",
            "revision": "1",
            "conditions": {**original["conditions"], "f_number": {"value": 8.0, "unit": "1"}},
        },
    )
    draft["assembly"]["model_ids"] = [
        fixed["id"] if i == model["id"] else i for i in draft["assembly"]["model_ids"]
    ]
    before = snapshots(service)
    result = scene_camera.preview(service, draft)
    assert result["status"] == "needs_input"
    assert any("light_id" in i["path"] or "light_id" in i["message"] for i in result["issues"])
    with pytest.raises(PreflightError):
        scene_camera.save(service, {"draft": draft, "preview_sha256": result["preview_sha256"]})
    assert snapshots(service) == before


@pytest.mark.parametrize(
    "fault,expected_message",
    [
        ("spectral_grid", "Lens and sensor wavelength coordinates"),
        ("angular_coverage", "lens angles exceed sensor angular response coverage"),
        ("readout_gain", "Fixed sensor gain"),
        ("readout_depth", "RAW format"),
        ("readout_black", "RAW format"),
        ("raw_cfa", "RAW format"),
        ("raw_layout", "RAW format"),
        ("raw_packing", "RAW format"),
        ("raw_streams", "RAW format"),
    ],
)
def test_declared_compatible_models_need_executable_camera_contract(
    tmp_path, fault, expected_message
):
    service = PlatformService(tmp_path / "state")
    draft, *_ = setup(service)
    models = {
        service.registry.get("models", i)["kind"]: service.registry.get("models", i)
        for i in draft["assembly"]["model_ids"]
    }
    changed = {}
    if fault == "spectral_grid":
        changed["lens"] = copy.deepcopy(models["lens"])
        response = changed["lens"]["payload"]
        # Identical spectral coverage still permits incompatible interior samples.
        response["wavelength_nm"] = [450.0, 510.0, 550.0, 600.0, 650.0]
        for field in ("angular_weights", "relative_illumination"):
            response[field] = [copy.deepcopy(response[field]) for _ in range(5)]
    elif fault.startswith("raw_"):
        key, value = {
            "raw_cfa": ("cfa", "BGGR"),
            "raw_layout": ("layout", "planar"),
            "raw_packing": ("packing", "packed12"),
            "raw_streams": ("hdr_streams", 2),
        }[fault]
        # All three declarations agree; the actual implemented readout cannot.
        draft["assembly"]["raw_format"][key] = value
        for kind, field in (("sensor", "raw_format"), ("isp", "input_raw_format")):
            changed[kind] = copy.deepcopy(models[kind])
            changed[kind][field][key] = value
    else:
        changed["sensor"] = copy.deepcopy(models["sensor"])
        response = changed["sensor"]["payload"]
        if fault == "angular_coverage":
            response["sensor_angular_response"]["theta_deg"][-1] = 25.0
        else:
            field, value = {
                "readout_gain": ("gain", 2.0),
                "readout_depth": ("bit_depth", 10),
                "readout_black": ("black_level_dn", 128.0),
            }[fault]
            response["readout"][field] = value
    for kind, changed_model in changed.items():
        fields = {k: v for k, v in changed_model.items() if k in ModelCreate.model_fields}
        fields["parents"] = [models[kind]["id"]]
        replacement = service.create_model(fields)
        draft["assembly"]["model_ids"] = [
            replacement["id"] if i == models[kind]["id"] else i
            for i in draft["assembly"]["model_ids"]
        ]
    before = snapshots(service)
    result = scene_camera.preview(service, draft)
    assert result["compatibility"]["status"] == "PASS", result["compatibility"]
    assert result["status"] == "needs_input"
    assert any(expected_message in i["message"] for i in result["issues"]), result["issues"]
    with pytest.raises(PreflightError):
        scene_camera.save(service, {"draft": draft, "preview_sha256": result["preview_sha256"]})
    assert snapshots(service) == before
    for model in models.values():
        assert service.registry.get("models", model["id"]) == model


def test_response_preflight_uses_bounded_crop_and_accepts_matching_spectral_lens(
    tmp_path, monkeypatch
):
    service = PlatformService(tmp_path / "state")
    draft, _, _, lens = setup(service)
    fields = {k: copy.deepcopy(v) for k, v in lens.items() if k in ModelCreate.model_fields}
    fields["parents"] = [lens["id"]]
    fields["payload"]["wavelength_nm"] = fields["payload"]["psf"]["wavelength_nm"]
    for field in ("angular_weights", "relative_illumination"):
        fields["payload"][field] = [copy.deepcopy(fields["payload"][field]) for _ in range(5)]
    spectral = service.create_model(fields)
    draft["assembly"]["model_ids"] = [
        spectral["id"] if i == lens["id"] else i for i in draft["assembly"]["model_ids"]
    ]
    original = spatial.SpatialSceneRuntime.validate_request
    inspected = []

    def bounded_runtime_check(runtime, request):
        import numpy as np

        image = np.asarray(request["scene"]["spectral_irradiance"])
        assert image.shape == (8, 8, 5)
        assert not image.any()
        assert request["scene"]["sampling"]["full_sensor_shape"] == [1440, 1920]
        assert request["scene"]["sampling"]["crop_origin"] == [0, 0]
        assert request["lens"]["wavelength_nm"] == request["wavelength_nm"]
        assert request["frames"] == 1
        inspected.append(True)
        return original(runtime, request)

    def no_execution(*args, **kwargs):
        pytest.fail("Readiness must not execute a synthetic camera or persist probe evidence")

    monkeypatch.setattr(spatial.SpatialSceneRuntime, "validate_request", bounded_runtime_check)
    monkeypatch.setattr(spatial.SpatialSceneRuntime, "execute", no_execution)
    monkeypatch.setattr(service.registry, "store_blob", no_execution)
    before = snapshots(service)
    result = scene_camera.preview(service, draft)
    assert inspected == [True]
    assert result["status"] == "ready", result["issues"]
    assert result["physical_validation"] == "not_established"
    assert snapshots(service) == before

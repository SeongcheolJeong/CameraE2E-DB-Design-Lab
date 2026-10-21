"""Reuse real saved radiance only when its ray geometry and evidence still match."""

import copy
import hashlib
import io
import zipfile

import numpy as np
import pytest
from pydantic import ValidationError
from test_scene_render import finish, scene_workflow  # noqa: F401

from camera_platform import imaging, scene_projection, scene_render
from camera_platform.contracts import AssemblyCreate, ModelCreate, SpectralImageImport
from camera_platform.demo import DOMAIN, PROVENANCE
from camera_platform.evidence import build_bundle
from camera_platform.projection_contracts import SceneProjectionCreate
from camera_platform.registry import ConflictError, IntegrityError, canonical, digest
from camera_platform.service import PreflightError


def array(service, job, name):
    artifact = next(a for a in job["artifacts"] if a["name"] == name)
    return np.load(
        io.BytesIO(service.registry.blob_path(artifact["sha256"]).read_bytes()), allow_pickle=False
    )


def snapshots(service):
    with service.registry.connection() as conn:
        return {
            table: list(map(tuple, conn.execute("SELECT * FROM " + table)))
            for table in ("records", "jobs", "audit")
        }


@pytest.fixture
def projection_source(scene_workflow, monkeypatch):  # noqa: F811
    service, assembly, request = scene_workflow
    request["projection"]["on_axis_transmittance"]["values"] = [
        0.0 for _ in request["projection"]["on_axis_transmittance"]["values"]
    ]
    source = finish(service, scene_render.submit(service, request))
    assert np.max(array(service, source, "spectral-radiance.npy")) > 0
    assert not np.any(array(service, source, "spectral-image.npy"))

    def forbidden():
        pytest.fail("Stored radiance projection must not probe or execute Mitsuba")

    monkeypatch.delenv("CAMERA_PLATFORM_MITSUBA_PYTHON")
    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    return service, assembly, source


def target_camera(service, source, *, lens_change=None, sensor_change=None, conditions=None):
    """Explicit response fixtures, never relabel a protected Factory model."""
    ids = []
    for identity in source["model_ids"]:
        model = service.registry.get("models", identity)
        mutate = (
            lens_change
            if model["kind"] == "lens"
            else sensor_change
            if model["kind"] == "sensor"
            else None
        )
        if mutate is not None or model["kind"] == "lens":
            data = {k: copy.deepcopy(model[k]) for k in ModelCreate.model_fields}
            data.update(
                name="Explicit projection target fixture",
                parents=[identity],
                source_kind="estimated",
                source_uri="fixture://projection-target",
                method_and_version="declared-response-assumption:1",
                factory_job_id=None,
                psf_factory_job_id=None,
                factory_application_conditions=None,
                factory_condition_scope="all_declared",
            )
            if model["kind"] == "lens":
                # Explicit synthetic response fixture declares its own domain;
                # the original Factory model's narrower domain remains untouched.
                data["valid_domain"] = copy.deepcopy(DOMAIN)
            if mutate:
                mutate(data)
            model = service.create_model(data)
        ids.append(model["id"])
    data = {k: copy.deepcopy(source[k]) for k in AssemblyCreate.model_fields}
    data.update(name="Stored-radiance target camera", model_ids=ids)
    if conditions:
        conditions(data["conditions"])
    return service.create_assembly(data)


def draft(service, source, target, throughput=0.6):
    result = scene_projection.template(service, source["id"], target["id"])["request"]
    result["projection"]["on_axis_transmittance"]["values"] = [
        throughput for _ in result["projection"]["on_axis_transmittance"]["values"]
    ]
    result["projection"].update(
        source_uri="fixture://explicit-projection-throughput",
        method_and_version="declared-throughput-for-test:1",
    )
    return result


def queue(service, payload):
    plan = scene_projection.preview(service, payload)
    assert plan["status"] == "ready", plan["issues"]
    return scene_projection.submit(
        service, {"draft": payload, "preview_sha256": plan["preview_sha256"]}
    )


def test_real_zero_throughput_source_can_recover_raw_without_renderer(projection_source):
    service, assembly, source = projection_source
    target = target_camera(
        service, assembly, conditions=lambda c: c.update(exposure_s={"value": 0.006, "unit": "s"})
    )
    before = snapshots(service)
    template = scene_projection.template(service, source["id"], target["id"])
    assert template["request"]["projection"]["on_axis_transmittance"]["values"] == [None] * 5
    payload = draft(service, source, target)
    plan = scene_projection.preview(service, payload)
    assert plan["status"] == "ready", plan["issues"]
    assert snapshots(service) == before
    assert plan["source_snapshot_sha256"] == digest(source)
    queued = queue(service, payload)
    assert queue(service, payload)["id"] == queued["id"]
    result = finish(service, queued)
    assert result["job_kind"] == scene_projection.JOB_KIND
    assert result["result"]["summary"]["renderer_invoked"] is False
    assert result["result"]["total_uncertainty"] == {"kind": "unknown"}
    expected = scene_render.project_radiance(
        array(service, source, "spectral-radiance.npy"), payload["projection"]
    )
    np.testing.assert_array_equal(array(service, result, "spectral-image.npy"), expected)
    assert expected.max() > 0
    np.testing.assert_array_equal(
        array(service, result, "irradiance-mc-standard-error.npy"),
        scene_render.project_radiance(
            array(service, source, "radiance-mc-standard-error.npy"), payload["projection"]
        ),
    )
    assert "not_model_or_total_uncertainty" in result["result"]["summary"]["mc_standard_error"]
    image = scene_projection.publish(service, {"job_id": result["id"], "name": "Reused scene"})
    assert image["projection_job_snapshot"]["id"] == result["id"]
    assert image["source_render_snapshot"] == source
    assert "render_job_snapshot" not in image
    lens = result["lens_snapshot"]["id"]
    assert image["projection_lens_model_id"] == lens
    assert image["conditions"] == target["conditions"]
    with pytest.raises(ValueError, match="pinned projection lens"):
        imaging._scene_input(service, image, lens_model_id=source["lens_snapshot"]["id"])
    test = service.create_test(imaging.spatial_test_template(service, target["id"], image["id"]))
    camera = finish(
        service,
        service.submit_run(
            {
                "assembly_id": target["id"],
                "test_id": test["id"],
                "plugin_id": "spatial_scene_v1",
                "scene_id": image["id"],
                "parameters": {"frames": 2, "seed": 717},
            }
        ),
    )
    assert array(service, camera, "raw_frames.npy").shape == (2, 8, 8)
    assert array(service, camera, "raw_frames.npy").std() > 1
    assert array(service, camera, "rgb_frames.npy").shape == (2, 8, 8, 3)
    for job, kind in [(result, "scene_projections"), (camera, "runs")]:
        with zipfile.ZipFile(build_bundle(service, kind, job["id"])) as bundle:
            for artifact in [*source["artifacts"], *result["artifacts"]]:
                contents = bundle.read("artifacts/" + artifact["sha256"] + "/" + artifact["name"])
                assert hashlib.sha256(contents).hexdigest() == artifact["sha256"]
    assert service.registry.get_run(source["id"]) == source
    assert service.registry.get("assemblies", assembly["id"]) == assembly


def test_quadrature_cell_mean_radiance_reprojects_into_raw_without_renderer(
    scene_workflow, monkeypatch,  # noqa: F811
):
    service, assembly, request = scene_workflow
    request["samples_per_pixel"] = 2
    request["source_pixel_quadrature"] = {
        "model": "native_sensor_area_midpoint_grid_v1", "grid_side": 2,
    }
    throughput = request["projection"]["on_axis_transmittance"]["values"]
    request["projection"]["on_axis_transmittance"]["values"] = [0.0] * len(throughput)
    source = finish(service, scene_render.submit(service, request))
    radiance = array(service, source, "spectral-radiance.npy")
    assert radiance.max() > 0
    assert np.count_nonzero(array(service, source, "spectral-image.npy")) == 0
    assert source["result"]["summary"]["source_pixel_quadrature"][
        "source_cell_representation"
    ] == "area_mean_radiance_assigned_to_native_cell_centre"

    def forbidden():
        pytest.fail("Reprojecting saved cell-mean radiance must not invoke Mitsuba")

    monkeypatch.delenv("CAMERA_PLATFORM_MITSUBA_PYTHON")
    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    payload = draft(service, source, assembly, throughput=0.6)
    result = finish(service, queue(service, payload))
    expected = radiance * (0.6 * np.pi / (4 * 8**2))
    np.testing.assert_allclose(array(service, result, "spectral-image.npy"), expected, rtol=1e-14)
    np.testing.assert_allclose(
        array(service, result, "irradiance-mc-standard-error.npy"),
        array(service, source, "radiance-mc-standard-error.npy")
        * (0.6 * np.pi / (4 * 8**2)),
        rtol=1e-14,
    )
    assert result["result"]["summary"]["source_pixel_quadrature"] == (
        source["result"]["summary"]["source_pixel_quadrature"]
    )
    assert result["result"]["summary"]["continuous_scene_psf_integration"] is False
    image = scene_projection.publish(service, {"job_id": result["id"], "name": "Cell mean reuse"})
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], image["id"]))
    camera = finish(service, service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 717},
    }))
    assert array(service, camera, "raw_frames.npy").shape == (1, 8, 8)
    assert array(service, camera, "rgb_frames.npy").shape == (1, 8, 8, 3)
    assert array(service, camera, "raw_frames.npy").std() > 0


def test_quadrature_reprojection_refuses_forged_sampling_evidence(
    scene_workflow,  # noqa: F811
):
    service, _, request = scene_workflow
    request.update(samples_per_pixel=2, source_pixel_quadrature={
        "model": "native_sensor_area_midpoint_grid_v1", "grid_side": 2,
    })
    source = finish(service, scene_render.submit(service, request))
    frozen = source["request"]
    summary = source["result"]["summary"]
    scene_projection._verify_renderer_sampling(frozen, summary)
    for path, value in (
        (("sampling",), "pixel_centre_pinhole_fixed_wavelength_path_samples"),
        (("source_pixel_quadrature", "grid_side"), 4),
        (("source_pixel_quadrature", "integrator_path_samples"), 1),
        (("source_pixel_quadrature", "sampler_seed"), 0),
        (("source_pixel_quadrature", "subcell_centres_yx"), [[0.5, 0.5]]),
        (("source_pixel_quadrature", "subcell_weight"), 1.0),
        (("source_pixel_quadrature", "source_cell_representation"), "unaveraged"),
        (("source_pixel_quadrature", "continuous_scene_psf_integration"), True),
        (("samples_per_pixel",), 3),
    ):
        forged = copy.deepcopy(summary)
        node = forged
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(ValueError, match="quadrature"):
            scene_projection._verify_renderer_sampling(frozen, forged)
    legacy = copy.deepcopy(frozen)
    legacy.pop("source_pixel_quadrature")
    with pytest.raises(ValueError, match="sampling"):
        scene_projection._verify_renderer_sampling(legacy, summary)

    # A new immutable record can carry internally consistent artifact/result
    # bytes and still falsely describe the original ray quadrature. Re-read it
    # through the public projection entry point, not just the pure helper.
    forged = copy.deepcopy(summary)
    forged["source_pixel_quadrature"]["sampler_seed"] += 1
    document = {
        key: copy.deepcopy(value) for key, value in source.items()
        if key not in {"id", "status", "cache_key", "content_hash", "created_at",
                       "updated_at", "result", "error", "artifacts"}
    }
    queued = service.registry.submit(document, digest(document), force=True)
    assert service.registry.claim()["id"] == queued["id"]
    false_result = copy.deepcopy(source["result"])
    false_result["summary"] = forged
    false_result["spectral_image_metadata"]["source_uri"] = (
        "scene-render://" + queued["id"]
    )
    summary_artifact = service.registry.store_blob(
        canonical(forged).encode(), "renderer-summary.json", "application/json"
    )
    false_result["artifacts"] = [
        summary_artifact if artifact["name"] == "renderer-summary.json" else artifact
        for artifact in false_result["artifacts"]
    ]
    forged_source = service.registry.finish(queued["id"], result=false_result)
    with pytest.raises(ValueError, match="quadrature"):
        scene_projection.template(service, forged_source["id"], source["assembly_id"])
    assert service.registry.get_run(source["id"]) == source


@pytest.mark.parametrize(
    "field",
    [
        "focal",
        "pitch",
        "full_shape",
        "wavelength",
        "temperature",
        "add_condition",
        "delete_condition",
    ],
)
def test_ray_grid_or_scene_condition_changes_require_new_render(projection_source, field):
    service, assembly, source = projection_source

    def lens_change(model):
        if field == "focal":
            model["properties"]["focal_length"]["value"] = 5.0

    def sensor_change(model):
        if field == "pitch":
            model["payload"]["pixel_pitch_um"] = 1.5
        elif field == "full_shape":
            model["properties"]["active_width"]["value"] = 3.84
        elif field == "wavelength":
            model["payload"]["wavelength_nm"][1] = 505.0

    def conditions(values):
        if field == "temperature":
            values["temperature_c"]["value"] = 26.0
        elif field == "add_condition":
            values["scene_variant"] = {"value": "different", "unit": "1"}
        elif field == "delete_condition":
            values.pop("light_id")

    target = target_camera(
        service,
        assembly,
        lens_change=lens_change,
        sensor_change=sensor_change if field in {"pitch", "full_shape", "wavelength"} else None,
        conditions=conditions,
    )
    payload = draft(service, source, target)
    plan = scene_projection.preview(service, payload)
    assert plan["status"] == "rerender_required", plan["issues"]
    before = snapshots(service)
    with pytest.raises(PreflightError):
        scene_projection.submit(
            service, {"draft": payload, "preview_sha256": plan["preview_sha256"]}
        )
    assert snapshots(service) == before


def test_aperture_uses_target_fixed_lens_and_not_original_irradiance(projection_source):
    service, assembly, source = projection_source
    target = target_camera(
        service,
        assembly,
        lens_change=lambda m: m["properties"].update(f_number={"value": 16.0, "unit": "1"}),
        conditions=lambda c: c.update(f_number={"value": 16.0, "unit": "1"}),
    )
    request = draft(service, source, target, 0.8)
    result = finish(service, queue(service, request))
    np.testing.assert_allclose(
        array(service, result, "spectral-image.npy"),
        array(service, source, "spectral-radiance.npy") * np.pi * 0.8 / (4 * 16**2),
        rtol=1e-12,
    )


def test_stale_preview_response_errors_and_unknown_throughput_are_not_saved(projection_source):
    service, assembly, source = projection_source
    target = target_camera(service, assembly)
    request = draft(service, source, target)
    plan = scene_projection.preview(service, request)
    request["name"] += " edited"
    before = snapshots(service)
    with pytest.raises(ConflictError, match="preview"):
        scene_projection.submit(
            service, {"draft": request, "preview_sha256": plan["preview_sha256"]}
        )
    request["projection"]["f_number"] = 4.0
    assert scene_projection.preview(service, request)["status"] == "needs_input"
    with pytest.raises(ValidationError):
        scene_projection.preview(
            service, scene_projection.template(service, source["id"], target["id"])["request"]
        )
    assert snapshots(service) == before


@pytest.mark.parametrize("name", ["pose", "shape", "crop_origin", "scene_asset_id", "sampling"])
def test_reprojection_cannot_override_frozen_scene_grid(name):
    payload = {
        "name": "Valid projection",
        "source_render_id": "run-source",
        "assembly_id": "camera",
        "projection": {
            **PROVENANCE,
            "projection_model": "paraxial_on_axis_projection_v1",
            "focal_length_mm": 4.0,
            "f_number": 8.0,
            "on_axis_transmittance": {"wavelength_nm": [450.0, 650.0], "values": [0.8, 0.8]},
            "geometry_model": "pinhole_pixel_centre",
            "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
            "approximation_uncertainty": "unknown",
        },
    }
    SceneProjectionCreate.model_validate(payload)
    with pytest.raises(ValidationError) as error:
        SceneProjectionCreate.model_validate({**payload, name: [0, 0]})
    assert [(e["type"], e["loc"]) for e in error.value.errors()] == [("extra_forbidden", (name,))]


def test_source_focal_metadata_is_preserved_without_rejecting_valid_unit_value(
    scene_workflow,  # noqa: F811
    monkeypatch,
):
    service, assembly, request = scene_workflow
    annotated = target_camera(
        service,
        assembly,
        lens_change=lambda m: m["properties"]["focal_length"].update(
            note="Original design annotation retained with the value"
        ),
    )
    request["assembly_id"] = annotated["id"]
    source = finish(service, scene_render.submit(service, request))
    original_hash = source["content_hash"]

    def forbidden():
        pytest.fail("Stored projection must not re-render a lens annotation")

    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    projected = finish(service, queue(service, draft(service, source, annotated)))
    frozen = projected["source_render_snapshot"]
    assert frozen == source and frozen["content_hash"] == original_hash
    assert frozen["lens_snapshot"]["properties"]["focal_length"]["note"].startswith("Original")
    assert service.registry.get_run(source["id"]) == source


@pytest.mark.parametrize("mode", ["add_null", "delete_null", "same_null", "same_invalid_quantity"])
def test_condition_presence_and_publication_contract_are_checked_before_queue(
    scene_workflow,  # noqa: F811
    monkeypatch,
    mode,
):
    service, original, request = scene_workflow
    bad = {"value": [1, 2], "unit": "1"} if mode == "same_invalid_quantity" else None
    if mode == "add_null":
        source_assembly = original
    else:
        source_assembly = target_camera(
            service, original, conditions=lambda c: c.update(scene_variant=bad)
        )
    request["assembly_id"] = source_assembly["id"]
    # Earlier rendering accepts opaque conditions. Projection publication must
    # validate them up front even when the original render completed successfully.
    source = finish(service, scene_render.submit(service, request))

    def change(values):
        if mode == "add_null":
            values["scene_variant"] = None
        elif mode == "delete_null":
            values.pop("scene_variant")

    target = target_camera(service, source_assembly, conditions=change)

    def forbidden():
        pytest.fail("Rejecting unpublishable conditions must not call a renderer")

    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    payload = draft(service, source, target)
    before = snapshots(service)
    plan = scene_projection.preview(service, payload)
    if mode in {"add_null", "delete_null"}:
        assert plan["status"] == "rerender_required", plan["issues"]
        assert any(p["code"] == "rerender_condition_changed" for p in plan["issues"])
    else:
        assert plan["status"] == "needs_input", plan["issues"]
    if mode != "delete_null":
        assert any(p["code"] == "invalid_projection_conditions" for p in plan["issues"])
    with pytest.raises(PreflightError):
        scene_projection.submit(
            service, {"draft": payload, "preview_sha256": plan["preview_sha256"]}
        )
    assert snapshots(service) == before


def test_corrupt_original_bytes_and_forged_publication_are_rejected(projection_source):
    service, assembly, source = projection_source
    request = draft(service, source, assembly)
    result = finish(service, queue(service, request))
    metadata = SpectralImageImport.model_validate(
        result["result"]["spectral_image_metadata"]
    ).model_dump()
    content = service.registry.blob_path(result["artifacts"][0]["sha256"]).read_bytes()
    invalid = copy.deepcopy(metadata)
    invalid["sampling"]["crop_origin"][0] += 1
    with pytest.raises(ValueError, match="physical contract"):
        scene_projection.publication_evidence(service, result["id"], invalid, content)
    old = next(a for a in source["artifacts"] if a["name"] == "spectral-image.npy")
    with pytest.raises(ValueError, match="new projection"):
        scene_projection.publication_evidence(
            service, result["id"], metadata, service.registry.blob_path(old["sha256"]).read_bytes()
        )
    artifact = next(a for a in source["artifacts"] if a["name"] == "spectral-radiance.npy")
    path = service.registry.blob_path(artifact["sha256"])
    path.write_bytes(b"corrupt saved radiance")
    with pytest.raises(IntegrityError):
        scene_projection.preview(service, request)
    with pytest.raises(IntegrityError):
        scene_projection.publication_evidence(service, result["id"], metadata, content)


def test_old_renderer_environment_does_not_need_current_mitsuba(projection_source):
    service, assembly, source = projection_source
    # A separately audited historical result with identical original numeric bytes
    # demonstrates environment separation without modifying an immutable record.
    frozen = {
        k: copy.deepcopy(v)
        for k, v in source.items()
        if k
        not in {
            "id",
            "status",
            "cache_key",
            "content_hash",
            "created_at",
            "updated_at",
            "result",
            "error",
            "artifacts",
        }
    }
    frozen["environment"]["platform_version"] = "historical-renderer-release"
    queued = service.registry.submit(frozen, digest(frozen), force=True)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    result = copy.deepcopy(source["result"])
    result["spectral_image_metadata"]["source_uri"] = "scene-render://" + queued["id"]
    historical = service.registry.finish(queued["id"], result=result)
    projected = finish(service, queue(service, draft(service, historical, assembly)))
    assert projected["environment"] == service.loaded_environment
    assert (
        projected["source_render_snapshot"]["environment"]["platform_version"]
        == "historical-renderer-release"
    )

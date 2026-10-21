"""Registered 3D subcell radiance to approximate PSF to RAW/ISP path."""

import copy
import io

import numpy as np
import pytest
from test_scene_render import finish, scene_workflow, spec  # noqa: F401

from camera_platform import imaging, scene_projection, scene_render, spatial, subcell_optics
from camera_platform.demo import PROVENANCE
from camera_platform.registry import canonical, digest


def _array(service, job, name):
    artifact = next(a for a in job["artifacts"] if a["name"] == name)
    return np.load(io.BytesIO(service.registry.blob_path(artifact["sha256"]).read_bytes()),
                   allow_pickle=False)


def _edge_scene(service, request):
    """Black foreground crosses one native source cell in the actual 3D ray grid."""
    assembly, sensor, pitch, full = imaging._native(service, request["assembly_id"])
    del assembly, sensor
    source_col = (full[1] - request["shape"][1]) // 2 + 4
    edge = (source_col + 0.30 - full[1] / 2) * pitch * 1e-3 / 4.0

    def asset(name, payload):
        return scene_render.create_asset(service, {**PROVENANCE, "name": name,
                                                   "payload": payload})

    mesh = asset("Subcell foreground edge", {
        "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
        "vertices": [[edge, -1.0, 1.0], [1.0, -1.0, 1.0],
                     [1.0, 1.0, 1.0], [edge, 1.0, 1.0]],
        "faces": [[0, 1, 2], [0, 2, 3]],
    })
    black = asset("Black occluder", {
        "kind": "material", "bsdf": "diffuse", "spectrum": spec([0, 0, 0]),
        "surface_state": "synthetic black foreground",
    })
    original = service.registry.get("scene_assets", request["scene_asset_id"])
    payload = copy.deepcopy(original["payload"])
    payload["instances"].append({
        "geometry_id": mesh["id"], "material_id": black["id"],
        "to_world": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    altered = asset("Plane with fractional foreground", payload)
    assert altered["illumination_id"] == original["illumination_id"]
    request["scene_asset_id"] = altered["id"]


def test_registered_retained_3d_subcells_reproject_into_raw_and_isp(
    scene_workflow, monkeypatch,  # noqa: F811
):
    service, assembly, request = scene_workflow
    _edge_scene(service, request)
    request.update(samples_per_pixel=2, source_pixel_quadrature={
        "model": subcell_optics.MODEL, "grid_side": 2,
    })
    request["projection"]["on_axis_transmittance"]["values"] = [0.0] * len(
        request["projection"]["on_axis_transmittance"]["values"]
    )
    source = finish(service, scene_render.submit(service, request))
    radiance = _array(service, source, "spectral-radiance.npy")
    subcells = _array(service, source, "source-subcell-radiance.npy")
    assert np.any(subcells[4, 4] != subcells[4, 4, 0, 0])
    subcell_optics.validate_subcells(subcells, radiance, 2)
    assert not np.any(_array(service, source, "spectral-image.npy"))

    def forbidden():
        pytest.fail("Stored source-subcell projection must not invoke Mitsuba")

    monkeypatch.delenv("CAMERA_PLATFORM_MITSUBA_PYTHON")
    monkeypatch.setattr(scene_render, "backend_identity", forbidden)
    draft = scene_projection.template(service, source["id"], assembly["id"])["request"]
    draft["projection"]["on_axis_transmittance"]["values"] = [0.6] * len(
        draft["projection"]["on_axis_transmittance"]["values"]
    )
    draft["projection"].update(
        source_uri="fixture://subcell-target-throughput",
        method_and_version="declared-throughput-for-test:1",
    )
    plan = scene_projection.preview(service, draft)
    assert plan["status"] == "ready", plan["issues"]
    queued = scene_projection.submit(service, {
        "draft": draft, "preview_sha256": plan["preview_sha256"],
    })
    projected = finish(service, queued)
    image = scene_projection.publish(service, {
        "job_id": projected["id"], "name": "Retained subcell source",
    })
    test = service.create_test(imaging.spatial_test_template(
        service, assembly["id"], image["id"]
    ))
    run = finish(service, service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 57},
    }))
    summary = run["result"]["summary"]
    assert summary["source_subcell_transfer"]["model"] == subcell_optics.TRANSFER
    assert summary["source_subcell_transfer"]["radiance_sha256"] == next(
        a["sha256"] for a in source["artifacts"]
        if a["name"] == "source-subcell-radiance.npy"
    )
    np.testing.assert_allclose(
        summary["flux_diagnostics"]["input_spectral_flux"],
        summary["flux_diagnostics"]["full_convolution_spectral_flux"], rtol=2e-12,
    )
    assert _array(service, run, "raw_frames.npy").shape == (1, 8, 8)
    assert _array(service, run, "rgb_frames.npy").shape == (1, 8, 8, 3)
    irradiance = _array(service, run, "source_subcell_irradiance.npy")
    psf = _array(service, run, "psf_intensity.npy")
    expected, _ = subcell_optics.transfer(irradiance, psf,
                                         image["sampling"]["pixel_pitch_um"],
                                         image["sampling"]["full_sensor_shape"],
                                         image["sampling"]["crop_origin"])
    np.testing.assert_allclose(_array(service, run, "blurred_spectral_irradiance.npy"),
                               expected, rtol=0, atol=2e-15)
    cell_mean, _ = spatial._convolve(_array(service, projected, "spectral-image.npy"),
                                    psf, image["sampling"]["pixel_pitch_um"])
    assert not np.allclose(expected, cell_mean, rtol=1e-4, atol=1e-15)

    # A new internally consistent record cannot substitute its retained
    # subcell artifact while continuing to claim the original cell mean.
    forged_doc = {
        key: copy.deepcopy(value) for key, value in source.items()
        if key not in {"id", "status", "cache_key", "content_hash", "created_at",
                       "updated_at", "result", "error", "artifacts"}
    }
    forged_queue = service.registry.submit(forged_doc, digest(forged_doc), force=True)
    assert service.registry.claim()["id"] == forged_queue["id"]
    forged_result = copy.deepcopy(source["result"])
    bad = subcells.copy()
    bad[4, 4, 0, 0, 0] += 1
    stream = io.BytesIO()
    np.save(stream, bad, allow_pickle=False)
    replacement = service.registry.store_blob(
        stream.getvalue(), "source-subcell-radiance.npy", "application/octet-stream"
    )
    forged_result["artifacts"] = [
        replacement if a["name"] == "source-subcell-radiance.npy" else a
        for a in forged_result["artifacts"]
    ]
    forged_result["spectral_image_metadata"]["source_uri"] = (
        "scene-render://" + forged_queue["id"]
    )
    forged = service.registry.finish(forged_queue["id"], result=forged_result)
    with pytest.raises(ValueError, match="native-cell mean"):
        scene_projection.template(service, forged["id"], assembly["id"])

    forged_queue = service.registry.submit(forged_doc, digest(forged_doc), force=True)
    assert service.registry.claim()["id"] == forged_queue["id"]
    false_result = copy.deepcopy(source["result"])
    false_summary = copy.deepcopy(false_result["summary"])
    false_summary["source_pixel_quadrature"]["fractional_source_psf_model"] = (
        "exact_continuous_psf"
    )
    false_result["summary"] = false_summary
    false_result["spectral_image_metadata"]["source_uri"] = (
        "scene-render://" + forged_queue["id"]
    )
    summary_artifact = service.registry.store_blob(
        canonical(false_summary).encode(), "renderer-summary.json", "application/json"
    )
    false_result["artifacts"] = [
        summary_artifact if a["name"] == "renderer-summary.json" else a
        for a in false_result["artifacts"]
    ]
    false_source = service.registry.finish(forged_queue["id"], result=false_result)
    with pytest.raises(ValueError, match="quadrature"):
        scene_projection.template(service, false_source["id"], assembly["id"])


def test_registered_retained_subcell_psf_halo_reaches_native_raw(scene_workflow):  # noqa: F811
    """The real renderer retains an enlarged source before a 31×31 PSF/RAW crop."""
    service, assembly, request = scene_workflow
    request.update(
        render_psf_halo=True,
        samples_per_pixel=2,
        source_pixel_quadrature={"model": subcell_optics.MODEL, "grid_side": 2},
    )
    source = finish(service, scene_render.submit(service, request))
    assert source["request"]["capture_crop"]["shape"] == [8, 8]
    assert source["request"]["shape"][0] > 8
    assert source["request"]["psf_halo"]["support_policy"] == (
        "native_sensor_clipped_subcell_shift_psf_support"
    )
    image = scene_render.publish(service, {
        "job_id": source["id"], "name": "Retained subcell PSF halo",
    })
    test = service.create_test(imaging.spatial_test_template(
        service, assembly["id"], image["id"]
    ))
    run = finish(service, service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 77},
    }))
    subcells = _array(service, run, "source_subcell_irradiance.npy")
    psf = _array(service, run, "psf_intensity.npy")
    assert psf.shape == (len(image["wavelength_nm"]), 31, 31)
    assert (np.prod(source["request"]["shape"]) * 4 * psf.shape[0]
            * 31**2 * 4 > 10_000_000)
    support, ledger = subcell_optics.transfer(
        subcells, psf, image["sampling"]["pixel_pitch_um"],
        image["sampling"]["full_sensor_shape"],
        image["sampling"]["crop_origin"],
    )
    capture = source["request"]["capture_crop"]
    origin = source["request"]["crop_origin"]
    row, col = [a - b for a, b in zip(capture["crop_origin"], origin, strict=True)]
    np.testing.assert_allclose(
        _array(service, run, "blurred_spectral_irradiance.npy"),
        support[row:row + 8, col:col + 8], rtol=1e-13, atol=1e-18,
    )
    np.testing.assert_allclose(
        ledger["input_spectral_flux"], ledger["full_convolution_spectral_flux"],
        rtol=1e-9,
    )
    assert run["result"]["summary"]["source_subcell_transfer"]["model"] == (
        subcell_optics.TRANSFER
    )
    assert _array(service, run, "raw_frames.npy").shape == (1, 8, 8)

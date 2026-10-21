"""Actual Factory -> render -> stored radiance -> RAW with one stop multiplier.

The shared Factory fixture is numerical execution, not a physical-camera claim.
Pure contract checks below deliberately do not assert Factory execution.
"""

from copy import deepcopy

import numpy as np
import pytest
from pydantic import ValidationError
from test_prescription_workflow import finish
from test_scene_projection import array, snapshots
from test_scene_render import PYTHON
from test_stop_transmission_workflow import stop_workflow as stop_workflow

from camera_platform import imaging, projection_radiometry, scene_projection, scene_render
from camera_platform.projection_contracts import SceneProjectionCreate
from camera_platform.registry import IntegrityError, digest
from camera_platform.scene_contracts import PrescriptionProjection, Projection
from camera_platform.scene_templates import render_template


def legacy():
    return {
        "projection_model": "paraxial_on_axis_projection_v1",
        "focal_length_mm": 4.0,
        "f_number": 8.0,
        "on_axis_transmittance": {"wavelength_nm": [500.0, 600.0], "values": [0.8, 0.6]},
        "geometry_model": "pinhole_pixel_centre",
        "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
        "approximation_uncertainty": "unknown",
        "source_kind": "estimated",
        "source_uri": "fixture://historical-explicit-throughput",
        "license": "MIT",
        "method_and_version": "declared-paraxial:1",
        "physical_instance": None,
        "parents": [],
    }


def test_legacy_import_union_serialization_and_exact_expression_are_preserved():
    old = legacy()
    assert Projection.model_validate(old).model_dump() == old
    request = {
        "name": "Legacy",
        "source_render_id": "source",
        "assembly_id": "camera",
        "projection": old,
        "force": False,
    }
    assert SceneProjectionCreate.model_validate(request).model_dump() == request
    radiance = np.arange(128, dtype=float).reshape(8, 8, 2) / 7
    expected = radiance * np.asarray([0.8, 0.6]) * (np.pi / (4 * 8.0**2))
    np.testing.assert_array_equal(scene_render.project_radiance(radiance, old), expected)


@pytest.fixture(scope="module")
def rendered_stop(stop_workflow):
    if not PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    service, scene, records = stop_workflow
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CAMERA_PLATFORM_MITSUBA_PYTHON", str(PYTHON))
        request = render_template(service, scene["id"], records[0]["assembly"]["id"])["request"]
        request.update(name="Actual clear-stop source radiance", shape=[8, 8], samples_per_pixel=2)
        source = finish(service, scene_render.submit(service, request))
        yield service, scene, records, request, source


def test_derived_projection_has_exact_original_bindings_and_no_extra_factors(stop_workflow):
    service, scene, records = stop_workflow
    for item in records:
        before = snapshots(service)
        draft = render_template(service, scene["id"], item["assembly"]["id"])["request"][
            "projection"
        ]
        assert snapshots(service) == before
        assert draft["lens_model_sha256"] == digest(item["lens"])
        assert draft["factory_job_sha256"] == digest(item["ray_job"])
        assert draft["psf_factory_job_sha256"] == digest(item["psf_job"])
        assert draft["stop_transmission_sha256"] == digest(
            item["ray_job"]["request"]["stop_transmission"]
        )
        assert "on_axis_transmittance" not in draft
        expected = np.asarray(item["lens"]["payload"]["radiometry"]["irradiance_per_radiance_sr"])[
            :, 0
        ]
        np.testing.assert_array_equal(
            draft["on_axis_irradiance_per_radiance_sr"]["values"], expected
        )
        np.testing.assert_array_equal(
            scene_render.project_radiance(np.ones((8, 8, 2)), draft),
            np.broadcast_to(expected, (8, 8, 2)),
        )
        with pytest.raises(ValidationError):
            PrescriptionProjection.model_validate(
                {**draft, "on_axis_transmittance": legacy()["on_axis_transmittance"]}
            )


@pytest.mark.parametrize("field", ["coefficient", "factory_job_sha256", "stop_transmission_sha256"])
def test_changed_values_or_evidence_hashes_are_rejected_before_queue(stop_workflow, field):
    service, scene, records = stop_workflow
    item = records[1]
    request = render_template(service, scene["id"], item["assembly"]["id"])["request"]
    if field == "coefficient":
        request["projection"]["on_axis_irradiance_per_radiance_sr"]["values"][0] *= 0.5
    else:
        request["projection"][field] = "0" * 64
    before = snapshots(service)
    with pytest.raises(ValueError, match="exact Lens/Factory/PSF/stop"):
        scene_render.submit(service, request)
    assert snapshots(service) == before


def test_even_clear_v2_lens_cannot_ignore_its_stop_via_legacy_projection(stop_workflow):
    service, scene, records = stop_workflow
    item = records[0]
    request = render_template(service, scene["id"], item["assembly"]["id"])["request"]
    projection = legacy()
    projection.update(
        focal_length_mm=request["projection"]["focal_length_mm"],
        f_number=request["projection"]["f_number"],
    )
    request["projection"] = projection
    before = snapshots(service)
    with pytest.raises(ValueError, match="requires prescription radiometry projection"):
        scene_render.submit(service, request)
    assert snapshots(service) == before


def test_actual_render_reprojection_publication_and_raw_preserve_one_stop_factor(rendered_stop):
    service, _, records, request, source = rendered_stop
    target = records[1]["assembly"]
    projection = scene_projection.template(service, source["id"], target["id"])["request"]
    # Reuse the actual new-schema source without invoking its renderer again.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            scene_render, "backend_identity", lambda: pytest.fail("Reprojection invoked renderer")
        )
        plan = scene_projection.preview(service, projection)
        assert plan["status"] == "ready", plan["issues"]
        queued = scene_projection.submit(
            service, {"draft": projection, "preview_sha256": plan["preview_sha256"]}
        )
        projected = finish(service, queued)
        assert (
            scene_projection.submit(
                service, {"draft": projection, "preview_sha256": plan["preview_sha256"]}
            )["id"]
            == projected["id"]
        )
        image = scene_projection.publish(
            service, {"job_id": projected["id"], "name": "Quarter-stop image"}
        )
        assert (
            scene_projection.publish(
                service, {"job_id": projected["id"], "name": "Quarter-stop image"}
            )
            == image
        )
    np.testing.assert_allclose(
        array(service, projected, "spectral-image.npy"),
        array(service, source, "spectral-image.npy") * 0.25,
        rtol=1e-12,
    )
    coefficient = np.asarray(
        projection["projection"]["on_axis_irradiance_per_radiance_sr"]["values"]
    )
    np.testing.assert_array_equal(
        array(service, projected, "irradiance-mc-standard-error.npy"),
        array(service, source, "radiance-mc-standard-error.npy") * coefficient,
    )
    assert image["projection_lens_model_id"] == records[1]["lens"]["id"]
    with pytest.raises(ValueError, match="pinned projection lens"):
        imaging._scene_input(service, image, lens_model_id=records[0]["lens"]["id"])
    output_means = []
    for assembly, saved in [
        (
            records[0]["assembly"],
            scene_render.publish(service, {"job_id": source["id"], "name": "Clear-stop image"}),
        ),
        (target, image),
    ]:
        test = service.create_test(
            imaging.spatial_test_template(service, assembly["id"], saved["id"])
        )
        run = finish(
            service,
            service.submit_run(
                {
                    "assembly_id": assembly["id"],
                    "test_id": test["id"],
                    "plugin_id": "spatial_scene_v1",
                    "scene_id": saved["id"],
                    "parameters": {"frames": 2, "seed": 791},
                }
            ),
        )
        assert array(service, run, "raw_frames.npy").shape == (2, 8, 8)
        output_means.append(array(service, run, "mean_electrons.npy"))
    np.testing.assert_allclose(output_means[1], output_means[0] * 0.25, rtol=1e-12, atol=1e-12)
    assert projected["result"]["summary"]["renderer_invoked"] is False
    assert projected["projection_evidence"]["factory_executed_for_projection"] is False
    assert service.registry.get_run(source["id"]) == source
    assert source["request"]["projection"] == request["projection"]


def test_missing_factory_original_blocks_template_cached_render_and_publication(rendered_stop):
    service, scene, records, request, source = rendered_stop
    artifact = next(
        a
        for a in records[0]["ray_job"]["artifacts"]
        if a["name"] == "irradiance_per_radiance_sr.npy"
    )
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    try:
        path.write_bytes(b"corrupt original E/L")
        for operation in (
            lambda: render_template(service, scene["id"], records[0]["assembly"]["id"]),
            lambda: scene_render.submit(service, request),
            lambda: scene_render.publish(
                service, {"job_id": source["id"], "name": "Refuse stale evidence"}
            ),
        ):
            with pytest.raises(IntegrityError):
                operation()
    finally:
        path.write_bytes(original)


def test_cached_render_output_and_frozen_projection_evidence_are_checked(rendered_stop):
    service, _, _, request, source = rendered_stop
    artifact = next(a for a in source["artifacts"] if a["name"] == "spectral-image.npy")
    path, original = service.registry.blob_path(artifact["sha256"]), None
    original = path.read_bytes()
    try:
        path.write_bytes(b"corrupt cached irradiance")
        with pytest.raises(IntegrityError):
            scene_render.submit(service, request)
    finally:
        path.write_bytes(original)
    changed = deepcopy(source)
    changed["projection_evidence"]["factory_executed_for_projection"] = True
    with pytest.raises(ValueError, match="Frozen projection evidence"):
        projection_radiometry.verify_job(service, changed)
    assert scene_render.submit(service, request)["id"] == source["id"]

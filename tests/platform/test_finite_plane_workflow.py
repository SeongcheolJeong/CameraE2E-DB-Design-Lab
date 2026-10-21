"""Finite object chart, immutable optical evidence, sensor calculation and refusals."""

import io
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import factory, finite_plane, imaging, prescription_psf, projection_radiometry
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import seed
from camera_platform.finite_conjugate import application_conditions
from camera_platform.registry import IntegrityError
from camera_platform.service import PlatformService


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def finish(service, queued):
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    return service.registry.finish(claimed["id"], result=service.execute_run(claimed))


def build_camera(service, distance=500.0):
    original = next(a for a in service.registry.list("assemblies")
                    if "object_distance_mm" not in a["conditions"])
    request = prescription_psf.request_template(version=5)
    request["prescription"]["object_conjugate"]["distance_mm"] = distance
    request["field_x_mm"] = request["field_y_mm"] = [-0.03, 0.0, 0.03]
    conditions = {
        **original["conditions"],
        **application_conditions(request["prescription"]["object_conjugate"]),
    }
    payload = factory.template(service, "rayoptics_lens_v1", request_version=5)
    payload["request"] = request["prescription"]
    radiometry = finish(service, factory.submit(service, payload))
    assert radiometry["result"]["summary"]["status"] == "PROPOSED"
    parent = factory.publish(
        service,
        {
            "job_id": radiometry["id"],
            "name": f"Finite {distance} RI",
            "revision": "1",
            "conditions": conditions,
        },
    )
    payload = factory.template(service, "prescription_psf_v1", request_version=5)
    payload["request"] = request
    psf = finish(service, factory.submit(service, payload))
    assert psf["result"]["summary"]["status"] == "PROPOSED", psf["result"]["summary"]
    conditions["f_number"] = {
        "value": psf["result"]["summary"]["geometric_reference_f_number"],
        "unit": "1",
    }
    lens = factory.publish(
        service,
        {
            "job_id": psf["id"],
            "name": f"Finite {distance} PSF",
            "revision": "1",
            "conditions": conditions,
            "base_lens_model_id": parent["id"],
            "condition_scope": "optical_response",
        },
    )
    source_sensor = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    sensor = fields(source_sensor, ModelCreate)
    sensor.update(name="Authored 16px spectral sensor", parents=[source_sensor["id"]])
    sensor["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    sensor["payload"]["wavelength_nm"] = [500, 600]
    sensor["payload"]["qe"] = [[row[1], row[3]] for row in sensor["payload"]["qe"]]
    response = sensor["payload"]["sensor_angular_response"]
    response["relative_response"] = [[row[1], row[3]] for row in response["relative_response"]]
    for key in ("active_height", "active_width"):
        sensor["properties"][key] = {"value": 0.048, "unit": "mm"}
    sensor = service.create_model(sensor)
    assembly = fields(original, AssemblyCreate)
    assembly.update(
        name=f"Finite {distance} camera",
        conditions=conditions,
        spectral_range=sensor["spectral_range"],
        model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
    )
    assembly = service.create_assembly(assembly)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
    return assembly, lens, parent, psf, radiometry


@pytest.fixture(scope="module")
def workflow(tmp_path_factory):
    service = PlatformService(tmp_path_factory.mktemp("finite-plane"))
    seed(service)
    cameras = [build_camera(service, distance) for distance in [500.0, 600.0]]
    return service, cameras


def array(service, record, name):
    artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def test_physical_chart_sign_and_no_duplicate_inverse_square_loss():
    request = finite_plane.PlaneFixtureCreate(
        assembly_id="test", object_distance_mm=100.0, reference_magnification=-0.2
    ).model_dump()
    sampling = {"full_sensor_shape": [16, 16], "crop_origin": [0, 0], "pixel_pitch_um": 3.0}
    xy, radiance, irradiance = finite_plane._compute(
        request, sampling, [500, 600], np.array([0.002, 0.003])
    )
    assert xy[0, 0].tolist() == pytest.approx([0.1125, 0.1125])
    assert xy[-1, -1].tolist() == pytest.approx([-0.1125, -0.1125])
    np.testing.assert_array_equal(irradiance, radiance * [0.002, 0.003])
    moved = finite_plane._compute(
        {**request, "object_distance_mm": 600.0}, sampling, [500, 600], np.array([0.002, 0.003])
    )
    for before, after in zip((xy, radiance, irradiance), moved, strict=True):
        np.testing.assert_array_equal(before, after)


def test_finite_plane_to_actual_sensor_changes_with_distance(workflow):
    service, cameras = workflow
    runs = []
    for assembly, lens, _, _, _ in cameras:
        request = finite_plane.template(service, assembly["id"])
        image = finite_plane.create(service, request)
        finite_plane.verify_scene(service, image, lens)
        test = service.create_test(
            imaging.spatial_test_template(service, assembly["id"], image["id"])
        )
        run = finish(
            service,
            service.submit_run(
                {
                    "assembly_id": assembly["id"],
                    "test_id": test["id"],
                    "scene_id": image["id"],
                    "plugin_id": "spatial_scene_v1",
                    "parameters": {"frames": 2, "seed": 73},
                }
            ),
        )
        assert run["result"]["physical_validation"] == "not_established"
        assert array(service, run, "raw_frames").shape == (2, 16, 16)
        runs.append((run, image))
    np.testing.assert_array_equal(
        array(service, runs[0][1], "spectral-radiance"),
        array(service, runs[1][1], "spectral-radiance"),
    )
    # Same pupil, fixed sensor position and ideal source: the actual finite OPD
    # changes sensor irradiance, not merely metadata or acquisition noise.
    a, b = [array(service, run, "blurred_spectral_irradiance") for run, _ in runs]
    assert np.max(np.abs(a - b)) > 1e-8
    assert not np.array_equal(*[array(service, run, "raw_frames") for run, _ in runs])


def test_mismatched_plane_and_3d_reuse_refused(workflow):
    service, cameras = workflow
    assembly, lens, parent, _, radiometry = cameras[0]
    request = finite_plane.template(service, assembly["id"])
    for key, value in (("object_distance_mm", 600.0), ("reference_magnification", -0.03)):
        with pytest.raises(ValueError, match="distance/magnification"):
            finite_plane.create(service, {**request, key: value})
    with pytest.raises(ValueError, match="mixed-depth"):
        projection_radiometry.model_projection(service, lens)
    with pytest.raises(ValueError, match="object distance"):
        factory.publish(
            service,
            {
                "job_id": radiometry["id"],
                "name": "invalid depth",
                "revision": "1",
                "conditions": {
                    k: v for k, v in assembly["conditions"].items() if k != "object_distance_mm"
                },
            },
        )
    wrong = deepcopy(parent)
    wrong["factory_application_conditions"]["object_distance_mm"]["value"] = 100.0
    with pytest.raises(ValueError, match="object distance"):
        factory.verify_model_link(service, wrong)


def test_tampering_and_generic_input_cannot_bypass_conjugate(workflow):
    service, cameras = workflow
    assembly, lens, *_ = cameras[0]
    image = finite_plane.create(service, finite_plane.template(service, assembly["id"]))
    altered = deepcopy(image)
    altered["conditions"]["object_distance_mm"]["value"] = 600.0
    with pytest.raises(ValueError, match="metadata"):
        finite_plane.verify_scene(service, altered, lens)
    artifact = next(a for a in image["artifacts"] if a["name"] == "object-plane-xy-mm.npy")
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    try:
        path.write_bytes(b"corrupted coordinate evidence")
        with pytest.raises(IntegrityError):
            finite_plane.verify_scene(service, image, lens)
    finally:
        path.write_bytes(original)
    generic = imaging.create_fixture(service, {"assembly_id": assembly["id"], "shape": [16, 16]})
    with pytest.raises(ValueError, match="conjugate-bound"):
        imaging._scene_input(service, generic, lens_model_id=lens["id"])
    metadata = imaging.spectral_template(service, assembly["id"])
    metadata["source_uri"] = image["source_uri"]
    stream = io.BytesIO()
    np.save(stream, array(service, image, "spectral-image"), allow_pickle=False)
    with pytest.raises(ValueError, match="creation path"):
        imaging.import_spectral(service, metadata, stream.getvalue())

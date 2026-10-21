"""Passive stop power survives publication and reaches camera electrons once."""

import io
from copy import deepcopy

import numpy as np
import pytest
from test_prescription_workflow import fields, finish

from camera_platform import factory, imaging, prescription_psf, scene_render
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE, seed
from camera_platform.registry import IntegrityError, digest
from camera_platform.scene_templates import render_template
from camera_platform.service import PlatformService
from camera_platform.spatial import SpatialSceneRuntime


@pytest.fixture(scope="session")
def stop_workflow(tmp_path_factory):
    service = PlatformService(tmp_path_factory.mktemp("stop-power-workflow"))
    seed(service)
    original = service.registry.list("assemblies")[0]
    sensor = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    sensor_data = fields(sensor, ModelCreate)
    sensor_data.update(name="Authored two-band native sensor", parents=[sensor["id"]])
    sensor_data["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    sensor_data["payload"]["wavelength_nm"] = [500, 600]
    sensor_data["payload"]["qe"] = [[row[1], row[3]] for row in sensor_data["payload"]["qe"]]
    angular = sensor_data["payload"]["sensor_angular_response"]
    angular["relative_response"] = [[row[1], row[3]] for row in angular["relative_response"]]
    for key in ("active_height", "active_width"):
        sensor_data["properties"][key] = {"value": 0.384, "unit": "mm"}
    sensor = service.create_model(sensor_data)
    light = scene_render.create_asset(
        service,
        {
            **PROVENANCE,
            "name": "Uniform spectral radiance",
            "payload": {
                "kind": "light",
                "distribution": "constant_environment",
                "spectrum": {"wavelength_nm": [500, 600], "values": [0.2, 0.2]},
                "unit": "W/m^2/sr/nm",
                "time_model": "static",
            },
        },
    )
    scene = scene_render.create_asset(
        service,
        {
            **PROVENANCE,
            "name": "Uniform environment",
            "payload": {
                "kind": "scene",
                "coordinate_system": "right_handed_y_up_metres",
                "instances": [],
                "environment_light_id": light["id"],
                "point_lights": [],
            },
        },
    )
    records = []
    for label, power in (("clear", 1.0), ("quarter", 0.25)):
        request = prescription_psf.request_template(version=2)
        mask = request["prescription"]["stop_transmission"]
        mask["power_transmittance"] = [[power] * len(mask["normalized_radius"])] * 2
        mask["source"]["description"] = "명시한 수동 투과율 " + label
        psf_input = factory.template(service, "prescription_psf_v1", request_version=2)
        psf_input.update(name=label + " prescription PSF", request=request)
        psf_job = finish(service, factory.submit(service, psf_input))
        assert psf_job["result"]["summary"]["status"] == "PROPOSED"
        conditions = {
            **original["conditions"],
            "f_number": {
                "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
                "unit": "1",
            },
            "light_id": {"value": scene["illumination_id"], "unit": "1"},
        }
        ray_input = factory.template(service, "rayoptics_lens_v1", request_version=2)
        ray_input.update(name=label + " absolute radiometry", request=request["prescription"])
        ray_job = finish(service, factory.submit(service, ray_input))
        assert ray_job["result"]["summary"]["status"] == "PROPOSED"
        parent = factory.publish(
            service,
            {
                "job_id": ray_job["id"],
                "name": label + " RI",
                "revision": "1",
                "conditions": conditions,
            },
        )
        publication = {
            "job_id": psf_job["id"],
            "base_lens_model_id": parent["id"],
            "name": label + " matched lens",
            "revision": "1",
            "conditions": conditions,
            "condition_scope": "optical_response",
        }
        lens = factory.publish(service, publication)
        camera = fields(original, AssemblyCreate)
        camera.update(
            name=label + " stop camera",
            model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
            conditions=conditions,
            spectral_range=sensor_data["spectral_range"],
        )
        assembly = service.create_assembly(camera)
        assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
        records.append(
            {
                "lens": lens,
                "parent": parent,
                "ray_job": ray_job,
                "psf_job": psf_job,
                "publication": publication,
                "assembly": assembly,
            }
        )
    return service, scene, records


def test_uniform_transmission_preserves_conditional_psf_and_scales_photons_once(stop_workflow):
    service, scene, records = stop_workflow
    outputs = []
    for item in records:
        lens, assembly = item["lens"], item["assembly"]
        response = lens["payload"]["radiometry"]
        assert response["prescription_sha256"] == digest(item["ray_job"]["request"])
        assert response["stop_transmission_sha256"] == digest(response["stop_transmission"])
        prepared = render_template(service, scene["id"], assembly["id"])
        assert prepared["issues"] == []
        projection = prepared["request"]["projection"]
        assert projection["projection_model"] == "prescription_radiometry_projection_v1"
        assert "on_axis_transmittance" not in projection
        image = scene_render.project_radiance(np.full((16, 16, 2), 0.2), projection)
        buffer = io.BytesIO()
        np.save(buffer, image, allow_pickle=False)
        metadata = imaging.spectral_template(service, assembly["id"])
        metadata.update(
            name=lens["name"] + " explicit projected image",
            source_uri="fixture://verified-radiometry-uniform-radiance",
            method_and_version="explicit-scalar-projection-unit-check:1",
        )
        metadata["sampling"]["crop_origin"] = [56, 56]
        registered = imaging.import_spectral(service, metadata, buffer.getvalue())
        test = service.create_test(
            imaging.spatial_test_template(service, assembly["id"], registered["id"])
        )
        run = finish(
            service,
            service.submit_run(
                {
                    "assembly_id": assembly["id"],
                    "test_id": test["id"],
                    "scene_id": registered["id"],
                    "plugin_id": "spatial_scene_v1",
                    "parameters": {"frames": 2, "seed": 71},
                }
            ),
        )
        artifact = next(a for a in run["artifacts"] if a["name"] == "mean_electrons.npy")
        mean = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
        # Independent noiseless readout check complements the actual queued noisy run.
        request = deepcopy(run["request"])
        request["scene"]["spectral_irradiance"] = image
        request["noise_enabled"] = False
        noiseless = SpatialSceneRuntime().execute(request)
        outputs.append((image, mean, noiseless["arrays"]["raw_frames"]))
    clear, quarter = [r["lens"]["payload"] for r in records]
    np.testing.assert_allclose(quarter["psf"]["values"], clear["psf"]["values"], atol=1e-14)
    np.testing.assert_allclose(quarter["angular_weights"], clear["angular_weights"], atol=1e-14)
    np.testing.assert_allclose(
        quarter["relative_illumination"], clear["relative_illumination"], atol=1e-14
    )
    for column in (0, 1):
        np.testing.assert_allclose(
            outputs[1][column], outputs[0][column] * 0.25, rtol=1e-12, atol=1e-12
        )
    sensor = next(
        service.registry.get("models", i)
        for i in records[0]["assembly"]["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    readout = sensor["payload"]["readout"]
    dark = (
        readout["dark_current_e_per_s"]
        * records[0]["assembly"]["conditions"]["exposure_s"]["value"]
    )
    scale = (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"]) / readout["full_well_e"]
    pedestal = readout["black_level_dn"] + dark * scale
    np.testing.assert_allclose(
        outputs[1][2] - pedestal, (outputs[0][2] - pedestal) * 0.25, atol=1.0
    )


def test_different_stop_parent_and_independent_pupil_are_refused(stop_workflow):
    service, _, (clear, quarter) = stop_workflow
    with pytest.raises(ValueError, match="same prescription"):
        factory.publish(
            service, {**quarter["publication"], "base_lens_model_id": clear["parent"]["id"]}
        )
    forged = deepcopy(quarter["psf_job"])
    forged["plugin_id"] = "prysm_psf_v1"
    with pytest.raises(ValueError, match="same prescription"):
        factory.psf_candidate_fields(
            forged, quarter["parent"], quarter["publication"]["conditions"], service=service
        )


@pytest.mark.parametrize("mutation", ["factor", "mask", "strip_authority"])
def test_actionable_radiometry_cannot_be_relabelled_or_edited(stop_workflow, mutation):
    service, _, records = stop_workflow
    data = fields(records[1]["lens"], ModelCreate)
    if mutation == "factor":
        data["payload"]["radiometry"]["irradiance_per_radiance_sr"][0][0] *= 0.5
    elif mutation == "mask":
        data["payload"]["radiometry"]["stop_transmission"]["source"]["description"] += " modified"
    else:
        data.update(
            psf_factory_job_id=None,
            factory_application_conditions=None,
            factory_condition_scope="all_declared",
            source_uri="fixture://forged",
            method_and_version="forged:1",
        )
    with pytest.raises(ValueError, match="preserved PSF|Factory provenance"):
        service.create_model(data)


def test_corrupt_absolute_array_blocks_new_publication(stop_workflow):
    service, _, records = stop_workflow
    item = records[1]
    artifact = next(
        a for a in item["ray_job"]["artifacts"] if a["name"] == "irradiance_per_radiance_sr.npy"
    )
    path = service.registry.blob_path(artifact["sha256"])
    saved = path.read_bytes()
    try:
        path.write_bytes(b"corrupt absolute optical throughput")
        with pytest.raises(IntegrityError):
            factory.publish(service, item["publication"])
    finally:
        path.write_bytes(saved)


def test_http_templates_explicitly_select_version_without_changing_legacy(tmp_path):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app

    with TestClient(create_app(tmp_path / "template-api", start_worker=False)) as client:
        for plugin, schema in (
            ("rayoptics_lens_v1", "lens_prescription"),
            ("prescription_psf_v1", "prescription_field_psf"),
        ):
            old = client.get("/api/v1/templates/factory", params={"plugin_id": plugin})
            new = client.get(
                "/api/v1/templates/factory",
                params={
                    "plugin_id": plugin,
                    "request_version": 2,
                },
            )
            assert old.status_code == new.status_code == 200
            assert old.json()["request"]["schema"] == schema + "_v1"
            assert new.json()["request"]["schema"] == schema + "_v2"
            factory.adapter(plugin).validate_request(new.json()["request"])
        refused = client.get(
            "/api/v1/templates/factory",
            params={
                "plugin_id": "prysm_psf_v1",
                "request_version": 2,
            },
        )
        assert refused.status_code == 422

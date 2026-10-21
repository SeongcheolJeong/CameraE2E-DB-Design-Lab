"""Same-prescription radiometry and wavefront evidence must reach actual RAW/RGB."""

import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import factory, imaging, prescription_psf
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import seed
from camera_platform.evidence import build_bundle
from camera_platform.registry import IntegrityError, digest
from camera_platform.service import PlatformService


def finish(service, queued):
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    return service.registry.finish(claimed["id"], result=service.execute_run(claimed))


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


@pytest.fixture(scope="module", params=[1, 4])
def optical_workflow(tmp_path_factory, request):
    version = request.param
    service = PlatformService(tmp_path_factory.mktemp("prescription-workflow"))
    seed(service)
    original = service.registry.list("assemblies")[0]
    request = prescription_psf.request_template(version=version)
    # Authored small-aperture fixture, not a vendor lens. These explicit clear
    # apertures enclose the transmitted bundle; RI quadrature covers its actual
    # 0.75mm image radius. No numerical acceptance thresholds are relaxed.
    request["prescription"]["image_height_mm"] = [0, 0.375, 0.75]
    for surface in request["prescription"]["surfaces"][1:]:
        surface["semi_diameter_mm"] = 0.8
    request["prescription"]["sampling"].update(polar_rings=44, azimuth_samples=44)
    radiometry = factory.template(service, "rayoptics_lens_v1", request_version=version)
    radiometry["request"] = request["prescription"]
    radiometry = finish(service, factory.submit(service, radiometry))
    assert radiometry["result"]["summary"]["status"] == "PROPOSED"
    parent = factory.publish(
        service,
        {
            "job_id": radiometry["id"],
            "name": "Same prescription RI/angles",
            "revision": "1",
            "conditions": original["conditions"],
        },
    )
    if version == 4:
        # The OPD calculation has its own numerical grid. Its physical lens
        # remains identical to the independently converged ray-power parent.
        request["prescription"]["sampling"].update(polar_rings=20, azimuth_samples=20)
    payload = factory.template(service, "prescription_psf_v1", request_version=version)
    payload["request"] = request
    job = finish(service, factory.submit(service, payload))
    assert job["result"]["summary"]["status"] == "PROPOSED"
    conditions = {
        **original["conditions"],
        "f_number": {
            "value": job["result"]["summary"]["geometric_reference_f_number"],
            "unit": "1",
        },
    }
    publication = {
        "job_id": job["id"],
        "base_lens_model_id": parent["id"],
        "name": "Prescription field optics",
        "revision": "1",
        "conditions": conditions,
        "condition_scope": "optical_response",
    }
    model = factory.publish(service, publication)
    sensor = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    data = fields(sensor, ModelCreate)
    data.update(name="Declared 128px sensor", parents=[sensor["id"]])
    data["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    data["payload"]["wavelength_nm"] = [500, 600]
    data["payload"]["qe"] = [[row[1], row[3]] for row in data["payload"]["qe"]]
    response = data["payload"]["sensor_angular_response"]
    response["relative_response"] = [[row[1], row[3]] for row in response["relative_response"]]
    for key in ("active_height", "active_width"):
        data["properties"][key] = {"value": 0.384, "unit": "mm"}
    sensor = service.create_model(data)
    a = fields(original, AssemblyCreate)
    a.update(
        name="Prescription optics camera",
        conditions=conditions,
        spectral_range=data["spectral_range"],
    )
    a["model_ids"] = [model["id"], sensor["id"], original["model_ids"][2]]
    assembly = service.create_assembly(a)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
    return service, assembly, parent, model, job, radiometry, publication


def test_real_two_factories_to_raw_replay_and_complete_evidence(optical_workflow):
    service, assembly, parent, model, job, radiometry, _ = optical_workflow
    if job["request"]["schema"] == "prescription_field_psf_v4":
        binding = model["properties"]["physical_prescription_binding"]
        assert binding["schema"] == "camera_lens_physical_prescription_binding_v1"
        assert binding["radiometry_request_sha256"] == digest(radiometry["request"])
        assert binding["psf_prescription_request_sha256"] == digest(
            job["request"]["prescription"]
        )
        assert radiometry["request"]["sampling"] != job["request"]["prescription"]["sampling"]
    else:
        assert "physical_prescription_binding" not in model["properties"]
    assert model["source_kind"] == "simulated"
    assert model["factory"]["method"] == "prescription_psf_v1"
    assert model["payload"]["field_reference_radius_mm"] == 0.75
    image = imaging.create_fixture(service, {"assembly_id": assembly["id"]})
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], image["id"]))
    run = finish(
        service,
        service.submit_run(
            {
                "assembly_id": assembly["id"],
                "test_id": test["id"],
                "scene_id": image["id"],
                "plugin_id": "spatial_scene_v1",
                "parameters": {"frames": 2, "seed": 41},
            }
        ),
    )
    assert run["result"]["psf"]["spatial_model"] == "field_grid"
    assert run["result"]["physical_validation"] == "not_established"
    assert run["result"]["uncertainty"] == {"kind": "unknown"}
    assert run["result"]["radial_response_sampling"]["field_reference_radius_mm"] == 0.75

    def array(record, name):
        a = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
        return np.load(service.registry.blob_path(a["sha256"]), allow_pickle=False)

    raw, rgb = array(run, "raw_frames"), array(run, "rgb_frames")
    assert raw.shape == (2, 64, 64) and raw.std() > 10
    assert rgb.shape == (2, 64, 64, 3)
    isp = imaging.isp_template(service, assembly["id"])
    isp["payload"]["white_balance"]["gains_rgb"] = [1.4, 1.0, 0.8]
    isp = service.create_model(isp)
    replay = finish(
        service,
        imaging.submit_replay(
            service,
            {
                "name": "Preserved prescription RAW",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        ),
    )
    np.testing.assert_array_equal(array(replay, "raw_frames"), raw)
    assert not np.array_equal(array(replay, "rgb_frames"), rgb)
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["package_hash"] == digest(
            {k: manifest[k] for k in ("run", "model_lineage", "factory_jobs")}
        )
        for record in (parent, model, job, radiometry):
            assert record["id"] in json.dumps(manifest)
        for name in ("fine_opd_nm.npy", "fine_admitted.npy", "psf_kernel.npy"):
            artifact = next(a for a in job["artifacts"] if a["name"] == name)
            assert any(artifact["sha256"] in p for p in bundle.namelist())
    artifact = next(a for a in job["artifacts"] if a["name"] == "fine_opd_nm.npy")
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    try:
        path.write_bytes(b"corrupted original optical paths")
        with pytest.raises(IntegrityError, match="checksum"):
            service.export_run(run["id"])
    finally:
        path.write_bytes(original)


def test_corrupted_radiometry_original_blocks_psf_publication(optical_workflow):
    service, _, _, _, _, radiometry, publication = optical_workflow
    artifact = next(a for a in radiometry["artifacts"] if a["name"].endswith(".npy"))
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    try:
        path.write_bytes(b"corrupted original radiometry")
        with pytest.raises(IntegrityError, match="checksum"):
            factory.publish(service, publication)
    finally:
        path.write_bytes(original)


def test_unrelated_or_changed_prescription_cannot_be_published(optical_workflow):
    service, _, parent, _, job, _, publication = optical_workflow
    unrelated = next(
        m
        for m in service.registry.list("models")
        if m["kind"] == "lens" and not m.get("factory_job_id") and not m.get("psf_factory_job_id")
    )
    with pytest.raises(ValueError, match="verified RayOptics"):
        factory.publish(service, {**publication, "base_lens_model_id": unrelated["id"]})
    modified = deepcopy(job)
    modified["request"]["prescription"]["surfaces"][1]["radius_mm"] += 0.1
    with pytest.raises(ValueError, match="same prescription"):
        factory.psf_candidate_fields(modified, parent, publication["conditions"], service=service)
    altered = deepcopy(publication)
    altered["conditions"]["f_number"]["value"] *= 1.01
    with pytest.raises(ValueError, match="geometric reference"):
        factory.publish(service, altered)


def test_psf_provenance_and_physical_radius_cannot_be_forged(optical_workflow):
    service, _, _, model, _, _, _ = optical_workflow
    data = fields(model, ModelCreate)
    data["payload"]["field_reference_radius_mm"] = 1.0
    with pytest.raises(ValueError, match="preserved PSF"):
        service.create_model(data)
    data = fields(model, ModelCreate)
    data.update(
        psf_factory_job_id=None,
        factory_application_conditions=None,
        factory_condition_scope="all_declared",
        source_uri="fixture://fake-source",
    )
    with pytest.raises(ValueError, match="Factory provenance"):
        service.create_model(data)


def test_independently_sampled_physical_binding_cannot_be_forged(optical_workflow):
    service, _, _, model, job, _, _ = optical_workflow
    if job["request"]["schema"] != "prescription_field_psf_v4":
        return
    data = fields(model, ModelCreate)
    data["properties"]["physical_prescription_binding"][
        "physical_prescription_sha256"
    ] = "0" * 64
    with pytest.raises(ValueError, match="Composite Lens differs"):
        service.create_model(data)

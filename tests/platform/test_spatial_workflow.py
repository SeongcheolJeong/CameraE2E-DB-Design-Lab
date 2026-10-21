"""Actual spatial solver and preserved RAW replay across service/evidence boundaries."""

import copy
import hashlib
import io
import json
import zipfile

import numpy as np
import pytest

from camera_platform import factory, imaging
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import seed
from camera_platform.evidence import build_bundle
from camera_platform.registry import IntegrityError
from camera_platform.service import PlatformService, PreflightError


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


def array(service, record, name):
    artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


@pytest.fixture
def workflow(tmp_path):
    pytest.importorskip("prysm")
    service = PlatformService(tmp_path / "spatial")
    seed(service)
    original = service.registry.list("assemblies")[0]
    base = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    job = finish(service, factory.submit(service, factory.template(service, "prysm_psf_v1")))
    assert job["result"]["summary"]["status"] == "PROPOSED"
    lens = factory.publish(
        service,
        {
            "job_id": job["id"],
            "base_lens_model_id": base["id"],
            "name": "Pupil composition",
            "revision": "1",
            "conditions": original["conditions"],
        },
    )
    fields = {k: copy.deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    fields.update(name="Native spatial fixture", revision="spatial-1")
    fields["model_ids"] = [lens["id"] if i == base["id"] else i for i in fields["model_ids"]]
    assembly = service.create_assembly(fields)
    scene = imaging.create_fixture(service, imaging.fixture_template(service, assembly["id"]))
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], scene["id"]))
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "plugin_id": "spatial_scene_v1",
        "scene_id": scene["id"],
        "parameters": {"seed": 471, "frames": 3},
    }
    return service, assembly, scene, test, request, job


def test_actual_spatial_raw_replay_changes_rgb_and_keeps_original_evidence(workflow):
    service, assembly, scene, test, request, factory_job = workflow
    run = finish(service, service.submit_run(request))
    raw = array(service, run, "raw_frames")
    rgb = array(service, run, "rgb_frames")
    assert raw.shape == (3, 64, 64) and np.std(raw) > 10
    assert raw.max() < 4095 and raw.min() >= 0
    assert rgb.shape == (3, 64, 64, 3)
    assert not np.array_equal(raw[0], raw[1])
    assert run["result"]["uncertainty"] == {"kind": "unknown"}
    assert run["result"]["prediction"]["metric_id"] == "raw.channel_mean_dn"
    assert run["result"]["geometry"]["full_sensor_shape"] == [1440, 1920]
    source = imaging.replay_sources(service)[0]
    assert source["conditions"] == test["conditions"]
    isp_fields = imaging.isp_template(service, assembly["id"])
    isp_fields["payload"]["white_balance"]["gains_rgb"] = [1.3, 1.0, 0.75]
    model = service.create_model(isp_fields)
    replay = finish(
        service,
        imaging.submit_replay(
            service,
            {
                "name": "Preserved acquisition",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": model["id"],
            },
        ),
    )
    np.testing.assert_array_equal(array(service, replay, "raw_frames"), raw)
    altered = array(service, replay, "rgb_frames")
    assert not np.array_equal(altered, rgb)
    assert altered[..., 0].mean() > rgb[..., 0].mean()
    assert altered[..., 2].mean() < rgb[..., 2].mean()
    assert replay["result"]["acquisition_conditions"] == test["conditions"]
    assert replay["result"]["raw_sha256"] == source["raw_sha256"]
    assert len(service.list_simulation_runs()) == len(service.list_replays()) == 1
    assert service.registry.list("captures") == service.registry.list("measurements") == []
    with zipfile.ZipFile(build_bundle(service, "replays", replay["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source"] == run
        assert manifest["factory_jobs"][0]["id"] == factory_job["id"]
        assert len(manifest["model_lineage"]) >= 5
        for path in archive.namelist():
            if path.startswith("artifacts/"):
                assert hashlib.sha256(archive.read(path)).hexdigest() == path.split("/")[1]
        for artifact in scene["artifacts"]:
            assert archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}") == (
                service.registry.blob_path(artifact["sha256"]).read_bytes()
            )
    assert service.registry.audit()["integrity"] == "verified"


def test_registered_spectral_source_preserves_halo_capture_crop_through_worker(workflow):
    service, assembly, scene, test, request, _ = workflow
    source_origin = scene["sampling"]["crop_origin"]
    roi_origin = [source_origin[0] + 16, source_origin[1] + 16]
    full = finish(service, service.submit_run(request))
    cropped_request = copy.deepcopy(request)
    cropped_request["parameters"]["capture_crop"] = {
        "crop_origin": roi_origin, "shape": [16, 16],
    }
    cropped = finish(service, service.submit_run(cropped_request))
    region = (slice(16, 32), slice(16, 32))
    np.testing.assert_allclose(array(service, cropped, "mean_electrons"),
                               array(service, full, "mean_electrons")[region],
                               rtol=1e-12, atol=1e-12)
    assert array(service, cropped, "raw_frames").shape == (3, 16, 16)
    assert array(service, cropped, "rgb_frames").shape == (3, 16, 16, 3)
    support = cropped["result"]["optical_support"]
    assert support["source_support"]["crop_origin"] == source_origin
    assert support["capture_crop"]["crop_origin"] == roi_origin
    assert cropped["request"]["scene"]["sampling"]["capture_crop"] == (
        cropped_request["parameters"]["capture_crop"]
    )

    unsupported = copy.deepcopy(request)
    unsupported["parameters"]["capture_crop"] = {
        "crop_origin": source_origin, "shape": [16, 16],
    }
    with pytest.raises(PreflightError) as rejected:
        service.submit_run(unsupported)
    assert any("PSF halo" in item["message"] for item in rejected.value.report["issues"])


@pytest.mark.parametrize("missing", ["light_id", "temperature_c"])
def test_scene_cannot_inherit_missing_test_conditions(workflow, missing):
    service, assembly, scene, _, request, _ = workflow
    metadata = imaging.spectral_template(service, assembly["id"])
    metadata["revision"] = "missing-" + missing
    metadata["conditions"].pop(missing)
    artifact = next(a for a in scene["artifacts"] if a["name"] == "spectral-image.npy")
    changed = imaging.import_spectral(
        service, metadata, service.registry.blob_path(artifact["sha256"]).read_bytes()
    )
    test = service.create_test(
        imaging.spatial_test_template(service, assembly["id"], changed["id"])
        | {"revision": "missing-" + missing}
    )
    with pytest.raises(ValueError, match="missing required"):
        service.submit_run(request | {"scene_id": changed["id"], "test_id": test["id"]})


def test_wrong_scene_and_radial_test_are_refused(workflow):
    service, assembly, scene, _, request, _ = workflow
    radial = next(
        t for t in service.registry.list("tests") if t["metric_id"] != "raw.channel_mean_dn"
    )
    with pytest.raises(ValueError, match="native RAW mean"):
        service.submit_run(request | {"test_id": radial["id"]})
    changed = imaging.create_fixture(
        service,
        imaging.fixture_template(service, assembly["id"])
        | {"pattern": "point", "revision": "point"},
    )
    with pytest.raises(ValueError, match="frozen spatial Test"):
        service.submit_run(request | {"scene_id": changed["id"]})
    with pytest.raises(ValueError, match="Scene|scene|spatial|Spatial"):
        service.submit_run(request | {"plugin_id": "response_db"})


def test_replay_format_mismatch_and_counterfactual_capture_fields_are_refused(workflow):
    service, assembly, _, _, request, _ = workflow
    run = finish(service, service.submit_run(request))
    fields = imaging.isp_template(service, assembly["id"])
    fields["input_raw_format"]["black_level"] = 65
    model = service.create_model(fields)
    payload = {
        "name": "Mismatch",
        "source_kind": "run",
        "source_id": run["id"],
        "isp_model_id": model["id"],
    }
    with pytest.raises(ValueError, match="RAW format differs"):
        imaging.submit_replay(service, payload)
    with pytest.raises(ValueError, match="Extra inputs"):
        imaging.submit_replay(service, payload | {"exposure_ms": 2})


def test_tampered_raw_cannot_replay(workflow):
    service, assembly, _, _, request, _ = workflow
    run = finish(service, service.submit_run(request))
    model = service.create_model(imaging.isp_template(service, assembly["id"]))
    source = imaging.replay_source(service, "run", run["id"])
    path = service.registry.blob_path(source["raw_sha256"])
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    with pytest.raises(IntegrityError):
        imaging.submit_replay(
            service,
            {
                "name": "Tampered",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": model["id"],
            },
        )


@pytest.mark.parametrize(
    "values",
    [
        np.ones((8, 8, 5), dtype=np.uint16),
        np.full((8, 8, 5), -1.0),
        np.full((8, 8, 5), np.nan),
        np.ones((7, 8, 5)),
        np.ones((8, 8, 3), dtype=object),
    ],
)
def test_malformed_spectral_source_is_refused_before_storage(values):
    stream = io.BytesIO()
    np.save(stream, values)
    with pytest.raises(ValueError):
        imaging.load_spectral_npy(stream.getvalue())

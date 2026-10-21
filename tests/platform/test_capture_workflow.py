"""Actual engine RAW through capture/ROI/validation; all acquisition data is simulated."""

import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate, MeasurementCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.observations import load_raw_npy, verify_capture_measurement
from camera_platform.registry import ConflictError
from camera_platform.runtime import run_plugin


def _payload(record, contract):
    return {key: deepcopy(record[key]) for key in contract.model_fields if key in record}


def _npy(array, *, allow_pickle=False):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=allow_pickle)
    return stream.getvalue()


def _execute(service, assembly_id, test_id, *, seed=223):
    run = service.submit_run(
        {
            "assembly_id": assembly_id,
            "test_id": test_id,
            "force": True,
            "parameters": {"seed": seed, "frames": 8, "output_size": [64, 64]},
        }
    )
    claimed = service.registry.claim()
    assert claimed["id"] == run["id"]
    return service.registry.finish(claimed["id"], result=service.execute_run(claimed))


@pytest.fixture
def workflow(tmp_path):
    app = create_app(tmp_path / "capture-workflow", start_worker=False)
    service = app.state.service
    original = service.registry.list("tests")[0]
    payload = _payload(original, SpecificationContract)
    payload.update(
        name="Simulated RAW observation implementation check",
        revision="fixture-1",
        validation_kind="implementation_verification",
    )
    test = service.create_test(payload)
    assembly = next(
        item for item in service.registry.list("assemblies") if "Lens A" in item["name"]
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        headers = {"x-platform-token": bootstrap["token"]}
        yield service, client, headers, assembly, test


def _acquisition(workflow, *, with_dark=True, seed=223):
    service, _, _, assembly, test = workflow
    run = _execute(service, assembly["id"], test["id"], seed=seed)
    artifact = next(item for item in run["artifacts"] if item["name"] == "raw_frames.npy")
    raw_bytes = service.registry.blob_path(artifact["sha256"]).read_bytes()
    metadata = {
        "name": "Actual Runtime RAW fixture, not a hardware measurement",
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "fixture://registered-runtime/" + run["id"],
        "license": "MIT; synthetic implementation fixture",
        "method_and_version": "response_db_v1-capture-fixture",
        "physical_instance": None,
        "parents": [],
        "test_id": test["id"],
        "assembly_id": assembly["id"],
        "dataset_ids": ["simulated-acquisition-" + run["id"]],
        "instrument": {},
        "observation": {
            "raw_format": {
                "cfa": "RGGB",
                "bit_depth": 12,
                "black_level_dn": 64,
                "response_type": "linear",
            },
            "geometry": {
                "kind": "representative_normalized_field_raster",
                "raster_shape": [64, 64],
                "mapping": "center_to_corner_radius_v1",
            },
            "rois": [
                {"id": "center", "field_radius": 0.0, "box": [24, 24, 16, 16]},
                {"id": "middle", "field_radius": 0.5, "box": [8, 8, 16, 16]},
                {"id": "edge", "field_radius": 1.0, "box": [0, 0, 8, 8]},
            ],
            "conditions": deepcopy(test["conditions"]),
            "included_effects": deepcopy(test["included_effects"]),
        },
    }
    dark_bytes = None
    if with_dark:
        dark_request = deepcopy(run["request"])
        dark_request.update(
            seed=seed + 1000, spectral_irradiance_w_m2_nm=[0.0] * len(dark_request["wavelength_nm"])
        )
        dark = run_plugin("response_db", dark_request)["arrays"]["raw_frames"]
        dark_bytes = _npy(dark)
        metadata["observation"]["dark_conditions"] = deepcopy(test["conditions"])
    return metadata, raw_bytes, dark_bytes, run


@pytest.mark.parametrize("with_dark", [False, True])
def test_multipart_raw_to_immutable_observation_and_same_runtime_operator(workflow, with_dark):
    service, client, headers, assembly, base_test = workflow
    metadata, raw_bytes, dark_bytes, source_run = _acquisition(workflow, with_dark=with_dark)
    files = {"raw": ("captured-raw.npy", raw_bytes, "application/octet-stream")}
    if dark_bytes is not None:
        files["dark"] = ("matched-dark.npy", dark_bytes, "application/octet-stream")
    response = client.post(
        "/api/v1/captures/import",
        headers=headers,
        data={"metadata": json.dumps(metadata)},
        files=files,
    )
    assert response.status_code == 201, response.text
    result = response.json()
    capture, measurement, test = result["capture"], result["measurement"], result["test"]
    assert service.registry.get("tests", base_test["id"]) == base_test
    assert test["id"] != base_test["id"] and test["parent_test_id"] == base_test["id"]
    assert test["observation_method"] == measurement["observation_method"]
    assert test["observation_shape"] == [64, 64]
    assert measurement["raw_capture_id"] == capture["id"]
    assert capture["source_kind"] == measurement["source_kind"] == "simulated"
    assert verify_capture_measurement(service, measurement)["id"] == capture["id"]
    artifacts = {item["name"]: item for item in capture["artifacts"]}
    assert (
        service.registry.blob_path(artifacts["raw-capture.npy"]["sha256"]).read_bytes() == raw_bytes
    )
    if with_dark:
        assert (
            service.registry.blob_path(artifacts["dark-capture.npy"]["sha256"]).read_bytes()
            == dark_bytes
        )
    download = client.get(artifacts["raw-capture.npy"]["url"])
    assert download.status_code == 200 and download.content == raw_bytes
    simulated = _execute(service, assembly["id"], test["id"])
    prediction = simulated["result"]["prediction"]
    assert prediction["observation_method"] == measurement["observation_method"]
    assert prediction["axes"] == measurement["axes"]
    assert len(prediction["values"]) == 6
    # Same spatial observer, with remaining shot/read noise and ADC rounding.
    np.testing.assert_allclose(prediction["values"], measurement["values"], atol=0.02)
    assert len(source_run["result"]["prediction"]["values"]) == 10
    report = service.validate(
        {"run_id": simulated["id"], "measurement_id": measurement["id"], "test_id": test["id"]}
    )
    assert report["status"] == "INCONCLUSIVE"
    assert report["physical_validation_pass"] is False
    assert measurement["uncertainty"] == {"kind": "unknown"}
    assert service.registry.audit()["integrity"] == "verified"


def test_dataset_and_identical_pixel_relabeling_are_both_refused(workflow):
    service, _, _, _, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    service.import_capture(metadata, raw_bytes, dark_bytes)
    counts = {
        kind: len(service.registry.list(kind)) for kind in ("captures", "measurements", "tests")
    }
    with pytest.raises(ConflictError):
        service.import_capture(metadata, raw_bytes, dark_bytes)
    relabeled = deepcopy(metadata)
    relabeled.update(name="Same pixels under another identity", dataset_ids=["different-label"])
    reencoded = _npy(load_raw_npy(raw_bytes).astype(np.uint16))
    assert reencoded != raw_bytes
    with pytest.raises(ConflictError, match="pixel|capture|RAW"):
        service.import_capture(relabeled, reencoded, dark_bytes)
    assert {kind: len(service.registry.list(kind)) for kind in counts} == counts


@pytest.mark.parametrize("reused", ["subset", "reordered", "mixed"])
def test_reusing_any_raw_frame_cannot_create_an_independent_capture(workflow, reused):
    service, _, _, _, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    service.import_capture(metadata, raw_bytes, dark_bytes)
    raw = load_raw_npy(raw_bytes)
    if reused == "subset":
        changed = raw[:3]
    elif reused == "reordered":
        changed = raw[::-1]
    else:
        # A partly new sequence still contains one already registered exposure.
        changed = raw.copy()
        changed[1:] += 1
    metadata.update(name="Reused exposures fixture", dataset_ids=["renamed-" + reused])
    counts = {
        kind: len(service.registry.list(kind)) for kind in ("captures", "measurements", "tests")
    }
    with pytest.raises(ConflictError, match="RAW frames overlap"):
        service.import_capture(metadata, _npy(changed), dark_bytes)
    assert {kind: len(service.registry.list(kind)) for kind in counts} == counts


def test_measured_capture_cannot_claim_representative_or_wrong_native_geometry(workflow):
    service, _, _, _, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    metadata.update(
        source_kind="measured",
        physical_instance="rejected-fixture-serial",
        instrument={"id": "fixture-only", "calibration_revision": "fixture-only"},
    )
    with pytest.raises(ValueError, match="native sensor/crop geometry"):
        service.import_capture(metadata, raw_bytes, dark_bytes)
    metadata["observation"]["geometry"] = {
        "kind": "native_sensor",
        "full_sensor_shape": [64, 64],
        "crop_origin": [0, 0],
    }
    with pytest.raises(ValueError, match="native sensor shape disagrees"):
        service.import_capture(metadata, raw_bytes, dark_bytes)
    assert service.registry.list("captures") == []
    assert service.registry.list("measurements") == []


def test_quick_estimate_cannot_queue_for_spatial_observation_test(workflow):
    service, _, _, assembly, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    result = service.import_capture(metadata, raw_bytes, dark_bytes)
    before = service.registry.list_runs()
    with pytest.raises(ValueError, match="spatial Response-DB"):
        service.submit_run(
            {
                "assembly_id": assembly["id"],
                "test_id": result["test"]["id"],
                "plugin_id": "quick_estimate",
            }
        )
    assert service.registry.list_runs() == before


@pytest.mark.parametrize(
    "tamper", ["values", "source_kind", "raw_capture_id", "missing_capture", "method"]
)
def test_direct_measurement_cannot_break_registered_raw_binding(workflow, tamper):
    service, _, _, _, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    original = service.import_capture(metadata, raw_bytes, dark_bytes)["measurement"]
    changed = _payload(original, MeasurementCreate)
    if tamper == "values":
        changed["values"][0] += 0.2
    elif tamper == "source_kind":
        changed.update(source_kind="measured", physical_instance="forged-fixture-serial")
        changed["instrument"].update(id="fixture-only", calibration_revision="fixture-only")
    elif tamper == "raw_capture_id":
        changed["raw_capture_id"] = "capture-that-does-not-exist"
    elif tamper == "missing_capture":
        changed["raw_capture_id"] = None
    else:
        changed["observation_method"]["sampling_hash"] = "0" * 64
    # Keep original dataset IDs: binding must fail before ordinary duplicate-data
    # protection, so that a generic duplicate rejection cannot hide a binding bug.
    with pytest.raises((ValueError, KeyError), match="capture|RAW|analysis"):
        service.create_measurement(changed)
    assert service.registry.list("measurements") == [original]


def test_test_operator_must_be_reconstructible_from_frozen_specification(workflow):
    service, _, _, _, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    test = service.import_capture(metadata, raw_bytes, dark_bytes)["test"]
    forged = _payload(test, SpecificationContract)
    forged.update(name="Mismatched observer metadata", revision="forged-fixture")
    forged["observation_method"]["specification"]["rois"][0]["box"][0] += 2
    with pytest.raises(ValueError, match="observation|operator|specification"):
        service.create_test(forged)


@pytest.mark.parametrize(
    "malicious", ["object", "huge_header", "truncated", "trailing", "fortran", "zip"]
)
def test_npy_parser_rejects_unsafe_or_misdeclared_inputs_without_loading_objects(malicious):
    normal = _npy(np.full((3, 8, 8), 128, dtype=np.uint16))
    if malicious == "object":
        content = _npy(np.full((3, 8, 8), "object-fixture", dtype=object), allow_pickle=True)
    elif malicious == "huge_header":
        stream = io.BytesIO()
        np.lib.format.write_array_header_1_0(
            stream, {"descr": "<u2", "fortran_order": False, "shape": (1, 2**31, 2**31)}
        )
        content = stream.getvalue()
    elif malicious == "truncated":
        content = normal[:-1]
    elif malicious == "trailing":
        content = normal + b"not-part-of-array"
    elif malicious == "fortran":
        content = _npy(np.asfortranarray(np.full((3, 8, 8), 128, dtype=np.uint16)))
    else:
        stream = io.BytesIO()
        np.savez(stream, raw=np.full((3, 8, 8), 128, dtype=np.uint16))
        content = stream.getvalue()
    with pytest.raises(ValueError):
        load_raw_npy(content)


def test_real_synthetic_raw_can_propose_bounded_candidate_without_physical_approval(workflow):
    service, client, _, assembly, _ = workflow
    metadata, raw_bytes, dark_bytes, _ = _acquisition(workflow)
    result = service.import_capture(metadata, raw_bytes, dark_bytes)
    base = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    before = deepcopy(base)
    proposal = service.propose_calibration(
        {
            "name": "Synthetic RAW integration candidate",
            "revision": "synthetic-fit-1",
            "base_sensor_model_id": base["id"],
            "measurement_ids": [result["measurement"]["id"]],
            "parameter_bounds": {
                channel: {"min": [0.5, 0.5], "max": [1.5, 1.5], "unit": "1"}
                for channel in ("R", "B")
            },
        }
    )
    assert proposal["status"] == "PROPOSED", proposal.get("issues")
    assert proposal["physical_evidence"] is False
    assert proposal["evidence_kind"] == "implementation_verification"
    assert proposal["approval_status"] == "unapproved"
    assert service.registry.get("models", base["id"]) == before
    candidate = service.registry.get("models", proposal["candidate_model_id"])
    assert candidate["source_kind"] == "estimated" and candidate["parents"] == [base["id"]]
    assert set(metadata["dataset_ids"]) <= set(candidate["calibration_dataset_ids"])
    assert {
        "raw-frame-sha256:" + frame_hash for frame_hash in result["capture"]["raw_frame_hashes"]
    } <= set(candidate["calibration_dataset_ids"])
    assert candidate["uncertainty"]["kind"] == "unknown"
    camera = _payload(assembly, AssemblyCreate)
    camera.update(
        name="Synthetic calibrated model assembly",
        revision="synthetic-fit-1",
        model_ids=[
            candidate["id"] if identity == base["id"] else identity
            for identity in assembly["model_ids"]
        ],
    )
    calibrated = service.create_assembly(camera)
    run = _execute(service, calibrated["id"], result["test"]["id"])
    report = service.validate(
        {
            "run_id": run["id"],
            "measurement_id": result["measurement"]["id"],
            "test_id": result["test"]["id"],
            "allow_calibrated_model": True,
        }
    )
    assert report["physical_validation_pass"] is False
    assert report["status"] == "INCONCLUSIVE"
    # Training evidence must remain reachable even when the release has no
    # validation links. This is source provenance, independent of comparison.
    release = service.create_release(
        {"name": "Unapproved synthetic calibration package", "run_ids": [run["id"]]}
    )
    assert release["validation_ids"] == [] and release["status"] == "blocked"
    manifest = service.export_release(release["id"])
    assert manifest["validations"] == []
    assert manifest["calibrations"] == [proposal]
    assert {item["id"] for item in manifest["model_lineage"]} >= {base["id"], candidate["id"]}
    assert manifest["measurements"] == [result["measurement"]]
    assert manifest["captures"] == [result["capture"]]
    assert manifest["measurement_tests"] == [result["test"]]
    download = client.get(f"/api/v1/releases/{release['id']}/bundle")
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        artifacts = result["capture"]["artifacts"] + [result["measurement"]["original_artifact"]]
        for artifact in artifacts:
            path = f"artifacts/{artifact['sha256']}/{artifact['name']}"
            assert archive.read(path) == service.registry.blob_path(artifact["sha256"]).read_bytes()
    assert not list((service.registry.directory / "downloads").glob("*.zip"))

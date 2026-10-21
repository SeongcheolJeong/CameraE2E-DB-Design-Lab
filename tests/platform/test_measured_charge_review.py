"""Independent regressions for imported evidence byte and dependency closure gaps."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as workflow
from test_isp_capture_studies import imported_capture, npy, selector
from test_nonlinear_capture_api import files

from camera_platform import factory, isp_replay_v3, readout_calibrations
from camera_platform.contracts import AssemblyCreate
from camera_platform.readout_calibration_contracts import ReadoutCalibrationImport
from camera_platform.registry import canonical
from camera_platform.worker import execute


def test_oversize_valid_evidence_json_whitespace_is_refused_without_prefix_publication(
    workflow, tmp_path
):
    service, sdk, *_ = workflow
    originals, _, _ = files(workflow, tmp_path, seed=817)
    original = originals["evidence.json"] + b" " * (2 * 1024 * 1024)
    prefix = original[: 2 * 1024 * 1024 + 1]
    assert len(original) > len(prefix)
    assert json.loads(prefix) == json.loads(original)
    before = service.registry.list("readout_calibrations")
    response = sdk.http.post(
        "/api/v1/readout-calibrations/import",
        files={
            "metadata_file": ("response.json", originals["response.json"], "application/json"),
            "dark_rate": ("dark.npy", originals["dark-rate.npy"], "application/octet-stream"),
            "evidence": ("original-evidence.json", original, "application/json"),
        },
    )
    assert response.status_code == 413, response.text
    assert service.registry.list("readout_calibrations") == before


def test_response_fitting_capture_ancestor_exports_alternate_lens_factory_original_bytes(workflow):
    service, sdk, assembly, *_ = workflow
    target, initial_response = imported_capture(workflow, seed=821)
    source_lens = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    # A separate authored Factory input, retained by a distinct lens revision.
    # Equivalent numeric kernels still require their own original input provenance.
    draft = factory.template(service, "prysm_psf_v1")
    draft.update(name="Review alternate fitting-acquisition PSF", source_uri="fixture://review-psf")
    alternate_job = finish(service, factory.submit(service, draft))
    assert alternate_job["status"] == "succeeded", alternate_job.get("error")
    assert alternate_job["id"] != source_lens["psf_factory_job_id"]
    alternate_lens = factory.publish(
        service,
        {
            "job_id": alternate_job["id"],
            "base_lens_model_id": source_lens["parents"][0],
            "name": "Review fitting-capture-only lens",
            "revision": "review-1",
            "conditions": assembly["conditions"],
            "condition_scope": "optical_response",
        },
    )
    assembly_data = {key: deepcopy(assembly[key]) for key in AssemblyCreate.model_fields}
    assembly_data.update(name="Review fitting-only assembly", revision="review-1")
    assembly_data["model_ids"] = [
        alternate_lens["id"] if identity == source_lens["id"] else identity
        for identity in assembly["model_ids"]
    ]
    alternate_assembly = service.create_assembly(assembly_data)
    alternate_workflow = list(workflow)
    alternate_workflow[2] = alternate_assembly
    ancestor, _ = imported_capture(alternate_workflow, seed=822, calibration=initial_response)
    fitting, _ = imported_capture(
        workflow, seed=823, calibration=initial_response, parents=[ancestor["id"]]
    )
    metadata = {
        key: deepcopy(initial_response[key]) for key in ReadoutCalibrationImport.model_fields
    }
    metadata.update(
        name="Review response with alternate-lens fitting ancestry",
        revision="review-1",
        parents=[initial_response["id"]],
        reference_capture_ids=[fitting["id"]],
        dataset_ids=["review-response-fit-dataset"],
    )
    response = readout_calibrations.import_calibration(
        service,
        metadata,
        npy(np.full((16, 16), 2.0)),
        canonical(readout_calibrations.evidence_template(metadata)).encode(),
    )
    selected = selector(response)
    model = service.create_model(
        isp_replay_v3.template(
            service,
            source_kind="capture",
            source_id=target["id"],
            selector=selected,
        )
    )
    queued = sdk.post(
        "replays",
        {
            "schema": "camera_isp_replay_v3",
            "name": "Review imported ancestor closure",
            "source_kind": "capture",
            "source_id": target["id"],
            "selector": selected,
            "isp_model_id": model["id"],
        },
    )
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    execute(service, claimed["id"])
    replay = sdk.get("replays/" + queued["id"])
    assert replay["status"] == "succeeded", replay.get("error")
    assert alternate_lens["id"] not in target["assembly_snapshot"]["model_ids"]
    manifest = sdk.get("replays/" + queued["id"] + "/export")
    assert {alternate_lens["id"], *alternate_lens["parents"]} <= {
        model["id"] for model in manifest["model_lineage"]
    }
    assert alternate_job in manifest["factory_jobs"]
    assert {target["id"], fitting["id"], ancestor["id"]} == {
        record["id"] for record in manifest["nonlinear_acquisition_lineage"]
    }
    assert alternate_job["input_artifacts"]
    bundle = sdk.http.get("/api/v1/replays/" + queued["id"] + "/bundle")
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        for artifact in [*alternate_job["input_artifacts"], *alternate_job["artifacts"]]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert content == service.registry.blob_path(artifact["sha256"]).read_bytes()
            assert len(content) == artifact["bytes"]
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        for original in (fitting, ancestor, response):
            for artifact in original["artifacts"]:
                assert archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}") == (
                    service.registry.blob_path(artifact["sha256"]).read_bytes()
                )

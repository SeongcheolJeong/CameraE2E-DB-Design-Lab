"""Actual multipart/API/Worker path for independently uploaded nonlinear acquisition files."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_capture_studies import imported_capture, npy, selector

from camera_platform import isp_charge_source, readout_calibrations
from camera_platform.nonlinear_capture_contracts import NonlinearCaptureImport
from camera_platform.readout_calibration_contracts import ReadoutCalibrationImport
from camera_platform.worker import execute


def _original(data):
    return (json.dumps(data, indent=3).replace("\n", "\r\n") + "\r\n").encode()


def files(workflow, directory, *, shared=False, seed=719):
    service, sdk, assembly, *_, test, _ = workflow
    capture, response = imported_capture(workflow, shared=shared, seed=seed)
    metadata = {k: deepcopy(capture[k]) for k in NonlinearCaptureImport.model_fields}
    metadata.update(name="SDK uploaded original", dataset_ids=[f"sdk-{seed}"],
                    acquisition_group_id=f"sdk-group-{seed}", parents=[])
    # Independent synthetic pixel realization; never relabel it as measured.
    raw = np.random.default_rng(seed + 11).integers(400, 425, capture["raw_shape"], dtype=np.uint16)
    metadata["clock"] = {"clock_id": "fixture", "unit": "ns", "reference": "relative",
                         "origin": "Authored relative start"}
    metadata["timing_mode"] = {"shutter": "rolling", "line_time_ns": 1000,
                               "hidden_top_rows": 2, "readout_basis": "full_sensor_rows"}
    for i, frame in enumerate(metadata["frames"]):
        frame["frame_start_ns"] = i * 40_000_000
        frame["applied"]["applied_at_ns"] = i * 40_000_000
    response_data = {k: deepcopy(response[k]) for k in ReadoutCalibrationImport.model_fields}
    response_data.update(name="SDK response version", revision="sdk-1", parents=[response["id"]])
    evidence = readout_calibrations.evidence_template(response_data)
    data = {
        "capture.json": _original(metadata), "raw.npy": npy(raw),
        "response.json": _original(response_data), "evidence.json": _original(evidence),
        "dark-rate.npy": npy(np.full((16, 16), 2.0)),
    }
    for name, content in data.items():
        (directory / name).write_bytes(content)
    return data, raw, response


@pytest.mark.parametrize("path", [None, "hcg", "lcg"])
def test_original_sdk_upload_v3_worker_binding_and_portable_bytes(workflow, tmp_path, path):
    service, sdk, assembly, *_, test, _ = workflow
    originals, raw, parent_response = files(workflow, tmp_path, shared=path is not None)
    before = {k: len(service.registry.list(k)) for k in ("measurements", "tests", "captures")}
    capture = sdk.import_nonlinear_capture(tmp_path / "capture.json", tmp_path / "raw.npy")
    assert len(service.registry.list("captures")) == before["captures"] + 1
    for kind in ("measurements", "tests"):
        assert len(service.registry.list(kind)) == before[kind]
    response = sdk.import_readout_calibration(
        tmp_path / "response.json", tmp_path / "dark-rate.npy", tmp_path / "evidence.json"
    )
    assert sdk.get("captures/" + capture["id"]) == capture
    assert sdk.get("readout-calibrations/" + response["id"]) == response
    assert sdk.get("readout_calibrations/" + response["id"]) == response
    for record, names in [
        (capture, {"raw-capture.npy": "raw.npy", "acquisition-metadata.json": "capture.json"}),
        (response, {"readout-calibration-metadata.json": "response.json",
                    "expected-dark-rate.npy": "dark-rate.npy",
                    "readout-evidence.json": "evidence.json"}),
    ]:
        for artifact in record["artifacts"]:
            if artifact["name"] in names:
                reply = sdk.http.get(artifact["url"])
                assert reply.status_code == 200, reply.text
                assert reply.content == originals[names[artifact["name"]]]
                assert hashlib.sha256(reply.content).hexdigest() == artifact["sha256"]
    selected = selector(response, path)
    choices = [r for r in sdk.get("replay-sources") if r["source_id"] == capture["id"]]
    assert len(choices) == (4 if path else 2)  # Two explicitly selected response versions.
    assert len({json.dumps(r["selector"], sort_keys=True) for r in choices}) == len(choices)
    draft = sdk.template("isp-profile", version=3, source_kind="capture", source_id=capture["id"],
                         selector=json.dumps(selected))
    model = service.create_model(draft)
    queued = sdk.post("replays", {
        "schema": "camera_isp_replay_v3", "name": "Uploaded nonlinear RAW replay",
        "source_kind": "capture", "source_id": capture["id"], "selector": selected,
        "isp_model_id": model["id"],
    })
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    execute(service, claimed["id"])
    replay = sdk.get("replays/" + claimed["id"])
    assert replay["status"] == "succeeded", replay.get("error")
    source = replay["source"]
    assert "capture_program_id" not in source and "plugin_id" not in source["source_snapshot"]
    assert source["decoder_binding"]["readout_calibration_id"] == response["id"]
    assert source["response_calibration_snapshot"] == response
    assert source["source_provenance"]["source_kind"] == "simulated"
    assert source["frame_records"][0]["applied"]["isp_model_id"] is None
    windows = source["timing"]["frames"][0]["exposure_windows"]
    assert windows["row_start_ns"][:2] == [2000, 3000]
    assert windows["row_end_ns"][-1] == 4_017_000
    restored, calibrations = isp_charge_source.load_inputs(service, source)
    expected = raw if path is None else raw[:, 0 if path == "hcg" else 1]
    np.testing.assert_array_equal(restored, expected)
    for calibration in calibrations:
        np.testing.assert_allclose(calibration["dark_e"], 0.008, rtol=0, atol=1e-15)
    summary = replay["result"]["summary"]
    assert summary["decoder_binding"] == source["decoder_binding"]
    assert summary["physical_validation"] == "not_established"
    bundle = sdk.http.get("/api/v1/replays/" + replay["id"] + "/bundle")
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {r["id"] for r in manifest["readout_calibrations"]} == {
            response["id"], parent_response["id"],
        }
        for record in [capture, response, parent_response]:
            for artifact in record["artifacts"]:
                content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                assert content == service.registry.blob_path(artifact["sha256"]).read_bytes()
    projected = next(r for r in sdk.get("workbench")["readout_calibrations"]
                     if r["id"] == response["id"])
    assert "electrical_model" not in projected and "_projection" in projected
    assert sdk.get("workbench/readout_calibrations/" + response["id"])["record"] == response


def test_unbound_raw_discovery_and_unknown_settings_fail_before_job(workflow, tmp_path):
    service, sdk, assembly, *_, test, _ = workflow
    sensor = next(service.registry.get("models", i) for i in assembly["model_ids"]
                  if service.registry.get("models", i)["kind"] == "sensor")
    draft = sdk.template("nonlinear-capture", assembly_id=assembly["id"], test_id=test["id"])
    draft.update(dataset_ids=["unknown-dataset"], acquisition_group_id="unknown-acquisition")
    (tmp_path / "unknown.json").write_bytes(_original(draft))
    (tmp_path / "raw.npy").write_bytes(npy(np.full((1, 16, 16), 100, dtype=np.uint16)))
    capture = sdk.import_nonlinear_capture(tmp_path / "unknown.json", tmp_path / "raw.npy")
    assert not any(r["source_id"] == capture["id"] for r in sdk.get("replay-sources"))
    response_draft = sdk.template("readout-calibration", sensor_model_id=sensor["id"])
    assert response_draft["charge_reference"]["kind"] == "authored_simulation"
    legacy = sdk.http.post("/api/v1/replays", json={
        "name": "Unsafe implicit linear replay", "source_kind": "capture",
        "source_id": capture["id"],
        "isp_model_id": next(i for i in assembly["model_ids"]
                             if service.registry.get("models", i)["kind"] == "isp"),
    })
    assert legacy.status_code == 422 and "explicit response calibration" in legacy.text


@pytest.mark.parametrize("schema", [[], {}, None, "unknown"])
def test_invalid_selection_schema_is_request_error(schema):
    with pytest.raises(ValueError):
        isp_charge_source.validate_selector({"schema": schema, "mode": "sequential_frames"})


def test_multipart_ambiguous_metadata_is_rejected(workflow):
    _, sdk, *_ = workflow
    for endpoint, extra in [
        ("captures/nonlinear/import", {"raw": ("raw.npy", b"invalid")}),
        ("readout-calibrations/import", {"dark_rate": ("dark.npy", b"invalid"),
                                         "evidence": ("evidence.json", b"{}")}),
    ]:
        response = sdk.http.post("/api/v1/" + endpoint, data={"metadata": "{}"},
                                 files={**extra, "metadata_file": ("original.json", b"{}")})
        assert response.status_code == 422 and "exactly one" in response.text

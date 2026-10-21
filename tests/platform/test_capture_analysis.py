"""One acquisition, distinct observation revisions, unchanged evidence independence."""

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
import test_control_response_workflow as response_workflow
from fastapi.testclient import TestClient
from test_control_response_workflow import response_run, source
from test_control_workflow import register

from camera_platform import capture_analysis as ca
from camera_platform import control_response_service as response
from camera_platform.api import create_app
from camera_platform.contracts import MeasurementCreate
from camera_platform.evidence import build_bundle
from camera_platform.observations import capture_content_ids, verify_capture_measurement
from camera_platform.registry import ConflictError, canonical
from camera_platform.sdk import PlatformClient

workflow = response_workflow.workflow


@pytest.fixture
def acquired(workflow):
    service, test, run = response_run(workflow)
    metadata, raw = source(service, test, run)
    imported = response.import_capture(service, metadata, raw)
    return service, test, run, imported, metadata, raw


def request(capture, test, **changes):
    return {
        "capture_id": capture["id"],
        "test_id": test["id"],
        "name": "Response analysis",
        "revision": "2",
        "uncertainty": {
            "kind": "unknown",
            "unit": test["unit"],
            "reason": "Unestablished metric bound",
        },
        **changes,
    }


def another_test(service, run, **options):
    return service.create_test(
        response.test_template(
            service,
            {
                "control_program_id": run["control_program_id"],
                **options,
            },
        )
    )


def test_metrics_and_reductions_reuse_one_original_acquisition(acquired):
    service, test, run, imported, metadata, raw = acquired
    capture = imported["capture"]
    assert "measurement_contract" not in capture
    assert "analysis_measurement" not in capture
    assert "uncertainty" not in capture
    assert {a["name"] for a in capture["artifacts"]} == {
        "raw-capture.npy",
        "acquisition-metadata.json",
    }
    peak_test = another_test(
        service,
        run,
        reduction="peak_absolute_target_error",
        target=0.18,
        window_start_frame_id=test["stimulus"]["planned_frames"][0]["frame_id"],
        window_end_frame_id=test["stimulus"]["planned_frames"][-1]["frame_id"],
    )
    latency_test = another_test(service, run, observable="ae_command_latency_ns")
    peak = ca.create(service, request(capture, peak_test))
    latency = ca.create(service, request(capture, latency_test))
    assert peak["measurement"]["values"] == [
        max(abs(v - 0.18) for v in imported["measurement"]["values"])
    ]
    assert latency["measurement"]["unit"] == "ns"
    assert len(service.registry.list("captures")) == 1
    assert len(service.registry.list("capture_analyses")) == 3
    assert service.registry.get("captures", capture["id"]) == capture
    for result in [imported, peak, latency]:
        assert result["measurement"]["dataset_ids"] == metadata["dataset_ids"]
        assert result["measurement"]["source_kind"] == "simulated"
        assert result["analysis"]["independence"]["adds_independent_acquisition"] is False
        assert capture_content_ids(
            verify_capture_measurement(service, result["measurement"])
        ) == capture_content_ids(capture)
    artifact = next(a for a in capture["artifacts"] if a["name"] == "raw-capture.npy")
    assert service.registry.blob_path(artifact["sha256"]).read_bytes() == raw


def test_revision_parent_and_concurrent_request_are_idempotent(acquired):
    service, test, _, imported, _, _ = acquired
    payload = request(imported["capture"], test, parent_analysis_id=imported["analysis"]["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ca.create(service, payload), range(2)))
    assert results[0] == results[1]
    assert len(service.registry.list("capture_analyses")) == 2
    assert len(service.registry.list("measurements")) == 2
    assert ca.create(service, payload) == results[0]
    assert results[0]["analysis"]["parent_hash"] == imported["analysis"]["content_hash"]
    verify_capture_measurement(service, results[0]["measurement"])


def test_different_camera_execution_cannot_be_relabelled_as_reanalysis(acquired, workflow):
    service, _, _, imported, _, _ = acquired
    program, _, _ = register(workflow)
    test = service.create_test(
        response.test_template(service, {"control_program_id": program["id"]})
    )
    with pytest.raises(ValueError, match="execution context"):
        ca.create(service, request(imported["capture"], test))
    assert len(service.registry.list("capture_analyses")) == 1


@pytest.mark.parametrize(
    "uncertainty",
    [
        {
            "kind": "absolute_bound",
            "value": 1,
            "unit": "1",
            "source_uri": "test://bound",
            "method_and_version": "1",
        },
        {
            "kind": "absolute_bound",
            "values": [1],
            "unit": "ns",
            "source_uri": "test://bound",
            "method_and_version": "1",
        },
        {"kind": "unknown", "unit": "ns", "value": 0, "reason": "Invalid invented zero"},
    ],
)
def test_metric_bound_is_not_implicitly_reused_for_new_units_or_shape(acquired, uncertainty):
    service, _, run, imported, _, _ = acquired
    test = another_test(service, run, observable="exposure_ns")
    with pytest.raises(ValueError, match="uncertainty|Unknown"):
        ca.create(service, request(imported["capture"], test, uncertainty=uncertainty))
    assert len(service.registry.list("measurements")) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_ids", ["new-fake-independent-dataset"]),
        ("source_kind", "estimated"),
        ("capture_analysis_id", None),
        ("values", [0.0]),
    ],
)
def test_measurement_cannot_forge_derived_identity_or_values(acquired, field, value):
    service, _, _, imported, _, _ = acquired
    modified = deepcopy(imported["measurement"])
    modified[field] = value
    with pytest.raises(ValueError, match="preserved"):
        verify_capture_measurement(service, modified)


def test_reimport_and_generic_measurement_route_do_not_bypass_acquisition_identity(acquired):
    service, _, _, imported, metadata, raw = acquired
    metadata["dataset_ids"] = ["renamed-dataset"]
    with pytest.raises(ConflictError, match="RAW frames"):
        response.import_capture(service, metadata, raw)
    contract = {
        k: v for k, v in imported["measurement"].items() if k in MeasurementCreate.model_fields
    }
    with pytest.raises(ConflictError, match="Dataset identity"):
        service.create_measurement(contract)
    assert len(service.registry.list("captures")) == 1
    assert len(service.registry.list("measurements")) == 1


def test_atomic_import_failure_publishes_no_partial_acquisition(workflow, monkeypatch):
    service, test, run = response_run(workflow)
    metadata, raw = source(service, test, run)
    audit = service.registry.audit()
    original = service.registry._audit

    def fail(conn, action, *args):
        if action == "capture_analyses.created":
            raise RuntimeError("injected analysis publication failure")
        return original(conn, action, *args)

    monkeypatch.setattr(service.registry, "_audit", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        response.import_capture(service, metadata, raw)
    for kind in ["captures", "capture_analyses", "measurements"]:
        assert service.registry.list(kind) == []
    assert service.registry.audit() == audit


def test_legacy_capture_is_reusable_without_rewriting_its_first_analysis(workflow):
    service, test, run = response_run(workflow)
    metadata, raw_bytes = source(service, test, run)
    # Exact pre-separation record layout; the original contract lacks the new optional ID.
    observation = run["result"]["response_observation"]
    import numpy as np

    from camera_platform.control_observation import metric_fields

    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
    contract = MeasurementCreate.model_validate(
        {
            **{k: v for k, v in metadata.items() if k != "acquisition"},
            **metric_fields(observation),
            "included_effects": test["included_effects"],
        }
    ).model_dump()
    contract.pop("capture_analysis_id")
    capture = service.registry.put(
        "captures",
        {
            **metadata,
            "capture_kind": response.METHOD,
            "observation": test["observation_specification"],
            "observation_method": test["observation_method"],
            "analysis_measurement": observation,
            "measurement_contract": contract,
            "raw_shape": list(raw.shape),
            "raw_content_hash": response.pixel_hash(raw),
            "raw_frame_hashes": [response.pixel_hash(frame) for frame in raw],
            "artifacts": [
                service.registry.store_blob(
                    raw_bytes, "raw-capture.npy", "application/octet-stream"
                ),
                service.registry.store_blob(
                    canonical(metadata).encode(), "acquisition-metadata.json", "application/json"
                ),
                service.registry.store_blob(
                    canonical(observation).encode(), "response-analysis.json", "application/json"
                ),
            ],
        },
    )
    original = service.create_measurement({**contract, "raw_capture_id": capture["id"]})
    second = another_test(service, run, observable="wb_blue_gain")
    result = ca.create(service, request(capture, second))
    assert service.registry.get("captures", capture["id"]) == capture
    verify_capture_measurement(service, original)
    verify_capture_measurement(service, result["measurement"])
    assert result["measurement"]["dataset_ids"] == original["dataset_ids"]


def test_sdk_worker_comparison_bundle_and_backup_preserve_analysis_lineage(acquired, tmp_path):
    from camera_platform.backup import backup_state, restore_state

    service, _, run, imported, _, _ = acquired
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as http:
        test = another_test(service, run, observable="exposure_ns")
        payload = request(imported["capture"], test, parent_analysis_id=imported["analysis"]["id"])
        assert http.post("/api/v1/capture_analyses", json=payload).status_code == 403
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = http
        result = sdk.analyze_capture(**{k: v for k, v in payload.items()})
        assert len(sdk.get("capture_analyses")) == 2
        queued = sdk.submit(
            run["assembly_id"],
            test["id"],
            plugin_id="closed_loop_scene_v1",
            control_program_id=run["control_program_id"],
            parameters={"seed": 123},
        )
        completed = sdk.wait(queued["id"], timeout=30, poll_interval=0.05)
        report = sdk.compare(completed["id"], result["measurement"]["id"], test["id"])
        assert report["status"] == "INCONCLUSIVE"
        release = service.create_release(
            {
                "name": "Derived observation evidence",
                "run_ids": [completed["id"]],
                "validation_ids": [report["id"]],
            }
        )
        bundle = build_bundle(service, "releases", release["id"])
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert len(manifest["captures"]) == 1
            assert len(manifest["capture_analyses"]) == 2
            assert manifest["release"]["status"] == "blocked"
            for analysis in manifest["capture_analyses"]:
                for artifact in analysis["artifacts"]:
                    assert archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                    assert http.get(artifact["url"]).status_code == 200
    # Backup stores every immutable record and blob, including the newly added collection.
    backup_state(service.registry.directory, tmp_path / "capture-backup.zip")
    restored = tmp_path / "restored"
    restore_state(tmp_path / "capture-backup.zip", restored)
    from camera_platform.registry import SQLiteRegistry

    assert SQLiteRegistry(restored).list("capture_analyses") == service.registry.list(
        "capture_analyses"
    )

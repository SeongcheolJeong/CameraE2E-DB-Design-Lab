#!/usr/bin/env python3
"""Verify installed detailed solvers through HTTP, durable workers, and reusable evidence."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

from verify_camera_platform import WORKSPACE, start, stop

from camera_platform.contracts import AssemblyCreate
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def main():
    report = {
        "schema": "camera_factory_verification_v1",
        "environment": environment_fingerprint(),
        "physical_validation": "not_established",
        "fixture_origin": "explicit authored synthetic designs; no hardware measurements",
        "checks": [],
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with tempfile.TemporaryDirectory(prefix="camera-factory-http-") as directory:
        state = Path(directory).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (state / "http.log").open("ab") as log:
            process = start(state, port, log)
            try:
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    boot = client.get("bootstrap")
                    plugins = client.get("factory/plugins")
                    required = {"rayoptics_lens_v1", "meep_planar_stack_v1"}
                    available = {p["plugin_id"] for p in plugins if p["available"]}
                    assert required <= available, plugins
                    report["backends"] = plugins
                    passed("trusted_installed_rayoptics_and_serial_meep_probes")
                    jobs = {}
                    for plugin in ("rayoptics_lens_v1", "meep_planar_stack_v1"):
                        request = client.get("templates/factory?plugin_id=" + plugin)
                        queued = client.characterize(request)
                        for artifact in queued["input_artifacts"]:
                            response = client.http.get(artifact["url"])
                            response.raise_for_status()
                            assert (
                                hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
                            )
                        job = client.wait_characterization(queued["id"])
                        assert job["status"] == "succeeded", job
                        assert client.get("runs") == []
                        assert client.characterize(request)["id"] == job["id"]
                        for artifact in job["artifacts"]:
                            response = client.http.get(artifact["url"])
                            response.raise_for_status()
                            assert (
                                hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
                            )
                        jobs[plugin] = job
                    passed(
                        "both_actual_solvers_complete_in_durable_worker_with_original_input_and_artifact_hashes"
                    )
                    lens, meep = jobs.values()
                    assert lens["result"]["summary"]["status"] == "PROPOSED"
                    assert meep["result"]["summary"]["status"] == "NUMERICALLY_CONVERGED"
                    assert meep["result"]["model_candidate"] is None
                    assert {"results.json", "configurations.json", "solver.log"} <= {
                        a["name"] for a in meep["artifacts"]
                    }
                    for artifact in meep["artifacts"]:
                        if artifact["name"] == "results.json":
                            report["meep_signed_results"] = client.http.get(artifact["url"]).json()
                    report["lens_summary"] = lens["result"]["summary"]
                    report["meep_summary"] = meep["result"]["summary"]
                    original = boot["assemblies"][0]
                    candidate = client.post(
                        "factory/models",
                        {
                            "job_id": lens["id"],
                            "name": "HTTP characterized lens",
                            "revision": "http-factory-1",
                            "conditions": original["conditions"],
                        },
                    )
                    assert candidate["factory_job_id"] == lens["id"]
                    assert candidate["factory_application_conditions"] == original["conditions"]
                    assert candidate["uncertainty"] == {"kind": "unknown"}
                    refused = client.http.post(
                        "/api/v1/factory/models",
                        json={
                            "job_id": meep["id"],
                            "name": "Forbidden QE model",
                            "revision": "1",
                            "conditions": original["conditions"],
                        },
                    )
                    assert refused.status_code == 409, refused.text
                    passed(
                        "convergence_is_not_approval_and_meep_absorption_cannot_publish_collected_electron_qe"
                    )
                    assembly = {
                        k: deepcopy(v)
                        for k, v in original.items()
                        if k in AssemblyCreate.model_fields
                    }
                    assembly.update(
                        name="Actual factory lens with existing Sensor and ISP",
                        revision="factory-http",
                    )
                    models = {m["id"]: m for m in boot["models"]}
                    assembly["model_ids"] = [
                        candidate["id"] if models[i]["kind"] == "lens" else i
                        for i in assembly["model_ids"]
                    ]
                    assembly = client.post("assemblies", assembly)
                    run = client.wait(client.submit(assembly["id"], boot["demo"]["test_id"])["id"])
                    assert run["status"] == "succeeded", run
                    assert {"raw_frames.npy", "rgb.npy"} <= {a["name"] for a in run["artifacts"]}
                    passed(
                        "characterized_spectral_lens_reused_with_sensor_and_isp_to_generate_raw_rgb"
                    )
                    release = client.post(
                        "releases",
                        {"name": "Factory evidence", "run_ids": [run["id"]], "validation_ids": []},
                    )
                    assert release["status"] == "blocked"
                    response = client.http.get(f"/api/v1/releases/{release['id']}/bundle")
                    response.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
                        manifest = json.loads(bundle.read("manifest.json"))
                        assert manifest["factory_jobs"][0]["id"] == lens["id"]
                        for artifact in lens["artifacts"]:
                            saved = bundle.read(
                                f"artifacts/{artifact['sha256']}/{artifact['name']}"
                            )
                            assert hashlib.sha256(saved).hexdigest() == artifact["sha256"]
                    assert client.get("measurements") == []
                    assert client.get("audit")["integrity"] == "verified"
                    passed(
                        "release_bundle_preserves_factory_lineage_without_fabricated_physical_evidence"
                    )
                    report["camera_run"] = {
                        "id": run["id"],
                        "status": run["status"],
                        "artifact_count": len(run["artifacts"]),
                    }
            finally:
                stop(process)
    destination = WORKSPACE / "reports/camera_platform/factory-http-verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {"checks": len(report["checks"]), "status": "passed", "report": str(destination)}
        )
    )


if __name__ == "__main__":
    main()

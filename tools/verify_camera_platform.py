#!/usr/bin/env python3
"""Verify real HTTP -> durable worker -> CameraE2E -> evidence -> validation and restart."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from copy import deepcopy
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))

from camera_platform.contracts import AssemblyCreate, TestCreate  # noqa: E402
from camera_platform.sdk import PlatformClient  # noqa: E402
from camera_platform.service import environment_fingerprint  # noqa: E402


def capture_cycle(client, runs, demo, directory, passed):
    """Synthetic implementation exercise through the real acquisition HTTP boundary."""
    base_test = client.get("tests/" + demo["test_id"])
    dark_test = {k: deepcopy(v) for k, v in base_test.items() if k in TestCreate.model_fields}
    dark_test.update(name="Synthetic zero-irradiance capture", revision="smoke-dark")
    dark_test["stimulus"]["spectral_irradiance_w_m2_nm"] = [0.0] * len(
        dark_test["stimulus"]["wavelength_nm"]
    )
    dark_test = client.post("tests", dark_test)
    dark_run = client.wait(client.submit(demo["assembly_ids"][0], dark_test["id"])["id"])
    assert dark_run["status"] == "succeeded", dark_run
    paths = {}
    for kind, run in (("raw", runs[0]), ("dark", dark_run)):
        artifact = next(a for a in run["artifacts"] if a["name"] == "raw_frames.npy")
        response = client.http.get(artifact["url"])
        response.raise_for_status()
        paths[kind] = directory / (kind + ".npy")
        paths[kind].write_bytes(response.content)
    metadata = client.get("templates/capture")
    metadata.update(
        source_kind="simulated",
        name="Synthetic HTTP RAW acquisition, not hardware",
        source_uri="fixture://http-worker/" + runs[0]["id"],
        dataset_ids=["smoke-synthetic-acquisition-1"],
    )
    metadata["observation"]["dark_conditions"] = {
        k: v for k, v in metadata["observation"]["conditions"].items() if k != "light_id"
    }
    imported = client.import_capture(metadata, paths["raw"], dark_path=paths["dark"])
    assert imported["test"]["id"] != base_test["id"]
    assert imported["measurement"]["source_kind"] == "simulated"
    for artifact in imported["capture"]["artifacts"]:
        if artifact["name"] in {"raw-capture.npy", "dark-capture.npy"}:
            kind = artifact["name"].split("-")[0]
            assert client.http.get(artifact["url"]).content == paths[kind].read_bytes()
    observed = client.wait(client.submit(demo["assembly_ids"][0], imported["test"]["id"])["id"])
    assert observed["status"] == "succeeded", observed
    prediction = observed["result"]["prediction"]
    assert prediction["observation_method"] == imported["measurement"]["observation_method"]
    assert prediction["axes"] == imported["measurement"]["axes"]
    comparison = client.compare(
        observed["id"], imported["measurement"]["id"], imported["test"]["id"]
    )
    assert comparison["status"] == "INCONCLUSIVE", comparison
    passed("multipart_raw_dark_preservation_frozen_roi_prediction_and_inconclusive_comparison")
    sensor = next(m for m in client.get("models") if m["kind"] == "sensor")
    count = len(sensor["payload"]["sensor_angular_response"]["theta_deg"]) - 1
    calibration = client.post(
        "calibrations",
        {
            "name": "Synthetic bounded angular fit",
            "revision": "smoke-fit",
            "base_sensor_model_id": sensor["id"],
            "measurement_ids": [imported["measurement"]["id"]],
            "parameter_bounds": {
                c: {"min": [0.5] * count, "max": [1.5] * count, "unit": "1"} for c in ("R", "B")
            },
        },
    )
    assert calibration["status"] == "PROPOSED", calibration
    assert calibration["approval_status"] == "unapproved"
    candidate = client.get("models/" + calibration["candidate_model_id"])
    assert candidate["uncertainty"] == {"kind": "unknown"}
    assert set(calibration["training_content_ids"]) <= set(candidate["calibration_dataset_ids"])
    original = client.get("assemblies/" + demo["assembly_ids"][0])
    assembly = {k: deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    assembly.update(name="Synthetic calibration candidate camera", revision="smoke-fit")
    assembly["model_ids"] = [
        candidate["id"] if i == sensor["id"] else i for i in assembly["model_ids"]
    ]
    assembly = client.post("assemblies", assembly)
    fitted_run = client.wait(client.submit(assembly["id"], imported["test"]["id"])["id"])
    assert fitted_run["status"] == "succeeded", fitted_run
    training_comparison = client.compare(
        fitted_run["id"],
        imported["measurement"]["id"],
        imported["test"]["id"],
        allow_calibrated_model=True,
    )
    assert training_comparison["status"] == "INVALID_COMPARISON", training_comparison
    assert any(i["code"] == "calibration_dataset_overlap" for i in training_comparison["issues"])
    passed("bounded_candidate_reused_in_worker_and_training_data_refused_as_independent_validation")
    release = client.post(
        "releases",
        {
            "name": "Synthetic captured RAW evidence",
            "run_ids": [fitted_run["id"]],
            "validation_ids": [training_comparison["id"]],
        },
    )
    response = client.http.get(f"/api/v1/releases/{release['id']}/bundle")
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["captures"][0]["id"] == imported["capture"]["id"]
        assert manifest["calibrations"][0]["id"] == calibration["id"]
        assert {sensor["id"], candidate["id"]} <= {m["id"] for m in manifest["model_lineage"]}
        for a in imported["capture"]["artifacts"]:
            assert (
                hashlib.sha256(
                    archive.read("artifacts/" + a["sha256"] + "/" + a["name"])
                ).hexdigest()
                == a["sha256"]
            )
    passed("release_bundle_preserves_raw_dark_metadata_and_calibration_evidence_links")
    return imported


def start(state, port, log):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "camera_platform",
            "serve",
            "--workspace",
            str(WORKSPACE),
            "--state-dir",
            str(state),
            "--port",
            str(port),
        ],
        env={**os.environ, "PYTHONPATH": str(WORKSPACE / "src")},
        cwd=WORKSPACE,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
    )
    import httpx

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Verification server exited before startup")
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/health", timeout=1, trust_env=False
            )
            if response.status_code == 200:
                health = response.json()
                assert health["state_dir"] == str(state)
                assert health["worker"] == "running"
                return process
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    stop(process)
    raise TimeoutError("Verification server did not become ready")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    report = {
        "schema": "camera_platform_verification_v1",
        "environment": environment_fingerprint(),
        "physical_validation": "not_established",
        "checks": [],
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with tempfile.TemporaryDirectory(prefix="camera-platform-verify-") as directory:
        state = Path(directory).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (state / "http.log").open("ab") as log:
            process = start(state, port, log)
            try:
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    bootstrap = client.get("bootstrap")
                    assert bootstrap["measurements"] == []
                    passed("isolated_http_identity_and_empty_measurement_registry")
                    demo = bootstrap["demo"]
                    runs = [
                        client.wait(client.submit(a, demo["test_id"])["id"], timeout=40)
                        for a in demo["assembly_ids"]
                    ]
                    assert all(r["status"] == "succeeded" for r in runs), runs
                    assert (
                        runs[0]["result"]["channel_ratios"] != runs[1]["result"]["channel_ratios"]
                    )
                    passed("real_worker_runs_two_lens_response_models_through_camerae2e")
                    cached = client.submit(demo["assembly_ids"][0], demo["test_id"])
                    assert cached["cache_hit"] and cached["id"] == runs[0]["id"]
                    replay = client.wait(
                        client.submit(demo["assembly_ids"][0], demo["test_id"], force=True)["id"],
                        timeout=40,
                    )
                    assert replay["status"] == "succeeded"
                    # Summary JSON contains a distinct run identity; numerical artifacts reproduce.
                    assert [
                        (a["name"], a["sha256"])
                        for a in replay["artifacts"]
                        if a["name"] != "summary.json"
                    ] == [
                        (a["name"], a["sha256"])
                        for a in runs[0]["artifacts"]
                        if a["name"] != "summary.json"
                    ]
                    passed("cache_and_forced_seeded_replay_artifact_identity")
                    original = client.get("templates/measurement")
                    original["name"] = "검증 전용 형식 fixture · 실측 아님"
                    source = json.dumps(original, ensure_ascii=False, indent=3) + "\n"
                    observation = client.post("measurements/import", {"document_text": source})
                    response = client.http.get(observation["original_artifact"]["url"])
                    assert response.content == source.encode()
                    report_row = client.compare(runs[0]["id"], observation["id"], demo["test_id"])
                    assert report_row["status"] == "INCONCLUSIVE", report_row
                    assert report_row["physical_validation_pass"] is False
                    passed("verbatim_measurement_import_and_unknown_physical_validation_gate")
                    package = client.post(
                        "releases",
                        {
                            "name": "Verification package",
                            "run_ids": [runs[0]["id"]],
                            "validation_ids": [report_row["id"]],
                        },
                    )
                    assert package["status"] == "blocked"
                    response = client.http.get(f"/api/v1/releases/{package['id']}/bundle")
                    response.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        manifest = json.loads(archive.read("manifest.json"))
                        assert manifest["measurements"][0]["id"] == observation["id"]
                        for name in archive.namelist():
                            if name.startswith("artifacts/"):
                                assert (
                                    hashlib.sha256(archive.read(name)).hexdigest()
                                    == name.split("/")[1]
                                )
                        report["bundle_files"] = len(archive.namelist())
                    passed(
                        "blocked_release_bundle_contains_verified_original_artifacts_and_measurement"
                    )
                    imported = capture_cycle(client, runs, demo, state, passed)
                    assert client.get("audit")["integrity"] == "verified"
                    expected_runs = len(client.get("runs"))
                    report["case_results"] = [
                        {
                            "name": r["name"],
                            "channel_ratios": r["result"]["channel_ratios"],
                            "raw_temporal_snr_db": r["result"]["raw_temporal_snr_db"],
                            "artifacts": len(r["artifacts"]),
                        }
                        for r in runs
                    ]
                stop(process)
                process = start(state, port, log)
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    assert len(client.get("runs")) == expected_runs
                    assert client.get("runs/" + runs[0]["id"])["status"] == "succeeded"
                    assert client.get("audit")["integrity"] == "verified"
                    passed("server_and_worker_restart_preserve_completed_results_and_audit")
                stop(process)
                archive = state / "backup.zip"
                restored = state / "restored"
                env = {**os.environ, "PYTHONPATH": str(WORKSPACE / "src")}
                backup = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "camera_platform",
                        "backup",
                        "--state-dir",
                        str(state),
                        "--output",
                        str(archive),
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                manifest = json.loads(backup.stdout)
                restore = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "camera_platform",
                        "restore",
                        "--archive",
                        str(archive),
                        "--state-dir",
                        str(restored),
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                restoration = json.loads(restore.stdout)
                assert restoration["integrity"] == "verified" and not restoration["workers_started"]
                assert restoration["manifest_hash"] == manifest["manifest_hash"]
                process = start(restored, port, log)
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    assert len(client.get("runs")) == expected_runs
                    measurement = client.get("measurements/" + imported["measurement"]["id"])
                    assert measurement == imported["measurement"]
                    assert client.get("audit")["integrity"] == "verified"
                    for a in imported["capture"]["artifacts"]:
                        assert (
                            hashlib.sha256(client.http.get(a["url"]).content).hexdigest()
                            == a["sha256"]
                        )
                passed("cli_backup_restore_and_reopened_http_preserve_registry_raw_and_audit")
            finally:
                stop(process)
    report["status"] = "passed"
    target = WORKSPACE / "reports/camera_platform/http-worker-verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "checks": len(report["checks"]), "report": str(target)}))


if __name__ == "__main__":
    main()

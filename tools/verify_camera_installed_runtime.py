#!/usr/bin/env python3
"""Verify a prepared package via detached launcher, real Worker and source isolation.

Creates a separate synthetic workspace and registry. Never modifies the user's
checkout or restarts their main workbench. Existing Python dependencies are reused.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import start_camera_platform as launcher
from install_camera_platform_runtime import select


def bind_installed_client(root, expected_environment):
    """Fail closed on cached checkout modules instead of mixing two packages."""
    root = Path(root).resolve()
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] not in {"camera_platform", "pyisetcam"}:
            continue
        path = getattr(module, "__file__", None)
        if path is None or not Path(path).resolve().is_relative_to(root):
            raise RuntimeError(
                f"Cached {name} is outside the selected package; run this verifier "
                "in a fresh Python interpreter"
            )
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    names = ("camera_platform", "pyisetcam", "camera_platform.service")
    modules = {name: importlib.import_module(name) for name in names}
    paths = {name: str(Path(module.__file__).resolve()) for name, module in modules.items()}
    if not all(Path(path).is_relative_to(root) for path in paths.values()):
        raise RuntimeError("Installed verification client imported development modules")
    if modules["camera_platform.service"].environment_fingerprint() != expected_environment:
        raise RuntimeError("Installed verification client differs from the package manifest")
    return paths


def prescription_command(root, url, state, output, *, read_only=False):
    """Fresh isolated client process; no server or Worker lifecycle operations."""
    script = Path(__file__).resolve().with_name("verify_camera_prescription_optics.py")
    code = """
import runpy, sys
from pathlib import Path
root, script = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path[:0] = [str(root), str(script.parent)]
sys.argv = [str(script), *sys.argv[3:]]
runpy.run_path(str(script), run_name="__main__")
"""
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        code,
        str(root),
        str(script),
        "--base-url",
        url.rstrip("/"),
        "--expected-state",
        str(state),
        "--output",
        str(output),
        "--expected-runtime-root",
        str(root),
    ]
    if read_only:
        command.append("--read-only")
    return command


def assert_prescription_preserved(original, repeated):
    """Saved jobs, physical evidence and installed binding must all survive."""
    if repeated.get("status") != "passed":
        raise RuntimeError("Repeated installed prescription verification did not pass")
    for key in (
        "records",
        "environment",
        "static_sha256",
        "runtime_binding",
        "artifact_sha256",
        "published_record_sha256",
        "camera_record_sha256",
        "replay_record_sha256",
        "radiometry_record_sha256",
        "psf_record_sha256",
        "source_assembly_sha256",
        "source_model_sha256",
        "prescription_request",
        "radiometry_summary",
        "psf_summary",
        "physical_validation",
        "total_uncertainty",
    ):
        if original[key] != repeated[key]:
            raise RuntimeError(f"Installed prescription evidence changed: {key}")
    for key in ("camera", "replay"):
        if original["bundles"][key]["package_hash"] != repeated["bundles"][key]["package_hash"]:
            raise RuntimeError(f"Installed {key} ZIP manifest changed")
        if (
            original["bundles"][key]["artifacts_verified"]
            != repeated["bundles"][key]["artifacts_verified"]
        ):
            raise RuntimeError(f"Installed {key} ZIP artifact count changed")
    invocation = repeated["latest_invocation"]
    if (
        invocation["read_only"] is not True
        or invocation["post_count"] != 0
        or invocation["counts_before"] != invocation["counts_after"]
    ):
        raise RuntimeError("Installed prescription recheck must preserve counts with zero POSTs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--meep-python", type=Path)
    parser.add_argument("--mitsuba-python", type=Path, required=True)
    parser.add_argument(
        "--assets-manifest",
        type=Path,
        help="Bind verified upstream data and the selected Python in the installed runtime pin",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=launcher.WORKSPACE / "reports/camera_platform/installed-runtime-verification.json",
    )
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    manifest = launcher.verify_manifest(root)
    # Verify existing import state before loading tools that retain module aliases.
    client_paths = bind_installed_client(root, manifest["environment"])
    from verify_camera_control_delivery import verify as verify_feedback
    from verify_camera_response_delivery import verify as verify_response
    from verify_camera_scene_http import verify as verify_scene
    from verify_camera_scene_preparation import repeat as repeat_preparation
    from verify_camera_scene_preparation import verify as verify_preparation
    from verify_camera_scene_projection import repeat as repeat_projection
    from verify_camera_scene_projection import verify as verify_projection
    from verify_camera_temporal import verify

    from camera_platform.sdk import PlatformClient

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = launcher.WORKSPACE / "outputs/camera-platform-installed-qa" / stamp
    workspace, state = directory / "workspace", directory / "camera-platform-scene-qa-state"
    workspace.mkdir(parents=True)
    for base in [workspace, workspace / "src"]:
        for name in ["camera_platform", "pyisetcam"]:
            package = base / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "raise RuntimeError('development code imported')\n"
            )
    launcher.WORKSPACE = workspace
    assets = (
        launcher.asset_binding_from_manifest(args.assets_manifest)
        if args.assets_manifest is not None
        else None
    )
    if assets is not None:
        select(root, state, sys.executable, bind_python=True, assets=assets)
    else:
        select(root, state, sys.executable)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    report = {
        "schema": "camera_installed_runtime_verification_v1",
        "runtime_root": str(root),
        "workspace": str(workspace),
        "state_dir": str(state),
        "environment": manifest["environment"],
        "checks": [],
        "dependency_scope": "existing Python dependencies, not a clean OS installation",
        "physical_validation": "not_established",
        "verification_client_module_paths": client_paths,
        "asset_binding": assets,
        "python_binding_explicit": assets is not None,
    }
    pid = None

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    def launch():
        nonlocal pid
        options = ["--port", str(port), "--state-dir", str(state), "--no-open"]
        if args.meep_python:
            options += ["--meep-python", str(args.meep_python)]
        if (
            not (state / "runtime-config.json").exists()
            or launcher.load_runtime_config(state, "mitsuba_python") is None
        ):
            options += ["--mitsuba-python", str(args.mitsuba_python)]
        launcher.main(options)
        pid = int((state / "server.pid").read_text())
        launcher.verify_running_package(url, state, launcher.load_installed_runtime(state))
        return pid

    def stop():
        nonlocal pid
        if pid is None:
            return
        witness = launcher._json_file(state / "runtime-process.json")
        if witness["pid"] != pid or witness["runtime_root"] != str(root):
            raise RuntimeError("Refusing to stop a different runtime process")
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
        if str(state) not in command or str(root) not in command:
            raise RuntimeError("Refusing to stop a different command")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                finished, _ = os.waitpid(pid, os.WNOHANG)
                if finished == pid:
                    pid = None
                    return
            except ChildProcessError:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pid = None
                    return
            time.sleep(0.1)
        raise RuntimeError("Isolated server did not terminate; its state was preserved")

    def repeat(ids):
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            record = client.wait(
                client.submit_temporal(
                    ids["assembly"],
                    ids["test"],
                    ids["scene"],
                    ids["control"],
                    seed=741,
                    force=True,
                )["id"],
                timeout=180,
            )
            assert record["status"] == "succeeded", record.get("error")
            assert record["environment"] == manifest["environment"]
            original = client.get("runs/" + ids["run"])

            def raw(run):
                return next(a["sha256"] for a in run["artifacts"] if a["name"] == "raw_frames.npy")

            assert raw(record) == raw(original)
            return record["id"]

    def repeat_feedback(ids):
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            result = client.wait(
                client.submit_closed_loop(
                    ids["assembly"],
                    ids["test"],
                    ids["program"],
                    seed=993,
                    force=True,
                )["id"],
                timeout=180,
            )
            assert result["status"] == "succeeded", result.get("error")
            assert result["environment"] == manifest["environment"]
            original = client.get("runs/" + ids["run"])

            def raw(r):
                return next(a["sha256"] for a in r["artifacts"] if a["name"] == "raw_frames.npy")

            assert raw(result) == raw(original)
            assert (
                result["result"]["summary"]["control_trace"]
                == original["result"]["summary"]["control_trace"]
            )
            return result["id"]

    def repeat_response(proof):
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            ids = proof["records"]
            assert client.get("captures/" + ids["capture"])["content_hash"] == proof["capture_hash"]
            repeated = client.wait(
                client.submit(
                    ids["assembly"],
                    ids["test"],
                    plugin_id="closed_loop_scene_v1",
                    control_program_id=ids["program"],
                    parameters={"seed": 123},
                    force=True,
                )["id"],
                timeout=120,
                poll_interval=0.1,
            )
            assert (
                repeated["status"] == "succeeded"
                and repeated["environment"] == manifest["environment"]
            )
            original = client.get("runs/" + ids["run"])

            def raw(r):
                return next(a["sha256"] for a in r["artifacts"] if a["name"] == "raw_frames.npy")

            assert raw(repeated) == raw(original)
            comparison = client.compare(repeated["id"], ids["measurement"], ids["test"])
            assert comparison["status"] == "INCONCLUSIVE" and all(
                v == 0 for v in comparison["residuals"]["values"]
            )
            return repeated["id"]

    def repeat_scene(proof):
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            original = client.get("scene_renders/" + proof["records"]["pairs"][0]["render"])
            from camera_platform.scene_contracts import SceneRenderCreate

            request = {
                k: v for k, v in original["request"].items() if k in SceneRenderCreate.model_fields
            }
            request["force"] = True
            repeated = client.wait_scene_render(client.render_scene(request)["id"])
            assert repeated["status"] == "succeeded", repeated.get("error")
            assert repeated["environment"] == manifest["environment"]
            assert repeated["backend"] == original["backend"]
            for name in ["spectral-radiance.npy", "spectral-image.npy", "first-hit-depth-m.npy"]:

                def checksum(job, name=name):
                    return next(a["sha256"] for a in job["artifacts"] if a["name"] == name)

                assert checksum(repeated) == checksum(original)
            return repeated["id"]

    def prescription_check(*, read_only=False):
        checkpoint = directory / "prescription-http-verification.json"
        command = prescription_command(root, url, state, checkpoint, read_only=read_only)
        completed = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True, timeout=300
        )
        if completed.returncode:
            raise RuntimeError(
                "Installed prescription verifier failed; its saved job IDs remain in "
                f"{checkpoint}:\n{completed.stderr[-12000:]}\n{completed.stdout[-2000:]}"
            )
        proof = json.loads(checkpoint.read_text())
        if proof.get("status") != "passed" or proof["environment"] != manifest["environment"]:
            raise RuntimeError("Prescription verification did not use the selected environment")
        binding = proof["runtime_binding"]
        if (
            binding["mode"] != "verified_installed_package"
            or binding["runtime_root"] != str(root)
            or binding["wheel_sha256"] != manifest["wheel_sha256"]
        ):
            raise RuntimeError("Prescription verification imported another package")
        if not all(Path(p).is_relative_to(root) for p in binding["module_paths"].values()):
            raise RuntimeError("Prescription verification client imported development code")
        return proof

    try:
        first_pid = launch()
        passed("detached_launcher_imports_verified_package_despite_broken_checkout")
        temporal = verify(url.rstrip("/"), stamp)
        report["temporal"] = temporal
        assert temporal["environment"] == manifest["environment"]
        passed("real_psf_camera_temporal_and_raw_replay_execute_with_pinned_code")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            feedback = verify_feedback(client, temporal["records"]["assembly"], stamp)
        report["feedback"] = feedback
        assert feedback["environment"] == manifest["environment"]
        passed("feedback_policy_program_32_frame_worker_and_raw_replay_use_installed_code")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            response_proof = verify_response(client, feedback["records"]["program"], stamp)
        report["response"] = response_proof
        assert response_proof["environment"] == manifest["environment"]
        passed("installed_response_file_upload_common_observation_and_analysis_revisions")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            scene_proof = verify_scene(client, state)
        report["scene"] = scene_proof
        assert scene_proof["environment"] == manifest["environment"]
        passed("installed_scene_assets_mitsuba_material_swap_to_camera_raw_rgb")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            preparation = verify_preparation(client, state, scene_proof)
        report["preparation"] = preparation
        assert preparation["environment"] == manifest["environment"]
        passed("installed_camera_preparation_preserves_source_and_executes_mitsuba_raw_rgb")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            projection = verify_projection(
                client,
                state,
                preparation["records"]["render"],
                preparation["records"]["assembly"],
            )
        report["projection"] = projection
        assert projection["environment"] == manifest["environment"]
        passed("installed_projection_reuses_original_radiance_to_real_raw_without_new_render")
        prescription = prescription_check()
        report["prescription"] = prescription
        passed("installed_prescription_factories_same_parent_raw_rgb_replay_and_original_zip")
        source = workspace / "src/camera_platform"
        (source / "__init__.py").write_text("raise RuntimeError('changed during a live run')\n")
        static = source / "static"
        static.mkdir()
        (static / "app.js").write_text("throw Error('development UI must not be served');")
        report["after_development_edit"] = repeat(temporal["records"])
        report["feedback_after_development_edit"] = repeat_feedback(feedback["records"])
        report["response_after_development_edit"] = repeat_response(response_proof)
        report["scene_after_development_edit"] = repeat_scene(scene_proof)
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            report["preparation_after_development_edit"] = repeat_preparation(
                client, state, preparation
            )
            report["projection_after_development_edit"] = repeat_projection(
                client, state, projection
            )
        report["prescription_after_development_edit"] = prescription_check(read_only=True)
        assert_prescription_preserved(prescription, report["prescription_after_development_edit"])
        passed("checkout_edits_preserve_prescription_saved_ids_exact_raw_rgb_and_original_evidence")
        passed("editing_checkout_does_not_change_worker_environment_or_raw_hash")
        passed("checkout_edits_preserve_preparation_hash_and_downloaded_render_raw_rgb")
        passed("checkout_edits_preserve_original_radiance_new_irradiance_and_camera_bytes")
        assert launch() == first_pid
        passed("finder_equivalent_relaunch_reuses_verified_api_process")
        stop()
        second_pid = launch()
        assert second_pid != first_pid
        report["after_restart"] = repeat(temporal["records"])
        report["feedback_after_restart"] = repeat_feedback(feedback["records"])
        report["response_after_restart"] = repeat_response(response_proof)
        report["scene_after_restart"] = repeat_scene(scene_proof)
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            report["preparation_after_restart"] = repeat_preparation(client, state, preparation)
            report["projection_after_restart"] = repeat_projection(client, state, projection)
        report["prescription_after_restart"] = prescription_check(read_only=True)
        assert_prescription_preserved(prescription, report["prescription_after_restart"])
        passed("restart_preserves_prescription_saved_ids_exact_raw_rgb_and_original_evidence")
        passed("saved_projection_and_exact_seeded_camera_outputs_survive_restart")
        passed("saved_preparation_idempotent_save_and_render_raw_rgb_survive_restart")
        passed("persisted_mitsuba_venv_binding_and_exact_spectral_outputs_survive_restart")
        passed("response_acquisition_hash_analysis_replay_and_exact_raw_survive_restart")
        passed("feedback_raw_and_control_trace_survive_restart_and_checkout_edits")
        with PlatformClient(url.rstrip("/"), timeout=120) as client:
            report["audit"] = client.get("audit")
            assert report["audit"]["integrity"] == "verified"
        passed("restart_preserves_registry_audit_and_exact_seeded_raw")
        report["process_witness"] = launcher._json_file(state / "runtime-process.json")
        report["status"] = "passed"
    finally:
        stop()
        try:
            with urlopen(url + "api/v1/health", timeout=2):
                raise RuntimeError("Isolated HTTP listener remains live after shutdown")
        except URLError:
            report["isolated_server_stopped"] = True
        report["verified_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "report": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()

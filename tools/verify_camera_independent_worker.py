#!/usr/bin/env python3
"""Verify a separate dependency environment with detached API/Worker and restart.

Uses explicit installer functions and the existing installed bootstrap, never
the launcher CLI's implicit .venv-platform selection. Successful QA is left live
for browser verification; this report identifies owned process groups for cleanup.
All application state is newly created outside the checkout. Solver interpreters
remain external dependencies. Existing package/delivery proofs are never edited.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import start_camera_platform as launcher
from install_camera_platform_runtime import prepare, select, verify_wheel
from prepare_camera_platform_environment import stop_process_group
from verify_camera_environment_execution import PROBE
from verify_camera_installed_runtime import assert_prescription_preserved, prescription_command


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def binding(path):
    return {"path": str(Path(path).absolute()), "sha256": sha(path)}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def package_proof(execution, environment, wheel, python):
    """Derive acceptance only from this independently executed environment."""
    require(
        execution.get("schema") == "camera_independent_environment_execution_v1"
        and execution.get("status") == "passed"
        and execution.get("original_proof_reused_as_execution_evidence") is False
        and execution.get("process_stopped") is True
        and execution.get("cleanup", {}).get("process_group_stopped") is True,
        "Independent calculation execution is not complete",
    )
    require(
        environment.get("status") == "prepared"
        and environment.get("pip_check") == "passed"
        and environment.get("system_site_packages") is False,
        "Independent environment preparation is incomplete",
    )
    require(
        Path(execution["python"]).absolute() == python
        and Path(environment["destination"]).absolute() == python.parent.parent,
        "Calculation and dependency proof refer to another interpreter",
    )
    calculation = execution["calculation"]
    require(
        calculation.get("status") == "passed"
        and calculation["environment"]
        == execution["probe"]["environment"]
        == environment["environment"],
        "Independent computation environment differs from its prepared environment",
    )
    wheel_sha = sha(wheel)
    require(
        execution["wheel_sha256"] == environment["wheel_sha256"] == wheel_sha,
        "Execution/preparation wheel identity differs",
    )
    with zipfile.ZipFile(wheel) as archive:
        payload = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith(("camera_platform/", "pyisetcam/")) and not name.endswith("/")
        }
    require(
        payload and execution["probe"]["payload_sha256"] == payload,
        "Calculation proof does not bind every installed wheel payload file",
    )
    proof = {
        **copy.deepcopy(calculation),
        "wheel_sha256": wheel_sha,
        "all_checked_source_static_bytes_match": True,
        "independent_payload_file_count": len(payload),
        "source": "Derived from this independent environment's actual calculation; "
        "old candidate success is not reused",
        "original_candidate_execution_reused": False,
    }
    verify_wheel(wheel, proof)
    return proof


def rows(state):
    with sqlite3.connect((state / "registry.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        return {
            table: {
                str(row[key]): hashlib.sha256(
                    json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                for row in connection.execute("SELECT * FROM " + table)
            }
            for table, key in (("records", "id"), ("jobs", "id"), ("audit", "seq"))
        }


def api(url, path):
    with build_opener(ProxyHandler({})).open(url + "/api/v1/" + path, timeout=10) as response:
        return json.load(response)


def processes():
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="],
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    entries = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and all(value.isdigit() for value in parts[:3]):
            entries.append(
                dict(
                    zip(
                        ("pid", "ppid", "pgid", "command"),
                        [*(int(value) for value in parts[:3]), parts[3]],
                        strict=True,
                    )
                )
            )
    require(any(item["pid"] == os.getpid() for item in entries), "Process inventory is incomplete")
    return entries


def launch_command(python, root, state, manifest_sha, workspace, port):
    require(
        python.is_absolute() and root.is_absolute() and state.is_absolute(),
        "Explicit absolute launch paths required",
    )
    require(
        "qa" in state.name.lower() and state != launcher.WORKSPACE / "outputs/camera-platform",
        "Detached verification requires a separate QA state",
    )
    require(port != 8770 and 1024 <= port <= 65535, "Never use the primary workbench port")
    return [
        str(python),
        "-P",
        "-B",
        "-c",
        launcher.INSTALLED_BOOTSTRAP,
        str(root),
        str(state),
        manifest_sha,
        "serve",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
        "--port",
        str(port),
    ]


class OwnedAPI:
    """Retain the newest process immediately, including failed start/restart attempts."""

    def __init__(self):
        self.process = None

    def spawn(self, command, *, cwd, env, log):
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
        return self.process

    def cleanup(self):
        return stop_process_group(self.process) if self.process is not None else None


def run(args):
    python = args.python.absolute()  # Retain venv symlink identity.
    output = args.output_directory.absolute()
    require(not output.exists() and not output.is_symlink(), "Use a fresh evidence directory")
    output.mkdir(parents=True)
    report_path = output / "verification.json"
    report = {
        "schema": "camera_independent_worker_restart_v1",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "python": str(python),
        "activated_in_primary": False,
        "primary_accessed": False,
        "scope": "Independent application dependencies, detached API/Worker, prescription optics "
        "and restart. External solver environments reused; browser/clean OS/physical accuracy "
        "not verified.",
        "inputs": {
            name: binding(path)
            for name, path in {
                "environment": args.environment_report,
                "execution": args.execution_proof,
                "wheel": args.wheel,
                "verifier": Path(__file__),
                "installer": launcher.WORKSPACE / "tools/install_camera_platform_runtime.py",
                "launcher": Path(launcher.__file__),
                "prescription_verifier": launcher.WORKSPACE
                / "tools/verify_camera_prescription_optics.py",
                "environment_execution_helper": launcher.WORKSPACE
                / "tools/verify_camera_environment_execution.py",
                "cleanup_helper": launcher.WORKSPACE
                / "tools/prepare_camera_platform_environment.py",
                "installed_runtime_helper": launcher.WORKSPACE
                / "tools/verify_camera_installed_runtime.py",
            }.items()
        },
        "process_lifecycle": [],
        "checks": [],
    }
    save(report_path, report)
    environment, execution = read(args.environment_report), read(args.execution_proof)
    report["external_solvers"] = execution["external_solvers"]
    external = Path(tempfile.mkdtemp(prefix="camera-independent-worker-qa-")).resolve()
    workspace, state = external / "workspace", external / "camera-platform-independent-qa-state"
    workspace.mkdir()
    for parent in (workspace, workspace / "src"):
        for name in ("camera_platform", "pyisetcam"):
            package = parent / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "raise RuntimeError('checkout must not be imported')\n"
            )
    report.update(workspace=str(workspace), state_dir=str(state))
    save(report_path, report)
    process = None
    owner = OwnedAPI()
    try:
        accepted = package_proof(execution, environment, args.wheel, python)
        accepted["source_execution"] = binding(args.execution_proof)
        accepted["source_environment"] = binding(args.environment_report)
        accepted_path = output / "independent-package-proof.json"
        save(accepted_path, accepted)
        # Re-observe installed dependencies and payload now, independent of the historical report.
        observed = subprocess.run(
            [str(python), "-I", "-B", "-c", PROBE, str(args.wheel.absolute())],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
            timeout=90,
        )
        fresh = json.loads(observed.stdout)
        require(fresh == execution["probe"], "Independent installed payload or environment changed")
        report["fresh_probe"] = fresh
        check = subprocess.run(
            [str(python), "-I", "-m", "pip", "--isolated", "check"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        report["pip_check"] = check.stdout.strip()
        root = prepare(
            args.wheel, accepted_path, executable=python, base_directory=output / "runtimes"
        )
        pin = select(root, state, python)
        manifest = launcher.verify_manifest(root)
        require(
            manifest["environment"] == accepted["environment"],
            "Prepared runtime environment differs",
        )
        report.update(
            runtime_root=str(root),
            runtime_pin=pin,
            environment=manifest["environment"],
            accepted_package_proof=binding(accepted_path),
        )
        for solver, key in (("meep", "meep_python"), ("mitsuba", "mitsuba_python")):
            launcher.save_runtime_config(state, execution["external_solvers"][solver], key)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        report.update(base_url=url, port=port)
        save(report_path, report)

        def launch():
            nonlocal process
            command = launch_command(python, root, state, pin["manifest_sha256"], workspace, port)
            env = {
                **os.environ,
                "PYTHONPATH": str(root),
                "PYTHONSAFEPATH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                launcher.MEEP_ENV: execution["external_solvers"]["meep"],
                launcher.MITSUBA_ENV: execution["external_solvers"]["mitsuba"],
            }
            with (state / "server.log").open("ab") as log:
                owned = owner.spawn(
                    command,
                    cwd=workspace,
                    env=env,
                    log=log,
                )
            process = owned  # Retain ownership even if readiness/binding verification fails.
            record = {
                "event": "launch",
                "api_pid": owned.pid,
                "api_pgid": owned.pid,
                "command": command,
                "started_at": datetime.now(UTC).isoformat(),
            }
            report["process_lifecycle"].append(record)
            save(report_path, report)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                require(owned.poll() is None, "Detached API exited during start")
                try:
                    health = api(url, "health")
                    if health.get("worker") == "running":
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise TimeoutError("Detached API not ready; preserve its owned PID for observation")
            require(
                health["workspace"] == str(workspace) and health["state_dir"] == str(state),
                "API is not the new external QA state",
            )
            (state / "server.pid").write_text(str(owned.pid) + "\n")
            launcher.verify_running_package(
                url + "/", state, launcher.load_installed_runtime(state)
            )
            children = [
                item
                for item in processes()
                if item["ppid"] == owned.pid and "camera_platform.worker" in item["command"]
            ]
            require(
                len(children) == 1
                and children[0]["command"].startswith(str(python) + " ")
                and str(state) in children[0]["command"],
                "Worker interpreter or state is not bound",
            )
            record.update(worker=children[0], witness=read(state / "runtime-process.json"))
            save(report_path, report)
            return owned

        def prescription(name, *, read_only=False):
            receipt = output / "prescription.json"
            command = prescription_command(root, url, state, receipt, read_only=read_only)
            command[0] = str(python)
            with (
                (output / (name + ".stdout.log")).open("w") as stdout,
                (output / (name + ".stderr.log")).open("w") as stderr,
            ):
                result = subprocess.run(
                    command, cwd=workspace, stdout=stdout, stderr=stderr, timeout=300, check=True
                )
            require(result.returncode == 0, "Prescription client failed")
            value = read(receipt)
            save(output / (name + ".json"), value)
            return value

        process = launch()
        report["before_rows"] = rows(state)
        first = prescription("prescription-initial")
        require(
            first["environment"] == manifest["environment"], "Actual Worker environment differs"
        )
        report["prescription"] = first
        report["after_execution_rows"] = rows(state)
        report["checks"].append(
            {"name": "actual_independent_worker_prescription_raw_rgb_replay", "status": "passed"}
        )
        save(report_path, report)
        cleanup = stop_process_group(process)
        require(cleanup["process_group_stopped"], "First detached API/Worker did not stop")
        report["process_lifecycle"].append({"event": "restart_shutdown", **cleanup})
        before_restart = rows(state)
        save(report_path, report)
        previous_pid = process.pid
        process = launch()
        require(process.pid != previous_pid, "Restart did not produce a new API process")
        repeated = prescription("prescription-after-restart", read_only=True)
        assert_prescription_preserved(first, repeated)
        require(
            rows(state) == before_restart == report["after_execution_rows"],
            "Restart/recheck changed saved registry rows",
        )
        report["prescription_after_restart"] = repeated
        report["after_restart_rows"] = rows(state)
        report["checks"].append(
            {
                "name": "restart_preserves_all_rows_saved_ids_raw_rgb_and_evidence_zero_posts",
                "status": "passed",
            }
        )
        hashes = {
            kind: next(
                key.split(":", 1)[1]
                for key in first["artifact_sha256"][record]
                if key.startswith(name + ":")
            )
            for kind, record, name in [
                ("raw_sha256", "camera", "raw_frames.npy"),
                ("rgb_sha256", "camera", "rgb_frames.npy"),
                ("replay_rgb_sha256", "replay", "rgb_frames.npy"),
            ]
        }
        require(
            all(
                value == execution["calculation"]["prescription_optics"][key]
                for key, value in hashes.items()
            ),
            "Detached Worker outputs differ from independent calculation",
        )
        report["actual_output_hashes"] = hashes
        report["checks"].append(
            {
                "name": "detached_outputs_equal_independently_verified_calculation",
                "status": "passed",
            }
        )
        for item in report["inputs"].values():
            require(sha(item["path"]) == item["sha256"], "Bound input changed")
        report.update(
            status="passed_for_declared_scope",
            live_for_browser=True,
            api_pid=process.pid,
            worker=report["process_lifecycle"][-1]["worker"],
            cleanup_owner="root after browser verification",
            verified_at=datetime.now(UTC).isoformat(),
        )
        save(report_path, report)
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "status",
                        "base_url",
                        "python",
                        "workspace",
                        "state_dir",
                        "runtime_root",
                        "api_pid",
                        "worker",
                    )
                },
                indent=2,
            )
        )
    except BaseException as error:
        report.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            if owner.process is not None:
                report["failure_cleanup"] = owner.cleanup()
        except BaseException as cleanup_error:
            report["failure_cleanup"] = {
                "process_group": owner.process.pid,
                "process_group_stopped": False,
                "error": f"{type(cleanup_error).__name__}: {cleanup_error}",
            }
        finally:
            save(report_path, report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("python", "environment-report", "execution-proof", "wheel", "output-directory"):
        parser.add_argument("--" + flag, type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()

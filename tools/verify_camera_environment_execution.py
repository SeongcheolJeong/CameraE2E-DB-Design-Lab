#!/usr/bin/env python3
"""Run the installed calculation suite in an independently resolved application venv.

The original candidate proof is an input identity, not evidence for this environment.
External Meep/Mitsuba interpreters remain separately declared dependencies. This is
an API TestClient calculation check, not browser, detached-Worker or customer-OS QA.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from install_camera_platform_runtime import verify_wheel
from prepare_camera_platform_environment import stop_process_group
from start_camera_platform import WORKSPACE, _json_file

PROBE = r"""
import hashlib, importlib.metadata, json, site, sys, sysconfig, zipfile
from pathlib import Path
import camera_platform, pyisetcam
from camera_platform.service import environment_fingerprint
prefix = Path(sys.prefix).resolve()
base = Path(sys.base_prefix).resolve()
assert sys.prefix != sys.base_prefix, "A dedicated virtual environment is required"
assert site.ENABLE_USER_SITE is False, "User site packages must be disabled"
assert "include-system-site-packages = false" in (prefix / "pyvenv.cfg").read_text().lower()
for entry in sys.path:
    path = Path(entry).resolve()
    if path.is_relative_to(prefix):
        continue
    assert path.is_relative_to(base), "External import search path: " + str(path)
    assert not {"site-packages", "dist-packages"}.intersection(path.parts), str(path)
paths = {m.__name__: str(Path(m.__file__).resolve()) for m in (camera_platform, pyisetcam)}
assert all(Path(p).is_relative_to(prefix) for p in paths.values())
purelib = Path(sysconfig.get_path("purelib")).resolve()
assert purelib.is_relative_to(prefix)
distributions = []
for d in importlib.metadata.distributions():
    location = Path(d.locate_file("")).resolve()
    assert location.is_relative_to(prefix), str(location)
    distributions.append({"name": d.metadata["Name"], "version": d.version,
                          "location": str(location)})
payload = {}
with zipfile.ZipFile(sys.argv[1]) as archive:
    for name in archive.namelist():
        if name.startswith(("camera_platform/", "pyisetcam/")) and not name.endswith("/"):
            installed = purelib / name
            assert not installed.is_symlink(), name
            data = archive.read(name)
            assert installed.read_bytes() == data, "Installed wheel payload differs: " + name
            payload[name] = hashlib.sha256(data).hexdigest()
assert payload
print(json.dumps({"prefix": str(prefix), "base_prefix": sys.base_prefix,
    "purelib": str(purelib), "module_paths": paths, "sys_path": sys.path,
    "environment": environment_fingerprint(), "payload_sha256": payload,
    "distributions": sorted(distributions, key=lambda d: d["name"].lower())}))
"""


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    for name in ("python", "wheel", "package_proof", "meep_python", "mitsuba_python", "output"):
        # Preserve the venv interpreter symlink; resolve() would select its base Python.
        setattr(args, name, getattr(args, name).absolute())
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise RuntimeError("Use a new report path; prior evidence is never overwritten")
    original = _json_file(args.package_proof)
    wheel_sha = verify_wheel(args.wheel, original)
    script_source = WORKSPACE / "tools/verify_camera_package.py"
    tree = ast.parse(script_source.read_text())
    child = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHILD" for t in node.targets)
    )
    outside = Path(tempfile.mkdtemp(prefix="camera-independent-execution-")).resolve()
    report = {
        "schema": "camera_independent_environment_execution_v1",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "python": str(args.python.absolute()),
        "workspace": str(outside),
        "wheel_sha256": wheel_sha,
        "package_proof_sha256": checksum(args.package_proof),
        "verification_tool_sha256": checksum(__file__),
        "cleanup_tool_sha256": checksum(WORKSPACE / "tools/prepare_camera_platform_environment.py"),
        "calculation_suite_sha256": checksum(script_source),
        "child_sha256": hashlib.sha256(child.encode()).hexdigest(),
        "external_solvers": {
            "meep": str(args.meep_python.absolute()),
            "mitsuba": str(args.mitsuba_python.absolute()),
        },
        "scope": "Fresh application venv, installed payload and API TestClient calculations. "
        "External solver environments are reused; no browser, detached Worker, "
        "clean OS or physical-camera validation claim.",
        "original_proof_reused_as_execution_evidence": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    save()
    process = None
    try:
        probe = subprocess.run(
            [str(args.python), "-I", "-B", "-c", PROBE, str(args.wheel.absolute())],
            cwd=outside,
            capture_output=True,
            text=True,
            check=True,
            timeout=90,
        )
        report["probe"] = json.loads(probe.stdout)
        check = subprocess.run(
            [str(args.python), "-I", "-m", "pip", "--isolated", "check"],
            cwd=outside,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        report["pip_check"] = check.stdout.strip()
        report["original_environment_equal"] = (
            report["probe"]["environment"] == original["environment"]
        )
        script = outside / "verify.py"
        script.write_text(child)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["CAMERA_PLATFORM_MEEP_PYTHON"] = str(args.meep_python.absolute())
        env["CAMERA_PLATFORM_MITSUBA_PYTHON"] = str(args.mitsuba_python.absolute())
        stdout, stderr = outside / "stdout.json", outside / "stderr.log"
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(
                [str(args.python), "-I", "-B", str(script), report["probe"]["purelib"]],
                cwd=outside,
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            report.update(pid=process.pid, stdout=str(stdout), stderr=str(stderr))
            save()
            code = process.wait(timeout=600)
        report["exit_code"] = code
        if code:
            raise RuntimeError("Installed calculations failed; inspect " + str(stderr))
        result = json.loads(stdout.read_text())
        if (
            result.get("status") != "passed"
            or result["environment"] != report["probe"]["environment"]
        ):
            raise RuntimeError("Calculation result or environment differs from its import probe")
        report["calculation"] = result
        comparisons = {
            name: result[name] == original[name] for name in ("raw_sha256", "scene_radiance_sha256")
        }
        comparisons.update(
            {
                "prescription_" + name: result["prescription_optics"][name]
                == original["prescription_optics"][name]
                for name in ("raw_sha256", "rgb_sha256", "replay_rgb_sha256")
            }
        )
        report["original_artifact_hash_comparisons"] = comparisons
        report["selected_original_artifact_hashes_equal"] = all(comparisons.values())
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", failure=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            if process is not None:
                cleanup = stop_process_group(process)
                report["cleanup"] = cleanup
                report["process_stopped"] = process.poll() is not None
                if not cleanup["process_group_stopped"]:
                    raise RuntimeError("Calculation process group still has live members")
        except BaseException as error:
            report.update(status="failed", cleanup_error=f"{type(error).__name__}: {error}")
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--package-proof", type=Path, required=True)
    parser.add_argument("--meep-python", type=Path, required=True)
    parser.add_argument("--mitsuba-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    result = run(parser.parse_args())
    print(
        json.dumps(
            {
                "status": result["status"],
                "workspace": result["workspace"],
                "selected_original_artifact_hashes_equal": result[
                    "selected_original_artifact_hashes_equal"
                ],
            }
        )
    )


if __name__ == "__main__":
    main()

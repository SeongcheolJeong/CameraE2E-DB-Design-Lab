#!/usr/bin/env python3
"""Qualify a fresh case-enabled wheel with installed TestClient calculations.

Uses the selected Python's existing dependencies. No detached worker, live
server, physical-camera validation, or primary-state mutation is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {"camera_platform", "pyisetcam"}
TEXT_SUFFIXES = {".py", ".js", ".html", ".css"}
REQUIRED = {
    "camera_platform/__init__.py",
    "camera_platform/case_contracts.py",
    "camera_platform/case_reviews.py",
    "camera_platform/sdk.py",
    "camera_platform/static/cases.js",
    "camera_platform/static/index.html",
    "camera_platform/static/app.js",
    "pyisetcam/__init__.py",
}

CHILD = r"""
import hashlib, io, json, sys
from pathlib import Path
target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
import camera_platform, pyisetcam, numpy as np
from camera_platform.api import create_app
from camera_platform.registry import digest
from camera_platform.service import environment_fingerprint
from fastapi.testclient import TestClient
def check(value, message):
    if not value: raise RuntimeError(message)
app = create_app(Path.cwd() / "state", workspace=Path.cwd(), start_worker=False)
service = app.state.service
with TestClient(app) as http:
    def get(path):
        response = http.get(path)
        check(response.status_code == 200, response.text)
        return response.json()
    def post(path, body, status=201):
        response = http.post(path, json=body)
        check(response.status_code == status, response.text)
        return response.json()
    session = get("/api/v1/session")
    http.headers["X-Platform-Token"] = session["token"]
    static = Path(camera_platform.__file__).parent / "static"
    static_hashes = {}
    for file in sorted(static.iterdir()):
        if not file.is_file(): continue
        response = http.get("/" + file.name)
        check(response.status_code == 200, "Static HTTP status: " + file.name)
        check(response.content == file.read_bytes(), "Static HTTP bytes: " + file.name)
        static_hashes[file.name] = hashlib.sha256(response.content).hexdigest()
    check('src="/cases.js"' in http.get("/").text, "Case script missing from index")
    boot = get("/api/v1/bootstrap")
    assembly, test = boot["assemblies"][0], boot["tests"][0]
    queued = post("/api/v1/runs", {"assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "response_db", "parameters": {"frames": 2, "output_size": [8, 8]}}, 202)
    job = service.registry.claim()
    check(job["id"] == queued["id"], "Claimed another job")
    service.registry.finish(job["id"], result=service.execute_run(job))
    run = get("/api/v1/runs/" + job["id"])
    check(run["status"] == "succeeded", "Installed calculation failed")
    artifacts, shapes = [], {}
    for artifact in run["artifacts"]:
        response = http.get(artifact["url"])
        check(response.status_code == 200, "Artifact HTTP failed")
        check(len(response.content) == artifact["bytes"], "Artifact size mismatch")
        check(hashlib.sha256(response.content).hexdigest() == artifact["sha256"], "Artifact hash")
        artifacts.append({key: artifact[key] for key in ("name", "bytes", "sha256")})
        if artifact["name"] in {"raw_frames.npy", "rgb.npy"}:
            array = np.load(io.BytesIO(response.content), allow_pickle=False)
            check(np.isfinite(array).all(), "Nonfinite calculated image")
            shapes[artifact["name"]] = list(array.shape)
    check(shapes == {"raw_frames.npy": [2, 8, 8], "rgb.npy": [8, 8, 3]}, "Image shapes")
    payload = {"request_id": "package-case-first", "name": "Installed case",
        "question": "What does this calculated response establish?",
        "requirements": [{"key": "R1", "description": "Fixed response", "test_id": test["id"]}],
        "assembly_ids": [assembly["id"]],
        "evidence": [{"kind": "runs", "id": run["id"], "requirement_keys": ["R1"]}],
        "decision": {"status": "investigate", "rationale": "Independent RAW remains unavailable"},
        "unresolved": ["Physical accuracy unestablished"]}
    first = post("/api/v1/case_reviews", payload)
    audit = service.registry.audit()
    check(post("/api/v1/case_reviews", payload) == first, "Retry changed the record")
    check(service.registry.audit() == audit, "Retry changed the audit")
    post("/api/v1/case_reviews", {**payload, "question": "changed identity"}, 409)
    second_payload = {**payload, "request_id": "package-case-revised",
        "previous_revision_id": first["id"], "unresolved": ["Acquire independent RAW"]}
    second = post("/api/v1/case_reviews", second_payload)
    post("/api/v1/case_reviews", {**second_payload, "request_id": "package-case-stale"}, 409)
    check(second["revision"] == 2 and second["case_id"] == first["case_id"], "Revision identity")
    check(get("/api/v1/case_reviews") == [second], "Latest case listing differs")
    check(get("/api/v1/case_reviews/" + first["id"]) == first, "Prior revision changed")
    history = get("/api/v1/case_reviews/" + second["id"] + "/history")
    check(history == [first, second], "History differs")
    frozen = second["snapshots"]["evidence"][0]
    check(frozen["snapshot"] == run and frozen["record_sha256"] == digest(run), "Full evidence")
    check(second["authority"] == {"engineering_note": True,
        "physical_validation": False, "release_approval": False}, "Case conferred authority")
    projection = get("/api/v1/workbench")["case_reviews"][0]
    detail = get("/api/v1" + projection["_projection"]["detail_path"])
    check(detail["record"] == second, "Workbench detail differs")
    audit = service.registry.audit()
    check(audit["integrity"] == "verified", "Registry audit failed")
module_paths = {name: str(Path(module.__file__).resolve())
    for name, module in sys.modules.items()
    if name.split('.')[0] in {"camera_platform", "pyisetcam"}
    and getattr(module, "__file__", None)}
check(module_paths and all(Path(p).is_relative_to(target)
    for p in module_paths.values()), "Product imports outside target")
print(json.dumps({"status": "passed", "platform_version": camera_platform.__version__,
    "environment": environment_fingerprint(), "installed_module_paths": module_paths,
    "static_http_sha256": static_hashes, "static_http_matches_installed_bytes": True,
    "artifact_http_hashes": artifacts, "image_shapes": shapes,
    "raw_and_rgb_artifact_http_hashes_verified": True,
    "case_revision_ids": [first["id"], second["id"]], "case_retry_and_history_verified": True,
    "registry_integrity": audit["integrity"], "audit_head": audit["head_hash"]}))
"""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _wheel_payload(data):
    """Reject ambiguous/unsafe ZIP entries before invoking pip."""
    payload, names, total = {}, set(), 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or str(path) != entry.filename.rstrip("/")
                or entry.filename in names
                or entry.flag_bits & 1
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
            ):
                raise RuntimeError("Unsupported wheel archive path or entry")
            names.add(entry.filename)
            total += entry.file_size
            if total > 512 * 1024**2 or len(names) > 20_000:
                raise RuntimeError("Wheel exceeds supported package limits")
            if not entry.is_dir() and path.parts[0] in PACKAGES:
                payload[entry.filename] = archive.read(entry)
    if not REQUIRED <= payload.keys():
        raise RuntimeError("Wheel is missing required case platform files")
    return payload


def _files(root, *, text_only=False):
    result = {}
    for package in sorted(PACKAGES):
        folder = root / package
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError("Missing or symbolic product package")
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("Symbolic product file")
            if path.is_dir():
                continue
            if not path.is_file():
                raise RuntimeError("Non-regular product file")
            if not text_only or path.suffix in TEXT_SUFFIXES:
                result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _equal_files(expected, actual, description):
    missing, extra = (
        sorted(expected.keys() - actual.keys()),
        sorted(actual.keys() - expected.keys()),
    )
    changed = sorted(
        name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
    )
    if missing or extra or changed:
        raise RuntimeError(
            f"{description} differ: missing={missing[:8]}, extra={extra[:8]}, changed={changed[:8]}"
        )


def _run(command, *, cwd, output, phase, timeout):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    for suffix, content in (("stdout", result.stdout), ("stderr", result.stderr)):
        with (output / f"{phase}.{suffix}.log").open("x") as stream:
            stream.write(content)
    if result.returncode:
        raise RuntimeError(f"{phase} failed: " + result.stderr[-4000:])
    return result.stdout


def verify(wheel, output_directory, *, executable=sys.executable, source_root=None):
    wheel = Path(wheel).expanduser().absolute()
    output = Path(output_directory).expanduser().absolute()
    executable = Path(executable).expanduser().absolute()
    source_root = Path(source_root or ROOT / "src").resolve()
    if os.path.lexists(output):
        raise FileExistsError("Output directory must be fresh; refusing to overwrite")
    if not output.parent.is_dir():
        raise FileNotFoundError("Output parent directory must already exist")
    if any(
        output.resolve().is_relative_to(path)
        for path in (ROOT / "src", ROOT / "tools", ROOT / "tests", ROOT / "outputs/camera-platform")
    ):
        raise RuntimeError("Output must not be inside source or primary state")
    if wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl":
        raise RuntimeError("Select a regular local wheel file")
    wheel_data = wheel.read_bytes()
    payload = _wheel_payload(wheel_data)
    source = _files(source_root, text_only=True)
    text_payload = {
        name: data for name, data in payload.items() if Path(name).suffix in TEXT_SUFFIXES
    }
    _equal_files(source, text_payload, "Source and wheel product text")
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="camera-case-package-") as temporary:
            outside = Path(temporary).resolve()
            if outside.is_relative_to(ROOT):
                raise RuntimeError("Installation temporary directory must be outside checkout")
            target = outside / "installed"
            frozen_wheel = outside / wheel.name
            frozen_wheel.write_bytes(wheel_data)
            _run(
                [
                    str(executable),
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    "--target",
                    str(target),
                    str(frozen_wheel),
                ],
                cwd=outside,
                output=output,
                phase="install",
                timeout=120,
            )
            _equal_files(payload, _files(target), "Installed wheel payload")
            script = outside / "verify.py"
            script.write_text(CHILD)
            result = _run(
                [str(executable), "-I", "-B", str(script), str(target)],
                cwd=outside,
                output=output,
                phase="calculation",
                timeout=90,
            )
            report = json.loads(result)
            if (
                report.get("status") != "passed"
                or not report.get("environment")
                or report.get("raw_and_rgb_artifact_http_hashes_verified") is not True
                or report.get("case_retry_and_history_verified") is not True
                or report.get("registry_integrity") != "verified"
            ):
                raise RuntimeError("Installed calculation did not produce the required proof")
            _equal_files(payload, _files(target), "Installed payload after calculation")
        _equal_files(
            source, _files(source_root, text_only=True), "Source changed during qualification"
        )
        if wheel.read_bytes() != wheel_data:
            raise RuntimeError("Wheel changed during qualification")
        report.update(
            schema="camera_case_package_proof_v1",
            wheel=str(wheel.resolve()),
            wheel_sha256=_sha(wheel_data),
            wheel_bytes=len(wheel_data),
            source_static_file_count=len(source),
            checked_source_static_sha256={name: _sha(data) for name, data in source.items()},
            installed_payload_file_count=len(payload),
            all_checked_source_static_bytes_match=True,
            temporary_state_removed=True,
            execution_mode="installed TestClient; calculations executed in the test process",
            detached_worker_verified=False,
            physical_validation=False,
            scope="Installed wheel outside checkout with -I -B and selected Python's existing "
            "dependencies; not a fresh dependency install or physical-camera validation",
        )
        with (output / "package-proof.json").open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return report
    except Exception as error:
        with (output / "failure.json").open("x") as stream:
            json.dump({"status": "failed", "error": str(error)}, stream, indent=2)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    report = verify(args.wheel, args.output_directory, executable=args.python)
    print(
        json.dumps(
            {
                "status": report["status"],
                "wheel_sha256": report["wheel_sha256"],
                "report": str(args.output_directory / "package-proof.json"),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

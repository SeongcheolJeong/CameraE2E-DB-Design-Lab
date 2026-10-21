#!/usr/bin/env python3
"""Prepare a verified local wheel as an immutable runtime and optionally select it.

Dependencies come from the selected Python. An explicit --python persists that
verified interpreter for subsequent launches; omission retains legacy defaults.
This tool does not create or qualify a clean OS/dependency environment.
Selecting a package never interrupts a live API or Worker.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from start_camera_platform import (
    PACKAGE_SCHEMA,
    WORKSPACE,
    _fingerprint,
    _json_file,
    asset_binding_from_manifest,
    default_runtime_python,
    load_installed_runtime,
    package_files,
    probe_package,
    runtime_python_path,
    save_runtime_pin,
    verify_manifest,
)


def verify_wheel(wheel, report):
    wheel = Path(wheel)
    if wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl":
        raise RuntimeError("Select a regular local wheel file")
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if (
        not isinstance(report, dict)
        or report.get("status") != "passed"
        or report.get("wheel_sha256") != checksum
        or report.get("all_checked_source_static_bytes_match") is not True
        or report.get("raw_and_rgb_artifact_http_hashes_verified") is not True
        or report.get("registry_integrity") != "verified"
        or not isinstance(report.get("environment"), dict)
    ):
        raise RuntimeError(
            "Wheel does not match an accepted installed-execution verification report"
        )
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set()
            size = 0
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or member.filename in names
                    or member.flag_bits & 1
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise RuntimeError("Unsupported wheel archive path or entry")
                names.add(member.filename)
                size += member.file_size
            if (
                size > 512 * 1024**2
                or len(names) > 20000
                or not {"camera_platform/__init__.py", "pyisetcam/__init__.py"} <= names
            ):
                raise RuntimeError("Wheel exceeds the supported Camera Platform package contract")
    except zipfile.BadZipFile as error:
        raise RuntimeError("Invalid wheel archive") from error
    return checksum


def prepare(wheel, report_path, *, executable, base_directory):
    report = _json_file(report_path)
    checksum = verify_wheel(wheel, report)
    expected = report["environment"]
    base = Path(base_directory).resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = base / (checksum[:20] + "-" + _fingerprint(expected)[:16])
    with (base / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if root.exists() or root.is_symlink():
            manifest = verify_manifest(root)
            if (
                manifest.get("wheel_sha256") != checksum
                or manifest.get("environment") != expected
                or package_files(root) != manifest.get("files")
            ):
                raise RuntimeError("Existing runtime directory differs from its immutable package")
            if probe_package(executable, root)["environment"] != expected:
                raise RuntimeError(
                    "Selected runtime dependencies differ from the verified environment"
                )
            return root
        with tempfile.TemporaryDirectory(prefix=".install-", dir=base) as temporary:
            stage = Path(temporary) / "package"
            command = [
                str(executable),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--no-compile",
                "--target",
                str(stage),
                str(Path(wheel).resolve()),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    "Local wheel installation failed: " + error.stderr[-2000:]
                ) from error
            actual = probe_package(executable, stage)
            if actual["environment"] != expected:
                raise RuntimeError(
                    "Installed package environment differs from the verified artifact; "
                    "run installed-package verification again"
                )
            manifest = {
                "schema": PACKAGE_SCHEMA,
                "wheel_sha256": checksum,
                "environment": expected,
                "files": package_files(stage),
            }
            (stage / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            os.rename(stage, root)
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
        return root


def select(root, state_dir, executable, *, bind_python=False, assets=None):
    """Verify a previously prepared package before saving the launcher selection."""
    manifest = verify_manifest(root)
    root = Path(root).resolve()
    if not bind_python and probe_package(executable, root)["environment"] != manifest.get(
        "environment"
    ):
        raise RuntimeError("Selected runtime failed package or environment verification")
    pin = save_runtime_pin(
        state_dir, root, executable=executable if bind_python else None, assets=assets
    )
    assert load_installed_runtime(state_dir)["runtime_root"] == str(root)
    return pin


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wheel", type=Path)
    mode.add_argument(
        "--select", type=Path, help="Select an already prepared version, including rollback"
    )
    parser.add_argument(
        "--verification-report",
        type=Path,
        default=WORKSPACE / "reports/camera_platform/package-verification.json",
    )
    parser.add_argument("--state-dir", type=Path, default=WORKSPACE / "outputs/camera-platform")
    parser.add_argument(
        "--python",
        type=Path,
        help="Verify and persist this Python, preserving its venv bin/python path",
    )
    parser.add_argument(
        "--assets-manifest",
        type=Path,
        help="Verify and persist prepared assets; requires --python when selecting",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Save this runtime for future launcher starts; never stop a live server",
    )
    args = parser.parse_args(argv)
    if args.assets_manifest is not None and args.python is None:
        parser.error("--assets-manifest requires --python")
    assets = (
        asset_binding_from_manifest(args.assets_manifest)
        if args.assets_manifest is not None
        else None
    )
    executable = runtime_python_path(args.python) if args.python else default_runtime_python()
    root = (
        prepare(
            args.wheel,
            args.verification_report,
            executable=executable,
            base_directory=WORKSPACE / "outputs/camera-platform-runtimes",
        )
        if args.wheel
        else args.select
    )
    pin = (
        select(root, args.state_dir, executable, bind_python=args.python is not None, assets=assets)
        if args.activate or args.select
        else None
    )
    print(
        json.dumps(
            {
                "status": "prepared",
                "runtime_root": str(root),
                "activated": pin is not None,
                "state_dir": str(args.state_dir.resolve()),
                "live_server_restarted": False,
                "python": executable,
                "interpreter_persisted": pin is not None and "python" in pin,
                "assets": pin.get("assets") if pin else assets,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

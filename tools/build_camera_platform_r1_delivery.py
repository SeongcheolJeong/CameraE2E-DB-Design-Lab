#!/usr/bin/env python3
"""Bundle one hash-bound R1 engineering RC for offline review and installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checked_file(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink() or not path.is_file() or not path.is_relative_to(ROOT):
        raise ValueError(f"Delivery source must be a regular project file: {path}")
    return path


def _checked_tree(directory: Path) -> dict[str, Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Delivery source tree is missing or a symlink: {directory}")
    return {
        item.relative_to(ROOT).as_posix(): _checked_file(item)
        for item in sorted(directory.rglob("*")) if item.is_file() or item.is_symlink()
    }


def _zip_member(name: str, content: bytes, archive: zipfile.ZipFile) -> None:
    info = zipfile.ZipInfo(name, (2026, 9, 27, 0, 0, 0))
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED if name.endswith(".whl") else zipfile.ZIP_DEFLATED
    archive.writestr(info, content)


def build(release_dir: Path, support: Path, quickstart: Path, output: Path) -> dict:
    release_dir = release_dir.absolute()
    support, quickstart, output = support.absolute(), quickstart.absolute(), output.absolute()
    if not release_dir.is_relative_to(ROOT / "reports/camera_platform"):
        raise ValueError("Release directory must be under reports/camera_platform")
    if output.exists() or output.is_symlink() or output.parent != release_dir / "delivery":
        raise ValueError("Choose a new ZIP in the release's delivery directory")
    manifest_path = _checked_file(release_dir / "product-release-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema") != "camera_platform_r1_product_release_manifest_v1"
            or manifest.get("status") != "rc_ready"
            or manifest.get("approval_status") != "unapproved"
            or manifest.get("physical_validation") != "not_established"
            or manifest.get("blockers")):
        raise ValueError("Delivery requires a complete, unapproved engineering RC manifest")
    wheel = _checked_file(Path(manifest["files"]["wheel"]["path"]))
    evidence = _checked_file(Path(manifest["files"]["install_evidence"]["path"]))
    support = _checked_file(support)
    quickstart = _checked_file(quickstart)
    evidence_link = "../" + evidence.relative_to(ROOT).as_posix()
    if (evidence_link not in support.read_text(encoding="utf-8")
            or evidence_link not in quickstart.read_text(encoding="utf-8")):
        raise ValueError("Release documents must link to the bound installed evidence")
    for label, path in (("wheel", wheel), ("install_evidence", evidence), ("support_doc", support)):
        if manifest["files"][label]["sha256"] != sha(path):
            raise ValueError(f"Manifest {label} no longer matches its bytes")
    if (not wheel.is_relative_to(release_dir / "packages")
            or not evidence.is_relative_to(release_dir)):
        raise ValueError("Wheel and installed evidence must belong to this release directory")
    version_match = re.fullmatch(r"pyisetcam-(0\.16\.1rc\d+)-py3-none-any\.whl", wheel.name)
    if version_match is None:
        raise ValueError("Only a versioned R1 universal wheel can enter this delivery")
    version = version_match.group(1)
    lock = _checked_file(release_dir / "requirements-r1-offline.lock.txt")
    expected = f"pyisetcam[platform]=={version} --hash=sha256:{sha(wheel)}"
    product_lines = [line for line in lock.read_text().splitlines()
                     if line.startswith("pyisetcam[platform]==")]
    if product_lines != [expected]:
        raise ValueError("Offline lock does not bind the exact R1 wheel")
    installed = json.loads(evidence.read_text(encoding="utf-8"))
    if (installed.get("status") != "passed"
            or installed.get("wheel_sha256") != sha(wheel)
            or installed.get("support_doc_sha256") != sha(support)
            or any(check.get("status") != "passed"
                   for check in installed.get("checks", {}).values())
            or len(installed.get("checks", {})) < 14):
        raise ValueError("Installed RC2 proof is incomplete or unbound")
    files: dict[str, Path] = {}
    for directory in (
        release_dir / "packages", release_dir / "wheelhouse",
        evidence.parent, ROOT / "examples/r1",
    ):
        files.update(_checked_tree(directory))
    for path in (
        manifest_path, release_dir / "r1-package-proof.json", lock,
        support, quickstart, ROOT / "LICENSE",
        ROOT / "src/camera_platform/r1_registry_seed.py",
        ROOT / "tools/create_camera_platform_r1_release.py",
        ROOT / "tests/platform/test_r1_registry_seed.py",
    ):
        checked = _checked_file(path)
        files[checked.relative_to(ROOT).as_posix()] = checked
    if len([name for name in files if name.startswith(
            (release_dir / "wheelhouse").relative_to(ROOT).as_posix() + "/"
    ) and name.endswith(".whl")]) != 41:
        raise ValueError("Expected 41 offline dependency wheels")
    prefix = f"CameraE2E-R1-{version}"
    readme = (
        f"# CameraE2E R1 {version}\n\n"
        "Engineering simulation release candidate for macOS arm64 / Python 3.12.\n"
        "Product approval and physical-camera prediction accuracy are not established.\n\n"
        "Start with docs/camera-platform-r1-rc2-support.md and\n"
        "docs/camera-platform-r1-rc2-quickstart.md. The offline wheel, exact\n"
        "dependency lock, 41 dependency wheels, synthetic R1 examples, installed\n"
        "proof and release manifest are included. The proof's absolute state\n"
        "paths identify the validation machine; SHA256SUMS binds the portable bytes.\n"
    ).encode()
    content = {"README.md": readme}
    for relative, path in files.items():
        content[relative] = path.read_bytes()
    sums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(content.items())
    ).encode("utf-8")
    content["SHA256SUMS"] = sums
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    if partial.exists():
        raise ValueError("A previous partial delivery is present")
    try:
        with zipfile.ZipFile(partial, "x", allowZip64=True) as archive:
            for name, data in sorted(content.items()):
                _zip_member(f"{prefix}/{name}", data, archive)
        with zipfile.ZipFile(partial) as archive:
            names = archive.namelist()
            if archive.testzip() is not None or len(names) != len(content):
                raise ValueError("Delivery ZIP did not pass complete member verification")
            for name, data in content.items():
                if archive.read(f"{prefix}/{name}") != data:
                    raise ValueError(f"Delivery member differs: {name}")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {"schema": "camera_platform_r1_delivery_v1", "status": "verified",
            "release_version": version, "zip_path": str(output), "zip_sha256": sha(output),
            "zip_bytes": output.stat().st_size, "file_count": len(content),
            "wheel_sha256": sha(wheel), "manifest_sha256": sha(manifest_path),
            "support_doc_sha256": sha(support), "approval_status": "unapproved",
            "physical_validation": "not_established"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--support-doc", required=True, type=Path)
    parser.add_argument("--quickstart-doc", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = build(args.release_dir, args.support_doc, args.quickstart_doc, args.output)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

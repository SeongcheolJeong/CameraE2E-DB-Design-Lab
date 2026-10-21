#!/usr/bin/env python3
"""Verify exact dev34 wheel bytes and asset-free precomputed-charge RAW."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from zipfile import ZipFile


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    installed = args.installed_root.resolve(strict=True)
    repository = args.repository_root.resolve(strict=True)
    if "PYISETCAM_UPSTREAM_ROOT" in os.environ:
        raise RuntimeError("Installed offline check must not use PYISETCAM_UPSTREAM_ROOT")

    import numpy as np

    import camera_platform
    import pyisetcam
    from camera_platform.sensor_db_bridge import inspect_sensor_revision
    from camera_platform.spatial import SpatialSceneRuntime, request_template
    from pyisetcam.assets import AssetStore

    expected_version = wheel.name.split("-")[1]
    if (
        camera_platform.__version__ != expected_version
        or pyisetcam.__version__ != expected_version
    ):
        raise RuntimeError("The installed package version differs from the wheel")
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(installed):
            raise RuntimeError(f"{module.__name__} leaked from the source checkout")

    installed_count = 0
    source_python_count = 0
    with ZipFile(wheel) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        for item in members:
            if item.filename.endswith(".dist-info/RECORD"):
                continue  # pip re-creates the installed RECORD file.
            content = archive.read(item)
            if (installed / item.filename).read_bytes() != content:
                raise RuntimeError(f"Installed wheel member differs: {item.filename}")
            installed_count += 1
            if item.filename.endswith(".py") and item.filename.startswith(
                ("camera_platform/", "pyisetcam/")
            ):
                if (repository / "src" / item.filename).read_bytes() != content:
                    raise RuntimeError(f"Source Python differs from wheel: {item.filename}")
                source_python_count += 1

    def no_implicit_asset_store(*_args, **_kwargs):
        raise RuntimeError("Unexpected upstream asset fetch in precomputed-charge RAW")

    AssetStore.default = classmethod(no_implicit_asset_store)
    output = SpatialSceneRuntime().execute(request_template())
    raw = np.asarray(output["arrays"]["raw_frames"])
    if raw.shape != (4, 64, 64) or raw.dtype != np.dtype("int32"):
        raise RuntimeError("Installed spatial RAW differs from its expected contract")

    saved = inspect_sensor_revision("DEF-1803-805", root=repository / "camerae2e_db")
    if not saved["executable"]:
        raise RuntimeError("Saved Sensor DB revision lost executable compatibility")

    report = {
        "schema": "camera_dev34_installed_offline_proof_v1",
        "version": expected_version,
        "wheel_sha256": _sha(wheel),
        "installed_exact_member_count": installed_count,
        "source_python_member_count": source_python_count,
        "implicit_upstream_asset_fetch_forbidden": True,
        "spatial_raw_frames_shape": list(raw.shape),
        "spatial_raw_dtype": str(raw.dtype),
        "saved_sensor_revision_executable": True,
        "saved_sensor_revision_report_code": "DEF-1803-805",
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "commercial_release_allowed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

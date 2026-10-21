#!/usr/bin/env python3
"""Recover hash-verified platform-only dependency wheels from a prior pip cache.

The prior environment supplies dependency metadata, not executable code for R1.
Only exact wheel bytes named by its artifact lock are copied. The new R1 wheel
is supplied separately to the independent-environment installer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import zipfile
from collections import deque
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

from pip._vendor.cachecontrol.controller import CacheController
from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def selected_requirements(requires: list[str], extra: str) -> list[Requirement]:
    environment = default_environment() | {"extra": extra}
    return [
        requirement
        for item in requires
        if (requirement := Requirement(item)).marker is None
        or requirement.marker.evaluate(environment)
    ]


def dependency_names(distributions: dict) -> set[str]:
    pending = deque([("pyisetcam", ""), ("pyisetcam", "platform")])
    seen = set()
    names = set()
    while pending:
        name, extra = pending.popleft()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        if name not in distributions:
            raise ValueError(f"Prior environment lacks required distribution {name}")
        names.add(name)
        for requirement in selected_requirements(distributions[name].requires or [], extra):
            child = canonicalize_name(requirement.name)
            if child not in distributions:
                raise ValueError(f"Prior environment lacks required distribution {child}")
            if requirement.specifier and distributions[child].version not in requirement.specifier:
                raise ValueError(f"Prior environment version fails {requirement}")
            pending.append((child, ""))
            pending.extend((child, child_extra) for child_extra in requirement.extras)
    return names - {"pyisetcam"}


def wheel_requires(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("R1 wheel lacks unique metadata")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    return metadata.get_all("Requires-Dist", [])


def active_requirements(requires: list[str]) -> list[str]:
    return sorted(
        str(requirement)
        for extra in ("", "platform")
        for requirement in selected_requirements(requires, extra)
    )


def materialize(previous_venv: Path, lock_path: Path, wheel: Path, cache: Path,
                destination: Path) -> dict:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Wheelhouse destination must be new")
    if not previous_venv.is_dir() or not lock_path.is_file() or not wheel.is_file():
        raise ValueError("Prior environment, lock and R1 wheel are required")
    site = previous_venv / "lib/python3.12/site-packages"
    if not site.is_dir():
        raise ValueError("Expected prior Python 3.12 site-packages is missing")
    distributions = {
        canonicalize_name(item.metadata["Name"]): item
        for item in importlib.metadata.distributions(path=[str(site)])
    }
    if "pyisetcam" not in distributions:
        raise ValueError("Prior environment lacks pyisetcam metadata")
    if active_requirements(distributions["pyisetcam"].requires or []) != active_requirements(
        wheel_requires(wheel)
    ):
        raise ValueError("R1 base/platform dependencies differ from the prior environment")
    lock = json.loads(lock_path.read_text())
    if lock.get("schema") != "camera_platform_dependency_artifacts_v1":
        raise ValueError("Prior artifact lock has the wrong schema")
    entries = {item["name"]: item for item in lock["artifacts"]}
    names = dependency_names(distributions)
    if not names <= entries.keys():
        raise ValueError("Prior lock lacks required platform dependencies")
    sources = []
    total_bytes = 0
    for name in sorted(names):
        entry = entries[name]
        url = entry["url"]
        parsed = urlsplit(url)
        filename = Path(parsed.path).name
        if (parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org"
                or parsed.query or parsed.fragment or not filename.endswith(".whl")
                or filename != os.path.basename(filename)
                or entry["version"] != distributions[name].version):
            raise ValueError(f"Untrusted or mismatched wheel artifact for {name}")
        key = hashlib.sha224(CacheController.cache_url(url).encode()).hexdigest()
        body = cache.joinpath("http-v2", *key[:5], key + ".body")
        metadata = body.with_suffix("")
        if (not body.is_file() or not metadata.is_file()
                or checksum(body) != entry["sha256"]):
            raise ValueError(f"Cache bytes do not match locked wheel for {name}")
        size = body.stat().st_size
        total_bytes += size
        sources.append((name, entry, filename, body, size))
    destination.mkdir(parents=True)
    artifacts = []
    try:
        for name, entry, filename, body, size in sources:
            target = destination / filename
            if target.exists():
                raise ValueError(f"Duplicate wheel filename: {filename}")
            shutil.copyfile(body, target)
            if target.stat().st_size != size or checksum(target) != entry["sha256"]:
                raise ValueError(f"Copied wheel hash differs for {name}")
            artifacts.append({
                "name": name, "version": entry["version"], "filename": filename,
                "bytes": size, "sha256": entry["sha256"], "source_url": entry["url"],
            })
        manifest = {
            "schema": "camera_platform_r1_offline_wheelhouse_v1",
            "scope": "R1 base + platform dependency wheels, excluding the R1 product wheel",
            "previous_environment": str(previous_venv.resolve()),
            "previous_lock_sha256": checksum(lock_path),
            "r1_wheel_sha256": checksum(wheel),
            "artifact_count": len(artifacts), "total_bytes": total_bytes,
            "artifacts": artifacts,
        }
        (destination / "wheelhouse-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        return manifest
    except BaseException:
        # Keep the incomplete directory as diagnostic evidence, never as a valid
        # wheelhouse: the completion manifest is written only after all hashes pass.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-venv", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    report = materialize(args.previous_venv, args.lock, args.wheel, args.cache,
                         args.destination)
    print(json.dumps({"status": "passed", "artifacts": report["artifact_count"],
                      "bytes": report["total_bytes"], "destination": str(args.destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

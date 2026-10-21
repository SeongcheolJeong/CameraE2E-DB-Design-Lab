#!/usr/bin/env python3
"""Download immutable public DeepLens/RayOptics optical examples as data only.

Source code is archived solely as format evidence; it is never imported or run.
Git blob hashes authenticate fetched bytes against the archived pinned tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORIES = {
    "deeplens": {
        "repo": "vccimaging/DeepLens",
        "commit": "2e3bcb24f4b550258ab14f35c0dae5635d0f6d6d",
        "repository_license": "Apache-2.0",
        "metadata": [
            "LICENSE",
            "README.md",
            "datasets/lenses/readme.md",
            "deeplens/geolens_pkg/io.py",
            "deeplens/geolens.py",
            "deeplens/geometric_surface/base_surface.py",
            "deeplens/geometric_surface/spheric.py",
            "deeplens/geometric_surface/aspheric.py",
            "deeplens/geometric_surface/aperture.py",
            "deeplens/material/materials.py",
            "deeplens/material/materials_data.json",
            "deeplens/material/PLASTIC2022.AGF",
            "deeplens/material/MISC.AGF",
            "deeplens/material/CDGM.AGF",
            "deeplens/material/SCHOTT.AGF",
        ],
    },
    "rayoptics_examples": {
        "repo": "mjhoptics/ray-optics",
        "commit": "bcef48ce3a83479b8eb1d18d4c97540513dcb421",
        "repository_license": "BSD-3-Clause",
        "metadata": ["LICENSE", "README.rst"],
    },
}


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def fetch(url: str, destination: Path, *, blob_sha: str | None = None) -> dict:
    sidecar = destination.with_name(destination.name + ".source.json")
    if destination.exists():
        if not sidecar.exists():
            raise ValueError(f"Existing file has no provenance: {destination}")
        metadata = json.loads(sidecar.read_text())
        content = destination.read_bytes()
        if (
            metadata["source_url"] != url
            or hashlib.sha256(content).hexdigest() != metadata["sha256"]
        ):
            raise ValueError(f"Existing source/provenance mismatch: {destination}")
    else:
        for attempt in range(3):
            try:
                with urlopen(
                    Request(url, headers={"User-Agent": "CameraE2E-lens-source-acquisition/1"}),
                    timeout=45,
                ) as response:
                    content = response.read(20_000_001)
                    metadata = {
                        "source_url": url,
                        "resolved_url": response.url,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "content_type": response.headers.get("Content-Type"),
                        "status_code": response.status,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        if len(content) > 20_000_000:
            raise ValueError(f"Source exceeds 20 MB limit: {url}")
    if blob_sha:
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != blob_sha:
            raise ValueError(f"Pinned Git blob mismatch: {url}")
        metadata["git_blob_sha1"] = blob_sha
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        write_json(sidecar, metadata)
    return metadata


def acquire(name: str, config: dict, source_root: Path) -> dict:
    root = source_root / name
    repo, commit = config["repo"], config["commit"]
    tree_url = f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1"
    tree_path = root / "metadata" / "git-tree.json"
    fetch(tree_url, tree_path)
    tree = json.loads(tree_path.read_text())
    if tree.get("truncated") or tree.get("sha") != commit:
        raise ValueError("Incomplete or unpinned repository tree")
    blobs = {x["path"]: x for x in tree["tree"] if x["type"] == "blob"}
    suffixes = {".json", ".zmx"} if name == "deeplens" else {".seq", ".zmx", ".roa"}
    selected = [
        p
        for p in sorted(blobs)
        if Path(p).suffix.lower() in suffixes
        and (
            p.startswith("datasets/lenses/")
            if name == "deeplens"
            else p.startswith("src/rayoptics/")
        )
    ]
    license_info = {
        "identifier": None,
        "status": "repository_license_with_third_party_data_provenance_review_required",
        "repository_license": config["repository_license"],
        "source_url": f"https://github.com/{repo}/blob/{commit}/LICENSE",
        "evidence_path": "metadata/LICENSE",
        "note": (
            "Repository licensing is preserved as evidence, not asserted to override original "
            "optical dataset rights. Models may be transcriptions, conversions, or synthetic/test "
            "designs, not verified manufactured products."
        ),
    }

    def download(item: tuple[str, bool]) -> dict:
        path, is_model = item
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{quote(path, safe='/')}"
        relative = Path("raw" if is_model else "metadata") / path
        metadata = fetch(url, root / relative, blob_sha=blobs[path]["sha"])
        result = dict(
            metadata,
            path=relative.as_posix(),
            repository_path=path,
            repository=f"https://github.com/{repo}",
            commit=commit,
        )
        if is_model:
            suffix = Path(path).suffix.lower()
            result.update(
                {
                    "format": "deeplens_json" if suffix == ".json" else suffix[1:],
                    "title": Path(path).stem,
                    "category": Path(path).parent.name,
                    "license": license_info,
                    "source_notes": (
                        "All indexed model formats retained, including unsupported and intentional "
                        "parser/test variants; file count is not a count of independent usable "
                        "camera designs."
                    ),
                }
            )
            if suffix == ".json":
                try:
                    payload = json.loads((root / relative).read_text())
                    if isinstance(payload, dict):
                        info = payload.get("info")
                        if isinstance(info, str) and info.strip().lower() not in {"", "none"}:
                            result["title"] = info
                except (ValueError, UnicodeError) as exc:
                    result["content_validation"] = {"status": "blocked", "error": str(exc)}
            publication = re.search(
                r"\b(US|EP|JP|KR|WO|CN)[ -]*(\d[\d /.-]*\d)([ABC]\d)?",
                result["title"],
                re.I,
            )
            if publication:
                result["publication_number"] = (
                    publication[1].upper()
                    + re.sub(r"\D", "", publication[2])
                    + (publication[3] or "").upper()
                )
            example = re.search(r"\bExample\s*0*(\d+)", result["title"], re.I)
            if example:
                result["example_label"] = "Example " + str(int(example[1]))
        return result

    jobs = [(p, True) for p in selected] + [(p, False) for p in config["metadata"]]
    successes, failures = [], []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [(job, executor.submit(download, job)) for job in jobs]
        for job, future in futures:
            try:
                successes.append((job[1], future.result()))
            except (OSError, ValueError, KeyError) as exc:
                failures.append({"repository_path": job[0], "model": job[1], "error": str(exc)})
    files = [value for is_model, value in successes if is_model]
    metadata = [value for is_model, value in successes if not is_model]
    manifest = {
        "schema": "camerae2e.public_lens_source_manifest.v1",
        "source": name,
        "repository": f"https://github.com/{repo}",
        "commit": commit,
        "acquisition_completed_at": datetime.now(UTC).isoformat(),
        "selection": (
            "All model files at the pinned repository tree in the specified lens/example paths."
        ),
        "indexed_model_files": len(selected),
        "files": files,
        "metadata_files": metadata,
        "errors": failures,
    }
    write_json(root / "manifest.json", manifest)
    result = {
        "source": name,
        "commit": commit,
        "manifest": str(root / "manifest.json"),
        "indexed": len(selected),
        "downloaded": len(files),
        "formats": dict(Counter(f["format"] for f in files)),
        "errors": failures,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("camerae2e_db/sources"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/lens_source_expansion_v2/opensource_acquisition.json"),
    )
    args = parser.parse_args()
    results = [acquire(name, config, args.source_root) for name, config in REPOSITORIES.items()]
    write_json(args.report, {"sources": results, "files": sum(x["downloaded"] for x in results)})
    if any(x["errors"] for x in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

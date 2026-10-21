"""Portable, deduplicated evidence with a lossless logical manifest.

The graph is a serialization of verified evidence, not a substitute for domain
validation. Legacy export APIs keep their complete logical JSON representation.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_evidence_bundle_v2"
MAX_PHYSICAL_BYTES = 256 * 1024**2
MAX_LOGICAL_BYTES = 1024**3


def graph(value):
    from .evidence_objects import pack

    return pack(value, max_expanded_bytes=MAX_LOGICAL_BYTES)


def graph_size(packed):
    return sum(len(b) for b in packed["objects"].values())


def _inventory(packed, artifacts):
    unique = {}
    for artifact in artifacts:
        checksum, size = artifact["sha256"], artifact["bytes"]
        if (not isinstance(checksum, str) or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
                or type(size) is not int or size < 0):
            raise IntegrityError("Invalid bundle artifact descriptor")
        if checksum in unique and unique[checksum] != size:
            raise IntegrityError("Conflicting bundle artifact byte sizes")
        unique[checksum] = size
    return {
        "schema": SCHEMA,
        "root": packed["root"],
        "objects": [
            {"sha256": sha, "bytes": len(content), "path": f"objects/{sha}.json"}
            for sha, content in sorted(packed["objects"].items())
        ],
        "artifact_blobs": [
            {"sha256": sha, "bytes": size, "path": f"artifacts/{sha}"}
            for sha, size in sorted(unique.items())
        ],
    }


def build_graph_bundle(service, manifest, artifacts):
    packed = graph(manifest)
    envelope = _inventory(packed, artifacts)
    envelope["manifest_sha256"] = digest(envelope)
    encoded = canonical(envelope).encode()
    size = len(encoded) + graph_size(packed) + sum(a["bytes"] for a in envelope["artifact_blobs"])
    if size > MAX_PHYSICAL_BYTES:
        raise ValueError("Deduplicated evidence exceeds the 256 MiB archive limit")
    directory = service.registry.directory / "downloads"
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as stream:
        target = Path(stream.name)
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", encoded)
            archive.writestr(
                "README.txt",
                "Camera Platform lossless evidence graph v2. Each object and artifact "
                "is stored once by SHA-256. Use camera_platform.evidence_package.read_bundle "
                "to verify and reconstruct the complete logical manifest. Pass an externally "
                "pinned expected_manifest_sha256 from the verified export to prove exact "
                "correspondence; an unpinned archive is not independent source authentication. "
                "Original artifact "
                "names remain in that manifest. Physical validation and approval are exactly "
                "those of the logical evidence; export grants no new authority.\n",
            )
            for entry in envelope["objects"]:
                archive.writestr(entry["path"], packed["objects"][entry["sha256"]])
            for entry in envelope["artifact_blobs"]:
                path = service.registry.blob_path(entry["sha256"])
                if path.stat().st_size != entry["bytes"]:
                    raise IntegrityError("Bundle artifact size differs from its descriptor")
                archive.write(path, entry["path"])
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def read_bundle(path, *, expected_manifest_sha256=None):
    """Verify a v2 bundle without extracting files; restore the exact logical JSON."""
    from .evidence_objects import unpack

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or len(names) > 100_002:
            raise IntegrityError("Duplicate or excessive bundle members")
        if sum(i.file_size for i in infos) > MAX_PHYSICAL_BYTES + 4096:
            raise IntegrityError("Expanded evidence archive exceeds its size budget")
        header = archive.getinfo("manifest.json")
        if header.file_size > 16 * 1024**2:
            raise IntegrityError("Evidence graph inventory is too large")
        envelope = json.loads(archive.read(header))
        if (set(envelope) != {"schema", "root", "objects", "artifact_blobs", "manifest_sha256"}
                or envelope["schema"] != SCHEMA
                or digest({k: v for k, v in envelope.items() if k != "manifest_sha256"})
                != envelope["manifest_sha256"]):
            raise IntegrityError("Evidence graph inventory checksum differs")
        if (expected_manifest_sha256 is not None
                and envelope["root"].get("semantic_sha256") != expected_manifest_sha256):
            raise IntegrityError("Evidence differs from the externally pinned logical manifest")
        expected, objects = {"manifest.json", "README.txt"}, {}
        for group, prefix, suffix in (("objects", "objects/", ".json"),
                                     ("artifact_blobs", "artifacts/", "")):
            if not isinstance(envelope[group], list):
                raise IntegrityError("Invalid evidence graph inventory")
            seen = set()
            for item in envelope[group]:
                if not isinstance(item, dict) or set(item) != {"sha256", "bytes", "path"}:
                    raise IntegrityError("Invalid evidence member descriptor")
                sha = item["sha256"]
                if (not isinstance(sha, str) or len(sha) != 64
                        or any(c not in "0123456789abcdef" for c in sha)
                        or sha in seen or type(item["bytes"]) is not int or item["bytes"] < 0
                        or item["path"] != prefix + sha + suffix):
                    raise IntegrityError("Invalid evidence member path or identity")
                seen.add(sha)
                info = archive.getinfo(item["path"])
                if info.file_size != item["bytes"]:
                    raise IntegrityError("Evidence member byte size differs")
                content = archive.read(info)
                if hashlib.sha256(content).hexdigest() != sha:
                    raise IntegrityError("Evidence member SHA differs")
                expected.add(item["path"])
                if group == "objects":
                    objects[sha] = content
        if expected != set(names):
            raise IntegrityError("Evidence archive members differ from exact closure")
        loaded = set()

        def load(sha):
            loaded.add(sha)
            if sha not in objects:
                raise IntegrityError("Missing evidence graph object")
            return objects[sha]

        result = unpack(envelope["root"], load, max_expanded_bytes=MAX_LOGICAL_BYTES)
        if loaded != set(objects):
            raise IntegrityError("Unreachable objects in evidence archive")
        # The inventory must contain exactly the artifact bytes referenced by
        # the reconstructed logical manifest, not just any checksum-valid files.
        from .isp_studies import _artifacts

        expected_inventory = _inventory({"root": {}, "objects": {}}, _artifacts(result))
        if expected_inventory["artifact_blobs"] != envelope["artifact_blobs"]:
            raise IntegrityError("Artifact inventory differs from logical evidence")
        return result

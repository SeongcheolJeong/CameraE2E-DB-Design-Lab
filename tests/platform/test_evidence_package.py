import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import pytest

from camera_platform import evidence_package as packages
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical, digest


@pytest.fixture
def evidence(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    one = registry.store_blob(b"original native raw", "raw.npy", "application/octet-stream")
    alias = {**one, "name": "original.npy"}
    source = {"conditions": {"exposure_ns": 4000000}, "response": list(range(600)),
              "artifacts": [one, alias]}
    manifest = {"schema": "camera_isp_study_evidence_v1",
                "study": {"evidence_format": packages.SCHEMA},
                "sources": [deepcopy(source) for _ in range(12)], "artifacts": [one, alias]}
    return SimpleNamespace(registry=registry), manifest


def rewrite(path, change):
    with zipfile.ZipFile(path) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    change(files)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    stream.seek(0)
    return stream


def test_roundtrip_full_semantics_and_single_blob_with_distinct_names(evidence):
    service, manifest = evidence
    path = packages.build_graph_bundle(service, manifest, manifest["artifacts"])
    assert packages.read_bundle(path, expected_manifest_sha256=digest(manifest)) == manifest
    with zipfile.ZipFile(path) as archive:
        envelope = json.loads(archive.read("manifest.json"))
        assert len(envelope["artifact_blobs"]) == 1
        assert sum(e["bytes"] for e in envelope["objects"]) < len(canonical(manifest)) / 2
    with pytest.raises(IntegrityError, match="externally pinned"):
        packages.read_bundle(path, expected_manifest_sha256="0" * 64)


@pytest.mark.parametrize("kind", ["object", "artifact"])
def test_original_bytes_rechecked_on_every_read(evidence, kind):
    service, manifest = evidence
    path = packages.build_graph_bundle(service, manifest, manifest["artifacts"])
    assert packages.read_bundle(path) == manifest

    def corrupt(files):
        name = next(n for n in files if n.startswith(kind + "s/"))
        files[name] = files[name][:-1] + bytes([files[name][-1] ^ 1])

    with pytest.raises(IntegrityError, match="SHA"):
        packages.read_bundle(rewrite(path, corrupt))


def test_inventory_cannot_omit_a_logically_referenced_artifact(evidence):
    service, manifest = evidence
    path = packages.build_graph_bundle(service, manifest, manifest["artifacts"])

    def omit(files):
        header = json.loads(files["manifest.json"])
        for item in header["artifact_blobs"]:
            files.pop(item["path"])
        header["artifact_blobs"] = []
        header["manifest_sha256"] = digest({k: v for k, v in header.items()
                                            if k != "manifest_sha256"})
        files["manifest.json"] = canonical(header).encode()

    with pytest.raises(IntegrityError, match="logical evidence"):
        packages.read_bundle(rewrite(path, omit))


def test_existing_v1_bundle_keeps_inline_manifest_and_named_artifacts(evidence, monkeypatch):
    from camera_platform import isp_studies

    service, manifest = evidence
    manifest["study"].pop("evidence_format")
    monkeypatch.setattr(isp_studies, "export_manifest", lambda *_: manifest)
    path = isp_studies.export_bundle(service, "preserved-v1-study")
    with zipfile.ZipFile(path) as archive:
        assert archive.read("manifest.json") == canonical(manifest).encode()
        for artifact in manifest["artifacts"]:
            assert archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")


def test_physical_archive_budget_does_not_expand_to_match_logical_size(evidence, monkeypatch):
    service, manifest = evidence
    monkeypatch.setattr(packages, "MAX_PHYSICAL_BYTES", 20)
    with pytest.raises(ValueError, match="256 MiB"):
        packages.build_graph_bundle(service, manifest, manifest["artifacts"])
    assert not list(service.registry.directory.glob("downloads/*.zip"))

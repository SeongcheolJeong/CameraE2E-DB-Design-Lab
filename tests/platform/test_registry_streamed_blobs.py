"""Large result files retain the registry's typed, checksum-checked artifact contract."""

import hashlib
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import registry as registry_module
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, SQLiteRegistry, digest


def _npy_file(path):
    image = np.lib.format.open_memmap(path, mode="w+", dtype=np.uint16, shape=(1024, 1024))
    for row in range(image.shape[0]):
        image[row] = row
    image.flush()
    del image
    return path


def _checksum(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(16 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def test_large_npy_file_is_ingested_and_bound_without_whole_file_reads(
    tmp_path, monkeypatch
):
    registry = SQLiteRegistry(tmp_path / "state")
    source = _npy_file(tmp_path / "raw.npy")
    expected_size = source.stat().st_size
    expected_sha = _checksum(source)

    # Both ingestion and a fresh artifact lookup must avoid Path.read_bytes().
    original_read = Path.read_bytes

    def forbid_whole_file_read(path):
        if path == source or path.parent == registry.blobs:
            pytest.fail("Blob was read into memory in one call")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_whole_file_read)
    descriptor = registry.store_file(source, "raw.npy", "application/x-npy")
    assert descriptor == {
        "id": expected_sha, "name": "raw.npy", "sha256": expected_sha,
        "bytes": expected_size, "media_type": "application/x-npy",
        "url": f"/api/v1/artifacts/{expected_sha}",
    }
    request = {"scene": "test"}
    run = registry.submit(request, digest(request), force=True)
    assert registry.claim()["id"] == run["id"]
    registry.finish(run["id"], result={"artifacts": [descriptor]})
    binding = registry.artifact_binding(
        descriptor["id"], owner_kind="runs", owner_id=run["id"]
    )
    assert binding["descriptor"] == descriptor
    array = np.load(binding["path"], mmap_mode="r", allow_pickle=False)
    assert array.shape == (1024, 1024)
    assert array.dtype == np.dtype("uint16")
    assert (array[0, 0], array[1023, 1023]) == (0, 1023)

    # A duplicate file has the same identity and leaves the original blob intact.
    assert registry.store_file(source, "copy.npy", "application/x-npy")["id"] == expected_sha
    assert _checksum(registry.blob_path(expected_sha)) == expected_sha


def test_registered_streamed_blob_download_and_tamper_detection(tmp_path):
    state = tmp_path / "state"
    registry = SQLiteRegistry(state)
    source = _npy_file(tmp_path / "raw.npy")
    descriptor = registry.store_file(source, "camera-raw.npy", "application/x-npy")
    run = registry.submit({"request": "raw"}, "streamed-raw", force=True)
    registry.claim()
    registry.finish(run["id"], result={"artifacts": [descriptor]})
    client = TestClient(create_app(state_dir=state, start_worker=False, seed_demo=False))
    response = client.get(descriptor["url"])
    assert response.status_code == 200
    assert len(response.content) == descriptor["bytes"]
    assert hashlib.sha256(response.content).hexdigest() == descriptor["sha256"]
    assert "camera-raw.npy" in response.headers["content-disposition"]

    blob = registry.blobs / descriptor["sha256"]
    with blob.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"X")
    with pytest.raises(IntegrityError, match="Artifact checksum mismatch"):
        registry.blob_path(descriptor["sha256"])


def test_file_ingestion_rejects_symlinks_and_changing_source(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path / "state")
    source = tmp_path / "source.bin"
    source.write_bytes(b"0123456789abcdef")
    link = tmp_path / "link.bin"
    link.symlink_to(source)
    with pytest.raises(IntegrityError, match="symlink"):
        registry.store_file(link, "link.bin", "application/octet-stream")

    original = registry._regular_file

    class MutatingReader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def read(self, size):
            content = self.stream.read(size)
            if content and not self.changed:
                source.write_bytes(b"X" + source.read_bytes()[1:])
                self.changed = True
            return content

        def fileno(self):
            return self.stream.fileno()

    @contextmanager
    def mutate_during_copy(path):
        with original(path) as (stream, before):
            yield MutatingReader(stream), before

    monkeypatch.setattr(registry, "_regular_file", mutate_during_copy)
    with pytest.raises(IntegrityError, match="changed during blob ingestion"):
        registry.store_file(source, "source.bin", "application/octet-stream")
    assert list(registry.blobs.iterdir()) == []


def test_existing_corrupt_or_symlink_blob_is_never_reused(tmp_path):
    registry = SQLiteRegistry(tmp_path / "state")
    source = tmp_path / "source.bin"
    source.write_bytes(b"original source bytes")
    descriptor = registry.store_file(source, "source.bin", "application/octet-stream")
    target = registry.blobs / descriptor["sha256"]
    target.write_bytes(b"corrupted target bytes")
    with pytest.raises(IntegrityError, match="Existing blob failed checksum"):
        registry.store_file(source, "again.bin", "application/octet-stream")
    target.unlink()
    target.symlink_to(source)
    with pytest.raises(IntegrityError, match="symlink"):
        registry.store_file(source, "again.bin", "application/octet-stream")
    with pytest.raises(IntegrityError, match="symlink"):
        registry.blob_path(descriptor["sha256"])


def test_hashing_uses_bounded_chunks_for_ingestion_and_reads(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path / "state")
    source = tmp_path / "source.bin"
    source.write_bytes(b"123456789" * 10)
    monkeypatch.setattr(registry_module, "_BLOB_CHUNK_BYTES", 7)
    read_sizes = []
    regular_file = registry._regular_file

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 < size <= 7
            return self.stream.read(size)

        def fileno(self):
            return self.stream.fileno()

    @contextmanager
    def track_reads(path):
        with regular_file(path) as (stream, before):
            yield BoundedReader(stream), before

    monkeypatch.setattr(registry, "_regular_file", track_reads)
    # The classmethod digest opens through the class, so wrap it separately.
    monkeypatch.setattr(SQLiteRegistry, "_regular_file", staticmethod(track_reads))
    descriptor = registry.store_file(source, "source.bin", "application/octet-stream")
    assert registry.blob_path(descriptor["sha256"]).is_file()
    assert read_sizes and max(read_sizes) == 7

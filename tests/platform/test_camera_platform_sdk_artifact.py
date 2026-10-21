"""SDK artifact downloads never accept unbound or changed bytes."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from camera_platform.sdk import PlatformClient


def _client(body: bytes) -> PlatformClient:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/api/v1/artifacts/")
        return httpx.Response(200, content=body)

    client = object.__new__(PlatformClient)
    client.http = httpx.Client(transport=httpx.MockTransport(respond), base_url="http://test")
    return client


def test_download_artifact_verifies_descriptor_and_writes_once(tmp_path):
    content = b"retained-raw-evidence"
    sha = hashlib.sha256(content).hexdigest()
    descriptor = {"sha256": sha, "url": "/api/v1/artifacts/" + sha}
    client = _client(content)
    destination = tmp_path / "evidence.bin"
    try:
        assert client.download_artifact(descriptor, destination) == destination
        assert destination.read_bytes() == content
        with pytest.raises(FileExistsError):
            client.download_artifact(descriptor, destination)
        with pytest.raises(ValueError, match="download limit"):
            client.download_artifact(descriptor, tmp_path / "too-large", max_bytes=4)
        assert not (tmp_path / "too-large").exists()
    finally:
        client.close()


def test_download_artifact_rejects_changed_bytes_and_foreign_url(tmp_path):
    sha = hashlib.sha256(b"expected").hexdigest()
    client = _client(b"changed")
    try:
        with pytest.raises(ValueError, match="SHA-256"):
            client.download_artifact(
                {"sha256": sha, "url": "/api/v1/artifacts/" + sha},
                tmp_path / "changed.bin",
            )
        with pytest.raises(ValueError, match="Artifact URL"):
            client.download_artifact(
                {"sha256": sha, "url": "https://example.com/" + sha},
                tmp_path / "foreign.bin",
            )
        assert not (tmp_path / "changed.bin").exists()
        assert not (tmp_path / "foreign.bin").exists()
    finally:
        client.close()


def test_download_artifact_streams_large_body_and_cleans_failed_staging(tmp_path):
    chunks = [b"a" * (1024 * 1024), b"b" * (1024 * 1024), b"end"]
    payload = b"".join(chunks)
    checksum = hashlib.sha256(payload).hexdigest()

    class Segmented(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks

    client = object.__new__(PlatformClient)
    client.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Segmented())
        ),
        base_url="http://test",
    )
    try:
        descriptor = {
            "sha256": checksum, "url": "/api/v1/artifacts/" + checksum,
            "bytes": len(payload),
        }
        target = tmp_path / "large.bin"
        assert client.download_artifact(
            descriptor, target, max_bytes=len(payload)
        ) == target
        assert hashlib.sha256(target.read_bytes()).hexdigest() == checksum
        assert not list(tmp_path.glob(".*.part"))
        with pytest.raises(ValueError, match="byte count"):
            client.download_artifact(
                {**descriptor, "bytes": len(payload) - 1},
                tmp_path / "mismatch.bin", max_bytes=len(payload),
            )
        assert not (tmp_path / "mismatch.bin").exists()
        assert not list(tmp_path.glob(".*.part"))
    finally:
        client.close()

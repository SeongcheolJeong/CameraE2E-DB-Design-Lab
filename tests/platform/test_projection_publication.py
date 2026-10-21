"""Derived image and source asset must be committed together under retries/failures."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from camera_platform.registry import ConflictError, SQLiteRegistry


def test_concurrent_publication_keeps_one_asset_image_and_audit_pair(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    asset = {"name": "Frozen radiance", "source_kind": "simulated"}
    image = {"name": "New projection", "shape": [8, 8, 5], "artifacts": []}
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(
            lambda _: registry.publish_spectral_image("a" * 64, asset, image), range(12)
        ))
    assert all(result == results[0] for result in results)
    assert len(registry.list("assets")) == len(registry.list("spectral_images")) == 1
    assert registry.get("assets", results[0]["asset_id"]) == results[0]["asset_snapshot"]
    with registry.connection() as conn:
        assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == 2
    assert registry.audit()["integrity"] == "verified"


def test_image_failure_rolls_back_asset_and_both_audit_events(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    with pytest.raises(ValueError):
        registry.publish_spectral_image("a" * 64, {"name": "Asset"}, {"invalid": float("nan")})
    assert registry.list("assets") == registry.list("spectral_images") == []
    with registry.connection() as conn:
        assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["asset", "image"])
def test_same_publication_identity_cannot_replace_originals(tmp_path, changed):
    registry = SQLiteRegistry(tmp_path)
    asset, image = {"name": "Asset"}, {"name": "Image"}
    original = registry.publish_spectral_image("a" * 64, asset, image)
    audit = registry.audit()
    with pytest.raises(ConflictError):
        registry.publish_spectral_image(
            "a" * 64,
            {"name": "Changed"} if changed == "asset" else asset,
            {"name": "Changed"} if changed == "image" else image,
        )
    assert registry.get("spectral_images", original["id"]) == original
    assert registry.audit() == audit

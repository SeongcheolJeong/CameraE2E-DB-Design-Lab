"""Offline execution must be independent of the package install location."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from camera_platform.offline_assets import offline_asset_identity, offline_asset_store
from pyisetcam.assets import DEFAULT_UPSTREAM_SHA, AssetStore
from pyisetcam.exceptions import MissingAssetError

WORKSPACE = Path(__file__).resolve().parents[2]


def test_explicit_workspace_binds_pinned_assets_without_download(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Platform job attempted to download upstream assets")

    monkeypatch.setattr("pyisetcam.assets.ensure_upstream_snapshot", forbidden)
    store = offline_asset_store(workspace=WORKSPACE)
    identity = offline_asset_identity(workspace=WORKSPACE)
    assert store.snapshot_root == (
        WORKSPACE / ".cache/upstream/isetcam" / DEFAULT_UPSTREAM_SHA
    ).resolve()
    assert identity["upstream_revision"] == DEFAULT_UPSTREAM_SHA
    assert len(identity["required_files_sha256"]) == 8
    store.load_color_filters("rgb")


def test_missing_explicit_workspace_fails_closed_without_download(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Platform job attempted to download upstream assets")

    monkeypatch.setattr("pyisetcam.assets.ensure_upstream_snapshot", forbidden)
    with pytest.raises(ValueError, match="Pinned upstream ISETCam assets are unavailable"):
        offline_asset_store(workspace=tmp_path)


def test_nested_default_store_is_scoped_to_the_calling_thread(tmp_path):
    first = AssetStore(snapshot_root=tmp_path / "first")
    second = AssetStore(snapshot_root=tmp_path / "second")

    def observe(store):
        with AssetStore.bound_default(store):
            assert AssetStore.default() is store
            return AssetStore.default().snapshot_root

    with ThreadPoolExecutor(max_workers=2) as executor:
        observed = list(executor.map(observe, (first, second)))
    assert observed == [first.snapshot_root, second.snapshot_root]
    assert AssetStore.default() is not first
    assert AssetStore.default() is not second


def test_offline_store_never_downloads_when_snapshot_disappears(tmp_path, monkeypatch):
    store = offline_asset_store(workspace=WORKSPACE)
    store.snapshot_root = tmp_path / "removed-after-preflight"

    def forbidden(*args, **kwargs):
        raise AssertionError("Platform job attempted to download upstream assets")

    monkeypatch.setattr("pyisetcam.assets.ensure_upstream_snapshot", forbidden)
    with pytest.raises(MissingAssetError, match="disappeared"):
        store.ensure()


def test_offline_store_rejects_cwd_shadow_and_outside_paths(tmp_path, monkeypatch):
    shadow = tmp_path / "data/lights/D65.mat"
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b"untrusted shadow")
    monkeypatch.chdir(tmp_path)
    store = offline_asset_store(workspace=WORKSPACE)
    expected = store.snapshot_root / "data/lights/D65.mat"
    assert store.resolve("data/lights/D65.mat") == expected
    assert store._resolve_spectra_path("data/lights/D65.mat") == expected
    with pytest.raises(MissingAssetError, match="outside"):
        store.resolve(shadow)

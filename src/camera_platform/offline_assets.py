"""Bind platform jobs to a local, pinned ISETCam asset snapshot.

The pyisetcam default store downloads a snapshot when its package-relative
cache is absent.  A platform job must instead use assets already present in
the selected workspace, including when installed from a wheel elsewhere.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pyisetcam.assets import DEFAULT_UPSTREAM_SHA, AssetStore
from pyisetcam.exceptions import MissingAssetError

_REQUIRED = (
    "data/sensor/colorfilters/RGB.mat",
    "data/lights/D65.mat",
    "data/human/XYZEnergy.mat",
    "data/human/xyzQuanta.mat",
    "data/human/XYZ.mat",
    "data/human/luminosity.mat",
    "data/displays/lcdExample.mat",
    "data/surfaces/reflectances/macbethChart.mat",
)


class _OfflineAssetStore(AssetStore):
    def ensure(self) -> Path:
        """Never fetch a snapshot if it disappears after preflight."""
        if not self.snapshot_root.is_dir():
            raise MissingAssetError(
                f"Pinned offline ISETCam snapshot disappeared: {self.snapshot_root}"
            )
        return self.snapshot_root

    def resolve(self, relative_path: str | Path) -> Path:
        """Resolve only beneath the pinned snapshot, never via the current CWD."""
        root = self.ensure().resolve()
        requested = Path(relative_path)
        candidate = (requested if requested.is_absolute() else root / requested).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise MissingAssetError(
                f"Offline asset is outside or absent from snapshot: {requested}"
            )
        return candidate

    def _resolve_spectra_path(self, spectra_name: str | Path) -> Path:
        """Preserve upstream spectral aliases without CWD or symlink fallback."""
        source = Path(spectra_name)
        if source.is_absolute():
            return self.resolve(source)
        candidates = [source]
        if not source.suffix:
            candidates.append(source.with_suffix(".mat"))
        prefixes = (
            "data/surfaces/reflectances",
            "data/lights",
            "data/human",
            "data/sensor/colorfilters",
            "scripts/color",
        )
        if not source.parts or source.parts[0] not in {"data", "scripts"}:
            candidates += [Path(prefix) / item for item in tuple(candidates) for prefix in prefixes]
        for candidate in candidates:
            try:
                return self.resolve(candidate)
            except MissingAssetError:
                pass
        root = self.ensure().resolve()
        for candidate in candidates:
            for match in root.rglob(candidate.name):
                try:
                    return self.resolve(match)
                except MissingAssetError:
                    pass
        raise MissingAssetError(f"Offline spectral asset is absent from snapshot: {spectra_name}")


def _snapshot(workspace: str | Path | None, source: str | Path | None) -> Path:
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(Path(workspace).expanduser().resolve())
    else:
        if source is not None:
            resolved = Path(source).expanduser().resolve()
            candidates.extend((resolved if resolved.is_dir() else resolved.parent).parents)
            candidates.insert(0, resolved if resolved.is_dir() else resolved.parent)
        # This fallback keeps the source checkout usable for library calls
        # without an explicit workspace; an installed wheel must provide one.
        candidates.append(Path(__file__).resolve().parents[2])
    for candidate in dict.fromkeys(candidates):
        snapshot = candidate / ".cache" / "upstream" / "isetcam" / DEFAULT_UPSTREAM_SHA
        if not snapshot.is_dir():
            continue
        resolved_snapshot = snapshot.resolve()
        for relative in _REQUIRED:
            asset = (resolved_snapshot / relative).resolve()
            if not asset.is_relative_to(resolved_snapshot) or not asset.is_file():
                raise ValueError(f"Pinned upstream asset is absent or escapes snapshot: {relative}")
        return resolved_snapshot
    location = str(candidates[0]) if candidates else "an explicit workspace"
    raise ValueError(
        "Pinned upstream ISETCam assets are unavailable under "
        f"{location}/.cache/upstream/isetcam/{DEFAULT_UPSTREAM_SHA}; "
        "install the approved local snapshot before submitting this offline job"
    )


def offline_asset_store(
    *, workspace: str | Path | None = None, source: str | Path | None = None
) -> AssetStore:
    """Return a non-downloading AssetStore for this bounded workflow."""
    return _OfflineAssetStore(snapshot_root=_snapshot(workspace, source))


def offline_asset_identity(
    *, workspace: str | Path | None = None, source: str | Path | None = None
) -> dict[str, Any]:
    """Freeze the upstream revision and files consumed by these comparisons."""
    snapshot = _snapshot(workspace, source)
    return {
        "upstream_revision": DEFAULT_UPSTREAM_SHA,
        "required_files_sha256": {
            relative: hashlib.sha256((snapshot / relative).read_bytes()).hexdigest()
            for relative in _REQUIRED
        },
    }

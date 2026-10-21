"""Explicit project mode for a separate R1 engineering state.

The marker belongs to the state directory, so API and Worker processes enforce
the same mode without relying on the state directory's name or checkout path.
Existing dedicated design-experiments development states remain readable for
their historical v1 jobs; they are not an R1 project.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SCHEMA = "camera_platform_project_mode_v1"
FILENAME = "project-mode.json"
R1_MODE = "r1_engineering"
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_LEGACY_DEV_PREFIX = "camera-platform-design-experiments-"


def _marker_path(state_dir):
    state = Path(state_dir)
    if state.is_symlink():
        raise ValueError("Project state must not be a symbolic link")
    return state / FILENAME


def read(state_dir):
    """Return a validated marker, or None for a legacy unmarked state."""
    path = _marker_path(state_dir)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
        raise ValueError("Project mode marker must be a bounded regular file")

    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("Project mode marker has duplicate fields")
            data[key] = value
        return data

    try:
        data = json.loads(path.read_text(), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Project mode marker is not valid JSON") from error
    if (not isinstance(data, dict)
            or set(data) != {"schema", "mode", "project_id"}
            or data["schema"] != SCHEMA
            or data["mode"] != R1_MODE
            or not isinstance(data["project_id"], str)
            or not _PROJECT_ID.fullmatch(data["project_id"])):
        raise ValueError("Project mode marker has an unsupported contract")
    return data


def configure(state_dir, *, project_id):
    """Create or verify one immutable R1 project marker before API startup."""
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ValueError("R1 project ID requires 1..64 lowercase slug characters")
    path = _marker_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    intended = {"schema": SCHEMA, "mode": R1_MODE, "project_id": project_id}
    existing = read(state_dir)
    if existing is not None:
        if existing != intended:
            raise ValueError("R1 project state is already bound to another mode or ID")
        return intended
    # A primary or historical state must never be relabeled as an R1 project.
    # New projects start from an empty directory; restoring an R1 project also
    # restores its marker, and passes through the idempotent branch above.
    if any(path.parent.iterdir()):
        raise ValueError("R1 project mode requires a new, empty state directory")
    content = (json.dumps(intended, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if read(state_dir) != intended:
            raise ValueError("R1 project state is already bound to another mode or ID") from None
        return intended
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return intended


def require_design_experiments(service):
    """Accept explicit R1 mode; preserve the historical isolated v1 state only."""
    state = service.registry.directory.resolve()
    marker = read(state)
    if marker is not None:
        return marker
    root = Path(service.workspace).resolve()
    if state.parent == root / "outputs" and state.name.startswith(_LEGACY_DEV_PREFIX):
        return {"schema": "legacy_design_experiments_development_state_v1"}
    raise ValueError("New design experiments require an explicit R1 project mode")

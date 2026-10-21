"""A bounded project directory; external-volume free space is not the budget."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import stat
from pathlib import Path

BUDGET_BYTES = 500_000_000_000
RESERVE_BYTES = 5 * 1024**3


def root(service):
    configured = os.environ.get("CAMERA_PLATFORM_WORKSHOP_ROOT")
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        volume = Path("/Volumes/T7")
        if not volume.is_mount():
            raise ValueError(
                "T7 SSD is not mounted; workshop capture requires its project storage."
            )
        path = volume / "CameraE2E"
    path.mkdir(parents=True, exist_ok=True)
    return path


def usage(path):
    """Count the same allocation budget with one stat per file.

    DirEntry retains the directory-entry type and caches stat results during this
    traversal only. No cross-check cache is used: every call walks the current
    project again, and unreadable directories fail rather than undercount.
    """
    path = os.fspath(path)
    try:
        root_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(root_stat.st_mode):
        raise ValueError("Project storage may not contain symbolic links.")
    if not stat.S_ISDIR(root_stat.st_mode):
        return 0
    total = 0
    pending = [path]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError("Project storage may not contain symbolic links.")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                metadata = entry.stat(follow_symlinks=False)
                # Also reject a link replacing an entry after its initial type check.
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("Project storage may not contain symbolic links.")
                if stat.S_ISREG(metadata.st_mode):
                    total += max(metadata.st_size, getattr(metadata, "st_blocks", 0) * 512)
    return total


def status(service):
    path = root(service)
    used = usage(path)
    return {
        "path": str(path),
        "budget_bytes": BUDGET_BYTES,
        "used_bytes": used,
        "remaining_bytes": max(0, BUDGET_BYTES - used),
        "volume_free_bytes": shutil.disk_usage(path).free,
        "minimum_free_bytes": RESERVE_BYTES,
        "policy": "500 GB decimal project budget; "
        "actual allocated or logical size, whichever larger",
    }


@contextlib.contextmanager
def reserve(service, estimated_bytes):
    """Serialize this project's writers, check budget before and during execution.

    The OS lock releases on worker cancellation/crash, with no stale reservation.
    All workshop capture, cache and replay writes occur inside this context.
    """
    path = root(service)
    with (path / ".workshop-writer.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "Another workshop job is writing to this project; retry after it finishes."
            ) from error

        def check():
            used = usage(path)
            free = shutil.disk_usage(path).free
            if used + estimated_bytes > BUDGET_BYTES:
                raise ValueError(
                    "500 GB project budget would be exceeded; resolution was not reduced."
                )
            if free - estimated_bytes < RESERVE_BYTES:
                raise ValueError("Insufficient free SSD space; resolution was not reduced.")
            return used

        check()
        yield path, check

"""Isolated subprocess observation for bounded Lens x Sensor development jobs.

Polling a child's RSS and killing it is useful fault containment, but it is
not an enforced memory limit: allocation can outpace polling. This module
never claims that its supervisor qualifies a 1024 Scene for execution.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class ProcessGuardUnavailableError(RuntimeError):
    """The requested observer cannot start before the child launches."""


def probe_hard_address_space_limit(*, interpreter: str | None = None) -> dict[str, Any]:
    """Check OS enforcement inside a small disposable child process.

    A 256 MiB address-space limit is attempted before allocating 320 MiB. If
    setting the limit fails, the child never allocates. This probes RLIMIT_AS,
    not a general-purpose RSS or GPU-memory guarantee. On Darwin RLIMIT_RSS
    is a source-compatibility alias for RLIMIT_AS; failure at 256 MiB can also
    reflect the child's existing virtual reservations, not a blanket denial
    of every setrlimit call.
    """
    executable = interpreter or sys.executable
    script = (
        "import json,resource,sys\n"
        "limit=256*1024**2\n"
        "try:\n"
        " resource.setrlimit(resource.RLIMIT_AS,(limit,resource.RLIM_INFINITY))\n"
        "except (OSError,ValueError) as exc:\n"
        " print(json.dumps({'status':'limit_set_failed','error_type':type(exc).__name__}))\n"
        " sys.exit(0)\n"
        "try:\n"
        " block=bytearray(320*1024**2)\n"
        " print(json.dumps({'status':'allocation_succeeded','bytes':len(block)}))\n"
        "except MemoryError:\n"
        " print(json.dumps({'status':'allocation_blocked_by_rlimit_as'}))\n"
    )
    try:
        completed = subprocess.run(
            [executable, "-B", "-c", script], check=False, capture_output=True,
            text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "probe_failed", "reason": type(exc).__name__,
            "hard_address_space_limit_verified": False,
            "hard_rss_limit_verified": False,
        }
    import json

    try:
        child = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        child = {"status": "probe_no_valid_child_result"}
    allocation_probe_passed = (
        completed.returncode == 0
        and child.get("status") == "allocation_blocked_by_rlimit_as"
    )
    return {
        "status": child.get("status", "probe_no_valid_child_result"),
        "child_exit_code": completed.returncode,
        "rlimit_as_allocation_probe_passed": allocation_probe_passed,
        # MemoryError alone cannot establish its cause or a hard RSS boundary.
        "hard_address_space_limit_verified": False,
        # RLIMIT_AS applies to virtual address space, not peak resident set;
        # Darwin's RLIMIT_RSS alias does not turn it into a hard RSS quota.
        # Even a working AS limit would need a separate full-job proof.
        "hard_rss_limit_verified": False,
        "suitable_for_large_scene_admission": False,
        **({"error_type": child["error_type"]} if "error_type" in child else {}),
    }


def _observed_child_rss(process: Any) -> int:
    """Best-effort direct-child RSS; macOS sandbox denies global child listing."""
    import psutil

    try:
        return int(process.memory_info().rss)
    except psutil.NoSuchProcess:
        return 0


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_observed_child(
    command: list[str], *, state_root: str | Path, observed_rss_limit_bytes: int,
    timeout_s: float, poll_interval_s: float = 0.02,
) -> dict[str, Any]:
    """Run a small child in disposable state, killing on observed RSS/time.

    Results are never promoted to the caller's durable state. This is an
    observer validation and fault-containment utility only; a child can spike
    between polls, so ``suitable_for_large_scene_admission`` is always false.
    """
    if (
        not isinstance(command, list) or not command or
        any(not isinstance(part, str) or not part for part in command) or
        not Path(command[0]).is_absolute() or
        type(observed_rss_limit_bytes) is not int or observed_rss_limit_bytes <= 0 or
        not 0.001 <= timeout_s <= 3600 or
        not 0.001 <= poll_interval_s <= 0.5
    ):
        raise ValueError("Declare an absolute executable and finite positive guard limits.")
    try:
        import psutil
    except ImportError as exc:
        raise ProcessGuardUnavailableError(
            "psutil observer is unavailable; child not launched."
        ) from exc
    root = Path(state_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("The isolated-state parent directory must already exist.")

    with tempfile.TemporaryDirectory(prefix="camera-rss-observer-", dir=root) as folder:
        isolated = Path(folder)
        child = subprocess.Popen(
            command, cwd=isolated, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process = psutil.Process(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError) as exc:
            _kill_group(child.pid)
            child.wait(timeout=2)
            raise ProcessGuardUnavailableError(
                "Direct child RSS cannot be inspected; child terminated."
            ) from exc
        start = time.monotonic()
        peak = 0
        status = "child_exited"
        reason = "child_exit"
        try:
            while True:
                try:
                    peak = max(peak, _observed_child_rss(process))
                except (psutil.AccessDenied, PermissionError):
                    status, reason = "observer_failed_closed", "process_rss_access_denied"
                    _kill_group(child.pid)
                    break
                if peak >= observed_rss_limit_bytes:
                    status, reason = "observed_rss_limit_exceeded", "polled_rss_reached_limit"
                    _kill_group(child.pid)
                    break
                if time.monotonic() - start >= timeout_s:
                    status, reason = "timeout", "wall_time_reached_limit"
                    _kill_group(child.pid)
                    break
                if child.poll() is not None:
                    break
                time.sleep(poll_interval_s)
        finally:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(child.pid)
                child.wait(timeout=2)
        result = {
            "status": status,
            "reason": reason,
            "child_exit_code": child.returncode,
            "peak_polled_direct_child_rss_bytes": peak,
            "observed_rss_limit_bytes": observed_rss_limit_bytes,
            "timeout_s": timeout_s,
            "poll_interval_s": poll_interval_s,
            "elapsed_s": time.monotonic() - start,
            "isolated_child_state_removed": True,
            "hard_rss_limit_enforced": False,
            "descendant_rss_monitored": False,
            "suitable_for_large_scene_admission": False,
            "result_promoted": False,
        }
    return result

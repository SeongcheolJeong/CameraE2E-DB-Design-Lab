"""A surviving execution child keeps the single-Worker flock after supervisor loss."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from camera_platform import worker
from camera_platform.registry import digest
from camera_platform.service import PlatformService


@pytest.mark.skipif(os.name != "posix", reason="Worker child fencing uses POSIX flock/pass_fds")
def test_execution_child_retains_worker_lock_after_supervisor_unwinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = PlatformService(tmp_path / "state")
    request = {"name": "worker lock inheritance fixture"}
    job = service.registry.submit(request, digest(request))
    marker = tmp_path / "child-ready"
    child_script = tmp_path / "lease_child.py"
    child_script.write_text(
        "import os,pathlib,sys,time\n"
        "os.fstat(int(sys.argv[1]))\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    spawned: list[subprocess.Popen] = []
    original_popen = subprocess.Popen

    class SimulatedSupervisorLossError(RuntimeError):
        pass

    def lose_supervisor_after_spawn(command, **kwargs):
        assert command[-1] == job["id"]
        assert kwargs["start_new_session"] is True
        assert kwargs["close_fds"] is True
        inherited = kwargs.get("pass_fds")
        assert isinstance(inherited, tuple) and len(inherited) == 1
        child = original_popen(
            [sys.executable, str(child_script), str(inherited[0]), str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=inherited,
            start_new_session=True,
        )
        spawned.append(child)
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.01)
        assert marker.read_text() == str(child.pid)
        # Unwind the supervisor's context, releasing its own file descriptor,
        # while leaving the private execution child alive.
        raise SimulatedSupervisorLossError("supervisor disappeared after spawning")

    monkeypatch.setattr(
        worker,
        "subprocess",
        SimpleNamespace(
            Popen=lose_supervisor_after_spawn,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    try:
        with pytest.raises(SimulatedSupervisorLossError):
            worker.work(service, once=True)
        assert len(spawned) == 1 and spawned[0].poll() is None
        with pytest.raises(RuntimeError, match="already running"):
            worker.work(service, once=True)
        with (service.registry.directory / "worker.lock").open("a+") as challenger:
            with pytest.raises(BlockingIOError):
                fcntl.flock(challenger, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for child in spawned:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)
    with (service.registry.directory / "worker.lock").open("a+") as challenger:
        fcntl.flock(challenger, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(challenger, fcntl.LOCK_UN)

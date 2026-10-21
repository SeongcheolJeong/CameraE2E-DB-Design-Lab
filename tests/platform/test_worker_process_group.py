"""Every runner exit must terminate its private descendants and retain job evidence."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from camera_platform import worker
from camera_platform.registry import digest
from camera_platform.service import PlatformService


def _active_process(pid):
    """A transient orphan zombie is terminated, even before the OS reaps its PID."""
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=2
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="Current Worker uses POSIX process groups")
@pytest.mark.parametrize(
    "operation,runner_ignores_term",
    [
        ("cancel", False),
        ("timeout", True),
        ("stop", False),
        ("normal_exit", False),
        ("error_exit", False),
        ("crash_exit", False),
        ("success", False),
    ],
)
def test_worker_terminates_sigterm_ignoring_grandchild_and_preserves_terminal_status(
    tmp_path, monkeypatch, operation, runner_ignores_term
):
    service = PlatformService(tmp_path / "state")
    document = {"request": {"seed": 717}, "purpose": "private process group lifecycle test"}
    job = service.registry.submit(document, digest(document))
    ready = tmp_path / "grandchild-ready.json"
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import json,os,signal,sys,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "data={'pid':os.getpid(),'parent_pid':os.getppid(),'group':os.getpgrp(),"
        "'ignores_term':signal.getsignal(signal.SIGTERM)==signal.SIG_IGN}\n"
        "with open(sys.argv[1]+'.tmp','w') as output: json.dump(data,output)\n"
        "os.replace(sys.argv[1]+'.tmp',sys.argv[1])\n"
        "while True: time.sleep(1)\n"
    )
    runner_script = tmp_path / "runner.py"
    runner_script.write_text(
        "import os,signal,subprocess,sys,time\n"
        "if sys.argv[3]=='ignore': signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "child=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
        "if sys.argv[4] in ('normal_exit','error_exit','crash_exit','success'):\n"
        " while not os.path.exists(sys.argv[2]+'.exit'): time.sleep(.01)\n"
        " if sys.argv[4]=='crash_exit': os.kill(os.getpid(),signal.SIGKILL)\n"
        " os._exit(17 if sys.argv[4]=='error_exit' else 0)\n"
        "child.wait()\n"
    )
    runners, evidence, thread_errors = [], {}, []
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def spawn_test_runner(command, **kwargs):
        assert "--execute" in command
        assert command[-1] == job["id"]
        # Refuse to create the fixture without a private process group: cleanup
        # must never signal the pytest process group or another user's process.
        assert kwargs.get("start_new_session") is True
        process = subprocess.Popen(
            [
                sys.executable,
                str(runner_script),
                str(grandchild_script),
                str(ready),
                "ignore" if runner_ignores_term else "default",
                operation,
            ],
            **kwargs,
        )
        runners.append(process)
        return process

    monkeypatch.setattr(
        worker,
        "subprocess",
        SimpleNamespace(
            Popen=spawn_test_runner,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    def observe_and_cancel():
        try:
            deadline = time.monotonic() + 3
            while not ready.exists():
                if time.monotonic() > deadline:
                    raise AssertionError("Grandchild did not initialize before the test deadline")
                time.sleep(0.01)
            evidence.update(json.loads(ready.read_text()))
            assert evidence["ignores_term"]
            assert evidence["parent_pid"] == evidence["group"] == runners[0].pid
            assert evidence["group"] != os.getpgrp()
            # Prove the descendant really survives SIGTERM before testing the Worker.
            os.kill(evidence["pid"], signal.SIGTERM)
            time.sleep(0.04)
            assert _active_process(evidence["pid"])
            evidence["survived_sigterm"] = True
            if operation == "cancel":
                service.registry.cancel(job["id"])
            elif operation == "stop":
                os.kill(os.getpid(), signal.SIGTERM)
            elif operation in {"normal_exit", "error_exit", "crash_exit", "success"}:
                if operation == "success":
                    service.registry.finish(job["id"], result={"retained": "solver result"})
                # Release only after independently proving this descendant is live
                # and ignores TERM. The runner exits without waiting for it.
                ready.with_name(ready.name + ".exit").write_text("exit permitted\n")
        except BaseException as error:
            thread_errors.append(error)

    observer = threading.Thread(target=observe_and_cancel, name="worker-tree-test-observer")
    try:
        observer.start()
        worker.work(service, once=True, timeout=0.8 if operation == "timeout" else 4)
        observer.join(timeout=3)
        assert not observer.is_alive()
        if thread_errors:
            raise thread_errors[0]
        assert evidence["survived_sigterm"]
        assert len(runners) == 1
        runner = runners[0]
        assert runner.poll() is not None
        expected_code = {
            "cancel": -signal.SIGTERM,
            "timeout": -signal.SIGKILL,
            "stop": -signal.SIGTERM,
            "normal_exit": 0,
            "error_exit": 17,
            "crash_exit": -signal.SIGKILL,
            "success": 0,
        }[operation]
        assert runner.returncode == expected_code
        deadline = time.monotonic() + 2
        while _active_process(evidence["pid"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _active_process(evidence["pid"]), (
            "A solver grandchild survived Worker termination"
        )
        finished = service.registry.get_run(job["id"])
        assert finished["request"] == document["request"]
        if operation == "cancel":
            assert finished["status"] == "cancelled"
        elif operation == "timeout":
            assert finished["status"] == "failed"
            assert "timeout" in finished["error"].lower()
        elif operation == "stop":
            assert finished["status"] == "failed"
            assert finished["error"] == "Worker stopped"
        elif operation == "success":
            assert finished["status"] == "succeeded"
            assert finished["result"] == {"retained": "solver result"}
        else:
            assert finished["status"] == "failed"
            assert finished["error"] == (
                f"Execution process exited without a result (code {expected_code})"
            )
        assert service.registry.audit()["integrity"] == "verified"
    finally:
        # Only process groups created by this fixture are addressed; cleanup also
        # runs when a regression leaves the runner or descendant alive.
        for runner in runners:
            try:
                os.killpg(runner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Fixture cleanup independently confirms already-terminated PIDs.
                if _active_process(runner.pid) or (
                    evidence.get("pid") and _active_process(evidence["pid"])
                ):
                    raise
            runner.wait(timeout=3)
        observer.join(timeout=3)
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize(
    "observation,allowed",
    [
        ("empty", True),
        ("zombie", True),
        ("live", False),
        ("mixed", False),
        ("unreadable", False),
        ("malformed", False),
    ],
)
def test_darwin_permission_error_needs_positive_termination_evidence(
    monkeypatch, observation, allowed
):
    from camera_platform import process_groups

    group = os.getpgrp() + 1000000
    calls = []

    def denied(pgid, signum):
        calls.append((pgid, signum))
        raise PermissionError("intentional group signal denial")

    def observed(*args, **kwargs):
        assert args[0] == ["/bin/ps", "-axo", "pid=,pgid=,stat="]
        assert kwargs["check"] and kwargs["timeout"] == 2
        if observation == "unreadable":
            raise subprocess.TimeoutExpired(args[0], 2)
        own = f"{os.getpid()} {os.getpgrp()} S\n"
        rows = {
            "empty": "",
            "zombie": f"123 {group} Z\n",
            "live": f"123 {group} S\n",
            "mixed": f"123 {group} Z\n124 {group} S\n",
            "malformed": "bad row\n",
        }
        return SimpleNamespace(stdout=own + rows[observation])

    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(process_groups.os, "killpg", denied)
    monkeypatch.setattr(process_groups.subprocess, "run", observed)
    if allowed:
        process_groups.signal_private_group(group, signal.SIGKILL)
    else:
        with pytest.raises(PermissionError, match="intentional"):
            process_groups.signal_private_group(group, signal.SIGKILL)
    assert calls == [(group, signal.SIGKILL)]


@pytest.mark.parametrize("group", [-1, 0, 1, None, True, "123"])
def test_refuse_broadcast_or_invalid_group_without_signalling(monkeypatch, group):
    from camera_platform import process_groups

    def forbidden(*args):
        pytest.fail("Must not signal an invalid or broadcast group")

    monkeypatch.setattr(process_groups.os, "killpg", forbidden)
    with pytest.raises(ValueError, match="separate positive"):
        process_groups.signal_private_group(group, signal.SIGKILL)
    with pytest.raises(ValueError, match="separate positive"):
        process_groups.signal_private_group(os.getpgrp(), signal.SIGKILL)

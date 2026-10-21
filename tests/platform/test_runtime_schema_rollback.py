"""Real SQLite migration reversal and fail-closed preservation of later work."""

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from camera_platform.registry import SQLiteRegistry, canonical, digest, now

PROJECT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
HELPER = (
    Path(__file__).with_name("camera_runtime_rollback.py")
    if Path(__file__).with_name("camera_runtime_rollback.py").exists()
    else PROJECT / "tools/camera_runtime_rollback.py"
)
SPEC = importlib.util.spec_from_file_location("schema_rollback_under_test", HELPER)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def schema_one(state):
    with sqlite3.connect(state / "registry.sqlite") as c:
        c.execute("DROP TABLE artifact_heads")
        c.execute("DROP TABLE artifact_owners")
        c.execute("DROP INDEX audit_subject_action")
        c.execute("UPDATE schema_version SET version=1")


def old_open(state):
    # Exact historical v15 guard, including its harmful autocommit before refusal.
    with sqlite3.connect(state / "registry.sqlite", isolation_level=None) as c:
        c.executescript("INSERT OR IGNORE INTO schema_version VALUES(1);")
        if [r[0] for r in c.execute("SELECT version FROM schema_version")] != [1]:
            raise ValueError("Unsupported registry schema; explicit migration required")


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "installed"
    package = root / "camera_platform"
    package.mkdir(parents=True)
    files = {}
    for name in (
        "__init__.py",
        "registry.py",
        "backup.py",
        "evidence_storage.py",
        "evidence_objects.py",
    ):
        source = PROJECT / "src/camera_platform" / name
        target = package / name
        shutil.copyfile(source, target)
        files["camera_platform/" + name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"files": files}
    (root / "runtime-manifest.json").write_text(json.dumps(manifest))
    return {
        "runtime_root": str(root),
        "manifest_sha256": hashlib.sha256(
            (root / "runtime-manifest.json").read_bytes()
        ).hexdigest(),
    }


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    registry = SQLiteRegistry(path)
    registry.put("assets", {"name": "immutable original"})
    registry.store_blob(b"original bytes", "original.bin", "application/octet-stream")
    schema_one(path)
    return path


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(recovery, "observe_processes", lambda *args, **kwargs: {})


def recover(state, baseline, runtime, tmp_path):
    with recovery.transition_lock(state), recovery.quiescent_state(state, sys.executable) as lease:
        return recovery.recover_schema(
            state, baseline, runtime, sys.executable, dict(os.environ), tmp_path / "recovery", lease
        )


def test_real_schema_reversal_restores_old_constructor_and_exact_history(
    state, runtime, tmp_path, quiet
):
    baseline = recovery.capture_state(state, tmp_path / "proof", "before")
    SQLiteRegistry(state)
    report = recover(state, baseline, runtime, tmp_path)
    assert report["status"] == "derived_schema_reversed"
    old_open(state)
    actual = recovery.capture_state(state, tmp_path / "proof", "after")
    for key in ("schema", "schema_version", "rows", "sqlite_sequence", "blobs", "derived"):
        assert actual[key] == baseline[key]
    assert Path(report["failed_candidate"]["database_evidence"]["path"]).is_file()
    SQLiteRegistry(state)  # Reopening new version safely rebuilds its exact derived schema.
    assert recovery.database_snapshot(state / "registry.sqlite")["schema_version"] == [2]


@pytest.mark.parametrize("write", ["record", "job", "blob", "temporary_blob"])
def test_new_work_preserves_entire_candidate_and_refuses_downgrade(
    state, runtime, tmp_path, quiet, write
):
    baseline = recovery.capture_state(state, tmp_path / "proof", "before")
    registry = SQLiteRegistry(state)
    if write == "record":
        registry.put("assets", {"name": "new candidate work"})
    elif write == "job":
        registry.submit({"authored_test": True}, "new-job")
    elif write == "blob":
        registry.store_blob(b"new candidate data", "new.bin", "application/octet-stream")
    else:
        (state / "blobs/.incomplete.tmp").write_bytes(b"not yet registered")
    before = recovery.capture_state(state, tmp_path / "proof", "candidate")
    with pytest.raises(recovery.RecoveryRefusedError) as error:
        recover(state, baseline, runtime, tmp_path)
    assert error.value.evidence["status"] == "forward_recovery_required"
    after = recovery.capture_state(state, tmp_path / "proof", "refused")
    for key in ("schema", "schema_version", "rows", "sqlite_sequence", "blobs", "derived"):
        assert after[key] == before[key]
    assert after["schema_version"] == [2]


def append_legacy_record(state):
    record = {"id": "asset-pre-stop", "created_at": now(), "name": "arrived before shutdown"}
    checksum = digest(record)
    with sqlite3.connect(state / "registry.sqlite") as c:
        previous = c.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()[0]
        event = {
            "timestamp": now(),
            "action": "assets.created",
            "subject": record["id"],
            "details": {"content_hash": checksum},
            "previous_hash": previous,
        }
        c.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            ("assets", record["id"], canonical(record), checksum, record["created_at"]),
        )
        c.execute(
            "INSERT INTO audit(timestamp,action,subject,details,previous_hash,event_hash) "
            "VALUES(?,?,?,?,?,?)",
            (
                event["timestamp"],
                event["action"],
                event["subject"],
                canonical(event["details"]),
                previous,
                digest(event),
            ),
        )


def test_work_before_candidate_start_still_resumes_legacy_without_overwrite(
    state, runtime, tmp_path, quiet
):
    baseline = recovery.capture_state(state, tmp_path / "proof", "before")
    append_legacy_record(state)
    report = recover(state, baseline, runtime, tmp_path)
    assert report["status"] == "same_schema_code_rollback"
    assert report["database_changed"] is False
    old_open(state)
    after = recovery.database_snapshot(state / "registry.sqlite")
    assert "asset-pre-stop" in after["rows"]["records"]


@pytest.mark.parametrize(
    "fault", ["mixed_version", "missing_trigger", "owner_corruption", "original_blob"]
)
def test_corruption_is_not_hidden_by_reversal(state, runtime, tmp_path, quiet, fault):
    baseline = recovery.capture_state(state, tmp_path / "proof", "before")
    SQLiteRegistry(state)
    with sqlite3.connect(state / "registry.sqlite") as c:
        if fault == "mixed_version":
            c.execute("INSERT INTO schema_version VALUES(1)")
        elif fault == "missing_trigger":
            c.execute("DROP TRIGGER immutable_audit_delete")
        elif fault == "owner_corruption":
            c.execute("INSERT INTO artifact_heads VALUES(?,?)", ("0" * 64, "1" * 64))
        else:
            next((state / "blobs").iterdir()).write_bytes(b"tampered")
    before = recovery.database_snapshot(state / "registry.sqlite")
    with pytest.raises(recovery.RecoveryRefusedError):
        recover(state, baseline, runtime, tmp_path)
    assert recovery.database_snapshot(state / "registry.sqlite") == before


def test_reversal_sql_failure_is_atomic(state, runtime, tmp_path, quiet, monkeypatch):
    baseline = recovery.capture_state(state, tmp_path / "proof", "before")
    SQLiteRegistry(state)
    before = recovery.database_snapshot(state / "registry.sqlite")
    original = recovery._connection_snapshot

    def fail_after_drop(connection):
        value = original(connection)
        if connection.in_transaction and value["schema_version"] == [1]:
            raise RuntimeError("authored failure after DROP before COMMIT")
        return value

    monkeypatch.setattr(recovery, "_connection_snapshot", fail_after_drop)
    with pytest.raises(recovery.RecoveryRefusedError, match="after DROP"):
        recover(state, baseline, runtime, tmp_path)
    monkeypatch.setattr(recovery, "_connection_snapshot", original)
    assert recovery.database_snapshot(state / "registry.sqlite") == before


def test_snapshot_keeps_committed_wal_rows(state, tmp_path):
    connection = sqlite3.connect(state / "registry.sqlite")
    connection.execute("PRAGMA journal_mode=WAL")
    append_legacy_record(state)
    assert (state / "registry.sqlite-wal").stat().st_size > 0
    proof = recovery.capture_state(state, tmp_path / "proof", "wal")
    assert "asset-pre-stop" in proof["rows"]["records"]
    connection.close()


def test_transition_lock_reentrant_and_foreign_pin_write_refused(state, tmp_path):
    marker = tmp_path / "pin-was-overwritten"
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from camera_runtime_rollback import transition_lock;"
        "from pathlib import Path;"
        "\nwith transition_lock(sys.argv[2]):Path(sys.argv[3]).write_text('bad')"
    )
    with recovery.transition_lock(state), recovery.transition_lock(state):
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(HELPER.parent), str(state), str(marker)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0 and "owns this state" in result.stderr
        assert not marker.exists()
    with recovery.transition_lock(state):
        pass


@pytest.mark.parametrize("operation", ["launch", "pin"])
def test_actual_launcher_and_pin_writer_are_serialized(state, operation):
    tools = HELPER.parent
    pin = state / "installed-runtime.json"
    pin.write_text('{"original":true}')
    command = (
        "import sys;sys.path[:0]=sys.argv[1:3];import start_camera_platform as launcher;"
        "launcher.ready=lambda *args: (_ for _ in ()).throw(AssertionError('past lock'));"
        "\nif sys.argv[4]=='launch':launcher.main(['--state-dir',sys.argv[3],'--no-open'])"
        "\nelse:launcher._write_runtime_pin(sys.argv[3],{'unwanted':True})"
    )
    with recovery.transition_lock(state):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                command,
                str(tools),
                str(PROJECT / "tools"),
                str(state),
                operation,
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0 and "owns this state" in result.stderr
    assert pin.read_text() == '{"original":true}'
    assert not (state / "server.pid").exists()


def test_lock_symlink_and_worker_lock_refuse(state, tmp_path, quiet):
    recovery.transition_lock_path(state).symlink_to(tmp_path / "foreign")
    with pytest.raises(recovery.RecoveryRefusedError):
        with recovery.transition_lock(state):
            pytest.fail("symlink lock accepted")
    recovery.transition_lock_path(state).unlink()
    descriptor = recovery._lock_fd(state / "worker.lock")
    try:
        with recovery.transition_lock(state), pytest.raises(recovery.RecoveryRefusedError):
            with recovery.quiescent_state(state, sys.executable):
                pytest.fail("live lock accepted")
    finally:
        os.close(descriptor)


def process(pid, parent, *, role=None, created=1.0, argv=None):
    return {
        "pid": pid,
        "ppid": parent,
        "created": created,
        "argv": argv or ["authored-child"],
        "role": role,
        "status": "sleeping",
    }


@pytest.mark.parametrize("fault", ["pid_reuse", "unknown_writer", "worker_without_api"])
def test_process_identity_faults_never_signal_unknowns(state, runtime, monkeypatch, fault):
    api = process(
        12345,
        1,
        role="installed_api",
        argv=[
            "python",
            "-c",
            "fixture",
            runtime["runtime_root"],
            str(state),
            runtime["manifest_sha256"],
            "serve",
            "--state-dir",
            str(state),
        ],
    )
    observed = {api["pid"]: api}
    if fault == "unknown_writer":
        observed[12346] = process(12346, 1, role="worker")
    if fault == "worker_without_api":
        observed = {12346: process(12346, 1, role="worker")}
    calls = []

    def observe(*args):
        calls.append(True)
        if fault == "pid_reuse" and len(calls) > 1:
            return {12345: {**api, "created": 2.0}}
        return observed

    monkeypatch.setattr(recovery, "observe_processes", observe)
    monkeypatch.setattr(recovery.os, "kill", lambda *args: pytest.fail("unproven signal"))
    with pytest.raises(recovery.RecoveryRefusedError):
        recovery.stop_runtime(state, runtime, sys.executable, expected_pid=12345)


@pytest.mark.parametrize("with_witness_pid", [False, True])
def test_actual_orphan_worker_separate_runner_and_leaf_are_stopped(
    state, runtime, tmp_path, with_witness_pid
):
    """No product API: finite authored subprocess tree with real production argv structure."""
    fake = tmp_path / "authored-worker-package"
    package = fake / "camera_platform"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    leaf = (
        "import os,signal,sys,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "Path(sys.argv[1],'leaf.pid').write_text(str(os.getpid()));time.sleep(30)"
    )
    (package / "worker.py").write_text(
        "import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "state=sys.argv[sys.argv.index('--state-dir')+1]\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "if '--execute' in sys.argv:\n"
        " Path(state,'compute.pid').write_text(str(os.getpid()))\n"
        f" subprocess.Popen([sys.executable,'-c',{leaf!r},state])\n"
        "else:\n"
        " Path(state,'worker.pid').write_text(str(os.getpid()))\n"
        " subprocess.Popen([sys.executable,'-m','camera_platform.worker','--state-dir',state,"
        "'--execute','authored-job'],start_new_session=True)\n"
        "time.sleep(30)\n"
    )
    code = (
        "import subprocess,sys,time;subprocess.Popen([sys.executable,'-m',"
        "'camera_platform.worker','--state-dir',sys.argv[2]]);time.sleep(30)"
    )
    api = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            runtime["runtime_root"],
            str(state),
            runtime["manifest_sha256"],
            "serve",
            "--state-dir",
            str(state),
        ],
        env={**os.environ, "PYTHONPATH": str(fake)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pids = []
    try:
        deadline = time.monotonic() + 4
        while not (state / "leaf.pid").exists():
            assert time.monotonic() < deadline, "fixture tree did not initialize"
            time.sleep(0.02)
        pids = [
            int((state / name).read_text()) for name in ("worker.pid", "compute.pid", "leaf.pid")
        ]
        report = recovery.stop_runtime(
            state,
            runtime,
            sys.executable,
            expected_pid=api.pid if with_witness_pid else None,
            force_after=0.1,
            timeout=6,
        )
        assert {row["pid"] for row in report["captured"]} == {api.pid, *pids}
        assert recovery.observe_processes(state, sys.executable, [api.pid, *pids]) == {}
        api.wait(timeout=3)
    finally:
        # Cleanup only this fixture's exact known argument identities, never a process group.
        import psutil

        for pid in [*pids, api.pid]:
            try:
                process = psutil.Process(pid)
                if str(state) in process.cmdline():
                    process.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        api.wait(timeout=3)


@pytest.mark.parametrize("fresh_addition", ["descendant", "unknown_writer"])
def test_pre_signal_observation_retains_new_identities(state, runtime, monkeypatch, fresh_addition):
    """A just-observed child cannot be forgotten when its parents exit on TERM."""
    api = process(
        12345,
        1,
        role="installed_api",
        argv=[
            "python",
            "-c",
            "fixture",
            runtime["runtime_root"],
            str(state),
            runtime["manifest_sha256"],
            "serve",
            "--state-dir",
            str(state),
        ],
    )
    worker = process(12346, 12345, role="worker")
    new = process(
        12347,
        12346 if fresh_addition == "descendant" else 1,
        role=None if fresh_addition == "descendant" else "worker",
    )
    observations, signals = [], []
    alive = {"parents": True, "new": True}

    def observe(_state, _python, seeds=(), private_groups=()):
        observations.append(set(seeds))
        if len(observations) == 1:
            return {12345: api, 12346: worker}
        if alive["parents"]:
            return {12345: api, 12346: worker, 12347: new}
        # Same filtering as the real observer: an orphan with no platform role
        # remains discoverable only because its previously observed PID is seeded.
        if alive["new"] and 12347 in seeds:
            return {12347: {**new, "ppid": 1}}
        return {}

    def send(pid, signum):
        signals.append((pid, signum))
        if pid == 12345:
            alive["parents"] = False
        elif pid == 12347:
            alive["new"] = False
        else:
            pytest.fail("unexpected identity signalled")

    monkeypatch.setattr(recovery, "observe_processes", observe)
    monkeypatch.setattr(recovery.os, "kill", send)
    if fresh_addition == "unknown_writer":
        with pytest.raises(recovery.RecoveryRefusedError, match="Unknown"):
            recovery.stop_runtime(state, runtime, sys.executable, force_after=-1)
        assert signals == []
    else:
        result = recovery.stop_runtime(state, runtime, sys.executable, force_after=-1)
        assert not alive["new"], "Observed late descendant was lost after parent shutdown"
        assert {row["pid"] for row in result["captured"]} == {12345, 12346, 12347}
        assert 12347 in observations[2]
        assert [pid for pid, _ in signals] == [12345, 12347]


def test_new_unknown_member_of_captured_private_group_refuses_without_group_signal(
    state, runtime, monkeypatch
):
    api = process(
        12345,
        1,
        role="installed_api",
        argv=[
            "python",
            "-c",
            "fixture",
            runtime["runtime_root"],
            str(state),
            runtime["manifest_sha256"],
            "serve",
            "--state-dir",
            str(state),
        ],
    )
    runner = {
        **process(
            12346,
            12345,
            role="worker",
            argv=["python", "-m", "camera_platform.worker", "--execute", "job"],
        ),
        "pgid": 12346,
        "sid": 12346,
    }
    orphan = {**process(12347, 1), "pgid": 12346, "sid": 12346}
    signals, groups_seen = [], []

    def observe(_state, _python, seeds=(), private_groups=()):
        groups_seen.append(list(private_groups))
        if not signals:
            return {12345: api, 12346: runner}
        return {12347: orphan} if any(g["pgid"] == 12346 for g in private_groups) else {}

    monkeypatch.setattr(recovery, "observe_processes", observe)
    monkeypatch.setattr(recovery.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(recovery.RecoveryRefusedError, match="Unknown") as error:
        recovery.stop_runtime(state, runtime, sys.executable, force_after=-1)
    assert signals == [(12345, recovery.signal.SIGTERM)]
    assert error.value.evidence["process_closure"]["stopped"] is False
    assert error.value.evidence["process_closure"]["private_groups"][0]["pgid"] == 12346
    assert groups_seen[-1][0]["pgid"] == 12346


def test_real_late_orphan_private_group_remains_visible_and_prevents_recovery(state, tmp_path):
    """The leader is observed before spawning; its later generic child is orphaned."""
    package = tmp_path / "private-group-fixture" / "camera_platform"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    leaf = (
        "import os,sys,time;from pathlib import Path;"
        "Path(sys.argv[1],'late-leaf.pid').write_text(str(os.getpid()));time.sleep(20)"
    )
    (package / "worker.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "state=Path(sys.argv[sys.argv.index('--state-dir')+1])\n"
        "while not (state/'spawn-late').exists():time.sleep(.01)\n"
        f"child=subprocess.Popen([sys.executable,'-c',{leaf!r},str(state)])\n"
        "while not (state/'late-leaf.pid').exists():time.sleep(.01)\n"
    )
    leader = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "camera_platform.worker",
            "--state-dir",
            str(state),
            "--execute",
            "authored-private-group",
        ],
        env={**os.environ, "PYTHONPATH": str(package.parent)},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    orphan = None
    try:
        rows = recovery.observe_processes(state, sys.executable)
        group = recovery._private_managed_group(rows[leader.pid])
        assert group is not None and group["pgid"] == leader.pid
        (state / "spawn-late").touch()
        leader.wait(timeout=4)
        leaf_pid = int((state / "late-leaf.pid").read_text())
        assert recovery.observe_processes(state, sys.executable) == {}
        members = recovery.observe_processes(state, sys.executable, (), [group])
        orphan = members[leaf_pid]
        assert orphan["role"] is None and orphan["ppid"] != leader.pid
        with (
            recovery.transition_lock(state),
            pytest.raises(recovery.RecoveryRefusedError, match="group member"),
        ):
            with recovery.quiescent_state(state, sys.executable, private_groups=[group]):
                pytest.fail("unobserved private-group orphan was ignored")
    finally:
        import psutil

        if orphan is not None:
            try:
                current = psutil.Process(orphan["pid"])
                if (
                    current.create_time() == orphan["created"]
                    and current.cmdline() == orphan["argv"]
                ):
                    current.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=3)

"""Schema-aware recovery for managed local runtime transitions.

Never construct an old registry to inspect a candidate database. Reversing the
derived artifact index requires an unchanged stopped state and an exact schema
contract; new work is retained for forward recovery instead.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import time
from contextlib import closing, contextmanager
from pathlib import Path

_LOCKS = {}
BASE_TABLES = {"records": "id", "jobs": "id", "audit": "seq"}


class RecoveryRefusedError(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}


def _sha(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def _file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _state_path(state):
    path = Path(state).expanduser().absolute()
    if path.is_symlink() or path.resolve() != path:
        raise RecoveryRefusedError("Runtime state requires a canonical non-symbolic path")
    return path


def _lock_fd(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RecoveryRefusedError("Runtime coordination lock must be a regular file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def transition_lock_path(state):
    # Always use the same external path, even before state exists. Invalid probes
    # retain the existing no-state-created contract, without changing lock identity
    # when another launcher creates the state while this process is waiting.
    state = _state_path(state)
    directory = state.parent / ".camera-platform-runtime-locks"
    if directory.is_symlink():
        raise RecoveryRefusedError("Runtime lock directory cannot be symbolic")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.is_dir() or directory.resolve() != directory:
        raise RecoveryRefusedError("Runtime lock directory requires a canonical directory")
    return directory / (hashlib.sha256(str(state).encode()).hexdigest() + ".lock")


@contextmanager
def transition_lock(state):
    """Serialize normal launches, pin writes and transitions; nested calls reuse FD."""
    state = _state_path(state)
    key = (os.getpid(), str(state))
    if key in _LOCKS:
        yield
        return
    try:
        descriptor = _lock_fd(transition_lock_path(state))
    except (OSError, BlockingIOError) as error:
        raise RecoveryRefusedError(
            "Another managed launch or transition owns this state"
        ) from error
    _LOCKS[key] = descriptor
    try:
        yield
    finally:
        del _LOCKS[key]
        os.close(descriptor)


def _connection_snapshot(connection):
    connection.row_factory = sqlite3.Row
    schema = [
        {
            "type": r["type"],
            "name": r["name"],
            "table": r["tbl_name"],
            "sql": re.sub(r"\s+", " ", r["sql"] or "").strip(),
        }
        for r in connection.execute("SELECT * FROM sqlite_master ORDER BY type,name")
    ]
    rows = {
        table: {
            str(row[key]): _sha(dict(row))
            for row in connection.execute("SELECT * FROM " + table + " ORDER BY " + key)
        }
        for table, key in BASE_TABLES.items()
    }
    derived = {}
    names = {r["name"] for r in schema if r["type"] == "table"}
    for table in ("artifact_owners", "artifact_heads"):
        if table in names:
            entries = sorted(_sha(dict(r)) for r in connection.execute("SELECT * FROM " + table))
            derived[table] = {"count": len(entries), "sha256": _sha(entries)}
    return {
        "schema_version": [
            r[0] for r in connection.execute("SELECT version FROM schema_version ORDER BY version")
        ],
        "schema": schema,
        "rows": rows,
        "sqlite_sequence": [
            list(r)
            for r in connection.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name")
        ],
        "derived": derived,
    }


def database_snapshot(database):
    if database.is_symlink() or not database.is_file():
        raise RecoveryRefusedError("Registry must be a regular existing database")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return _connection_snapshot(connection)


def blob_inventory(state):
    directory = state / "blobs"
    if directory.is_symlink() or not directory.is_dir():
        raise RecoveryRefusedError("Runtime blobs require their regular directory")
    result = {}
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RecoveryRefusedError("Blob inventory contains a link or special entry")
        result[path.name] = {"bytes": info.st_size, "sha256": _file_hash(path)}
    return result


def copy_database(source, destination):
    """SQLite backup includes committed WAL; no live-file copy or sidecar replacement."""
    if destination.exists() or destination.is_symlink():
        raise RecoveryRefusedError("Recovery database evidence must have a fresh path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        with closing(sqlite3.connect(destination)) as writer:

            def bounded(_status, _remaining, total):
                if total * page_size > 4 * 1024**3 or time.monotonic() - started > 30:
                    raise RecoveryRefusedError("Recovery snapshot exceeds its bounded budget")

            reader.backup(writer, pages=256, progress=bounded, sleep=0.01)
            writer.execute("PRAGMA journal_mode=DELETE")
    return {
        "path": str(destination),
        "sha256": _file_hash(destination),
        "bytes": destination.stat().st_size,
    }


def capture_state(state, directory, label):
    state = _state_path(state)
    saved = copy_database(state / "registry.sqlite", Path(directory) / (label + ".sqlite"))
    snapshot = database_snapshot(Path(saved["path"]))
    blobs = blob_inventory(state)
    if database_snapshot(state / "registry.sqlite") != snapshot or blob_inventory(state) != blobs:
        raise RecoveryRefusedError("State changed while recovery evidence was captured")
    return {**snapshot, "blobs": blobs, "database_evidence": saved}


def original_prefix_preserved(before, after):
    return all(
        all(after["rows"][table].get(k) == v for k, v in rows.items())
        for table, rows in before["rows"].items()
    ) and all(after["blobs"].get(k) == v for k, v in before["blobs"].items())


VERIFY_SNAPSHOT_CODE = r"""
import sys,json,hashlib,inspect
from pathlib import Path
root,database,blobs=map(Path,sys.argv[1:4])
if hashlib.sha256((root/'runtime-manifest.json').read_bytes()).hexdigest()!=sys.argv[4]:
    raise RuntimeError('Recovery verifier package manifest changed')
manifest=json.loads((root/'runtime-manifest.json').read_text())
for name in ('camera_platform/registry.py','camera_platform/backup.py'):
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=manifest['files'][name]:
        raise RuntimeError('Recovery verifier package bytes changed')
sys.path.insert(0,str(root))
from camera_platform.backup import _verify_snapshot
options={'blobs':blobs} if 'blobs' in inspect.signature(_verify_snapshot).parameters else {}
report,references=_verify_snapshot(database,**options)
print(json.dumps({'registry':report,'reference_count':len(references)}))
"""


def verify_saved_database(python, binding, state, snapshot, environment):
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            VERIFY_SNAPSHOT_CODE,
            str(binding["runtime_root"]),
            snapshot["database_evidence"]["path"],
            str(state / "blobs"),
            binding["manifest_sha256"],
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        env=environment,
    )
    return json.loads(result.stdout)


PROCESS_OBSERVATION_CODE = r"""
import json,os,sys,psutil
state=sys.argv[1]; seeds=set(json.loads(sys.argv[2])); rows={}
groups={tuple(g) for g in json.loads(sys.argv[3])}
def role(args):
    for i,arg in enumerate(args[:-1]):
        if arg=='-m' and args[i+1] in ('camera_platform.worker','camera_platform'):
            if '--state-dir' in args and args[args.index('--state-dir')+1]==state:
                return 'worker' if args[i+1].endswith('.worker') else 'direct_api'
        if arg=='-c' and len(args)>i+4 and args[i+3]==state:
            if 'serve' in args[i+5:] and '--state-dir' in args:
                return 'installed_api'
    return None
for process in psutil.process_iter():
    try:
        if process.uids().real!=os.getuid():continue
        with process.oneshot():
            args=process.cmdline(); status=process.status()
            rows[process.pid]={'pid':process.pid,'ppid':process.ppid(),
                'created':process.create_time(),'argv':args,'status':status,
                'pgid':os.getpgid(process.pid),'sid':os.getsid(process.pid),
                'role':role(args)}
    except (psutil.NoSuchProcess,psutil.ZombieProcess,ProcessLookupError):continue
    except psutil.AccessDenied as error:
        raise RuntimeError('Cannot observe process ownership') from error
if os.getpid() not in rows:raise RuntimeError('Process observer did not observe itself')
selected=seeds|{p for p,r in rows.items() if r['role'] or (r['pgid'],r['sid']) in groups}
while True:
    expanded=selected|{p for p,r in rows.items() if r['ppid'] in selected}
    if expanded==selected:break
    selected=expanded
print(json.dumps([rows[p] for p in sorted(selected) if p in rows and p!=os.getpid()]))
"""


def observe_processes(state, python, seeds=(), private_groups=()):
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            PROCESS_OBSERVATION_CODE,
            str(state),
            json.dumps(list(seeds)),
            json.dumps([[g["pgid"], g["sid"]] for g in private_groups]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RecoveryRefusedError("Invalid process observation")
    return {row["pid"]: row for row in rows if row["status"] != "zombie"}


def _same_process(first, second):
    return all(first.get(k) == second.get(k) for k in ("pid", "created", "argv"))


def _managed_api(row, root, state, manifest_hash):
    args = row["argv"]
    if row["role"] != "installed_api" or "-c" not in args:
        return False
    index = args.index("-c")
    return args[index + 2 : index + 5] == [str(root), str(state), manifest_hash]


def _private_managed_group(row):
    # Only already proven managed session leaders establish a private group.
    # Group membership is refusal evidence, never authority for a group signal.
    if row.get("pgid") != row["pid"] or row.get("sid") != row["pid"]:
        return None
    if row.get("role") != "installed_api" and not (
        row.get("role") == "worker" and "--execute" in row["argv"]
    ):
        return None
    return {"pgid": row["pgid"], "sid": row["sid"], "leader": row}


def stop_runtime(state, binding, python, *, expected_pid=None, timeout=20, force_after=8):
    """Stop only proven managed identities; unknown same-state writers refuse recovery."""
    state = _state_path(state)
    rows = observe_processes(state, python)
    apis = [
        r
        for r in rows.values()
        if _managed_api(r, binding["runtime_root"], state, binding["manifest_sha256"])
    ]
    if expected_pid is not None:
        apis = [r for r in apis if r["pid"] == expected_pid]
    if not apis:
        if rows:
            raise RecoveryRefusedError(
                "Unidentified state processes remain; no process was signalled"
            )
        return {"captured": [], "private_groups": [], "stopped": True}
    if len(apis) != 1:
        raise RecoveryRefusedError("Multiple managed APIs prevent exclusive transition")
    api = apis[0]
    known = {api["pid"]: api}
    groups = {}

    def admit(observed):
        while True:
            addition = {
                pid: row
                for pid, row in observed.items()
                if pid not in known and row["ppid"] in known
            }
            if not addition:
                break
            known.update(addition)
        for pid, row in observed.items():
            if pid not in known or not _same_process(known[pid], row):
                raise RecoveryRefusedError(
                    "Unknown or reused state-process identity; recovery refused"
                )
        for row in known.values():
            group = _private_managed_group(row)
            if group is not None:
                groups[(group["pgid"], group["sid"])] = group

    def send(row, signum):
        fresh = observe_processes(state, python, known, groups.values())
        # Retain children first: TERM can orphan a just-observed descendant,
        # making parent-only discovery impossible on the next observation.
        admit(fresh)
        if row["pid"] not in fresh:
            return
        if not _same_process(row, fresh[row["pid"]]):
            raise RecoveryRefusedError("Process identity changed before signal")
        try:
            os.kill(row["pid"], signum)
        except ProcessLookupError:
            pass  # It exited after exact identity observation; no replacement is signalled.

    try:
        admit(rows)
        send(api, signal.SIGTERM)
        started = time.monotonic()
        while True:
            live = observe_processes(state, python, known, groups.values())
            admit(live)
            if not live:
                break
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                raise RecoveryRefusedError(
                    "Managed processes did not stop before recovery deadline"
                )
            if elapsed > force_after:
                for row in live.values():
                    send(row, signal.SIGKILL)
            time.sleep(0.1)
    except Exception as error:
        raise RecoveryRefusedError(
            str(error),
            {
                "process_closure": {
                    "captured": list(known.values()),
                    "private_groups": list(groups.values()),
                    "stopped": False,
                }
            },
        ) from error
    return {
        "captured": list(known.values()),
        "private_groups": list(groups.values()),
        "stopped": True,
    }


@contextmanager
def quiescent_state(state, python, *, private_groups=()):
    """Supporting worker lock plus managed-process absence, held through recovery."""
    state = _state_path(state)
    if (os.getpid(), str(state)) not in _LOCKS:
        raise RecoveryRefusedError("Recovery requires the managed transition lock")
    if observe_processes(state, python, (), private_groups):
        raise RecoveryRefusedError("A state writer or private managed group member remains alive")
    try:
        descriptor = _lock_fd(state / "worker.lock")
    except OSError as error:
        raise RecoveryRefusedError("Worker lock is still held or untrusted") from error

    class Lease:
        active = True

        def check(self):
            if not self.active or observe_processes(state, python, (), private_groups):
                raise RecoveryRefusedError("State is no longer quiescent")

    lease = Lease()
    try:
        lease.check()
        yield lease
    finally:
        lease.active = False
        os.close(descriptor)


def recover_schema(state, baseline, binding, python, environment, directory, lease):
    """Authorize code rollback, or reverse only the exact no-new-work derived schema."""
    state = _state_path(state)
    lease.check()
    current = capture_state(state, directory, "failed-candidate")
    evidence = {
        "before": baseline,
        "failed_candidate": current,
        "status": "forward_recovery_required",
    }
    try:
        evidence["candidate_integrity"] = verify_saved_database(
            python, binding, state, current, environment
        )
        if not original_prefix_preserved(baseline, current):
            raise RecoveryRefusedError("Original history or blobs changed")
        if (
            current["schema_version"] == baseline["schema_version"]
            and current["schema"] == baseline["schema"]
        ):
            lease.check()
            return {**evidence, "status": "same_schema_code_rollback", "database_changed": False}
        if baseline["schema_version"] != [1] or current["schema_version"] != [2]:
            raise RecoveryRefusedError("Unsupported schema transition")
        if (
            current["rows"] != baseline["rows"]
            or current["sqlite_sequence"] != baseline["sqlite_sequence"]
            or current["blobs"] != baseline["blobs"]
        ):
            raise RecoveryRefusedError("Candidate work exists; preserve it for forward recovery")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", name) or value["sha256"] != name
            for name, value in current["blobs"].items()
        ):
            raise RecoveryRefusedError("Unexpected incomplete blob prevents schema reversal")
        with closing(
            sqlite3.connect(state / "registry.sqlite", isolation_level=None)
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                locked = _connection_snapshot(connection)
                if any(locked[k] != current[k] for k in locked):
                    raise RecoveryRefusedError(
                        "Registry changed before recovery acquired its writer lock"
                    )
                if blob_inventory(state) != current["blobs"]:
                    raise RecoveryRefusedError("Blob inventory changed before recovery")
                lease.check()
                connection.execute("DROP TABLE artifact_heads")
                connection.execute("DROP TABLE artifact_owners")
                connection.execute("DROP INDEX audit_subject_action")
                connection.execute("UPDATE schema_version SET version=1 WHERE version=2")
                restored = _connection_snapshot(connection)
                if any(restored[k] != baseline[k] for k in restored):
                    raise RecoveryRefusedError("Reversed schema differs from the original database")
                if [r[0] for r in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                    raise RecoveryRefusedError("Reversed database failed SQLite integrity")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RecoveryRefusedError("Reversed database failed foreign-key integrity")
                lease.check()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        evidence["after_reversal"] = capture_state(state, directory, "reversed-schema")
        return {**evidence, "status": "derived_schema_reversed", "database_changed": True}
    except Exception as error:
        evidence["refusal"] = str(error)
        raise RecoveryRefusedError(str(error), evidence) from error

"""Single-host durable worker. Heavy computation runs in a separate child process."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .process_groups import signal_private_group
from .service import PlatformService

DATABASE_BUSY_TIMEOUT_MS = 250
DATABASE_RETRY_TIMEOUT_S = 30.0
DATABASE_SHUTDOWN_TIMEOUT_S = 3.0
DATABASE_RETRY_INITIAL_S = 0.05
DATABASE_RETRY_MAX_S = 0.5
DEFAULT_JOB_TIMEOUT_S = 120
PRESCRIPTION_PSF_JOB_TIMEOUT_S = 210
OUT_OF_CORE_DIAGNOSTIC_NAME = "out_of_core_recovery_diagnostic.json"
DISK_RAW_JOB_KINDS = frozenset(
    {
        "lens_sensor_out_of_core_raw",
        "lens_sensor_registered_roi_disk_raw",
    }
)
_SCRATCH_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z")
_SCRATCH_SCAN_LIMIT = 1024
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class WorkerDatabaseBusyTimeoutError(RuntimeError):
    """A bounded publication/queue wait expired; never reported as empty or success."""


class _WorkerStoppingError(Exception):
    pass


@contextmanager
def _short_database_waits(registry):
    """Worker-local connections remain responsive without changing the API process."""
    previous = registry.busy_timeout_ms
    registry.busy_timeout_ms = DATABASE_BUSY_TIMEOUT_MS
    try:
        yield
    finally:
        registry.busy_timeout_ms = previous


def _retry_database_busy(operation, *, label, stop_event=None, timeout=None):
    """Retry only SQLITE_BUSY families, using a fresh registry transaction each time.

    Call only for idempotent operations or a claim whose committed return has no
    fallible database read. The deadline bounds retries, plus at most one short
    SQLite call; it does not interrupt successful commits. A computation is
    never an operation passed here. SQLITE_LOCKED and other errors remain errors.
    """
    timeout = DATABASE_RETRY_TIMEOUT_S if timeout is None else timeout
    deadline = time.monotonic() + timeout
    delay = DATABASE_RETRY_INITIAL_S
    attempts = 0
    last_error = None

    def expired(error):
        return WorkerDatabaseBusyTimeoutError(
            f"SQLite BUSY deadline exceeded while {label} after {attempts} attempts "
            f"({timeout:g} s retry budget): {error}"
        )

    while True:
        if stop_event is not None and stop_event.is_set():
            raise _WorkerStoppingError
        if last_error is not None and time.monotonic() >= deadline:
            raise expired(last_error) from last_error
        try:
            attempts += 1
            return operation()
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if type(code) is not int or code & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            last_error = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise expired(error) from error
            if attempts == 1:
                print(
                    f"SQLite BUSY while {label}; waiting within {timeout:g} s budget",
                    file=sys.stderr,
                    flush=True,
                )
            interval = min(delay, remaining)
            if stop_event is None:
                time.sleep(interval)
            elif stop_event.wait(interval):
                raise _WorkerStoppingError from error
            delay = min(delay * 2, DATABASE_RETRY_MAX_S)


def execute(service, identity):
    with _short_database_waits(service.registry):
        job = _retry_database_busy(
            lambda: service.registry.get_run(identity), label=f"reading claimed run {identity}"
        )
        if job["status"] != "running":
            return
        error = None
        try:
            if job.get("job_kind") == "isp_study_cell":
                from .isp_studies import execute as execute_study_cell

                result = execute_study_cell(service, job)
            else:
                result = service.execute_run(job)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = getattr(exc, "evidence_result", None)
        # The exact completed result/error remains in this process during the
        # retry. A publication conflict must never invoke physics a second time.
        _retry_database_busy(
            lambda: service.registry.finish(identity, result=result, error=error),
            label=f"publishing run {identity}",
        )


def _record_out_of_core_recovery_diagnostic(service, *, stop_event=None):
    """Observe abandoned scratch without inferring that an old child has exited.

    A terminal Registry row alone is insufficient evidence for deletion: the
    previous supervisor may have died while its private-session child survived.
    The current supervisor only deletes a run root after it reaps that child.
    """
    parent = service.registry.directory / "out_of_core_work"
    report = {
        "schema": "camera_platform_out_of_core_recovery_diagnostic_v1",
        "observed_at_unix_s": time.time(),
        "scratch_parent": "out_of_core_work",
        "parent_state": "absent",
        "safe_to_delete": False,
        "entries": [],
        "truncated": False,
    }
    try:
        parent_mode = parent.lstat().st_mode
    except FileNotFoundError:
        parent_mode = None
    if parent_mode is not None:
        if stat.S_ISLNK(parent_mode):
            report["parent_state"] = "symlink"
        elif not stat.S_ISDIR(parent_mode):
            report["parent_state"] = "not_directory"
        else:
            # Bind enumeration to the directory itself. A concurrent swap to a
            # symlink cannot redirect the scan outside the private state root.
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                directory_fd = os.open(parent, flags)
            except OSError:
                report["parent_state"] = "changed_or_unreadable"
            else:
                report["parent_state"] = "directory"
                try:
                    with os.scandir(directory_fd) as entries:
                        for index, entry in enumerate(entries):
                            if index >= _SCRATCH_SCAN_LIMIT:
                                report["truncated"] = True
                                break
                            try:
                                mode = entry.stat(follow_symlinks=False).st_mode
                            except FileNotFoundError:
                                kind = "vanished"
                            else:
                                kind = (
                                    "symlink"
                                    if stat.S_ISLNK(mode)
                                    else "directory"
                                    if stat.S_ISDIR(mode)
                                    else "file"
                                    if stat.S_ISREG(mode)
                                    else "other"
                                )
                            item = {
                                "name": entry.name,
                                "entry_type": kind,
                                "registry_state": "not_queried",
                                "job_kind": None,
                                "status": None,
                                "terminal": False,
                                "safe_to_delete": False,
                                "reason": (
                                    "Scratch entry is a symlink; never follow it."
                                    if kind == "symlink"
                                    else "Scratch entry has no verified disk-RAW run ownership."
                                ),
                            }
                            if kind != "vanished" and _SCRATCH_RUN_ID.fullmatch(entry.name):
                                try:
                                    job = _retry_database_busy(
                                        lambda identity=entry.name: service.registry.get_run(
                                            identity
                                        ),
                                        label=f"inspecting disk-RAW scratch owner {entry.name}",
                                        stop_event=stop_event,
                                    )
                                except KeyError:
                                    item["registry_state"] = "unknown"
                                else:
                                    item["registry_state"] = "known"
                                    item["job_kind"] = job.get("job_kind")
                                    item["status"] = job["status"]
                                    item["terminal"] = job["status"] in _TERMINAL_RUN_STATUSES
                                    if kind == "symlink":
                                        item["reason"] = (
                                            "Scratch entry is a symlink; never follow it."
                                        )
                                    elif kind != "directory":
                                        item["reason"] = "Scratch entry is not a real directory."
                                    elif job.get("job_kind") not in DISK_RAW_JOB_KINDS:
                                        item["reason"] = "Registry owner is not a disk-RAW job."
                                    elif not item["terminal"]:
                                        item["reason"] = "Disk-RAW job is not terminal."
                                    else:
                                        item["reason"] = (
                                            "Terminal status does not prove the old child "
                                            "process exited."
                                        )
                            report["entries"].append(item)
                finally:
                    os.close(directory_fd)

    # A private, atomic diagnostic survives a Worker restart. The report is
    # deliberately advisory and never grants deletion authority.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".out_of_core_recovery_",
            suffix=".tmp",
            dir=service.registry.directory,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, service.registry.directory / OUT_OF_CORE_DIAGNOSTIC_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


def work(service, *, once=False, timeout=None):
    lock_path = service.registry.directory / "worker.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A worker is already running for this state directory") from None
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        stopping = threading.Event()

        def stop(signum, frame):
            stopping.set()

        previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            with _short_database_waits(service.registry):
                _retry_database_busy(
                    service.registry.recover_interrupted,
                    label="recovering interrupted runs",
                    stop_event=stopping,
                )
                _record_out_of_core_recovery_diagnostic(service, stop_event=stopping)
                # Keep this same flock open in the private execution child. If
                # the supervisor dies unexpectedly, a surviving child still
                # fences a replacement Worker from claiming another job.
                _work_loop(
                    service, stopping, once=once, timeout=timeout, worker_lock_fd=lock.fileno()
                )
        except _WorkerStoppingError:
            return
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def _work_loop(service, stopping, *, once, timeout, worker_lock_fd):
    while not stopping.is_set():
        job = _retry_database_busy(
            service.registry.claim,
            label="claiming queued work",
            stop_event=stopping,
        )
        if job is None:
            if once:
                return
            stopping.wait(0.25)
            continue
        if stopping.is_set():
            _retry_database_busy(
                lambda identity=job["id"]: service.registry.finish(
                    identity, error="Worker stopped"
                ),
                label=f"recording shutdown of claimed run {job['id']}",
                timeout=DATABASE_SHUTDOWN_TIMEOUT_S,
            )
            return
        command = [
            sys.executable,
            "-m",
            "camera_platform.worker",
            "--state-dir",
            str(service.registry.directory),
            "--workspace",
            str(service.workspace),
            "--execute",
            job["id"],
        ]
        with (service.registry.directory / "worker.log").open("ab") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                pass_fds=(worker_lock_fd,),
                start_new_session=True,
            )
            job_timeout = timeout
            if job_timeout is None and job.get("job_kind") == "lens_sensor_registered_roi_disk_raw":
                from .lens_sensor_registered_roi_disk_raw_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "lens_sensor_frequency_target":
                from .lens_sensor_frequency_target_job import (
                    EXTENDED_SCHEMA,
                    EXTENDED_WORKER_TIMEOUT_S,
                    WORKER_TIMEOUT_S,
                )

                job_timeout = (
                    EXTENDED_WORKER_TIMEOUT_S
                    if (job.get("specification") or {}).get("schema") == EXTENDED_SCHEMA
                    else WORKER_TIMEOUT_S
                )
            elif job_timeout is None and job.get("job_kind") == "lens_geometric_pupil_density":
                from .lens_geometric_pupil_density_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "sensor_structure_study":
                from .sensor_structure_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "lens_psf_lut_camera":
                from .lens_psf_lut_camera import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "lens_optical_psf_lut":
                from .lens_psf_lut_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (
                job.get("job_kind") == "lens_dense_geometric_psf_candidate"
            ):
                from .lens_dense_geometric_psf_candidate_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (
                job.get("job_kind") == "lens_dense_geometric_psf_candidate_v2"
            ):
                from .lens_dense_geometric_psf_candidate_v2_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (
                job.get("job_kind") == "lens_dense_geometric_psf_q4_rebin"
            ):
                from .lens_dense_geometric_psf_q4_rebin_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (
                job.get("job_kind") == "lens_sensor_dense_psf_sensitivity"
            ):
                from .lens_sensor_dense_psf_sensitivity_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (job.get("job_kind") == "lens_sensor_q4_psf_sensitivity"):
                from .lens_sensor_q4_psf_sensitivity_job import (
                    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
                    EXTENDED_WORKER_TIMEOUT_S,
                    WORKER_TIMEOUT_S,
                )

                goal = (job.get("specification") or {}).get("fixed_grid_goal") or {}
                job_timeout = (
                    EXTENDED_WORKER_TIMEOUT_S
                    if goal.get("coordinate_policy") == EXTENDED_SPARSE_ROI_COORDINATE_POLICY
                    else WORKER_TIMEOUT_S
                )
            elif job_timeout is None and (
                job.get("job_kind") == "lens_sensor_joint_scene_dense_psf_ladder"
            ):
                from .lens_sensor_joint_sampling_ladder_job import (
                    EXTENDED_SPARSE_ROI_COORDINATE_POLICY,
                    EXTENDED_WORKER_TIMEOUT_S,
                    WORKER_TIMEOUT_S,
                )

                goal = (job.get("specification") or {}).get("joint_goal") or {}
                job_timeout = (
                    EXTENDED_WORKER_TIMEOUT_S
                    if goal.get("coordinate_policy") == EXTENDED_SPARSE_ROI_COORDINATE_POLICY
                    else WORKER_TIMEOUT_S
                )
            elif job_timeout is None and job.get("job_kind") == "road_hdr_study":
                from .road_hdr_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "design_workshop":
                from .design_workshop_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "design_asset_update":
                job_timeout = 300
            elif job_timeout is None and job.get("job_kind") == "reference_workshop":
                from .reference_workshop_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "calibration_unified_workflow":
                from .calibration_unified import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") in {
                "calibration_lab",
                "calibration_lab_validation",
                "calibration_db_replay",
            }:
                from .calibration_lab import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "virtual_lab":
                from .virtual_lab_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "road_design_study":
                from .road_design_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "pedestrian_braking_study":
                from .pedestrian_braking_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and job.get("job_kind") == "lens_sensor_out_of_core_raw":
                from .lens_sensor_out_of_core_raw_job import WORKER_TIMEOUT_S

                job_timeout = WORKER_TIMEOUT_S
            elif job_timeout is None and (
                job.get("job_kind") == "characterization"
                and job.get("plugin_id") == "prescription_psf_v1"
            ):
                job_timeout = PRESCRIPTION_PSF_JOB_TIMEOUT_S
            elif job_timeout is None:
                job_timeout = DEFAULT_JOB_TIMEOUT_S
            deadline = time.monotonic() + job_timeout
            exit_error = None
            try:
                while child.poll() is None:
                    state = service.registry.get_run(job["id"])["status"]
                    if stopping.is_set() or state == "cancelled" or time.monotonic() > deadline:
                        exit_error = (
                            "Worker stopped" if stopping.is_set() else "Run exceeded worker timeout"
                        )
                        signal_private_group(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    stopping.wait(0.1)
            finally:
                # A runner can finish, crash or exit on TERM while a solver
                # remains alive. Clean its private group on every exit path,
                # including observation errors, before moving to another job.
                # Permission failures remain errors; never claim cleanup when
                # the OS did not permit signalling a live group.
                signal_private_group(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
            cleanup_error = None
            if job.get("job_kind") in DISK_RAW_JOB_KINDS:
                # The child cannot run Python finally after forced termination.
                # Its optical temporary directory is confined to this run root.
                from .lens_sensor_out_of_core_raw_job import cleanup_work_root

                try:
                    cleanup_work_root(service.registry.directory, job["id"])
                except Exception as exc:
                    cleanup_error = (
                        f"Disk-backed scratch cleanup failed: {type(exc).__name__}: {exc}"
                    )
                    print(cleanup_error, file=sys.stderr, flush=True)
            # finish() uses a running-status CAS. A committed success, failure
            # or cancellation survives this idempotent fallback unchanged.
            final_error = exit_error or (
                f"Execution process exited without a result (code {child.returncode})"
            )
            if cleanup_error is not None:
                final_error += "; " + cleanup_error
            _retry_database_busy(
                lambda identity=job["id"], error=final_error: service.registry.finish(
                    identity, error=error
                ),
                label=f"reconciling execution exit for {job['id']}",
                timeout=DATABASE_SHUTDOWN_TIMEOUT_S if stopping.is_set() else None,
            )
        if once:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execute")
    parser.add_argument("--timeout", type=int)
    args = parser.parse_args()
    service = PlatformService(args.state_dir, args.workspace)
    if args.execute:
        execute(service, args.execute)
    else:
        work(service, once=args.once, timeout=args.timeout)


if __name__ == "__main__":
    main()

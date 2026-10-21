#!/usr/bin/env python3
"""Resume a source-frozen periodic OCL spectral campaign on the budgeted SSD.

One Meep process; persistent eight-hour wall deadline including reused pilot
solver time; 24 GiB RSS. Solver failures are preserved and bounded, whereas a
numerically invalid completed sample is recorded without hiding other cases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import psutil

from camera_platform import design_workshop_storage as storage
from camera_platform import sensor_structure_spectral as spectral
from camera_platform.registry import canonical, digest

SOLVER = Path("/Users/seongcheoljeong/FDTD/.meep-env/bin/python")
BUDGET_SECONDS = 8 * 3600
RSS_LIMIT = 24 * 1024**3


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(canonical(value))
    temporary.replace(path)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_tasks(path, workspace):
    payload = json.loads(Path(path).read_text())
    tasks = payload["tasks"] if isinstance(payload, dict) else payload
    if not isinstance(tasks, list) or not tasks or len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("Unique frozen solver tasks are required")
    worker = spectral.worker_path(workspace)
    worker_hash = sha(worker)
    for task in tasks:
        if task.get("worker_sha256") != worker_hash:
            raise ValueError("Current solver does not match task-pinned worker hash")
        if digest({k: v for k, v in task.items() if k != "id"}) != task["id"]:
            raise ValueError("Task identity mismatch")
    return tasks, worker, worker_hash


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def other_meep_processes():
    result = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = " ".join(process.info["cmdline"] or [])
            if process.pid != os.getpid() and ".meep-env/bin/python" in command:
                result.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def run(args):
    workspace = Path(args.workspace).resolve()
    tasks, source_worker, worker_hash = load_tasks(args.tasks, workspace)
    output = Path(args.output).resolve()
    status_path = Path(args.status).resolve()
    service = SimpleNamespace(workspace=workspace)
    project = storage.root(service).resolve()
    if not output.is_relative_to(project) or output == project:
        raise ValueError("Campaign output must be inside the budgeted SSD project")
    if not 1 <= args.max_seconds <= BUDGET_SECONDS:
        raise ValueError("Campaign budget must be at most eight hours")
    campaign_id = digest(dict(tasks=tasks, worker_sha256=worker_hash))
    samples_dir = output/"samples"
    ledger_path = output/"ledger.json"
    interrupted = False

    def cancel(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)
    # Uses the common writer lock and 500 GB accounting; never bypasses other
    # capture jobs or lowers resolution to fit storage.
    with storage.reserve(service, 2_000_000_000, allow_internal_aliases=True) as (_, check):
        output.mkdir(parents=True, exist_ok=True)
        samples_dir.mkdir(exist_ok=True)
        frozen = output/"worker.py"
        if frozen.exists() and sha(frozen) != worker_hash:
            raise ValueError("Preserve changed frozen solver; do not overwrite")
        if not frozen.exists():
            shutil.copyfile(source_worker, frozen)
        samples = []
        for task in tasks:
            path = samples_dir/(task["id"]+".json")
            if path.exists():
                samples.append(spectral.validate_sample(task, path, worker_sha256=worker_hash))
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text())
            if ledger["campaign_id"] != campaign_id or ledger["max_seconds"] != args.max_seconds:
                raise ValueError("Campaign identity/budget changed; cannot reset the prior ledger")
        else:
            reused_seconds = sum(float(s.get("elapsed_s", 0)) for s in samples)
            started = time.time()
            ledger = dict(schema="camera_ocl_spectral_campaign_ledger_v1", campaign_id=campaign_id,
                created_epoch=started, deadline_epoch=started+args.max_seconds-reused_seconds,
                max_seconds=args.max_seconds, reused_solver_seconds=reused_seconds,
                scope="new RGB periodic-plane-wave study; prior finite-beam campaign preserved",
                attempts=[], failed_tasks={}, worker_sha256=worker_hash)
            atomic_json(ledger_path, ledger)
        atomic_json(output/"tasks.json", tasks)
        completed = {s["task_id"] for s in samples}
        failures = ledger.setdefault("failed_tasks", {})
        current = None
        state = "running"

        def report():
            now = time.time()
            value = dict(schema="camera_ocl_spectral_campaign_v1", campaign_id=campaign_id,
                status=state, output_directory=str(output), completed=len(samples), total=len(tasks),
                numerical_valid=sum(s["numerical_valid"] for s in samples), failed=len(failures),
                failed_tasks=failures, current=current,
                budget_seconds=ledger["max_seconds"], deadline_epoch=ledger["deadline_epoch"],
                budget_elapsed_s=now-ledger["created_epoch"]+ledger["reused_solver_seconds"],
                budget_remaining_s=max(0., ledger["deadline_epoch"]-now),
                solver_elapsed_s=sum(float(a.get("elapsed_s", 0)) for a in ledger["attempts"])
                                 +ledger["reused_solver_seconds"],
                worker_sha256=worker_hash, samples=samples, ratios=spectral.ratios(samples),
                camera_response_eligible=False, measured_accuracy=False)
            atomic_json(output/"summary.json", value)
            atomic_json(status_path, {k: v for k, v in value.items() if k not in ("samples", "ratios")})
            return value

        report()
        for task in tasks:
            if task["id"] in completed or task["id"] in failures:
                continue
            if interrupted or time.time() >= ledger["deadline_epoch"]:
                state = "cancelled" if interrupted else "budget_exhausted"
                break
            if other_meep_processes():
                state = "blocked_other_meep"
                current = {"reason": "Another Meep process exists; not launching a second solver"}
                report()
                break
            current = {k: task[k] for k in ("id", "field", "wavelength_nm", "cfa_channel",
                                           "variant", "polarization", "resolution", "angle_deg")}
            report()
            final = samples_dir/(task["id"]+".json")
            request = samples_dir/(task["id"]+".task.json")
            atomic_json(request, task)
            previous_attempts = [a for a in ledger["attempts"] if a["task_id"] == task["id"]]
            if len(previous_attempts) >= 2:
                failures[task["id"]] = dict(reason="Bounded attempts exhausted before restart", attempts=len(previous_attempts))
                atomic_json(ledger_path, ledger)
                report()
                continue
            # Maximum initial attempt + one retry, across process restarts.
            for number in range(len(previous_attempts)+1, 3):
                if interrupted or time.time() >= ledger["deadline_epoch"]:
                    state = "cancelled" if interrupted else "budget_exhausted"
                    break
                temporary = samples_dir/(task["id"]+f".attempt-{number}.partial.json")
                log_path = samples_dir/(task["id"]+f".attempt-{number}.log")
                attempt = dict(task_id=task["id"], number=number, started_epoch=time.time(),
                               status="running", elapsed_s=0., observed_peak_rss_bytes=0)
                ledger["attempts"].append(attempt)
                atomic_json(ledger_path, ledger)
                started = time.monotonic()
                peak, outcome, error = 0, "failed", None
                try:
                    env = {**os.environ, "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "1"}
                    with log_path.open("w") as log:
                        process = subprocess.Popen([str(SOLVER), str(frozen), str(request), str(temporary)],
                            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
                        try:
                            heartbeat = 0.
                            while process.poll() is None:
                                if interrupted:
                                    raise InterruptedError("Campaign cancellation requested")
                                if time.time() >= ledger["deadline_epoch"]:
                                    raise TimeoutError("Eight-hour deadline reached; precision retained")
                                p = psutil.Process(process.pid)
                                try:
                                    rss = sum(child.memory_info().rss for child in [p, *p.children(recursive=True)] if child.is_running())
                                except psutil.NoSuchProcess:
                                    rss = 0
                                peak = max(peak, rss)
                                if peak > RSS_LIMIT:
                                    raise MemoryError("24 GiB single-process limit exceeded")
                                if time.monotonic()-heartbeat >= 5:
                                    heartbeat = time.monotonic()
                                    attempt.update(elapsed_s=time.monotonic()-started, observed_peak_rss_bytes=peak)
                                    atomic_json(ledger_path, ledger)
                                    report()
                                time.sleep(.5)
                            if process.returncode:
                                raise RuntimeError("Meep failed: " + log_path.read_text()[-1800:])
                        finally:
                            stop_process(process)
                    sample = spectral.validate_sample(task, temporary, worker_sha256=worker_hash)
                    sample["observed_peak_rss_bytes"] = peak
                    atomic_json(temporary, sample)
                    # Validity may be false; completed arrays are still retained.
                    temporary.with_suffix(".npz").replace(final.with_suffix(".npz"))
                    temporary.replace(final)
                    samples.append(sample)
                    completed.add(task["id"])
                    outcome = "completed"
                except (InterruptedError, TimeoutError, MemoryError) as exc:
                    error = str(exc)
                    state = ("cancelled" if isinstance(exc, InterruptedError) else
                             "budget_exhausted" if isinstance(exc, TimeoutError) else "resource_limit")
                except Exception as exc:
                    error = str(exc)
                finally:
                    attempt.update(status=outcome, elapsed_s=time.monotonic()-started,
                                   observed_peak_rss_bytes=peak, error=error, ended_epoch=time.time())
                    atomic_json(ledger_path, ledger)
                    report()
                if outcome == "completed" or state != "running":
                    break
                if number == 2:
                    failures[task["id"]] = dict(reason=error, attempts=2)
                    atomic_json(ledger_path, ledger)
            if state != "running":
                break
            if len(samples) % 24 == 0:
                check()
        if state == "running":
            state = "completed" if len(samples) == len(tasks) else "completed_with_failures"
        current = None
        check()
        summary = report()
        print(json.dumps({k: v for k, v in summary.items() if k not in ("samples", "ratios")}, ensure_ascii=False), flush=True)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--max-seconds", type=int, default=BUDGET_SECONDS)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()

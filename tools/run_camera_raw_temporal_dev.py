#!/usr/bin/env python3
"""Run the optional RAW comparison workbench on a SEPARATE development state.

Copy only the archived synthetic QA registry/blobs, never the installed primary.
Prepare one continuous 16-frame example through the real Worker process. Start
a foreground loopback API/Worker only with --serve. Existing evidence is reused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from camera_platform import capture_programs, sequences
from camera_platform.contracts import CaptureProgramCreate, ControlSequenceCreate, SceneSequenceCreate
from camera_platform.service import PlatformService
from run_camera_pwm_case import SOURCE, STATE, encode, load_original
from run_camera_pwm_transient import extend_static

DEFAULT = ROOT / "outputs/camera-platform-raw-temporal-dev-20260926"


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def initialize(state: Path) -> dict:
    state = state.resolve()
    if state.parent != ROOT / "outputs" or state.name != DEFAULT.name:
        raise ValueError("Use the dedicated RAW temporal development state; never the installed primary")
    marker = state / "raw-temporal-dev.json"
    if marker.exists():
        data = json.loads(marker.read_text())
        if data.get("kind") != "isolated_raw_temporal_development":
            raise ValueError("Unexpected development state marker")
        return data
    if state.exists():
        raise FileExistsError("Partial state exists; preserve it and inspect before retrying")
    state.mkdir(parents=True)
    with sqlite3.connect(f"file:{STATE / 'registry.sqlite'}?mode=ro", uri=True) as source:
        with sqlite3.connect(state / "registry.sqlite") as target:
            source.backup(target)
    shutil.copytree(STATE / "blobs", state / "blobs", symlinks=False)
    service = PlatformService(state, ROOT)
    if any(j["status"] in {"queued", "running"} for j in service.registry.list_runs()):
        raise ValueError("Copied archive has pending work; do not execute unrelated queued jobs")
    record, request, original_raw, _, source_hash, _ = load_original(SOURCE, STATE)
    extended = extend_static(request)
    scene = fields(record["scene_sequence_snapshot"], SceneSequenceCreate)
    scene.update(name="RAW replay demo · original static input / 16 frames", revision="raw-replay-16",
                 parents=[record["scene_sequence_id"]])
    scene["payload"]["knots"][-1]["time_ns"] = extended["scene_sequence"]["knots"][-1]["time_ns"]
    scene = sequences.create_sequence(service, "scene_sequences", scene)
    control = fields(record["control_sequence_snapshot"], ControlSequenceCreate)
    control.update(name="RAW replay demo · 20 ms continuous control", revision="raw-replay-16",
                   parents=[record["control_sequence_id"]], payload=extended["control_sequence"],
                   scene_sequence_id=scene["id"])
    control = sequences.create_sequence(service, "control_sequences", control)
    program = fields(record["capture_program_snapshot"], CaptureProgramCreate)
    program.update(name="RAW replay demo · unchanged trap/PWM, 16 frames", revision="raw-replay-16",
                   parents=[record["capture_program_id"]], payload=extended["capture_program"])
    program = capture_programs.create(service, program)
    test = service.create_test(sequences.test_template(service, record["assembly_id"], scene["id"],
                                                      control["id"], program["id"]))
    queued = service.submit_run({"assembly_id": record["assembly_id"], "test_id": test["id"],
        "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "electrical_control_v1",
        "parameters": {"seed": request["seed"], "noise_enabled": False}})
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "src")}
    with (state / "demo-worker.log").open("w") as log:
        worker = subprocess.run([sys.executable, "-m", "camera_platform.worker", "--state-dir", str(state),
            "--workspace", str(ROOT), "--once", "--timeout", "120"], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT, timeout=150, check=False)
    result = service.registry.get_run(queued["id"])
    if worker.returncode or result["status"] != "succeeded":
        raise RuntimeError(f"Demo acquisition failed: {result.get('error')}; see demo-worker.log")
    import numpy as np
    artifact = next(a for a in result["artifacts"] if a["name"] == "raw_groups.npy")
    current = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
    if current.dtype != original_raw.dtype or not np.array_equal(current[:4], original_raw):
        raise AssertionError("Registered 16-frame acquisition changed the historical four-frame RAW prefix")
    data = {"kind": "isolated_raw_temporal_development", "state_dir": str(state),
        "source_run_id": result["id"], "historical_source_sha256": source_hash,
        "historical_prefix_exact": True, "demo_worker_exit_code": worker.returncode,
        "physical_validation": "not_established", "primary_modified": False,
        "url": "http://127.0.0.1:8796/raw-temporal.html"}
    marker.write_bytes(encode(data))
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8796)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 8770:
        parser.error("Choose a development port, never primary port 8770")
    print(json.dumps(initialize(DEFAULT), ensure_ascii=False), flush=True)
    if args.serve:
        import uvicorn
        from camera_platform.api import create_app
        uvicorn.run(create_app(DEFAULT, workspace=ROOT, start_worker=True), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

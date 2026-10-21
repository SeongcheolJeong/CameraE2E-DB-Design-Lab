"""Prepare the bounded3x3 patent prescription grid with three independent solvers."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs/reference-optics-grid-v1"


def candidate(pair):
    curvature, shift = pair
    name = f"c{round(curvature * 1000):04d}-z{shift:+03d}"
    directory = OUTPUT / name
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    with (OUTPUT / (name + ".log")).open("w") as log:
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/reference_optics_demo.py"),
                "--output",
                str(directory),
                "--curvature-scale",
                str(curvature),
                "--sensor-shift-um",
                str(shift),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    path = directory / "result.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    return {
        "name": name,
        "directory": str(directory),
        "exit_code": process.returncode,
        "revision": result.get("revision"),
        "status": result.get("status"),
        "elapsed_s": result.get("elapsed_s"),
        "succeeded_fields": sum(f["status"] == "succeeded" for f in result.get("fields", [])),
    }


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    pairs = [
        (curvature, shift)
        for curvature in (0.995, 1.0, 1.005)
        for shift in (-20, 0, 20)
        if (curvature, shift) not in {(1.0, 0), (1.005, 0)}
    ]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for done in concurrent.futures.as_completed([executor.submit(candidate, p) for p in pairs]):
            result = done.result()
            results.append(result)
            print(json.dumps(result), flush=True)
            (OUTPUT / "preparation.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(r["exit_code"] == 0 for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

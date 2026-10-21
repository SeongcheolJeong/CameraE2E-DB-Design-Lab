"""Run eligible sensors and persist resumable CameraE2E simulation artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pyisetcam.sensor_simulation_db import (
    build_sensor_simulation_db,
    sensor_simulation_db_validate,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="CameraE2E DB root directory")
    parser.add_argument("--catalog-root", type=Path, help="Source sensor_db catalog directory")
    parser.add_argument("--sensors", nargs="+", help="Report codes or canonical IDs; default all")
    parser.add_argument("--size", type=int, default=32)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Keep history and create fresh runs")
    parser.add_argument(
        "--report", type=Path, default=Path("reports/sensor_simulation_db/build.json")
    )
    args = parser.parse_args()

    def progress(item):
        if item["index"] % 5 == 0 or item["status"] in {"failed", "quarantined"}:
            print(json.dumps(item), flush=True)

    result = build_sensor_simulation_db(
        root=args.root,
        catalog_root=args.catalog_root,
        sensor_ids=args.sensors,
        settings={"size": args.size, "nframes": args.frames, "seed": args.seed},
        force=args.force,
        progress=progress,
    )
    validation = sensor_simulation_db_validate(root=args.root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"summary": result, "validation": validation}, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "db_path": result["db_path"],
                "counts": result["counts"],
                "build_counts": result["build_counts"],
                "validation": validation,
            },
            indent=2,
        )
    )
    return 0 if validation["status"] == "passed" and not result["counts"]["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare validated sensor FDTD LUTs; report missing inputs as pending."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyisetcam.sensor_fdtd_comparison import camerae2e_compare_fdtd_sensors  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lut", action="append", default=[], metavar="SENSOR=PATH")
    parser.add_argument(
        "--sensors", nargs="+", default=["DEF-2011-804", "DEF-1807-801", "DEF-2206-805"],
    )
    parser.add_argument("--output", type=Path, default=Path("reports/fdtd_sensor_e2e/comparison"))
    parser.add_argument("--frames", type=int, default=32)
    parser.add_argument("--size", type=int, default=128)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    inputs = {}
    for item in args.lut:
        if "=" not in item:
            parser.error("--lut requires SENSOR=PATH")
        code, path = item.split("=", 1)
        if not code or not path or code in inputs:
            parser.error("--lut must specify unique nonempty sensor identifiers and paths")
        inputs[code] = path
    options = {}
    if args.smoke:
        options = {"illuminants": ("D65",), "scene_types": ("uniform",),
                   "fnumbers": (4,), "cra_degrees": (0, 20), "azimuth_degrees": (0,)}
    report = camerae2e_compare_fdtd_sensors(
        inputs, sensor_ids=args.sensors, output_dir=args.output, nframes=args.frames,
        output_size=(args.size, args.size), **options,
    )
    print(json.dumps({k: report[k] for k in (
        "status", "pending_sensors", "case_count", "sample_count", "artifacts",
    )}, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())

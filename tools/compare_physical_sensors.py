"""Run practical estimated sensor E2E comparisons without a 3D FDTD LUT."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyisetcam.sensor_physical_comparison import camerae2e_compare_physical_sensors  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sensors", nargs="+", default=["DEF-2011-804", "DEF-1807-801", "DEF-2206-805"]
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/physical_sensor_e2e/comparison")
    )
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--size", type=int, default=32)
    parser.add_argument("--no-sensitivity", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    options = {}
    if args.smoke:
        options = {
            "illuminants": ("D65",),
            "scene_types": ("uniform",),
            "fnumbers": (4,),
            "cra_degrees": (0, 20),
            "azimuth_degrees": (0,),
        }
    report = camerae2e_compare_physical_sensors(
        sensor_ids=args.sensors,
        output_dir=args.output,
        nframes=args.frames,
        output_size=(args.size, args.size),
        sensitivity=not args.no_sensitivity,
        **options,
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "status",
                    "case_count",
                    "sample_count",
                    "sensitivity_sample_count",
                    "stress_sample_count",
                    "routing_sample_count",
                    "optical_solve_count",
                    "reasonableness",
                    "artifacts",
                )
            },
            indent=2,
        )
    )
    return 0 if report["reasonableness"]["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

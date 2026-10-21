#!/usr/bin/env python3
"""Write a matched-crop DB-estimate sensor comparison, without external solvers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyisetcam.sensor_comparison import camerae2e_compare_sensors  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensors", nargs="+", help="Catalog sensor IDs or report codes.")
    parser.add_argument("--root", type=Path, help="Optional image sensor catalog root.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/db_sensor_comparison"))
    parser.add_argument("--frames", type=int, default=32)
    parser.add_argument("--luminances", type=float, nargs="+", default=[1, 10, 100])
    parser.add_argument(
        "--exposures", type=float, nargs="+", default=[0.001, 0.004, 0.016], help="Seconds."
    )
    parser.add_argument(
        "--output-size", type=int, nargs=2, default=[128, 128], metavar=("ROWS", "COLS")
    )
    parser.add_argument(
        "--scenes",
        nargs="+",
        choices=["uniform", "macbeth_d65", "slanted_edge"],
        default=["uniform", "macbeth_d65", "slanted_edge"],
    )
    parser.add_argument(
        "--baseline-only", action="store_true", help="Disable the eleven-set sensitivity sweep."
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fnumber", type=float, default=4.0)
    parser.add_argument("--focal-length-m", type=float, default=0.004)
    args = parser.parse_args(argv)
    result = camerae2e_compare_sensors(
        args.sensors,
        root=args.root,
        output_dir=args.output_dir,
        nframes=args.frames,
        luminances=args.luminances,
        exposures=args.exposures,
        output_size=tuple(args.output_size),
        scene_types=args.scenes,
        assumption_sweep=not args.baseline_only,
        seed=args.seed,
        fnumber=args.fnumber,
        focal_length_m=args.focal_length_m,
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "estimate_only",
                    "comparison_contract",
                    "case_count",
                    "sample_count",
                    "optical_solve_count",
                    "artifacts",
                ]
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

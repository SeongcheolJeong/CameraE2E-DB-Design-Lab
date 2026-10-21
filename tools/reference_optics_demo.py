"""Replay or inspect pinned patent optical-bench candidates without changing DB inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from camera_platform import reference_workshop_optics as optics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--curvature-scale", type=float, default=1.0)
    parser.add_argument("--sensor-shift-um", type=float, default=0.0)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    if args.inspect:
        result = optics.load_solution(args.output)["result"]
    else:
        spec = optics.template(workspace)
        spec["controls"].update(
            curvature_scale=args.curvature_scale, sensor_shift_um=args.sensor_shift_um
        )
        result = optics.solve(
            workspace, spec, args.output, on_progress=lambda message: print(message, flush=True)
        )
    print(
        json.dumps(
            {
                "status": result["status"],
                "revision": result["revision"],
                "elapsed_s": result.get("elapsed_s"),
                "fields": [
                    {
                        key: field.get(key)
                        for key in (
                            "id",
                            "status",
                            "issues",
                            "chief_ray_angle_deg",
                            "relative_illumination",
                            "distortion_percent",
                            "minimum_retained_energy_fraction",
                            "sampling_convergence",
                        )
                    }
                    for field in result["fields"]
                ],
            },
            indent=2,
        )
    )
    return 0 if all(f["status"] == "succeeded" for f in result["fields"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())

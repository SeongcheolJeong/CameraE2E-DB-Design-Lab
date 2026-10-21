#!/usr/bin/env python3
"""Generate canonical sensor bundles and run at most one bounded Meep pilot."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None):
    from pyisetcam.cfa_materials import image_sensor_cfa_materials

    spec = importlib.util.spec_from_file_location(
        "sensor_fdtd_runner", ROOT / "simulations/fdtd_tcad/run_sensor_response.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/fdtd_sensor_e2e/solver")
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--sensors", nargs="+", default=["DEF-2011-804", "DEF-1807-801", "DEF-2206-805"]
    )
    parser.add_argument("--resolution", type=int, default=80)
    parser.add_argument("--sampling", choices=["initial", "final", "refined"], default="final")
    for name in ("wavelength-step-nm", "theta-step-deg", "phi-step-deg"):
        parser.add_argument("--" + name, type=float)
    parser.add_argument("--run-pilot", choices=["planar_calibration", "3d_process_check"])
    parser.add_argument("--max-wall-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    paths = []
    for code in args.sensors:
        bundle = image_sensor_cfa_materials(code, root=args.root)
        path = args.output_dir / "input_bundles" / (bundle["report_code"] + ".json")
        if path.exists() and json.loads(path.read_text()) != bundle:
            raise ValueError("Canonical bundle changed: use a new campaign directory.")
        runner.write_json(path, bundle)
        paths.append(path)
    manifest = runner.plan_campaign(
        paths,
        args.output_dir,
        resolution=args.resolution,
        sampling=args.sampling,
        wavelength_step_nm=args.wavelength_step_nm,
        theta_step_deg=args.theta_step_deg,
        phi_step_deg=args.phi_step_deg,
    )
    result = {
        "manifest": str(args.output_dir.resolve() / "campaign.json"),
        "production_jobs": manifest["production_jobs"],
        "total_jobs": len(manifest["jobs"]),
    }
    if args.run_pilot:
        result["pilot"] = runner.run_job(
            result["manifest"], role=args.run_pilot, max_wall_seconds=args.max_wall_seconds
        )
    result["status"] = runner.campaign_status(result["manifest"])
    runner.write_json(args.output_dir / "campaign_status.json", result["status"])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

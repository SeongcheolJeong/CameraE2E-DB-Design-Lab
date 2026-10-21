#!/usr/bin/env python3
"""Compute matched full-field angular-off/on acquisitions and a same-RAW ISP replay."""

import argparse
import copy
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--fov", type=float, default=12)
    parser.add_argument("--lens", choices=["L1", "L2", "analytic"], default="L1")
    parser.add_argument("--sensor", choices=["S1", "S2"], default="S1")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["off", "physical_relative", "physical_pupil", "cosine_power"],
        default=["off", "physical_relative"],
        help="Angular response acquisitions to compare (same camera, scene and seed).",
    )
    parser.add_argument(
        "--suite", choices=["all", "resolution", "color", "noise", "tone"], default="all"
    )
    parser.add_argument("--output", default="outputs/virtual-lab-cra-demo-20261011/runs.json")
    args = parser.parse_args()
    records = []
    with PlatformClient(args.base_url, timeout=120) as client:
        template = client.virtual_lab_template()
        spec = template["specification"]
        lens = next(item for item in template["catalog"]["lenses"] if item["id"] == args.lens)
        spec.update(lens_id=args.lens, sensor_id=args.sensor, test_suite=args.suite)
        spec["scene"] = (
            "uniform"
            if args.suite == "noise"
            else "resolution"
            if args.suite == "all"
            else args.suite
        )
        spec["optics"]["f_number"] = lens.get("f_number", 4)
        spec["field_model"] = {"mode": "full_field", "horizontal_fov_deg": args.fov}
        for mode in args.modes:
            request = copy.deepcopy(spec)
            request["name"] = f"Full-field {args.lens} × {args.sensor} · {args.fov:g}° · {mode}"
            request["angular_response"]["mode"] = mode
            job = client.submit_virtual_lab(request)
            print(f"{request['name']}: {job['id']}", flush=True)
            record = client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=2)
            records.append(record)
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2))
            if record["status"] != "succeeded":
                raise RuntimeError(record.get("error"))
        isp = {
            **records[-1]["specification"]["isp"],
            "profile": "detail",
            "denoise": 0,
            "sharpen": 1.2,
        }
        job = client.replay_virtual_lab(
            records[-1]["id"], isp, name="Full-field angular response · same RAW detail ISP"
        )
        record = client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=2)
        records.append(record)
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2))
        if record["status"] != "succeeded":
            raise RuntimeError(record.get("error"))
        print(
            json.dumps(
                [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "report_url": args.base_url + r["result"]["summary"]["report_url"],
                    }
                    for r in records
                ],
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

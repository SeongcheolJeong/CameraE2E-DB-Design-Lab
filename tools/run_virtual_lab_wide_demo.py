#!/usr/bin/env python3
"""Submit real 2MP wide-angle Lab captures and same-RAW ISP replay through the SDK."""

import argparse
import copy
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--sensors", nargs="+", choices=["S1", "S2"], default=["S1", "S2"])
    parser.add_argument(
        "--projections",
        nargs="+",
        choices=["rectilinear", "equidistant"],
        default=["rectilinear", "equidistant"],
    )
    parser.add_argument(
        "--scene",
        choices=["grid", "natural", "resolution", "color", "uniform", "tone"],
        default="natural",
    )
    parser.add_argument(
        "--suite", choices=["all", "resolution", "color", "noise", "tone"], default="all"
    )
    parser.add_argument("--sampling", choices=["auto", "2", "4", "8"], default="auto")
    parser.add_argument("--fov", type=float, default=120)
    parser.add_argument(
        "--angular",
        choices=["off", "physical_relative", "physical_pupil", "cosine_power"],
        default="physical_pupil",
    )
    parser.add_argument("--output", default="outputs/virtual-lab-wide-demo-20261011/runs.json")
    parser.add_argument("--no-replay", action="store_true")
    args = parser.parse_args()
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    records = []

    def save(record):
        records.append(record)
        destination.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        if record["status"] != "succeeded":
            raise RuntimeError(record.get("error") or record["status"])
        print(args.base_url + record["result"]["summary"]["report_url"], flush=True)

    with PlatformClient(args.base_url, timeout=120) as client:
        template = client.virtual_lab_template()["specification"]
        for sensor in args.sensors:
            for projection in args.projections:
                spec = copy.deepcopy(template)
                spec.update(
                    name=f"120° {projection} · {sensor} · real 2MP RAW",
                    lens_id="wide_parametric",
                    sensor_id=sensor,
                    scene=args.scene,
                    test_suite=args.suite,
                )
                spec["field_model"] = {"mode": "full_field", "horizontal_fov_deg": args.fov}
                spec["optics"] = {"f_number": 2.8, "defocus_um": 0}
                spec["angular_response"]["mode"] = args.angular
                spec["wide_angle"].update(
                    projection=projection,
                    sampling="auto" if args.sampling == "auto" else int(args.sampling),
                )
                job = client.submit_virtual_lab(spec)
                print(f"Capture {sensor}/{projection}: {job['id']}", flush=True)
                save(client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=3))
        if not args.no_replay:
            isp = {
                **records[0]["specification"]["isp"],
                "profile": "detail",
                "denoise": 0,
                "sharpen": 1.2,
            }
            job = client.replay_virtual_lab(
                records[0]["id"], isp, name="120° same native RAW · detail ISP"
            )
            print(f"Replay: {job['id']}", flush=True)
            save(client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=3))


if __name__ == "__main__":
    main()

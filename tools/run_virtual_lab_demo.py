#!/usr/bin/env python3
"""Prepare real, reusable Lens x Sensor and same-RAW ISP demo results."""

import argparse
import copy
import json

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--matrix", action="store_true", help="Execute all four DB camera combinations"
    )
    parser.add_argument(
        "--showcase",
        action="store_true",
        help="Prepare four cameras, general optics, warm light, low light and ISP variants",
    )
    parser.add_argument(
        "--suite", default="all", choices=["all", "resolution", "color", "noise", "tone"]
    )
    args = parser.parse_args()
    with PlatformClient(args.base_url, timeout=120) as client:
        template = client.virtual_lab_template()
        spec = template["specification"]
        spec["test_suite"] = args.suite
        lenses = [v for v in template["catalog"]["lenses"] if v["id"] in {"L1", "L2"}]
        sensors = template["catalog"]["sensors"][:2]
        combinations = (
            [(lens, sensor) for lens in lenses for sensor in sensors]
            if args.matrix or args.showcase
            else [(lenses[0], sensors[0])]
        )
        runs = []
        for lens, sensor in combinations:
            request = copy.deepcopy(spec)
            request.update(
                lens_id=lens["id"],
                sensor_id=sensor["id"],
                name=f"Lab {lens['id']} × {sensor['id']} · {args.suite}",
            )
            request["optics"]["f_number"] = lens.get("f_number", request["optics"]["f_number"])
            job = client.submit_virtual_lab(request)
            print(f"{request['name']}: {job['id']}", flush=True)
            complete = client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=2)
            if complete["status"] != "succeeded":
                raise RuntimeError(complete.get("error"))
            runs.append(complete)
        baseline = runs[0]
        if args.showcase:
            examples = [
                {
                    "name": "General optics · f/4 focused",
                    "lens_id": "analytic",
                    "test_suite": "resolution",
                    "optics": {"f_number": 4, "defocus_um": 0},
                },
                {
                    "name": "General optics · f/8 defocus 30 µm",
                    "lens_id": "analytic",
                    "test_suite": "resolution",
                    "optics": {"f_number": 8, "defocus_um": 30},
                },
                {
                    "name": "Warm illumination · A",
                    "illuminant": "A",
                    "scene": "color",
                    "test_suite": "color",
                },
                {
                    "name": "Low light · 100 cd/m²",
                    "luminance_cd_m2": 100,
                    "scene": "natural",
                    "test_suite": "all",
                },
            ]
            for example in examples:
                request = {**copy.deepcopy(spec), **example}
                job = client.submit_virtual_lab(request)
                print(f"{request['name']}: {job['id']}", flush=True)
                complete = client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=2)
                if complete["status"] != "succeeded":
                    raise RuntimeError(complete.get("error"))
                runs.append(complete)
        for profile, denoise, sharpen in (("detail", 0, 1.2), ("low_noise", 0.85, 0)):
            isp = {
                **baseline["specification"]["isp"],
                "profile": profile,
                "denoise": denoise,
                "sharpen": sharpen,
            }
            job = client.replay_virtual_lab(baseline["id"], isp, name="Lab same RAW · " + profile)
            print(f"{profile}: {job['id']}", flush=True)
            complete = client.wait_virtual_lab(job["id"], timeout=3660, poll_interval=2)
            if complete["status"] != "succeeded":
                raise RuntimeError(complete.get("error"))
            runs.append(complete)
        print(
            json.dumps(
                [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "report_url": args.base_url + r["result"]["summary"]["report_url"],
                    }
                    for r in runs
                ],
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

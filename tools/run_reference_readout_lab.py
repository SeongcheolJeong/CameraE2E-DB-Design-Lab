#!/usr/bin/env python3
"""Prepare source-pinned readout demonstrations through the public SDK.

Each receipt is saved before waiting. Re-running the same specification uses the
server's content identity; it never treats a failed or cancelled run as success.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from camera_platform.sdk import PlatformClient

RECIPES = {
    "gain_budget": {
        "charts": ["uniform", "texture", "dynamic_range"],
        "light_scales": [0.01, 1.0],
        "compare": ["digital_gain", "analog_gain"],
        "chart": "texture",
        "light": 0.01,
    },
    "noise_structure": {
        "charts": ["dark", "uniform", "texture"],
        "light_scales": [0.01],
        "compare": ["pixel_iid", "column_noise"],
        "chart": "dark",
        "light": 0.01,
    },
    "adc_headroom": {
        "charts": ["uniform", "dynamic_range"],
        "light_scales": [1.0],
        "compare": ["adc_10bit", "adc_14bit"],
        "chart": "dynamic_range",
        "light": 1.0,
    },
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/reference-readout-lab-20261012")
    )
    parser.add_argument("--recipe", choices=["all", *RECIPES], default="all")
    parser.add_argument("--timeout", type=float, default=7260)
    args = parser.parse_args()
    selected = list(RECIPES) if args.recipe == "all" else [args.recipe]
    receipts = []
    with PlatformClient(args.base_url, timeout=120) as client:
        for recipe in selected:
            config = RECIPES[recipe]
            started = time.monotonic()
            job = client.submit_reference_readout_study(
                recipe,
                fields=["center", "outer-ne"],
                charts=config["charts"],
                light_scales=config["light_scales"],
                name="Reference Readout Design Lab · " + recipe,
            )
            receipt = {"recipe": recipe, "id": job["id"], "status": job["status"]}
            receipts.append(receipt)
            write(args.output / f"{recipe}-submitted.json", job)
            write(args.output / "runs.json", receipts)
            print(f"{recipe}: {job['id']} · {job['status']}", flush=True)
            try:
                done = client.wait_reference_workshop(
                    job["id"], timeout=args.timeout, poll_interval=3
                )
            except TimeoutError:
                receipt["wait_status"] = "timeout_job_may_still_be_running"
                write(args.output / "runs.json", receipts)
                raise
            write(args.output / f"{recipe}-record.json", done)
            receipt["status"] = done["status"]
            if done["status"] != "succeeded":
                write(args.output / "runs.json", receipts)
                raise RuntimeError(f"{recipe}: {done['status']}: {done.get('error')}")
            summary = done["result"]["summary"]
            case = next(
                c
                for c in summary["cases"]
                if c["field_id"] == "outer-ne"
                and c["chart"] == config["chart"]
                and c["light_scale"] == config["light"]
            )
            a, b = config["compare"]
            profile = client.save_reference_sensor_mode_profile(
                job["id"],
                {
                    "name": "Readout demo · " + recipe,
                    "case_id": case["id"],
                    "mode": b,
                    "baseline_mode": a,
                    "interpolation": "nearest",
                },
            )
            write(args.output / f"{recipe}-profile.json", profile)
            query = urlencode(
                {
                    "sensor_study": job["id"],
                    "sensor_case": case["id"],
                    "field": "outer-ne",
                    "sensor_a": a,
                    "sensor_b": b,
                }
            )
            receipt.update(
                {
                    "demo_url": args.base_url.rstrip("/") + "/reference-workshop.html?" + query,
                    "report_url": args.base_url.rstrip("/") + summary["report_url"],
                    "bundle_url": args.base_url.rstrip("/") + summary["bundle_url"],
                    "manifest_sha256": summary["manifest_sha256"],
                    "package_bytes": summary["package_bytes"],
                    "cases": len(summary["cases"]),
                    "new_readout_frames": summary["new_readout_frames"],
                    "elapsed_seconds": summary["elapsed_seconds"],
                    "observed_wait_seconds": time.monotonic() - started,
                    "profile_id": profile["id"],
                }
            )
            write(args.output / "runs.json", receipts)
            print(receipt["demo_url"], flush=True)
    write(
        args.output / "delivery.json",
        {
            "title": "Reference Sensor Readout Design Lab",
            "studies": receipts,
            "physical_validation": "not_established",
            "product_modes": False,
        },
    )


if __name__ == "__main__":
    main()

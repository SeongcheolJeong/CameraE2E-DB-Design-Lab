#!/usr/bin/env python3
"""Prepare Reference Binning/DCG studies through the public SDK, sequentially.

The server owns source checks, acquisition, storage limits and cached job identity.
This entrypoint saves each submitted ID before waiting so timeouts are recoverable.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urlencode

from camera_platform.sdk import PlatformClient


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def specifications(client, recipe="both", *, reprocess=None, margin_dn=None):
    """Resolve server truth; replay retains every frozen acquisition setting."""
    if reprocess:
        if recipe == "binning":
            raise ValueError("--reprocess supports paired DCG RAW, not binning.")
        previous = client.reference_sensor_mode_study(reprocess)
        if (
            previous["status"] != "succeeded"
            or previous["sensor_mode_study"]["recipe"] != "dual_gain"
        ):
            raise ValueError("--reprocess requires a succeeded dual_gain study.")
        specification = copy.deepcopy(previous["sensor_mode_study"])
        specification["name"] = "Reference Sensor Mode · DCG preserved RAW replay"
        specification["reprocess_of"] = reprocess
        result = [specification]
    else:
        template = client.reference_sensor_mode_studies()["template"]
        recipes = ("binning", "dual_gain") if recipe == "both" else (recipe,)
        result = []
        for selected in recipes:
            specification = copy.deepcopy(template)
            specification["recipe"] = selected
            specification["name"] = "Reference Sensor Mode · " + (
                "Binning" if selected == "binning" else "Dual Gain"
            )
            if selected == "dual_gain":
                specification["isp"]["display_white_level"] = 16.0
                if "dynamic_range" not in specification["charts"]:
                    specification["charts"].append("dynamic_range")
            result.append(specification)
    if margin_dn is not None:
        if margin_dn < 1:
            raise ValueError("--margin-dn must be at least one code.")
        if all(spec["recipe"] != "dual_gain" for spec in result):
            raise ValueError("--margin-dn changes DCG merge only; select dual_gain or both.")
        for spec in result:
            if spec["recipe"] == "dual_gain":
                spec["dcg_merge"]["saturation_margin_dn"] = margin_dn
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--recipe", choices=("binning", "dual_gain", "both"), default="both")
    parser.add_argument(
        "--output",
        default="outputs/reference-sensor-mode-lab-delivery",
        help="Directory for exact specifications, submitted IDs and final records.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=7260,
        help="Per-study wait seconds; timeout does not cancel the server job.",
    )
    parser.add_argument("--reprocess", metavar="RUN", help="Remerge preserved paired DCG RAW.")
    parser.add_argument("--margin-dn", type=float, help="DCG RAW saturation guard margin, >=1 DN.")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    output = Path(args.output)
    receipts = []
    with PlatformClient(args.base_url, timeout=120) as client:
        specs = specifications(
            client, args.recipe, reprocess=args.reprocess, margin_dn=args.margin_dn
        )
        for spec in specs:
            recipe = spec["recipe"]
            write(output / f"{recipe}-specification.json", spec)
            job = client.submit_reference_sensor_mode_study(spec)
            write(output / f"{recipe}-submitted.json", job)
            receipt = {"recipe": recipe, "id": job["id"], "status": job["status"]}
            receipts.append(receipt)
            write(output / "runs.json", receipts)
            print(f"{recipe}: {job['id']} · {job['status']}", flush=True)
            try:
                done = client.wait_reference_workshop(
                    job["id"], timeout=args.timeout, poll_interval=2
                )
            except TimeoutError as error:
                receipt["wait_status"] = "timed_out_server_job_may_still_be_active"
                write(output / "runs.json", receipts)
                raise SystemExit(str(error)) from error
            write(output / f"{recipe}-record.json", done)
            receipt["status"] = done["status"]
            if done["status"] != "succeeded":
                write(output / "runs.json", receipts)
                raise SystemExit(f"{recipe}: {done['status']}: {done.get('error')}")
            summary = done["result"]["summary"]
            write(output / f"{recipe}-result.json", summary)
            a, b = ("native", "charge_binning") if recipe == "binning" else ("lcg", "dcg")
            query = urlencode(
                {"sensor_study": job["id"], "field": "outer-ne", "sensor_a": a, "sensor_b": b}
            )
            receipt.update(
                {
                    "demo_url": args.base_url.rstrip("/") + "/reference-workshop.html?" + query,
                    "report_url": args.base_url.rstrip("/") + summary["report_url"],
                    "bundle_url": args.base_url.rstrip("/") + summary["bundle_url"],
                    "manifest_sha256": summary["manifest_sha256"],
                    "package_bytes": summary["package_bytes"],
                    "elapsed_seconds": summary["elapsed_seconds"],
                }
            )
            write(output / "runs.json", receipts)
            print(receipt["demo_url"], flush=True)
    write(output / "delivery.json", {"studies": receipts, "physical_validation": "not_established"})


if __name__ == "__main__":
    main()

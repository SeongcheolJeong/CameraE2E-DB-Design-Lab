#!/usr/bin/env python3
"""Prepare an actual optics/CFA/same-RAW ISP workshop using only the public SDK."""

import argparse
import copy
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", default="outputs/design-workshop-delivery-20261011/runs.json")
    parser.add_argument("--skip-focus", action="store_true")
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = json.loads(target.read_text()) if target.exists() else []
    with PlatformClient(args.base_url, timeout=120) as client:
        spec = client.design_workshop_catalog()["specification"]

        def finish(label, submit):
            existing = next((r for r in reversed(records) if r["label"] == label), None)
            if existing:
                record = client.design_workshop_run(existing["id"])
                if record["status"] == "succeeded":
                    print(label, record["id"], "already complete", flush=True)
                    return record
            job = submit()
            print(label, job["id"], flush=True)
            result = client.wait(job["id"], timeout=7260, poll_interval=3)
            records.append({"label": label, **result})
            target.write_text(json.dumps(records, indent=2))
            if result["status"] != "succeeded":
                raise RuntimeError(result.get("error") or result["status"])
            print(args.base_url + result["result"]["summary"]["report_url"], flush=True)
            return result

        baseline = finish("baseline", lambda: client.submit_design_workshop(spec))
        finish(
            "cfa-thickness",
            lambda: client.submit_design_sweep(
                spec,
                [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}],
                name="CFA thickness · fixed stack height",
            ),
        )
        if not args.skip_focus:
            finish(
                "through-focus",
                lambda: client.submit_design_sweep(
                    spec,
                    [{"path": "lens.controls.sensor_shift_um", "values": [-20, 0, 20]}],
                    name="Through-focus · actual prescription",
                ),
            )
        aperture = copy.deepcopy(spec)
        aperture["name"] = "Stop radius 0.65 mm · actual prescription"
        aperture["lens"]["controls"]["stop_radius_mm"] = 0.65
        finish("aperture", lambda: client.submit_design_workshop(aperture))
        for label, denoise, sharpen in [("low-noise", 0.85, 0), ("detail", 0, 1.2)]:
            tuning = copy.deepcopy(spec["isp"])
            tuning.update(denoise=denoise, sharpen=sharpen)
            finish(
                label,
                lambda tuning=tuning, label=label: client.replay_design_workshop(
                    baseline["id"], tuning, name="Same RAW · " + label
                ),
            )
        bright = copy.deepcopy(spec)
        bright.update(name="Bright field · lens and CFA baseline", luminance_cd_m2=10000)
        bright["isp"]["display_white_level"] = 10
        bright_baseline = finish("bright-baseline", lambda: client.submit_design_workshop(bright))
        finish(
            "bright-cfa",
            lambda: client.submit_design_sweep(
                bright,
                [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}],
                name="Bright field · CFA thickness · fixed ISP",
            ),
        )
        bright_aperture = copy.deepcopy(bright)
        bright_aperture["name"] = "Bright field · aperture0.65mm"
        bright_aperture["lens"]["controls"]["stop_radius_mm"] = 0.65
        bright_lens = finish(
            "bright-aperture", lambda: client.submit_design_workshop(bright_aperture)
        )
        print(
            args.base_url
            + "/design-workshop.html?run="
            + bright_lens["id"]
            + "&baseline="
            + bright_baseline["id"],
            flush=True,
        )
        print(args.base_url + "/design-workshop.html?run=" + baseline["id"], flush=True)


if __name__ == "__main__":
    main()

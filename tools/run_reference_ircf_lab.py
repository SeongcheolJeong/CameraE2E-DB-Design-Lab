#!/usr/bin/env python3
"""Run the IRCF demo through the public SDK; retain receipts and saved A/B profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

from camera_platform.sdk import PlatformClient


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--scope", choices=["first_pair", "full"], default="full")
    parser.add_argument("--step", type=int, choices=[5, 10], default=10)
    parser.add_argument("--nir-scale", type=float, choices=[0.5, 1, 2], default=1)
    parser.add_argument("--output", type=Path, default=Path("outputs/reference-ircf-lab-20261012"))
    args = parser.parse_args()
    key = f"{args.scope}-{args.step}nm-nir{args.nir_scale:g}"
    with PlatformClient(args.base_url, timeout=180) as client:
        spec = client.reference_ircf_studies()["template"]
        spec.update(
            name="Reference IRCF Design Lab · " + key,
            wavelength_step_nm=args.step,
            nir_scale=args.nir_scale,
        )
        if args.scope == "first_pair":
            spec.update(
                fields=["center"],
                charts=["color"],
                illuminants=["day"],
                filters=["baseline", "nir_leak_1pct"],
                include_angle_diagnostic=False,
            )
        job = client.submit_reference_ircf_study(spec)
        write(args.output / f"{key}-submitted.json", job)
        print(job["id"], job["status"], flush=True)
        done = client.wait_reference_workshop(job["id"], poll_interval=3)
        write(args.output / f"{key}-record.json", done)
        if done["status"] != "succeeded":
            raise RuntimeError(f"{done['status']}: {done.get('error')}")
        summary = done["result"]["summary"]
        profiles = []
        for light in spec["illuminants"]:
            cases = [
                c
                for c in summary["cases"]
                if c["chart"] == "color" and c["field_id"] == "center" and c["illuminant"] == light
            ]
            a = next(c for c in cases if c["filter_id"] == "baseline")
            b = next(c for c in cases if c["filter_id"] == "nir_leak_1pct")
            profile = client.save_reference_ircf_profile(
                job["id"],
                {
                    "name": "IRCF leak sensitivity · " + light,
                    "baseline_case_id": a["id"],
                    "case_id": b["id"],
                    "ccm_a": "baseline",
                    "ccm_b": "baseline",
                    "stage": "display",
                },
            )
            write(args.output / f"{key}-{light}-profile.json", profile)
            query = urlencode(
                {
                    "run": job["id"],
                    "case": a["id"],
                    "a": "baseline",
                    "b": "nir_leak_1pct",
                    "ccm_a": "baseline",
                    "ccm_b": "baseline",
                }
            )
            profiles.append(
                {
                    "illuminant": light,
                    "profile_id": profile["id"],
                    "url": args.base_url + "/reference-ircf.html?" + query,
                }
            )
        if args.scope == "full":
            for name, field, chart, filter_id, angles, ccms in (
                (
                    "Same RAW color recalibration",
                    "center",
                    "color",
                    "cutoff_plus20",
                    ("pupil", "pupil"),
                    ("baseline", "refit"),
                ),
                (
                    "Outer field incidence",
                    "outer-ne",
                    "uniform",
                    "baseline",
                    ("independent", "pupil"),
                    ("baseline", "baseline"),
                ),
            ):
                pair = [
                    next(
                        c
                        for c in summary["cases"]
                        if c["field_id"] == field
                        and c["chart"] == chart
                        and c["illuminant"] == "ill_a"
                        and c["filter_id"] == filter_id
                        and c["angular_mode"] == angle
                    )
                    for angle in angles
                ]
                profile = client.save_reference_ircf_profile(
                    job["id"],
                    {
                        "name": name,
                        "baseline_case_id": pair[0]["id"],
                        "case_id": pair[1]["id"],
                        "ccm_a": ccms[0],
                        "ccm_b": ccms[1],
                        "stage": "display",
                    },
                )
                query = urlencode(
                    {
                        "run": job["id"],
                        "case": pair[1]["id"],
                        "a": filter_id,
                        "b": filter_id,
                        "angle_a": angles[0],
                        "angle_b": angles[1],
                        "ccm_a": ccms[0],
                        "ccm_b": ccms[1],
                    }
                )
                profiles.append(
                    {
                        "name": name,
                        "profile_id": profile["id"],
                        "url": args.base_url + "/reference-ircf.html?" + query,
                    }
                )
                write(args.output / f"{key}-{filter_id}-profile.json", profile)
        delivery = {
            "id": job["id"],
            "profiles": profiles,
            "report_url": args.base_url + summary["report_url"],
            "bundle_url": args.base_url + summary["bundle_url"],
            "manifest_sha256": summary["manifest_sha256"],
            "new_raw_frames": summary["new_raw_frames"],
            "package_bytes": summary["package_bytes"],
            "elapsed_seconds": summary["elapsed_seconds"],
            "physical_validation": "not_established",
        }
        write(args.output / f"{key}-delivery.json", delivery)
        print(json.dumps(delivery, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

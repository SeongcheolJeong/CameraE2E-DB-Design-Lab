"""Prepare a source-pinned Ghost Lab or reprocess its preserved RAW."""

import argparse
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", default="outputs/reference-ghost-delivery-20261012")
    parser.add_argument("--angle", type=float)
    parser.add_argument("--reprocess")
    parser.add_argument("--white", type=float)
    parser.add_argument(
        "--probes-demo",
        action="store_true",
        help="Prepare black/dark charts at saved ghost peak and ring coordinates",
    )
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with PlatformClient(args.url, timeout=120) as client:
        if args.reprocess:
            spec = client.reference_ghost_study(args.reprocess)["ghost_study"]
            spec.update(
                name="Ghost Lab · preserved RAW display replay", reprocess_of=args.reprocess
            )
        else:
            spec = client.reference_ghost_studies()["template"]
            # First actual native comparison is prepared at15 degrees.
            spec["angles_deg"] = (
                [args.angle] if args.angle is not None else [15.0, 0.0, 5.0, 10.0, 20.0, 25.0, 30.0]
            )
        if args.probes_demo:
            if args.reprocess:
                raise ValueError("Choose either new probe acquisition or preserved RAW replay")
            spec.update(
                name="Ghost chart lab · black / dark contrast / selected native probes",
                angles_deg=[15.0],
                coatings=["r005"],
                fields=["center"],
                probes=[
                    {"id": "probe-peak", "xy_mm": [6.7499609375, 6.8033203125]},
                    {"id": "probe-ring", "xy_mm": [-5.8962109375, -5.8962109375]},
                ],
                charts=["black", "dark_contrast"],
                irradiance_w_m2=[1.0, 10.0],
            )
            spec["isp"].update(lsc_enabled=False, display_white_level=0.05)
        if args.white is not None:
            spec["isp"]["display_white_level"] = args.white
        (out / "specification.json").write_text(json.dumps(spec, indent=2))
        job = client.submit_reference_ghost_study(spec)
        (out / "submitted.json").write_text(json.dumps(job, indent=2))
        print(job["id"], flush=True)
        result = client.wait_reference_workshop(job["id"], timeout=14500, poll_interval=3)
        (out / "record.json").write_text(json.dumps(result, indent=2))
        if result["status"] != "succeeded":
            raise RuntimeError(result.get("error") or result["status"])
        summary = result["result"]["summary"]
        (out / "delivery.json").write_text(
            json.dumps(
                {
                    "run_id": job["id"],
                    "url": args.url + "/reference-ghost.html?run=" + job["id"],
                    "cases": len(summary["cases"]),
                    "seconds": summary["elapsed_seconds"],
                    "bytes": summary["package_bytes"],
                    "refinement": summary["refinement"],
                },
                indent=2,
            )
        )
        print(args.url + "/reference-ghost.html?run=" + job["id"], flush=True)


if __name__ == "__main__":
    main()

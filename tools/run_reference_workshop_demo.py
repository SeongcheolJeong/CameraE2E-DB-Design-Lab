"""Prepare pinned Reference bench comparisons through the public SDK."""

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--output", default="outputs/reference-workshop-delivery-20261011/runs.json"
    )
    parser.add_argument(
        "--lens-sweep", action="store_true", help="Also execute the prepared nine-lens grid."
    )
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = json.loads(target.read_text()) if target.exists() else []
    with PlatformClient(args.base_url, timeout=120) as client:
        spec = client.reference_workshop_catalog()["specification"]

        def finish(label, submit):
            previous = next((r for r in reversed(records) if r["label"] == label), None)
            if previous:
                result = client.reference_workshop_run(previous["id"])
                if result["status"] == "succeeded":
                    print(label, result["id"], "already complete", flush=True)
                    return result
                if result["status"] in ("queued", "running"):
                    job = result
                else:
                    job = submit()
            else:
                job = submit()
            records.append({"label": label, **job})
            target.write_text(json.dumps(records, ensure_ascii=False, indent=2))
            print(label, job["id"], flush=True)
            result = client.wait_reference_workshop(job["id"], timeout=7260, poll_interval=5)
            records[-1] = {"label": label, **result}
            target.write_text(json.dumps(records, ensure_ascii=False, indent=2))
            if result["status"] != "succeeded":
                raise RuntimeError(result.get("error") or result["status"])
            summary = result["result"]["summary"]
            print(label, summary.get("successful_fields"), "fields", flush=True)
            return result

        baseline = finish("baseline", lambda: client.submit_reference_workshop(spec))
        lens = copy.deepcopy(spec)
        lens["name"] = "Reference · first curvature ×1.005"
        lens["lens"]["curvature_scale"] = 1.005
        finish("lens-curvature", lambda: client.submit_reference_workshop(lens))
        finish(
            "cfa",
            lambda: client.submit_reference_sweep(
                spec,
                [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}],
                name="Reference CFA thickness",
            ),
        )
        for label, denoise in [("lsc", 0), ("lsc-denoise", 0.85)]:
            isp = {**spec["isp"], "lsc_enabled": True, "denoise": denoise, "sharpen": 0}
            finish(
                label,
                lambda isp=isp, label=label: client.replay_reference_workshop(
                    baseline["id"], isp, name="Same RAW · " + label
                ),
            )
        low = copy.deepcopy(spec)
        low.update(name="Reference · 1000 cd/m²", luminance_cd_m2=1000)
        low["isp"]["display_white_level"] = 1
        low_baseline = finish("low-light", lambda: client.submit_reference_workshop(low))
        low_lens = copy.deepcopy(low)
        low_lens["name"] = "Reference · 1000 cd/m² · curvature ×1.005"
        low_lens["lens"]["curvature_scale"] = 1.005
        low_changed = finish(
            "low-light-lens", lambda: client.submit_reference_workshop(low_lens)
        )
        finish(
            "low-light-cfa",
            lambda: client.submit_reference_sweep(
                low,
                [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}],
                name="Reference · 1000 cd/m² · CFA thickness",
            ),
        )
        for label, denoise in [("low-light-lsc", 0), ("low-light-lsc-denoise", 0.85)]:
            isp = {**low["isp"], "lsc_enabled": True, "denoise": denoise, "sharpen": 0}
            finish(
                label,
                lambda isp=isp, label=label: client.replay_reference_workshop(
                    low_baseline["id"], isp, name="Same RAW · " + label
                ),
            )
        if args.lens_sweep:
            finish(
                "lens-grid",
                lambda: client.submit_reference_sweep(
                    low,
                    [
                        {"path": "lens.curvature_scale", "values": [0.995, 1, 1.005]},
                        {"path": "lens.sensor_shift_um", "values": [-20, 0, 20]},
                    ],
                    name="Reference curvature × sensor Z",
                ),
            )
        print(
            args.base_url
            + "/reference-workshop.html?run="
            + low_changed["id"]
            + "&baseline="
            + low_baseline["id"],
            flush=True,
        )
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("verify_reference_workshop_demo.py")),
            "--base-url", args.base_url,
            "--runs", str(target.resolve()),
            "--output", str(target.with_name("verification.json").resolve()),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()

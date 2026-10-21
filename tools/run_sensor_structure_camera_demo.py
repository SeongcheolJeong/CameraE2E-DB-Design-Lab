#!/usr/bin/env python3
"""Run the source-pinned OCL hypothesis demo without invoking Meep."""
import argparse
import json
from pathlib import Path
from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8785")
    parser.add_argument("--output", type=Path, default=Path("outputs/ocl-camera-demo-20261018"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with PlatformClient(args.base_url, timeout=120) as client:
        spec = client.sensor_structure_camera_template()["specification"]
        job = client.submit_sensor_structure_camera_study(spec)
        (args.output / "receipt.json").write_text(json.dumps(job, indent=2, ensure_ascii=False))
        print(job["id"], job["status"], flush=True)
        result = client.wait(job["id"], timeout=1800, poll_interval=3,
                             expected_kind="sensor_structure_camera_demo")
        (args.output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] != "succeeded":
            raise RuntimeError(str(result.get("error")))
        print(args.base_url + "/sensor-structure-camera.html?run=" + job["id"], flush=True)


if __name__ == "__main__":
    main()

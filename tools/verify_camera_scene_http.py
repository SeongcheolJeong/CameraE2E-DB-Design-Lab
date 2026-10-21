#!/usr/bin/env python3
"""Verify the shared synthetic scene workflow in an explicitly selected QA state."""

import argparse
import json
from pathlib import Path

from create_camera_scene_example import create_example

from camera_platform.sdk import PlatformClient


def verify(client, expected_state):
    assert expected_state.name.startswith("camera-platform-scene-qa")
    return create_example(client, expected_state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with PlatformClient(args.base_url) as client:
        result = verify(client, args.state_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "records": result["records"],
            }
        )
    )

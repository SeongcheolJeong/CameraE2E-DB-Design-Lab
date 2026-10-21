#!/usr/bin/env python3
"""Freeze, preflight, acquire, reprocess and evaluate a road HDR package."""

import argparse
import json
from pathlib import Path

from camera_platform.road_hdr_inputs import (
    bound_specification,
    freeze_first,
    freeze_sequences,
    resource_plan,
)
from camera_platform.road_hdr_study import run


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "action",
        choices=[
            "freeze-first",
            "freeze-sequences",
            "preflight",
            "first-photo",
            "acquire",
            "reprocess",
            "evaluate",
        ],
    )
    p.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--dataset-root", type=Path)
    p.add_argument("--version", default="v1.0-trainval")
    p.add_argument("--stage", choices=["first-photo", "full"], default="full")
    a = p.parse_args()
    if a.action == "freeze-first":
        result = freeze_first(a.workspace, a.package)
    elif a.action == "freeze-sequences":
        if a.dataset_root is None:
            p.error("--dataset-root required")
        result = freeze_sequences(a.workspace, a.dataset_root, a.package, version=a.version)
    elif a.action == "preflight":
        result = resource_plan(a.workspace, a.package, bound_specification(a.package)[0], a.stage)
    else:
        result = run(a.workspace, a.package, a.action, on_progress=lambda x: print(x, flush=True))
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

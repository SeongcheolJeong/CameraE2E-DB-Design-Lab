#!/usr/bin/env python3
"""Inventory, freeze, preflight, execute, verify or replay pedestrian studies."""

import argparse
import json
import shutil
from pathlib import Path

from camera_platform.pedestrian_braking_inputs import bound_specification, freeze_inputs, inventory
from camera_platform.pedestrian_braking_study import resource_plan, run_study, verify_package
from camera_platform.road_design_inputs import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "inventory",
            "freeze",
            "preflight",
            "first-frame-connection",
            "first-sequence",
            "run",
            "verify",
            "replay",
        ),
    )
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--version", default="v1.0-mini")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--stage", choices=("first-sequence", "full"), default="full")
    args = parser.parse_args()
    if (
        args.action in {"inventory", "freeze", "first-frame-connection"}
        and args.dataset_root is None
    ):
        parser.error("Input actions require --dataset-root.")
    if args.action == "inventory":
        audit = inventory(args.dataset_root, args.version)
        if args.output:
            write_json(args.output, audit)
        print(
            json.dumps(
                {
                    "risk_events": audit["risk_events"],
                    "negative_events": audit["negative_events"],
                    "excluded": len(audit["excluded"]),
                }
            )
        )
        return
    if args.action == "first-frame-connection":
        from camera_platform.pedestrian_braking_connection import run_connection

        if args.output is None:
            parser.error("Connection requires a new --output directory.")
        print(
            json.dumps(
                run_connection(args.workspace, args.dataset_root, args.output), ensure_ascii=False
            )
        )
        return
    if args.package is None:
        parser.error("--package is required.")
    package = args.package.resolve()
    if args.action == "freeze":
        spec = freeze_inputs(args.workspace, args.dataset_root, package, version=args.version)
        result = {"status": "frozen", "events": len(spec["events"])}
    elif args.action == "verify":
        result = verify_package(package)
    elif args.action == "preflight":
        spec, _ = bound_specification(package)
        result = resource_plan(args.workspace, package, spec, stage=args.stage)
        write_json(package / "resource-plan.json", result)
    else:
        if args.action == "replay":
            if args.destination is None or args.destination.exists():
                parser.error("Replay requires a new --destination directory.")
            bound_specification(package)
            args.destination.mkdir(parents=True)
            for name in ("specification.json", "specification-binding.json"):
                shutil.copyfile(package / name, args.destination / name)
            shutil.copytree(package / "inputs", args.destination / "inputs")
            package = args.destination.resolve()
        result = run_study(
            args.workspace,
            package,
            stage="first-sequence" if args.action == "first-sequence" else args.stage,
            on_progress=lambda message: print(message, flush=True),
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

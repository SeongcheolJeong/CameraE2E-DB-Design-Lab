#!/usr/bin/env python3
"""One entry point for freeze, first-photo, complete study, resume and verify."""

import argparse
import json
import shutil
from pathlib import Path

from camera_platform.road_design_inputs import freeze_study
from camera_platform.road_design_study import (
    bound_specification,
    json_load,
    run_study,
    verify_package,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("freeze", "first-photo", "run", "verify", "replay", "review")
    )
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--destination", type=Path, help="Fresh directory for a calculation replay")
    parser.add_argument("--stage", choices=("first-photo", "full"), default="full")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    package = (args.package if args.package.is_absolute() else workspace / args.package).resolve()
    if args.action == "freeze":
        if args.dataset_root is None:
            parser.error("freeze requires --dataset-root")
        result = freeze_study(workspace, args.dataset_root.resolve(), package)
        print(
            json.dumps(
                {
                    "status": "frozen",
                    "images": len(result["images"]),
                    "candidates": len(result["candidates"]),
                }
            )
        )
    elif args.action == "verify":
        print(json.dumps(verify_package(package)))
    else:
        if args.action == "review":
            import os

            from camera_platform.road_design_inputs import write_json
            from camera_platform.road_design_report import build_report
            from camera_platform.road_rgb_source import file_sha256

            if args.destination is None:
                parser.error("review requires --destination for a new immutable report revision")
            destination = (
                args.destination if args.destination.is_absolute() else workspace / args.destination
            ).resolve()
            if destination.exists():
                parser.error("review destination exists; the published source stays unchanged")
            verify_package(package)

            def copy_artifact(source, target):
                # Large arrays remain immutable and can share storage. All
                # reviewable metadata and images get separate copies.
                return (
                    os.link(source, target)
                    if Path(source).suffix == ".npy"
                    else shutil.copy2(source, target)
                )

            shutil.copytree(
                package,
                destination,
                copy_function=copy_artifact,
                ignore=shutil.ignore_patterns(
                    "*.tmp*",
                    ".execution.lock",
                    "progress.json",
                    "package-manifest.json",
                    "delivery-implementation.json",
                ),
            )
            write_json(
                destination / "review-revision.json",
                {
                    "parent_package": str(package.relative_to(workspace))
                    if package.is_relative_to(workspace)
                    else str(package),
                    "parent_manifest_sha256": file_sha256(package / "package-manifest.json"),
                    "selection_and_calculations_changed": False,
                    "purpose": (
                        "Expose failed contrast conditions and area/pixel-count costs; "
                        "preserve the published parent."
                    ),
                },
            )
            spec, _ = bound_specification(destination)
            build_report(
                destination,
                spec,
                json_load(destination / "decision.json"),
                json_load(destination / "selection-records.json"),
                json_load(destination / "evaluation-records.json"),
            )
            print(json.dumps(verify_package(destination)))
            return
        if args.action == "replay":
            if args.destination is None:
                parser.error("replay requires --destination for a fresh output directory")
            destination = (
                args.destination if args.destination.is_absolute() else workspace / args.destination
            ).resolve()
            if destination.exists():
                parser.error("replay destination already exists; choose a new directory")
            bound_specification(package)
            destination.mkdir(parents=True)
            for name in ("specification.json", "specification-binding.json"):
                shutil.copyfile(package / name, destination / name)
            shutil.copytree(package / "inputs", destination / "inputs")
            package = destination
        result = run_study(
            workspace,
            package,
            stage=args.stage
            if args.action == "replay"
            else ("first-photo" if args.action == "first-photo" else "full"),
            on_progress=lambda message: print(message, flush=True),
        )
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in ("status", "recommended_id", "elapsed_s_this_invocation")
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

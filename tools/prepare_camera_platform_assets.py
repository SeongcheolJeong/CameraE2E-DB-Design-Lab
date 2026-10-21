#!/usr/bin/env python3
"""Prepare the pinned complete ISETCam asset snapshot offline into a fresh directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from camera_runtime_assets import prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Fresh JSON preparation report")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        raise FileExistsError("Preparation report already exists")
    result = prepare(args.archive, args.destination)
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

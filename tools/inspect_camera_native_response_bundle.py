"""Read-only inspection of a source-pinned native angular response bundle.

Example: PYTHONPATH=src .venv-platform/bin/python \
    tools/inspect_camera_native_response_bundle.py /path/to/bundle \
    --lens-model /path/to/registered-lens-model.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from camera_platform.native_response_assets import inspect_native_response_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--lens-model", type=Path,
                        help="JSON Lens model with payload.psf and payload.directional_response")
    parser.add_argument("--output", type=Path, help="Save the inspection JSON")
    args = parser.parse_args()
    try:
        optical = {}
        if args.lens_model is not None:
            model = json.loads(args.lens_model.read_text())
            payload = model.get("payload", model)
            optical = {"psf": payload["psf"],
                       "directional_response": payload["directional_response"]}
        result = inspect_native_response_bundle(args.bundle, **optical)
    except (ValueError, KeyError, FileNotFoundError, OSError) as exc:
        result = {"schema": "camera_native_response_bundle_inspection_v1",
                  "status": "invalid", "reason": str(exc),
                  "physical_validation": "not_established",
                  "product_ranking_allowed": False}
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if result["status"] == "research_eligible" else 2


if __name__ == "__main__":
    raise SystemExit(main())

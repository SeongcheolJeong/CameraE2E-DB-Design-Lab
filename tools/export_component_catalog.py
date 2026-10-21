"""Export the component catalog, optionally filtered to one manufacturer."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pyisetcam.component_catalog import component_catalog_export  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Existing CameraE2E DB root")
    parser.add_argument("--output-dir", type=Path, help="Export destination directory")
    parser.add_argument("--manufacturer", help="Export only this manufacturer")
    parser.add_argument("--report", type=Path, help="Also save the export result as JSON")
    args = parser.parse_args(argv)

    try:
        result = component_catalog_export(
            root=args.root, output_dir=args.output_dir, manufacturer=args.manufacturer
        )
        failed = result.get("status") in {"failed", "error"} or bool(result.get("errors"))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        result = {"status": "failed", "error": str(exc)}
        failed = True

    text = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"Cannot write component catalog export report: {exc}", file=sys.stderr)
            failed = True
    print(text, end="")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

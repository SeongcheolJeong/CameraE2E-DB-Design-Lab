"""Build the additive lens/sensor catalog without changing the source databases."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pyisetcam.component_catalog import (  # noqa: E402
    build_component_catalog,
    component_catalog_validate,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Destination CameraE2E DB root")
    parser.add_argument("--lens-db", type=Path, help="Source lens SQLite database")
    parser.add_argument("--sensor-catalog", type=Path, help="Source sensor_catalog.json")
    parser.add_argument(
        "--sensor-simulations", type=Path, help="Source sensor simulation SQLite database"
    )
    parser.add_argument(
        "--verify-files", action="store_true",
        help="Also verify registered artifact files and hashes",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("reports/component_catalog/build.json"),
        help="Build and validation JSON report (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        summary = build_component_catalog(
            root=args.root,
            lens_db=args.lens_db,
            sensor_catalog=args.sensor_catalog,
            sensor_simulations=args.sensor_simulations,
        )
        validation = component_catalog_validate(root=args.root, verify_files=args.verify_files)
        payload = {"summary": summary, "validation": validation}
        failed = (
            validation.get("status") != "passed"
            or bool(validation.get("errors"))
            or summary.get("status") in {"failed", "error"}
            or bool(summary.get("errors"))
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        payload = {"status": "failed", "error": str(exc)}
        failed = True

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"Cannot write component catalog report: {exc}", file=sys.stderr)
        failed = True
    print(text, end="")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

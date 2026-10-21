#!/usr/bin/env python3
"""Normalize public lens files and add them to the component catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pyisetcam.component_catalog import component_catalog_validate  # noqa: E402
from pyisetcam.lens_sources import (  # noqa: E402
    import_lens_sources,
    register_lens_source_trace_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--root", type=Path, default=REPO_ROOT / "camerae2e_db")
    parser.add_argument("--legacy-db", type=Path)
    parser.add_argument("--raytrace-report", type=Path)
    parser.add_argument(
        "--resolve-glass",
        action="store_true",
        help="Resolve exact names in declared local opticalglass catalogs",
    )
    parser.add_argument(
        "--report", type=Path, default=REPO_ROOT / "reports/lens_source_expansion/import.json"
    )
    args = parser.parse_args()
    result = import_lens_sources(
        args.manifest, root=args.root, legacy_db=args.legacy_db, resolve_glass=args.resolve_glass
    )
    # Preserve the committed import result even if attaching optional diagnostics fails.
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.raytrace_report:
        result["raytrace_diagnostics"] = register_lens_source_trace_report(
            args.raytrace_report, result, root=args.root
        )
    result["catalog_validation"] = component_catalog_validate(root=args.root, verify_files=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in result.items()
        if key
        in {
            "added",
            "files_by_status",
            "files_by_source",
            "configurations_by_status",
            "legacy_publication_overlap_files",
            "legacy_example_overlap_files",
        }
    }
    summary["catalog_validation"] = result["catalog_validation"]["status"]
    summary["report"] = str(args.report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["catalog_validation"]["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

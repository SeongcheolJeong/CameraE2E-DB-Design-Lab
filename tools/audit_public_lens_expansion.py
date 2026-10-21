#!/usr/bin/env python3
"""Verify additive catalog expansion and export file/configuration counts separately."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyisetcam._catalog_store import file_hash  # noqa: E402
from pyisetcam.component_catalog import component_catalog_validate  # noqa: E402


def audit(baseline_path: Path, report_path: Path, reimport_path: Path, root: Path) -> dict:
    baseline = json.loads(baseline_path.read_text())
    report = json.loads(report_path.read_text())
    reimport = json.loads(reimport_path.read_text())
    with sqlite3.connect((root / "catalog.sqlite").as_uri() + "?mode=ro", uri=True) as con:
        con.execute("ATTACH DATABASE ? AS prior", (Path(baseline["backup"]).as_uri() + "?mode=ro",))
        before, after, missing = {}, {}, {}
        for table in baseline["counts"]:
            quoted = '"' + table.replace('"', '""') + '"'
            before[table] = con.execute(f"SELECT count(*) FROM prior.{quoted}").fetchone()[0]
            after[table] = con.execute(f"SELECT count(*) FROM main.{quoted}").fetchone()[0]
            missing[table] = con.execute(
                f"SELECT count(*) FROM (SELECT * FROM prior.{quoted} EXCEPT "
                f"SELECT * FROM main.{quoted})"
            ).fetchone()[0]
        families = dict(con.execute("SELECT family,count(*) FROM components GROUP BY family"))
        new_missing = con.execute(
            "SELECT count(*) FROM main.artifacts WHERE availability='missing' AND artifact_id "
            "NOT IN (SELECT artifact_id FROM prior.artifacts WHERE availability='missing')"
        ).fetchone()[0]
    legacy = {
        path: file_hash(ROOT / path) == expected
        for path, expected in baseline["legacy_sha256"].items()
    }
    prior_raw = {
        path: file_hash(root / path) == expected
        for path, expected in baseline["prior_raw_sha256"].items()
    }
    inputs_unchanged = all(file_hash(Path(p)) == h for p, h in report["inputs"].items())
    validation = component_catalog_validate(root=root, verify_files=True)
    records = defaultdict(list)
    for record in report["records"]:
        records[record["path"]].append(record)
    by_source = {}
    for source in report["files_by_source"]:
        files = [rows for rows in records.values() if rows[0]["source"] == source]
        flat = [record for rows in files for record in rows]
        by_source[source] = {
            "files": len(files),
            "new_files_since_baseline": sum(
                rows[0]["path"] not in baseline["prior_raw_sha256"] for rows in files
            ),
            "files_fully_normalized": sum(
                all(r["status"] == "normalized" for r in rows) for rows in files
            ),
            "files_with_normalized_configuration": sum(
                any(r["status"] == "normalized" for r in rows) for rows in files
            ),
            "configurations_by_status": dict(Counter(r["status"] for r in flat)),
            "represented_components": len(
                {r["component_id"] for r in flat if r.get("component_id")}
            ),
            "formats": dict(Counter(rows[0].get("format") for rows in files)),
        }
    hashes = defaultdict(set)
    for path, rows in records.items():
        hashes[rows[0]["sha256"]].add(path)
    result = {
        "schema": "camerae2e_lens_expansion_audit_v2",
        "before": before,
        "after": after,
        "added_since_backup": {t: after[t] - before[t] for t in before},
        "missing_or_changed_prior_rows": missing,
        "all_prior_rows_preserved": not any(missing.values()),
        "component_families_before": baseline["component_families"],
        "component_families_after": families,
        "idempotent_reimport_added": reimport["added"],
        "idempotent_reimport_all_tables": reimport.get("all_table_deltas", {}),
        "legacy_db_hashes_unchanged": legacy,
        "prior_raw_hashes_unchanged": all(prior_raw.values()),
        "prior_raw_files_checked": len(prior_raw),
        "input_manifests_unchanged": inputs_unchanged,
        "catalog_validation": validation,
        "new_missing_artifact_warnings": new_missing,
        "files_by_source": by_source,
        "files_by_status": report["files_by_status"],
        "configurations_by_status": report["configurations_by_status"],
        "raw_duplicate_groups": [sorted(paths) for paths in hashes.values() if len(paths) > 1],
        "same_publication_example_candidates": report["same_publication_example_candidates"],
        "raytrace_diagnostics": report.get("raytrace_diagnostics"),
        "corrected_prior_revisions": [
            {
                "path": record["path"],
                "configuration_index": record["configuration_index"],
                "prior_revision_ids": record["corrects_prior_revisions"],
                "corrected_revision_id": record["revision_id"],
            }
            for record in report["records"]
            if record.get("corrects_prior_revisions")
        ],
        "scope": "Structural normalization is not PSF/MTF or manufactured lens validation.",
    }
    result["status"] = (
        "passed"
        if (
            not any(missing.values())
            and not any(reimport["added"].values())
            and not any(reimport.get("all_table_deltas", {}).values())
            and all(legacy.values())
            and all(prior_raw.values())
            and inputs_unchanged
            and validation["status"] == "passed"
            and new_missing == 0
        )
        else "failed"
    )
    destination = report_path.parent
    (destination / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    fields = [
        "source",
        "path",
        "format",
        "sha256",
        "title",
        "manufacturer",
        "publication_number",
        "example_label",
        "status",
        "configurations",
        "normalized_configurations",
        "blocked_configurations",
        "component_ids",
        "issue_codes",
    ]
    with (destination / "inventory.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for rows in records.values():
            row = {key: rows[0].get(key) for key in fields[:8]}
            statuses = {r["status"] for r in rows}
            normalized = sum(r["status"] == "normalized" for r in rows)
            row.update(
                status=next(iter(statuses)) if len(statuses) == 1 else "partial",
                configurations=len(rows),
                normalized_configurations=normalized,
                blocked_configurations=len(rows) - normalized,
                component_ids=";".join(
                    sorted({r["component_id"] for r in rows if r.get("component_id")})
                ),
                issue_codes=";".join(
                    sorted(
                        {
                            i["code"]
                            for r in rows
                            for i in r.get("issues", [])
                            if i.get("severity") == "error"
                        }
                    )
                ),
            )
            writer.writerow(row)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--import-report", type=Path, required=True)
    parser.add_argument("--reimport-report", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT / "camerae2e_db")
    args = parser.parse_args()
    result = audit(args.baseline, args.import_report, args.reimport_report, args.root.resolve())
    print(
        json.dumps(
            {k: result[k] for k in ("status", "added_since_backup", "files_by_source")}, indent=2
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

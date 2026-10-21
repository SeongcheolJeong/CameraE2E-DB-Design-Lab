#!/usr/bin/env python3
"""Audit structural lens readiness; optionally persist validation and quarantine.

Without --write-db the SQLite databases are opened read-only. A passing result
is not approval of generated PSFs or image quality. Writes only add/update the
dedicated validation table and clear invalid rows' existing recommendations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyisetcam.lens_validation import VALIDATOR_VERSION, validate_lens_prescription  # noqa: E402


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_database(db_path: Path, *, write_db: bool = False) -> dict[str, Any]:
    """Validate every simulation using exactly its configured surface rows."""
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    before_hash = file_sha256(db_path)
    validated_at = datetime.now(UTC).isoformat()
    results: list[dict[str, Any]] = []
    surface_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            "SELECT * FROM lens_surfaces ORDER BY lens_id, configuration, surface_order"
        ):
            surface_groups[(row["lens_id"], row["configuration"])].append(dict(row))
        for row in con.execute("SELECT * FROM simulation_results ORDER BY simulation_id"):
            validation = validate_lens_prescription(
                surface_groups[(row["lens_id"], row["configuration"])],
                focal_length_mm=row["focal_length_mm"],
                f_number=row["f_number"],
                configuration=row["configuration"],
            )
            results.append(
                {
                    "simulation_id": row["simulation_id"],
                    "lens_id": row["lens_id"],
                    "configuration": row["configuration"],
                    "company": row["company"],
                    **validation,
                }
            )
    if file_sha256(db_path) != before_hash:
        raise RuntimeError(
            "Database changed while its validation snapshot was being read; retry the audit."
        )

    cleared = 0
    if write_db:
        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""
                CREATE TABLE IF NOT EXISTS prescription_validation (
                    simulation_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    validator_version TEXT NOT NULL,
                    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
                    prescription_sha256 TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    FOREIGN KEY(simulation_id) REFERENCES simulation_results(simulation_id)
                )
            """)
            con.executemany(
                """
                INSERT INTO prescription_validation (
                    simulation_id, schema_version, validator_version, valid,
                    prescription_sha256, validated_at, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(simulation_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    validator_version=excluded.validator_version,
                    valid=excluded.valid,
                    prescription_sha256=excluded.prescription_sha256,
                    validated_at=excluded.validated_at,
                    result_json=excluded.result_json
            """,
                [
                    (
                        row["simulation_id"],
                        row["schema_version"],
                        row["validator_version"],
                        int(row["valid"]),
                        row["prescription_sha256"],
                        validated_at,
                        json.dumps(row, sort_keys=True, allow_nan=False),
                    )
                    for row in results
                ],
            )
            # Never promote a row: a structurally valid prescription still needs
            # independently validated, current PSF/radiometry assets.
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='camerae2e_quality'"
            ).fetchone():
                cursor = con.executemany(
                    """
                    UPDATE camerae2e_quality SET recommended_for_camerae2e=0
                    WHERE simulation_id=? AND recommended_for_camerae2e!=0
                """,
                    [(row["simulation_id"],) for row in results if not row["valid"]],
                )
                cleared = cursor.rowcount
    issue_counts = Counter(item["code"] for row in results for item in row["issues"])
    error_rows = Counter(
        code
        for row in results
        for code in {item["code"] for item in row["issues"] if item["severity"] == "error"}
    )
    return {
        "schema": "lens_prescription_validation_report_v1",
        "schema_version": 1,
        "validator_version": VALIDATOR_VERSION,
        "validation_scope": "prescription_structure_only",
        "image_quality_validated": False,
        "validated_at": validated_at,
        "database": str(db_path),
        "database_sha256_before": before_hash,
        "database_sha256_after": file_sha256(db_path),
        "database_written": write_db,
        "summary": {
            "simulation_rows": len(results),
            "valid_structure": sum(row["valid"] for row in results),
            "invalid_structure": sum(not row["valid"] for row in results),
            "recommendations_cleared": cleared,
            "issue_counts": dict(sorted(issue_counts.items())),
            "error_row_counts": dict(sorted(error_rows.items())),
        },
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        "--db-path",
        type=Path,
        action="append",
        help="SQLite path; repeat for multiple DBs.",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Write a versioned JSON audit report here."
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Persist validation and clear invalid recommendations; never promote rows.",
    )
    args = parser.parse_args(argv)
    paths = args.db or [
        ROOT / "src/pyisetcam/data/lens_patents/lens_patent_simulation_v6.sqlite",
        ROOT
        / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents"
        / "lens_patent_simulation_v9.sqlite",
    ]
    if args.output.expanduser().resolve() in {path.expanduser().resolve() for path in paths}:
        parser.error("The report output must not overwrite an input SQLite database.")
    reports = [audit_database(path, write_db=args.write_db) for path in paths]
    document = {
        "schema": "lens_prescription_validation_batch_v1",
        "schema_version": 1,
        "validator_version": VALIDATOR_VERSION,
        "validation_scope": "prescription_structure_only",
        "image_quality_validated": False,
        "databases": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")
    for report in reports:
        print(json.dumps({"database": report["database"], **report["summary"]}, sort_keys=True))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

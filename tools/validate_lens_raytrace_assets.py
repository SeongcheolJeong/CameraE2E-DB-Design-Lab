#!/usr/bin/env python3
"""Audit current lens LUT contracts and optionally synchronize preview recommendations.

Contract acceptance means a usable geometric preview, not calibrated radiometry,
sampling convergence, diffraction accuracy, or validated camera image quality.
Databases stay read-only unless --write-db is explicitly supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyisetcam.lens_patents import lens_patent_raytrace_optics  # noqa: E402

VALIDATOR_VERSION = "1.0.0"
SCHEMA_VERSION = 1
RECOMMENDED_SCOPE = "geometric_preview_not_calibrated_IQ"
ASSET_DIRECTORIES = ("raytrace_psf", "raytrace_psf_highres", "raytrace_psf_v2")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_asset_root(db_path: Path, asset_root: Path | None = None) -> Path:
    """Company partitions share the nearest ancestor's named lens asset folders."""
    if asset_root is not None:
        root = asset_root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(root)
        return root
    for parent in db_path.parents:
        if any((parent / name).is_dir() for name in ASSET_DIRECTORIES):
            return parent
    return db_path.parent


def _sampling_status(raytrace: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    quality = raytrace.get("quality", {})
    quality = quality if isinstance(quality, dict) else {}
    keys = (
        "sampling_validated_for_sensor",
        "sensor_pitch_um",
        "sample_spacing_um",
        "pupil_sampling_convergence_validated",
        "spectral_sampling_convergence_validated",
        "min_retained_energy_fraction",
    )
    details = {key: quality[key] for key in keys if key in quality}
    if quality.get("sampling_validated_for_sensor") is True:
        status = "sensor_pitch_checked_convergence_unverified"
        if (
            quality.get("pupil_sampling_convergence_validated") is True
            and quality.get("spectral_sampling_convergence_validated") is True
        ):
            status = "sensor_pitch_and_declared_convergence_checked"
    else:
        status = "unvalidated"
    return status, details


def _failure_code(error: Exception) -> str:
    message = str(error).lower()
    if "legacy psf" in message:
        return "legacy_contract"
    if "fingerprint mismatch" in message:
        return "source_prescription_mismatch"
    if "invalid lens prescription" in message:
        return "invalid_prescription"
    if "identity mismatch" in message:
        return "asset_identity_mismatch"
    if isinstance(error, FileNotFoundError):
        return "missing_asset"
    return "invalid_contract"


def _audit_row(
    db_path: Path,
    root: Path,
    folder: Path,
    row: dict[str, Any],
    index: int,
    duplicate_ids: set[str],
) -> dict[str, Any]:
    simulation_id = str(row["simulation_id"])
    file_name = str(row.get("file") or f"@manifest-row-{index:06d}")
    path = (folder / file_name).resolve()
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = f"{folder.name}/@outside-root-row-{index:06d}"
    result: dict[str, Any] = {
        "simulation_id": simulation_id,
        "asset_relative_path": relative_path,
        "manifest_status": str(row.get("status", "unknown")),
        "contract_version": None,
        "contract_sha256": None,
        "source_prescription_sha256": None,
        "asset_sha256": None,
        "valid_contract": False,
        "reason_code": "invalid_contract",
        "radiometry_status": "unknown",
        "sampling_status": "unvalidated",
        "sampling_details": {},
        "image_quality_validated": False,
        "recommended_scope": RECOMMENDED_SCOPE,
    }
    if not path.is_relative_to(folder.resolve()):
        result.update(
            reason_code="asset_path_outside_folder", error="Manifest file escapes its folder."
        )
        return result
    if simulation_id in duplicate_ids:
        result.update(
            reason_code="ambiguous_manifest_identity",
            error="Multiple generated rows share this simulation ID.",
        )
        return result
    if row.get("status") not in {"generated", "exists"}:
        result.update(
            reason_code="no_generated_asset", error="Manifest does not declare a generated asset."
        )
        return result
    if not path.is_file():
        result.update(reason_code="missing_asset", error="Manifest asset file does not exist.")
        return result
    try:
        result["asset_sha256"] = file_sha256(path)
        with np.load(path, allow_pickle=False) as data:
            metadata = json.loads(str(data["optics_json"]))
        raytrace = metadata.get("raytrace", {})
        contract_version = int(raytrace.get("contract_version", 0))
        source_hash = raytrace.get("source_prescription_sha256")
        if source_hash is not None and not isinstance(source_hash, str):
            raise ValueError("Source prescription hash must be a string")
        sampling_status, sampling_details = _sampling_status(raytrace)
        json.dumps(sampling_details, allow_nan=False)
        result.update(
            contract_version=contract_version,
            contract_sha256=hashlib.sha256(
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            source_prescription_sha256=source_hash,
            radiometry_status=str(raytrace.get("radiometry_status", "unknown")),
            sampling_status=sampling_status,
            sampling_details=sampling_details,
        )
    except Exception as error:
        result.update(reason_code="corrupt_asset", error=f"{type(error).__name__}: {error}")
        return result

    try:
        # Do not pass allow_legacy=True: this audit must enforce the default loader.
        optics = lens_patent_raytrace_optics(
            simulation_id,
            psf_dir=folder,
            db_path=db_path,
            allow_approximate_radiometry=True,
        )
        loaded = optics["raytrace"]
        if loaded.get("asset_sha256") != result["asset_sha256"]:
            raise ValueError("Loader selected another asset or file changed during validation")
        if file_sha256(path) != result["asset_sha256"]:
            raise ValueError("Asset changed during validation")
        result.update(
            valid_contract=True,
            reason_code="valid_geometric_contract",
            radiometry_status=str(loaded.get("radiometry_status", "unknown")),
            approximate_radiometry_accepted=bool(
                loaded.get("radiometry_assumption_accepted", False)
            ),
            psf_shape=list(np.asarray(loaded["psf"]["function"]).shape),
            wavelength_nm=np.asarray(loaded["psf"]["wavelength_nm"]).tolist(),
        )
    except Exception as error:
        result.update(
            reason_code="legacy_contract" if contract_version != 2 else _failure_code(error),
            error=f"{type(error).__name__}: {error}",
        )
    return result


def _persist(
    db_path: Path, results: list[dict[str, Any]], validated_at: str, before_hash: str
) -> dict:
    if file_sha256(db_path) != before_hash:
        raise RuntimeError("Database changed during asset validation; rerun the audit.")
    valid_ids = sorted({row["simulation_id"] for row in results if row["valid_contract"]})
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""
            CREATE TABLE IF NOT EXISTS raytrace_asset_validation (
                simulation_id TEXT NOT NULL,
                asset_relative_path TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                validator_version TEXT NOT NULL,
                contract_version INTEGER,
                contract_sha256 TEXT,
                source_prescription_sha256 TEXT,
                asset_sha256 TEXT,
                valid_contract INTEGER NOT NULL CHECK(valid_contract IN (0, 1)),
                radiometry_status TEXT NOT NULL,
                sampling_status TEXT NOT NULL,
                validated_at TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (simulation_id, asset_relative_path)
            )
        """)
        # Replace this database's audit snapshot so removed files cannot leave
        # a stale successful row behind. No historic availability tier is changed.
        con.execute("DELETE FROM raytrace_asset_validation")
        con.executemany(
            "INSERT INTO raytrace_asset_validation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["simulation_id"],
                    row["asset_relative_path"],
                    SCHEMA_VERSION,
                    VALIDATOR_VERSION,
                    row["contract_version"],
                    row["contract_sha256"],
                    row["source_prescription_sha256"],
                    row["asset_sha256"],
                    int(row["valid_contract"]),
                    row["radiometry_status"],
                    row["sampling_status"],
                    validated_at,
                    json.dumps(row, sort_keys=True, allow_nan=False),
                )
                for row in results
            ],
        )
        quality_exists = (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='camerae2e_quality'"
            ).fetchone()
            is not None
        )
        before_ids: set[str] = set()
        after_ids: set[str] = set()
        if quality_exists:
            before_ids = {
                r[0]
                for r in con.execute(
                    "SELECT simulation_id FROM camerae2e_quality WHERE recommended_for_camerae2e!=0"
                )
            }
            con.execute("UPDATE camerae2e_quality SET recommended_for_camerae2e=0")
            con.executemany(
                "UPDATE camerae2e_quality SET recommended_for_camerae2e=1 WHERE simulation_id=?",
                [(simulation_id,) for simulation_id in valid_ids],
            )
            after_ids = {
                r[0]
                for r in con.execute(
                    "SELECT simulation_id FROM camerae2e_quality WHERE recommended_for_camerae2e=1"
                )
            }
        con.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES ('recommended_scope',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (RECOMMENDED_SCOPE,),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES ('raytrace_asset_validation',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                json.dumps(
                    {
                        "validator_version": VALIDATOR_VERSION,
                        "validated_at": validated_at,
                        "scope": RECOMMENDED_SCOPE,
                        "image_quality_validated": False,
                        "valid_contract_assets": sum(row["valid_contract"] for row in results),
                    }
                ),
            ),
        )
    return {
        "quality_table_present": quality_exists,
        "recommendations_cleared": len(before_ids - after_ids),
        "recommendations_enabled": len(after_ids - before_ids),
        "recommended_simulations": len(after_ids),
    }


def audit_database(
    db_path: str | Path,
    output_path: str | Path | None = None,
    *,
    write_db: bool = False,
    asset_root: str | Path | None = None,
) -> dict[str, Any]:
    """Audit all three named asset directories for simulations present in this DB."""
    db = Path(db_path).expanduser().resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    if output_path is not None and Path(output_path).expanduser().resolve() == db:
        raise ValueError("The output report must not overwrite its input database.")
    root = resolve_asset_root(db, None if asset_root is None else Path(asset_root))
    before_hash = file_sha256(db)
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
        ids = {str(row[0]) for row in con.execute("SELECT simulation_id FROM simulation_results")}
    validated_at = datetime.now(UTC).isoformat()
    results: list[dict[str, Any]] = []
    manifest_errors: list[dict[str, str]] = []
    manifests: list[dict[str, str]] = []
    ignored_rows = 0
    for directory in ASSET_DIRECTORIES:
        folder = root / directory
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest_hash = file_sha256(manifest_path)
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))["rows"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Manifest rows must be an array of objects")
        except Exception as error:
            manifest_errors.append({"manifest": str(manifest_path), "error": str(error)})
            continue
        manifests.append({"path": str(manifest_path), "sha256": manifest_hash})
        counts = Counter(
            str(row.get("simulation_id"))
            for row in rows
            if row.get("status") in {"generated", "exists"}
        )
        duplicates = {identity for identity, count in counts.items() if count > 1}
        for index, row in enumerate(rows):
            if str(row.get("simulation_id")) not in ids:
                ignored_rows += 1
                continue
            results.append(_audit_row(db, root, folder, row, index, duplicates))
        if file_sha256(manifest_path) != manifest_hash:
            raise RuntimeError("Manifest changed during asset validation; rerun the audit.")
    if file_sha256(db) != before_hash:
        raise RuntimeError("Database changed during asset validation; rerun the audit.")
    # Multiple identical manifest rows may resolve to the same primary key.
    results = list({(r["simulation_id"], r["asset_relative_path"]): r for r in results}.values())
    recommendations = _persist(db, results, validated_at, before_hash) if write_db else {}
    report = {
        "schema": "lens_raytrace_asset_validation_report_v1",
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "validation_scope": RECOMMENDED_SCOPE,
        "image_quality_validated": False,
        "validated_at": validated_at,
        "database": str(db),
        "asset_root": str(root),
        "database_written": write_db,
        "database_sha256_before": before_hash,
        "database_sha256_after": file_sha256(db),
        "manifests": manifests,
        "manifest_errors": manifest_errors,
        "summary": {
            "simulation_rows": len(ids),
            "asset_rows": len(results),
            "valid_contract_assets": sum(r["valid_contract"] for r in results),
            "invalid_contract_assets": sum(not r["valid_contract"] for r in results),
            "valid_contract_simulations": len(
                {r["simulation_id"] for r in results if r["valid_contract"]}
            ),
            "ignored_other_database_rows": ignored_rows,
            "reason_counts": dict(sorted(Counter(r["reason_code"] for r in results).items())),
            **recommendations,
        },
        "results": results,
    }
    if output_path is not None:
        output = Path(output_path).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--company",
        type=Path,
        action="append",
        default=[],
        metavar="DB",
        help="Also audit a company partition; repeat for additional DBs.",
    )
    parser.add_argument(
        "--asset-root",
        type=Path,
        help="Shared parent of raytrace_psf, raytrace_psf_highres and raytrace_psf_v2.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args(argv)
    paths = list(dict.fromkeys(path.expanduser().resolve() for path in [args.db, *args.company]))
    if args.output.expanduser().resolve() in paths:
        parser.error("The output report must not overwrite an input database.")
    reports = [
        audit_database(path, write_db=args.write_db, asset_root=args.asset_root) for path in paths
    ]
    payload = (
        reports[0]
        if len(reports) == 1
        else {
            "schema": "lens_raytrace_asset_validation_batch_v1",
            "schema_version": SCHEMA_VERSION,
            "validation_scope": RECOMMENDED_SCOPE,
            "image_quality_validated": False,
            "databases": reports,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    for report in reports:
        print(json.dumps({"database": report["database"], **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

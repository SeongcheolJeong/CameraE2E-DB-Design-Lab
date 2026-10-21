"""Add public optical prescriptions to the existing component catalog.

Raw downloads, transcription quality, and optical validation are independent.
Importing a design never creates a PSF or certifies a manufactured product.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from ._catalog_store import CatalogWriter, connect, digest, file_hash, json_text
from .lens_source_identity import optical_fingerprint
from .lens_validation import validate_lens_prescription


def publication_key(value: Any) -> str | None:
    """Normalize a publication identifier, not a patent-family relationship."""
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    match = re.fullmatch(r"([A-Z]{2})(\d+?)(?:[A-Z]\d?)?", text)
    if not match:
        return None
    return match[1] + str(int(match[2]))


def example_key(value: Any) -> str | None:
    match = re.search(r"(?:example|embodiment|ex)\s*0*(\d+)", str(value or ""), re.I)
    if not match and str(value or "").isdigit():
        return str(int(value))
    return str(int(match[1])) if match else None


def _legacy_index(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if path is None:
        return index
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            "SELECT lens_id,publication_number,example_label,company FROM lenses"
        ):
            key = publication_key(row["publication_number"])
            if key:
                index[key].append(dict(row))
    return index


def _source_path(manifest: Path, entry: dict[str, Any]) -> Path:
    relative = Path(entry["path"])
    path = (manifest.parent / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(manifest.parent.resolve()):
        raise ValueError(f"Source file must stay inside its manifest directory: {relative}")
    if not path.is_file():
        raise FileNotFoundError(path)
    expected = entry.get("sha256")
    if not expected or file_hash(path) != expected:
        raise ValueError(f"Source hash missing or mismatched: {relative}")
    return path


def _freeze_source(root: Path, path: Path, sha256: str) -> Path:
    """Freeze raw bytes even when the acquisition path is already inside root."""
    destination = root / "artifacts" / "lens_sources" / sha256[:2] / (sha256 + path.suffix.lower())
    if destination.exists():
        if file_hash(destination) != sha256:
            raise ValueError(f"Immutable lens source is corrupt: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(path, temporary)
        if file_hash(temporary) != sha256:
            raise ValueError(f"Source changed while copying immutable bytes: {path}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _identity(entry: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    metadata = parsed.get("metadata", {})
    title = entry.get("title") or metadata.get("title") or Path(entry["path"]).stem
    publication = entry.get("publication_number") or entry.get("patent")
    example = entry.get("example_label") or entry.get("example")
    if not publication:
        match = re.search(r"\b([A-Z]{2}[\d-]{5,}(?:[ABC]\d)?)", str(title), re.I)
        publication = match[1] if match else None
    if not publication:
        match = re.search(r"([A-Z]{2}\d{5,}(?:[ABC]\d)?)", Path(entry["path"]).stem, re.I)
        publication = match[1] if match else None
    return {
        "title": title,
        "manufacturer": entry.get("manufacturer")
        or entry.get("company")
        or entry.get("brand_from_title"),
        "publication_number": publication,
        "publication_key": publication_key(publication),
        "example_label": example,
        "example_key": example_key(example),
    }


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("components", "model_revisions", "lens_surfaces", "sources", "artifacts")
    }


def _flag_prior_aperture_errors(writer, component, source_id, revision, parsed):
    """Retain history but flag old same-source configurations with a wrong AS diameter."""
    if parsed.get("format") != "photons_to_photos" or parsed.get("status") != "parsed":
        return []
    expected = {
        row["surface_key"]: row["effective_aperture_mm"]
        for row in parsed["surfaces"]
        if row.get("aperture_basis") == "configuration Aperture Diameter; base stopdown=0"
    }
    if not expected:
        return []
    corrected = []
    for prior in writer.conn.execute(
        "SELECT revision_id,snapshot_json FROM model_revisions "
        "WHERE component_id=? AND source_id=? AND revision_id<>?",
        (component, source_id, revision),
    ).fetchall():
        snapshot = json.loads(prior["snapshot_json"])
        if snapshot.get("specifications", {}).get("configuration_name") != parsed["metadata"].get(
            "configuration_name"
        ):
            continue
        differences = [
            {
                "surface_key": row["surface_key"],
                "prior_diameter_mm": row.get("effective_aperture_mm"),
                "source_configuration_diameter_mm": expected[row["surface_key"]],
            }
            for row in snapshot.get("surfaces", [])
            if row.get("surface_key") in expected
            and row.get("effective_aperture_mm") != expected[row["surface_key"]]
        ]
        if differences:
            writer.validation(
                "revision",
                prior["revision_id"],
                "source_aperture_consistency",
                "failed",
                {
                    "reason": (
                        "Prior parser omitted configuration-specific OpticalBench AS diameter"
                    ),
                    "corrected_revision_id": revision,
                    "differences": differences,
                },
            )
            corrected.append(prior["revision_id"])
    return corrected


def import_lens_sources(
    manifests: list[str | Path],
    *,
    root: str | Path,
    legacy_db: str | Path | None = None,
    resolve_glass: bool = False,
) -> dict[str, Any]:
    """Import checked local download manifests, preserving existing catalog rows.

    Each manifest contains ``files`` with path relative to itself, sha256,
    source_url and format. Download errors stay in acquisition reports; malformed
    files are individually reported. Integrity failures abort the transaction.
    """
    from .lens_source_parser import parse_lens_configurations

    root = Path(root).expanduser().resolve()
    manifests = [Path(p).expanduser().resolve() for p in manifests]
    legacy_path = Path(legacy_db).expanduser().resolve() if legacy_db else None
    legacy_before = file_hash(legacy_path) if legacy_path else None
    legacy = _legacy_index(legacy_path)
    inputs = {str(p): file_hash(p) for p in manifests}
    source_inputs: dict[Path, str] = {}
    records: list[dict[str, Any]] = []
    source_fingerprints: dict[str, list[str]] = defaultdict(list)
    source_patents: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    with closing(connect(root / "catalog.sqlite", write=True)) as con, con:
        writer = CatalogWriter(con, root)
        before = _counts(con)
        for manifest in manifests:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            source_name = str(payload.get("source") or manifest.parent.name)
            if not isinstance(payload.get("files"), list):
                raise ValueError(f"Manifest has no files list: {manifest}")
            for entry in payload["files"]:
                if not entry.get("path"):
                    continue  # Failed acquisition entries are not downloads.
                path = _source_path(manifest, entry)
                source_inputs[path] = entry["sha256"]
                base_record: dict[str, Any] = {
                    "source": source_name,
                    "path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
                    "source_url": entry.get("source_url"),
                    "sha256": entry["sha256"],
                    "format": entry.get("format"),
                }
                source_id = writer.source(
                    "public_lens_prescription",
                    [source_name, entry.get("source_url") or entry["path"]],
                    uri=entry.get("source_url"),
                    sha256=entry["sha256"],
                    metadata={"download": entry, "source_name": source_name},
                )
                try:
                    variants = parse_lens_configurations(path, source_format=entry.get("format"))
                except (ValueError, UnicodeError, OSError, KeyError, IndexError) as exc:
                    record = dict(
                        base_record, status="parse_failed", error=f"{type(exc).__name__}: {exc}"
                    )
                    records.append(record)
                    record["source_artifact_id"] = writer.artifact(
                        _freeze_source(root, path, entry["sha256"]),
                        "unparsed_optical_prescription_source",
                        expected_sha256=entry["sha256"],
                        metadata={
                            "source_id": source_id,
                            "format": entry.get("format"),
                            "license": entry.get("license"),
                        },
                    )
                    writer.validation("source", source_id, "prescription_parse", "failed", record)
                    continue
                if resolve_glass:
                    from .lens_source_glass import resolve_catalog_glasses

                    variants = [resolve_catalog_glasses(v) for v in variants]
                variant_checks = [
                    validate_lens_prescription(
                        v.get("surfaces", []),
                        focal_length_mm=v.get("metadata", {}).get("focal_length_mm"),
                        f_number=v.get("metadata", {}).get("f_number"),
                        configuration=v.get("metadata", {}).get(
                            "configuration_name", "source_default"
                        ),
                    )
                    for v in variants
                ]
                complete_design = all(
                    v.get("status") == "parsed" and c["valid"]
                    for v, c in zip(variants, variant_checks, strict=True)
                )
                design_fingerprint = digest([optical_fingerprint(v) for v in variants])
                dedup_key = design_fingerprint if complete_design else "raw:" + entry["sha256"]
                source_alias = json_text([source_name, entry.get("source_url") or entry["path"]])
                for parsed, structural in zip(variants, variant_checks, strict=True):
                    record = dict(base_record)
                    records.append(record)
                    identity = _identity(entry, parsed)
                    record.update(identity)
                    surfaces = parsed.get("surfaces", [])
                    metadata = parsed.get("metadata", {})
                    record["configuration_index"] = metadata.get("configuration_index", 0)
                    record["configuration_name"] = metadata.get(
                        "configuration_name", "source_default"
                    )
                    record["configuration_ref"] = (
                        record["path"] + "#" + record["configuration_name"]
                    )
                    issues = parsed.get("issues", [])
                    parsed_ok = parsed.get("status") == "parsed" and not any(
                        issue.get("severity") == "error" for issue in issues
                    )
                    accepted = parsed_ok and structural["valid"]
                    record.update(
                        status="normalized" if accepted else "blocked",
                        parser_status=parsed.get("status"),
                        surface_count=len(surfaces),
                        issues=issues,
                        structural_validation=structural,
                        image_quality_validated=False,
                        raytrace_validated=False,
                    )
                    matches = legacy.get(identity["publication_key"], [])
                    record["legacy_publication_matches"] = matches
                    record["legacy_example_matches"] = [
                        row
                        for row in matches
                        if identity["example_key"] is not None
                        and example_key(row["example_label"]) == identity["example_key"]
                    ]
                    if identity["publication_key"]:
                        source_patents[
                            (identity["publication_key"], identity["example_key"])
                        ].append(record["path"])
                    if not surfaces:
                        record["source_artifact_id"] = writer.artifact(
                            _freeze_source(root, path, entry["sha256"]),
                            "unparsed_optical_prescription_source",
                            expected_sha256=entry["sha256"],
                            metadata={
                                "source_id": source_id,
                                "format": entry.get("format"),
                                "license": entry.get("license"),
                            },
                        )
                        writer.validation(
                            "source", source_id, "prescription_parse", "failed", record
                        )
                        continue
                    fingerprint = optical_fingerprint(parsed)
                    source_fingerprints[fingerprint].append(record["configuration_ref"])
                    # Source identity stays stable across changed bytes/parser versions.
                    # Complete identical designs from another source share a component;
                    # every explicit focus/zoom state remains a separate revision.
                    aliases = con.execute(
                        "SELECT component_id FROM aliases WHERE namespace=? AND alias=?",
                        ("public_lens_prescription:source", source_alias),
                    ).fetchall()
                    if not aliases:
                        aliases = con.execute(
                            "SELECT component_id FROM aliases WHERE namespace=? AND alias=?",
                            ("public_lens_prescription:design_fingerprint", dedup_key),
                        ).fetchall()
                    component = (
                        aliases[0][0]
                        if aliases
                        else writer.component(
                            "lens",
                            json_text({"public_prescription_source": source_alias}),
                            identity["manufacturer"],
                            identity["title"],
                            "published_optical_design",
                        )
                    )
                    writer.alias(component, "public_lens_prescription:source", source_alias)
                    writer.alias(
                        component, "public_lens_prescription:design_fingerprint", dedup_key
                    )
                    writer.alias(component, source_name + ":file", path.name)
                    writer.alias(component, source_name + ":title", identity["title"])
                    snapshot = {
                        "schema": "camerae2e_public_lens_snapshot_v1",
                        "parser_schema": parsed.get("schema"),
                        "format": parsed.get("format"),
                        "surfaces": surfaces,
                        "specifications": metadata,
                        "parse_issues": issues,
                        "structural_validation": structural,
                        "status": record["status"],
                        "provenance_scope": (
                            "published_design_not_confirmed_production_prescription"
                        ),
                    }
                    revision = writer.revision(
                        component, "public_optical_prescription", snapshot, source_id=source_id
                    )
                    record["corrects_prior_revisions"] = _flag_prior_aperture_errors(
                        writer, component, source_id, revision, parsed
                    )
                    point = writer.operating_point(
                        revision,
                        record["configuration_name"],
                        {
                            "configuration": record["configuration_name"],
                            "object_distance_mm": metadata.get("object_distance_mm"),
                            "wavelengths_nm": metadata.get("wavelengths_nm"),
                            "field_angles_deg": metadata.get("field_angles_deg"),
                            "source_settings": metadata,
                        },
                    )
                    writer.put(
                        "lens_configurations",
                        operating_point_id=point,
                        focal_length_mm=metadata.get("focal_length_mm"),
                        f_number=metadata.get("f_number"),
                        image_height_mm=metadata.get("image_height_mm"),
                        image_circle_mm=metadata.get("image_circle_mm"),
                        metadata_json=json_text(
                            {"source_settings": metadata, "status": record["status"]}
                        ),
                    )
                    for index, surface in enumerate(surfaces, 1):
                        writer.put(
                            "lens_surfaces",
                            revision_id=revision,
                            surface_order=index,
                            radius_mm=surface.get("radius_mm"),
                            thickness_mm=surface.get("thickness_mm"),
                            aperture_mm=surface.get("effective_aperture_mm"),
                            material=surface.get("material") or None,
                            nd=surface.get("nd"),
                            vd=surface.get("vd"),
                            conic=surface.get("conic"),
                            coefficients_json=json_text(surface.get("coefficients", {})),
                            metadata_json=json_text(
                                {"source_row": surface, "aperture_convention": "diameter"}
                            ),
                        )
                    writer.observation(
                        component,
                        "public_prescription_source",
                        identity,
                        revision_id=revision,
                        operating_point_id=point,
                        source_id=source_id,
                        source_locator={
                            "url": entry.get("source_url"),
                            "file_sha256": entry["sha256"],
                        },
                        extraction_method="explicit_optical_file_parser",
                    )
                    for key, unit in (
                        ("focal_length_mm", "mm"),
                        ("f_number", None),
                        ("image_height_mm", "mm"),
                    ):
                        writer.observation(
                            component,
                            key,
                            metadata.get(key),
                            unit=unit,
                            source_id=source_id,
                            revision_id=revision,
                            operating_point_id=point,
                            origin_kind="reported" if metadata.get(key) is not None else "unknown",
                            missing_reason="not_reported" if metadata.get(key) is None else None,
                            extraction_method="explicit_optical_file_parser",
                        )
                    writer.artifact(
                        _freeze_source(root, path, entry["sha256"]),
                        "optical_prescription_source",
                        revision_id=revision,
                        expected_sha256=entry["sha256"],
                        metadata={
                            "source_id": source_id,
                            "format": parsed.get("format"),
                            "license": entry.get("license"),
                        },
                    )
                    writer.validation(
                        "revision",
                        revision,
                        "public_prescription_structure",
                        "passed" if accepted else "failed",
                        {"parser_issues": issues, "structural_validation": structural},
                    )
                    writer.validation(
                        "revision",
                        revision,
                        "scientific_accuracy",
                        "unknown",
                        {
                            "image_quality_validated": False,
                            "raytrace_validated": False,
                            "reason": (
                                "Parsing does not verify source transcription, diffraction, "
                                "or manufactured lens accuracy."
                            ),
                        },
                    )
                    record.update(
                        component_id=component,
                        revision_id=revision,
                        operating_point_id=point,
                        optical_fingerprint=fingerprint,
                    )
        for manifest in manifests:
            if file_hash(manifest) != inputs[str(manifest)]:
                raise ValueError(f"Manifest changed during import: {manifest}")
        for source_path, expected_hash in source_inputs.items():
            if file_hash(source_path) != expected_hash:
                raise ValueError(f"Source file changed during import: {source_path}")
        if legacy_path and file_hash(legacy_path) != legacy_before:
            raise ValueError("Legacy lens DB changed during import; retry a consistent snapshot.")
        after = _counts(con)
    file_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        file_records[record["path"]].append(record)
    file_statuses = []
    for variants in file_records.values():
        statuses = {r["status"] for r in variants}
        file_statuses.append(next(iter(statuses)) if len(statuses) == 1 else "partial")
    return {
        "schema": "camerae2e_lens_source_import_v1",
        "catalog_path": str(root / "catalog.sqlite"),
        "inputs": inputs,
        "resolve_glass": resolve_glass,
        "importer_sha256": {
            name: file_hash(Path(__file__).with_name(name))
            for name in (
                "lens_sources.py",
                "lens_source_parser.py",
                "lens_source_json.py",
                "lens_source_glass.py",
                "lens_source_identity.py",
            )
            if Path(__file__).with_name(name).is_file()
        },
        "legacy_db": str(legacy_path) if legacy_path else None,
        "legacy_sha256": legacy_before,
        "legacy_unchanged": True,
        "counts_before": before,
        "counts_after": after,
        "added": {key: after[key] - before[key] for key in before},
        "files_by_status": dict(Counter(file_statuses)),
        "configurations_by_status": dict(Counter(r["status"] for r in records)),
        "files_by_source": dict(Counter(v[0]["source"] for v in file_records.values())),
        "exact_prescription_groups": [
            paths for paths in source_fingerprints.values() if len(paths) > 1
        ],
        "same_publication_example_candidates": [
            {"publication": key[0], "example": key[1], "paths": sorted(set(paths))}
            for key, paths in source_patents.items()
            if len(set(paths)) > 1
        ],
        "legacy_publication_overlap_files": sum(
            bool(v[0].get("legacy_publication_matches")) for v in file_records.values()
        ),
        "legacy_example_overlap_files": sum(
            bool(v[0].get("legacy_example_matches")) for v in file_records.values()
        ),
        "deduplication_scope": (
            "Exact parsed values and settings only; publication/example matches are review "
            "candidates, not automatic merges. Cross-country patent families are not inferred."
        ),
        "records": records,
    }


def register_lens_source_trace_report(
    report_path: str | Path, import_report: dict[str, Any], *, root: str | Path
) -> dict[str, Any]:
    """Attach reproducible diagnostic runs to the exact source configuration.

    The only new gate is geometric_raytrace_smoke. It never promotes the
    scientific_accuracy gate or installs any camera PSF/default model.
    """
    root = Path(root).expanduser().resolve()
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    expected_parser = file_hash(Path(__file__).with_name("lens_source_parser.py"))
    if report.get("parser_sha256") != expected_parser:
        raise ValueError("Raytrace report was not generated with the current source parser")
    dependencies = report.get("parser_dependencies_sha256", {})
    if any(case.get("format") in {"deeplens", "deeplens_json"} for case in report["cases"]):
        if "lens_source_json.py" not in dependencies:
            raise ValueError("Raytrace report is missing the JSON source parser hash")
    for name, expected in dependencies.items():
        if name != "lens_source_json.py" or file_hash(Path(__file__).with_name(name)) != expected:
            raise ValueError("Raytrace report source parser dependency hash does not match")
    registered = []
    with closing(connect(root / "catalog.sqlite", write=True)) as con, con:
        writer = CatalogWriter(con, root)
        source = writer.source(
            "public_lens_raytrace_diagnostic",
            report["schema"],
            sha256=file_hash(path),
            uri=path.as_uri(),
            metadata={
                "scope": report["scope"],
                "parser_sha256": expected_parser,
                "parser_dependencies_sha256": dependencies,
                "tool_sha256": report.get("tool_sha256"),
            },
        )
        for case in report["cases"]:
            matches = [
                r
                for r in import_report["records"]
                if r.get("sha256") == case.get("sha256")
                and r.get("configuration_index", 0) == (case.get("configuration_index") or 0)
                and r.get("revision_id")
            ]
            for record in matches:
                # Trust the hash-bound import record's source format, not an
                # optional/possibly incorrect label in the diagnostic case.
                if record.get("format") in {"deeplens", "deeplens_json"}:
                    current_json = file_hash(Path(__file__).with_name("lens_source_json.py"))
                    if dependencies.get("lens_source_json.py") != current_json:
                        raise ValueError("Matching JSON source requires its current parser hash")
                    if (
                        import_report.get("importer_sha256", {}).get("lens_source_json.py")
                        != current_json
                    ):
                        raise ValueError("Import report uses a different JSON source parser")
                source_path = Path(record["path"])
                if not source_path.is_absolute():
                    source_path = root / source_path
                if file_hash(source_path) != case["sha256"]:
                    raise ValueError("Raytrace report source hash no longer matches raw file")
                passed = (
                    case.get("normalized_raytrace", {}).get("status") == "passed"
                    and record["status"] == "normalized"
                )
                run = writer.run(
                    record["revision_id"],
                    record["operating_point_id"],
                    [
                        report["schema"],
                        case["sha256"],
                        report.get("tool_sha256"),
                        expected_parser,
                        dependencies,
                    ],
                    "passed" if passed else "blocked",
                    solver="RayOptics",
                    settings=report["settings"],
                    source_id=source,
                    input_sha256=case["sha256"],
                    metadata={
                        "scope": report["scope"],
                        "case": case,
                        "rayoptics_version": report["rayoptics_version"],
                    },
                )
                writer.artifact(
                    _freeze_source(root, path, file_hash(path)),
                    "geometric_raytrace_diagnostic",
                    run_id=run,
                    expected_sha256=file_hash(path),
                    metadata={"scope": report["scope"]},
                )
                writer.validation(
                    "run",
                    run,
                    "geometric_raytrace_smoke",
                    "passed" if passed else "unknown",
                    {"case": case, "scope": report["scope"], "image_quality_validated": False},
                )
                registered.append(
                    {
                        "run_id": run,
                        "revision_id": record["revision_id"],
                        "status": "passed" if passed else "blocked",
                    }
                )
    return {
        "runs": registered,
        "scope": report["scope"],
        "counts": dict(Counter(r["status"] for r in registered)),
    }

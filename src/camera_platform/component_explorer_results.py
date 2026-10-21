"""Read-only, source-bound links from catalogue revisions to saved Reference results.

The list is an evidence index, not a fresh integrity or physical validation. An
open rechecks the exact revision, current sources and every saved run artifact.
A sensor report link means a reviewed derivative of the same source PDF; it does
not claim that the legacy extraction or a product-calibrated model was executed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

from .registry import IntegrityError, digest
from .virtual_lab_job import sha256

_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RUN_CACHE = {}


def _registry_stamp(registry):
    parts = []
    for path in (registry.path, Path(str(registry.path) + "-wal")):
        try:
            st = path.stat()
            parts.append((str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except FileNotFoundError:
            parts.append((str(path), None))
    return tuple(parts)


def _runs(service):
    registry = service.registry
    if not hasattr(registry, "path") or not hasattr(registry, "_job"):
        return {
            r["id"]: r for r in registry.list_runs() if r.get("job_kind") == "reference_workshop"
        }
    stamp = _registry_stamp(registry)
    key = str(registry.path)
    cached = _RUN_CACHE.get(key)
    if cached and cached[0] == stamp:
        return deepcopy(cached[1])
    with registry.connection() as conn:
        conn.execute("BEGIN")
        identities = []
        for row in conn.execute("SELECT id,document FROM jobs ORDER BY created_at DESC,id"):
            document = registry.decode_document(row["document"])
            if document.get("job_kind") == "reference_workshop":
                identities.append(row["id"])
        if not identities:
            return {}
        marks = ",".join("?" for _ in identities)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY created_at DESC,id", identities
        ).fetchall()
        events = {
            r["subject"]: r
            for r in conn.execute(
                "SELECT * FROM audit WHERE action IN ('runs.succeeded','runs.failed') "
                f"AND subject IN ({marks}) ORDER BY seq",
                identities,
            )
        }
    runs = {}
    for row in rows:
        decoded = registry._job(row, events)
        retained = {
            k: decoded[k]
            for k in (
                "id",
                "status",
                "plugin_id",
                "job_kind",
                "storage_root",
                "reference_snapshot",
                "source",
                "isp_study",
                "ircf_study",
                "sensor_mode_study",
                "ghost_study",
                "asset_trial",
                "db_design_study",
                "specification",
            )
            if k in decoded
        }
        retained["result"] = {
            "summary": {
                "manifest_sha256": (decoded.get("result") or {})
                .get("summary", {})
                .get("manifest_sha256"),
                "baseline_candidate_id": (decoded.get("result") or {})
                .get("summary", {})
                .get("baseline_candidate_id"),
                "candidates": [
                    {
                        key: candidate.get(key)
                        for key in (
                            "id",
                            "status",
                            "label",
                            "lens_key",
                            "sensor_key",
                            "source_identities",
                        )
                    }
                    for candidate in (decoded.get("result") or {})
                    .get("summary", {})
                    .get("candidates", [])
                ]
                if decoded.get("db_design_study")
                else [],
            }
        }
        runs[retained["id"]] = retained
    if _registry_stamp(registry) == stamp:
        if len(_RUN_CACHE) > 4:
            _RUN_CACHE.clear()
        _RUN_CACHE[key] = (stamp, deepcopy(runs))
    return runs


def _catalog(service, item):
    path = Path(service.workspace) / "camerae2e_db/catalog.sqlite"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT r.*,c.family,s.metadata_json AS source_metadata,s.sha256 AS source_sha256 "
            "FROM model_revisions r JOIN components c ON c.component_id=r.component_id "
            "LEFT JOIN sources s ON s.source_id=r.source_id WHERE r.revision_id=?",
            (item.get("revision_id"),),
        ).fetchone()
    if row is None:
        raise IntegrityError("Selected catalog revision is unavailable.")
    row = dict(row)
    if any(
        row[k] != item.get(k) for k in ("revision_id", "component_id", "content_sha256", "family")
    ):
        raise IntegrityError("Selected catalog revision or content hash changed.")
    snapshot = json.loads(row["snapshot_json"])
    if digest(snapshot) != row["content_sha256"]:
        raise IntegrityError("Catalog snapshot no longer matches its revision.")
    return row, snapshot


def _source_link(record):
    if record.get("source"):
        return record["source"]
    for key in ("ircf_study", "sensor_mode_study", "ghost_study"):
        if record.get(key, {}).get("source"):
            return record[key]["source"]
    return None


def _lineage(record, runs):
    """Return the original capture and ordered pinned source dependencies."""
    chain, seen = [record], set()
    while True:
        current = chain[-1]
        if current["id"] in seen or len(chain) > 32:
            raise IntegrityError("Saved source lineage is cyclic or too deep.")
        seen.add(current["id"])
        if current.get("status") != "succeeded":
            raise IntegrityError("Saved source is not a completed acquisition.")
        link = _source_link(current)
        if link is None:
            if not current.get("reference_snapshot"):
                raise IntegrityError("Saved result has no unambiguous Reference source.")
            return current, chain
        parent = runs.get(link.get("run_id"))
        if parent is None:
            raise IntegrityError("Pinned source run is missing.")
        expected = link.get("manifest_sha256")
        if (
            not expected
            or parent.get("result", {}).get("summary", {}).get("manifest_sha256") != expected
        ):
            raise IntegrityError("Pinned source manifest differs from the saved result.")
        chain.append(parent)


def _directory(record):
    if not _RUN.fullmatch(record.get("id", "")):
        raise IntegrityError("Invalid saved run identity.")
    path = Path(record.get("storage_root", ""))
    if not path.is_absolute():
        raise IntegrityError("Saved storage root is missing.")
    directory = path / "reference-workshop" / record["id"]
    if not (directory / "manifest.json").is_file():
        raise IntegrityError("Saved result storage is unavailable.")
    return directory


def _sensor_source(row, snapshot, pixel, *, verify):
    from pyisetcam._catalog_sensor import _semantic

    if row["model_kind"] != "sensor_report_extraction":
        raise IntegrityError("Sensor result links require the report extraction revision.")
    meta = json.loads(row["source_metadata"] or "{}")
    raw = meta.get("source_record", {})
    expected = {
        "derived_specs": raw.get("derived_specs", {}),
        "derived_evidence": _semantic(raw.get("derived_evidence", {})),
        "table_evidence": _semantic(raw.get("table_evidence", [])),
        "metadata": _semantic(raw.get("metadata", {})),
        "origin_kind": "extracted",
    }
    code = snapshot.get("metadata", {}).get("code")
    if snapshot != expected or digest(_semantic(raw)) != row["source_sha256"]:
        raise IntegrityError("Sensor extraction no longer matches its imported source record.")
    evidence = pixel.get("provenance", {}).get("structural_evidence", {})
    source = evidence.get("sources", {}).get(code, {})
    if not code or code != pixel.get("source_report") or code != evidence.get("report_code"):
        raise IntegrityError("Sensor report identity does not match the saved acquisition.")
    if not evidence.get("available") or not _HASH.fullmatch(source.get("sha256", "")):
        raise IntegrityError("Saved sensor evidence has no pinned source PDF hash.")
    relative = Path(source.get("relative_path", ""))
    candidates = [
        Path(p)
        for p in raw.get("source_files", {}).get("pdf", [])
        if isinstance(p, str)
        and Path(p).is_absolute()
        and tuple(Path(p).parts[-len(relative.parts) :]) == relative.parts
    ]
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or len(candidates) != 1
    ):
        raise IntegrityError("Catalog and saved sensor evidence do not identify the same PDF.")
    pdf = candidates[0]
    if not pdf.is_file():
        raise IntegrityError("Original sensor report PDF is unavailable.")
    if verify:
        if sha256(pdf) != source["sha256"]:
            raise IntegrityError("Sensor report PDF changed from the acquisition evidence.")
        from pyisetcam.sensor_techinsights import _manifest

        if _manifest()[1] != evidence.get("manifest_sha256"):
            raise IntegrityError("Reviewed structural evidence changed after acquisition.")
        provenance = pixel["provenance"]
        for key in ("optical_model", "generated_stack"):
            filename = provenance.get("source_files", {}).get(key)
            expected_hash = provenance.get("source_" + key + "_sha256")
            if (
                not filename
                or not expected_hash
                or not Path(filename).is_file()
                or sha256(filename) != expected_hash
            ):
                raise IntegrityError("Sensor model source is missing or changed: " + key)
    return {
        "relation": "reviewed_derivative_of_source_report",
        "report_code": code,
        "source_pdf_sha256": source["sha256"],
        "reviewed_evidence_sha256": evidence.get("manifest_sha256"),
        "legacy_extraction_executed": False,
    }


def _bind(service, item, original, *, verify=False):
    row, snapshot = _catalog(service, item)
    reference = original["reference_snapshot"]
    result = {
        "component_id": item["component_id"],
        "revision_id": item["revision_id"],
        "content_sha256": item["content_sha256"],
        "source_run_id": original["id"],
        "physical_validation": "not_established",
    }
    if item["family"] == "lens":
        lens = reference.get("lens", {})
        if (
            lens.get("revision_id") != item["revision_id"]
            or lens.get("revision_content_sha256") != item["content_sha256"]
        ):
            raise IntegrityError("Lens source revision does not match the saved acquisition.")
        if item.get("configuration_label") and str(item["configuration_label"]) != str(
            snapshot.get("configuration")
        ):
            raise IntegrityError("Lens configuration differs from the saved acquisition.")
        if verify:
            from .lens_catalog_bridge import _revision_source
            from .lens_db_wavefront_bridge import _reviewed_csv

            _, _, db, db_hash = _revision_source(
                item["revision_id"], Path(service.workspace) / "camerae2e_db"
            )
            if db_hash != lens.get("source_database_sha256"):
                raise IntegrityError("Lens source database differs from the saved acquisition.")
            relative = lens.get("reviewed_csv_relative_path", "")
            if relative.startswith("reviewed_prescriptions/"):
                relative = relative.removeprefix("reviewed_prescriptions/")
            _reviewed_csv(db, relative, lens.get("reviewed_csv_sha256"))
        result.update(
            relation="exact_lens_revision",
            configuration_id=item.get("configuration_id"),
            configuration_label=snapshot.get("configuration"),
        )
    elif item["family"] == "sensor":
        result.update(_sensor_source(row, snapshot, reference.get("pixel", {}), verify=verify))
    else:
        raise IntegrityError("Unsupported result asset family.")
    return result


def _presentation(record):
    identity = record["id"]
    plugin = record.get("plugin_id", "")
    kind, label, params = "reference_bench", "Reference · RAW / ISP", {"run": identity}
    path = "/reference-workshop.html"
    if plugin == "camera_reference_asset_trial_v1":
        kind, label = "asset_trial", "Adopted readout · new RAW / fixed ISP"
    elif plugin == "camera_reference_ircf_study_v1":
        kind, label, path = "ircf_study", "IRCF · signal / color", "/reference-ircf.html"
    elif plugin == "camera_reference_sensor_mode_study_v1":
        kind, label, params = (
            "sensor_modes",
            "Sensor · readout / binning",
            {"sensor_study": identity},
        )
    elif plugin in {"camera_reference_ghost_study_v1", "camera_reference_ghost_study_v2"}:
        kind, label, path = "ghost_study", "Ghost · source angle / RAW", "/reference-ghost.html"
    elif record.get("isp_study") or record.get("source"):
        kind, label = "isp_study", "ISP · preserved RAW replay"
    return kind, label, path + "?" + urlencode(params)


def _result(record, original, binding):
    kind, label, url = _presentation(record)
    if binding.get("relation") == "reviewed_derivative_of_source_report":
        label += " · reviewed report derivative"
    actions = ["raw_isp", "workbench"]
    if kind in {"reference_bench", "isp_study"}:
        actions.insert(0, "stored_psf")
    return {
        "id": record["id"],
        "kind": kind,
        "label": label,
        "run_id": record["id"],
        "url": url,
        "available": True,
        "status": "stored_evidence",
        "reason": "Saved evidence; source and artifact hashes are checked when opened.",
        "actions": actions,
        "binding": {
            **binding,
            "result_manifest_sha256": record["result"]["summary"]["manifest_sha256"],
            "source_manifest_sha256": original["result"]["summary"]["manifest_sha256"],
        },
    }


def _db_result(service, item, record, *, verify=False):
    """Bind a multi-candidate DB acquisition without inventing Reference lineage."""
    from . import db_design_job

    if (
        record.get("plugin_id") != db_design_job.SCHEMA
        or record.get("status") != "succeeded"
        or record.get("db_design_study", {}).get("schema") != db_design_job.SCHEMA
    ):
        raise IntegrityError("DB design result is not a completed acquisition.")
    _, catalog_snapshot = _catalog(service, item)
    pinned = record["db_design_study"]["snapshot"]
    summary = record.get("result", {}).get("summary", {})
    family = item["family"]
    family_key = {"lens": "lenses", "sensor": "sensors"}.get(family)
    if family_key is None:
        raise IntegrityError("Unsupported DB design asset family.")
    candidates = []
    for candidate in summary.get("candidates", []):
        if candidate.get("status") != "succeeded":
            continue
        keys = (candidate.get("lens_key"), candidate.get("sensor_key"))
        sources = candidate.get("source_identities", {})
        if any(
            not key
            or key not in pinned.get(plural, {})
            or digest(sources.get(singular)) != digest(pinned[plural][key])
            for key, singular, plural in zip(
                keys, ("lens", "sensor"), ("lenses", "sensors"), strict=True
            )
        ):
            continue
        source = sources[family]
        identity = source.get("source", {}) if family == "lens" else source
        source_hash = identity.get("revision_content_sha256", identity.get("content_sha256"))
        if (
            identity.get("revision_id") != item["revision_id"]
            or source_hash != item["content_sha256"]
        ):
            continue
        if family == "sensor" and source.get("asset_id") != item["id"]:
            continue
        if family == "lens" and item.get("configuration_label") != catalog_snapshot.get(
            "configuration"
        ):
            continue
        candidates.append(candidate)
    if not candidates:
        raise IntegrityError("No completed DB candidate matches this exact catalog asset.")
    # One stable entry per asset/run: prefer the first (other-axis Reference) case.
    candidate = sorted(candidates, key=lambda value: value["id"])[0]
    baseline = summary.get("baseline_candidate_id")
    if not baseline or baseline not in {c.get("id") for c in summary.get("candidates", [])}:
        raise IntegrityError("DB study baseline candidate is unavailable.")
    if verify:
        current = db_design_job.snapshot(service.workspace, record["specification"])
        if digest(current) != digest(pinned):
            raise IntegrityError("DB design source snapshot changed after acquisition.")
    manifest_hash = summary.get("manifest_sha256")
    if not _HASH.fullmatch(manifest_hash or ""):
        raise IntegrityError("DB design result has no pinned manifest hash.")
    return {
        "id": record["id"],
        "run_id": record["id"],
        "kind": "db_design_study",
        "label": "DB Lens × Sensor · " + candidate.get("label", candidate["id"]),
        "url": "/db-design.html?"
        + urlencode({"run": record["id"], "a": baseline, "b": candidate["id"]}),
        "available": True,
        "status": "stored_evidence",
        "reason": "Pinned DB candidate evidence; source and artifact hashes are checked on open.",
        "actions": ["raw_isp", "workbench"],
        "binding": {
            "relation": "exact_db_design_candidate",
            "component_id": item["component_id"],
            "revision_id": item["revision_id"],
            "content_sha256": item["content_sha256"],
            "candidate_id": candidate["id"],
            "related_candidate_ids": [c["id"] for c in candidates],
            "source_run_id": record["id"],
            "source_snapshot_sha256": digest(pinned),
            "result_manifest_sha256": manifest_hash,
            "source_manifest_sha256": manifest_hash,
            "physical_validation": "not_established",
        },
    }


def bindings(service, items):
    """Index exact source relationships; do not load RAW/PSF arrays or run a model."""
    result = {item["id"]: [] for item in items}
    by_lens, by_sensor = {}, {}
    for item in items:
        if item["family"] == "lens":
            by_lens.setdefault(item["revision_id"], []).append(item)
        elif item["family"] == "sensor":
            code = item.get("report_code") or item.get("parameters", {}).get("report_code")
            by_sensor.setdefault(code, []).append(item)
    runs = _runs(service)
    cached = {}
    for record in runs.values():
        if record.get("status") != "succeeded":
            continue
        if record.get("db_design_study"):
            pinned = record["db_design_study"].get("snapshot", {})
            revisions = {
                entry.get("revision_id")
                for entries in pinned.values()
                if isinstance(entries, dict)
                for entry in entries.values()
                if isinstance(entry, dict)
            }
            for item in items:
                if item["revision_id"] not in revisions:
                    continue
                try:
                    entry = _db_result(service, item, record)
                    try:
                        _directory(record)
                    except (ValueError, OSError):
                        entry.update(
                            available=False,
                            status="unavailable",
                            url=None,
                            reason="Saved DB result storage is unavailable.",
                        )
                    result[item["id"]].append(entry)
                except (ValueError, KeyError, OSError, sqlite3.Error):
                    continue
            continue
        try:
            original, chain = _lineage(record, runs)
            reference = original["reference_snapshot"]
            candidates = by_lens.get(
                reference.get("lens", {}).get("revision_id"), []
            ) + by_sensor.get(reference.get("pixel", {}).get("source_report"), [])
            for item in candidates:
                key = (item["id"], original["id"])
                if key not in cached:
                    try:
                        cached[key] = _bind(service, item, original)
                    except (ValueError, KeyError, OSError, sqlite3.Error):
                        cached[key] = None
                if cached[key] is None:
                    continue
                entry = _result(record, original, cached[key])
                try:
                    for parent in chain:
                        _directory(parent)
                except (ValueError, OSError):
                    entry.update(
                        available=False,
                        status="unavailable",
                        url=None,
                        reason="Saved result or pinned source storage is unavailable.",
                    )
                result[item["id"]].append(entry)
        except (ValueError, KeyError, OSError):
            continue
    return result


def validate_result(service, item, result_id):
    """Revalidate a selected link; never replace a missing/stale result with another."""
    from . import reference_workshop_job as workshop

    if not _RUN.fullmatch(str(result_id)):
        raise ValueError("Choose an exact saved result ID.")
    record = service.registry.get_run(result_id)
    if record.get("job_kind") != "reference_workshop":
        raise ValueError("Result is not a Reference workshop computation.")
    if record.get("db_design_study"):
        result = _db_result(service, item, record, verify=True)
        _directory(record)
        checked = workshop.get(service, record["id"])
        workshop.verify_files(service, checked)
        result.update(
            status="verified_files",
            reason=(
                "Exact DB revisions, current source snapshot and saved artifact hashes match; "
                "physical validation is not established."
            ),
        )
        return result
    # Fetch only this chain, rather than parsing all historical jobs at open time.
    runs, pending = {record["id"]: record}, record
    for _ in range(32):
        link = _source_link(pending)
        if link is None:
            break
        parent_id = link.get("run_id")
        if parent_id in runs:
            raise IntegrityError("Saved source lineage is cyclic.")
        pending = service.registry.get_run(parent_id)
        runs[parent_id] = pending
    original, chain = _lineage(record, runs)
    binding = _bind(service, item, original, verify=True)
    for parent in chain:
        _directory(parent)
        checked = workshop.get(service, parent["id"])
        workshop.verify_files(service, checked)
    result = _result(record, original, binding)
    result.update(
        status="verified_files",
        reason=(
            "Catalog/source and saved artifact hashes match; "
            "physical validation is not established."
        ),
    )
    return result

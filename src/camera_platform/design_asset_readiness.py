"""Evidence progression for design assets without changing source databases.

Source completeness, executable adapters and measured agreement are independent.
A user-assumed readout revision can be executable without being product calibrated.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from pathlib import Path

from . import component_explorer_catalog as catalog
from . import component_explorer_results as bindings
from .registry import ConflictError, IntegrityError, digest

SCHEMA = "camera_design_asset_readiness_v1"
MODEL_KIND = "design_asset_models"
PROPOSAL_KIND = "design_asset_proposals"
SCAN_KIND = "design_asset_scans"
CHECK_KIND = "design_asset_checks"
LINK_KIND = "design_asset_validation_links"
FIELDS = {"read_noise_e": (0.0, 1e6), "dark_current_e_per_s": (0.0, 1e12)}
_UNITS = {"read_noise_e": "electron RMS", "dark_current_e_per_s": "electron/s"}


def _records(service, kind):
    return service.registry.list(kind)


def _once(service, kind, value):
    identity = kind.rstrip("s") + "-" + digest(value)[:32]
    try:
        return service.registry.get(kind, identity)
    except KeyError:
        try:
            return service.registry.put(kind, value, identity=identity)
        except ConflictError:
            return service.registry.get(kind, identity)


def _safe(value):
    """Retain structured scalar evidence without source paths or quoted reports."""
    if isinstance(value, dict):
        return {
            str(k): _safe(v)
            for k, v in value.items()
            if k not in {"source_files", "source_file", "source_dirs", "text", "raw", "uri"}
        }
    if isinstance(value, list):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        if value.startswith(("/", "file://")) or "/Users/" in value or "/Volumes/" in value:
            return "source location withheld"
        return value[:500]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _index(service):
    try:
        return catalog.index(service.workspace)
    except (OSError, sqlite3.Error, ValueError):
        return None


def _asset(service, identity):
    view = _index(service)
    if view is None:
        raise ConflictError("Source catalog is unavailable; original records were not deleted.")
    for item in view["items"]:
        if item["id"] == identity:
            return item
    raise KeyError("Design asset is not present in the current catalog snapshot.")


def _reference_cell(service):
    from .reference_workshop_sensor import build_cell

    return build_cell(Path(service.workspace))


def _reference(service, item, source_run_id=None):
    """Pin the exact current reviewed cell and an existing native acquisition."""
    from . import reference_workshop_job as workshop
    from .reference_sensor_job import _load_source
    from .virtual_lab_job import sha256

    if item["family"] != "sensor" or item.get("report_code") != "DEF-2011-804":
        raise ValueError("Only the exact reviewed IMX462 report has the first readout adapter.")
    cell = _reference_cell(service)
    row, snapshot = bindings._catalog(service, item)
    report_binding = bindings._sensor_source(row, snapshot, cell, verify=True)
    if source_run_id:
        candidates = [service.registry.get_run(source_run_id)]
    else:
        runs = bindings._runs(service)
        candidates = [r for r in runs.values() if not bindings._source_link(r)]
    failures = []
    for record in candidates:
        try:
            if record.get("status") != "succeeded" or not record.get("reference_snapshot"):
                raise ValueError("Source is not a completed native Reference acquisition.")
            if bindings._source_link(record):
                raise ValueError("Choose the original acquisition, not an ISP or sensor replay.")
            source = {
                "run_id": record["id"],
                "candidate_id": None,
                "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
            }
            directory, capture = _load_source(
                service, {"source": source, "fields": ["center", "outer-ne"], "charts": ["uniform"]}
            )
            manifest = json.loads(
                (workshop._directory(service, record["id"]) / "manifest.json").read_text()
            )
            if sha256(directory / "pixel-cell.json") != manifest["files"].get("pixel-cell.json"):
                raise IntegrityError("Preserved pixel cell no longer matches its acquisition.")
            saved = json.loads((directory / "pixel-cell.json").read_text())
            if saved != cell:
                raise IntegrityError(
                    "Current Reference inputs differ from the preserved acquisition."
                )
            return (
                {
                    **source,
                    "cell_sha256": cell["cell_sha256"],
                    "source_report": cell["source_report"],
                    "source_evidence_sha256": cell["provenance"]["source_evidence_sha256"],
                    "catalog_content_sha256": item["content_sha256"],
                    "source_model_sha256": cell["provenance"]["source_model_sha256"],
                    "source_material_bundle_sha256": cell["provenance"][
                        "source_material_bundle_sha256"
                    ],
                    **report_binding,
                },
                cell,
                capture,
            )
        except (KeyError, ValueError, OSError) as exc:
            failures.append(_safe(str(exc)))
    raise ValueError(
        failures[0]
        if source_run_id and failures
        else "No native acquisition matches the current reviewed Reference inputs."
    )


def _applied(service, item):
    if item["family"] != "sensor" or item.get("report_code") != "DEF-2011-804":
        return {}, None
    try:
        cell = _reference_cell(service)
        row, snapshot = bindings._catalog(service, item)
        bindings._sensor_source(row, snapshot, cell, verify=True)
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        return {}, _safe(str(exc))
    facts = cell["field_classification"]["source_derived"]
    values = {field: value["value"] for field, value in facts.items()}
    for field in FIELDS:
        values[field] = cell["electrical_model"][field]
    return {"values": values, "cell": cell}, None


def _applied_parameter_map(applied):
    """Explicit adapter values; aliases never cross electrical units or readout stages."""
    if not applied:
        return {}
    cell = applied["cell"]
    mapping = {}
    for field, fact in cell["field_classification"]["source_derived"].items():
        mapping[field] = {
            "value": fact["value"],
            "unit": fact.get("unit"),
            "origin_kind": "source_derived",
            "source_label": "Reviewed Reference structural evidence",
        }
    electrical = cell["electrical_model"]
    units = {
        "full_well_e": "electron",
        "conversion_gain_v_per_e": "V/electron",
        "read_noise_e": "electron RMS",
        "dark_current_e_per_s": "electron/s",
        "reference_temperature_c": "degC",
        "post_gain_noise_v": "V RMS",
        "digital_gain": "ratio",
        "dark_current_doubling_c": "degC",
        "bit_depth": "bit",
        "full_scale_v": "V",
        "black_level_dn": "DN",
        "prnu_sigma": "fraction",
        "dsnu_e_per_s": "electron/s",
        "seed": None,
        "gain": "ratio",
    }
    flattened = {
        **{k: v for k, v in electrical.items() if k not in {"schema", "adc", "fixed_pattern"}},
        **electrical.get("adc", {}),
        **electrical.get("fixed_pattern", {}),
        **{
            k: v
            for k, v in cell["field_classification"]["common_assumptions"]
            .get("readout", {})
            .items()
            if k not in {"schema", "adc", "fixed_pattern"}
        },
    }
    for field, value in flattened.items():
        mapping[field] = {
            "value": value,
            "unit": units.get(field),
            "origin_kind": "assumed",
            "source_label": "Reference shared electrical assumption",
        }
    aliases = {
        "full_well": "full_well_e",
        "full_well_capacity_e": "full_well_e",
        "adc_bit_depth": "bit_depth",
        "adc.bit_depth": "bit_depth",
        "black_level": "black_level_dn",
        "adc.black_level_dn": "black_level_dn",
        "adc.full_scale_v": "full_scale_v",
        "prnu_fraction": "prnu_sigma",
        "fixed_pattern.prnu_sigma": "prnu_sigma",
        "fixed_pattern.dsnu_e_per_s": "dsnu_e_per_s",
    }
    for alias, canonical in aliases.items():
        if canonical in mapping:
            mapping[alias] = {**mapping[canonical], "canonical_field": canonical}
    # Total measured noise, e/ADU, DSNU electrons and dark-rate DSNU are not aliases.
    return mapping


def _observations(service, item, applied=None):
    path = Path(service.workspace) / "camerae2e_db/catalog.sqlite"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT observation_id,revision_id,operating_point_id,property_name,"
            "value_json,unit,origin_kind,source_id,conditions_json,missing_reason "
            "FROM spec_observations WHERE component_id=? AND revision_id=? "
            "ORDER BY property_name,observation_id",
            (item["component_id"], item["revision_id"]),
        ).fetchall()
    mapping = _applied_parameter_map(applied)
    parameters = []
    for row in rows:
        source_value = _safe(json.loads(row["value_json"]))
        field = row["property_name"]
        used = mapping.get(field)
        parameters.append(
            {
                "field": field,
                "source_value": source_value,
                "applied_value": _safe(used["value"]) if used else None,
                "applied_origin_kind": used["origin_kind"] if used else None,
                "applied_source_label": used["source_label"] if used else None,
                "applied_unit": used["unit"] if used else None,
                "applied_mapping_status": "mapped" if used else "mapping_unknown",
                "evidence_used_for_reference": False,
                "unit": row["unit"],
                "origin_kind": row["origin_kind"],
                "source_label": item["source_label"],
                "source_id": row["source_id"],
                "observation_id": row["observation_id"],
                "revision_id": row["revision_id"],
                "operating_point_id": row["operating_point_id"],
                "conditions": _safe(json.loads(row["conditions_json"])),
                "missing_reason": row["missing_reason"],
                "applied_to_reference": True if used else None,
            }
        )
    if applied:
        for field, value in mapping.items():
            if value["origin_kind"] != "assumed" or "canonical_field" in value:
                continue
            parameters.append(
                {
                    "field": field,
                    "source_value": None,
                    "applied_value": _safe(value["value"]),
                    "unit": value["unit"],
                    "applied_unit": value["unit"],
                    "origin_kind": "assumed",
                    "applied_origin_kind": "assumed",
                    "source_label": value["source_label"],
                    "applied_source_label": value["source_label"],
                    "applied_mapping_status": "mapped",
                    "evidence_used_for_reference": True,
                    "conditions": {
                        "reference_temperature_c": applied["cell"]["electrical_model"][
                            "reference_temperature_c"
                        ]
                    },
                    "missing_reason": "Product readout measurement is not available.",
                    "applied_to_reference": True,
                    "observation_id": None,
                    "revision_id": item["revision_id"],
                }
            )
        for field, value in applied["cell"]["field_classification"]["common_assumptions"].items():
            if field == "readout":
                continue
            parameters.append(
                {
                    "field": field,
                    "source_value": None,
                    "applied_value": _safe(value),
                    "unit": None,
                    "applied_unit": None,
                    "origin_kind": "assumed",
                    "applied_origin_kind": "assumed",
                    "source_label": "Reference common assumption",
                    "applied_source_label": "Reference common assumption",
                    "applied_mapping_status": "mapped",
                    "evidence_used_for_reference": True,
                    "conditions": {},
                    "missing_reason": None,
                    "applied_to_reference": True,
                    "observation_id": None,
                    "revision_id": item["revision_id"],
                }
            )
    return parameters


def _model_views(service, asset_id=None):
    checks = []
    for record in _records(service, CHECK_KIND):
        if record.get("job_id"):
            try:
                if service.registry.get_run(record["job_id"])["status"] != "succeeded":
                    continue
            except (KeyError, ValueError, OSError):
                continue
        checks.append(record)
    trials = _records(service, "design_asset_trial_links")
    current_sources = digest(_source_snapshot(service)) if checks else None
    models = []
    for record in _records(service, MODEL_KIND):
        if asset_id is not None and record["asset_id"] != asset_id:
            continue
        model = copy.deepcopy(record)
        model["latest_check"] = next(
            (
                c
                for c in checks
                if c["model_id"] == model["id"] and c["model_content_hash"] == model["content_hash"]
            ),
            None,
        )
        if (
            model["latest_check"]
            and model["latest_check"]["execution_ready"]
            and model["latest_check"].get("source_fingerprint") != current_sources
        ):
            model["latest_check"] = {
                **model["latest_check"],
                "execution_ready": False,
                "status": "stale",
                "issues": ["Registered source inputs changed; check this model again."],
            }
        model["trials"] = []
        for link in trials:
            if (
                link.get("model_revision_id") != model["id"]
                or link.get("model_content_hash") != model["content_hash"]
            ):
                continue
            status = "unavailable"
            try:
                run = service.registry.get_run(link["run_id"])
                if (
                    run.get("design_asset_model_id") == model["id"]
                    and run.get("design_asset_model_content_hash") == model["content_hash"]
                ):
                    status = run["status"]
            except (KeyError, ValueError, OSError):
                pass
            model["trials"].append({"run_id": link["run_id"], "status": status})
        model["trial_completed"] = any(t["status"] == "succeeded" for t in model["trials"])
        model["latest_trial"] = model["trials"][0] if model["trials"] else None
        models.append(model)
    return models


def _proposal_views(service, asset_id=None):
    models = _records(service, MODEL_KIND)
    result = []
    for saved in _records(service, PROPOSAL_KIND):
        if asset_id is not None and saved["asset_id"] != asset_id:
            continue
        entry = copy.deepcopy(saved)
        entry["adopted_model_id"] = next(
            (m["id"] for m in models if m["proposal_id"] == entry["id"]), None
        )
        result.append(entry)
    return result


def _db_execution(service, items):
    """Separate the supported DB capture path from user-adopted readout revisions."""
    from . import db_design_job, db_design_optics, db_design_sensor

    selected = {}
    for family, module in (("lens", db_design_optics), ("sensor", db_design_sensor)):
        try:
            choices = module.candidates(service.workspace)
        except (ValueError, KeyError, OSError, sqlite3.Error):
            choices = []
        for choice in choices:
            source = choice.get("source", {}) if family == "lens" else choice
            revision = source.get("revision_id", choice.get("revision_id"))
            content = source.get("revision_content_sha256", source.get("content_sha256"))
            for item in items:
                if (
                    item["family"] != family
                    or item["revision_id"] != revision
                    or item["content_sha256"] != content
                    or (family == "sensor" and choice.get("asset_id") != item["id"])
                ):
                    continue
                selected[item["id"]] = {
                    "status": "adapter_supported" if choice.get("execution_ready") else "blocked",
                    "adapter_key": choice["key"],
                    "route": "/db-design.html",
                    "parameter_sources": _safe(choice.get("parameter_sources", [])),
                    "reason": choice.get("reason"),
                    "results": [],
                    "scope": "Pinned DB lens/sensor capture at native 2.9 um RGGB, "
                    "256×256 center/outer-ne probes, fixed ISP. "
                    "Separate from adopted readout changes; product agreement is not established.",
                }
    eligible = [item for item in items if item["id"] in selected]
    indexed = bindings.bindings(service, eligible) if eligible else {}
    current_snapshots = {}
    for item in eligible:
        completed = [
            row
            for row in indexed.get(item["id"], [])
            if row.get("kind") == "db_design_study" and row.get("available")
        ]
        for row in completed:
            run_id = row["run_id"]
            if run_id not in current_snapshots:
                try:
                    run = service.registry.get_run(run_id)
                    current_snapshots[run_id] = (
                        digest(db_design_job.snapshot(service.workspace, run["specification"]))
                        if run["status"] == "succeeded"
                        else None
                    )
                except (ValueError, KeyError, OSError, sqlite3.Error):
                    current_snapshots[run_id] = None
            if current_snapshots[run_id] != row["binding"]["source_snapshot_sha256"]:
                row.update(
                    available=False,
                    status="stale",
                    url=None,
                    reason="Current DB execution inputs differ from this retained run.",
                )
        selected[item["id"]]["results"] = completed
        if (
            any(row["available"] for row in completed)
            and selected[item["id"]]["status"] == "adapter_supported"
        ):
            selected[item["id"]]["status"] = "succeeded"
        elif completed and any(row["status"] == "stale" for row in completed):
            selected[item["id"]]["status"] = "stale"
            selected[item["id"]]["reason"] = (
                "Retained DB computations use different inputs; a new capture is required."
            )
    return selected


def _statuses(items, models, links, db_execution=None):
    result = {}
    for item in items:
        related = [m for m in models if m["asset_id"] == item["id"]]
        ready = [m for m in related if (m.get("latest_check") or {}).get("execution_ready")]
        measured = [r for r in links if r["asset_id"] == item["id"] and r.get("physical_match")]
        result[item["id"]] = {
            "source": "source_backed",
            "execution": "trial_completed"
            if any(m["trial_completed"] for m in ready)
            else "ready"
            if ready
            else "stale"
            if any((m.get("latest_check") or {}).get("status") == "stale" for m in related)
            else "blocked"
            if related
            else "adapter_supported"
            if item.get("report_code") == "DEF-2011-804"
            else "not_checked",
            "measurement": "scoped_match" if measured else "not_established",
        }
        if db_execution and item["id"] in db_execution:
            db_status = db_execution[item["id"]]["status"]
            # Adapter availability must not erase a completed/checked adopted
            # revision or hide its stale-input warning. DB acquisition is an
            # independent path and its detailed state remains in db_execution.
            if db_status == "succeeded" or not related:
                result[item["id"]]["execution"] = db_status
    return result


def overview(service):
    view = _index(service)
    observations = {}
    if view:
        db = Path(service.workspace) / "camerae2e_db/catalog.sqlite"
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
            observations = dict(
                con.execute(
                    "SELECT origin_kind,count(*) FROM spec_observations GROUP BY origin_kind"
                )
            )
    models, proposals = _model_views(service), _proposal_views(service)
    scans, links = _records(service, SCAN_KIND), _records(service, LINK_KIND)
    db_execution = _db_execution(service, view["items"] if view else [])
    statuses = _statuses(view["items"] if view else [], models, links, db_execution)
    summary = {axis: {} for axis in ("source", "execution", "measurement")}
    for status in statuses.values():
        for axis, value in status.items():
            summary[axis][value] = summary[axis].get(value, 0) + 1
    return {
        "schema": SCHEMA,
        "source_status": "available" if view else "unavailable",
        "counts": {
            "assets": len(statuses),
            "classified_observations": observations,
            "models": len(models),
            "proposals": len(proposals),
            "scans": len(scans),
        },
        "status_summary": summary,
        "asset_statuses": statuses,
        "db_execution": db_execution,
        "latest_scan": scans[0] if scans else None,
        "models": models,
        "proposals": proposals,
        "scope": "Source evidence, executable checks and scoped physical agreement "
        "are independent; no accuracy percentage.",
    }


def detail(service, asset_id):
    item = _asset(service, asset_id)
    applied, error = _applied(service, item)
    models, proposals = _model_views(service, asset_id), _proposal_views(service, asset_id)
    links = [r for r in _records(service, LINK_KIND) if r["asset_id"] == asset_id]
    db_execution = _db_execution(service, [item])
    statuses = _statuses([item], models, links, db_execution)[asset_id]
    history = (
        proposals
        + models
        + [r for r in _records(service, CHECK_KIND) if r["asset_id"] == asset_id]
        + links
    )
    history.sort(key=lambda r: (r.get("created_at", ""), r["id"]), reverse=True)
    return {
        "schema": SCHEMA,
        "asset": item,
        "parameters": _observations(service, item, applied),
        "reconciliations": _reconciliations(applied),
        "readiness": {
            "source": {
                "status": "unavailable" if error else statuses["source"],
                "reasons": [error] if error else [],
            },
            "execution": {
                "status": "blocked" if error else statuses["execution"],
                "scope": db_execution[asset_id]["scope"]
                if asset_id in db_execution
                else "User readout adoption supports checked IMX462 read-noise/dark-current "
                "revisions. DB candidates use the separate pinned DB capture adapter.",
                "model_ids": [m["id"] for m in models],
            },
            "measurement": {"status": statuses["measurement"], "links": links},
        },
        "models": models,
        "proposals": proposals,
        "history": history,
        "db_execution": db_execution.get(asset_id),
    }


def _reconciliations(applied):
    if not applied:
        return []
    cell = applied["cell"]
    evidence = cell.get("provenance", {}).get("structural_evidence", {})
    result = []
    for field, fact in cell["field_classification"]["source_derived"].items():
        result.append(
            {
                "field": field,
                "source_value": fact["value"],
                "reviewed_value": fact["value"],
                "applied_value": applied["values"].get(field),
                "status": "already_applied",
                "origin_kind": "source_derived",
                "reason": "Reviewed structural input was already applied "
                "by the current Reference adapter.",
            }
        )
    for correction in evidence.get("corrected_legacy_classifications", []):
        result.append(
            {
                "field": correction["field"].removeprefix("derived_specs."),
                "source_value": correction["catalog_value"],
                "reviewed_value": correction.get("recommended_active_pixel_value"),
                "applied_value": None,
                "status": "reviewed_unmodeled",
                "origin_kind": "source_derived",
                "reason": "Reviewed classification corrects extraction; "
                "this effect is not in the current solver.",
            }
        )
    cfa = evidence.get("selected_facts", {}).get("cfa_thickness_observed_range_um")
    if cfa:
        result.append(
            {
                "field": "cfa_thickness_by_channel_um",
                "source_value": cfa["value"],
                "reviewed_value": cfa["value"],
                "applied_value": cell["field_classification"]["common_assumptions"][
                    "baseline_cfa_thickness_by_channel_um"
                ],
                "status": "assumption_within_reported_range",
                "origin_kind": "assumed",
                "reason": "The reported range has no per-color assignment; "
                "0.5 µm per channel remains a shared assumption.",
            }
        )
    return result


def _source_snapshot(service):
    workspace = Path(service.workspace).resolve()
    registered = {
        "component_catalog": workspace / "camerae2e_db/catalog.sqlite",
        "sensor_catalog": workspace / "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json",
        "reviewed_sensor_evidence": workspace
        / "src/pyisetcam/data/sensor_estimate/techinsights_evidence_v1.json",
        "reference_adapter": workspace / "src/camera_platform/reference_workshop_sensor.py",
    }
    cell_identity = {"status": "unavailable", "sha256": None}
    try:
        cell = _reference_cell(service)
        cell_identity = {"status": "available", "sha256": cell["cell_sha256"]}
        for key, value in cell["provenance"]["source_files"].items():
            path = Path(value).resolve()
            if path.is_relative_to(workspace):
                registered["reference_" + key] = path
    except (KeyError, ValueError, OSError):
        pass
    result = {"reference_cell": cell_identity}
    for label, path in registered.items():
        try:
            result[label] = {"status": "available", "sha256": catalog._hash_file(path)}
        except OSError:
            result[label] = {"status": "unavailable", "sha256": None}
    return result


def _reference_parameters(service):
    try:
        cell = _reference_cell(service)
    except (KeyError, ValueError, OSError) as exc:
        return {"status": "unavailable", "values": {}, "reason": _safe(str(exc))}
    values = {
        field: {"value": _safe(value["value"]), "origin_kind": "source_derived"}
        for field, value in cell["field_classification"]["source_derived"].items()
    }
    for field, value in cell["field_classification"]["common_assumptions"].items():
        if field != "readout":
            values[field] = {"value": _safe(value), "origin_kind": "assumed"}
    for field in FIELDS:
        values[field] = {"value": cell["electrical_model"][field], "origin_kind": "assumed"}
    return {"status": "available", "values": values}


def scan(service, progress=None):
    if progress:
        progress({"stage": "reading_registered_sources"})
    view, sources = _index(service), _source_snapshot(service)
    reference_parameters = _reference_parameters(service)
    old = _records(service, SCAN_KIND)
    previous = old[0] if old else None
    assets = (
        {
            i["id"]: {
                "revision_id": i["revision_id"],
                "content_sha256": i["content_sha256"],
                "fingerprint": digest(i),
                "name": i["name"],
            }
            for i in view["items"]
        }
        if view
        else copy.deepcopy((previous or {}).get("assets", {}))
    )
    changes = []
    if previous:
        for key in previous["sources"]:
            sources.setdefault(key, {"status": "unavailable", "sha256": None})
        for key in sorted(set(sources) | set(previous["sources"])):
            current, prior = sources.get(key), previous["sources"].get(key)
            if current != prior:
                changes.append(
                    {
                        "kind": "source",
                        "source": key,
                        "status": "unavailable"
                        if not current or current["status"] == "unavailable"
                        else "changed",
                        "before": prior,
                        "after": current,
                    }
                )
        if view:
            for key in sorted(set(assets) | set(previous["assets"])):
                if assets.get(key) != previous["assets"].get(key):
                    changes.append(
                        {
                            "kind": "asset",
                            "asset_id": key,
                            "status": "not_in_current_snapshot"
                            if key not in assets
                            else "added"
                            if key not in previous["assets"]
                            else "changed",
                            "before": previous["assets"].get(key),
                            "after": assets.get(key),
                        }
                    )
    if (
        previous
        and not changes
        and previous["source_status"] == ("available" if view else "unavailable")
    ):
        return {**previous, "unchanged": True}
    if progress:
        progress({"stage": "publishing_scan_snapshot"})
    record = _once(
        service,
        SCAN_KIND,
        {
            "schema": SCHEMA,
            "source_status": "available" if view else "unavailable",
            "sources": sources,
            "assets": assets,
            "reference_parameters": reference_parameters,
            "changes": changes,
            "previous_scan_id": previous["id"] if previous else None,
            "scope": "Registered local source inspection only; "
            "no model change or simulation was performed.",
        },
    )
    current_items = {i["id"]: i for i in view["items"]} if view else {}
    affected = {}
    for change in changes:
        if change["kind"] == "asset" and change["asset_id"] in current_items:
            affected.setdefault(change["asset_id"], []).append(change)
        elif (
            change["kind"] == "source"
            and change["status"] == "changed"
            and change["source"].startswith(("reference_", "reviewed_"))
        ):
            for item in current_items.values():
                if item.get("report_code") == "DEF-2011-804":
                    affected.setdefault(item["id"], []).append(change)
    for identity, source_changes in affected.items():
        item = current_items[identity]
        before = (previous or {}).get("reference_parameters", {})
        parameter_diff = []
        if (
            item.get("report_code") == "DEF-2011-804"
            and before.get("status") == reference_parameters["status"] == "available"
        ):
            for field in sorted(set(before["values"]) | set(reference_parameters["values"])):
                prior, current = (
                    before["values"].get(field),
                    reference_parameters["values"].get(field),
                )
                if prior != current:
                    parameter_diff.append(
                        {
                            "field": field,
                            "before": prior,
                            "after": current,
                            "origin_kind": (current or {}).get("origin_kind", "unknown"),
                            "reason": "Observed change in the Reference applied input snapshot.",
                        }
                    )
        _once(
            service,
            PROPOSAL_KIND,
            {
                "schema": SCHEMA,
                "kind": "source_change",
                "asset_id": identity,
                "asset_snapshot": item,
                "asset_fingerprint": digest(item),
                "parent_revision_id": item["revision_id"],
                "name": "Review source change",
                "scan_id": record["id"],
                "changes": parameter_diff,
                "source_changes": source_changes,
                "parameter_diff_status": "compared"
                if before.get("status") == reference_parameters["status"] == "available"
                else "unavailable",
                "source_fingerprint": digest(sources),
                "source_binding": None,
                "baseline_electrical_model": None,
                "applied_inputs": {},
                "input_evidence": _observations(service, item),
                "scope": "Evidence snapshot only; source changes were not mapped "
                "to an executable model.",
                "execution_supported": False,
                "unsupported_reasons": [
                    "Source changes require explicit model mapping; no defaults are promoted."
                ],
            },
        )
    return {**record, "unchanged": False}


def propose(service, payload):
    allowed = {"asset_id", "name", "changes", "source_run_id"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Proposal accepts asset_id, name, changes and optional source_run_id.")
    item = _asset(service, payload.get("asset_id"))
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError("Proposal name requires 1..120 characters.")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 16:
        raise ValueError("Proposal requires 1..16 explicit changes.")
    normalized, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {
            "field",
            "value",
            "origin_kind",
            "reason",
        }:
            raise ValueError("Each change requires field, value, origin_kind and reason.")
        field, value = change["field"], change["value"]
        if not isinstance(field, str) or not field or len(field) > 100 or field in seen:
            raise ValueError("Changed fields must be unique nonempty names.")
        if change["origin_kind"] != "user_assumption":
            raise ValueError("Manual changes remain user_assumption, never reported or measured.")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Changed values must be finite numbers.")
        if field in FIELDS and not FIELDS[field][0] <= value <= FIELDS[field][1]:
            raise ValueError("Readout value is outside the electrical model bounds.")
        if not isinstance(change["reason"], str) or not 1 <= len(change["reason"].strip()) <= 2000:
            raise ValueError("Every change needs a reason, up to 2000 characters.")
        seen.add(field)
        normalized.append({**change, "value": float(value), "reason": change["reason"].strip()})
    normalized.sort(key=lambda c: c["field"])
    unsupported = []
    if not seen <= set(FIELDS):
        unsupported.append(
            "The first execution adapter supports only read_noise_e and dark_current_e_per_s."
        )
    source, cell = None, None
    try:
        source, cell, _ = _reference(service, item, payload.get("source_run_id"))
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        unsupported.append(_safe(str(exc)))
    baseline = copy.deepcopy(cell["electrical_model"]) if cell else None
    applied = {k: baseline[k] for k in FIELDS} if baseline else {}
    applied.update({c["field"]: c["value"] for c in normalized if c["field"] in FIELDS})
    return _once(
        service,
        PROPOSAL_KIND,
        {
            "schema": SCHEMA,
            "kind": "parameter_change",
            "asset_id": item["id"],
            "asset_snapshot": item,
            "asset_fingerprint": digest(item),
            "parent_revision_id": item["revision_id"],
            "name": name.strip(),
            "changes": normalized,
            "source_binding": source,
            "baseline_electrical_model": baseline,
            "applied_inputs": applied,
            "input_evidence": _observations(service, item),
            "execution_supported": not unsupported,
            "unsupported_reasons": unsupported,
            "scope": "Explicit user-assumed revision; "
            "no source DB mutation or product calibration.",
        },
    )


def adopt(service, proposal_id):
    proposal = service.registry.get(PROPOSAL_KIND, proposal_id)
    item = _asset(service, proposal["asset_id"])
    if digest(item) != proposal["asset_fingerprint"]:
        raise ConflictError("Source changed after the proposal; create a new proposal.")
    if proposal.get("source_fingerprint") and proposal["source_fingerprint"] != digest(
        _source_snapshot(service)
    ):
        raise ConflictError("Registered inputs changed after the source proposal.")
    if proposal["execution_supported"]:
        source, cell, _ = _reference(service, item, proposal["source_binding"]["run_id"])
        if (
            source != proposal["source_binding"]
            or cell["electrical_model"] != proposal["baseline_electrical_model"]
        ):
            raise ConflictError(
                "Reference inputs changed after the proposal; no adoption occurred."
            )
    return _once(
        service,
        MODEL_KIND,
        {
            k: copy.deepcopy(proposal[k])
            for k in (
                "schema",
                "asset_id",
                "asset_snapshot",
                "asset_fingerprint",
                "parent_revision_id",
                "name",
                "changes",
                "source_binding",
                "baseline_electrical_model",
                "applied_inputs",
                "input_evidence",
                "execution_supported",
                "unsupported_reasons",
                "scope",
            )
        }
        | {
            "proposal_id": proposal_id,
            "source_changes": copy.deepcopy(proposal.get("source_changes", [])),
            "source_fingerprint": proposal.get("source_fingerprint"),
        },
    )


def check(service, model_id, progress=None, job_id=None):
    if progress:
        progress({"stage": "checking_pinned_model"})
    model = service.registry.get(MODEL_KIND, model_id)
    sources_before = digest(_source_snapshot(service))
    issues = list(model.get("unsupported_reasons", []))
    if not model["execution_supported"] and not issues:
        issues.append("The adopted revision has no supported execution adapter.")
    try:
        item = _asset(service, model["asset_id"])
        if digest(item) != model["asset_fingerprint"]:
            raise ConflictError("Adopted source revision or evidence changed.")
        if model["execution_supported"]:
            source, cell, _ = _reference(service, item, model["source_binding"]["run_id"])
            if (
                source != model["source_binding"]
                or cell["electrical_model"] != model["baseline_electrical_model"]
            ):
                raise ConflictError("Pinned Reference acquisition or applied inputs changed.")
            from .electrical import validate_electrical_model

            validate_electrical_model(
                {**model["baseline_electrical_model"], **model["applied_inputs"]}
            )
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        issues.append(_safe(str(exc)))
    sources_after = digest(_source_snapshot(service))
    if sources_before != sources_after:
        issues.append("Registered source inputs changed while checking; retry this check.")
    if progress:
        progress({"stage": "publishing_model_check"})
    return service.registry.put(
        CHECK_KIND,
        {
            "schema": SCHEMA,
            "model_id": model_id,
            "job_id": job_id,
            "asset_id": model["asset_id"],
            "model_content_hash": model["content_hash"],
            "source_fingerprint": sources_after,
            "status": "blocked" if issues else "passed",
            "execution_ready": not issues,
            "issues": list(dict.fromkeys(issues)),
            "physical_validation_pass": False,
            "scope": "Source and adapter readiness only. "
            "A successful run or physical accuracy is not established.",
        },
    )


def link_validation(service, payload):
    if not isinstance(payload, dict) or set(payload) != {"model_id", "validation_id"}:
        raise ValueError("Validation link requires model_id and validation_id.")
    model = service.registry.get(MODEL_KIND, payload["model_id"])
    validation = service.registry.get("validations", payload["validation_id"])
    run = service.registry.get_run(validation["run_id"])
    if (
        run.get("design_asset_model_id") != model["id"]
        or run.get("design_asset_model_content_hash") != model["content_hash"]
    ):
        raise ValueError("Validation is not bound to this exact adopted model revision.")
    measurement = service.registry.get("measurements", validation["measurement_id"])
    test = service.registry.get("tests", validation["test_id"])
    expected = {
        "run": run["content_hash"],
        "measurement": measurement["content_hash"],
        "test": test["content_hash"],
    }
    if validation.get("evidence_hashes") != expected:
        raise IntegrityError("Validation source hashes no longer match its retained evidence.")
    physical = bool(
        validation.get("physical_validation_pass")
        and validation.get("comparison_valid")
        and validation.get("status") == "PASS"
        and measurement.get("source_kind") == "measured"
        and measurement.get("physical_instance")
        and measurement.get("instrument", {}).get("id")
        and measurement.get("instrument", {}).get("calibration_revision")
        and test.get("validation_kind") == "physical_validation"
    )
    return _once(
        service,
        LINK_KIND,
        {
            "schema": SCHEMA,
            "asset_id": model["asset_id"],
            "model_id": model["id"],
            "model_content_hash": model["content_hash"],
            "validation_id": validation["id"],
            "validation_content_hash": validation["content_hash"],
            "physical_match": physical,
            "status": "scoped_match" if physical else "no_physical_match",
            "metric_id": test.get("metric_id"),
            "conditions": _safe(measurement.get("conditions", {})),
            "valid_domain": _safe(measurement.get("valid_domain", {})),
            "scope": "Agreement applies only to the linked model, metric and declared conditions; "
            "other properties remain unvalidated.",
        },
    )

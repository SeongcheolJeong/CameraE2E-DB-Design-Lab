"""Discover existing design assets and save immutable, same-family shortlists.

The source component databases remain read-only. Only the platform Registry is
written when an engineer explicitly saves a shortlist.
"""

from __future__ import annotations

import copy
import math
import sqlite3

from fastapi import HTTPException, Request

from . import component_explorer_catalog as assets
from . import component_explorer_results as evidence
from .registry import ConflictError, digest

ROUTE = "/api/v1/component-explorer"
KIND = "component_explorer_shortlists"
SCHEMA = "camera_component_shortlist_v1"
RANGES = {
    "focal_length": "focal_length_mm",
    "f_number": "f_number",
    "pixel_pitch": "pixel_pitch_um",
    "stack_height": "optical_stack_height_um",
}
FILTERS = {
    "family",
    "q",
    "manufacturer",
    "cfa_pattern",
    "pixel_architecture",
    "has_results",
    "limit",
    "offset",
    *(prefix + suffix for prefix in RANGES for suffix in ("_min", "_max")),
}


def _index(service):
    try:
        return assets.index(service.workspace)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            503, "Design source catalog is unavailable; saved shortlists remain accessible."
        ) from exc


def catalog(service, filters=None):
    filters = dict(filters or {})
    if set(filters) - FILTERS:
        raise ValueError("Unsupported asset filters: " + ", ".join(sorted(set(filters) - FILTERS)))
    family = filters.get("family", "")
    if family not in ("", "lens", "sensor"):
        raise ValueError("family must be lens or sensor")
    limits = {}
    for prefix, key in RANGES.items():
        for suffix in ("_min", "_max"):
            value = filters.get(prefix + suffix)
            if value not in (None, ""):
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError("Numeric filters must be finite and nonnegative")
                limits[(key, suffix)] = number
        lo, hi = limits.get((key, "_min")), limits.get((key, "_max"))
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("Minimum cannot exceed maximum")
    has_results = str(filters.get("has_results", "false")).lower()
    if has_results not in ("true", "false", "1", "0"):
        raise ValueError("has_results must be true or false")
    try:
        limit, offset = int(filters.get("limit", 5000)), int(filters.get("offset", 0))
    except (ValueError, TypeError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    if not 1 <= limit <= 10000 or offset < 0:
        raise ValueError("limit must be 1..10000 and offset must be nonnegative")
    result = copy.deepcopy(_index(service))
    linked = evidence.bindings(service, result["items"])
    for item in result["items"]:
        item["results"] = linked.get(item["id"], [])
        item["has_results"] = any(r.get("available") for r in item["results"])
    result["counts"]["assets_with_results"] = sum(i["has_results"] for i in result["items"])
    query = str(filters.get("q", "")).strip().casefold()

    def matches(item):
        params = item["parameters"]
        if family and item["family"] != family:
            return False
        if has_results in ("true", "1") and not item["has_results"]:
            return False
        if (
            query
            and query
            not in " ".join(
                str(item.get(k, ""))
                for k in (
                    "name",
                    "manufacturer",
                    "source_label",
                    "id",
                    "component_id",
                    "revision_id",
                )
            ).casefold()
        ):
            return False
        for key in ("manufacturer", "cfa_pattern", "pixel_architecture"):
            wanted = filters.get(key)
            actual = item.get(key) if key == "manufacturer" else params.get(key)
            if wanted and str(actual or "").casefold() != str(wanted).casefold():
                return False
        for (key, suffix), value in limits.items():
            actual = params.get(key)
            if actual is None or not isinstance(actual, (int, float)) or not math.isfinite(actual):
                return False
            if (suffix == "_min" and actual < value) or (suffix == "_max" and actual > value):
                return False
        return True

    selected = [i for i in result["items"] if matches(i)]
    result.update(
        items=selected[offset : offset + limit], total=len(selected), limit=limit, offset=offset
    )
    result["scope"] = "Existing design evidence; counts are not product or validated model counts."
    return result


def detail(service, identity):
    item = copy.deepcopy(_item(service, identity))
    try:
        result = copy.deepcopy(assets.detail(service.workspace, identity))
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "Design source catalog is unavailable.") from exc
    if result["content_sha256"] != item["content_sha256"]:
        raise ConflictError("Selected source changed while loading; reload the catalog")
    result["item"] = item
    result["results"] = evidence.bindings(service, [item]).get(identity, [])
    item["results"] = result["results"]
    return result


def _item(service, identity):
    for item in _index(service)["items"]:
        if item["id"] == identity:
            return item
    raise KeyError("Design asset is no longer in the catalog")


def _text(payload, key, maximum, *, required=True):
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{key} must be text with 1..{maximum} characters")
    return value.strip()


def save_shortlist(service, payload):
    if not isinstance(payload, dict) or set(payload) - {"name", "question", "items"}:
        raise ValueError("Shortlist accepts name, question, items")
    name = _text(payload, "name", 120)
    question = _text(payload, "question", 2000)
    selections = payload.get("items")
    if not isinstance(selections, list) or not 1 <= len(selections) <= 4:
        raise ValueError("Select one to four assets of the same family")
    ids, families, retained = set(), set(), []
    for selected in selections:
        if not isinstance(selected, dict) or set(selected) - {
            "asset_id",
            "revision_id",
            "content_sha256",
            "reason",
        }:
            raise ValueError(
                "Each candidate requires asset_id, revision_id, content_sha256, reason"
            )
        identity = _text(selected, "asset_id", 200)
        if identity in ids:
            raise ValueError("A shortlist cannot contain the same asset twice")
        item = _item(service, identity)
        if (
            selected.get("revision_id") != item["revision_id"]
            or selected.get("content_sha256") != item["content_sha256"]
        ):
            raise ConflictError("Selected asset revision changed. Reload it before saving.")
        families.add(item["family"])
        ids.add(identity)
        retained.append(
            {
                "asset_id": identity,
                "revision_id": item["revision_id"],
                "content_sha256": item["content_sha256"],
                "reason": _text(selected, "reason", 2000, required=False),
                "item": copy.deepcopy(item),
                "asset_fingerprint": digest(item),
            }
        )
    if len(families) != 1:
        raise ValueError("Compare lenses with lenses or sensors with sensors")
    linked = evidence.bindings(service, [entry["item"] for entry in retained])
    for entry in retained:
        entry["result_references"] = copy.deepcopy(linked.get(entry["asset_id"], []))
        if digest(_item(service, entry["asset_id"])) != entry["asset_fingerprint"]:
            raise ConflictError("Selected source changed while saving the shortlist")
    return service.registry.put(
        KIND,
        {
            "schema": SCHEMA,
            "name": name,
            "question": question,
            "family": next(iter(families)),
            "items": retained,
            "catalog_snapshot_id": _index(service)["snapshot_id"],
            "scope": "Engineering shortlist, not a performance ranking or product recommendation",
        },
    )


def get_shortlist(service, identity):
    saved = copy.deepcopy(service.registry.get(KIND, identity))
    source_available = True
    try:
        current = {item["id"]: item for item in assets.index(service.workspace)["items"]}
    except (OSError, sqlite3.Error):
        current, source_available = {}, False
    for selected in saved["items"]:
        item = current.get(selected["asset_id"])
        selected["current_status"] = (
            "source_unavailable"
            if not source_available
            else "missing"
            if item is None
            else "current"
            if digest(item) == selected["asset_fingerprint"]
            else "stale"
        )
        selected["current_revision_id"] = item["revision_id"] if item else None
    linked = (
        evidence.bindings(
            service,
            [current[i["asset_id"]] for i in saved["items"] if i["current_status"] == "current"],
        )
        if source_available
        else {}
    )
    for selected in saved["items"]:
        selected["current_result_references"] = copy.deepcopy(linked.get(selected["asset_id"], []))
        selected["results_verified"] = False
    saved["source_available"] = source_available
    saved["all_current"] = all(i["current_status"] == "current" for i in saved["items"])
    return saved


def install_routes(app, service):
    @app.get(ROUTE + "/catalog", tags=["component_explorer"])
    def listing(request: Request):
        return catalog(service, dict(request.query_params))

    @app.get(ROUTE + "/assets/{identity}", tags=["component_explorer"])
    def asset(identity: str):
        return detail(service, identity)

    @app.get(ROUTE + "/assets/{identity}/results/{result_id}", tags=["component_explorer"])
    def result(identity: str, result_id: str):
        return evidence.validate_result(service, _item(service, identity), result_id)

    @app.get(ROUTE + "/shortlists", tags=["component_explorer"])
    def shortlists():
        return {"shortlists": service.registry.list(KIND)}

    @app.post(ROUTE + "/shortlists", status_code=201, tags=["component_explorer"])
    def save(payload: dict):
        return save_shortlist(service, payload)

    @app.get(ROUTE + "/shortlists/{identity}", tags=["component_explorer"])
    def shortlist(identity: str):
        return get_shortlist(service, identity)

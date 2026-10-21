"""Bind a declared sensor active mode to an immutable Registry artifact.

The library preflight accepts a local source path for offline engineering use.
This adapter is the path-free boundary for a future API: callers register the
declaration bytes once, then refer only to its immutable record ID. It never
turns an engineering declaration into verified product geometry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Request

from .lens_sensor_fullfield_study_preflight import (
    _CORE_KEYS,
    DECLARATION_SCHEMA,
    MODE_SCHEMA,
    _core,
    preflight_fullfield_study,
)
from .registry import IntegrityError, SQLiteRegistry, canonical
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_registered_active_mode_declaration_v1"
KIND = "active_mode_declarations"
MAX_DECLARATION_BYTES = 1024 * 1024
MAX_DECLARATION_REQUEST_BYTES = MAX_DECLARATION_BYTES + 4096
# This entry point receives the engineer's JSON values, not an external
# document. Claiming an external reference here would misstate provenance.
SOURCE_KINDS = {"engineering_declaration"}


def _jsonable(value: Any) -> Any:
    """Return plain JSON types before a result crosses the HTTP boundary."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def register_active_mode_declaration(
    registry: SQLiteRegistry,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    active_mode: Mapping[str, Any],
    source_kind: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Save a user declaration by digest after binding its saved sensor run."""
    if not isinstance(registry, SQLiteRegistry):
        raise ValueError("A local immutable Registry is required.")
    if (
        not isinstance(sensor_identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", sensor_identifier)
        or not isinstance(sensor_run_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sensor_run_id)
    ):
        raise ValueError("Choose a bounded saved sensor revision identifier and run hash.")
    if not isinstance(active_mode, Mapping) or set(active_mode) != _CORE_KEYS:
        raise ValueError("Provide the active-mode fields only, without a local source path.")
    if active_mode.get("schema") != MODE_SCHEMA:
        raise ValueError("Active-mode schema is unsupported.")
    if (
        not isinstance(active_mode.get("mode_id"), str)
        or not 1 <= len(active_mode["mode_id"].strip()) <= 128
    ):
        raise ValueError("Active-mode ID must contain 1..128 characters.")
    if not isinstance(source_kind, str) or source_kind not in SOURCE_KINDS:
        raise ValueError("Source kind must identify an unverified declaration.")
    try:
        inspection = inspect_sensor_revision(sensor_identifier, root=root, run_id=sensor_run_id)
    except (KeyError, FileNotFoundError) as exc:
        raise ValueError("Selected saved sensor revision is unavailable.") from exc
    if inspection.get("executable") is not True:
        raise ValueError("Selected saved sensor revision is not executable.")
    revision = inspection["revision"]
    _core(
        {**active_mode, "source": {}},
        revision,
        inspection.get("native_response", {}).get("native_cfa"),
    )
    declaration = {"schema": DECLARATION_SCHEMA, "active_mode": dict(active_mode)}
    try:
        contents = canonical(declaration).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Active-mode declaration must be finite JSON.") from exc
    if len(contents) > MAX_DECLARATION_BYTES:
        raise ValueError("Active-mode declaration exceeds 1 MiB.")
    artifact = registry.store_blob(
        contents, "active-mode-declaration.json", "application/json"
    )
    return registry.put(KIND, {
        "schema": SCHEMA,
        "mode_id": active_mode["mode_id"],
        "sensor_identifier": sensor_identifier,
        "sensor_run_id": sensor_run_id,
        "sensor_result_sha256": revision["result_sha256"],
        "source_kind": source_kind,
        "declaration_artifact": artifact,
        "declaration_artifact_sha256": artifact["sha256"],
        "declaration_artifact_bytes": len(contents),
        "physical_mode_verified": False,
    })


def _unavailable(
    lens_revision_id: str,
    sensor_identifier: str,
    sensor_run_id: str,
    *,
    root: str | Path | None,
    reason: str,
) -> dict[str, Any]:
    baseline = preflight_fullfield_study(
        lens_revision_id, sensor_identifier,
        sensor_run_id=sensor_run_id, active_mode=None, root=root,
    )
    return _jsonable({
        **baseline,
        "status": "blocked",
        "reason": reason,
        "registered_declaration": None,
    })


def preflight_registered_fullfield_study(
    registry: SQLiteRegistry,
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    declaration_id: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Check saved model support from an artifact ID, never a caller path."""
    if not isinstance(registry, SQLiteRegistry):
        raise ValueError("A local immutable Registry is required.")
    if not isinstance(declaration_id, str) or not declaration_id:
        raise ValueError("A registered active-mode declaration ID is required.")
    try:
        record = registry.get(KIND, declaration_id)
        if (
            record.get("schema") != SCHEMA
            or record.get("sensor_identifier") != sensor_identifier
            or record.get("sensor_run_id") != sensor_run_id
            or not isinstance(record.get("source_kind"), str)
            or record["source_kind"] not in SOURCE_KINDS
        ):
            raise ValueError("Declaration is not bound to the selected saved sensor.")
        checksum = record["declaration_artifact_sha256"]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
            or (registry.blobs / checksum).is_symlink()
        ):
            raise ValueError("Registered declaration artifact ID is invalid.")
        binding = registry.artifact_binding(
            checksum, owner_kind=KIND, owner_id=record["id"]
        )
        if binding["descriptor"] != record.get("declaration_artifact"):
            raise ValueError("Registered declaration artifact owner differs.")
        path = registry.blob_path(checksum)
        contents = path.read_bytes()
        if len(contents) > MAX_DECLARATION_BYTES:
            raise ValueError("Registered declaration exceeds 1 MiB.")
        declaration = json.loads(contents)
        if (
            not isinstance(declaration, dict)
            or set(declaration) != {"schema", "active_mode"}
            or declaration["schema"] != DECLARATION_SCHEMA
            or not isinstance(declaration["active_mode"], dict)
            or declaration["active_mode"].get("mode_id") != record["mode_id"]
            or declaration["active_mode"].get("sensor_result_sha256")
            != record["sensor_result_sha256"]
        ):
            raise ValueError("Registered declaration content changed or is malformed.")
    except (KeyError, IntegrityError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return _unavailable(
            lens_revision_id, sensor_identifier, sensor_run_id, root=root,
            reason="registered_active_mode_artifact_unavailable_or_invalid",
        )

    active_mode = {
        **declaration["active_mode"],
        "source": {
            "kind": record["source_kind"],
            "path": str(path),
            "sha256": checksum,
        },
    }
    result = deepcopy(preflight_fullfield_study(
        lens_revision_id, sensor_identifier,
        sensor_run_id=sensor_run_id, active_mode=active_mode, root=root,
    ))
    # The underlying offline checker uses the internal blob path twice to
    # detect mutation. No filesystem location belongs in an API result.
    artifact_ref = f"registry-artifact://sha256/{checksum}"
    if isinstance(result.get("source_validation"), dict):
        result["source_validation"].pop("path", None)
        result["source_validation"]["artifact_reference"] = artifact_ref
    fov = result.get("geometric_fov")
    if isinstance(fov, dict) and isinstance(fov.get("active_mode"), dict):
        source = fov["active_mode"].get("geometry_source")
        if isinstance(source, dict):
            source["reference"] = artifact_ref
    result["registered_declaration"] = {
        "id": record["id"],
        "artifact_sha256": checksum,
        "source_kind": record["source_kind"],
        "physical_mode_verified": False,
    }
    return _jsonable(result)


def install_routes(app: Any, service: Any) -> None:
    """Expose only pinned declaration IDs across the authenticated API."""
    root = Path(service.workspace) / "camerae2e_db"

    @app.post("/api/v1/lens-sensor-active-modes", status_code=201,
              tags=["lens_sensor_fov"])
    async def register(request: Request) -> dict[str, Any]:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_DECLARATION_REQUEST_BYTES:
                raise ValueError("Active-mode request body exceeds the 1 MiB policy.")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Active-mode request requires a JSON object.") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "sensor_identifier", "sensor_run_id", "active_mode", "source_kind"
        }:
            raise ValueError("Declare sensor revision, active mode and source kind exactly.")
        return register_active_mode_declaration(
            service.registry, payload["sensor_identifier"],
            sensor_run_id=payload["sensor_run_id"],
            active_mode=payload["active_mode"], source_kind=payload["source_kind"],
            root=root,
        )

    @app.get("/api/v1/lens-sensor-active-modes/{identity}", tags=["lens_sensor_fov"])
    def detail(identity: str) -> dict[str, Any]:
        return service.registry.get(KIND, identity)

    @app.post("/api/v1/lens-sensor-fov/registered-inspect", tags=["lens_sensor_fov"])
    def inspect(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {
            "lens_revision_id", "sensor_identifier", "sensor_run_id", "declaration_id"
        }:
            raise ValueError("Declare pinned lens, sensor and declaration IDs exactly.")
        return preflight_registered_fullfield_study(
            service.registry, payload["lens_revision_id"], payload["sensor_identifier"],
            sensor_run_id=payload["sensor_run_id"],
            declaration_id=payload["declaration_id"], root=root,
        )

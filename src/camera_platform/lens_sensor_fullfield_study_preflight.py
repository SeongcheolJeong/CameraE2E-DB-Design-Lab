"""Fail-closed input contract for a *planned* full-active-field tile study.

The saved sensor simulation is a local 32 x 32 response crop.  A reported
megapixel count, die size, or extracted PDAF unit-cell dimension does not give
the active sensor rectangle.  This module accepts only an explicit, hash-pinned
mode declaration and a pinned geometric Lens/Sensor DB pair.  Even a successful
check establishes saved-model field coverage, not a runnable full-frame study
or a physically qualified lens/sensor product.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .lens_sensor_fov_feasibility import (
    MODE_SCHEMA as FOV_MODE_SCHEMA,
)
from .lens_sensor_fov_feasibility import (
    inspect_db_full_sensor_fov,
)
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_fullfield_tile_study_preflight_v1"
MODE_SCHEMA = "camerae2e_fullfield_tile_study_active_mode_v1"
DECLARATION_SCHEMA = "camerae2e_fullfield_tile_study_mode_declaration_v1"
_SOURCE_MAX_BYTES = 1024 * 1024
_CORE_KEYS = {
    "schema",
    "mode_id",
    "sensor_run_id",
    "sensor_report_code",
    "sensor_result_sha256",
    "active_rows",
    "active_cols",
    "pixel_pitch_um",
    "sensor_center_offset_from_optical_axis_um_row_col",
    "sensor_plane",
    "sensor_orientation_deg_clockwise",
    "cfa_origin_row_col",
    "image_plane_binding",
}
_MODE_KEYS = _CORE_KEYS | {"source"}


class _ContractError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _pair(value: Any, *, integer: bool, name: str) -> list[int] | list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _ContractError("active_mode_geometry_invalid", f"{name} needs two values.")
    if integer:
        if any(type(item) is not int or not 0 <= item <= 1_000_000 for item in value):
            raise _ContractError(
                "active_mode_geometry_invalid", f"{name} needs two nonnegative integers."
            )
        return [int(value[0]), int(value[1])]
    if any(not _finite_number(item) for item in value):
        raise _ContractError("active_mode_geometry_invalid", f"{name} needs finite numbers.")
    return [float(value[0]), float(value[1])]


def _core(
    active_mode: Mapping[str, Any],
    revision: Mapping[str, Any],
    native_cfa: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if set(active_mode) != _MODE_KEYS:
        raise _ContractError(
            "active_mode_fields_incomplete",
            "A complete active-mode, mount, CFA and source contract is required.",
        )
    core = {key: active_mode[key] for key in _CORE_KEYS}
    if core["schema"] != MODE_SCHEMA or not isinstance(core["mode_id"], str) or not core[
        "mode_id"
    ].strip():
        raise _ContractError("active_mode_identity_invalid", "Mode schema or ID is invalid.")
    for field, expected in (
        ("sensor_run_id", revision["run_id"]),
        ("sensor_report_code", revision["report_code"]),
        ("sensor_result_sha256", revision["result_sha256"]),
    ):
        if core[field] != expected:
            raise _ContractError(
                "active_mode_sensor_revision_mismatch",
                "Active mode is not bound to the selected saved sensor revision.",
            )
    if any(
        type(core[key]) is not int or not 2 <= core[key] <= 1_000_000
        for key in ("active_rows", "active_cols")
    ):
        raise _ContractError(
            "active_mode_geometry_invalid", "Active rows and columns need explicit counts."
        )
    if not _finite_number(core["pixel_pitch_um"]) or core["pixel_pitch_um"] <= 0:
        raise _ContractError("active_mode_geometry_invalid", "Pixel pitch must be positive.")
    core["sensor_center_offset_from_optical_axis_um_row_col"] = _pair(
        core["sensor_center_offset_from_optical_axis_um_row_col"],
        integer=False,
        name="Optical-axis offset",
    )
    if core["sensor_plane"] != "perpendicular_to_optical_axis":
        raise _ContractError("sensor_plane_unsupported", "Only a perpendicular plane is modeled.")
    if type(core["sensor_orientation_deg_clockwise"]) is not int or core[
        "sensor_orientation_deg_clockwise"
    ] not in (0, 90, 180, 270):
        raise _ContractError(
            "sensor_orientation_unsupported", "Declare a right-angle sensor orientation."
        )
    core["cfa_origin_row_col"] = _pair(
        core["cfa_origin_row_col"], integer=True, name="CFA origin"
    )
    pattern = native_cfa.get("pattern") if isinstance(native_cfa, Mapping) else None
    if (
        not isinstance(pattern, list)
        or not pattern
        or not all(isinstance(row, list) and len(row) == len(pattern[0]) for row in pattern)
        or len(pattern[0]) < 1
    ):
        raise _ContractError(
            "saved_native_cfa_unavailable", "Saved sensor CFA period is unavailable."
        )
    period = (len(pattern), len(pattern[0]))
    if core["sensor_orientation_deg_clockwise"] in (90, 270):
        period = period[::-1]
    if any(
        origin >= limit
        for origin, limit in zip(core["cfa_origin_row_col"], period, strict=True)
    ):
        raise _ContractError(
            "cfa_origin_outside_period", "CFA origin must lie inside the saved CFA period."
        )
    binding = core["image_plane_binding"]
    if not isinstance(binding, Mapping) or set(binding) != {
        "reference_plane", "axial_offset_um"
    }:
        raise _ContractError(
            "image_plane_binding_missing", "Declare the saved image plane and axial offset."
        )
    if binding["reference_plane"] != "saved_prescription_image_plane" or not _finite_number(
        binding["axial_offset_um"]
    ):
        raise _ContractError(
            "image_plane_binding_invalid", "Image-plane reference or offset is invalid."
        )
    if float(binding["axial_offset_um"]) != 0.0:
        raise _ContractError(
            "axial_offset_unsupported",
            "Only zero offset from the saved prescription image plane is supported.",
        )
    core["image_plane_binding"] = {
        "reference_plane": "saved_prescription_image_plane",
        "axial_offset_um": 0.0,
    }
    return core


def _verified_source(
    source: Any, core: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(source, Mapping) or set(source) != {"kind", "path", "sha256"}:
        raise _ContractError(
            "mode_source_missing", "A source kind, absolute file path and SHA-256 are required."
        )
    kind, path_text, expected = source["kind"], source["path"], source["sha256"]
    if not isinstance(kind, str) or kind not in {
        "engineering_declaration", "external_reference_unverified"
    }:
        raise _ContractError("mode_source_kind_invalid", "Unsupported declaration source kind.")
    if not isinstance(path_text, str) or not path_text.strip():
        raise _ContractError("mode_source_path_invalid", "Source path must be nonempty.")
    path = Path(path_text)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise _ContractError(
            "mode_source_unavailable", "Mode declaration must be a regular local file."
        )
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise _ContractError("mode_source_sha256_invalid", "Source SHA-256 must be lowercase hex.")
    if path.stat().st_size > _SOURCE_MAX_BYTES:
        raise _ContractError("mode_source_too_large", "Mode declaration exceeds 1 MiB.")
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise _ContractError("mode_source_unavailable", "Mode declaration cannot be read.") from exc
    if len(contents) > _SOURCE_MAX_BYTES:
        raise _ContractError("mode_source_too_large", "Mode declaration exceeds 1 MiB.")
    actual = hashlib.sha256(contents).hexdigest()
    if actual != expected:
        raise _ContractError(
            "mode_source_digest_mismatch", "Mode declaration changed from its pinned SHA-256."
        )
    try:
        declaration = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ContractError("mode_source_invalid_json", "Mode declaration is not JSON.") from exc
    try:
        values_match = json.dumps(
            declaration.get("active_mode"), sort_keys=True, allow_nan=False
        ) == json.dumps(core, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, AttributeError):
        values_match = False
    if (
        not isinstance(declaration, dict)
        or set(declaration) != {"schema", "active_mode"}
        or declaration["schema"] != DECLARATION_SCHEMA
        or not isinstance(declaration["active_mode"], dict)
        or not values_match
    ):
        raise _ContractError(
            "mode_source_values_mismatch",
            "Pinned declaration does not contain the submitted active-mode values.",
        )
    evidence = {
        "kind": kind,
        "path": str(path.resolve()),
        "sha256": actual,
        "file_hash_verified": True,
        "declaration_values_match": True,
        "physical_mode_verified": False,
    }
    return evidence, {
        "kind": kind,
        "reference": f"{evidence['path']}#sha256={actual}",
    }


def preflight_fullfield_study(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    active_mode: Mapping[str, Any] | None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Check a declared full active rectangle; never authorize capture or MTF.

    The declaration file must be a JSON object with ``schema`` equal to
    ``DECLARATION_SCHEMA`` and ``active_mode`` equal to the request's mode
    fields except ``source``.  Hash matching establishes a stable engineering
    declaration, not independent verification of the manufacturer's product.
    """
    try:
        baseline = inspect_db_full_sensor_fov(
            lens_revision_id,
            sensor_identifier,
            sensor_run_id=sensor_run_id,
            root=root,
        )
    except (ValueError, FileNotFoundError):
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "pinned_lens_sensor_revision_unavailable",
            "planning_ready": False,
            "execution_qualified": False,
            "full_frame_capture_qualified": False,
            "physical_mtf_allowed": False,
            "product_qualification_allowed": False,
            "lens_identity": None,
            "sensor_revision": None,
            "active_mode": None,
            "source_validation": None,
            "geometric_fov": None,
        }
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "not_evaluable",
        "reason": "full_sensor_active_mode_geometry_missing",
        "planning_ready": False,
        "execution_qualified": False,
        "full_frame_capture_qualified": False,
        "physical_mtf_allowed": False,
        "product_qualification_allowed": False,
        "lens_identity": baseline["lens_identity"],
        "sensor_revision": baseline["sensor_revision"],
        "active_mode": None,
        "source_validation": None,
        "geometric_fov": baseline,
    }
    if active_mode is None:
        return result
    if not isinstance(active_mode, Mapping):
        result.update(status="blocked", reason="active_mode_fields_incomplete")
        return result
    try:
        sensor_inspection = inspect_sensor_revision(
            sensor_identifier, root=root, run_id=sensor_run_id
        )
        core = _core(
            active_mode,
            baseline["sensor_revision"],
            sensor_inspection.get("native_response", {}).get("native_cfa"),
        )
        # Verify the declaration against the caller's exact JSON values.
        # Validation normalizes some legal integers to floats for calculations;
        # that normalization must not make a pinned, unchanged file fail.
        submitted_core = {key: active_mode[key] for key in _CORE_KEYS}
        source_validation, fov_source = _verified_source(
            active_mode["source"], submitted_core
        )
        fov_mode = {
            key: core[key]
            for key in (
                "mode_id", "sensor_run_id", "sensor_report_code",
                "sensor_result_sha256", "active_rows", "active_cols", "pixel_pitch_um",
                "sensor_center_offset_from_optical_axis_um_row_col", "sensor_plane",
            )
        }
        # The saved lens tables use optical row/column axes. A quarter-turn
        # swaps the sensor's declared active extents on those axes, especially
        # relevant when the optical axis has a nonzero row/column offset.
        if core["sensor_orientation_deg_clockwise"] in (90, 270):
            fov_mode["active_rows"], fov_mode["active_cols"] = (
                core["active_cols"], core["active_rows"]
            )
        fov_mode.update(schema=FOV_MODE_SCHEMA, geometry_source=fov_source)
        field = inspect_db_full_sensor_fov(
            lens_revision_id,
            sensor_identifier,
            sensor_run_id=sensor_run_id,
            active_mode=fov_mode,
            root=root,
        )
        source_path = Path(source_validation["path"])
        try:
            source_digest_after = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise _ContractError(
                "mode_source_changed_during_inspection",
                "Mode declaration disappeared during the geometric FOV check.",
            ) from exc
        if source_digest_after != source_validation["sha256"]:
            raise _ContractError(
                "mode_source_changed_during_inspection",
                "Mode declaration changed during the geometric FOV check.",
            )
    except _ContractError as exc:
        result.update(status="blocked", reason=exc.reason)
        return result
    except (ValueError, FileNotFoundError):
        result.update(status="blocked", reason="pinned_geometric_fov_contract_failed")
        return result
    result.update(
        active_mode=core,
        source_validation=source_validation,
        geometric_fov=field,
        active_extent_on_optical_axes_um_row_col=[
            fov_mode["active_rows"] * core["pixel_pitch_um"],
            fov_mode["active_cols"] * core["pixel_pitch_um"],
        ],
    )
    if field["status"] != "within_saved_model_support":
        result.update(
            status="blocked",
            reason=field.get("reason") or "outside_saved_geometric_field",
        )
        return result
    result.update(
        status="planning_only",
        reason="saved_geometric_field_support_only",
        planning_ready=True,
    )
    return result

"""Disk RAW for an off-axis ROI in a registered, unverified active-mode grid.

The full active shape fixes optical and CFA coordinates; only the selected ROI
is rendered and stored. A saved 32x32 sensor response is extended uniformly
over that declared grid as a model assumption, not as product validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from copy import deepcopy
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import numpy as np

from .lens_sensor_active_mode_asset import (
    KIND as ACTIVE_MODE_KIND,
)
from .lens_sensor_active_mode_asset import (
    MAX_DECLARATION_BYTES,
    preflight_registered_fullfield_study,
)
from .lens_sensor_fullfield_study_preflight import _CORE_KEYS, DECLARATION_SCHEMA, MODE_SCHEMA
from .lens_sensor_out_of_core_raw_job import (
    _ANGLES_DEG,
    _WAVELENGTHS_NM,
    _checked_npy,
    _db_root,
    _number,
    _pair,
    _reject_private_paths,
    _resource_policy,
    _work_root,
    cleanup_work_root,
)
from .lens_sensor_out_of_core_readout import (
    _OUTPUT_BYTES_PER_CELL,
    _POLICY_COLUMN_BYTES_PER_CELL,
    _POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL,
    _resident_payload_bytes,
    compute_out_of_core_readout,
    preflight_out_of_core_readout,
)
from .lens_sensor_registered_roi_work_budget import (
    RegisteredRoiActualWorkCounter,
    RegisteredRoiWorkPolicy,
    cheap_registered_roi_work_admission,
)
from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint
from .lens_sensor_streamed_analytic_capture import _optics_array_payload_bytes
from .lens_sensor_streamed_full_raw import _complete_tiles, _sensor_identity
from .lens_sensor_streamed_offaxis_mean import (
    _compute_preflighted_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_registered_roi_disk_raw_diagnostic_v1"
PENDING_SUBMISSION_SCHEMA = "camerae2e_registered_roi_worker_preflight_submission_v2"
JOB_KIND = "lens_sensor_registered_roi_disk_raw"
WORKER_TIMEOUT_S = 1800
_MAX_ACTIVE_AXIS = 65_536
_MAX_ACTIVE_CELLS = 268_435_456
_MAX_SCENE_AXIS = 65_536
_MAX_SCENE_CELLS = 536_870_912
_MAX_ROI_SIDE = 128
_MAX_ROI_CELLS = 16_384
_MAX_SENSOR_MODEL_ARRAY_BYTES = 16 * 1024**2
_TILE_EVIDENCE_RESERVE_BYTES = 64 * 1024
_METADATA_RESERVE_BYTES = 64 * 1024
_MAX_LENS_DB_HASH_BYTES = 256 * 1024**2
_MAX_PSF_HASH_BYTES = 128 * 1024**2
_MAX_SENSOR_RESULT_HASH_BYTES = 16 * 1024**2
_MAX_SENSOR_ARRAY_HASH_BYTES = 128 * 1024**2

LIMITATION = (
    "This is numerical RAW for one off-axis ROI in a registered engineering-declared "
    "active-mode coordinate system. The registered geometry and product mode are not "
    "independently verified. A saved db_estimate_v1 sensor response is assumed field "
    "uniform; pixel angle response and product peripheral behavior are unverified. "
    "The saved geometric PSF excludes diffraction; lens absolute transmission and "
    "relative illumination use unknown unity placeholders. Scene sampling convergence, "
    "physical FOV/MTF, full active-area capture, and measured accuracy are not established. "
    "Optical source tiles may be recalculated across sensor tiles. Admission budgets "
    "are policies, not hardware limits, peak RSS bounds, or runtime guarantees."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "등록 active mode의 오프축 ROI 디스크 RAW 진단",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "active_mode_declaration_id": "",
        "sensor_roi_bounds_row_col_exclusive": [64, 96, 128, 160],
        "analytic_scene": {
            "kind": "continuous_slanted_edge_d65",
            "object_distance_m": "infinity",
            "shape_row_col": [2048, 2048],
            "horizontal_fov_deg": 2.0,
            "bright_luminance_cd_m2": 1000.0,
            "edge_offset_deg": 0.0,
        },
        "capture": {"exposure_s": 0.004, "noise_flag": 0, "seed": 7},
        "sensor_tile_shape_row_col": [16, 16],
        "source_tile_shape_row_col": [64, 64],
        "readout_row_chunk_rows": 16,
        "accept_approximate_lens_radiometry": False,
        "acknowledge_unverified_mode_and_uniform_sensor_response": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    template = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(template):
        raise ValueError("Declare exactly the registered-mode ROI RAW fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported registered-mode ROI RAW schema.")
    if (
        payload["accept_approximate_lens_radiometry"] is not True
        or payload["acknowledge_unverified_mode_and_uniform_sensor_response"] is not True
    ):
        raise ValueError(
            "Explicit lens radiometry and unverified-mode acknowledgements are required."
        )
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    lens_id = payload["lens_revision_id"]
    if not isinstance(lens_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", lens_id):
        raise ValueError("Choose a Lens DB revision UUID, never a local path.")
    selected = payload["sensor_revision"]
    if (
        not isinstance(selected, dict)
        or set(selected) != {"identifier", "run_id"}
        or not isinstance(selected["identifier"], str)
        or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", selected["identifier"])
        or not isinstance(selected["run_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected["run_id"])
    ):
        raise ValueError("Choose one saved Sensor DB report code and run hash, never a path.")
    declaration_id = payload["active_mode_declaration_id"]
    if not isinstance(declaration_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", declaration_id
    ):
        raise ValueError("Choose a registered active-mode declaration ID, never a path.")
    bounds = payload["sensor_roi_bounds_row_col_exclusive"]
    if (
        not isinstance(bounds, list)
        or len(bounds) != 4
        or any(type(item) is not int or item < 0 or item > _MAX_ACTIVE_AXIS for item in bounds)
        or not (bounds[0] < bounds[1] and bounds[2] < bounds[3])
    ):
        raise ValueError("Sensor ROI requires four nonnegative half-open global bounds.")
    roi_rows, roi_cols = bounds[1] - bounds[0], bounds[3] - bounds[2]
    if (
        not 8 <= roi_rows <= _MAX_ROI_SIDE
        or not 8 <= roi_cols <= _MAX_ROI_SIDE
        or roi_rows * roi_cols > _MAX_ROI_CELLS
    ):
        raise ValueError("ROI exceeds the declared 8..128 axis / 16384-cell output policy.")
    scene = payload["analytic_scene"]
    if not isinstance(scene, dict) or set(scene) != set(template["analytic_scene"]):
        raise ValueError("Declare the complete analytic Scene.")
    if scene["kind"] != "continuous_slanted_edge_d65" or scene["object_distance_m"] != "infinity":
        raise ValueError("Only the infinity-conjugate continuous D65 slanted edge is supported.")
    sr, sc = _pair(scene["shape_row_col"], "analytic_scene.shape_row_col", 32, _MAX_SCENE_AXIS)
    if sr * sc > _MAX_SCENE_CELLS:
        raise ValueError("Analytic Scene exceeds the declared virtual-grid cell policy.")
    fov = _number(scene["horizontal_fov_deg"], "horizontal_fov_deg", 0.001, 90.0)
    luminance = _number(scene["bright_luminance_cd_m2"], "bright_luminance_cd_m2", 0.01, 10000.0)
    edge = _number(scene["edge_offset_deg"], "edge_offset_deg", -45.0, 45.0)
    if abs(edge) > fov / 2:
        raise ValueError("Edge offset must lie inside the declared Scene half-FOV.")
    capture = payload["capture"]
    if not isinstance(capture, dict) or set(capture) != set(template["capture"]):
        raise ValueError("Declare fixed exposure, noise flag and seed.")
    exposure = _number(capture["exposure_s"], "exposure_s", 1e-6, 1.0)
    if type(capture["noise_flag"]) is not int or capture["noise_flag"] not in {0, 2}:
        raise ValueError("noise_flag must be 0 or 2.")
    if type(capture["seed"]) is not int or not 0 <= capture["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    tile = _pair(payload["sensor_tile_shape_row_col"], "sensor_tile_shape_row_col", 2, 64)
    if tile[0] > roi_rows or tile[1] > roi_cols:
        raise ValueError("Sensor tile cannot exceed the ROI.")
    _pair(payload["source_tile_shape_row_col"], "source_tile_shape_row_col", 16, 256)
    chunks = payload["readout_row_chunk_rows"]
    if type(chunks) is not int or not 1 <= chunks <= 256 or chunks >= roi_rows:
        raise ValueError("Readout row chunk must be smaller than ROI rows.")
    result = deepcopy(payload)
    result["name"] = result["name"].strip()
    result["lens_revision_id"] = lens_id.lower()
    result["analytic_scene"].update(
        horizontal_fov_deg=fov, bright_luminance_cd_m2=luminance, edge_offset_deg=edge
    )
    result["capture"]["exposure_s"] = exposure
    return result


def _bounded_file_sha256(path: Path, *, maximum_bytes: int, label: str) -> str:
    """Pin current bytes without decoding a PSF or loading a model array."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Pinned {label} is unavailable as a regular file.") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > maximum_bytes:
            raise ValueError(f"Pinned {label} exceeds the bounded submit-time hash policy.")
        checksum = hashlib.sha256()
        seen = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                seen += len(chunk)
                if seen > maximum_bytes:
                    raise ValueError(f"Pinned {label} grew beyond the hash policy.")
                checksum.update(chunk)
        final = os.fstat(descriptor)
        if (
            (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns,
             initial.st_ctime_ns)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns,
                final.st_ctime_ns)
        ):
            raise ValueError(f"Pinned {label} changed during submit-time hashing.")
        return checksum.hexdigest()
    finally:
        os.close(descriptor)


def _bounded_db_child(base: Path, relative: str, *, label: str) -> Path:
    """Resolve a DB-owned relative artifact with no symlink traversal."""
    item = Path(relative)
    if item.is_absolute() or not item.parts or ".." in item.parts:
        raise ValueError(f"Pinned {label} has an unsafe relative path.")
    root = base.resolve()
    candidate = root
    for part in item.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"Pinned {label} must not traverse a symlink.")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError(f"Pinned {label} escaped its DB root.")
    return candidate


def _bounded_lens_source_path(root: Path, uri: str) -> Path:
    """Resolve one imported source DB without a multi-candidate hash search."""
    parsed = urlparse(uri) if isinstance(uri, str) else None
    if (
        parsed is None or parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
        or parsed.query or parsed.fragment
    ):
        raise ValueError("Pinned Lens source URI must be a local file URI.")
    historical = Path(unquote(parsed.path))
    if not historical.is_absolute():
        raise ValueError("Pinned Lens source URI must be absolute.")
    if historical.is_relative_to(root):
        return _bounded_db_child(
            root, historical.relative_to(root).as_posix(), label="Lens source DB"
        )
    if (
        historical.parent.name != "lens_patents"
        or historical.parent.parent.name != "data"
        or historical.parent.parent.parent.parent.name != "lens_db"
        or re.fullmatch(r"lens_patent_simulation_v[0-9]+\.sqlite", historical.name) is None
    ):
        raise ValueError("Historical Lens source URI is not eligible for relocation.")
    lens_dir = root / "lens_db"
    if lens_dir.is_symlink():
        raise ValueError("Lens DB directory must not be a symlink.")
    candidates = list(islice(lens_dir.glob(f"*/data/lens_patents/{historical.name}"), 2))
    if len(candidates) != 1:
        raise ValueError(
            "Relocated Lens source needs one unambiguous local DB; reimport the revision."
        )
    return _bounded_db_child(
        root, candidates[0].relative_to(root).as_posix(), label="Lens source DB"
    )


def _cheap_registered_mode(service: Any, spec: dict[str, Any]) -> tuple[dict, dict]:
    """Check the immutable declaration and coordinate contract, without FOV/PSF work."""
    record = service.registry.get(ACTIVE_MODE_KIND, spec["active_mode_declaration_id"])
    selected = spec["sensor_revision"]
    checksum = record.get("declaration_artifact_sha256")
    if (
        record.get("schema") != "camerae2e_registered_active_mode_declaration_v1"
        or record.get("sensor_identifier") != selected["identifier"]
        or record.get("sensor_run_id") != selected["run_id"]
        or record.get("source_kind") != "engineering_declaration"
        or record.get("physical_mode_verified") is not False
        or not isinstance(checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
    ):
        raise ValueError("Registered active mode is not bound to the selected saved sensor.")
    owner = service.registry.artifact_binding(
        checksum, owner_kind=ACTIVE_MODE_KIND, owner_id=record["id"]
    )
    if owner["descriptor"] != record.get("declaration_artifact"):
        raise IntegrityError("Registered active-mode artifact owner differs.")
    path = service.registry.blob_path(checksum)
    if _bounded_file_sha256(
        path, maximum_bytes=MAX_DECLARATION_BYTES, label="active-mode declaration"
    ) != checksum:
        raise IntegrityError("Registered active-mode declaration bytes changed.")
    contents_bytes = path.read_bytes()
    if (
        len(contents_bytes) > MAX_DECLARATION_BYTES
        or hashlib.sha256(contents_bytes).hexdigest() != checksum
    ):
        raise IntegrityError("Registered active-mode declaration changed during parsing.")
    contents = json.loads(contents_bytes)
    if (
        not isinstance(contents, dict)
        or set(contents) != {"schema", "active_mode"}
        or contents["schema"] != DECLARATION_SCHEMA
        or not isinstance(contents["active_mode"], dict)
        or set(contents["active_mode"]) != _CORE_KEYS
    ):
        raise ValueError("Registered active-mode declaration values are malformed.")
    mode = contents["active_mode"]
    if (
        mode.get("schema") != MODE_SCHEMA
        or mode.get("mode_id") != record.get("mode_id")
        or mode.get("sensor_run_id") != selected["run_id"]
        or mode.get("sensor_result_sha256") != record.get("sensor_result_sha256")
        or mode.get("sensor_center_offset_from_optical_axis_um_row_col") != [0.0, 0.0]
        or mode.get("sensor_orientation_deg_clockwise") != 0
        or mode.get("cfa_origin_row_col") != [0, 0]
        or mode.get("image_plane_binding")
        != {"reference_plane": "saved_prescription_image_plane", "axial_offset_um": 0.0}
    ):
        raise ValueError("Registered mode is not the supported zero-offset pinned sensor mode.")
    rows, cols, pitch = mode.get("active_rows"), mode.get("active_cols"), mode.get("pixel_pitch_um")
    if (
        type(rows) is not int or type(cols) is not int
        or not 1 <= rows <= _MAX_ACTIVE_AXIS or not 1 <= cols <= _MAX_ACTIVE_AXIS
        or rows * cols > _MAX_ACTIVE_CELLS
        or type(pitch) not in (int, float) or not 0 < float(pitch) < 1000
    ):
        raise ValueError("Registered active grid exceeds the coordinate policy.")
    r0, r1, c0, c1 = spec["sensor_roi_bounds_row_col_exclusive"]
    if r1 > rows or c1 > cols:
        raise ValueError("Requested global ROI lies outside the registered active-mode rectangle.")
    binding = {
        "declaration_id": record["id"],
        "declaration_artifact_sha256": checksum,
        "source_kind": record["source_kind"],
        "mode_id": mode["mode_id"],
        "active_shape_row_col": [rows, cols],
        "pixel_pitch_um": float(pitch),
        "global_sensor_roi_bounds_row_col_exclusive": [r0, r1, c0, c1],
        "physical_mode_verified": False,
    }
    return mode, binding


def _cheap_db_binding(service: Any, spec: dict[str, Any], mode: dict) -> dict[str, Any]:
    """Hash bounded current source files; defer model compatibility to Worker."""
    from pyisetcam._catalog_store import connect
    from pyisetcam._catalog_store import digest as catalog_digest
    from pyisetcam.component_catalog import component_catalog_path
    from pyisetcam.sensor_simulation_db import (
        _connect as sensor_connect,
    )
    from pyisetcam.sensor_simulation_db import (
        sensor_simulation_db_path,
    )

    root = _db_root(service).resolve()
    with connect(component_catalog_path(root)) as conn:
        lens = conn.execute(
            """SELECT r.revision_id,r.model_kind,r.content_sha256,r.snapshot_json,
            s.kind AS source_kind,s.uri AS source_uri,s.sha256 AS source_sha256,
            c.family FROM model_revisions r
            JOIN components c ON c.component_id=r.component_id
            JOIN sources s ON s.source_id=r.source_id WHERE r.revision_id=?""",
            (spec["lens_revision_id"],),
        ).fetchone()
        assets = conn.execute(
            """SELECT a.path,a.sha256 FROM artifacts a
            JOIN artifact_links l ON l.artifact_id=a.artifact_id
            WHERE l.revision_id=? AND a.kind='geometric_psf'
            AND a.path LIKE '%/raytrace_psf_v2/%.npz'""",
            (spec["lens_revision_id"],),
        ).fetchall()
    if (
        lens is None or lens["family"] != "lens"
        or lens["model_kind"] != "patent_prescription"
        or lens["source_kind"] != "legacy_lens_database"
        or len(assets) != 1
    ):
        raise ValueError("Selected Lens revision has no uniquely pinned geometric PSF v2.")
    snapshot = json.loads(lens["snapshot_json"])
    if catalog_digest(snapshot) != lens["content_sha256"]:
        raise IntegrityError("Selected Lens revision content differs from its catalog hash.")
    source_path = _bounded_lens_source_path(root, lens["source_uri"])
    source_observed = _bounded_file_sha256(
        source_path, maximum_bytes=_MAX_LENS_DB_HASH_BYTES, label="Lens source DB"
    )
    if source_observed != lens["source_sha256"]:
        raise IntegrityError("Pinned Lens source DB bytes differ from its catalog hash.")
    psf_path = _bounded_db_child(root, assets[0]["path"], label="geometric PSF")
    psf_observed = _bounded_file_sha256(
        psf_path, maximum_bytes=_MAX_PSF_HASH_BYTES, label="geometric PSF"
    )
    if psf_observed != assets[0]["sha256"]:
        raise IntegrityError("Pinned geometric PSF bytes differ from the catalog hash.")
    selected = spec["sensor_revision"]
    sensor_db = sensor_simulation_db_path(root)
    with sensor_connect(sensor_db) as conn:
        sensor = conn.execute(
            """SELECT report_code,status,model,config_sha256,result_path,result_sha256,
            arrays_path,arrays_sha256 FROM runs WHERE run_id=?
            AND lower(report_code)=lower(?)""",
            (selected["run_id"], selected["identifier"]),
        ).fetchone()
    if (
        sensor is None or sensor["status"] != "simulated"
        or sensor["result_sha256"] != mode["sensor_result_sha256"]
        or sensor["model"] != "db_estimate_v1"
    ):
        raise ValueError("Selected saved Sensor run differs from the active declaration.")
    result_path = _bounded_db_child(sensor_db.parent, sensor["result_path"], label="Sensor result")
    result_observed = _bounded_file_sha256(
        result_path, maximum_bytes=_MAX_SENSOR_RESULT_HASH_BYTES, label="Sensor result"
    )
    if result_observed != sensor["result_sha256"]:
        raise IntegrityError("Pinned Sensor result bytes differ from its saved run hash.")
    arrays_sha = sensor["arrays_sha256"]
    if sensor["arrays_path"] is not None:
        if not isinstance(arrays_sha, str):
            raise IntegrityError("Pinned Sensor array hash is missing.")
        arrays_path = _bounded_db_child(
            sensor_db.parent, sensor["arrays_path"], label="Sensor arrays"
        )
        observed = _bounded_file_sha256(
            arrays_path, maximum_bytes=_MAX_SENSOR_ARRAY_HASH_BYTES, label="Sensor arrays"
        )
        if observed != arrays_sha:
            raise IntegrityError("Pinned Sensor arrays differ from their saved run hash.")
    return {
        "lens_revision_id": lens["revision_id"],
        "lens_revision_sha256": lens["content_sha256"],
        "lens_source_db_sha256": source_observed,
        "geometric_psf_sha256": psf_observed,
        "sensor_report_code": sensor["report_code"],
        "sensor_run_id": selected["run_id"],
        "sensor_result_sha256": result_observed,
        "sensor_arrays_sha256": arrays_sha,
        "sensor_config_sha256": sensor["config_sha256"],
    }


def _registered_mode(service: Any, spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = spec["sensor_revision"]
    checked = preflight_registered_fullfield_study(
        service.registry,
        spec["lens_revision_id"],
        selected["identifier"],
        sensor_run_id=selected["run_id"],
        declaration_id=spec["active_mode_declaration_id"],
        root=_db_root(service),
    )
    mode = checked.get("active_mode")
    source = checked.get("source_validation") or {}
    registered = checked.get("registered_declaration") or {}
    field = checked.get("geometric_fov") or {}
    if (
        not isinstance(mode, dict)
        or source.get("file_hash_verified") is not True
        or source.get("declaration_values_match") is not True
        or source.get("physical_mode_verified") is not False
        or registered.get("id") != spec["active_mode_declaration_id"]
        or registered.get("physical_mode_verified") is not False
        or field.get("status") not in {"within_saved_model_support", "outside_saved_model_support"}
    ):
        raise ValueError(
            f"Registered active-mode declaration is unusable: {checked.get('reason')}."
        )
    if (
        mode.get("sensor_center_offset_from_optical_axis_um_row_col") != [0.0, 0.0]
        or mode.get("sensor_orientation_deg_clockwise") != 0
        or mode.get("cfa_origin_row_col") != [0, 0]
        or mode.get("image_plane_binding")
        != {"reference_plane": "saved_prescription_image_plane", "axial_offset_um": 0.0}
    ):
        raise ValueError(
            "Numerical ROI requires zero optical-axis offset, orientation, "
            "CFA origin and image-plane offset."
        )
    rows, cols = mode["active_rows"], mode["active_cols"]
    if rows > _MAX_ACTIVE_AXIS or cols > _MAX_ACTIVE_AXIS or rows * cols > _MAX_ACTIVE_CELLS:
        raise ValueError(
            "Registered active grid exceeds declared coordinate policy, not a hardware limit."
        )
    r0, r1, c0, c1 = spec["sensor_roi_bounds_row_col_exclusive"]
    if r1 > rows or c1 > cols:
        raise ValueError("Requested global ROI lies outside the registered active-mode rectangle.")
    binding = {
        "declaration_id": registered["id"],
        "declaration_artifact_sha256": registered["artifact_sha256"],
        "source_kind": registered["source_kind"],
        "mode_id": mode["mode_id"],
        "active_shape_row_col": [rows, cols],
        "pixel_pitch_um": float(mode["pixel_pitch_um"]),
        "global_sensor_roi_bounds_row_col_exclusive": [r0, r1, c0, c1],
        "full_boundary_saved_field_status": field["status"],
        "full_boundary_saved_field_reason": field.get("reason"),
        "physical_mode_verified": False,
    }
    return mode, binding


def _roi_tiles(bounds: list[int], tile_shape: list[int]) -> tuple[tuple[int, int, int, int], ...]:
    r0, r1, c0, c1 = bounds
    tiles = tuple(
        (r, min(r1, r + tile_shape[0]), c, min(c1, c + tile_shape[1]))
        for r in range(r0, r1, tile_shape[0])
        for c in range(c0, c1, tile_shape[1])
    )
    policy = _resource_policy()
    if not 2 <= len(tiles) <= policy.max_sensor_tiles:
        raise ValueError("ROI needs 2..256 optical tiles so no full ROI mean tile is held in RAM.")
    local = tuple((a - r0, b - r0, c - c0, d - c0) for a, b, c, d in tiles)
    _complete_tiles(local, (r1 - r0, c1 - c0))
    return tiles


def _execution_inputs(service: Any, spec: dict[str, Any]):
    from .lens_sensor_scene_geometry_tile import (
        d65_analytic_bright_photons,
        make_analytic_strict_geometry_provider,
    )
    from .lens_sensor_streamed_raw_job import _models

    mode, binding = _registered_mode(service, spec)
    model_spec = {
        "lens_revision_id": spec["lens_revision_id"],
        "sensor_revision": spec["sensor_revision"],
        "virtual_sensor_geometry": {
            "rows": mode["active_rows"],
            "cols": mode["active_cols"],
            "pixel_pitch_um": float(mode["pixel_pitch_um"]),
        },
        "analytic_scene": spec["analytic_scene"],
        "capture": spec["capture"],
    }
    lens, inspected, optics, sensor = _models(service, model_spec)
    # Saved db_estimate_v1 supplies a compact spectral/CFA model. Calibrated
    # full-active maps would turn model construction and hashing into a full
    # array path, so this first registered ROI execution refuses them.
    if any(
        sensor.fields.get(key) is not None
        for key in (
            "offset_fpn_image",
            "gain_fpn_image",
            "column_offset_fpn",
            "column_gain_fpn",
        )
    ) or isinstance(sensor.fields.get("etendue"), np.ndarray):
        raise ValueError("Registered ROI does not admit full-active calibration or etendue maps.")
    resident_sensor = _resident_payload_bytes(
        {"fields": sensor.fields, "metadata": sensor.metadata, "data": sensor.data}
    )
    if resident_sensor > _MAX_SENSOR_MODEL_ARRAY_BYTES:
        raise ValueError("Configured sensor retained ndarray payload exceeds ROI model policy.")
    native_cfa = (inspected.get("native_response") or {}).get("native_cfa") or {}
    if np.asarray(sensor.fields.get("pattern")).tolist() != native_cfa.get("pattern"):
        raise ValueError("Configured sensor CFA differs from the pinned saved native CFA.")
    scene = spec["analytic_scene"]
    bright = d65_analytic_bright_photons(
        workspace=service.workspace,
        horizontal_fov_deg=scene["horizontal_fov_deg"],
        luminance_cd_m2=scene["bright_luminance_cd_m2"],
        wavelengths_nm=_WAVELENGTHS_NM,
    )
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=tuple(scene["shape_row_col"]),
        scene_horizontal_fov_deg=scene["horizontal_fov_deg"],
        wavelengths_nm=_WAVELENGTHS_NM,
        bright_side_photons=bright,
        analytic_slanted_edge_offset_deg=scene["edge_offset_deg"],
        max_tile_cells=65_536,
    )
    tiles = _roi_tiles(
        spec["sensor_roi_bounds_row_col_exclusive"], spec["sensor_tile_shape_row_col"]
    )
    return mode, binding, lens, inspected, provider, sensor, tiles, resident_sensor


class _ModelDependentAdmissionError(ValueError):
    def __init__(
        self, cause_status: str, reason: str, resource_evidence: dict[str, Any]
    ) -> None:
        super().__init__(f"{cause_status}: {reason}")
        self.cause_status = cause_status
        self.resource_evidence = resource_evidence


def _admit(
    service: Any,
    spec: dict[str, Any],
    provider: Any,
    sensor: Any,
    tiles: tuple[tuple[int, int, int, int], ...],
    resident_sensor: int,
    *,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy = _resource_policy()
    bounds = spec["sensor_roi_bounds_row_col_exclusive"]
    rows, cols = bounds[1] - bounds[0], bounds[3] - bounds[2]
    cells = rows * cols
    chunks = spec["readout_row_chunk_rows"] * cols
    mean_bytes = 8 * cells + 4096
    readout_bytes = _OUTPUT_BYTES_PER_CELL * cells + 12 * 1024
    output_estimate = (
        mean_bytes
        + readout_bytes
        + len(tiles) * _TILE_EVIDENCE_RESERVE_BYTES
        + _METADATA_RESERVE_BYTES
    )
    readout_transient = (
        _POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL * chunks + _POLICY_COLUMN_BYTES_PER_CELL * cols
    )
    optics_payload = _optics_array_payload_bytes(provider)
    base = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "full_active_shape_row_col": list(sensor.fields["size"]),
        "global_sensor_roi_bounds_row_col_exclusive": bounds,
        "output_roi_shape_row_col": [rows, cols],
        "output_roi_cells": cells,
        "optical_sensor_tile_count": len(tiles),
        "resident_sensor_ndarray_payload_bytes": resident_sensor,
        "resident_optics_ndarray_payload_bytes": optics_payload,
        "readout_transient_policy_estimate_bytes": readout_transient,
        "estimated_output_bytes": output_estimate,
        "resource_policy": policy.as_dict(),
        "active_axis_coordinate_policy": _MAX_ACTIVE_AXIS,
        "active_cells_coordinate_policy": _MAX_ACTIVE_CELLS,
        "scene_axis_virtual_grid_policy": _MAX_SCENE_AXIS,
        "scene_cells_virtual_grid_policy": _MAX_SCENE_CELLS,
        "resource_policy_is_hardware_limit": False,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "hard_process_rss_limit_enforced": False,
        "free_disk_snapshot_is_reservation": False,
        "runtime_under_worker_deadline_guaranteed": False,
        "full_scene_or_oi_materialized": False,
        "full_active_mean_or_raw_materialized_in_ram": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "output_directory": str(output_dir),
    }
    if cells > policy.readout.max_sensor_cells or chunks > policy.readout.max_row_chunk_cells:
        raise _ModelDependentAdmissionError(
            "blocked_roi_readout_cells_policy",
            "ROI readout cells or row chunk exceed declared output policy.",
            {
                "output_roi_cells": cells,
                "max_sensor_cells": policy.readout.max_sensor_cells,
                "row_chunk_cells": chunks,
                "max_row_chunk_cells": policy.readout.max_row_chunk_cells,
            },
        )
    if (
        readout_transient > policy.readout.max_transient_bytes
        or readout_bytes > policy.readout.max_output_bytes
        or output_estimate > policy.max_total_output_bytes
        or mean_bytes > policy.max_mean_disk_bytes
    ):
        raise _ModelDependentAdmissionError(
            "blocked_roi_output_budget_policy",
            "ROI memory or output estimate exceeds declared policy.",
            {
                "readout_transient_policy_estimate_bytes": readout_transient,
                "readout_transient_budget_bytes": policy.readout.max_transient_bytes,
                "readout_output_estimate_bytes": readout_bytes,
                "readout_output_budget_bytes": policy.readout.max_output_bytes,
                "total_output_estimate_bytes": output_estimate,
                "total_output_budget_bytes": policy.max_total_output_bytes,
                "mean_disk_estimate_bytes": mean_bytes,
                "mean_disk_budget_bytes": policy.max_mean_disk_bytes,
            },
        )
    if output_dir.exists() or not output_dir.parent.is_dir():
        raise ValueError("ROI output must be new with an existing real parent.")
    if output_dir.parent.is_symlink():
        raise ValueError("ROI output parent cannot be a symlink.")
    free = shutil.disk_usage(output_dir.parent).free
    base["observed_free_disk_bytes_before_output"] = free
    # Publication temporarily duplicates the scratch arrays into Registry.
    if free - 2 * output_estimate < policy.min_free_after_output_bytes:
        raise _ModelDependentAdmissionError(
            "blocked_disk_reserve_policy",
            "Free disk snapshot does not cover scratch, publication and reserve.",
            {
                "observed_free_disk_bytes": free,
                "estimated_scratch_and_publication_bytes": 2 * output_estimate,
                "minimum_free_after_output_bytes": policy.min_free_after_output_bytes,
            },
        )
    plans: list[dict[str, Any]] = []
    total_work = RegisteredRoiActualWorkCounter(RegisteredRoiWorkPolicy())
    plan_hash = hashlib.sha256()
    worst_optical = 0
    source_work = 0
    for tile in tiles:
        plan = preflight_streamed_analytic_offaxis_mean(
            provider,
            sensor,
            sensor_tile_bounds_row_col_exclusive=tile,
            source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
            sample_angles_deg=_ANGLES_DEG,
            resource_policy=policy.optical_tile,
        )
        if plan["status"] != "ready_under_declared_policy":
            raise _ModelDependentAdmissionError(
                plan["status"], plan.get("reason") or "Optical tile blocked.",
                {
                    "sensor_tile_bounds_row_col_exclusive": list(tile),
                    "combined_policy_estimate_bytes": plan.get(
                        "combined_policy_estimate_bytes"
                    ),
                    "estimated_working_budget_bytes": plan["resource_policy"].get(
                        "estimated_working_budget_bytes"
                    ),
                    "psf_grid_preflight": plan.get("psf_grid_preflight"),
                    "candidate_source_tile_count": plan.get("candidate_source_tile_count"),
                    "model_dependent_work_before_block": total_work.snapshot(),
                },
            )
        accumulated = total_work.add_plan(plan)
        if accumulated["status"] != "ready_under_declared_total_work_policy":
            raise _ModelDependentAdmissionError(
                accumulated["status"], accumulated["reason"],
                {
                    "sensor_tile_bounds_row_col_exclusive": list(tile),
                    "proposed_candidate_source_tiles": accumulated[
                        "proposed_candidate_source_tiles"
                    ],
                    "max_model_dependent_candidate_source_tiles": (
                        total_work.policy.max_model_dependent_candidate_source_tiles
                    ),
                    "model_dependent_work_before_block": total_work.snapshot(),
                },
            )
        encoded = canonical(plan).encode()
        if len(encoded) + 1 > _TILE_EVIDENCE_RESERVE_BYTES:
            raise ValueError("An optical tile preflight exceeds evidence policy.")
        plan_hash.update(encoded)
        plans.append(plan)
        worst_optical = max(worst_optical, int(plan["combined_policy_estimate_bytes"]))
        source_work += int(plan["candidate_source_tile_count"])
    combined = max(
        worst_optical + resident_sensor,
        readout_transient + resident_sensor + optics_payload,
    )
    if combined > policy.max_combined_working_bytes:
        raise _ModelDependentAdmissionError(
            "blocked_combined_working_policy",
            "Combined optical/readout working estimate exceeds declared policy.",
            {
                "combined_working_policy_estimate_bytes": combined,
                "max_combined_working_bytes": policy.max_combined_working_bytes,
                "model_dependent_total_work": total_work.snapshot(),
            },
        )
    base.update(
        status="ready_under_declared_policy",
        tile_preflight_sha256=plan_hash.hexdigest(),
        maximum_optical_tile_policy_estimate_bytes=worst_optical,
        combined_working_policy_estimate_bytes=combined,
        summed_source_tile_work_across_sensor_tiles=source_work,
        optical_source_tile_reuse_across_sensor_tiles=False,
        model_dependent_total_work=total_work.snapshot(),
    )
    return base, plans


def _stable(plan: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(plan)
    result.pop("output_directory", None)
    result.pop("observed_free_disk_bytes_before_output", None)
    return result


def _recheck_registered_blob(
    service: Any, spec: dict[str, Any], mode: dict[str, Any], binding: dict[str, Any]
) -> None:
    """Recheck a small owner-bound declaration without loading another PSF."""
    record = service.registry.get(ACTIVE_MODE_KIND, binding["declaration_id"])
    selected = spec["sensor_revision"]
    if (
        record.get("sensor_identifier") != selected["identifier"]
        or record.get("sensor_run_id") != selected["run_id"]
        or record.get("declaration_artifact_sha256")
        != binding["declaration_artifact_sha256"]
        or record.get("source_kind") != binding["source_kind"]
    ):
        raise IntegrityError("Registered active-mode owner or pinned artifact changed.")
    owner = service.registry.artifact_binding(
        binding["declaration_artifact_sha256"],
        owner_kind=ACTIVE_MODE_KIND,
        owner_id=binding["declaration_id"],
    )
    if owner["descriptor"] != record.get("declaration_artifact"):
        raise IntegrityError("Registered active-mode artifact owner differs.")
    path = service.registry.blob_path(binding["declaration_artifact_sha256"])
    with service.registry._regular_file(path) as (stream, initial):
        if initial.st_size > MAX_DECLARATION_BYTES:
            raise IntegrityError("Registered active-mode declaration exceeds size policy.")
        encoded = stream.read(MAX_DECLARATION_BYTES + 1)
        if (
            len(encoded) != initial.st_size
            or hashlib.sha256(encoded).hexdigest()
            != binding["declaration_artifact_sha256"]
        ):
            raise IntegrityError("Registered active-mode artifact changed during recheck.")
    contents = json.loads(encoded)
    if (
        not isinstance(contents, dict)
        or set(contents) != {"schema", "active_mode"}
        or contents["schema"] != DECLARATION_SCHEMA
        or contents["active_mode"] != {key: mode[key] for key in _CORE_KEYS}
    ):
        raise IntegrityError("Registered active-mode declaration values changed.")


def _detailed_document(
    service: Any, spec: dict[str, Any], inputs: tuple[Any, ...], plan: dict[str, Any]
) -> dict[str, Any]:
    from .imaging import _current
    from .offline_assets import offline_asset_identity

    mode, binding, lens, inspected, provider, sensor, _tiles, _resident = inputs
    sampling = float(mode["pixel_pitch_um"] / provider.oi_pitch_um)
    if sampling < 1.0 - 1e-12:
        raise ValueError("Analytic Scene is coarser than one sample per saved sensor pixel.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "registered_active_mode": binding,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": inspected["revision"],
            "analytic_scene_sha256": provider.source_identity,
            "optical_state_sha256": provider.optics_state_sha256,
            "sensor_model_state_sha256": _sensor_identity(sensor),
        },
        "upstream_assets": offline_asset_identity(
            workspace=service.workspace, source=_db_root(service)
        ),
        "sampling": {
            "optical_convolution_sample_pitch_um": float(provider.oi_pitch_um),
            "native_sensor_pixel_pitch_um": float(mode["pixel_pitch_um"]),
            "input_scene_samples_per_native_pixel": sampling,
            "minimum_one_sample_per_pixel_met": True,
            "sampling_convergence": "not_assessed",
        },
        "resource_preflight": _stable(plan),
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Legacy eager-preflight contract retained for existing frozen v1 jobs."""
    spec = specification(payload)
    inputs = _execution_inputs(service, spec)
    _mode, _binding, _lens, _inspected, provider, sensor, tiles, resident = inputs
    target = service.registry.directory / (".registered_roi_admission_" + digest(spec))
    plan, _ = _admit(service, spec, provider, sensor, tiles, resident, output_dir=target)
    return _detailed_document(service, spec, inputs, plan)


def _prepare_pending(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Freeze bounded identities without constructing optics or planning optical tiles."""
    from .offline_assets import offline_asset_identity
    from .registry import ConflictError
    from .service import environment_fingerprint

    spec = specification(payload)
    mode, binding = _cheap_registered_mode(service, spec)
    work = cheap_registered_roi_work_admission(
        scene_shape_row_col=spec["analytic_scene"]["shape_row_col"],
        active_shape_row_col=binding["active_shape_row_col"],
        roi_bounds_row_col_exclusive=spec["sensor_roi_bounds_row_col_exclusive"],
        sensor_tile_shape_row_col=spec["sensor_tile_shape_row_col"],
        source_tile_shape_row_col=spec["source_tile_shape_row_col"],
        scene_fov_deg=spec["analytic_scene"]["horizontal_fov_deg"],
        pixel_pitch_um=binding["pixel_pitch_um"],
        policy=RegisteredRoiWorkPolicy(),
    )
    if work["status"] != "ready_for_model_dependent_preflight":
        raise ValueError(f"Cheap ROI work admission blocked: {work['status']}: {work['reason']}")
    source_binding = _cheap_db_binding(service, spec, mode)
    environment = environment_fingerprint()
    if service.loaded_environment != environment:
        raise ConflictError("Executable source changed after startup; restart the API.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "submission_contract_schema": PENDING_SUBMISSION_SCHEMA,
        "submission_preflight_status": "worker_preflight_pending",
        "specification": spec,
        "registered_mode_binding": binding,
        "pinned_source_binding": source_binding,
        "cheap_work_admission": work,
        "upstream_assets": offline_asset_identity(
            workspace=service.workspace, source=_db_root(service)
        ),
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "environment": environment,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = _prepare_pending(service, payload)
    return service.registry.submit(frozen, digest(frozen))


class _WorkerPreflightFailureError(ValueError):
    """A failed job with bounded, typed evidence that is not a computed result."""

    def __init__(
        self, job: dict[str, Any], *, stage: str, code: str, detail: str,
        cause_status: str | None = None, resource_evidence: dict[str, Any] | None = None,
    ):
        private = ("/", "\\")
        safe_detail = (
            "Pinned input unavailable or changed; inspect local Worker logs."
            if any(char in detail for char in private)
            else detail[:512]
        )
        super().__init__(f"{code}: {safe_detail}")
        self.evidence_result = {
            "preflight_failure": {
                "schema": "camerae2e_registered_roi_worker_preflight_failure_v1",
                "status": "blocked_before_optical_execution",
                "stage": stage,
                "reason_code": code,
                "reason": safe_detail,
                "cause_status": cause_status,
                "resource_evidence": resource_evidence,
                "frozen_request_sha256": job["content_hash"],
                "optical_convolution_executed": False,
                "physical_fov_qualified": False,
                "product_full_frame_qualified": False,
            },
            "artifacts": [],
        }


def _new_submission(job: dict[str, Any]) -> bool:
    contract = job.get("submission_contract_schema")
    if contract is None:
        return False
    if contract != PENDING_SUBMISSION_SCHEMA:
        raise IntegrityError(
            "Unknown registered ROI submission contract; explicit migration required."
        )
    return True


def _model_identities_match_pins(
    lens: dict[str, Any], inspected: dict[str, Any], pinned: dict[str, Any]
) -> bool:
    identity = lens["identity"]
    revision = inspected["revision"]
    return (
        identity.get("revision_id") == pinned["lens_revision_id"]
        and identity.get("revision_sha256") == pinned["lens_revision_sha256"]
        and identity.get("source_db_sha256") == pinned["lens_source_db_sha256"]
        and identity.get("asset_sha256") == pinned["geometric_psf_sha256"]
        and revision.get("report_code") == pinned["sensor_report_code"]
        and revision.get("run_id") == pinned["sensor_run_id"]
        and revision.get("result_sha256") == pinned["sensor_result_sha256"]
        and revision.get("config_sha256") == pinned["sensor_config_sha256"]
    )


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .offline_assets import offline_asset_identity

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Registered-mode ROI RAW job is not running or was cancelled.")
    pending = _new_submission(job)
    if not pending and job.get("environment") != service.loaded_environment:
        raise _WorkerPreflightFailureError(
            job, stage="legacy_environment", code="legacy_environment_unavailable_resubmit",
            detail=(
                "The legacy queued request belongs to an earlier executable source; "
                "resubmit it."
            ),
        )
    if pending:
        try:
            frozen = _prepare_pending(service, job["specification"])
        except Exception as exc:
            raise _WorkerPreflightFailureError(
                job, stage="frozen_input_recheck", code="pinned_input_changed_or_unavailable",
                detail=str(exc),
            ) from exc
        if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
            raise _WorkerPreflightFailureError(
                job, stage="frozen_input_recheck", code="frozen_submission_changed",
                detail="Pinned declaration, DB bytes, upstream assets or environment changed.",
            )
    if pending:
        spec = job["specification"]
        fresh = None
    else:
        fresh = prepare(service, job["specification"])
        if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
            raise IntegrityError("Frozen registered mode, model, Scene, assets or policy changed.")
        spec = fresh["specification"]
    try:
        inputs = _execution_inputs(service, spec)
    except Exception as exc:
        if not pending:
            raise
        raise _WorkerPreflightFailureError(
            job, stage="model_dependent_preflight", code="model_load_or_field_preflight_blocked",
            detail=str(exc),
        ) from exc
    mode, binding, lens, inspected, provider, sensor, tiles, resident = inputs
    if pending:
        if (
            any(binding.get(key) != value for key, value in job["registered_mode_binding"].items())
            or not _model_identities_match_pins(lens, inspected, job["pinned_source_binding"])
        ):
            raise _WorkerPreflightFailureError(
                job, stage="model_dependent_preflight", code="detailed_binding_changed",
                detail="Detailed model identities differ from the pinned lightweight request.",
            )
        if mode["pixel_pitch_um"] / provider.oi_pitch_um < 1.0 - 1e-12:
            raise _WorkerPreflightFailureError(
                job, stage="model_dependent_preflight", code="scene_sampling_below_pixel",
                detail="Analytic Scene is coarser than one sample per saved sensor pixel.",
            )
    elif (
        binding != fresh["registered_active_mode"]
        or lens["identity"] != fresh["source_identities"]["lens"]
        or inspected["revision"] != fresh["source_identities"]["sensor"]
        or provider.source_identity != fresh["source_identities"]["analytic_scene_sha256"]
        or provider.optics_state_sha256 != fresh["source_identities"]["optical_state_sha256"]
        or _sensor_identity(sensor) != fresh["source_identities"]["sensor_model_state_sha256"]
    ):
        raise IntegrityError("Pinned registered-mode ROI inputs changed before execution.")
    run_root = _work_root(service.registry.directory, job["id"])
    run_root.parent.mkdir(mode=0o700, exist_ok=True)
    if run_root.parent.is_symlink():
        raise IntegrityError("ROI scratch parent became a symlink.")
    run_root.mkdir(mode=0o700, exist_ok=False)
    destination = run_root / "capture"
    try:
        try:
            plan, tile_plans = _admit(
                service, spec, provider, sensor, tiles, resident, output_dir=destination
            )
            if pending:
                fresh = _detailed_document(service, spec, inputs, plan)
        except Exception as exc:
            if not pending:
                raise
            raise _WorkerPreflightFailureError(
                job, stage="model_dependent_preflight", code="model_dependent_preflight_blocked",
                detail=str(exc),
                cause_status=getattr(exc, "cause_status", None),
                resource_evidence=getattr(exc, "resource_evidence", None),
            ) from exc
        if pending and (
            fresh["upstream_assets"] != job["upstream_assets"]
            or fresh["environment"] != job["environment"]
            or any(
                fresh["registered_active_mode"].get(key) != value
                for key, value in job["registered_mode_binding"].items()
            )
            or not _model_identities_match_pins(lens, inspected, job["pinned_source_binding"])
        ):
            raise _WorkerPreflightFailureError(
                job, stage="model_dependent_preflight", code="detailed_binding_changed",
                detail="Detailed model preflight differs from the pinned lightweight request.",
            )
        if not pending and _stable(plan) != fresh["resource_preflight"]:
            raise IntegrityError("Executed ROI numerical preflight differs from frozen admission.")
        destination.mkdir(mode=0o700, exist_ok=False)
        bounds = spec["sensor_roi_bounds_row_col_exclusive"]
        r0, r1, c0, c1 = bounds
        shape = (r1 - r0, c1 - c0)
        mean_path = destination / "mean_electrons.npy"
        mean = np.lib.format.open_memmap(mean_path, mode="w+", dtype=np.float64, shape=shape)
        readout_plan = preflight_out_of_core_readout(
            sensor,
            mean,
            destination / "readout",
            seed=spec["capture"]["seed"],
            row_chunk_rows=spec["readout_row_chunk_rows"],
            policy=_resource_policy().readout,
            global_sensor_roi_bounds_row_col_exclusive=tuple(bounds),
        )
        if readout_plan["status"] != "ready_under_declared_policy":
            raise ValueError(f"ROI readout blocked before optics: {readout_plan['status']}.")
        tile_digest = hashlib.sha256()
        with (destination / "tile_execution.jsonl").open("w", encoding="utf-8") as handle:
            for tile, admitted in zip(tiles, tile_plans, strict=True):
                if (
                    provider.source_identity != fresh["source_identities"]["analytic_scene_sha256"]
                    or _optical_state_fingerprint(provider.optics)
                    != fresh["source_identities"]["optical_state_sha256"]
                    or _sensor_identity(sensor)
                    != fresh["source_identities"]["sensor_model_state_sha256"]
                    or sensor.data
                ):
                    raise IntegrityError("ROI optical model mutated during tile capture.")
                values, evidence = _compute_preflighted_streamed_analytic_offaxis_mean(
                    provider,
                    sensor,
                    sensor_tile_bounds_row_col_exclusive=tile,
                    source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
                    sample_angles_deg=_ANGLES_DEG,
                    resource_policy=_resource_policy().optical_tile,
                    admitted_plan=admitted,
                )
                a, b, c, d = tile
                if (
                    values.shape != (b - a, d - c)
                    or not np.isfinite(values).all()
                    or np.any(values < 0)
                    or evidence["sensor_execution"]["cfa_phase_origin_sensor_row_col"] != [a, c]
                ):
                    raise IntegrityError("ROI optical tile values or global CFA phase differ.")
                mean[a - r0 : b - r0, c - c0 : d - c0] = values
                line = canonical(evidence).encode() + b"\n"
                if len(line) > _TILE_EVIDENCE_RESERVE_BYTES:
                    raise IntegrityError("Executed ROI tile evidence exceeds policy.")
                handle.write(line.decode())
                tile_digest.update(line)
        mean.flush()
        del mean
        mean_readonly = np.load(mean_path, mmap_mode="r")
        readout = compute_out_of_core_readout(
            sensor,
            mean_readonly,
            destination / "readout",
            seed=spec["capture"]["seed"],
            row_chunk_rows=spec["readout_row_chunk_rows"],
            policy=_resource_policy().readout,
            global_sensor_roi_bounds_row_col_exclusive=tuple(bounds),
        )
        del mean_readonly
        # Recheck the immutable declaration and source digests without loading
        # a second large PSF while the executed provider is still resident.
        _recheck_registered_blob(service, spec, mode, binding)
        if (
            readout["full_sensor_shape_row_col"] != [mode["active_rows"], mode["active_cols"]]
            or readout["global_sensor_roi_bounds_row_col_exclusive"] != bounds
            or _optical_state_fingerprint(provider.optics)
            != fresh["source_identities"]["optical_state_sha256"]
            or _sensor_identity(sensor)
            != fresh["source_identities"]["sensor_model_state_sha256"]
            or offline_asset_identity(workspace=service.workspace, source=_db_root(service))
            != fresh["upstream_assets"]
            or service.loaded_environment != job["environment"]
            or (pending and _prepare_pending(service, spec) != frozen)
            or service.registry.get_run(job["id"])["status"] != "running"
        ):
            raise IntegrityError("Pinned ROI declaration, readout or DB changed during execution.")
        files = {
            "mean_electrons": (mean_path, np.dtype("float64"), "electron"),
            "raw_dn": (Path(readout["raw_dn_path"]), np.dtype("int32"), "DN"),
            "reference_dn": (Path(readout["reference_dn_path"]), np.dtype("float64"), "DN"),
            "volts": (Path(readout["volts_path"]), np.dtype("float64"), "V"),
        }
        artifacts = {}
        for name, (path, dtype, unit) in files.items():
            _checked_npy(path, shape=shape, dtype=dtype)
            descriptor = service.registry.store_file(
                path, f"registered-roi-{name}.npy", "application/octet-stream"
            )
            descriptor.update(dtype=str(dtype), shape=list(shape), unit=unit)
            artifacts[name] = descriptor
        tile_path = destination / "tile_execution.jsonl"
        _reject_private_paths(
            tile_path, workspace=Path(service.workspace), state_dir=service.registry.directory
        )
        artifacts["tile_execution"] = service.registry.store_file(
            tile_path, "registered-roi-tile-execution.jsonl", "application/x-ndjson"
        )
        actual_output_bytes = sum(
            path.stat().st_size for path in destination.rglob("*") if path.is_file()
        )
        if actual_output_bytes > _resource_policy().max_total_output_bytes:
            raise IntegrityError("Actual ROI scratch output exceeds declared disk policy.")
        summary = {
            "schema": SCHEMA,
            "status": "completed_numerical_registered_mode_roi_raw_diagnostic",
            "specification": spec,
            "registered_active_mode": binding,
            "source_identities": fresh["source_identities"],
            "upstream_assets": fresh["upstream_assets"],
            "sampling": fresh["sampling"],
            "resource_preflight": fresh["resource_preflight"],
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
            **({
                "submission_contract_schema": PENDING_SUBMISSION_SCHEMA,
                "frozen_submission_sha256": job["content_hash"],
                "submission_preflight_status": job["submission_preflight_status"],
                "registered_mode_binding": job["registered_mode_binding"],
                "pinned_source_binding": job["pinned_source_binding"],
                "source_binding_verified_at_publication": frozen["pinned_source_binding"],
                "cheap_work_admission": job["cheap_work_admission"],
            } if pending else {}),
            "execution": {
                "global_sensor_roi_bounds_row_col_exclusive": bounds,
                "full_active_shape_row_col": [mode["active_rows"], mode["active_cols"]],
                "output_roi_shape_row_col": list(shape),
                "optical_sensor_tile_count": len(tiles),
                "tile_execution_jsonl_sha256": tile_digest.hexdigest(),
                "mean_electrons_npy_sha256": artifacts["mean_electrons"]["sha256"],
                "readout_sensor_capture": readout["sensor_capture"],
                "actual_scratch_output_bytes": actual_output_bytes,
                "full_scene_or_oi_materialized": False,
                "full_active_mean_or_raw_materialized_in_ram": False,
                "native_seed_bit_parity": False,
                "runtime_under_worker_deadline_guaranteed": False,
                "hard_process_rss_limit_enforced": False,
            },
            "array_artifacts": artifacts,
            "sensor_response_field_uniform_assumption": True,
            "physical_pixel_field_response_verified": False,
            "physical_fov_qualified": False,
            "product_full_frame_qualified": False,
            "verification": {
                "implementation_verification": "offaxis_tiles_global_roi_raw_executed",
                "cross_tool_comparison": "not_performed",
                "physical_validation": "not_established",
            },
            "limitation": LIMITATION,
        }
        serialized = canonical(summary)
        if str(service.workspace) in serialized or str(service.registry.directory) in serialized:
            raise IntegrityError("Private local path leaked into registered ROI result.")
        result = service.registry.store_blob(
            serialized.encode(), "registered-roi-raw-diagnostic-result.json", "application/json"
        )
        return {"summary": summary, "artifacts": [*artifacts.values(), result]}
    finally:
        cleanup_work_root(service.registry.directory, job["id"])


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not registered-mode ROI disk RAW.")
    pending = _new_submission(job)
    failure = (job.get("result") or {}).get("preflight_failure")
    if failure is not None:
        if (
            job["status"] != "failed"
            or not isinstance(failure, dict)
            or failure.get("schema") != "camerae2e_registered_roi_worker_preflight_failure_v1"
            or failure.get("status") != "blocked_before_optical_execution"
            or failure.get("frozen_request_sha256") != job["content_hash"]
            or failure.get("optical_convolution_executed") is not False
            or failure.get("physical_fov_qualified") is not False
            or failure.get("product_full_frame_qualified") is not False
            or (job.get("result") or {}).get("artifacts") != []
            or not isinstance(failure.get("stage"), str)
            or not isinstance(failure.get("reason_code"), str)
            or not isinstance(failure.get("reason"), str)
            or len(canonical(failure)) > 16_384
        ):
            raise IntegrityError("Stored registered ROI preflight failure evidence differs.")
    summary = (job.get("result") or {}).get("summary")
    if (summary is not None and (job["status"] != "succeeded" or failure is not None)) or (
        job["status"] == "succeeded" and summary is None
    ):
        raise IntegrityError("Registered ROI result shape differs from terminal status.")
    if summary is not None:
        frozen_keys = (
            "specification", "upstream_assets", "default_worker_timeout_seconds",
        ) if pending else (
            "specification", "registered_active_mode", "source_identities",
            "upstream_assets", "sampling", "resource_preflight",
            "default_worker_timeout_seconds",
        )
        for key in frozen_keys:
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError(f"Stored registered ROI {key} differs from frozen request.")
        if pending:
            for key in (
                "submission_contract_schema", "submission_preflight_status",
                "registered_mode_binding", "pinned_source_binding",
                "source_binding_verified_at_publication", "cheap_work_admission",
            ):
                expected = (
                    job["pinned_source_binding"]
                    if key == "source_binding_verified_at_publication" else job[key]
                )
                if canonical(summary.get(key)) != canonical(expected):
                    raise IntegrityError(
                        f"Stored registered ROI {key} differs from frozen request."
                    )
            if (
                summary.get("frozen_submission_sha256") != job["content_hash"]
                or any(
                    summary["registered_active_mode"].get(key) != value
                    for key, value in job["registered_mode_binding"].items()
                )
                or not _model_identities_match_pins(
                    {"identity": summary["source_identities"]["lens"]},
                    {"revision": summary["source_identities"]["sensor"]},
                    job["pinned_source_binding"],
                )
            ):
                raise IntegrityError("Stored registered ROI result differs from pinned sources.")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("physical_fov_qualified") is not False
            or summary.get("product_full_frame_qualified") is not False
            or summary.get("sensor_response_field_uniform_assumption") is not True
        ):
            raise IntegrityError("Stored registered ROI qualification contract differs.")
        if job["status"] == "succeeded":
            bounds = job["specification"]["sensor_roi_bounds_row_col_exclusive"]
            shape = (bounds[1] - bounds[0], bounds[3] - bounds[2])
            for name, dtype in (
                ("mean_electrons", np.dtype("float64")),
                ("raw_dn", np.dtype("int32")),
                ("reference_dn", np.dtype("float64")),
                ("volts", np.dtype("float64")),
            ):
                descriptor = summary["array_artifacts"][name]
                if descriptor not in job["artifacts"]:
                    raise IntegrityError(f"Stored registered ROI {name} artifact link is missing.")
                _checked_npy(
                    service.registry.blob_path(descriptor["sha256"]), shape=shape, dtype=dtype
                )
            descriptor = summary["array_artifacts"]["tile_execution"]
            if descriptor not in job["artifacts"]:
                raise IntegrityError("Stored registered ROI tile evidence link is missing.")
            service.registry.blob_path(descriptor["sha256"])
            result = [
                item
                for item in job["artifacts"]
                if item["name"] == "registered-roi-raw-diagnostic-result.json"
            ]
            if (
                len(result) != 1
                or service.registry.blob_path(result[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Stored registered ROI result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {
            key: item.get(key)
            for key in (
                "id", "name", "status", "created_at", "error", "submission_preflight_status"
            )
        }
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-registered-roi-disk-raw/template", tags=["lens_sensor_roi_raw"])
    def template(q: str = ""):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        db_root = _db_root(service)
        return {
            "specification": default_spec(),
            "lens_catalog": lens_revision_catalog(catalog_root=db_root, query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=db_root, query=q or None),
            "registered_active_mode_endpoint": "/api/v1/lens-sensor-active-modes",
            "resource_policy": _resource_policy().as_dict(),
            "maximum_active_axis_coordinate_policy": _MAX_ACTIVE_AXIS,
            "maximum_active_cells_coordinate_policy": _MAX_ACTIVE_CELLS,
            "maximum_scene_axis_virtual_grid_policy": _MAX_SCENE_AXIS,
            "maximum_scene_cells_virtual_grid_policy": _MAX_SCENE_CELLS,
            "maximum_roi_shape_row_col": [_MAX_ROI_SIDE, _MAX_ROI_SIDE],
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
            "resource_policy_is_hardware_limit": False,
            "limitation": LIMITATION,
        }

    @app.post(
        "/api/v1/lens-sensor-registered-roi-disk-raw", status_code=202, tags=["lens_sensor_roi_raw"]
    )
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-registered-roi-disk-raw", tags=["lens_sensor_roi_raw"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-registered-roi-disk-raw/{identity}", tags=["lens_sensor_roi_raw"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/lens-sensor-registered-roi-disk-raw/{identity}/cancel",
        tags=["lens_sensor_roi_raw"],
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(
        "/api/v1/lens-sensor-registered-roi-disk-raw/{identity}/export",
        tags=["lens_sensor_roi_raw"],
    )
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="registered-roi-raw.json"'},
        )

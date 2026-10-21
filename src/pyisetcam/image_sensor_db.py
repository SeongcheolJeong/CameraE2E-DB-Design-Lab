"""Image-sensor database selection helpers.

This module indexes the external FDTD ``sensor_db`` package so callers can
search image sensors, select one by stable ID, and pass its generated stack /
TCAD paths into CameraE2E configuration code.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .fdtd_sensor import fdtd_sensor_default_lut_path
from .tcad_sensor import TCADSensorDB, tcad_sensor_default_paths

_DEFAULT_CAMERA_DB_ROOT_ENV = "PYISETCAM_CAMERA_DB_ROOT"


def image_sensor_db_root(root: str | Path | None = None) -> Path:
    """Return the active image-sensor DB root directory."""

    if root is not None:
        return Path(root).expanduser()
    explicit = os.environ.get("PYISETCAM_IMAGE_SENSOR_DB_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    camera_db_root = os.environ.get(_DEFAULT_CAMERA_DB_ROOT_ENV)
    if camera_db_root:
        return Path(camera_db_root).expanduser() / "fdtd_tcad/sensor_db"
    for candidate in _default_camera_db_sensor_roots():
        if candidate.exists():
            return candidate
    fdtd_root = Path(
        os.environ.get("PYISETCAM_FDTD_ROOT", "/Users/seongcheoljeong/FDTD")
    ).expanduser()
    return fdtd_root / "sensor_db"


def _default_camera_db_sensor_roots() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    return [
        repo_root / "camerae2e_db/fdtd_tcad/sensor_db",
        repo_root.parent / "CameraE2E-DB/fdtd_tcad/sensor_db",
    ]


def image_sensor_db_catalog_path(root: str | Path | None = None) -> Path:
    """Return the active image-sensor catalog JSON path."""

    return image_sensor_db_root(root) / "sensor_catalog.json"


def image_sensor_db_load(root: str | Path | None = None) -> dict[str, Any]:
    """Load the image-sensor catalog JSON."""

    path = image_sensor_db_catalog_path(root)
    if not path.exists():
        raise FileNotFoundError(f"Image sensor DB catalog not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def image_sensor_db_records(
    query: str | None = None,
    *,
    manufacturer: str | None = None,
    limit: int | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return normalized image-sensor records with optional text filters."""

    catalog = image_sensor_db_load(root)
    query_text = "" if query is None else str(query).lower()
    manufacturer_text = "" if manufacturer is None else str(manufacturer).lower()
    records: list[dict[str, Any]] = []
    for raw in catalog.get("records", []):
        record = _normalize_record(raw, image_sensor_db_root(root))
        if manufacturer_text and manufacturer_text not in record["manufacturer"].lower():
            continue
        haystack = " ".join(
            str(record.get(key, ""))
            for key in (
                "sensor_id",
                "code",
                "manufacturer",
                "device_name",
                "title",
                "pixel_architecture",
                "cfa_pattern",
                "illumination",
            )
        ).lower()
        if query_text and query_text not in haystack:
            continue
        records.append(record)
    records.sort(
        key=lambda item: (
            item["manufacturer"].lower(),
            item["device_name"].lower(),
            item["code"],
        )
    )
    if limit is not None:
        records = records[: int(limit)]
    return records


def image_sensor_db_get(sensor_id_or_code: str, root: str | Path | None = None) -> dict[str, Any]:
    """Return one normalized image-sensor record by ``sensor_id`` or report code."""

    key = str(sensor_id_or_code).lower()
    for record in image_sensor_db_records(root=root):
        if key in {str(record["sensor_id"]).lower(), str(record["code"]).lower()}:
            return record
    raise KeyError(f"Unknown image sensor DB record: {sensor_id_or_code}")


def image_sensor_db_parameters(
    sensor_id_or_code: str, root: str | Path | None = None,
    *, allow_shared_proxy: bool = False,
) -> dict[str, Any]:
    """Resolve a record's declared physics artifacts, with explicit proxy opt-in.

    A catalog record may declare ``physics_artifacts`` with its ``sensor_id``
    and paths named ``lut_path``, ``generation_map_path``,
    ``collection_summary_paths`` and ``accuracy_gate_path``. Relative paths
    resolve against the catalog directory. This association is provenance,
    not measured-accuracy certification.
    """

    record = image_sensor_db_get(sensor_id_or_code, root=root)
    db_root = image_sensor_db_root(root)
    declared = record["raw"].get("physics_artifacts")
    artifacts: dict[str, Any] = {
        "lut_path": None, "generation_map_path": None,
        "collection_summary_paths": [], "accuracy_gate_path": None,
    }
    scope = "missing"
    if declared is not None:
        if not isinstance(declared, Mapping) or declared.get("sensor_id") != record["sensor_id"]:
            raise ValueError("Physics artifact sensor_id does not match the selected sensor.")
        scope = "sensor_specific"
        for key in ("lut_path", "generation_map_path", "accuracy_gate_path"):
            value = declared.get(key)
            artifacts[key] = _catalog_artifact_path(value, db_root)
        summaries = declared.get("collection_summary_paths", [])
        if not isinstance(summaries, (list, tuple)) or any(value is None for value in summaries):
            raise ValueError("collection_summary_paths must be a list of artifact paths.")
        artifacts["collection_summary_paths"] = [
            _catalog_artifact_path(value, db_root) for value in summaries
        ]
        for value in [artifacts["lut_path"], artifacts["generation_map_path"],
                      artifacts["accuracy_gate_path"], *artifacts["collection_summary_paths"]]:
            if value is not None and not value.is_file():
                raise FileNotFoundError(f"Declared sensor physics artifact not found: {value}")
    elif allow_shared_proxy:
        defaults = tcad_sensor_default_paths(db_root.parent)
        artifacts.update({key: defaults[key] for key in artifacts if key != "lut_path"})
        artifacts["lut_path"] = fdtd_sensor_default_lut_path()
        scope = "shared_proxy"
    return {
        "sensor_id": record["sensor_id"],
        "code": record["code"],
        "catalog_path": image_sensor_db_catalog_path(root),
        "stack_config_path": (
            Path(record["stack_config_path"]) if record["stack_config_path"] else None
        ),
        "tcad_profile_path": (
            Path(record["tcad_profile_path"]) if record["tcad_profile_path"] else None
        ),
        **artifacts,
        "physics_binding": {
            "scope": scope,
            "sensor_id": record["sensor_id"],
            "sensor_specific": scope == "sensor_specific",
            "measured_accuracy_verified": False,
        },
    }


def _catalog_artifact_path(value: Any, root: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("Physics artifact paths must be nonempty strings.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def image_sensor_db_config(
    sensor_id_or_code: str,
    *,
    strategy: str = "hybrid",
    root: str | Path | None = None,
    allow_shared_proxy: bool = False,
    require_sensor_specific: bool = False,
    model: str = "auto",
    assumptions: Mapping[str, Any] | None = None,
    lut_path: str | Path | None = None,
    sensor_illumination: Mapping[str, Any] | None = None,
    collection_efficiency: float | None = None,
    optical_assumptions: Mapping[str, Any] | None = None,
    readout_profile: str | None = None,
    operating_point: str | None = None,
) -> dict[str, Any]:
    """Build a CameraE2E scenario fragment from one image-sensor DB record.

    ``auto`` prefers the fast ``physical_estimate_v1`` for supported Bayer
    geometry. Other eligible records retain the DB estimate with an explicit
    selection reason. ``model='db_estimate_v1'`` reproduces the older lumped-loss
    estimate. ``model='legacy'`` retains the original
    analytic/FDTD/TCAD strategy interface, including explicit shared-proxy opt-in.
    """

    record = image_sensor_db_get(sensor_id_or_code, root=root)
    selection = None
    if model == "auto":
        if allow_shared_proxy or require_sensor_specific or strategy == "lut_only":
            raise ValueError("Automatic estimates do not attach LUTs; use model='legacy'.")
        model, reason = _select_estimate_model(record, root, assumptions)
        selection = {"requested": "auto", "selected": model, "reason": reason}
        if model == "db_estimate_v1" and any(v is not None for v in (
            optical_assumptions, sensor_illumination, readout_profile, operating_point,
            collection_efficiency,
        )):
            raise ValueError(
                f"Physical model unavailable: {reason}. Explicit physical settings cannot "
                "be silently applied to the legacy DB estimate."
            )
    efficiency = 0.75 if collection_efficiency is None else collection_efficiency
    if model == "physical_estimate_v1":
        if lut_path is not None:
            raise ValueError("physical_estimate_v1 does not use a LUT path.")
        result = _physical_estimate_config(
            record, root=root, illumination=sensor_illumination,
            collection_efficiency=efficiency, optical_assumptions=optical_assumptions,
            assumptions=assumptions, readout_profile=readout_profile,
            operating_point=operating_point, strategy=strategy,
            allow_shared_proxy=allow_shared_proxy, require_sensor_specific=require_sensor_specific,
        )
        if selection:
            result["model_selection"] = selection
            result["scenario"]["image_sensor_db"]["model_selection"] = selection
        return result
    if model == "fdtd_estimate_v1":
        if optical_assumptions is not None:
            raise ValueError("FDTD optical assumptions are frozen in the validated LUT.")
        return _fdtd_estimate_config(
            record, root=root, lut_path=lut_path, illumination=sensor_illumination,
            collection_efficiency=efficiency, assumptions=assumptions,
            strategy=strategy, allow_shared_proxy=allow_shared_proxy,
            readout_profile=readout_profile, operating_point=operating_point,
        )
    if any(v is not None for v in (
        optical_assumptions, readout_profile, operating_point, collection_efficiency,
    )):
        raise ValueError(
            "Optical assumptions, profiles and collection_efficiency require a physical/FDTD "
            "estimate. db_estimate_v1 retains its legacy total effective_efficiency assumption."
        )
    if lut_path is not None or sensor_illumination is not None:
        raise ValueError("lut_path/sensor_illumination require model='fdtd_estimate_v1'.")
    if model == "db_estimate_v1":
        result = _estimate_config(
            record, root=root, assumptions=assumptions, strategy=strategy,
            allow_shared_proxy=allow_shared_proxy,
            require_sensor_specific=require_sensor_specific,
        )
        if selection:
            result["model_selection"] = selection
            result["scenario"]["image_sensor_db"]["model_selection"] = selection
        return result
    if model != "legacy":
        raise ValueError(
            "model must be 'auto', 'physical_estimate_v1', 'db_estimate_v1', "
            "'fdtd_estimate_v1' or 'legacy'."
        )
    if assumptions:
        raise ValueError("assumptions are supported only with model='db_estimate_v1'.")
    params = image_sensor_db_parameters(
        sensor_id_or_code, root=root, allow_shared_proxy=allow_shared_proxy,
    )
    resolved_strategy = _normalize_config_strategy(strategy)
    attach_luts = resolved_strategy in {"hybrid", "lut_only"}
    attach_analytic = resolved_strategy in {"hybrid", "analytic_only"}

    sensor_config = _analytic_sensor_config_from_record(
        record,
        include_ocl_proxy=attach_analytic,
    )
    fdtd_config: dict[str, Any] | None = None
    tcad_config: dict[str, Any] | None = None
    if attach_luts and _path_exists(params.get("lut_path")):
        fdtd_config = {
            "enabled": True,
            "lut": _path_string(params["lut_path"]),
            "mode": "qe+field+crosstalk",
            "crosstalk_strength": 1.0,
        }
    tcad_paths_available = (
        _path_exists(params.get("generation_map_path"))
        and bool(params.get("collection_summary_paths"))
        and all(_path_exists(item) for item in params.get("collection_summary_paths", []))
    )
    if attach_luts and tcad_paths_available:
        tcad_config = {
            "enabled": True,
            "db": {
                "root": _path_string(image_sensor_db_root(root).parent),
                "generation_map_path": _path_string(params["generation_map_path"]),
                "collection_summary_paths": [
                    _path_string(item) for item in params.get("collection_summary_paths", [])
                ],
                "accuracy_gate_path": (
                    _path_string(params["accuracy_gate_path"])
                    if _path_exists(params.get("accuracy_gate_path")) else None
                ),
            },
            "collection_mode": "collection",
            "allow_proxy_accuracy": True,
        }

    has_physics = fdtd_config is not None or tcad_config is not None
    if resolved_strategy == "lut_only" and not has_physics:
        raise ValueError("lut_only requires available sensor physics artifacts; none are bound.")
    if require_sensor_specific and (
        not params["physics_binding"]["sensor_specific"] or not has_physics
    ):
        raise ValueError("Sensor-specific physics artifacts are required for this configuration.")
    binding = {**params["physics_binding"], "attached": has_physics}

    scenario: dict[str, Any] = {
        "name": f"image_sensor_db_{record['sensor_id']}",
        "sensor": sensor_config,
        "image_sensor_db": {
            "sensor_id": record["sensor_id"],
            "code": record["code"],
            "strategy": resolved_strategy,
            "physics_binding": binding,
            "declared_scope": params["physics_binding"]["scope"],
            "require_sensor_specific": bool(require_sensor_specific),
            "physics_artifacts": {
                "fdtd": _physics_layer_identity("fdtd", fdtd_config),
                "tcad": _physics_layer_identity("tcad", tcad_config),
            },
        },
        # Clear physics already attached to a supplied Camera when this record
        # has no corresponding binding, including analytic-only selection.
        "fdtd": fdtd_config if fdtd_config is not None else {"enabled": False},
        "tcad": tcad_config if tcad_config is not None else {"enabled": False},
    }

    component_tiers = {
        "sensor_db_metadata": "proxy",
        "analytic_sensor_proxy": "proxy" if attach_analytic and sensor_config else "missing",
        "fdtd_lut": "proxy" if fdtd_config is not None else "missing",
        "tcad_collection": "calibration_required" if tcad_config is not None else "missing",
    }
    return {
        "schema_version": "camerae2e_image_sensor_db_config_v1",
        "model": "legacy",
        "sensor_id": record["sensor_id"],
        "code": record["code"],
        "manufacturer": record["manufacturer"],
        "device_name": record["device_name"],
        "strategy": resolved_strategy,
        "readiness_tier": (
            "calibration_required" if tcad_config is not None else "proxy"
        ),
        "scenario": scenario,
        "sensor": sensor_config,
        "fdtd": fdtd_config,
        "tcad": tcad_config,
        "paths": _jsonable_parameters(params),
        "physics_binding": binding,
        "policy": {
            "schema_version": "camerae2e_sensor_db_policy_v1",
            "component_tiers": component_tiers,
            "physics_binding": binding,
            "db_lut_role": (
                "Only declared sensor artifacts are attached by default. Shared reference "
                "LUTs require allow_shared_proxy=True and remain explicitly labeled. "
                "Artifact association does not establish measured calibration."
            ),
            "analytic_role": (
                "Analytic proxy axes support broad free-configuration sweeps and "
                "should be calibrated or validated against DB/LUT/FDTD/TCAD evidence "
                "before stronger claims are made."
            ),
            "truth_boundary": (
                "Sensor DB records are metadata-derived unless measured stack, "
                "sensor-specific FDTD LUT, TCAD process deck, QE/noise data, and "
                "lineage evidence are attached."
            ),
        },
        "record_summary": _record_summary(record),
    }


def _select_estimate_model(
    record: Mapping[str, Any], root: str | Path | None,
    assumptions: Mapping[str, Any] | None,
) -> tuple[str, str]:
    from .cfa_materials import image_sensor_cfa_materials
    from .sensor_estimate import image_sensor_db_model

    if set(assumptions or {}) & {"effective_efficiency", "active_si_thickness_um"}:
        return "db_estimate_v1", "Explicit legacy DB optical assumptions retain their meaning."
    estimate = image_sensor_db_model(record["sensor_id"], root=root)
    if estimate["cfa_pattern"] not in {"bayer", "rggb_bayer"}:
        return "db_estimate_v1", "Fast physical model currently supports native RGB Bayer geometry."
    try:
        bundle = image_sensor_cfa_materials(record["sensor_id"], root=root)
    except (FileNotFoundError, ValueError, KeyError) as error:
        return "db_estimate_v1", f"Canonical optical geometry unavailable: {error}"
    if str(bundle.get("cfa_pattern", "")).lower() not in {"bayer", "rggb"}:
        return "db_estimate_v1", "Canonical stack is not supported native RGB Bayer geometry."
    return "physical_estimate_v1", "Canonical Bayer CFA materials and stack geometry are available."


def _physical_estimate_config(
    record: Mapping[str, Any], *, root: str | Path | None,
    illumination: Mapping[str, Any] | None, collection_efficiency: float,
    optical_assumptions: Mapping[str, Any] | None, assumptions: Mapping[str, Any] | None,
    readout_profile: str | None, operating_point: str | None, strategy: str,
    allow_shared_proxy: bool, require_sensor_specific: bool,
) -> dict[str, Any]:
    from .physical_estimate import image_sensor_physical_model, physical_response
    from .sensor_readout_profiles import resolve_sensor_readout

    selected_strategy = _normalize_config_strategy(strategy)
    if allow_shared_proxy or require_sensor_specific or selected_strategy == "lut_only":
        raise ValueError(
            "physical_estimate_v1 is an analytic estimate; use model='legacy' for LUTs."
        )
    resolved = resolve_sensor_readout(
        record["sensor_id"], profile=readout_profile, operating_point=operating_point,
        overrides=assumptions, root=root,
    )
    model = image_sensor_physical_model(
        record["sensor_id"], root=root, collection_efficiency=collection_efficiency,
        optical_assumptions=optical_assumptions, readout=resolved["readout"],
    )
    assumed_normal = illumination is None
    illumination = _deep_copy_dict(illumination if illumination is not None else {
        "directions": [{"theta_deg": 0.0, "phi_deg": 0.0, "weight": 1.0,
                        "polarization": "unpolarized"}],
    })
    physical_response(model, model["wavelength_nm"], illumination)
    descriptor = {
        "kind": "physical_estimate_v1", "sensor_id": record["sensor_id"],
        "root": str(image_sensor_db_root(root).resolve()),
        "collection_efficiency": float(collection_efficiency),
        "optical_assumptions": _deep_copy_dict(optical_assumptions or {}),
        "readout_profile": resolved["profile"], "operating_point": resolved["operating_point"],
        "readout_overrides": _deep_copy_dict(assumptions or {}),
        "reviewed_evidence_sha256": model["provenance"]["reviewed_evidence_sha256"],
    }
    response_binding = {
        "kind": "physical_estimate_v1", "scope": "sensor_specific_estimate",
        "sensor_id": record["sensor_id"], "attached": True, "estimate_only": True,
        "measured_accuracy_verified": False,
        "descriptor_identity": _deep_copy_dict(descriptor),
    }
    binding = {
        "scope": "missing", "sensor_id": record["sensor_id"], "sensor_specific": False,
        "attached": False, "measured_accuracy_verified": False,
    }
    scenario = {
        "name": f"physical_sensor_{record['sensor_id']}", "sensor": {},
        "sensor_model": descriptor, "sensor_illumination": illumination,
        "fdtd": {"enabled": False}, "tcad": {"enabled": False},
        "image_sensor_db": {
            "sensor_id": record["sensor_id"], "code": record["code"],
            "strategy": selected_strategy, "model": "physical_estimate_v1",
            "physics_binding": binding, "declared_scope": "missing",
            "require_sensor_specific": False, "physics_artifacts": {"fdtd": None, "tcad": None},
            "optical_response_binding": response_binding,
            "illumination_assumption": "normal_incidence" if assumed_normal else "explicit",
        },
    }
    return {
        "schema_version": "camerae2e_image_sensor_db_config_v1", "model": "physical_estimate_v1",
        "sensor_id": record["sensor_id"], "code": record["code"],
        "manufacturer": record["manufacturer"], "device_name": record["device_name"],
        "strategy": selected_strategy, "readiness_tier": "proxy", "scenario": scenario,
        "sensor": {}, "fdtd": None, "tcad": None, "physics_binding": binding,
        "optical_response_binding": response_binding, "record_summary": _record_summary(record),
        "readout_profile": resolved,
        "paths": {"catalog_path": str(image_sensor_db_catalog_path(root))},
        "policy": {
            "schema_version": "camerae2e_sensor_db_policy_v1", "estimate_only": True,
            "component_tiers": {"sensor_db_metadata": "proxy", "fast_optical_response": "proxy",
                                "collection_efficiency": "proxy", "readout_profile": "proxy",
                                "fdtd_lut": "missing", "tcad_collection": "missing"},
            "model_provenance": model["provenance"],
            "readout_provenance": resolved["provenance"],
            "optical_response_binding": response_binding,
            "truth_boundary": (
                "Fast optical estimate with proxy CFA/microlens geometry and revision-specific "
                "camera readout proxies or explicit assumptions; not measured sensor calibration."
            ),
        },
    }


def _estimate_config(
    record: Mapping[str, Any], *, root: str | Path | None,
    assumptions: Mapping[str, Any] | None, strategy: str,
    allow_shared_proxy: bool, require_sensor_specific: bool,
) -> dict[str, Any]:
    from .sensor_estimate import image_sensor_db_model

    resolved_strategy = _normalize_config_strategy(strategy)
    if allow_shared_proxy or require_sensor_specific or resolved_strategy == "lut_only":
        raise ValueError(
            "db_estimate_v1 does not attach FDTD/TCAD artifacts. Use model='legacy' "
            "for lut_only, allow_shared_proxy or require_sensor_specific."
        )
    estimate = image_sensor_db_model(record["sensor_id"], root=root, assumptions=assumptions)
    descriptor = {
        "kind": "db_estimate_v1", "sensor_id": record["sensor_id"],
        "root": str(image_sensor_db_root(root).resolve()),
        "assumptions": _deep_copy_dict(assumptions or {}),
    }
    binding = {
        "scope": "missing", "sensor_id": record["sensor_id"], "sensor_specific": False,
        "measured_accuracy_verified": False, "attached": False,
    }
    scenario = {
        "name": f"image_sensor_db_{record['sensor_id']}", "sensor": {},
        "sensor_model": descriptor,
        "fdtd": {"enabled": False}, "tcad": {"enabled": False},
        "image_sensor_db": {
            "sensor_id": record["sensor_id"], "code": record["code"],
            "strategy": resolved_strategy, "model": "db_estimate_v1",
            "physics_binding": dict(binding), "declared_scope": "missing",
            "require_sensor_specific": False, "physics_artifacts": {"fdtd": None, "tcad": None},
        },
    }
    return {
        "schema_version": "camerae2e_image_sensor_db_config_v1", "model": "db_estimate_v1",
        "sensor_id": record["sensor_id"], "code": record["code"],
        "manufacturer": record["manufacturer"], "device_name": record["device_name"],
        "strategy": resolved_strategy, "readiness_tier": "proxy",
        "scenario": scenario, "sensor": {}, "fdtd": None, "tcad": None,
        "paths": {"catalog_path": str(image_sensor_db_catalog_path(root))},
        "physics_binding": binding, "record_summary": _record_summary(record),
        "policy": {
            "schema_version": "camerae2e_sensor_db_policy_v1",
            "component_tiers": {
                "sensor_db_metadata": "proxy", "analytic_sensor_proxy": "proxy",
                "db_optical_estimate": "proxy", "fdtd_lut": "missing", "tcad_collection": "missing",
            },
            "physics_binding": binding, "estimate_only": True,
            "model_provenance": estimate["provenance"],
            "truth_boundary": (
                "Database-derived estimate with common CFA proxies and assumed readout; "
                "not measured product calibration."
            ),
        },
    }


def _fdtd_estimate_config(
    record: Mapping[str, Any], *, root: str | Path | None,
    lut_path: str | Path | None, illumination: Mapping[str, Any] | None,
    collection_efficiency: float, assumptions: Mapping[str, Any] | None,
    strategy: str, allow_shared_proxy: bool,
    readout_profile: str | None = None, operating_point: str | None = None,
) -> dict[str, Any]:
    from .fdtd_estimate import image_sensor_fdtd_model

    selected_strategy = _normalize_config_strategy(strategy)
    if allow_shared_proxy or selected_strategy == "analytic_only":
        raise ValueError("fdtd_estimate_v1 requires its own LUT and hybrid/lut_only strategy.")
    if lut_path is None:
        raise ValueError("fdtd_estimate_v1 requires an explicit, validated lut_path.")
    if not isinstance(illumination, Mapping) or not illumination:
        raise ValueError("fdtd_estimate_v1 requires explicit sensor_illumination.")
    readout = _deep_copy_dict(assumptions or {})
    allowed = {"gain", "full_well_e", "read_noise_e", "dark_current_e_per_s",
               "bit_depth", "black_level_dn"}
    if set(readout) - allowed:
        raise ValueError(
            "FDTD assumptions accept only readout settings; use collection_efficiency."
        )
    model = image_sensor_fdtd_model(
        record["sensor_id"], lut_path, root=root,
        collection_efficiency=collection_efficiency,
    )
    descriptor = {
        "kind": "fdtd_estimate_v1", "sensor_id": record["sensor_id"],
        "root": str(image_sensor_db_root(root).resolve()),
        "lut_path": str(Path(lut_path).expanduser().resolve()),
        "collection_efficiency": float(collection_efficiency),
        "readout": readout,
    }
    if readout_profile is not None or operating_point is not None:
        from .sensor_readout_profiles import resolve_sensor_readout

        resolved = resolve_sensor_readout(
            record["sensor_id"], profile=readout_profile, operating_point=operating_point,
            overrides=readout, root=root,
        )
        descriptor.update(
            readout=resolved["readout"], readout_profile=resolved["profile"],
            operating_point=resolved["operating_point"], readout_overrides=readout,
        )
    response_binding = {
        "scope": "sensor_specific", "sensor_id": record["sensor_id"],
        "lut_path": descriptor["lut_path"], "attached": True,
        "estimate_only": True, "measured_accuracy_verified": False,
    }
    # Old multiplier layers are disabled; the new aggregate model has its own binding.
    legacy_binding = {
        "scope": "missing", "sensor_id": record["sensor_id"], "sensor_specific": False,
        "attached": False, "measured_accuracy_verified": False,
    }
    scenario = {
        "name": f"fdtd_sensor_{record['sensor_id']}", "sensor": {},
        "sensor_model": descriptor, "sensor_illumination": _deep_copy_dict(illumination),
        "fdtd": {"enabled": False}, "tcad": {"enabled": False},
        "image_sensor_db": {
            "sensor_id": record["sensor_id"], "code": record["code"],
            "strategy": selected_strategy, "model": "fdtd_estimate_v1",
            "physics_binding": legacy_binding, "declared_scope": "missing",
            "require_sensor_specific": False, "physics_artifacts": {"fdtd": None, "tcad": None},
            "optical_response_binding": response_binding,
        },
    }
    return {
        "schema_version": "camerae2e_image_sensor_db_config_v1", "model": "fdtd_estimate_v1",
        "sensor_id": record["sensor_id"], "code": record["code"],
        "manufacturer": record["manufacturer"], "device_name": record["device_name"],
        "strategy": selected_strategy, "readiness_tier": "proxy", "scenario": scenario,
        "sensor": {}, "fdtd": None, "tcad": None, "physics_binding": legacy_binding,
        "optical_response_binding": response_binding, "record_summary": _record_summary(record),
        "paths": {"catalog_path": str(image_sensor_db_catalog_path(root)),
                  "lut_path": descriptor["lut_path"]},
        "policy": {
            "schema_version": "camerae2e_sensor_db_policy_v1", "estimate_only": True,
            "component_tiers": {"sensor_db_metadata": "proxy", "fdtd_response": "validated",
                                "collection_efficiency": "proxy", "tcad_collection": "missing"},
            "model_provenance": model.get("provenance", {}),
            "optical_response_binding": response_binding,
            "truth_boundary": (
                "Numerically validated optical estimate with assumed collection/readout; "
                "not measured product calibration."
            ),
        },
    }


def image_sensor_db_optimize_camera_parameters(
    sensor_id_or_code: str,
    *,
    strategy: str = "analytic_only",
    base_scenario: Mapping[str, Any] | None = None,
    preset: str = "raw_factory",
    parameter_space: Mapping[str, Any] | None = None,
    objective: Any | None = None,
    method: str = "grid",
    max_cases: int | None = None,
    constraints: Any | None = None,
    scene: Any | None = None,
    camera: Any | None = None,
    asset_store: Any | None = None,
    seed: int = 0,
    top_k: int = 5,
    include_arrays: bool = False,
    root: str | Path | None = None,
    allow_shared_proxy: bool = False,
    require_sensor_specific: bool = False,
    model: str = "auto",
    assumptions: Mapping[str, Any] | None = None,
    lut_path: str | Path | None = None,
    sensor_illumination: Mapping[str, Any] | None = None,
    collection_efficiency: float | None = None,
    optical_assumptions: Mapping[str, Any] | None = None,
    readout_profile: str | None = None,
    operating_point: str | None = None,
) -> dict[str, Any]:
    """Optimize camera parameters from an image-sensor DB selection.

    This is the direct bridge from sensor DB selection to the FACA optimizer.
    The selected DB record is preserved in ``source_image_sensor_db`` and in the
    selected scenario dictionaries so RAW dataset exports keep provenance.
    """

    from .optimization import camerae2e_optimize_camera_parameters

    config = image_sensor_db_config(
        sensor_id_or_code, strategy=strategy, root=root,
        allow_shared_proxy=allow_shared_proxy, require_sensor_specific=require_sensor_specific,
        model=model, assumptions=assumptions,
        lut_path=lut_path, sensor_illumination=sensor_illumination,
        collection_efficiency=collection_efficiency,
        optical_assumptions=optical_assumptions, readout_profile=readout_profile,
        operating_point=operating_point,
    )
    scenario = _merge_scenario_dicts(config["scenario"], base_scenario or {})
    # Catalog provenance belongs to the selected record, not caller-supplied
    # stale image_sensor_db metadata from a different optimization run.
    scenario["image_sensor_db"] = _deep_copy_dict(config["scenario"]["image_sensor_db"])
    scenario = image_sensor_db_reconcile_scenario(scenario)
    result = camerae2e_optimize_camera_parameters(
        scenario,
        preset=preset,
        parameter_space=parameter_space,
        objective=objective,
        method=method,
        max_cases=max_cases,
        constraints=constraints,
        scene=scene,
        camera=camera,
        asset_store=asset_store,
        seed=seed,
        top_k=top_k,
        include_arrays=include_arrays,
    )
    # Runtime revalidation also covers parameter-space overrides. Reconcile the
    # returned scenarios defensively so persisted selections cannot retain a
    # stale sensor-specific label even with a custom optimizer/report provider.
    result["selected_scenarios"] = [
        dict(item) if item.get("image_sensor_db", {}).get("physics_binding_from_camera", False)
        else image_sensor_db_reconcile_scenario(item)
        for item in result.get("selected_scenarios", [])
    ]
    effective_binding = dict(scenario["image_sensor_db"]["physics_binding"])
    selected_bindings = [
        item.get("image_sensor_db", {}).get("physics_binding")
        for item in result["selected_scenarios"]
    ]
    if selected_bindings:
        if all(binding == selected_bindings[0] for binding in selected_bindings):
            effective_binding = dict(selected_bindings[0])
        else:
            effective_binding = {
                "scope": "mixed", "sensor_id": config["sensor_id"],
                "sensor_specific": False, "measured_accuracy_verified": False,
                "attached": any(binding.get("attached", False) for binding in selected_bindings),
            }
    policy = _deep_copy_dict(config["policy"])
    policy["physics_binding"] = effective_binding
    effective_scenarios = result["selected_scenarios"] or [scenario]
    layer_presence = {
        layer: [_physics_layer_identity(layer, item.get(layer)) is not None
                for item in effective_scenarios]
        for layer in ("fdtd", "tcad")
    }
    policy["component_variation"] = {
        layer: len(set(presence)) > 1 for layer, presence in layer_presence.items()
    }
    for layer, tier in (("fdtd", "proxy"), ("tcad", "calibration_required")):
        policy["component_tiers"]["fdtd_lut" if layer == "fdtd" else "tcad_collection"] = (
            tier if any(layer_presence[layer]) else "missing"
        )
    selected_models = [_jsonable_value(item.get("sensor_model")) for item in effective_scenarios]
    expected_model = config["scenario"].get("sensor_model")
    model_scope = "selected"
    if any(item != expected_model for item in selected_models):
        model_scope = (
            "overridden" if all(item == selected_models[0] for item in selected_models) else "mixed"
        )
        # The original record remains the optimization source, but its optical
        # provenance must not be presented as the effective model after selection.
        policy.pop("model_provenance", None)
        policy.pop("readout_provenance", None)
    policy["model_binding"] = {"scope": model_scope, "selected_sensor_models": selected_models}
    if expected_model is not None or any(item is not None for item in selected_models):
        policy["component_tiers"]["db_optical_estimate"] = (
            "proxy" if any(isinstance(item, Mapping) and item.get("kind") == "db_estimate_v1"
                           for item in selected_models) else "missing"
        )
        for kind, component, tier in (
            ("physical_estimate_v1", "fast_optical_response", "proxy"),
            ("fdtd_estimate_v1", "fdtd_response", "validated"),
        ):
            policy["component_tiers"][component] = (
                tier if any(isinstance(item, Mapping) and item.get("kind") == kind
                            for item in selected_models) else "missing"
            )
        selected_responses = [
            item.get("image_sensor_db", {}).get("optical_response_binding")
            for item in effective_scenarios
        ]
        policy["selected_optical_response_bindings"] = selected_responses
        if all(item == selected_responses[0] for item in selected_responses):
            policy["optical_response_binding"] = selected_responses[0]
        else:
            policy["optical_response_binding"] = {"scope": "mixed", "estimate_only": True}
    result["source_image_sensor_db"] = {
        "schema_version": "camerae2e_optimization_source_image_sensor_db_v1",
        "sensor_id": config["sensor_id"],
        "code": config["code"],
        "manufacturer": config["manufacturer"],
        "device_name": config["device_name"],
        "strategy": config["strategy"],
        "model": config["model"],
        "selected_sensor_models": selected_models,
        "readiness_tier": "calibration_required" if any(layer_presence["tcad"]) else "proxy",
        "physics_binding": effective_binding,
        "selected_physics_bindings": selected_bindings,
        "policy": policy,
        "record_summary": config["record_summary"],
    }
    return result


def image_sensor_db_reconcile_scenario(
    scenario: Mapping[str, Any], *, require_sensor_specific: bool | None = None,
    actual_physics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh catalog association after scenario overrides, without claiming calibration.

    FACA calls this before computing each case so parameter-space overrides are
    checked as well as the optimizer's base scenario. Unbound components are
    explicitly disabled to avoid inheriting physics from an existing Camera.
    After attachment, ``actual_physics`` can supply the sensor's fdtd/tcad
    config dictionaries to verify the resolved in-memory artifact identities.
    """
    updated = dict(scenario)
    raw_metadata = scenario.get("image_sensor_db")
    if not isinstance(raw_metadata, Mapping):
        return updated
    metadata = dict(raw_metadata)
    expected = metadata.get("physics_artifacts")
    expected = dict(expected) if isinstance(expected, Mapping) else {}
    original = metadata.get("physics_binding")
    original = dict(original) if isinstance(original, Mapping) else {}
    declared_scope = str(metadata.get("declared_scope", original.get("scope", "missing")))
    actual = {}
    for layer in ("fdtd", "tcad"):
        config = updated.get(layer)
        if not isinstance(config, Mapping):
            updated[layer] = {"enabled": False}
        effective = actual_physics.get(layer) if actual_physics is not None else updated.get(layer)
        actual[layer] = _physics_layer_identity(layer, effective)
    active = {layer: identity for layer, identity in actual.items() if identity is not None}
    mismatches = [layer for layer, identity in active.items() if identity != expected.get(layer)]
    attached = bool(active)
    scope = "overridden" if mismatches else declared_scope
    if not attached and scope != "missing":
        scope = "disabled"
    sensor_specific = attached and not mismatches and declared_scope == "sensor_specific"
    binding = {
        "scope": scope,
        "sensor_id": metadata.get("sensor_id"),
        "sensor_specific": bool(sensor_specific),
        "measured_accuracy_verified": False,
        "attached": attached,
        "overridden_components": mismatches,
    }
    strict = (
        bool(metadata.get("require_sensor_specific", False))
        if require_sensor_specific is None else bool(require_sensor_specific)
    )
    if strict and not sensor_specific:
        raise ValueError("Sensor-specific physics artifacts are required after scenario overrides.")
    metadata["physics_binding"] = binding
    response = metadata.get("optical_response_binding")
    if isinstance(response, Mapping):
        descriptor = updated.get("sensor_model", {})
        descriptor = descriptor if isinstance(descriptor, Mapping) else {}
        selected_path = descriptor.get("lut_path")
        selected_path = (
            str(Path(selected_path).expanduser().resolve()) if selected_path is not None else None
        )
        physical = response.get("kind") == "physical_estimate_v1"
        same = (
            _jsonable_value(descriptor) == response.get("descriptor_identity")
            if physical else (
                descriptor.get("kind") == "fdtd_estimate_v1"
                and descriptor.get("sensor_id") == response.get("sensor_id")
                and selected_path == response.get("lut_path")
            )
        )
        metadata["optical_response_binding"] = {
            **dict(response),
            "scope": ("sensor_specific_estimate" if physical else "sensor_specific")
            if same else "overridden",
            "attached": descriptor.get("kind") in {"fdtd_estimate_v1", "physical_estimate_v1"},
            "selected_sensor_id": descriptor.get("sensor_id"),
            "selected_lut_path": selected_path,
        }
    metadata["require_sensor_specific"] = strict
    metadata["physics_binding_from_camera"] = actual_physics is not None
    updated["image_sensor_db"] = metadata
    return updated


def _physics_layer_identity(layer: str, config: Any) -> Any:
    if not isinstance(config, Mapping) or not bool(config.get("enabled", True)):
        return None
    key = "lut" if layer == "fdtd" else "db"
    value = config.get(key)
    if layer == "tcad" and value is None and any(
        name in config
        for name in ("generation_map_path", "collection_summary_paths", "accuracy_gate_path")
    ):
        value = config  # tcad_sensor_config also accepts flattened DB path mappings.
    if value is None:
        raise ValueError(f"Enabled sensor DB {layer} requires an explicit {key} artifact.")

    def path_identity(path: Any) -> str | None:
        if path is None:
            return None
        return str(Path(path).expanduser().resolve()) if isinstance(path, (str, Path)) else None

    if layer == "fdtd":
        return (
            path_identity(value)
            or path_identity(getattr(value, "source_path", None))
            or {"unresolved_lut": True}
        )
    if isinstance(value, TCADSensorDB):
        value = {
            "generation_map_path": (
                None if value.generation_map is None else value.generation_map.source_path
            ),
            "accuracy_gate_path": (
                None if value.accuracy_gate is None else value.accuracy_gate.source_path
            ),
            "collection_summary_paths": [
                summary.source_path for summary in value.collection_summaries
            ],
        }
    if not isinstance(value, Mapping):
        return {"unresolved_db": True}
    paths = {
        "generation_map_path": path_identity(value.get("generation_map_path")),
        "accuracy_gate_path": path_identity(value.get("accuracy_gate_path")),
    }
    summaries = value.get("collection_summary_paths")
    paths["collection_summary_paths"] = (
        sorted(path_identity(item) or "<unresolved>" for item in summaries)
        if isinstance(summaries, (list, tuple)) else None
    )
    return paths


def image_sensor_db_summary(root: str | Path | None = None) -> dict[str, Any]:
    """Return compact image-sensor DB summary facts."""

    catalog = image_sensor_db_load(root)
    records = image_sensor_db_records(root=root)
    manufacturers: dict[str, int] = {}
    architectures: dict[str, int] = {}
    for record in records:
        manufacturers[record["manufacturer"]] = manufacturers.get(record["manufacturer"], 0) + 1
        arch = str(record.get("pixel_architecture") or "unknown")
        architectures[arch] = architectures.get(arch, 0) + 1
    return {
        "schema": catalog.get("schema"),
        "generated_at": catalog.get("generated_at"),
        "record_count": len(records),
        "manufacturer_count": len(manufacturers),
        "manufacturers": dict(sorted(manufacturers.items(), key=lambda item: (-item[1], item[0]))),
        "pixel_architectures": dict(
            sorted(architectures.items(), key=lambda item: (-item[1], item[0]))
        ),
        "catalog_path": str(image_sensor_db_catalog_path(root)),
    }


def _normalize_config_strategy(strategy: str) -> str:
    normalized = str(strategy).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "hybrid": "hybrid",
        "combo": "hybrid",
        "combination": "hybrid",
        "db_lut_preferred": "hybrid",
        "lut": "lut_only",
        "lut_only": "lut_only",
        "db_only": "lut_only",
        "analytic": "analytic_only",
        "analytic_only": "analytic_only",
        "proxy": "analytic_only",
        "free_configuration": "analytic_only",
    }
    if normalized not in aliases:
        raise ValueError("strategy must be one of: hybrid, lut_only, analytic_only.")
    return aliases[normalized]


def _merge_scenario_dicts(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    merged = _deep_copy_dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            nested = _deep_copy_dict(merged[key])
            nested.update(_deep_copy_dict(value))
            merged[key] = nested
        else:
            merged[key] = _jsonable_value(value)
    return merged


def _deep_copy_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable_value(item) for key, item in value.items()}


def _analytic_sensor_config_from_record(
    record: Mapping[str, Any], *, include_ocl_proxy: bool
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    pixel_pitch_um = _finite_float(record.get("pixel_pitch_um"))
    if pixel_pitch_um is not None and pixel_pitch_um > 0.0:
        config["pixel_size"] = pixel_pitch_um * 1.0e-6

    cfa_preset = _cfa_preset_from_record(record)
    if cfa_preset is not None:
        config["cfa_preset"] = cfa_preset

    if include_ocl_proxy:
        ocl_shape = _ocl_group_shape_from_record(record)
        if ocl_shape is not None:
            config["ocl_group_shape"] = ocl_shape
            config["ocl_group_equalization"] = 1.0 if ocl_shape == "2x2" else 0.0
    return config


def _cfa_preset_from_record(record: Mapping[str, Any]) -> str | None:
    pattern = str(record.get("cfa_pattern") or "").strip().lower()
    if pattern in {"bayer", "rggb_bayer"}:
        return "bayer_rgb"
    if pattern in {"quad_bayer", "tetracell_bayer"}:
        return "quad_bayer_rgb"
    return None


def _ocl_group_shape_from_record(record: Mapping[str, Any]) -> str | None:
    pattern = str(record.get("cfa_pattern") or "").strip().lower()
    architecture = str(record.get("pixel_architecture") or "").strip().lower()
    if pattern in {"quad_bayer", "tetracell_bayer"}:
        return "2x2"
    if architecture in {"four_shared", "quad_pixel", "super_qpd"}:
        return "2x2"
    if architecture in {"standalone", "two_shared", "dual_pixel", "eight_shared"}:
        return "1x1"
    return None


def _record_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "sensor_id",
        "code",
        "manufacturer",
        "device_name",
        "pixel_pitch_um",
        "pixel_architecture",
        "cfa_pattern",
        "illumination",
        "microlens_type",
        "dti_type",
        "has_dti",
        "has_pdaf",
        "has_hdr",
        "has_lofic",
        "stack_config_path",
        "tcad_profile_path",
    )
    return {key: record.get(key) for key in keys}


def _jsonable_parameters(params: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable_value(value) for key, value in params.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    return value


def _path_string(value: Any) -> str:
    return str(Path(value).expanduser()) if value is not None else ""


def _path_exists(value: Any) -> bool:
    return value is not None and Path(value).expanduser().exists()


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _normalize_record(raw: dict[str, Any], root: Path) -> dict[str, Any]:
    metadata = dict(raw.get("metadata", {}))
    specs = dict(raw.get("derived_specs", {}))
    generated = dict(raw.get("generated_files", {}))
    stack_path = generated.get("stack_config")
    tcad_path = generated.get("tcad_profile")
    sensor_id = _sensor_id(raw, stack_path)
    return {
        "sensor_id": sensor_id,
        "code": str(raw.get("code", metadata.get("code", ""))),
        "manufacturer": str(metadata.get("manufacturer", "Unknown")),
        "device_name": str(metadata.get("device_name", "")),
        "title": str(metadata.get("title", raw.get("description_excerpt", ""))),
        "report_type": str(metadata.get("report_type", "")),
        "analysis_year": metadata.get("analysis_year"),
        "pixel_pitch_um": specs.get("pixel_pitch_um"),
        "pixel_architecture": specs.get("pixel_architecture"),
        "cfa_pattern": specs.get("cfa_pattern"),
        "illumination": specs.get("illumination"),
        "microlens_type": specs.get("microlens_type"),
        "optical_stack_height_um": specs.get("optical_stack_height_um"),
        "active_si_thickness_um": specs.get("active_si_thickness_um"),
        "dti_type": specs.get("dti_type"),
        "has_dti": specs.get("has_dti"),
        "has_pdaf": specs.get("has_pdaf"),
        "has_hdr": specs.get("has_hdr"),
        "has_lofic": specs.get("has_lofic"),
        "resolution_mp": specs.get("resolution_mp"),
        "optical_format": specs.get("optical_format"),
        "stack_config_path": str(Path(stack_path).expanduser()) if stack_path else None,
        "tcad_profile_path": str(Path(tcad_path).expanduser()) if tcad_path else None,
        "raw": raw,
        "db_root": str(root),
    }


def _sensor_id(raw: dict[str, Any], stack_path: str | None) -> str:
    if stack_path:
        return Path(stack_path).stem
    metadata = dict(raw.get("metadata", {}))
    parts = [raw.get("code", ""), metadata.get("manufacturer", ""), metadata.get("device_name", "")]
    return _slugify("_".join(str(part) for part in parts if part))


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return "_".join(part for part in cleaned.split())


imageSensorDBRoot = image_sensor_db_root  # noqa: N816
imageSensorDBCatalogPath = image_sensor_db_catalog_path  # noqa: N816
imageSensorDBLoad = image_sensor_db_load  # noqa: N816
imageSensorDBRecords = image_sensor_db_records  # noqa: N816
imageSensorDBGet = image_sensor_db_get  # noqa: N816
imageSensorDBParameters = image_sensor_db_parameters  # noqa: N816
imageSensorDBConfig = image_sensor_db_config  # noqa: N816
imageSensorDBOptimizeCameraParameters = image_sensor_db_optimize_camera_parameters  # noqa: N816
imageSensorDBSummary = image_sensor_db_summary  # noqa: N816

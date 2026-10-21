"""Bounded RAW sensitivity studies for existing FDTD and TCAD proxy assets.

This path intentionally uses the generic pyisetcam sensor. A saved sensor
simulation revision already owns its optical response, so multiplying either
LUT on top of that revision would silently double-count unknown physics.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam import camera_get, camerae2e_run_scenario, sensor_get
from pyisetcam.assets import AssetStore
from pyisetcam.fdtd_sensor import (
    FDTDSensorLUT,
    _resolve_lut_sidecar_path,
    fdtd_sensor_default_lut_path,
    fdtd_sensor_field_response_map,
    fdtd_sensor_lut_load,
    fdtd_sensor_lut_summary,
    fdtd_sensor_lut_validate,
    fdtd_sensor_physics_validate,
)
from pyisetcam.tcad_sensor import (
    TCADSensorDB,
    tcad_sensor_collection_efficiency,
    tcad_sensor_db_load,
    tcad_sensor_default_root,
    tcad_sensor_summary,
    tcad_sensor_validate,
)

from .offline_assets import offline_asset_identity, offline_asset_store

SCHEMA = "camerae2e_physics_lut_raw_sensitivity_v1"
JOB_KIND = "physics_lut_sensitivity"
_SCENE = "uniform ee"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows_sha256(lut: FDTDSensorLUT) -> str:
    payload = json.dumps(
        {"summaries": lut.summary_rows, "long_rows": lut.long_rows},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fdtd_source_hashes(lut: FDTDSensorLUT) -> dict[str, str]:
    paths = [lut.source_path]
    if lut.source_path.suffix.lower() == ".json":
        for key, fallback in (
            ("summary_csv", "camera_lut_summary.csv"),
            ("long_csv", "camera_lut_long.csv"),
        ):
            path = _resolve_lut_sidecar_path(
                lut.source_path, lut.metadata.get(key), fallback,
            )
            if path.exists():
                paths.append(path)
    return {str(path): _sha256(path) for path in paths}


def _tcad_source_hashes(db: TCADSensorDB) -> dict[str, str]:
    paths = []
    if db.generation_map is not None:
        paths.append(db.generation_map.source_path)
    paths.extend(item.source_path for item in db.collection_summaries)
    if db.accuracy_gate is not None:
        paths.append(db.accuracy_gate.source_path)
    return {str(path): _sha256(path) for path in paths}


def inspect_fdtd_lut(path: str | Path | None = None) -> dict[str, Any]:
    """Inspect the exact optical proxy supplied for a bounded field study."""
    selected = path if path is not None else fdtd_sensor_default_lut_path()
    if selected is None:
        raise FileNotFoundError("No FDTD LUT was selected and no default is available")
    lut = fdtd_sensor_lut_load(selected)
    return _fdtd_inspection(lut)


def _fdtd_inspection(lut: FDTDSensorLUT) -> dict[str, Any]:
    validation = fdtd_sensor_lut_validate(lut)
    physics = fdtd_sensor_physics_validate(lut)
    summary = fdtd_sensor_lut_summary(lut)
    rows = lut.summary_rows
    center = any(str(row.get("case")) == "center" for row in rows)
    field_cases = sorted({
        str(row.get("case")) for row in rows
        if row.get("field_x_norm") is not None
        and not math.isclose(float(row["field_x_norm"]), 0.0)
    })
    return {
        "schema": "camerae2e_fdtd_lut_inspection_v1",
        "source_path": str(lut.source_path),
        "source_hashes": _fdtd_source_hashes(lut),
        "loaded_response_rows_sha256": _rows_sha256(lut),
        "summary": summary,
        "validation": validation,
        "physics_validation": physics,
        "available_effects": {
            "center_relative_field_at_selected_wavelength": bool(center and field_cases),
            "crosstalk_kernel": bool(lut.long_rows and summary["kernel_shape"] != [0, 0]),
            "field_cases": field_cases,
        },
        "quantity": "silicon optical absorption response proxy, not measured EQE",
        "normalization": "same-wavelength center-relative ratio",
        "physical_accuracy": "not_established",
    }


def inspect_tcad_db(*, root: str | Path | None = None) -> dict[str, Any]:
    """Inspect the collection framework and its separate accuracy gate."""
    db = tcad_sensor_db_load(root=root)
    return _tcad_inspection(db)


def _tcad_inspection(db: TCADSensorDB) -> dict[str, Any]:
    validation = tcad_sensor_validate(db)
    return {
        "schema": "camerae2e_tcad_lut_inspection_v1",
        "source_hashes": _tcad_source_hashes(db),
        "summary": tcad_sensor_summary(db),
        "validation": validation,
        "quantity": "collected-current / FDTD generation-current proxy",
        "normalization": "same-wavelength center collection-efficiency ratio",
        "physical_accuracy": "not_established",
    }


def _settings(size: Sequence[int], exposure_s: float, seed: int) -> tuple[tuple[int, int], float]:
    if (
        not isinstance(size, (list, tuple)) or len(size) != 2
        or any(type(value) is not int or not 8 <= value <= 32 for value in size)
    ):
        raise ValueError("size must be two integer dimensions from 8 to 32")
    if (
        isinstance(exposure_s, bool) or not isinstance(exposure_s, (int, float))
        or not math.isfinite(exposure_s) or not 0 < exposure_s <= 1
    ):
        raise ValueError("exposure_s must be finite and in (0, 1] seconds")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer")
    return (int(size[0]), int(size[1])), float(exposure_s)


def _scenario(size: tuple[int, int], exposure_s: float) -> dict[str, Any]:
    return {
        "scene": {"type": _SCENE, "args": [list(size)]},
        "sensor": {"size": list(size), "integration_time": exposure_s, "noise_flag": 0},
        "sensor_resize": False,
        "fdtd": {"enabled": False},
        "tcad": {"enabled": False},
    }


def _capture(
    scenario: Mapping[str, Any], *, seed: int, size: tuple[int, int], asset_store: AssetStore,
) -> dict[str, Any]:
    # A few legacy nested helpers still request AssetStore.default(). Bind
    # those lookups to this already-verified local snapshot for the call only.
    with AssetStore.bound_default(asset_store):
        computed = camerae2e_run_scenario(
            scenario, seed=seed, include_arrays=False, asset_store=asset_store,
        )
    sensor = camera_get(computed["camera"], "sensor")
    raw = np.asarray(sensor.data["raw_dn"])
    photo = np.asarray(sensor.data["mean_electrons"], dtype=float)
    if raw.dtype != np.dtype("int32") or raw.shape != size or photo.shape != size:
        raise ValueError("Physics LUT capture returned an unsupported RAW shape or dtype")
    return {
        "raw": {
            "dtype": "int32", "unit": "DN", "shape": list(size),
            "sha256": hashlib.sha256(raw.tobytes(order="C")).hexdigest(),
            "values": raw.tolist(), "mean": float(np.mean(raw)),
        },
        "photoelectrons": {
            "unit": "electron/pixel before readout", "shape": list(size),
            "mean": float(np.mean(photo)), "values": photo.tolist(),
        },
        "sensor": {
            "name": sensor_get(sensor, "name"),
            "pixel_pitch_m": [float(v) for v in sensor_get(sensor, "pixel size")],
            "filter_names": list(sensor_get(sensor, "filter names")),
            "readout": computed["sensor_capture"]["readout"],
            "saturation": computed["sensor_capture"]["saturation"],
        },
    }


def _comparison(
    *, layer: str, source: dict[str, Any], base: dict[str, Any], altered: dict[str, Any],
    size: tuple[int, int], exposure_s: float, seed: int, effect: dict[str, Any],
    limitations: list[str], upstream_assets: dict[str, Any],
) -> dict[str, Any]:
    first = np.asarray(base["raw"]["values"], dtype=np.int32)
    second = np.asarray(altered["raw"]["values"], dtype=np.int32)
    raw_delta = second.astype(np.int64) - first.astype(np.int64)
    photo_delta = np.asarray(altered["photoelectrons"]["values"]) - np.asarray(
        base["photoelectrons"]["values"]
    )
    fixed_sensor_keys = ("name", "pixel_pitch_m", "filter_names", "readout")
    if any(base["sensor"][key] != altered["sensor"][key] for key in fixed_sensor_keys):
        raise ValueError("Baseline and LUT captures did not retain the same sensor/readout")
    return {
        "schema": SCHEMA, "status": "simulated", "layer": layer,
        "source": source, "upstream_assets": upstream_assets, "effect": effect,
        "capture": {
            "scene": {"type": _SCENE, "args": [list(size)]},
            "size": list(size), "exposure_s": exposure_s, "noise_flag": 0, "seed": seed,
            "sensor_model": "pyisetcam generic default sensor; no sensor DB revision",
            "readout": base["sensor"]["readout"],
        },
        "baseline": base, "with_lut": altered,
        "difference": {
            "direction": "with_lut_minus_baseline",
            "raw_dn": {"unit": "DN", "values": raw_delta.tolist(),
                       "mean": float(np.mean(raw_delta))},
            "photoelectrons": {"unit": "electron/pixel", "values": photo_delta.tolist(),
                               "mean": float(np.mean(photo_delta))},
        },
        "physical_accuracy": "not_established", "limitations": limitations,
    }


def compare_fdtd_field_raw(
    path: str | Path | None = None, *, size: Sequence[int] = (8, 8),
    exposure_s: float = 0.004, wavelength_nm: float = 550.0, seed: int = 0,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Compare original RAW with a center-relative FDTD field-only correction.

    No absolute QE, spatial crosstalk, or TCAD collection multiplier is added.
    The saved sensor-simulation DB is not used by this generic-sensor study.
    """
    shape, exposure = _settings(size, exposure_s, seed)
    selected = path if path is not None else fdtd_sensor_default_lut_path()
    if selected is None:
        raise FileNotFoundError("No FDTD LUT was selected and no default is available")
    lut = fdtd_sensor_lut_load(selected)
    source = _fdtd_inspection(lut)
    if not source["validation"]["ok"] or source["physics_validation"]["status"] == "fail":
        raise ValueError("FDTD LUT failed structural or physics validation")
    if not source["available_effects"]["center_relative_field_at_selected_wavelength"]:
        raise ValueError("FDTD LUT needs center and noncenter field response rows")
    if (
        isinstance(wavelength_nm, bool) or not isinstance(wavelength_nm, (int, float))
        or not math.isfinite(wavelength_nm)
        or float(wavelength_nm) not in source["summary"]["wavelengths_nm"]
    ):
        raise ValueError("wavelength_nm must exactly match a LUT wavelength")
    wavelength = float(wavelength_nm)
    field_map = fdtd_sensor_field_response_map(
        lut, shape, wavelength_nm=wavelength, normalize_to_center=True,
    )
    if not np.all(np.isfinite(field_map)) or np.any(field_map < 0):
        raise ValueError("FDTD relative field map is not finite and nonnegative")
    upstream_assets = offline_asset_identity(workspace=workspace, source=lut.source_path)
    assets = offline_asset_store(workspace=workspace, source=lut.source_path)
    scenario = _scenario(shape, exposure)
    base = _capture(scenario, seed=seed, size=shape, asset_store=assets)
    altered = _capture(
        {**scenario, "fdtd": {"enabled": True, "lut": lut, "mode": "field",
                              "field_model": "radial", "wavelength_nm": wavelength,
                              "normalize_to_center": True}},
        seed=seed, size=shape, asset_store=assets,
    )
    if _fdtd_source_hashes(lut) != source["source_hashes"] or _rows_sha256(lut) != source[
        "loaded_response_rows_sha256"
    ]:
        raise ValueError("FDTD LUT changed during capture")
    if offline_asset_identity(workspace=workspace, source=lut.source_path) != upstream_assets:
        raise ValueError("Pinned upstream assets changed during FDTD capture")
    return _comparison(
        layer="fdtd_optical_relative_field", source=source, base=base, altered=altered,
        size=shape, exposure_s=exposure, seed=seed, upstream_assets=upstream_assets,
        effect={
            "quantity": "center-relative 550 nm optical absorption field response"
            if wavelength == 550.0 else "center-relative optical absorption field response",
            "wavelength_nm": wavelength, "normalization": "same-wavelength center = 1",
            "model": "radial interpolation of selected field/CRA samples",
            "field_factor": {"unit": "ratio", "min": float(np.min(field_map)),
                             "mean": float(np.mean(field_map)),
                             "max": float(np.max(field_map)), "values": field_map.tolist()},
            "absolute_qe_applied": False, "crosstalk_applied": False,
            "collection_applied": False,
        },
        limitations=[
            "The optical LUT is an absorption/field proxy, not measured product EQE.",
            "The existing sensor QE is retained; only its 2-D field response is changed.",
            "A single selected wavelength drives the post-spectral radial field map.",
            "No spatial crosstalk kernel or charge collection is inferred from missing data.",
            "The generic default sensor is not a selected saved sensor DB revision.",
            "Convergence and measured-product accuracy remain separate from RAW execution.",
        ],
    )


def compare_tcad_collection_raw(
    *, root: str | Path | None = None, case: str = "edge20x",
    wavelength_nm: float = 550.0, size: Sequence[int] = (8, 8),
    exposure_s: float = 0.004, seed: int = 0,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Compare original RAW with a TCAD-only center-relative collection proxy."""
    shape, exposure = _settings(size, exposure_s, seed)
    if not isinstance(case, str) or not case or case == "center":
        raise ValueError("case must select a noncenter collection summary")
    if (
        isinstance(wavelength_nm, bool) or not isinstance(wavelength_nm, (int, float))
        or not math.isfinite(wavelength_nm) or wavelength_nm <= 0
    ):
        raise ValueError("wavelength_nm must be finite and positive")
    db = tcad_sensor_db_load(root=root)
    source = _tcad_inspection(db)
    if not source["validation"]["framework_ready"]:
        raise ValueError("TCAD collection framework is not valid")
    wavelength = float(wavelength_nm)
    absolute_center = tcad_sensor_collection_efficiency(
        db, case="center", wavelength_nm=wavelength, normalize_to_center=False,
    )
    absolute_selected = tcad_sensor_collection_efficiency(
        db, case=case, wavelength_nm=wavelength, normalize_to_center=False,
    )
    relative = tcad_sensor_collection_efficiency(
        db, case=case, wavelength_nm=wavelength, normalize_to_center=True,
    )
    asset_source = db.source_root if db.source_root is not None else root
    upstream_assets = offline_asset_identity(workspace=workspace, source=asset_source)
    assets = offline_asset_store(workspace=workspace, source=asset_source)
    scenario = _scenario(shape, exposure)
    base = _capture(scenario, seed=seed, size=shape, asset_store=assets)
    altered = _capture(
        {**scenario, "tcad": {"enabled": True, "db": db, "mode": "collection",
                              "case": case, "wavelength_nm": wavelength,
                              "normalize_to_center": True,
                              "allow_proxy_accuracy": True}},
        seed=seed, size=shape, asset_store=assets,
    )
    if _tcad_source_hashes(db) != source["source_hashes"]:
        raise ValueError("TCAD source files changed during capture")
    if offline_asset_identity(workspace=workspace, source=asset_source) != upstream_assets:
        raise ValueError("Pinned upstream assets changed during TCAD capture")
    return _comparison(
        layer="tcad_collection_relative_proxy", source=source, base=base,
        altered=altered, size=shape, exposure_s=exposure, seed=seed,
        upstream_assets=upstream_assets,
        effect={
            "quantity": "collected current divided by same-case FDTD generation current",
            "case": case, "wavelength_nm": wavelength,
            "center_absolute_collection_efficiency": absolute_center,
            "selected_absolute_collection_efficiency": absolute_selected,
            "applied_center_relative_collection_factor": relative,
            "factor_scope": "one scalar applied to all pixels and CFA sites",
            "fdtd_optical_lut_applied": False,
        },
        limitations=[
            "TCAD is a proxy collection framework; its accuracy gate is not product-ready.",
            "This is a TCAD-only sensitivity; no FDTD optical LUT is multiplied into RAW.",
            "The selected case's one-wavelength collection ratio scales all pixels and sites.",
            "A generic default sensor supplies assumed QE/readout, not a saved sensor DB revision.",
            "The A/B result is not a product-sensor ranking or measured electrical response.",
        ],
    )


def default_spec(*, layer: str = "fdtd", source_path: str | Path | None = None) -> dict[str, Any]:
    """Make a finite study request; API callers must acknowledge its proxy scope."""
    if layer not in {"fdtd", "tcad"}:
        raise ValueError("layer must be fdtd or tcad")
    default_path = (
        fdtd_sensor_default_lut_path() if layer == "fdtd" else tcad_sensor_default_root()
    )
    return {
        "schema": SCHEMA,
        "name": "FDTD 광학 응답 민감도" if layer == "fdtd" else "TCAD 수집 응답 민감도",
        "layer": layer,
        "source_path": str(source_path or default_path or ""),
        "case": "relative_field" if layer == "fdtd" else "edge20x",
        "wavelength_nm": 550.0,
        "size": [8, 8],
        "exposure_s": 0.004,
        "seed": 0,
        "acknowledge_proxy_limits": False,
    }


def specification(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reject undeclared controls and ambiguous optical/collection semantics."""
    if not isinstance(payload, Mapping) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the physics LUT study fields")
    if payload["schema"] != SCHEMA or payload["acknowledge_proxy_limits"] is not True:
        raise ValueError("Explicit physics LUT proxy-limit acknowledgement required")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Study name requires 1..160 characters")
    layer = payload["layer"]
    if layer not in {"fdtd", "tcad"}:
        raise ValueError("layer must be fdtd or tcad")
    source_path = payload["source_path"]
    if not isinstance(source_path, str) or not 1 <= len(source_path) <= 2048:
        raise ValueError("Select one explicit LUT path or TCAD root")
    case = payload["case"]
    if layer == "fdtd" and case != "relative_field":
        raise ValueError("FDTD study supports only the relative_field effect")
    if layer == "tcad" and (not isinstance(case, str) or not case or case == "center"):
        raise ValueError("TCAD study requires an explicit noncenter collection case")
    shape, exposure = _settings(payload["size"], payload["exposure_s"], payload["seed"])
    wave = payload["wavelength_nm"]
    if isinstance(wave, bool) or not isinstance(wave, (int, float)) or not math.isfinite(wave):
        raise ValueError("wavelength_nm must be finite")
    if wave <= 0:
        raise ValueError("wavelength_nm must be positive")
    return {
        "schema": SCHEMA, "name": name.strip(), "layer": layer,
        "source_path": str(Path(source_path).expanduser().resolve()), "case": case,
        "wavelength_nm": float(wave), "size": list(shape), "exposure_s": exposure,
        "seed": payload["seed"], "acknowledge_proxy_limits": True,
    }


def _bounded_source(service: Any, source_path: str) -> Path:
    path = Path(source_path).resolve()
    root = Path(service.workspace).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Physics LUT API sources must reside under the workspace")
    return path


def _bounded_source_files(service: Any, source: Mapping[str, Any]) -> None:
    root = Path(service.workspace).resolve()
    if any(
        not Path(source_path).resolve().is_relative_to(root)
        for source_path in source["source_hashes"]
    ):
        raise ValueError("Physics LUT API source files must reside under the workspace")


def prepare(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze an asset identity and executable environment before queueing."""
    from .imaging import _current

    spec = specification(payload)
    path = _bounded_source(service, spec["source_path"])
    try:
        if spec["layer"] == "fdtd":
            if not path.is_file():
                raise FileNotFoundError(path)
            lut = fdtd_sensor_lut_load(path)
            source = _fdtd_inspection(lut)
            if not source["validation"]["ok"] or source["physics_validation"]["status"] == "fail":
                raise ValueError("FDTD LUT failed structural or physics validation")
            if not source["available_effects"]["center_relative_field_at_selected_wavelength"]:
                raise ValueError("FDTD LUT needs center and noncenter field response rows")
            if spec["wavelength_nm"] not in source["summary"]["wavelengths_nm"]:
                raise ValueError("wavelength_nm must exactly match a LUT wavelength")
            # Also reject ambiguous optical-shift branches before submission.
            fdtd_sensor_field_response_map(
                lut, tuple(spec["size"]), wavelength_nm=spec["wavelength_nm"],
                normalize_to_center=True,
            )
        else:
            if not path.is_dir():
                raise FileNotFoundError(path)
            db = tcad_sensor_db_load(root=path)
            source = _tcad_inspection(db)
            if not source["validation"]["framework_ready"]:
                raise ValueError("TCAD collection framework is not valid")
            tcad_sensor_collection_efficiency(
                db, case=spec["case"], wavelength_nm=spec["wavelength_nm"],
                normalize_to_center=True,
            )
    except FileNotFoundError as error:
        raise ValueError(f"Selected physics LUT source is missing: {error}") from error
    try:
        _bounded_source_files(service, source)
    except FileNotFoundError as error:
        raise ValueError(f"Selected physics LUT source is missing: {error}") from error
    return {
        "name": spec["name"], "job_kind": JOB_KIND, "plugin_id": SCHEMA,
        "specification": spec, "source_identity": source,
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "environment": _current(service),
        "physical_validation": "not_established",
    }


def submit(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    from .registry import digest

    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    """Worker entry: repeat source checks, execute two captures, save both RAWs."""
    from .registry import IntegrityError, canonical

    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen physics LUT source or executable changed")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Physics LUT study cancelled or no longer running")
    spec = fresh["specification"]
    if spec["layer"] == "fdtd":
        report = compare_fdtd_field_raw(
            spec["source_path"], size=spec["size"], exposure_s=spec["exposure_s"],
            wavelength_nm=spec["wavelength_nm"], seed=spec["seed"],
            workspace=service.workspace,
        )
    else:
        report = compare_tcad_collection_raw(
            root=spec["source_path"], case=spec["case"],
            wavelength_nm=spec["wavelength_nm"], size=spec["size"],
            exposure_s=spec["exposure_s"], seed=spec["seed"],
            workspace=service.workspace,
        )
    if canonical(report["source"]) != canonical(fresh["source_identity"]):
        raise IntegrityError("Executed physics LUT differs from the submitted source")
    if canonical(report["upstream_assets"]) != canonical(fresh["upstream_assets"]):
        raise IntegrityError("Executed upstream assets differ from the submitted source")
    artifacts = []
    for name, key in (("baseline", "baseline"), ("with-lut", "with_lut")):
        raw = np.asarray(report[key]["raw"]["values"], dtype=np.int32)
        stream = io.BytesIO()
        np.save(stream, raw, allow_pickle=False)
        artifact = service.registry.store_blob(
            stream.getvalue(), f"physics-{name}-raw.npy", "application/octet-stream",
        )
        artifact.update(shape=list(raw.shape), dtype="int32", unit="DN", source_kind="simulated")
        report[key]["raw_artifact"] = artifact
        artifacts.append(artifact)
    summary = {
        "schema": SCHEMA, "specification": spec,
        "source_identity": fresh["source_identity"],
        "upstream_assets": fresh["upstream_assets"],
        "comparison": report,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
    }
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "physics-lut-result.json", "application/json",
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    """Read a typed job and verify retained JSON evidence when complete."""
    from .registry import IntegrityError, canonical

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a physics LUT study")
    result = (job.get("result") or {}).get("summary")
    if result is not None:
        if canonical(result["specification"]) != canonical(job["specification"]):
            raise IntegrityError("Saved physics LUT specification differs from its input")
        if canonical(result["source_identity"]) != canonical(job["source_identity"]):
            raise IntegrityError("Saved physics LUT source differs from its input")
        if canonical(result["upstream_assets"]) != canonical(job["upstream_assets"]):
            raise IntegrityError("Saved upstream assets differ from the frozen input")
        artifacts = (job.get("result") or {}).get("artifacts", [])
        matches = [item for item in artifacts if item["name"] == "physics-lut-result.json"]
        if job["status"] == "succeeded" and len(matches) != 1:
            raise IntegrityError("Completed physics LUT study lacks its result artifact")
        if matches:
            content = service.registry.blob_path(matches[0]["sha256"]).read_bytes()
            if content != canonical(result).encode():
                raise IntegrityError("Saved physics LUT result artifact differs from its job")
        if job["status"] == "succeeded":
            for key in ("baseline", "with_lut"):
                capture = result["comparison"][key]
                artifact = capture["raw_artifact"]
                raw_bytes = service.registry.blob_path(artifact["sha256"]).read_bytes()
                raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
                expected = np.asarray(capture["raw"]["values"], dtype=np.int32)
                if (
                    raw.dtype != np.dtype("int32") or raw.shape != expected.shape
                    or not np.array_equal(raw, expected)
                ):
                    raise IntegrityError("Saved physics LUT RAW artifact differs from its job")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {"id": job["id"], "name": job["name"], "layer": job["specification"]["layer"],
         "status": job["status"], "created_at": job["created_at"],
         "error": job.get("error")}
        for job in service.registry.list_runs() if job.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    """Install a typed API; root application owns authentication and worker."""
    from fastapi.responses import Response

    from .registry import canonical

    @app.get("/api/v1/physics-lut/template", tags=["physics_lut"])
    def template() -> dict[str, Any]:
        options: dict[str, Any] = {}
        for layer in ("fdtd", "tcad"):
            spec = default_spec(layer=layer)
            path = spec["source_path"]
            if path:
                try:
                    _bounded_source(service, path)
                    inspection = (
                        inspect_fdtd_lut(path) if layer == "fdtd"
                        else inspect_tcad_db(root=path)
                    )
                    _bounded_source_files(service, inspection)
                    options[layer] = {
                        "specification": spec,
                        "inspection": inspection,
                    }
                except (FileNotFoundError, ValueError) as error:
                    options[layer] = {"specification": spec, "error": str(error)}
            else:
                options[layer] = {
                    "specification": spec,
                    "error": "No default source is available; provide a workspace asset path",
                }
        return {"schema": SCHEMA, "options": options}

    @app.post("/api/v1/physics-lut", status_code=202, tags=["physics_lut"])
    def create(payload: dict) -> dict[str, Any]:
        return submit(service, payload)

    @app.get("/api/v1/physics-lut", tags=["physics_lut"])
    def listing() -> list[dict[str, Any]]:
        return list_records(service)

    @app.get("/api/v1/physics-lut/{identity}", tags=["physics_lut"])
    def detail(identity: str) -> dict[str, Any]:
        return get(service, identity)

    @app.post("/api/v1/physics-lut/{identity}/cancel", tags=["physics_lut"])
    def cancel(identity: str) -> dict[str, Any]:
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/physics-lut/{identity}/export", tags=["physics_lut"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="physics-lut-study.json"'},
        )

"""Bounded quality comparison using the existing pyisetcam catalog estimate.

This path intentionally does not consume pinned sensor-simulation runs or a
catalog lens prescription.  It compares newly constructed ``db_estimate_v1``
sensor models under one shared ideal optical setup and fixed scene controls.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from pyisetcam.assets import AssetStore
from pyisetcam.image_sensor_db import image_sensor_db_get
from pyisetcam.sensor_comparison import camerae2e_compare_sensors
from pyisetcam.sensor_estimate import image_sensor_db_model

from .offline_assets import offline_asset_identity, offline_asset_store

SCHEMA = "camerae2e_catalog_quality_comparison_v1"
JOB_KIND = "catalog_quality_comparison"
_SCENES = frozenset({"uniform", "macbeth_d65", "slanted_edge"})
LIMITATION = (
    "Catalog-derived db_estimate_v1 sensor responses are recomputed under shared "
    "ideal optics, fixed controls and a common physical crop. This does not run "
    "a saved sensor-simulation revision or a DB lens prescription. Readout/efficiency "
    "are shared assumptions; Macbeth CIE76 is an in-sample calibration residual. "
    "Results are model sensitivity, not measured product ranking or physical validation."
)


def _bounded_real(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    return number


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}].")
    return value


def _readout_controls(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(
            "readout_controls must be a mapping of gain, bit_depth and black_level_dn."
        )
    controls = dict(value or {})
    if set(controls) - {"gain", "bit_depth", "black_level_dn"}:
        raise ValueError("readout_controls supports gain, bit_depth and black_level_dn only.")
    gain = _bounded_real(controls.get("gain", 1.0), "gain", 0.25, 8.0)
    bit_depth = _bounded_int(controls.get("bit_depth", 12), "bit_depth", 8, 16)
    black_level = _bounded_real(
        controls.get("black_level_dn", 64.0), "black_level_dn", 0.0, float(2**bit_depth - 2)
    )
    return {"gain": gain, "bit_depth": bit_depth, "black_level_dn": black_level}


def _available_metric(value: Any, unit: str, *, reason: str) -> dict[str, Any]:
    return (
        {"available": True, "value": float(value), "unit": unit}
        if value is not None and math.isfinite(float(value))
        else {"available": False, "reason": reason, "unit": unit}
    )


def _case_metrics(case: Mapping[str, Any]) -> dict[str, Any]:
    source = case["metrics"]
    eligible = bool(source["snr_ranking_eligible"])
    output_snr = _available_metric(
        source["common_output"]["snr_db"],
        "dB",
        reason="No positive temporal signal-to-noise ratio could be estimated.",
    )
    output_snr["ranking_eligible"] = eligible
    if not eligible:
        output_snr["interpretation"] = "Clipped capture: diagnostic SNR only."
    result: dict[str, Any] = {
        "native_raw_snr_db": _available_metric(
            source["native_raw"]["snr_db"],
            "dB",
            reason="No positive RAW temporal signal-to-noise ratio could be estimated.",
        ),
        "common_output_snr_db": output_snr,
        "mean_photoelectrons": source["native_raw"]["mean_photoelectrons"],
        "saturation_fractions": dict(source["saturation"]),
    }
    if "edge" in source:
        edge = source["edge"]
        result["edge_mtf50"] = (
            {
                "available": True,
                "value": edge["mtf50_cycles_per_output_pixel"],
                "unit": "cycles/output_pixel",
                "method": edge["method"],
            }
            if edge["available"]
            else {"available": False, "reason": edge["reason"]}
        )
    if "color" in source:
        color = source["color"]
        result["macbeth_cie76_mean"] = (
            {
                "available": True,
                "value": color["mean_delta_e"],
                "unit": "CIE76_delta_e",
                "evaluation_contract": color["evaluation_contract"],
                "independent_color_accuracy": False,
            }
            if color["available"]
            else {"available": False, "reason": color["reason"]}
        )
    return result


def compare_catalog_sensor_quality(
    sensor_ids: Sequence[str],
    *,
    root: str | Path | None = None,
    workspace: str | Path | None = None,
    output_dir: str | Path | None = None,
    scenes: Sequence[str] = ("uniform", "slanted_edge"),
    luminance_cd_m2: float = 10.0,
    exposure_s: float = 0.004,
    frames: int = 4,
    output_size: tuple[int, int] = (32, 32),
    f_number: float = 4.0,
    focal_length_m: float = 0.004,
    readout_controls: Mapping[str, Any] | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Compare two or three catalog sensor estimates on one matched scene set.

    Scenes, temporal samples and output crop are bounded for a workbench job.
    The full pyisetcam JSON/NPZ/HTML evidence is saved when ``output_dir`` is
    supplied; it must be new or empty so an older comparison is not overwritten.
    """
    if isinstance(sensor_ids, (str, bytes)) or not isinstance(sensor_ids, Sequence):
        raise ValueError("sensor_ids must contain two or three catalog IDs/report codes.")
    if not 2 <= len(sensor_ids) <= 3 or any(
        not isinstance(item, str) or not item.strip() for item in sensor_ids
    ):
        raise ValueError("sensor_ids must contain two or three catalog IDs/report codes.")
    if isinstance(scenes, (str, bytes)) or not isinstance(scenes, Sequence):
        raise ValueError("scenes must be a nonempty sequence of supported scene names.")
    if not scenes or len(scenes) > 3 or len(set(scenes)) != len(scenes) or set(scenes) - _SCENES:
        raise ValueError(f"scenes must be distinct values from {sorted(_SCENES)}.")
    if len(output_size) != 2:
        raise ValueError("output_size must contain two dimensions from 16 to 64.")
    rows = _bounded_int(output_size[0], "output rows", 16, 64)
    cols = _bounded_int(output_size[1], "output columns", 16, 64)
    frames = _bounded_int(frames, "frames", 2, 8)
    seed = _bounded_int(seed, "seed", 0, 2**32 - 1)
    luminance = _bounded_real(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    exposure = _bounded_real(exposure_s, "exposure_s", 1e-6, 1.0)
    f_number = _bounded_real(f_number, "f_number", 1.0, 32.0)
    focal_length = _bounded_real(focal_length_m, "focal_length_m", 1e-4, 0.1)
    controls = _readout_controls(readout_controls)

    # Resolve and validate every requested catalog model before any simulation.
    resolved_ids: list[str] = []
    for identifier in sensor_ids:
        record = image_sensor_db_get(identifier, root=root)
        if record["sensor_id"] in resolved_ids:
            raise ValueError("sensor_ids resolve to duplicate catalog records.")
        image_sensor_db_model(identifier, root=root)
        resolved_ids.append(record["sensor_id"])

    destination = None if output_dir is None else Path(output_dir).expanduser().resolve()
    if destination is not None and destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Quality comparison output_dir is not empty: {destination}")
    assets = offline_asset_store(workspace=workspace, source=root)
    asset_identity = offline_asset_identity(workspace=workspace, source=root)
    with AssetStore.bound_default(assets):
        report = camerae2e_compare_sensors(
            resolved_ids,
            root=root,
            output_dir=destination,
            nframes=frames,
            luminances=(luminance,),
            exposures=(exposure,),
            output_size=(rows, cols),
            scene_types=tuple(scenes),
            assumption_sweep=False,
            seed=seed,
            fnumber=f_number,
            focal_length_m=focal_length,
            readout=controls,
            asset_store=assets,
        )
    if report["skipped_sensors"] or len(report["selected_sensors"]) != len(resolved_ids):
        raise RuntimeError(
            f"Catalog quality comparison skipped sensors: {report['skipped_sensors']}"
        )
    artifact_hashes = {
        kind: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for kind, path in report["artifacts"].items()
    }
    return {
        "schema": SCHEMA,
        "status": "completed",
        "comparison_contract": report["comparison_contract"],
        "estimate_only": True,
        "source_boundary": {
            "sensor_model": "catalog_derived_db_estimate_v1_recomputed_for_this_comparison",
            "saved_sensor_simulation_run_used": False,
            "lens_model": "shared_ideal_f_number_and_focal_length_not_catalog_lens_revision",
            "physical_validation": "not_established",
        },
        "upstream_assets": asset_identity,
        "selected_sensors": report["selected_sensors"],
        "controls": report["controls"],
        "geometry": report.get("geometry"),
        "readout_assumptions": report["assumptions"],
        "sensitivity_sweep_run": False,
        "method": report["method"],
        "limitations": report["limitations"],
        "cases": [
            {
                "sensor_id": case["sensor_id"],
                "report_code": case["code"],
                "scene": case["scene"],
                "luminance_cd_m2": case["luminance_cd_m2"],
                "exposure_s": case["exposure_s"],
                "assumption": case["assumption"],
                "array_prefix": case.get("array_prefix"),
                "readout": case["readout"],
                "metrics": _case_metrics(case),
            }
            for case in report["cases"]
        ],
        "sample_count": report["sample_count"],
        "optical_solve_count": report["optical_solve_count"],
        "artifacts": report["artifacts"],
        "artifact_sha256": artifact_hashes,
    }


def default_spec() -> dict[str, Any]:
    """Return the documented, finite workbench request template."""
    return {
        "schema": SCHEMA,
        "name": "카탈로그 센서 화질 비교",
        "sensor_ids": [],
        "scenes": ["uniform", "slanted_edge"],
        "luminance_cd_m2": 10.0,
        "exposure_s": 0.004,
        "frames": 4,
        "output_size": [32, 32],
        "f_number": 4.0,
        "focal_length_m": 0.004,
        "readout_controls": {"gain": 1.0, "bit_depth": 12, "black_level_dn": 64.0},
        "seed": 0,
        "acknowledge_simulation_limits": False,
    }


def specification(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Check a submitted comparison before it reaches the background worker."""
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the catalog quality comparison fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit catalog-estimate simulation-limit acknowledgement required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Comparison name requires 1..160 characters.")
    identifiers = payload["sensor_ids"]
    if (
        not isinstance(identifiers, list)
        or not 2 <= len(identifiers) <= 3
        or any(
            not isinstance(item, str) or not 1 <= len(item.strip()) <= 128 for item in identifiers
        )
    ):
        raise ValueError("Select two or three catalog sensor IDs/report codes.")
    scenes = payload["scenes"]
    if (
        not isinstance(scenes, list)
        or not 1 <= len(scenes) <= 3
        or any(not isinstance(scene, str) for scene in scenes)
        or len(set(scenes)) != len(scenes)
        or set(scenes) - _SCENES
    ):
        raise ValueError(f"scenes must be distinct values from {sorted(_SCENES)}.")
    dimensions = payload["output_size"]
    if not isinstance(dimensions, list) or len(dimensions) != 2:
        raise ValueError("output_size must contain two dimensions from 16 to 64.")
    rows = _bounded_int(dimensions[0], "output rows", 16, 64)
    cols = _bounded_int(dimensions[1], "output columns", 16, 64)
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["sensor_ids"] = list(identifiers)
    result["scenes"] = list(scenes)
    result["output_size"] = [rows, cols]
    result["frames"] = _bounded_int(payload["frames"], "frames", 2, 8)
    result["seed"] = _bounded_int(payload["seed"], "seed", 0, 2**32 - 1)
    result["luminance_cd_m2"] = _bounded_real(
        payload["luminance_cd_m2"], "luminance_cd_m2", 0.01, 10000.0
    )
    result["exposure_s"] = _bounded_real(payload["exposure_s"], "exposure_s", 1e-6, 1.0)
    result["f_number"] = _bounded_real(payload["f_number"], "f_number", 1.0, 32.0)
    result["focal_length_m"] = _bounded_real(payload["focal_length_m"], "focal_length_m", 1e-4, 0.1)
    result["readout_controls"] = _readout_controls(payload["readout_controls"])
    return result


def _catalog_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db/fdtd_tcad/sensor_db"


def prepare(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze source-model values and executable identity with the request."""
    from .imaging import _current
    from .registry import digest

    spec = specification(payload)
    environment = _current(service)
    root = _catalog_root(service)
    asset_identity = offline_asset_identity(workspace=service.workspace, source=root)
    sources = []
    for identifier in spec["sensor_ids"]:
        try:
            record = image_sensor_db_get(identifier, root=root)
            model = image_sensor_db_model(identifier, root=root)
        except (KeyError, FileNotFoundError) as exc:
            raise ValueError(f"Catalog sensor source unavailable for {identifier}: {exc}") from exc
        if record["sensor_id"] in [item["sensor_id"] for item in sources]:
            raise ValueError("sensor_ids resolve to duplicate catalog records.")
        sources.append(
            {
                "sensor_id": record["sensor_id"],
                "report_code": record["code"],
                "catalog_record_sha256": digest(record),
                "effective_model_sha256": digest(model),
                "model_provenance": model["provenance"],
            }
        )
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identities": sources,
        "upstream_assets": asset_identity,
        "environment": environment,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    from .registry import digest

    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    """Run in the worker, then retain full metric, RAW and image evidence blobs."""
    from .registry import IntegrityError, canonical

    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen catalog models, controls or executable changed.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Quality comparison is not running or was cancelled.")
    spec = fresh["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-quality-") as temporary:
        comparison = compare_catalog_sensor_quality(
            spec["sensor_ids"],
            root=_catalog_root(service),
            workspace=service.workspace,
            output_dir=temporary,
            scenes=spec["scenes"],
            luminance_cd_m2=spec["luminance_cd_m2"],
            exposure_s=spec["exposure_s"],
            frames=spec["frames"],
            output_size=tuple(spec["output_size"]),
            f_number=spec["f_number"],
            focal_length_m=spec["focal_length_m"],
            readout_controls=spec["readout_controls"],
            seed=spec["seed"],
        )
        if comparison["upstream_assets"] != fresh["upstream_assets"]:
            raise IntegrityError("Pinned upstream assets changed during comparison.")
        after = prepare(service, spec)
        if canonical(after) != canonical(fresh):
            raise IntegrityError("Catalog model or executable changed during comparison.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Quality comparison was cancelled during execution.")
        source = json.loads(Path(comparison["artifacts"]["json"]).read_text(encoding="utf-8"))
        artifacts = []
        for kind, name, media in (
            ("npz", "catalog-quality-arrays.npz", "application/octet-stream"),
            ("html", "catalog-quality-report.html", "text/html"),
        ):
            data = Path(comparison["artifacts"][kind]).read_bytes()
            artifact = service.registry.store_blob(data, name, media)
            if artifact["sha256"] != comparison["artifact_sha256"][kind]:
                raise IntegrityError(f"Saved {kind} artifact differs from the computed report.")
            artifacts.append(artifact)
        source["artifacts"] = {
            "npz_sha256": artifacts[0]["sha256"],
            "html_sha256": artifacts[1]["sha256"],
            "storage": "platform_registry_blobs",
        }
        report_artifact = service.registry.store_blob(
            canonical(source).encode(), "catalog-quality-source-report.json", "application/json"
        )
        artifacts.append(report_artifact)
    summary = {
        **comparison,
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "upstream_assets": fresh["upstream_assets"],
        "artifacts": {item["name"]: item for item in artifacts},
        "artifact_sha256": {item["name"]: item["sha256"] for item in artifacts},
        "limitation": LIMITATION,
    }
    summary_artifact = service.registry.store_blob(
        canonical(summary).encode(), "catalog-quality-result.json", "application/json"
    )
    artifacts.append(summary_artifact)
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    """Read a job and verify completed result/RAW artifact integrity."""
    from .registry import IntegrityError, canonical

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a catalog quality comparison.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (
            canonical(summary["specification"]) != canonical(job["specification"])
            or canonical(summary["source_identities"]) != canonical(job["source_identities"])
            or canonical(summary["upstream_assets"]) != canonical(job["upstream_assets"])
        ):
            raise IntegrityError("Saved quality comparison differs from its frozen request.")
        if job["status"] == "succeeded":
            descriptors = {item["name"]: item for item in job["artifacts"]}
            if any(
                name not in descriptors or descriptors[name]["sha256"] != item["sha256"]
                for name, item in summary["artifacts"].items()
            ):
                raise IntegrityError("Saved quality comparison evidence links differ.")
            for item in job["artifacts"]:
                content = service.registry.blob_path(item["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise IntegrityError("Quality comparison artifact bytes differ.")
            result_artifacts = [
                item for item in job["artifacts"] if item["name"] == "catalog-quality-result.json"
            ]
            if len(result_artifacts) != 1 or (
                service.registry.blob_path(result_artifacts[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved quality comparison result differs from its artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "status": item["status"],
            "created_at": item["created_at"],
            "error": item.get("error"),
        }
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    from .registry import canonical

    @app.get("/api/v1/catalog-quality-comparisons/template", tags=["catalog_quality"])
    def template(q: str = ""):
        from pyisetcam.sensor_estimate import image_sensor_db_model_inventory

        try:
            inventory = image_sensor_db_model_inventory(root=_catalog_root(service))
        except FileNotFoundError as exc:
            raise ValueError(f"Catalog sensor source unavailable: {exc}") from exc
        choices = [
            row
            for row in inventory["records"]
            if row["available"]
            and (
                not q
                or q.lower()
                in " ".join(
                    str(row[key]) for key in ("sensor_id", "code", "device_name", "manufacturer")
                ).lower()
            )
        ][:100]
        return {
            "specification": default_spec(),
            "catalog": choices,
            "inventory_count": inventory["record_count"],
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/catalog-quality-comparisons", status_code=202, tags=["catalog_quality"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/catalog-quality-comparisons", tags=["catalog_quality"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/catalog-quality-comparisons/{identity}", tags=["catalog_quality"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/catalog-quality-comparisons/{identity}/cancel", tags=["catalog_quality"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/catalog-quality-comparisons/{identity}/export", tags=["catalog_quality"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="catalog-quality.json"'},
        )

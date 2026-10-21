"""Saved, bounded comparisons of existing CameraE2E lens and sensor revisions.

The two evidence tracks deliberately remain separate. The available patent
PSFs do not have validated radiometry or sensor sampling, so pairing those
lenses with saved sensor responses would invent an optical-to-RAW model.
"""

from __future__ import annotations

import io
import math
from copy import deepcopy

import numpy as np

from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_db_comparison_v1"
JOB_KIND = "db_comparison"
LIMITATION = (
    "Two hash-pinned geometric lens previews and two saved sensor-model RAW "
    "captures are compared in separate tracks. The lens PSFs lack validated "
    "radiometry and sensor sampling; no lens-by-sensor RAW cross product is "
    "computed. The uniform scene is a shared numerical input, not matched "
    "field of view. Results are model sensitivities, not product rankings, "
    "measured accuracy, HDR/LFM performance, or physical validation."
)


def _db_root(service):
    return service.workspace / "camerae2e_db"


def default_spec():
    return {
        "schema": SCHEMA,
        "name": "저장 DB revision 비교",
        "lens_revision_ids": [],
        "sensor_revisions": [],
        "wavelength_nm": 550.0,
        "scene": {"type": "uniform ee", "args": [[8, 8]]},
        "sensor_size": [8, 8],
        "exposure_s": 0.004,
        "noise_flag": 0,
        "seed": 7,
        "readout_overrides": {},
        "acknowledge_simulation_limits": False,
    }


def specification(payload):
    """Validate a small fixed comparison without searching for implicit assets."""
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the DB comparison fields")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit DB comparison simulation-limit acknowledgement required")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Comparison name requires 1..160 characters")
    lenses = payload["lens_revision_ids"]
    if (
        not isinstance(lenses, list)
        or len(lenses) != 2
        or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in lenses)
        or len(set(lenses)) != 2
    ):
        raise ValueError("Select exactly two distinct lens revision IDs")
    sensors = payload["sensor_revisions"]
    if (
        not isinstance(sensors, list)
        or len(sensors) != 2
        or any(
            not isinstance(value, dict)
            or set(value) != {"identifier", "run_id"}
            or not isinstance(value["identifier"], str)
            or not 1 <= len(value["identifier"]) <= 128
            or not isinstance(value["run_id"], str)
            or not 1 <= len(value["run_id"]) <= 128
            for value in sensors
        )
        or len({value["run_id"] for value in sensors}) != 2
    ):
        raise ValueError("Select exactly two distinct saved sensor run IDs")
    wave = payload["wavelength_nm"]
    if type(wave) not in (int, float) or not math.isfinite(wave):
        raise ValueError("Lens wavelength must be finite nm")
    size = payload["sensor_size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(value) is not int or not 8 <= value <= 32 for value in size)
    ):
        raise ValueError("Sensor size must be two integer dimensions from 8 to 32")
    scene = payload["scene"]
    if (
        not isinstance(scene, dict)
        or set(scene) != {"type", "args"}
        or scene["type"] != "uniform ee"
        or scene["args"] != [size]
    ):
        raise ValueError("DB comparison v1 requires a uniform ee scene matching sensor size")
    exposure = payload["exposure_s"]
    if type(exposure) not in (int, float) or not math.isfinite(exposure) or not 0 < exposure <= 1:
        raise ValueError("Exposure must be finite and in (0, 1] seconds")
    if type(payload["noise_flag"]) is not int or payload["noise_flag"] not in (0, 2):
        raise ValueError("Noise flag must be 0 or 2")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("Seed must be a uint32 integer")
    overrides = payload["readout_overrides"]
    from .sensor_db_bridge import _overrides

    _overrides(overrides)
    result = deepcopy(payload)
    result["name"] = payload["name"].strip()
    # Preserve the user's first/second ordering: reported deltas are second
    # minus first, so a sorted cache identity would silently flip the sign.
    result["lens_revision_ids"] = list(lenses)
    result["sensor_revisions"] = deepcopy(sensors)
    result["wavelength_nm"] = float(wave)
    result["exposure_s"] = float(exposure)
    return result


def prepare(service, payload):
    """Pin both DB revisions, source files and executable source identity."""
    from .imaging import _current
    from .lens_catalog_bridge import compare_geometric_lenses
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import inspect_sensor_revision

    environment = _current(service)
    spec = specification(payload)
    root = _db_root(service)
    lenses = compare_geometric_lenses(
        spec["lens_revision_ids"],
        catalog_root=root,
        wavelength_nm=spec["wavelength_nm"],
    )["lenses"]
    sensors = [
        inspect_sensor_revision(item["identifier"], root=root, run_id=item["run_id"])
        for item in spec["sensor_revisions"]
    ]
    for item in sensors:
        if not item["executable"]:
            raise ValueError(f"Sensor revision cannot execute: {item['reason']}")
    effective_readouts = [
        {key: field["value"] for key, field in item["readout"]["saved_values"].items()}
        | spec["readout_overrides"]
        for item in sensors
    ]
    mismatched = sorted(
        key
        for key in effective_readouts[0]
        if effective_readouts[0][key] != effective_readouts[1][key]
    )
    if mismatched:
        raise ValueError(
            f"Effective readout differs for {mismatched}; "
            "declare shared overrides before submitting"
        )
    if (
        spec["noise_flag"] == 2
        and sensors[0]["readout"]["fixed_pattern"] != sensors[1]["readout"]["fixed_pattern"]
    ):
        raise ValueError("Sampled-noise comparison requires matching fixed-pattern profiles")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identities": {
            "lenses": [item["identity"] for item in lenses],
            "sensors": [item["revision"] for item in sensors],
            "upstream_assets": offline_asset_identity(workspace=service.workspace),
        },
        "environment": environment,
        "limitation": LIMITATION,
    }


def submit(service, payload):
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service, job):
    from .lens_catalog_bridge import compare_geometric_lenses
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import compare_sensor_revisions

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("DB comparison was cancelled or is not running")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen DB comparison source, specification or executable changed")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("DB comparison was cancelled or is not running")
    spec = fresh["specification"]
    root = _db_root(service)
    lens_result = compare_geometric_lenses(
        spec["lens_revision_ids"], catalog_root=root, wavelength_nm=spec["wavelength_nm"]
    )
    sensor_result = compare_sensor_revisions(
        spec["sensor_revisions"],
        root=root,
        workspace=service.workspace,
        scene=spec["scene"],
        exposure_s=spec["exposure_s"],
        sensor_size=spec["sensor_size"],
        readout_overrides=spec["readout_overrides"],
        noise_flag=spec["noise_flag"],
        seed=spec["seed"],
    )
    if [item["identity"] for item in lens_result["lenses"]] != fresh["source_identities"][
        "lenses"
    ] or [item["revision"] for item in sensor_result["captures"]] != fresh["source_identities"][
        "sensors"
    ]:
        raise IntegrityError("Executed DB revision differs from the frozen selection")
    if offline_asset_identity(workspace=service.workspace) != fresh["source_identities"][
        "upstream_assets"
    ]:
        raise IntegrityError("Pinned upstream assets changed during DB comparison")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("DB comparison was cancelled during execution")
    artifacts = []
    for index, capture in enumerate(sensor_result["captures"]):
        raw = np.asarray(capture["raw"]["values"], dtype=np.int32)
        stream = io.BytesIO()
        np.save(stream, raw, allow_pickle=False)
        artifact = service.registry.store_blob(
            stream.getvalue(),
            f"sensor-{index + 1}-original-raw.npy",
            "application/octet-stream",
        )
        artifact.update(dtype="int32", shape=list(raw.shape), unit="DN", source_kind="simulated")
        capture["raw_artifact"] = artifact
        artifacts.append(artifact)
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "lens_geometry": lens_result,
        "sensor_raw": sensor_result,
        "lens_sensor_cross_product": {
            "status": "not_executed",
            "reason": (
                "Lens PSF radiometry and sensor sampling are unvalidated; "
                "a combined RAW would invent physics."
            ),
        },
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifacts.append(
        service.registry.store_blob(
            canonical(summary).encode(), "db-comparison-result.json", "application/json"
        )
    )
    return {"summary": summary, "artifacts": artifacts}


def get(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a DB comparison")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if canonical(summary["specification"]) != canonical(job["specification"]) or canonical(
            summary["source_identities"]
        ) != canonical(job["source_identities"]):
            raise IntegrityError("Saved DB comparison differs from its frozen request")
        if job["status"] == "succeeded":
            result_artifacts = [
                item for item in job["artifacts"] if item["name"] == "db-comparison-result.json"
            ]
            if len(result_artifacts) != 1:
                raise IntegrityError("Completed DB comparison lacks its saved result")
            if (
                service.registry.blob_path(result_artifacts[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved DB comparison result differs from the result artifact")
            for capture in summary["sensor_raw"]["captures"]:
                descriptor = capture["raw_artifact"]
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                original = np.load(io.BytesIO(content), allow_pickle=False)
                if original.dtype != np.dtype("int32") or not np.array_equal(
                    original, capture["raw"]["values"]
                ):
                    raise IntegrityError("Saved DB comparison RAW differs from the numeric result")
    return job


def list_records(service):
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


def install_routes(app, service):
    from fastapi.responses import Response

    @app.get("/api/v1/db-comparisons/template", tags=["db_comparisons"])
    def template(q: str = ""):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        root = _db_root(service)
        return {
            "specification": default_spec(),
            "lens_catalog": lens_revision_catalog(catalog_root=root, query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=root, query=q or None),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/db-comparisons", status_code=202, tags=["db_comparisons"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/db-comparisons", tags=["db_comparisons"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/db-comparisons/{identity}", tags=["db_comparisons"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/db-comparisons/{identity}/cancel", tags=["db_comparisons"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/db-comparisons/{identity}/export", tags=["db_comparisons"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="db-comparison.json"'},
        )

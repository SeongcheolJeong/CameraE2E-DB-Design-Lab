"""Durable opt-in job for one Lens DB × frozen Sensor DB geometric RAW preview."""

from __future__ import annotations

import io
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .lens_sensor_raw_bridge import BOUNDED_LIMITATION, LIMITATION, _number
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v1"
LOCAL_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v2"
BOUNDED_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v3"
COMMON_SCENE_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v4"
DENSE_COMMON_SCENE_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v5"
RADIANCE_SCENE_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v6"
LARGE_COMMON_SCENE_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v7"
DIRECT_REHISTOGRAM_SCHEMA = "camerae2e_lens_sensor_raw_preview_study_v8"
JOB_KIND = "lens_sensor_raw"


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 저장 센서 RAW 근사 미리보기",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "scene_kind": "slanted_edge",
        "sensor_size": [16, 16],
        "luminance_cd_m2": 1000.0,
        "exposure_s": 0.004,
        "noise_flag": 0,
        "seed": 7,
        "accept_approximate_lens_radiometry": False,
    }


def default_local_spec() -> dict[str, Any]:
    return {
        **default_spec(),
        "schema": LOCAL_SCHEMA,
        "name": "렌즈 시야 PSF × 저장 센서 국소 RAW 근사",
        "field_psf_index": 0,
        "field_azimuth_deg": 0.0,
    }


def default_bounded_spec() -> dict[str, Any]:
    return {
        **default_spec(),
        "schema": BOUNDED_SCHEMA,
        "name": "렌즈 × 저장 센서 RAW 중앙 strict PSF 분할 실행",
    }


def default_common_scene_spec() -> dict[str, Any]:
    return {
        **default_bounded_spec(),
        "schema": COMMON_SCENE_SCHEMA,
        "name": "렌즈 × 저장 센서 RAW 공통 입력 각도 장면",
        "scene_horizontal_fov_deg": 0.12,
    }


def default_dense_common_scene_spec() -> dict[str, Any]:
    return {
        **default_common_scene_spec(),
        "schema": DENSE_COMMON_SCENE_SCHEMA,
        "name": "렌즈 × 저장 센서 RAW 고밀도 공통 입력 각도 장면",
        "scene_samples_row_col": [128, 128],
    }


def default_radiance_scene_spec() -> dict[str, Any]:
    return {
        "schema": RADIANCE_SCENE_SCHEMA,
        "name": "렌즈 × 저장 센서 사용자 분광 장면 RAW",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "radiance_scene_id": "",
        "sensor_size": [16, 16],
        "exposure_s": 0.004,
        "noise_flag": 0,
        "seed": 7,
        "accept_approximate_lens_radiometry": False,
    }


def default_large_common_scene_spec() -> dict[str, Any]:
    return {
        **default_dense_common_scene_spec(),
        "schema": LARGE_COMMON_SCENE_SCHEMA,
        "name": "렌즈 × 센서 대형 입력 장면 자원 한도 사전 검사",
        "scene_samples_row_col": [1024, 1024],
        "resource_policy": "monitored_child_required_v1",
    }


def default_direct_rehistogram_spec() -> dict[str, Any]:
    return {
        **default_dense_common_scene_spec(),
        "schema": DIRECT_REHISTOGRAM_SCHEMA,
        "name": "렌즈 기하 PSF 직접 재히스토그램 × 센서 RAW 수치 진단",
        "scene_samples_row_col": [128, 128],
        "geometric_psf_sampling": "direct_same_ray_rehistogram",
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    local = isinstance(payload, dict) and payload.get("schema") == LOCAL_SCHEMA
    common_scene = isinstance(payload, dict) and payload.get("schema") == COMMON_SCENE_SCHEMA
    dense_scene = isinstance(payload, dict) and payload.get("schema") == DENSE_COMMON_SCENE_SCHEMA
    large_scene = isinstance(payload, dict) and payload.get("schema") == LARGE_COMMON_SCENE_SCHEMA
    direct_rehistogram = (
        isinstance(payload, dict) and payload.get("schema") == DIRECT_REHISTOGRAM_SCHEMA
    )
    radiance_scene = isinstance(payload, dict) and payload.get("schema") == RADIANCE_SCENE_SCHEMA
    expected = (
        default_local_spec() if local else
        default_radiance_scene_spec() if radiance_scene else
        default_direct_rehistogram_spec() if direct_rehistogram else
        default_large_common_scene_spec() if large_scene else
        default_dense_common_scene_spec() if dense_scene else
        default_common_scene_spec() if common_scene else default_spec()
    )
    if not isinstance(payload, dict) or set(payload) != set(expected):
        raise ValueError("Declare exactly the lens × sensor RAW preview fields.")
    if payload["schema"] not in {
        SCHEMA, LOCAL_SCHEMA, BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA,
        DENSE_COMMON_SCENE_SCHEMA, RADIANCE_SCENE_SCHEMA, LARGE_COMMON_SCENE_SCHEMA,
        DIRECT_REHISTOGRAM_SCHEMA,
    }:
        raise ValueError("Unsupported lens × sensor RAW preview schema.")
    if payload["accept_approximate_lens_radiometry"] is not True:
        raise ValueError("Explicit approximate lens-radiometry acceptance is required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Preview name requires 1..160 characters.")
    lens_id = payload["lens_revision_id"]
    if not isinstance(lens_id, str) or not 1 <= len(lens_id.strip()) <= 128:
        raise ValueError("Choose one explicit Lens DB revision ID.")
    sensor = payload["sensor_revision"]
    if (
        not isinstance(sensor, dict)
        or set(sensor) != {"identifier", "run_id"}
        or any(
            not isinstance(sensor[key], str) or not 1 <= len(sensor[key].strip()) <= 128
            for key in ("identifier", "run_id")
        )
    ):
        raise ValueError("Choose one explicit saved sensor identifier and run ID.")
    if radiance_scene:
        selected_scene = payload["radiance_scene_id"]
        if not isinstance(selected_scene, str) or not 1 <= len(selected_scene.strip()) <= 128:
            raise ValueError("Choose one immutable object-radiance scene ID.")
    elif payload["scene_kind"] not in {"uniform", "slanted_edge"}:
        raise ValueError("scene_kind must be uniform or slanted_edge.")
    size = payload["sensor_size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(item) is not int or not 8 <= item <= 32 for item in size)
    ):
        raise ValueError("sensor_size requires two integer dimensions from 8 to 32.")
    if type(payload["noise_flag"]) is not int or payload["noise_flag"] not in {0, 2}:
        raise ValueError("noise_flag must be 0 or 2.")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if local:
        if type(payload["field_psf_index"]) is not int or not 0 <= payload["field_psf_index"] <= 64:
            raise ValueError("field_psf_index must be a bounded stored field index.")
        payload_azimuth = _number(payload["field_azimuth_deg"], "field_azimuth_deg", 0.0, 360.0)
        if payload_azimuth == 360.0:
            raise ValueError("field_azimuth_deg must be below 360 degrees.")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["lens_revision_id"] = lens_id.strip()
    result["sensor_revision"] = {key: value.strip() for key, value in sensor.items()}
    if radiance_scene:
        result["radiance_scene_id"] = selected_scene.strip()
    else:
        result["luminance_cd_m2"] = _number(
            payload["luminance_cd_m2"], "luminance_cd_m2", 0.01, 10000.0
        )
    result["exposure_s"] = _number(payload["exposure_s"], "exposure_s", 1e-6, 1.0)
    if local:
        result["field_azimuth_deg"] = payload_azimuth
    if large_scene and payload["resource_policy"] != "monitored_child_required_v1":
        raise ValueError("Large Scene requires the monitored-child resource policy.")
    if direct_rehistogram and payload["geometric_psf_sampling"] != "direct_same_ray_rehistogram":
        raise ValueError("v8 requires the explicit direct same-ray geometric PSF method.")
    if common_scene or dense_scene or large_scene or direct_rehistogram:
        result["scene_horizontal_fov_deg"] = _number(
            payload["scene_horizontal_fov_deg"], "scene_horizontal_fov_deg", 0.001, 30.0
        )
    if dense_scene or large_scene or direct_rehistogram:
        from .lens_sensor_raw_bridge import _scene_samples

        result["scene_samples_row_col"] = list(
            _scene_samples(
                payload["scene_samples_row_col"], size,
                max_samples=1024 if large_scene else 256,
            )
        )
        if large_scene and max(result["scene_samples_row_col"]) <= 256:
            raise ValueError("Use v5 for scenes no larger than 256 samples.")
    return result


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_config

    from .imaging import _current
    from .lens_catalog_bridge import geometric_lens_preview
    from .lens_sensor_large_scene_preflight import (
        preflight_large_scene,
        require_large_scene_resources,
    )
    from .lens_sensor_radiance_scene import load_radiance_scene, source_identity
    from .lens_sensor_raw_bridge import (
        DIRECT_PSF_REHISTOGRAM_LIMITATION,
        LOCAL_FIELD_LIMITATION,
        RADIANCE_SCENE_LIMITATION,
        bounded_psf_resource_preflight,
        central_psf_resource_preflight,
        direct_rehistogram_resource_preflight,
        require_bounded_psf_budget,
        require_central_psf_budget,
        require_direct_rehistogram_budget,
    )
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import inspect_sensor_revision

    spec = specification(payload)
    lens = geometric_lens_preview(spec["lens_revision_id"], catalog_root=_db_root(service))
    if spec["schema"] == LOCAL_SCHEMA and spec["field_psf_index"] >= len(
        lens["model"]["field_samples_mm"]
    ):
        raise ValueError("field_psf_index exceeds the selected lens's stored PSF fields.")
    selected = spec["sensor_revision"]
    sensor = inspect_sensor_revision(
        selected["identifier"], root=_db_root(service), run_id=selected["run_id"]
    )
    if not sensor["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {sensor['reason']}")
    if sensor["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError(
            "Lens × sensor RAW preview supports db_estimate_v1 only; "
            "physical/FDTD responses need a matched lens pupil and CRA contract."
        )
    radiance_record = None
    if spec["schema"] == RADIANCE_SCENE_SCHEMA:
        radiance_record, _ = load_radiance_scene(service, spec["radiance_scene_id"])
        distance = lens["model"]["object_distance_m"]
        declared = radiance_record["object_distance_m"]
        if not (
            (distance == "infinity" and declared == "infinity")
            or (
                type(distance) in (int, float) and type(declared) in (int, float)
                and math.isclose(distance, declared, rel_tol=1e-9, abs_tol=1e-9))
        ):
            raise ValueError(
                "Object-radiance scene conjugate differs from saved Lens DB PSF. "
                "Reproject the scene or select a matching lens."
            )
        scene_diagonal_fov_deg = math.degrees(2.0 * math.atan(math.hypot(
            math.tan(math.radians(radiance_record["horizontal_fov_deg"]) / 2.0),
            math.tan(math.radians(radiance_record["vertical_fov_deg"]) / 2.0),
        )))
        if scene_diagonal_fov_deg > lens["model"]["max_fov_deg"] + 1e-9:
            raise ValueError(
                "Object-radiance scene diagonal FOV exceeds the saved Lens DB PSF field. "
                "Use a source crop within the recorded lens field or select a matching lens."
            )
    resource_preflight = None
    if spec["schema"] in {
        SCHEMA, BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
        RADIANCE_SCENE_SCHEMA, DIRECT_REHISTOGRAM_SCHEMA,
    }:
        preflight = (
            direct_rehistogram_resource_preflight
            if spec["schema"] == DIRECT_REHISTOGRAM_SCHEMA else
            bounded_psf_resource_preflight
            if spec["schema"] in {
                BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
                RADIANCE_SCENE_SCHEMA,
            }
            else central_psf_resource_preflight
        )
        resource_preflight = preflight(
            lens["model"], sensor["native_response"]["pixel_pitch_um"], spec["sensor_size"],
            **({"scene_horizontal_fov_deg": spec["scene_horizontal_fov_deg"]}
               if spec["schema"] in {
                   COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
                   DIRECT_REHISTOGRAM_SCHEMA,
               } else
               {"scene_horizontal_fov_deg": radiance_record["horizontal_fov_deg"]}
               if radiance_record else {}),
            **({"scene_samples_row_col": spec["scene_samples_row_col"]}
               if spec["schema"] in {
                   DENSE_COMMON_SCENE_SCHEMA, DIRECT_REHISTOGRAM_SCHEMA,
               } else
               {"scene_samples_row_col": radiance_record["shape"][:2]}
               if radiance_record else {}),
        )
        if spec["schema"] == DIRECT_REHISTOGRAM_SCHEMA:
            require_direct_rehistogram_budget(resource_preflight)
        elif spec["schema"] in {
            BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
            RADIANCE_SCENE_SCHEMA,
        }:
            require_bounded_psf_budget(resource_preflight)
        else:
            require_central_psf_budget(resource_preflight)
    if spec["schema"] == LARGE_COMMON_SCENE_SCHEMA:
        resource_preflight = preflight_large_scene(
            lens["model"], sensor["native_response"]["pixel_pitch_um"],
            spec["sensor_size"],
            scene_horizontal_fov_deg=spec["scene_horizontal_fov_deg"],
            scene_samples_row_col=spec["scene_samples_row_col"],
        )
        require_large_scene_resources(resource_preflight)
    scenario = sensor_simulation_db_config(
        sensor["revision"]["report_code"],
        root=_db_root(service),
        run_id=sensor["revision"]["run_id"],
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw" or scenario.get(
        "sensor_illumination"
    ) is not None:
        raise ValueError("Saved sensor must use non-angular digital_raw output.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": sensor["revision"],
            **({"radiance_scene": source_identity(radiance_record)} if radiance_record else {}),
        },
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "resource_preflight": resource_preflight,
        "environment": _current(service),
        "limitation": (
            LOCAL_FIELD_LIMITATION if spec["schema"] == LOCAL_SCHEMA else
            DIRECT_PSF_REHISTOGRAM_LIMITATION
            if spec["schema"] == DIRECT_REHISTOGRAM_SCHEMA else
            (BOUNDED_LIMITATION + " The explicit FOV fixes only the input angular scene; "
             "native RAW angular pixel grids and lens distortion remain unmatched.")
            if spec["schema"] in {
                COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA, LARGE_COMMON_SCENE_SCHEMA,
            } else
            RADIANCE_SCENE_LIMITATION if radiance_record else
            BOUNDED_LIMITATION if spec["schema"] == BOUNDED_SCHEMA else LIMITATION
        ),
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_radiance_scene import load_radiance_scene, source_identity
    from .lens_sensor_raw_bridge import preview_lens_sensor_raw
    from .offline_assets import offline_asset_identity

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens × sensor RAW preview is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen lens, sensor, assets or executable changed.")
    spec = fresh["specification"]
    sensor = spec["sensor_revision"]
    radiance_scene = None
    if spec["schema"] == RADIANCE_SCENE_SCHEMA:
        radiance_scene = load_radiance_scene(service, spec["radiance_scene_id"])
        if source_identity(radiance_scene[0]) != fresh["source_identities"]["radiance_scene"]:
            raise IntegrityError("Registered radiance scene differs from the frozen selection.")
    report = preview_lens_sensor_raw(
        spec["lens_revision_id"],
        sensor["identifier"],
        sensor_run_id=sensor["run_id"],
        root=_db_root(service),
        workspace=service.workspace,
        scene_kind=(
            "registered_object_radiance" if radiance_scene else spec["scene_kind"]
        ),
        sensor_size=spec["sensor_size"],
        luminance_cd_m2=None if radiance_scene else spec["luminance_cd_m2"],
        exposure_s=spec["exposure_s"],
        noise_flag=spec["noise_flag"],
        seed=spec["seed"],
        accept_approximate_lens_radiometry=True,
        field_psf_index=spec["field_psf_index"] if spec["schema"] == LOCAL_SCHEMA else None,
        field_azimuth_deg=spec["field_azimuth_deg"] if spec["schema"] == LOCAL_SCHEMA else 0.0,
        psf_compute_mode=(
            "bounded_streaming" if spec["schema"] in {
                BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
                RADIANCE_SCENE_SCHEMA, LARGE_COMMON_SCENE_SCHEMA,
                DIRECT_REHISTOGRAM_SCHEMA,
            }
            else "precomputed"
        ),
        geometric_psf_sampling=(
            "direct_same_ray_rehistogram"
            if spec["schema"] == DIRECT_REHISTOGRAM_SCHEMA else "stored_interpolated"
        ),
        scene_horizontal_fov_deg=(
            spec["scene_horizontal_fov_deg"] if spec["schema"] in {
                COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA, LARGE_COMMON_SCENE_SCHEMA,
                DIRECT_REHISTOGRAM_SCHEMA,
            } else None
        ),
        scene_samples_row_col=(
            spec["scene_samples_row_col"] if spec["schema"] in {
                DENSE_COMMON_SCENE_SCHEMA, LARGE_COMMON_SCENE_SCHEMA,
                DIRECT_REHISTOGRAM_SCHEMA,
            }
            else None
        ),
        radiance_scene=radiance_scene,
        record_sampling_evidence=spec["schema"] in {
            LARGE_COMMON_SCENE_SCHEMA, DIRECT_REHISTOGRAM_SCHEMA,
        },
    )
    if report["lens_identity"] != fresh["source_identities"]["lens"] or report[
        "sensor_revision"
    ] != fresh["source_identities"]["sensor"]:
        raise IntegrityError("Computed lens or sensor differs from the frozen selection.")
    if canonical(report["resource_preflight"]) != canonical(fresh["resource_preflight"]):
        raise IntegrityError("Computed PSF resource contract differs from the frozen selection.")
    if radiance_scene:
        fresh_record, _ = load_radiance_scene(service, spec["radiance_scene_id"])
        if (
            source_identity(fresh_record) != fresh["source_identities"]["radiance_scene"]
            or report["scene"].get("source_asset") != source_identity(fresh_record)
        ):
            raise IntegrityError("Registered radiance source changed during capture.")
    if (
        report["upstream_assets"] != fresh["upstream_assets"]
        or offline_asset_identity(workspace=service.workspace) != fresh["upstream_assets"]
    ):
        raise IntegrityError("Pinned upstream assets changed during capture.")
    if canonical(prepare(service, spec)) != canonical(fresh):
        raise IntegrityError("Frozen sources or executable changed during capture.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens × sensor RAW preview was cancelled during execution.")
    raw = np.asarray(report["raw"]["values"], dtype=np.int32)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_artifact = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-original-raw.npy", "application/octet-stream"
    )
    raw_artifact.update(dtype="int32", shape=list(raw.shape), unit="DN", source_kind="simulated")
    report["raw_artifact"] = raw_artifact
    summary = {
        "schema": spec["schema"],
        "status": "completed",
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "upstream_assets": fresh["upstream_assets"],
        "resource_preflight": fresh["resource_preflight"],
        "preview": report,
        "limitation": fresh["limitation"],
    }
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-raw-preview-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [raw_artifact, result_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a lens × sensor RAW preview.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for key in ("specification", "source_identities", "upstream_assets"):
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError(f"Saved lens × sensor {key} differs from its frozen request.")
        if (
            summary.get("schema") != job["specification"]["schema"]
            or summary.get("resource_preflight") != job.get("resource_preflight")
            or summary.get("resource_preflight")
            != (summary.get("preview") or {}).get("resource_preflight")
        ):
            raise IntegrityError("Saved lens × sensor execution contract differs from its request.")
        if summary["schema"] in {
            BOUNDED_SCHEMA, COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA,
            RADIANCE_SCENE_SCHEMA, DIRECT_REHISTOGRAM_SCHEMA,
        }:
            preview = summary.get("preview") or {}
            psf = (preview.get("lens_model") or {}).get("psf_execution") or {}
            selection = (preview.get("lens_model") or {}).get("field_psf_selection") or {}
            resource = summary["resource_preflight"] or {}
            if (
                not isinstance(resource, dict)
                or not isinstance(psf, dict)
                or not isinstance(selection, dict)
                or resource.get("status") != "within_development_budget"
                or resource.get("scope") != (
                    "central_bounded_direct_same_ray_rehistogram_diagnostic"
                    if summary["schema"] == DIRECT_REHISTOGRAM_SCHEMA
                    else "central_bounded_strict_raytrace"
                )
                or preview.get("scope") != "approximate_geometric_central_field_raw_preview"
                or psf.get("mode") != "strict_bounded_streaming"
                or psf.get("complete_precomputed_stack") is not False
                or type(psf.get("peak_cached_kernels")) is not int
                or not 0 <= psf["peak_cached_kernels"] <= 4
                or selection.get("psf_compute_mode") != "bounded_streaming"
            ):
                raise IntegrityError("Saved bounded strict PSF execution evidence is missing.")
            if summary["schema"] == DIRECT_REHISTOGRAM_SCHEMA:
                direct = (preview.get("lens_model") or {}).get(
                    "direct_same_ray_rehistogram"
                )
                if (
                    not isinstance(direct, dict)
                    or (preview.get("lens_model") or {}).get("psf_source_sampling")
                    != "direct_same_ray_rehistogram"
                    or resource.get("derived_psf_float64_bytes", 0)
                    > resource.get("derived_psf_budget_bytes", 0)
                    or resource.get("hard_memory_or_wall_time_bound") is not False
                ):
                    raise IntegrityError("Saved direct-ray PSF derivation evidence is missing.")
            if summary["schema"] in {
                COMMON_SCENE_SCHEMA, DENSE_COMMON_SCENE_SCHEMA, RADIANCE_SCENE_SCHEMA,
                DIRECT_REHISTOGRAM_SCHEMA,
            }:
                scene = preview.get("scene") or {}
                grid = scene.get("angular_grid_contract") or {}
                output = (preview.get("capture") or {}).get("native_output_angular_grid") or {}
                size = summary["specification"].get("sensor_size")
                source_shape = grid.get("source_shape")
                source_step = grid.get("center_angular_step_deg_row_col")
                native_pitch = output.get("pixel_pitch_m_row_col")
                native_step = output.get("center_angular_step_deg_row_col")
                saved_pitch_um = (preview.get("sensor_model") or {}).get("pixel_pitch_um")

                def positive_pair(value: Any) -> bool:
                    return (
                        isinstance(value, list) and len(value) == 2
                        and all(
                            type(item) in (int, float) and math.isfinite(item) and item > 0
                            for item in value
                        )
                    )

                def matches(value: Any, expected: float) -> bool:
                    return (
                        type(value) in (int, float) and math.isfinite(value)
                        and math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12)
                    )

                def positive_scalar(value: Any) -> bool:
                    return type(value) in (int, float) and math.isfinite(value) and value > 0

                expected_source = (
                    summary["source_identities"]["radiance_scene"]["shape"][:2]
                    if summary["schema"] == RADIANCE_SCENE_SCHEMA else
                    summary["specification"]["scene_samples_row_col"]
                    if summary["schema"] in {
                        DENSE_COMMON_SCENE_SCHEMA, DIRECT_REHISTOGRAM_SCHEMA,
                    } else
                    [max(32, 2 * size[0]), max(32, 2 * size[1])]
                )
                expected_fov = (
                    summary["source_identities"]["radiance_scene"]["horizontal_fov_deg"]
                    if summary["schema"] == RADIANCE_SCENE_SCHEMA else
                    summary["specification"]["scene_horizontal_fov_deg"]
                )
                if (
                    grid.get("mode") != "explicit_common_input_scene"
                    or grid.get("native_output_grid_alignment") != "not_established"
                    or grid.get("horizontal_fov_deg")
                    != expected_fov
                    or scene.get("horizontal_fov_deg") != grid.get("horizontal_fov_deg")
                    or output.get("status")
                    != "paraxial_center_only_full_distortion_map_not_established"
                    or output.get("sensor_resize") is not False
                    or not isinstance(size, list) or len(size) != 2
                    or any(type(value) is not int or not 8 <= value <= 32 for value in size)
                    or source_shape != expected_source
                    or not positive_scalar(grid.get("vertical_fov_deg"))
                    or not positive_scalar(output.get("image_distance_m"))
                    or not positive_scalar(saved_pitch_um)
                    or not positive_pair(source_step)
                    or not positive_pair(native_pitch)
                    or not positive_pair(native_step)
                    or resource.get("scene_samples_col") != source_shape[1]
                    or (
                        summary["schema"] in {
                            DENSE_COMMON_SCENE_SCHEMA, RADIANCE_SCENE_SCHEMA,
                            DIRECT_REHISTOGRAM_SCHEMA,
                        }
                        and resource.get("scene_samples_row") != source_shape[0]
                    )
                    or not matches(native_pitch[0], saved_pitch_um * 1e-6)
                    or not matches(native_pitch[1], saved_pitch_um * 1e-6)
                    or not matches(
                        source_step[0], math.degrees(math.atan(
                            2.0 * math.tan(math.radians(grid["vertical_fov_deg"]) / 2.0)
                            / source_shape[0]
                        ))
                    )
                    or not matches(
                        source_step[1], math.degrees(math.atan(
                            2.0 * math.tan(math.radians(grid["horizontal_fov_deg"]) / 2.0)
                            / source_shape[1]
                        ))
                    )
                    or not matches(
                        native_step[0], math.degrees(math.atan(
                            native_pitch[0] / output["image_distance_m"]
                        ))
                    )
                    or not matches(
                        native_step[1], math.degrees(math.atan(
                            native_pitch[1] / output["image_distance_m"]
                        ))
                    )
                    or not matches(
                        output.get("horizontal_fov_deg"), math.degrees(2.0 * math.atan(
                            size[1] * native_pitch[1] / (2.0 * output["image_distance_m"])
                        ))
                    )
                    or not matches(
                        output.get("vertical_fov_deg"), math.degrees(2.0 * math.atan(
                            size[0] * native_pitch[0] / (2.0 * output["image_distance_m"])
                        ))
                    )
                    or not matches(resource.get("scene_sample_spacing_um"),
                                   2.0 * output["image_distance_m"]
                                   * math.tan(math.radians(grid["horizontal_fov_deg"]) / 2.0)
                                   / source_shape[1] * 1e6)
                ):
                    raise IntegrityError("Saved common input angular scene evidence is missing.")
                if summary["schema"] == RADIANCE_SCENE_SCHEMA:
                    from .lens_sensor_radiance_scene import (
                        REFERENCE_PLANE,
                        UNIT,
                        load_radiance_scene,
                        source_identity,
                    )

                    record, _ = load_radiance_scene(
                        service, summary["specification"]["radiance_scene_id"]
                    )
                    identity = source_identity(record)
                    if (
                        identity != summary["source_identities"].get("radiance_scene")
                        or scene.get("kind") != "registered_object_radiance"
                        or scene.get("source_asset") != identity
                        or scene.get("luminance_cd_m2") is not None
                        or scene.get("source_unit") != UNIT
                        or scene.get("source_reference_plane") != REFERENCE_PLANE
                        or scene.get("source_wavelength_nm") != record["wavelength_nm"]
                        or scene.get("spectral_conversion")
                        != "linear_energy_density_then_energy_to_quanta_once"
                    ):
                        raise IntegrityError("Saved object-radiance source evidence is missing.")
        if job["status"] == "succeeded":
            results = [
                item for item in job["artifacts"]
                if item["name"] == "lens-sensor-raw-preview-result.json"
            ]
            if len(results) != 1 or (
                service.registry.blob_path(results[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved lens × sensor result differs from its artifact.")
            descriptor = summary["preview"]["raw_artifact"]
            if descriptor["sha256"] not in {item["sha256"] for item in job["artifacts"]}:
                raise IntegrityError("Original RAW artifact is not attached to this job.")
            content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
            original = np.load(io.BytesIO(content), allow_pickle=False)
            if original.dtype != np.dtype("int32") or not np.array_equal(
                original, summary["preview"]["raw"]["values"]
            ):
                raise IntegrityError("Saved original integer RAW differs from the result.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-raw/template", tags=["lens_sensor_raw"])
    def template(q: str = "", scope: str = "central"):
        from .lens_catalog_bridge import lens_revision_catalog
        from .lens_sensor_raw_bridge import (
            BOUNDED_LIMITATION,
            CENTRAL_PSF_STACK_BUDGET_BYTES,
            DIRECT_PSF_REHISTOGRAM_LIMITATION,
            LOCAL_FIELD_LIMITATION,
            RADIANCE_SCENE_LIMITATION,
        )
        from .sensor_db_bridge import sensor_revision_catalog

        if scope not in {
            "central", "central_bounded", "central_bounded_common_scene",
            "central_bounded_dense_common_scene", "central_bounded_radiance_scene",
            "central_bounded_large_scene_preflight",
            "central_bounded_direct_geometric_psf_diagnostic",
            "local_field_psf"
        }:
            raise ValueError(
                "scope must be central, central_bounded, central_bounded_common_scene "
                "central_bounded_dense_common_scene, central_bounded_radiance_scene "
                "central_bounded_large_scene_preflight, "
                "central_bounded_direct_geometric_psf_diagnostic or local_field_psf."
            )
        local = scope == "local_field_psf"
        bounded = scope == "central_bounded"
        common_scene = scope == "central_bounded_common_scene"
        dense_scene = scope == "central_bounded_dense_common_scene"
        radiance_scene = scope == "central_bounded_radiance_scene"
        large_scene = scope == "central_bounded_large_scene_preflight"
        direct_rehistogram = scope == "central_bounded_direct_geometric_psf_diagnostic"

        return {
            "specification": (
                default_local_spec() if local else
                default_radiance_scene_spec() if radiance_scene else
                default_direct_rehistogram_spec() if direct_rehistogram else
                default_large_common_scene_spec() if large_scene else
                default_dense_common_scene_spec() if dense_scene else
                default_common_scene_spec() if common_scene else
                default_bounded_spec() if bounded else default_spec()
            ),
            "lens_catalog": lens_revision_catalog(catalog_root=_db_root(service), query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "resource_contract": {
                "lens_preview_ready_meaning": "validated_stored_psf_asset_only",
                "central_execution_requires_pair_preflight": True,
                "central_float32_stack_development_budget_gib": (
                    CENTRAL_PSF_STACK_BUDGET_BYTES / 1024**3
                ),
                "central_bounded_uses_heuristic_working_budget": True,
                "large_scene_requires_enforced_child_process_rss_guard": True,
                "large_scene_guard_currently_available": False,
                "central_bounded_psf_execution": "strict_on_demand_with_exact_zero_crop",
                **({
                    "geometric_psf_sampling": "pinned_same_ray_direct_rehistogram_all_stored_nodes",
                    "runtime_guard": "heuristic_resource_budget_soft_time_only",
                    "azimuth_rotation_qualified": False,
                    "physical_mtf_comparison_allowed": False,
                } if direct_rehistogram else {}),
                "common_scene_fov_fixes_input_only": True,
                "common_scene_native_output_grid_alignment": "not_established",
                **({
                    "registered_radiance_reference_plane":
                        "object_directional_radiance_before_lens",
                    "registered_radiance_unit": "W/m^2/sr/nm",
                } if radiance_scene else {}),
                "execution_guaranteed_by_budget": False,
            },
            "limitation": (
                LOCAL_FIELD_LIMITATION if local else
                DIRECT_PSF_REHISTOGRAM_LIMITATION if direct_rehistogram else
                RADIANCE_SCENE_LIMITATION if radiance_scene else
                (BOUNDED_LIMITATION + " The explicit FOV fixes only the input angular scene; "
                 "native RAW angular pixel grids and lens distortion remain unmatched.")
                if common_scene or dense_scene else BOUNDED_LIMITATION if bounded else LIMITATION
            ),
        }

    @app.post("/api/v1/lens-sensor-raw", status_code=202, tags=["lens_sensor_raw"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-raw", tags=["lens_sensor_raw"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-raw/{identity}", tags=["lens_sensor_raw"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-raw/{identity}/cancel", tags=["lens_sensor_raw"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-raw/{identity}/export", tags=["lens_sensor_raw"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-raw.json"'},
        )

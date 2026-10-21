"""Worker lifecycle for authored PWM on one pinned geometric Lens x Sensor model."""

from __future__ import annotations

import hashlib
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any

from fastapi import File, Form, UploadFile

from .lens_sensor_raw_bridge import _number
from .lens_sensor_timing_mode import (
    CAPTURE_PROGRAM,
    MAX_MODE_BYTES,
    MAX_SOURCE_BYTES,
    PHASE_ORIGIN,
    mode_binding,
    validate_timing_mode,
    validate_timing_mode_bytes,
)
from .lens_sensor_timing_mode import (
    SCHEMA as TIMING_MODE_SCHEMA,
)
from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_pwm_lfm_study_v1"
BOUNDED_SCHEMA = "camerae2e_lens_sensor_pwm_lfm_study_v2"
TIMED_BOUNDED_SCHEMA = "camerae2e_lens_sensor_pwm_lfm_study_v3"
JOB_KIND = "lens_sensor_lfm"
LIMITATION = (
    "An ideal scalar PWM source is integrated per authored rolling-shutter row "
    "using one pinned geometric Lens DB and saved Sensor DB response. Native "
    "readout and saved FPN parameter assumptions produce original integer RAW; "
    "spatial FPN maps are model-generated on the virtual window, not measured "
    "or reused from a physical sensor. Lens radiometry, CRA and "
    "diffraction are unvalidated; 20 us line time is assumed. The configured "
    "window uses the saved native CFA tile at virtual origin zero, not a "
    "located crop of the saved full array. This is phase "
    "sensitivity, not a measured product LFM or HDR-mode result."
)
TIMED_LIMITATION = (
    "A SHA-256-pinned timing declaration replaces the authored default line time "
    "for relative virtual-window rolling-row sensitivity. Its source bytes and "
    "sensor/readout/exposure applicability are checked, but the asserted numeric "
    "value is not independently verified. Native crop and absolute PWM phase are "
    "unknown. Two brackets are independently simulated static-scene captures, "
    "not a vendor HDR/LFM acquisition mode or physical validation."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 PWM/LFM 위상 진단",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "sensor_size": [8, 8],
        "exposure_s": [0.001, 0.004],
        "pwm_period_s": 0.002,
        "duty_cycle": 0.5,
        "off_to_on_ratio": 0.02,
        "on_luminance_cd_m2": 1000.0,
        "seed": 7,
        "accept_approximate_lens_radiometry": False,
        "acknowledge_simulation_limits": False,
    }


def default_bounded_spec() -> dict[str, Any]:
    return {
        **default_spec(),
        "schema": BOUNDED_SCHEMA,
        "name": "렌즈 × 센서 PWM/LFM bounded strict PSF 진단",
    }


def default_timed_bounded_spec() -> dict[str, Any]:
    return {
        **default_bounded_spec(),
        "schema": TIMED_BOUNDED_SCHEMA,
        "name": "렌즈 × 센서 PWM/LFM · 출처 결속 timing 진단",
        "timing_mode_sha256": "",
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Declare exactly the joint Lens x Sensor PWM/LFM fields.")
    schema = payload.get("schema")
    if schema not in {SCHEMA, BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA}:
        raise ValueError("Unsupported joint Lens x Sensor PWM/LFM schema.")
    expected = default_timed_bounded_spec() if schema == TIMED_BOUNDED_SCHEMA else default_spec()
    if set(payload) != set(expected):
        raise ValueError("Declare exactly the joint Lens x Sensor PWM/LFM fields.")
    if (
        payload["accept_approximate_lens_radiometry"] is not True
        or payload["acknowledge_simulation_limits"] is not True
    ):
        raise ValueError("Accept approximate lens radiometry and simulation limits explicitly.")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Joint PWM/LFM name requires 1..160 characters.")
    lens = payload["lens_revision_id"]
    if not isinstance(lens, str) or not 1 <= len(lens.strip()) <= 128:
        raise ValueError("Select one explicit Lens DB revision ID.")
    sensor = payload["sensor_revision"]
    if (
        not isinstance(sensor, dict) or set(sensor) != {"identifier", "run_id"}
        or any(not isinstance(sensor[key], str) or not 1 <= len(sensor[key].strip()) <= 128
               for key in ("identifier", "run_id"))
    ):
        raise ValueError("Select one explicit saved sensor identifier and run ID.")
    size = payload["sensor_size"]
    if (
        not isinstance(size, list) or len(size) != 2
        or any(type(value) is not int or not 8 <= value <= 32 or value % 2 for value in size)
    ):
        raise ValueError("sensor_size needs two even integer dimensions from 8 to 32.")
    values = payload["exposure_s"]
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("Declare exactly two ascending exposure_s values.")
    exposures = [_number(value, "exposure_s", 0.0005, 0.02) for value in values]
    if exposures[0] >= exposures[1]:
        raise ValueError("Joint PWM/LFM exposures must be distinct and ascending.")
    period = _number(payload["pwm_period_s"], "pwm_period_s", 0.0005, 0.02)
    if exposures[-1] / period > 32:
        raise ValueError("PWM exposure exceeds the bounded 32-cycle integration domain.")
    duty = _number(payload["duty_cycle"], "duty_cycle", 0.05, 0.95)
    off = _number(payload["off_to_on_ratio"], "off_to_on_ratio", 0.0, 0.5)
    luma = _number(payload["on_luminance_cd_m2"], "on_luminance_cd_m2", 0.01, 10000.0)
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if schema == TIMED_BOUNDED_SCHEMA:
        sha = payload["timing_mode_sha256"]
        if (
            not isinstance(sha, str) or len(sha) != 64
            or any(character not in "0123456789abcdef" for character in sha)
        ):
            raise ValueError("Select an imported SHA-256-pinned timing mode.")
    result = deepcopy(payload)
    result.update(
        name=payload["name"].strip(), lens_revision_id=lens.strip(),
        sensor_revision={key: value.strip() for key, value in sensor.items()},
        exposure_s=exposures, pwm_period_s=period, duty_cycle=duty,
        off_to_on_ratio=off, on_luminance_cd_m2=luma,
    )
    return result


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def _mode_binding(sensor: dict, saved: dict, spec: dict) -> dict:
    profile = saved["metadata"].get("readout_profile") or {}
    readout = saved["metadata"]["model_snapshot"]["readout"]
    if (
        profile.get("readout") != readout
        or profile.get("profile") != sensor["readout"]["profile"]
        or profile.get("provenance", {}).get("classification")
        != sensor["readout"]["classification"]
    ):
        raise ValueError("Timing-mode Sensor readout profile differs from its saved revision.")
    return mode_binding(
        sensor["revision"], readout,
        {
            "profile": profile["profile"],
            "classification": profile["provenance"]["classification"],
        },
        spec["sensor_size"], spec["exposure_s"],
    )


def _selected_timing_mode(service: Any, sha: str, binding: dict) -> dict:
    path = service.registry.blob_path(sha)
    mode = validate_timing_mode_bytes(path.read_bytes(), sha)
    validate_timing_mode(mode, expected_binding=binding)
    source_sha = mode["provenance"]["source_document_sha256"]
    if source_sha is not None:
        source = service.registry.blob_path(source_sha)
        if not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
            raise ValueError("Timing source evidence exceeds its byte bound.")
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
            raise IntegrityError("Timing source evidence bytes changed.")
    return mode


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from pyisetcam.sensor_simulation_db import (
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    from .imaging import _current
    from .lens_catalog_bridge import geometric_lens_preview
    from .lens_sensor_raw_bridge import (
        bounded_psf_resource_preflight,
        require_bounded_psf_budget,
    )
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import inspect_sensor_revision

    spec = specification(payload)
    lens = geometric_lens_preview(spec["lens_revision_id"], catalog_root=_db_root(service))
    selected = spec["sensor_revision"]
    sensor = inspect_sensor_revision(
        selected["identifier"], root=_db_root(service), run_id=selected["run_id"]
    )
    if not sensor["executable"] or sensor["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError("Joint PWM/LFM requires an executable saved db_estimate_v1 sensor.")
    resource_preflight = None
    if spec["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA}:
        resource_preflight = bounded_psf_resource_preflight(
            lens["model"], sensor["native_response"]["pixel_pitch_um"],
            spec["sensor_size"],
        )
        require_bounded_psf_budget(resource_preflight)
    scenario = sensor_simulation_db_config(
        sensor["revision"]["report_code"], root=_db_root(service),
        run_id=sensor["revision"]["run_id"],
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw" or scenario.get(
        "sensor_illumination"
    ) is not None:
        raise ValueError("Joint PWM/LFM requires non-angular digital_raw saved sensor.")
    revision = sensor["revision"]
    saved = sensor_simulation_db_get(
        revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
    )
    native_cfa = saved["metadata"].get("native_cfa") or {}
    if not native_cfa.get("pattern") or not native_cfa.get("filter_names"):
        raise ValueError("Joint PWM/LFM requires the saved sensor's native CFA tile.")
    selected_mode = None
    if spec["schema"] == TIMED_BOUNDED_SCHEMA:
        selected_mode = _selected_timing_mode(
            service, spec["timing_mode_sha256"], _mode_binding(sensor, saved, spec)
        )
    frozen = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "source_identities": {
            "lens": lens["identity"], "sensor": sensor["revision"],
        },
        "native_cfa": native_cfa,
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "environment": _current(service),
        "limitation": (
            TIMED_LIMITATION + " Strict geometric PSF is streamed on demand."
            if spec["schema"] == TIMED_BOUNDED_SCHEMA else
            LIMITATION + " Strict geometric PSF is cropped and streamed on demand; "
            "the resource estimate is a heuristic, not a peak-memory bound."
            if spec["schema"] == BOUNDED_SCHEMA else LIMITATION
        ),
    }
    if spec["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA}:
        frozen["resource_preflight"] = resource_preflight
    if selected_mode is not None:
        frozen["timing_mode"] = selected_mode
        frozen["timing_mode_sha256"] = spec["timing_mode_sha256"]
        frozen["timing_source_document_sha256"] = selected_mode["provenance"][
            "source_document_sha256"
        ]
    return frozen


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_lfm import evaluate_lens_sensor_pwm_lfm

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Joint Lens x Sensor PWM/LFM is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens x Sensor PWM/LFM request or source changed.")
    spec = frozen["specification"]
    sensor = spec["sensor_revision"]
    with tempfile.TemporaryDirectory(prefix="camera-lens-sensor-lfm-") as temporary:
        report = evaluate_lens_sensor_pwm_lfm(
            spec["lens_revision_id"], sensor["identifier"],
            sensor_run_id=sensor["run_id"], root=_db_root(service),
            workspace=service.workspace, output_dir=Path(temporary) / "evidence",
            exposure_s=spec["exposure_s"], pwm_period_s=spec["pwm_period_s"],
            duty_cycle=spec["duty_cycle"], off_to_on_ratio=spec["off_to_on_ratio"],
            on_luminance_cd_m2=spec["on_luminance_cd_m2"],
            sensor_size=spec["sensor_size"], seed=spec["seed"],
            accept_approximate_lens_radiometry=True,
            psf_compute_mode=(
                "bounded_streaming"
                if spec["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA}
                else "precomputed"
            ),
            timing_mode=frozen.get("timing_mode"),
            timing_mode_sha256=frozen.get("timing_mode_sha256"),
        )
        if (
            report["lens_identity"] != frozen["source_identities"]["lens"]
            or report["sensor_revision"] != frozen["source_identities"]["sensor"]
            or report["upstream_assets"] != frozen["upstream_assets"]
            or report["geometry_provenance"]["saved_native_cfa"] != frozen["native_cfa"]
        ):
            raise IntegrityError("Joint PWM/LFM output lost its frozen lens/sensor binding.")
        if spec["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA} and (
            report.get("schema") != (
                "camerae2e_lens_sensor_pwm_lfm_v3"
                if spec["schema"] == TIMED_BOUNDED_SCHEMA
                else "camerae2e_lens_sensor_pwm_lfm_v2"
            )
            or canonical(report.get("resource_preflight"))
            != canonical(frozen["resource_preflight"])
            or report.get("psf_compute_mode") != "bounded_streaming"
            or not _valid_bounded_execution(report.get("psf_execution"))
        ):
            raise IntegrityError("Computed LFM bounded PSF execution lost its frozen contract.")
        if spec["schema"] == TIMED_BOUNDED_SCHEMA and (
            report["controls"].get("timing_mode") != frozen["timing_mode"]
            or report["controls"].get("timing_mode_sha256") != frozen["timing_mode_sha256"]
            or report["controls"].get("timing_mode_canonical_sha256")
            != digest(frozen["timing_mode"])
            or report["controls"].get("line_time_s")
            != frozen["timing_mode"]["timing"]["line_time_s"]
        ):
            raise IntegrityError("Computed LFM timing differs from its selected mode.")
        artifacts = []
        saved = {}
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError("Joint PWM/LFM artifact hash differs from computed result.")
            descriptor = service.registry.store_blob(
                content, f"lens-sensor-lfm-evidence.{kind}", media
            )
            artifacts.append(descriptor)
            saved[kind] = descriptor
        report = {key: value for key, value in report.items()
                  if key not in {"artifacts", "artifact_sha256"}}
        report["artifacts"] = saved
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Joint PWM/LFM source or executable changed during study.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Joint PWM/LFM study was cancelled during execution.")
    summary = {
        "schema": spec["schema"],
        "status": "completed",
        "specification": spec,
        "source_identities": frozen["source_identities"],
        "native_cfa": frozen["native_cfa"],
        "upstream_assets": frozen["upstream_assets"],
        "report": report,
        "verification": report["verification"],
        "limitation": frozen["limitation"],
    }
    if spec["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA}:
        summary["resource_preflight"] = frozen["resource_preflight"]
    if spec["schema"] == TIMED_BOUNDED_SCHEMA:
        summary["timing_mode_sha256"] = frozen["timing_mode_sha256"]
        summary["timing_source_document_sha256"] = frozen["timing_source_document_sha256"]
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-lfm-result.json", "application/json"
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens x Sensor PWM/LFM study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for field in ("specification", "source_identities", "native_cfa", "upstream_assets"):
            if canonical(summary[field]) != canonical(job[field]):
                raise IntegrityError(f"Joint PWM/LFM result differs from frozen {field}.")
        if summary["schema"] in {BOUNDED_SCHEMA, TIMED_BOUNDED_SCHEMA} and (
            summary.get("resource_preflight") != job.get("resource_preflight")
            or summary.get("resource_preflight")
            != summary["report"].get("resource_preflight")
            or summary["report"].get("schema") != (
                "camerae2e_lens_sensor_pwm_lfm_v3"
                if summary["schema"] == TIMED_BOUNDED_SCHEMA
                else "camerae2e_lens_sensor_pwm_lfm_v2"
            )
            or summary["report"].get("psf_compute_mode") != "bounded_streaming"
            or not _valid_bounded_execution(summary["report"].get("psf_execution"))
        ):
            raise IntegrityError("Saved LFM bounded PSF execution evidence is missing.")
        if summary["schema"] == TIMED_BOUNDED_SCHEMA:
            mode = _selected_timing_mode(
                service, job["timing_mode_sha256"], job["timing_mode"]["binding"]
            )
            current = prepare(service, job["specification"])
            if (
                mode != job["timing_mode"]
                or any(
                    current[key] != job[key]
                    for key in (
                        "source_identities", "native_cfa", "upstream_assets",
                        "timing_mode", "timing_mode_sha256", "timing_source_document_sha256",
                    )
                )
                or summary.get("timing_mode_sha256") != job["timing_mode_sha256"]
                or summary.get("timing_source_document_sha256")
                != job["timing_source_document_sha256"]
                or summary["report"]["controls"].get("timing_mode") != mode
                or summary["report"]["controls"].get("timing_mode_sha256")
                != job["timing_mode_sha256"]
                or summary["report"]["controls"].get("timing_mode_canonical_sha256")
                != digest(mode)
            ):
                raise IntegrityError("Saved LFM timing-mode evidence differs from frozen source.")
        if job["status"] == "succeeded":
            for descriptor in summary["report"]["artifacts"].values():
                if descriptor not in job["artifacts"]:
                    raise IntegrityError("Joint PWM/LFM evidence link is missing.")
            for descriptor in job["artifacts"]:
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError("Joint PWM/LFM evidence bytes differ.")
            result = [item for item in job["artifacts"]
                      if item["name"] == "lens-sensor-lfm-result.json"]
            if len(result) != 1 or service.registry.blob_path(
                result[0]["sha256"]
            ).read_bytes() != canonical(summary).encode():
                raise IntegrityError("Joint PWM/LFM result differs from its artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def _valid_bounded_execution(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("mode") == "strict_bounded_streaming"
        and value.get("complete_precomputed_stack") is False
        and type(value.get("peak_cached_kernels")) is int
        and 0 <= value["peak_cached_kernels"] <= 4
    )


def import_timing_mode(
    service: Any, content: bytes, expected_sha256: str,
    source_evidence: bytes | None = None,
) -> dict[str, Any]:
    """Store supplied mode and optional source bytes without endorsing their claims."""
    mode = validate_timing_mode_bytes(content, expected_sha256)
    expected_source = mode["provenance"]["source_document_sha256"]
    if expected_source is None:
        if source_evidence is not None:
            raise ValueError("Authored timing cannot carry vendor or measurement source bytes.")
        source_descriptor = None
    else:
        if (
            not isinstance(source_evidence, bytes)
            or not 0 < len(source_evidence) <= MAX_SOURCE_BYTES
            or hashlib.sha256(source_evidence).hexdigest() != expected_source
        ):
            raise ValueError("Timing source evidence bytes differ from their declared SHA-256.")
        source_descriptor = service.registry.store_blob(
            source_evidence, "lens-sensor-timing-source.bin", "application/octet-stream"
        )
    mode_descriptor = service.registry.store_blob(
        content, "lens-sensor-timing-mode.json", "application/json"
    )
    if mode_descriptor["sha256"] != expected_sha256:
        raise IntegrityError("Stored timing-mode JSON differs from submitted bytes.")
    record_data = {
        "schema": TIMING_MODE_SCHEMA,
        "mode_id": mode["mode_id"],
        "timing_mode_sha256": expected_sha256,
        "source_kind": mode["provenance"]["source_kind"],
        "artifacts": [
            mode_descriptor,
            *([] if source_descriptor is None else [source_descriptor]),
        ],
    }
    record_id = f"timing-mode-{expected_sha256}"
    try:
        service.registry.put("timing_modes", record_data, identity=record_id)
    except ConflictError as exc:
        existing = service.registry.get("timing_modes", record_id)
        if {
            key: value for key, value in existing.items()
            if key not in {"id", "created_at", "content_hash"}
        } != record_data:
            raise IntegrityError(
                "Existing timing-mode evidence differs from supplied bytes."
            ) from exc
    return {
        "schema": TIMING_MODE_SCHEMA,
        "record_id": record_id,
        "artifact": mode_descriptor,
        "source_evidence": source_descriptor,
        "timing_mode_sha256": expected_sha256,
        "mode_id": mode["mode_id"],
        "source_kind": mode["provenance"]["source_kind"],
        "verification": {
            "source_bytes": "sha256_verified" if source_descriptor is not None else "not_supplied",
            "timing_value": "declared_not_independently_verified",
            "sensor_applicability": "checked_on_job_submission_and_readback",
        },
    }


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-lfm/template", tags=["lens_sensor_lfm"])
    def template(q: str = "", scope: str = "central"):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        if scope not in {"central", "central_bounded", "central_bounded_timed"}:
            raise ValueError("scope must be central, central_bounded or central_bounded_timed.")
        return {
            "specification": (
                default_timed_bounded_spec() if scope == "central_bounded_timed"
                else default_bounded_spec() if scope == "central_bounded" else default_spec()
            ),
            "lens_catalog": lens_revision_catalog(catalog_root=_db_root(service), query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "limitation": (
                LIMITATION + " Strict geometric PSF is cropped and streamed on demand; "
                "the resource estimate is a heuristic, not a peak-memory bound."
                if scope == "central_bounded" else
                TIMED_LIMITATION if scope == "central_bounded_timed" else LIMITATION
            ),
            "resource_contract": {
                "central_bounded_uses_heuristic_working_budget": True,
                "execution_guaranteed": False,
            },
        }

    @app.post("/api/v1/lens-sensor-lfm/timing-modes/template", tags=["lens_sensor_lfm"])
    def timing_mode_template(payload: dict):
        from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

        from .sensor_db_bridge import inspect_sensor_revision

        if not isinstance(payload, dict) or set(payload) != {
            "sensor_revision", "sensor_size", "exposure_s"
        }:
            raise ValueError("Select a saved Sensor revision, virtual size and exposure pair.")
        selected = payload["sensor_revision"]
        if not isinstance(selected, dict) or set(selected) != {"identifier", "run_id"}:
            raise ValueError("Select an exact saved Sensor identifier and run ID.")
        sensor = inspect_sensor_revision(
            selected["identifier"], root=_db_root(service), run_id=selected["run_id"]
        )
        if not sensor["executable"] or sensor["native_response"]["kind"] != "db_estimate_v1":
            raise ValueError("Timing mode requires an executable saved db_estimate_v1 Sensor.")
        revision = sensor["revision"]
        saved = sensor_simulation_db_get(
            revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
        )
        binding = _mode_binding(sensor, saved, payload)
        return {
            "timing_mode_document_template": {
                "schema": TIMING_MODE_SCHEMA,
                "mode_id": "authored-relative-line-time",
                "binding": binding,
                "timing": {
                    "shutter": "rolling", "line_time_s": 20e-6, "frame_start_s": 0.0,
                    "phase_origin": PHASE_ORIGIN, "capture_program": CAPTURE_PROGRAM,
                },
                "provenance": {
                    "source_kind": "authored_assumption",
                    "source_uri": "authored://relative-row-timing-sensitivity",
                    "source_document_sha256": None,
                    "method_and_version": "user_declared_line_time_v1",
                    "value_verification": "declared_not_independently_verified",
                },
            },
            "source_sensor_mode_available": False,
            "source_sensor_timing_available": False,
            "limitation": TIMED_LIMITATION,
        }

    @app.post(
        "/api/v1/lens-sensor-lfm/timing-modes/import", status_code=201,
        tags=["lens_sensor_lfm"],
    )
    async def timing_mode_import(
        timing_mode: Annotated[UploadFile, File()], sha256: Annotated[str, Form()],
        source_evidence: Annotated[UploadFile | None, File()] = None,
    ):
        if timing_mode.content_type not in {"application/json", "application/octet-stream"}:
            raise ValueError("Timing-mode upload must be JSON bytes.")
        content = await timing_mode.read(MAX_MODE_BYTES + 1)
        evidence = (
            None if source_evidence is None
            else await source_evidence.read(MAX_SOURCE_BYTES + 1)
        )
        return import_timing_mode(service, content, sha256, evidence)

    @app.post("/api/v1/lens-sensor-lfm", status_code=202, tags=["lens_sensor_lfm"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-lfm", tags=["lens_sensor_lfm"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-lfm/{identity}", tags=["lens_sensor_lfm"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-lfm/{identity}/cancel", tags=["lens_sensor_lfm"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-lfm/{identity}/export", tags=["lens_sensor_lfm"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens × Sensor LFM result is not ready.")
        return Response(
            canonical(job), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-lfm.json"'},
        )

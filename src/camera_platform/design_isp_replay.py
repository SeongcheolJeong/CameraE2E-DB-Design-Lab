"""R1 design-case ISP tuning over an immutable, already acquired integer RAW.

This path intentionally does not call the optical/electrical response chain. A
replay is a separate job whose source is one saved experiment case, never an
edit to that case or to the experiment's frozen selection.
"""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy

import numpy as np
from PIL import Image

from . import isp
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_design_isp_replay_v1"
JOB_KIND = "design_isp_replay"
PROFILE_SOURCE = "user_declared_engineering_tuning"


def _read_blob(service, artifact):
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise IntegrityError("Design ISP source or output artifact bytes differ")
    return content


def _load_array(service, artifact):
    content = _read_blob(service, artifact)
    try:
        return np.load(io.BytesIO(content), allow_pickle=False)
    except (OSError, ValueError, TypeError) as exc:
        raise IntegrityError("Design ISP array artifact cannot be decoded") from exc


def source_case(service, experiment_id, case_id):
    """Resolve a completed case solely from saved evidence, without re-acquisition."""
    from .design_experiments import SCHEMA as EXPERIMENT_SCHEMA
    from .design_experiments import get as get_experiment

    source = get_experiment(service, experiment_id)
    if (source["status"] != "succeeded"
            or source["specification"].get("schema") != EXPERIMENT_SCHEMA):
        raise ValueError("R1 ISP replay requires a completed registered design experiment")
    rows = source["result"]["summary"]["rows"]
    matches = [row for row in rows if row["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError("Select exactly one saved design experiment case")
    row = matches[0]
    if ("geometry" not in row or "raw_format" not in row
            or "rgb_artifact" not in row or "rgb_preview_artifact" not in row):
        raise ValueError("This earlier case lacks the full RAW/ISP replay contract")
    original_profile = isp.validate_profile(source["isp_profile"])
    if (row["isp_profile_sha256"] != digest(original_profile)
            or row["isp_input_raw_sha256"] != row["raw_array_sha256"]):
        raise IntegrityError("Saved case ISP binding differs from its original RAW/profile")
    raw = _load_array(service, row["raw_artifact"])
    original_rgb = _load_array(service, row["rgb_artifact"])
    # _input validates the exact linear, single-stream RAW format and native
    # coordinates. Older partial case metadata must not be guessed into one.
    isp._input(raw, row["raw_format"], row["geometry"])
    if (raw.shape != tuple(row["raw_artifact"]["shape"])
            or str(raw.dtype) != row["raw_artifact"]["dtype"]
            or hashlib.sha256(raw.tobytes(order="C")).hexdigest() != row["raw_array_sha256"]
            or original_rgb.shape != (*raw.shape, 3)
            or original_rgb.dtype != np.float64
            or not np.isfinite(original_rgb).all()
            or np.any((original_rgb < 0) | (original_rgb > 1))):
        raise IntegrityError("Saved design ISP RAW/RGB arrays differ from their case contract")
    try:
        with Image.open(io.BytesIO(_read_blob(service, row["rgb_preview_artifact"]))) as preview:
            pixels = np.asarray(preview.convert("RGB"))
    except (OSError, ValueError) as exc:
        raise IntegrityError("Saved original ISP preview cannot be decoded") from exc
    if not np.array_equal(pixels, (original_rgb * 255).astype(np.uint8)):
        raise IntegrityError("Saved original ISP preview differs from RGB array")
    result_ref = next(
        item for item in source["artifacts"] if item["name"] == "experiment-result.json"
    )
    snapshot = {
        "experiment_id": experiment_id,
        "experiment_request_sha256": source["content_hash"],
        "experiment_result_sha256": result_ref["sha256"],
        "case_id": case_id,
        "row": deepcopy(row),
        "original_profile": original_profile,
        "source_identities_sha256": digest(source["source_identities"]),
    }
    return snapshot, raw, original_rgb


def _request(payload):
    if (not isinstance(payload, dict)
            or set(payload) != {"schema", "name", "experiment_id", "case_id", "profile"}
            or payload["schema"] != SCHEMA):
        raise ValueError("Declare one R1 design case and a complete v1 ISP profile")
    for field in ("name", "experiment_id", "case_id"):
        value = payload[field]
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
            raise ValueError(field + " requires 1..160 characters")
    return {**payload, "name": payload["name"].strip(),
            "profile": isp.validate_profile(payload["profile"])}


def prepare(service, payload):
    from .design_experiments import r1_project_only
    from .imaging import _current

    r1_project_only(service)
    request = _request(payload)
    snapshot, _, _ = source_case(service, request["experiment_id"], request["case_id"])
    # v1's validator only permits fixed WB, 3x3 CCM and transfer tuning in
    # addition to its mandatory, unchanged BLC/demosaic graph.
    return {"name": request["name"], "job_kind": JOB_KIND, "plugin_id": SCHEMA,
            "source": snapshot, "profile": request["profile"],
            "profile_sha256": digest(request["profile"]),
            "profile_source": PROFILE_SOURCE, "environment": _current(service),
            "physical_validation": "not_established"}


def submit(service, payload):
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


class ReplayExecutionError(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.evidence_result = result


def execute(service, job):
    from .imaging import _current

    if job.get("plugin_id") != SCHEMA or job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected worker job is not an R1 design ISP replay")
    if job["environment"] != _current(service):
        raise IntegrityError("Worker differs from frozen design ISP replay environment")
    saved, raw, before = source_case(
        service, job["source"]["experiment_id"], job["source"]["case_id"]
    )
    if canonical(saved) != canonical(job["source"]):
        raise IntegrityError("Original design case differs from frozen ISP replay source")
    profile = isp.validate_profile(job["profile"])
    if digest(profile) != job["profile_sha256"]:
        raise IntegrityError("ISP replay profile differs from its frozen checksum")
    row = saved["row"]
    output = isp.process_raw(raw, row["raw_format"], profile,
                             geometry=row["geometry"])
    if output["summary"]["input_array_bytes_sha256"] != row["raw_array_sha256"]:
        raise IntegrityError("ISP replay did not consume the saved integer RAW")
    after = output["arrays"]["rgb"]
    delta = np.abs(after - before)
    summary = {
        "schema": SCHEMA,
        "source_experiment_id": saved["experiment_id"],
        "source_case_id": saved["case_id"],
        "source_experiment_result_sha256": saved["experiment_result_sha256"],
        "source_raw_sha256": row["raw_artifact"]["sha256"],
        "source_raw_array_sha256": row["raw_array_sha256"],
        "source_original_rgb_sha256": row["rgb_artifact"]["sha256"],
        "source_original_profile_sha256": row["isp_profile_sha256"],
        "profile_sha256": job["profile_sha256"],
        "profile_source": PROFILE_SOURCE,
        "input_unchanged": hashlib.sha256(raw.tobytes(order="C")).hexdigest()
        == row["raw_array_sha256"],
        "new_captures": 0,
        "comparison": {
            "mean_abs_rgb_delta": float(delta.mean()),
            "max_abs_rgb_delta": float(delta.max()),
            "changed_channel_fraction": float(np.count_nonzero(delta) / delta.size),
            "unit": "normalized_sensor_rgb_1",
            "metric_scope": (
                "same saved integer RAW and geometry; "
                "display/colour accuracy not validated"
            ),
        },
        "isp": output["summary"],
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }
    artifacts = [deepcopy(row[key]) for key in (
        "raw_artifact", "rgb_artifact", "rgb_preview_artifact")]
    try:
        stream = io.BytesIO()
        np.save(stream, after, allow_pickle=False)
        rgb = service.registry.store_blob(stream.getvalue(), "replay-rgb.npy",
                                          "application/octet-stream")
        rgb.update(shape=list(after.shape), dtype=str(after.dtype), unit="1")
        artifacts.append(rgb)
        summary["replay_rgb_sha256"] = rgb["sha256"]
        summary["replay_rgb_array_sha256"] = hashlib.sha256(
            after.tobytes(order="C")
        ).hexdigest()
        stream = io.BytesIO()
        Image.fromarray((np.clip(after, 0, 1) * 255).astype(np.uint8), mode="RGB").save(
            stream, format="PNG")
        preview = service.registry.store_blob(stream.getvalue(),
                                              "replay-rgb.png", "image/png")
        artifacts.append(preview)
        summary["replay_preview_sha256"] = preview["sha256"]
        artifacts.append(service.registry.store_blob(canonical(summary).encode(),
                                                     "replay-summary.json", "application/json"))
    except Exception as error:
        raise ReplayExecutionError(str(error), {
            "summary": {**summary, "status": "failed"}, "artifacts": artifacts,
        }) from error
    return {"summary": {**summary, "status": "completed"}, "artifacts": artifacts}


def get(service, identity):
    from .design_experiments import development_only

    development_only(service)
    job = service.registry.get_run(identity)
    if job.get("plugin_id") != SCHEMA or job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not an R1 design ISP replay")
    saved, _, before = source_case(
        service, job["source"]["experiment_id"], job["source"]["case_id"]
    )
    if canonical(saved) != canonical(job["source"]):
        raise IntegrityError("Original design case differs from saved ISP replay source")
    if digest(isp.validate_profile(job["profile"])) != job["profile_sha256"]:
        raise IntegrityError("Saved ISP replay profile differs")
    if job["status"] == "succeeded":
        summary = job["result"]["summary"]
        by_name = {a["name"]: a for a in job["artifacts"]}
        if (summary.get("status") != "completed"
                or summary.get("source_raw_array_sha256") != saved["row"]["raw_array_sha256"]
                or summary.get("profile_sha256") != job["profile_sha256"]
                or summary.get("source_raw_sha256") != saved["row"]["raw_artifact"]["sha256"]
                or summary.get("source_original_rgb_sha256")
                != saved["row"]["rgb_artifact"]["sha256"]
                or summary.get("source_original_profile_sha256")
                != saved["row"]["isp_profile_sha256"]
                or summary.get("replay_rgb_sha256")
                != by_name.get("replay-rgb.npy", {}).get("sha256")
                or summary.get("replay_preview_sha256")
                != by_name.get("replay-rgb.png", {}).get("sha256")
                or len(job["artifacts"]) != 6
                or set(by_name) != {saved["row"]["raw_artifact"]["name"],
                                    saved["row"]["rgb_artifact"]["name"],
                                    saved["row"]["rgb_preview_artifact"]["name"],
                                    "replay-rgb.npy", "replay-rgb.png", "replay-summary.json"}):
            raise IntegrityError("Saved design ISP replay result binding differs")
        if _read_blob(service, by_name["replay-summary.json"]) != canonical(
                {k: v for k, v in summary.items() if k != "status"}).encode():
            raise IntegrityError("Saved design ISP replay summary differs")
        rgb = _load_array(service, by_name["replay-rgb.npy"])
        if (rgb.shape != tuple(by_name["replay-rgb.npy"]["shape"])
                or rgb.dtype != np.float64 or not np.isfinite(rgb).all()
                or np.any((rgb < 0) | (rgb > 1))
                or summary.get("replay_rgb_array_sha256")
                != hashlib.sha256(rgb.tobytes(order="C")).hexdigest()):
            raise IntegrityError("Saved design ISP replay RGB differs")
        delta = np.abs(rgb - before)
        expected_comparison = {
            "mean_abs_rgb_delta": float(delta.mean()),
            "max_abs_rgb_delta": float(delta.max()),
            "changed_channel_fraction": float(np.count_nonzero(delta) / delta.size),
            "unit": "normalized_sensor_rgb_1",
            "metric_scope": (
                "same saved integer RAW and geometry; "
                "display/colour accuracy not validated"
            ),
        }
        if canonical(summary.get("comparison")) != canonical(expected_comparison):
            raise IntegrityError("Saved design ISP comparison differs from RGB arrays")
        try:
            with Image.open(io.BytesIO(_read_blob(service, by_name["replay-rgb.png"]))) as image:
                preview = np.asarray(image.convert("RGB"))
        except (OSError, ValueError) as exc:
            raise IntegrityError("Saved design ISP replay preview cannot be decoded") from exc
        if not np.array_equal(preview, (rgb * 255).astype(np.uint8)):
            raise IntegrityError("Saved design ISP replay preview differs from RGB")
    return job


def list_records(service, experiment_id, case_id):
    from .design_experiments import development_only

    development_only(service)
    source_case(service, experiment_id, case_id)
    return [{"id": record["id"], "name": record["name"], "status": record["status"],
             "created_at": record["created_at"], "error": record.get("error"),
             "profile_sha256": record["profile_sha256"]}
            for record in service.registry.list_runs()
            if record.get("job_kind") == JOB_KIND
            and record["source"]["experiment_id"] == experiment_id
            and record["source"]["case_id"] == case_id]


def install_routes(app, service):
    from fastapi.responses import Response

    @app.post("/api/v1/design-isp-replays", status_code=202, tags=["design_experiments"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/design-isp-replays", tags=["design_experiments"])
    def listing(experiment_id: str, case_id: str):
        return list_records(service, experiment_id, case_id)

    @app.get("/api/v1/design-isp-replays/{identity}", tags=["design_experiments"])
    def detail(identity: str):
        return get(service, identity)

    @app.get("/api/v1/design-isp-replays/{identity}/export", tags=["design_experiments"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="design-isp-replay.json"'},
        )

    @app.post("/api/v1/design-isp-replays/{identity}/cancel", tags=["design_experiments"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

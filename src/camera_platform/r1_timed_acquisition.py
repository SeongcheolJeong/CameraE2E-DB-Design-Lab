"""Bounded, source-bound temporal RAW acquisition from installed R1 examples.

The package bundle supplies the optical/pixel/electrical model and every scene
knot. Timing and PWM are declared simulation inputs. This is not a Lens DB job,
measured sensor prediction, or a motion-aware HDR merger.
"""

from __future__ import annotations

import hashlib
import io
import math
from copy import deepcopy

import numpy as np

from . import isp
from .design_experiments import r1_project_only
from .electrical import validate_capture_conditions
from .hdr_runtime import HDRBracketRuntime, default_capture_program
from .imaging import _current
from .r1_design_assets import catalog, load_sources
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_r1_timed_acquisition_v1"
JOB_KIND = "r1_timed_acquisition"
ISP_ID = "r1-fixed-isp-v1"
MAX_KNOTS = 8
MAX_FRAMES = 4
SPEC_FIELDS = {"schema", "name", "model_id", "scene_knots", "interpolation",
               "control_sequence", "waveform", "merge_hdr", "noise_enabled",
               "seed", "acknowledge_simulation_limits"}
LIMITATION = (
    "Registered 16x16, two-wavelength synthetic R1 examples only. Temporal "
    "knots interpolate declared ideal-image radiance after the fixed on-axis "
    "throughput; they do not render geometric motion or establish physical "
    "timing. PWM is an authored ideal waveform, not vendor LFM. HDR fuses only "
    "independent-reset RAW brackets for an exactly declared static scene; no "
    "motion alignment, deghosting, hardware equivalence or product validation."
)


def default_spec(assets: dict) -> dict:
    model = assets["models"][0]["id"]
    scene = assets["scenes"][0]["id"]
    clock = {"clock_id": "r1-simulation-clock", "unit": "ns",
             "reference": "relative", "origin": "declared-frame-zero"}
    return {
        "schema": SCHEMA, "name": "R1 등록 장면 시간 RAW",
        "model_id": model,
        "scene_knots": [
            {"time_ns": 0, "scene_id": scene, "radiance_scale": 1.0},
            {"time_ns": 50_000_000, "scene_id": scene, "radiance_scale": 1.0},
        ],
        "interpolation": "zero_order_hold",
        "control_sequence": {
            "schema": "recorded_control_sequence_v1", "clock": clock,
            "mode": {"shutter": "rolling", "line_time_ns": 10_000,
                     "hidden_top_rows": 0, "readout_basis": "full_sensor_rows"},
            "frames": [
                {"frame_id": "short", "frame_start_ns": 0,
                 "applied": {"applied_at_ns": 0, "exposure_ns": 1_000_000,
                             "gain": 1.0, "isp_model_id": ISP_ID, "command_id": None}},
                {"frame_id": "long", "frame_start_ns": 10_000_000,
                 "applied": {"applied_at_ns": 10_000_000, "exposure_ns": 4_000_000,
                             "gain": 1.0, "isp_model_id": ISP_ID, "command_id": None}},
            ],
        },
        "waveform": None, "merge_hdr": False, "noise_enabled": False,
        "seed": 2601, "acknowledge_simulation_limits": False,
    }


def specification(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != SPEC_FIELDS:
        raise ValueError("Declare exactly the R1 timed acquisition fields")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit R1 simulation-limit acknowledgement required")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Acquisition name requires 1..160 characters")
    if not isinstance(payload["model_id"], str) or len(payload["model_id"]) > 80:
        raise ValueError("Choose one registered R1 model ID")
    knots = payload["scene_knots"]
    if not isinstance(knots, list) or not 2 <= len(knots) <= MAX_KNOTS:
        raise ValueError("Declare 2..8 registered scene knots")
    previous = None
    for knot in knots:
        if (not isinstance(knot, dict)
                or set(knot) != {"time_ns", "scene_id", "radiance_scale"}
                or type(knot["time_ns"]) is not int
                or not 0 <= knot["time_ns"] <= 1_000_000_000
                or (previous is not None and knot["time_ns"] <= previous)
                or not isinstance(knot["scene_id"], str)
                or len(knot["scene_id"]) > 80
                or type(knot["radiance_scale"]) not in (int, float)
                or not math.isfinite(knot["radiance_scale"])
                or not .05 <= knot["radiance_scale"] <= 30):
            raise ValueError(
                "Scene knots need ordered integer ns, registered IDs and finite scales"
            )
        previous = knot["time_ns"]
    if (not isinstance(payload["interpolation"], str)
            or payload["interpolation"] not in {"linear_irradiance", "zero_order_hold"}):
        raise ValueError("Choose declared linear or zero-order scene interpolation")
    control = payload["control_sequence"]
    if (not isinstance(control, dict) or not isinstance(control.get("clock"), dict)
            or not isinstance(control.get("frames"), list)
            or not 1 <= len(control["frames"]) <= MAX_FRAMES):
        raise ValueError("Declare 1..4 recorded frames")
    for frame in control["frames"]:
        applied = frame.get("applied") if isinstance(frame, dict) else None
        if not isinstance(applied, dict) or applied.get("isp_model_id") != ISP_ID:
            raise ValueError("Each frame must use the fixed R1 ISP profile")
        exposure, gain = applied.get("exposure_ns"), applied.get("gain")
        if (type(exposure) is not int or not 50_000 <= exposure <= 20_000_000
                or type(gain) not in (int, float) or not math.isfinite(gain)
                or not .25 <= gain <= 8):
            raise ValueError("R1 frames require 0.05..20 ms exposure and 0.25..8 gain")
    if (type(payload["merge_hdr"]) is not bool
            or type(payload["noise_enabled"]) is not bool
            or type(payload["seed"]) is not int
            or not 0 <= payload["seed"] <= 2**32 - MAX_FRAMES):
        raise ValueError("HDR/noise choices and bounded seed must be explicit")
    if payload["merge_hdr"]:
        if (len(control["frames"]) < 2
                or len({f["applied"]["exposure_ns"] for f in control["frames"]}) < 2):
            raise ValueError("HDR needs at least two distinct independent exposures")
        if len({(k["scene_id"], float(k["radiance_scale"])) for k in knots}) != 1:
            raise ValueError("HDR requires one registered static scene and radiance scale")
    return {**deepcopy(payload), "name": payload["name"].strip(),
            "scene_knots": [{**k, "radiance_scale": float(k["radiance_scale"])} for k in knots]}


def _sources(service, spec):
    scenes = {k["scene_id"] for k in spec["scene_knots"]}
    return {scene: load_sources(service.workspace, [spec["model_id"]], scene)[spec["model_id"]]
            for scene in sorted(scenes)}


def _runtime_request(spec, sources, profile):
    first = sources[spec["scene_knots"][0]["scene_id"]]
    request = first.rate_request()
    request.pop("scene")
    request["scene_sequence"] = {
        "schema": "timed_spectral_scene_v1",
        "clock": deepcopy(spec["control_sequence"]["clock"]),
        "interpolation": spec["interpolation"],
        "knots": [
            {"time_ns": knot["time_ns"],
             "scene": sources[knot["scene_id"]].radiance_request(
                 knot["radiance_scale"], None)["scene"]}
            for knot in spec["scene_knots"]
        ],
    }
    request["control_sequence"] = deepcopy(spec["control_sequence"])
    request["isp_profiles"] = {ISP_ID: deepcopy(profile)}
    request["noise_enabled"] = spec["noise_enabled"]
    request["seed"] = spec["seed"]
    # The HDR runtime reuses the platform temporal parser/integrator, but samples
    # through the registered electrical model. TemporalSceneRuntime's legacy
    # pyisetcam sensor_create path requests an external upstream asset snapshot
    # even with an explicit R1 QE; it is not portable in a fresh installed state.
    program = default_capture_program(request["readout"])
    if canonical(program["electrical_model"]) != canonical(first.electrical_model):
        raise ValueError("R1 electrical model differs from the selected readout")
    program["waveform"] = deepcopy(spec["waveform"])
    if not spec["merge_hdr"]:
        program["hdr_merge"] = None
    request["capture_program"] = program
    runtime = HDRBracketRuntime()
    for frame in spec["control_sequence"]["frames"]:
        validate_capture_conditions(
            first.electrical_model, frame["applied"]["exposure_ns"] / 1e9,
            analog_gain=frame["applied"]["gain"],
            temperature_c=request["temperature_c"],
        )
    issues = runtime.validate_request(request)
    if issues:
        raise ValueError("R1 timed acquisition preflight failed: " + canonical(issues))
    return runtime, request


def prepare(service, payload):
    mode = r1_project_only(service)
    spec = specification(payload)
    sources = _sources(service, spec)
    profile = isp.default_profile()
    runtime, request = _runtime_request(spec, sources, profile)
    return {
        "name": spec["name"], "job_kind": JOB_KIND, "plugin_id": SCHEMA,
        "runtime_plugin_id": runtime.plugin_id, "specification": spec,
        "source_identities": {scene: source.identity() for scene, source in sources.items()},
        "isp_profile": profile, "isp_profile_sha256": digest(profile),
        "project_mode": mode, "environment": _current(service),
        "resources": runtime.estimate_resources(request), "limitation": LIMITATION,
    }


def submit(service, payload):
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _save_array(service, artifacts, name, array, unit):
    value = np.ascontiguousarray(array)
    stream = io.BytesIO()
    np.save(stream, value, allow_pickle=False)
    artifact = service.registry.store_blob(stream.getvalue(), name, "application/octet-stream")
    artifact.update(shape=list(value.shape), dtype=str(value.dtype), unit=unit,
                    array_sha256=hashlib.sha256(value.tobytes()).hexdigest())
    artifacts.append(artifact)
    return artifact


class AcquisitionError(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.evidence_result = result


def execute(service, job):
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("R1 timed acquisition is not running or was cancelled")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen R1 timed sources, timing or executable changed")
    spec = fresh["specification"]
    sources = _sources(service, spec)
    runtime, request = _runtime_request(spec, sources, fresh["isp_profile"])
    output = runtime.execute(request)
    for source in sources.values():
        source.assert_unchanged()
    arrays, runtime_summary = output["arrays"], output["summary"]
    raw = arrays["raw_frames"]
    rgb = arrays["rgb_frames"]
    count = len(spec["control_sequence"]["frames"])
    if (raw.dtype != np.int32 or raw.shape != (count, 16, 16)
            or rgb.dtype != np.float64 or rgb.shape != (count, 16, 16, 3)
            or runtime_summary.get("hdr_merge_applied", False) != spec["merge_hdr"]):
        raise IntegrityError("R1 timed output differs from the bounded RAW/RGB contract")
    artifacts = []
    summary = {
        "schema": SCHEMA, "specification": spec,
        "source_identities": fresh["source_identities"],
        "runtime_plugin_id": runtime.plugin_id,
        "isp_profile_sha256": fresh["isp_profile_sha256"],
        "runtime": runtime_summary, "physical_validation": "not_established",
        "limitation": LIMITATION,
    }
    try:
        summary["raw_artifact"] = _save_array(
            service, artifacts, "original-raw-frames.npy", raw, "DN"
        )
        summary["rgb_frames_artifact"] = _save_array(service, artifacts, "rgb-frames.npy", rgb, "1")
        if spec["waveform"] is not None:
            summary["waveform_row_exposure_ratio_artifact"] = _save_array(
                service, artifacts, "waveform-row-exposure-ratio.npy",
                arrays["waveform_row_exposure_ratio"], "1")
        if spec["merge_hdr"]:
            summary["hdr_rgb_artifact"] = _save_array(
                service, artifacts, "hdr-rgb.npy", arrays["hdr_rgb"], "1")
            summary["hdr_valid_mask_artifact"] = _save_array(
                service, artifacts, "hdr-valid-mask.npy", arrays["hdr_valid_mask"], "boolean")
        summary_artifact = service.registry.store_blob(
            canonical(summary).encode(), "timed-acquisition-result.json", "application/json")
        artifacts.append(summary_artifact)
    except Exception as error:
        summary.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise AcquisitionError(
            str(error), {"summary": summary, "artifacts": artifacts}
        ) from error
    return {"summary": summary, "artifacts": artifacts}


def get(service, identity):
    r1_project_only(service)
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not an R1 timed acquisition")
    if job["status"] == "succeeded":
        result = job["result"]
        summary = result["summary"]
        if (canonical(summary["specification"]) != canonical(job["specification"])
                or canonical(summary["source_identities"]) != canonical(job["source_identities"])
                or summary["isp_profile_sha256"] != job["isp_profile_sha256"]
                or summary["runtime_plugin_id"] != job["runtime_plugin_id"]):
            raise IntegrityError("Saved R1 timed result differs from its frozen inputs")
        expected = {"raw_artifact", "rgb_frames_artifact"}
        if job["specification"]["waveform"] is not None:
            expected.add("waveform_row_exposure_ratio_artifact")
        if job["specification"]["merge_hdr"]:
            expected.update({"hdr_rgb_artifact", "hdr_valid_mask_artifact"})
        if (not expected <= set(summary)
                or any(key in summary for key in {
                    "waveform_row_exposure_ratio_artifact", "hdr_rgb_artifact",
                    "hdr_valid_mask_artifact"} - expected)):
            raise IntegrityError("Saved R1 timed result lacks its required RAW or display arrays")
        recorded = {a["sha256"] for a in result["artifacts"]}
        for key in ("raw_artifact", "rgb_frames_artifact", "hdr_rgb_artifact",
                    "hdr_valid_mask_artifact", "waveform_row_exposure_ratio_artifact"):
            artifact = summary.get(key)
            if artifact is None:
                continue
            if artifact["sha256"] not in recorded:
                raise IntegrityError("Saved R1 timed array lacks its registered artifact")
            array = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
            if (list(array.shape) != artifact["shape"] or str(array.dtype) != artifact["dtype"]
                    or hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
                    != artifact["array_sha256"]):
                raise IntegrityError("Saved R1 timed array differs from its descriptor")
        entries = [a for a in result["artifacts"] if a["name"] == "timed-acquisition-result.json"]
        if (len(entries) != 1
                or service.registry.blob_path(entries[0]["sha256"]).read_bytes()
                != canonical(summary).encode()):
            raise IntegrityError("Saved R1 timed summary differs from its artifact")
    return job


def list_records(service):
    r1_project_only(service)
    return [{"id": r["id"], "name": r["name"], "status": r["status"],
             "created_at": r["created_at"], "error": r.get("error"),
             "runtime_plugin_id": r.get("runtime_plugin_id")}
            for r in service.registry.list_runs() if r.get("job_kind") == JOB_KIND]


def install_routes(app, service):
    @app.get("/api/v1/r1-timed-acquisitions/template", tags=["r1_timed_acquisition"])
    def template():
        mode = r1_project_only(service)
        assets = catalog(service.workspace)
        return {"specification": default_spec(assets), "catalog": assets,
                "project_mode": mode, "maximum_frames": MAX_FRAMES,
                "maximum_knots": MAX_KNOTS, "limitation": LIMITATION}

    @app.post("/api/v1/r1-timed-acquisitions", status_code=202, tags=["r1_timed_acquisition"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/r1-timed-acquisitions", tags=["r1_timed_acquisition"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/r1-timed-acquisitions/{identity}", tags=["r1_timed_acquisition"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/r1-timed-acquisitions/{identity}/cancel", tags=["r1_timed_acquisition"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

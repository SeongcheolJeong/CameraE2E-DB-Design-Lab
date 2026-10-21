"""Chart calibration campaigns, immutable evidence and supervised worker jobs.

This workflow deliberately does not make generic camera validation accept arbitrary
Lab jobs. Its own chart/condition bridge pins the measurement operator and profile.
"""

from __future__ import annotations

import fcntl
import html
import json
import mimetypes
import re
import tempfile
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from fastapi import UploadFile

from .registry import ConflictError, IntegrityError, canonical, digest

ROUTE = "/api/v1/calibration-lab"
JOB_KIND = "calibration_lab"
VALIDATION_KIND = "calibration_lab_validation"
WORKER_TIMEOUT_S = 1800
CAMPAIGNS = "calibration_lab_campaigns"
MEASUREMENTS = "calibration_lab_measurements"
PROFILES = "calibration_lab_profiles"
ACTIVATIONS = "calibration_lab_activations"
MAX_UPLOAD_BYTES = 64 * 1024**2
MAX_METADATA_BYTES = 2 * 1024**2
MAX_OUTPUT_BYTES = 256 * 1024**2


def _json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate metadata JSON key: " + key)
            value[key] = item
        return value

    def finite(value):
        raise ValueError("Nonfinite JSON value: " + value)

    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_METADATA_BYTES:
        raise ValueError("Metadata must be nonempty UTF-8 JSON of at most 2 MiB")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid metadata JSON") from exc


@contextmanager
def _lock(service):
    # Serializes lineage admission and activation events across API processes.
    with (service.registry.directory / "calibration-lab.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _source_code():
    from .service import environment_fingerprint

    return environment_fingerprint()


def _unique_artifacts(artifacts):
    # One typed owner binding per checksum; files may legitimately share bytes.
    return list({a["sha256"]: a for a in artifacts}.values())


def _conditions_match(campaign, measurement):
    expected, actual = campaign["conditions"], measurement["conditions"]
    for key in expected:
        if key == "exposure_s":
            continue  # Exposure is a measured sweep coordinate, never a fitted nuisance.
        if actual.get(key) != expected[key]:
            raise ValueError(f"Measurement condition {key} differs from its campaign")
    if measurement["physical_instance"] != campaign["camera_id"]:
        raise ValueError("Measurement physical_instance differs from campaign camera_id")
    geometry = measurement.get("geometry")
    if (
        geometry
        and geometry.get("full_sensor_shape", campaign["native_shape"]) != campaign["native_shape"]
    ):
        raise ValueError("RAW full sensor shape differs from its campaign")
    observations = measurement["observations"]
    if (
        measurement["chart"] == "geometry"
        and observations.get("mode") == "multiview_planar"
        and observations.get("native_shape") != campaign["native_shape"]
    ):
        raise ValueError("Geometry pixel coordinate raster differs from its campaign")
    raw_format = measurement.get("raw_format")
    if (
        raw_format
        and raw_format.get("bit_depth") != campaign["baseline_profile"]["electronics"]["bit_depth"]
    ):
        raise ValueError("Measured RAW bit depth differs from its camera profile")


def _source_context(service, source):
    if not source:
        return None
    from .calibration_db import source_selection

    return source_selection(service, source)[1]


def create_campaign(service, payload):
    from .calibration_lab_contracts import campaign_spec
    from .calibration_lab_engine import validate_profile

    data = campaign_spec(payload)
    data["baseline_profile"] = validate_profile(data["baseline_profile"])
    for key in ("native_shape", "pixel_pitch_um", "conditions"):
        if data["baseline_profile"][key] != data[key]:
            raise ValueError(f"Campaign {key} differs from its explicit baseline profile")
    from .calibration_db import BRIDGES, campaign_bridge
    from .calibration_db_optics import COLLECTION, campaign_conversion

    bridge = campaign_bridge(service, data)
    optical = campaign_conversion(service, data)
    if (
        bridge
        and optical
        and any(
            bridge["source_context"].get(k) != optical["source_context"].get(k)
            for k in ("run_id", "candidate_id", "condition_id")
        )
    ):
        raise ValueError("Electrical and optical initializations require the same DB source")
    binding = bridge or optical
    data["source_context"] = (
        deepcopy(binding["source_context"])
        if binding
        else _source_context(service, data.get("source"))
    )
    references = []
    if bridge:
        data["db_bridge_sha256"] = bridge["content_hash"]
        references.append((BRIDGES, bridge))
    if optical:
        data["db_optical_profile_sha256"] = optical["content_hash"]
        references.append((COLLECTION, optical))
    if bridge and optical:
        data["source_context"].update(
            role="frozen_db_initializations",
            profile_conversion="electronics_and_optical_psf_initialization",
        )
    if binding:
        data["source"] = {
            k: binding["source_context"][k]
            for k in ("run_id", "candidate_id", "condition_id")
            if binding["source_context"][k] is not None
        }
    data["schema"] = "camera_calibration_campaign_v1"
    data["physical_validation"] = "not_established"
    if references:
        return service.registry.put_verified_record(CAMPAIGNS, data, references)
    return service.registry.put(CAMPAIGNS, data)


def _lineage(measurement):
    camera = measurement["physical_instance"]
    tokens = {
        "session:" + camera + ":" + measurement["session_id"],
        "acquisition:" + camera + ":" + measurement["acquisition_id"],
        "observations:"
        + digest(
            {
                "chart": measurement["chart"],
                "observations": measurement["observations"],
            }
        ),
    }
    for parent in measurement.get("parent_acquisition_ids", []):
        tokens.add("acquisition:" + camera + ":" + parent)
    if measurement.get("acquisition_group_id"):
        tokens.add("acquisition_group:" + camera + ":" + measurement["acquisition_group_id"])
    for checksum in measurement.get("source_frame_hashes", []):
        tokens.add("raw_frame:" + checksum)
    provenance = measurement.get("provenance", {})
    for checksum in provenance.get("source_frame_hashes", []):
        tokens.add("raw_frame:" + checksum)
    for item in measurement.get("artifacts", []):
        if item.get("name") != "measurement-metadata.json":
            tokens.add("source_bytes:" + item["sha256"])
    return tokens


def import_measurement(service, metadata, original=None, filename=None, *, metadata_bytes=None):
    from .calibration_lab_contracts import import_measurement as parse_file

    metadata_bytes = metadata_bytes or canonical(metadata).encode()
    if _json(metadata_bytes) != metadata:
        raise ValueError("Metadata bytes differ from supplied metadata")
    campaign = service.registry.get(CAMPAIGNS, metadata.get("campaign_id", ""))
    if original is None:
        data = parse_file(metadata, metadata_bytes, "measurement.json")
    else:
        if not isinstance(original, bytes) or not 0 < len(original) <= MAX_UPLOAD_BYTES:
            raise ValueError("Original measurement file must be nonempty and at most 64 MiB")
        if not filename or Path(filename).name != filename or len(filename) > 180:
            raise ValueError("Measurement filename must be a plain filename")
        data = parse_file(metadata, original, filename)
    _conditions_match(campaign, data)
    artifacts = [
        service.registry.store_blob(metadata_bytes, "measurement-metadata.json", "application/json")
    ]
    if original is not None:
        artifacts.append(
            service.registry.store_blob(
                original, filename, mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
        )
    data = {
        **data,
        "artifacts": _unique_artifacts(artifacts),
        "schema": "camera_calibration_measurement_v1",
    }
    data["lineage_tokens"] = sorted(_lineage(data))
    data["campaign_sha256"] = campaign["content_hash"]
    with _lock(service):
        for old in service.registry.list(MEASUREMENTS):
            overlap = set(data["lineage_tokens"]) & set(old["lineage_tokens"])
            if overlap and old["role"] != data["role"]:
                raise ConflictError(
                    "Training/development/final-validation must use independent sessions and "
                    "acquisitions; matching bytes, observations or declared lineage "
                    "cannot change roles"
                )
            if (
                old["campaign_id"] == data["campaign_id"]
                and old["chart"] == data["chart"]
                and old["acquisition_id"] == data["acquisition_id"]
                and old["observations"] == data["observations"]
            ):
                raise ConflictError("This acquisition and chart observation is already registered")
        return service.registry.put_verified_record(MEASUREMENTS, data, [(CAMPAIGNS, campaign)])


def _measurement(service, identity):
    record = service.registry.get(MEASUREMENTS, identity)
    for artifact in record["artifacts"]:
        binding = service.registry.artifact_binding(
            artifact["sha256"], owner_kind=MEASUREMENTS, owner_id=identity
        )
        # artifact_binding validates the typed owner and blob bytes on access.
        if binding["path"].stat().st_size != artifact["bytes"]:
            raise IntegrityError("Measurement original byte size changed")
    return record


def _active_profile(service, campaign_id):
    events = [r for r in service.registry.list(ACTIVATIONS) if r["campaign_id"] == campaign_id]
    if not events:
        return None
    event = max(events, key=lambda r: (r["created_at"], r["id"]))
    if event["profile_id"] is None:
        return None
    return service.registry.get(PROFILES, event["profile_id"])


def campaign_detail(service, identity):
    from . import calibration_db, calibration_db_optics

    campaign = service.registry.get(CAMPAIGNS, identity)
    runs = service.registry.list_runs(
        job_kinds={JOB_KIND, VALIDATION_KIND, calibration_db.JOB_KIND}
    )
    return {
        "campaign": campaign,
        "measurements": [
            r for r in service.registry.list(MEASUREMENTS) if r["campaign_id"] == identity
        ],
        "studies": [
            r
            for r in runs
            if r.get("job_kind") in {JOB_KIND, VALIDATION_KIND}
            and r.get("specification", {}).get("campaign_id") == identity
        ],
        "profiles": [r for r in service.registry.list(PROFILES) if r["campaign_id"] == identity],
        "activations": [
            r for r in service.registry.list(ACTIVATIONS) if r["campaign_id"] == identity
        ],
        "active_profile": _active_profile(service, identity),
        "completion_reports": [
            r for r in service.registry.list("calibration_completion_reports")
            if r["campaign_id"] == identity
        ],
        "db_bridge": calibration_db.get_bridge(service, campaign["db_bridge_id"])
        if campaign.get("db_bridge_id")
        else None,
        "db_optical_profile": calibration_db_optics.get(service, campaign["db_optical_profile_id"])
        if campaign.get("db_optical_profile_id")
        else None,
        "db_replays": [
            r
            for r in runs
            if r.get("job_kind") == calibration_db.JOB_KIND
            and r.get("specification", {}).get("campaign_id") == identity
        ],
    }


def _resolve_measurements(service, campaign, ids, roles):
    from .calibration_lab_contracts import measurement_resource_budget, study_measurement_ids

    ids = study_measurement_ids(ids)
    measurements = [_measurement(service, identity) for identity in ids]
    for item in measurements:
        if item["campaign_id"] != campaign["id"] or item["role"] not in roles:
            raise ValueError("Measurement campaign or dataset role is incompatible with this job")
        _conditions_match(campaign, item)
        if item.get("missing_observations") or item.get("quality", {}).get("not_fit_reasons"):
            raise ValueError(
                "Measurement is retained but not eligible for fitting/comparison: "
                + item["id"]
                + " "
                + str(item.get("missing_observations", []))
                + " "
                + str(item.get("quality", {}).get("not_fit_reasons", []))
            )
    if len({item["source_kind"] for item in measurements}) != 1:
        raise ValueError("Synthetic and measured observations cannot be pooled")
    measurement_resource_budget(measurements)
    return measurements


def submit(service, payload):
    from .calibration_lab_contracts import measurement_resource_budget, study_spec

    if service.loaded_environment != _source_code():
        raise ConflictError(
            "Executable source changed after startup; restart the API before submitting"
        )
    spec = study_spec(payload)
    campaign = service.registry.get(CAMPAIGNS, spec["campaign_id"])
    measurements = _resolve_measurements(
        service, campaign, spec["measurement_ids"], {"training", "development"}
    )
    if not any(m["role"] == "training" for m in measurements):
        raise ValueError("At least one training measurement is required")
    from .calibration_esfr_inputs import development_snapshot
    esfr_inputs = (development_snapshot(service, measurements)
                   if spec.get("fit_method") == "esfr_forward_observer_v1" else None)
    if spec.get("fit_method") == "esfr_forward_observer_v1" and esfr_inputs is None:
        raise ValueError("E-SFR forward fitting requires native E-SFR development captures")
    with _lock(service):
        base_id = spec.get("base_profile_id")
        base = (
            None
            if base_id == "baseline"
            else service.registry.get(PROFILES, base_id)
            if base_id
            else _active_profile(service, campaign["id"])
        )
        if base and base["campaign_id"] != campaign["id"]:
            raise ValueError("Base profile belongs to another camera campaign")
        document = {
            "name": spec.get("name", "Calibration study"),
            "job_kind": JOB_KIND,
            "plugin_id": "calibrated_chart_capture_v1",
            "specification": spec,
            "campaign_snapshot": campaign,
            "measurement_snapshots": measurements,
            "measurement_resource_budget": measurement_resource_budget(measurements),
            "base_profile_snapshot": base,
            "base_profile": deepcopy(base["profile"] if base else campaign["baseline_profile"]),
            "development_lineage": sorted(
                set().union(
                    *(set(m["lineage_tokens"]) for m in measurements),
                    set(base.get("development_lineage", []) if base else []),
                )
            ),
            "source_code": _source_code(),
            "input_artifacts": _unique_artifacts([a for m in measurements for a in m["artifacts"]]),
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        }
        if esfr_inputs is not None:
            document["esfr_development_snapshot"] = esfr_inputs
            document["input_artifacts"] = _unique_artifacts(
                document["input_artifacts"] + esfr_inputs["input_artifacts"]
            )
        # Explicit run means a fresh capture. The frozen request hash still identifies inputs.
        return service.registry.submit(document, digest(document), force=True)


def _directory(service, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"run-[0-9a-f]{32}", identity):
        raise ValueError("Invalid calibration study ID")
    return service.registry.directory / "calibration-lab" / identity


def get(service, identity):
    run = service.registry.get_run(identity)
    if run.get("job_kind") not in {JOB_KIND, VALIDATION_KIND}:
        raise ValueError("Selected job is not a calibration study")
    if run["status"] == "succeeded":
        summary_artifact = run["result"]["artifacts"][0]
        binding = service.registry.artifact_binding(
            summary_artifact["sha256"], owner_kind="runs", owner_id=identity
        )
        if json.loads(binding["path"].read_bytes()) != run["result"]["summary"]:
            raise IntegrityError("Calibration study summary differs from its saved artifact")
    if run["status"] in {"queued", "running"}:
        progress = _directory(service, identity) / "progress.json"
        if progress.is_file() and not progress.is_symlink():
            try:
                run["progress"] = json.loads(progress.read_text())
            except (ValueError, OSError):
                pass
    return run


def operational_progress(service, identity, expected_request_hash):
    """Poll a previously identified Lab job without reloading its evidence graph.

    The first request verifies the complete job and caches only its routing
    identity. Subsequent calls read mutable state from SQLite, bound to that
    immutable request hash. This response is never an evidence verification.
    """
    _directory(service, identity)  # Validate the identifier before any path use.
    if not isinstance(expected_request_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_request_hash
    ):
        raise ValueError("Progress polling requires the known study request hash")
    state = service.registry.get_run_state(identity, expected_request_hash)
    bindings = getattr(service, "_calibration_progress_bindings", None)
    if bindings is None:
        bindings = {}
        service._calibration_progress_bindings = bindings
    key = (identity, expected_request_hash)
    if key not in bindings:
        verified = get(service, identity)
        if verified["content_hash"] != expected_request_hash:
            raise IntegrityError("Calibration progress request hash changed during identification")
        if len(bindings) >= 256:
            bindings.pop(next(iter(bindings)))
        bindings[key] = {
            "job_kind": verified["job_kind"],
            "campaign_id": verified["specification"]["campaign_id"],
            "parent_study_id": verified.get("parent_study_id"),
        }
        # Include state changes that occurred during the one-time verification.
        state = service.registry.get_run_state(identity, expected_request_hash)
    progress = None
    path = _directory(service, identity) / "progress.json"
    if path.is_file() and not path.is_symlink() and path.stat().st_size <= 64 * 1024:
        try:
            progress = json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    return {**state, **bindings[key], "progress": progress}


def validate(service, identity, payload):
    from .calibration_lab_contracts import measurement_resource_budget, study_measurement_ids

    current_source = _source_code()
    if service.loaded_environment != current_source:
        raise ConflictError(
            "Executable source changed after startup; restart the API before validating"
        )
    if set(payload) != {"measurement_ids"}:
        raise ValueError("Validation requires measurement_ids only; the candidate is frozen")
    payload = {"measurement_ids": study_measurement_ids(payload["measurement_ids"])}
    parent = get(service, identity)
    if parent["job_kind"] != JOB_KIND or parent["status"] != "succeeded":
        raise ValueError("Independent validation requires a completed calibration candidate")
    campaign = parent["campaign_snapshot"]
    measurements = _resolve_measurements(
        service, campaign, payload["measurement_ids"], {"final_validation"}
    )
    lineage = set().union(*(set(m["lineage_tokens"]) for m in measurements))
    if lineage & set(parent["development_lineage"]):
        raise ValueError("Final validation overlaps candidate development lineage")
    summary = parent["result"]["summary"]
    with _lock(service):
        retry_of, reusable = [], []
        for old in service.registry.list_runs(job_kinds={VALIDATION_KIND}):
            if old.get("job_kind") != VALIDATION_KIND:
                continue
            if lineage & set(old["validation_lineage"]):
                if (
                    old["parent_study_id"] == identity
                    and old["specification"]["measurement_ids"] == payload["measurement_ids"]
                    and old["status"] != "cancelled"
                ):
                    if (
                        old["status"] == "failed"
                        and old.get("source_code") is not None
                        and old["source_code"] != current_source
                    ):
                        # A failed output/publication attempt may be replayed by
                        # repaired code, but only with this exact frozen parent
                        # and exact holdout IDs. No different candidate gets a
                        # second look at these acquisitions.
                        retry_of.append(old["id"])
                    else:
                        reusable.append(old["id"])
                    continue
                raise ConflictError(
                    "This final-validation acquisition was already exposed for another "
                    "candidate; acquire a new independent session"
                )
        # Inspect all overlapping records before returning an idempotent run;
        # an unrelated candidate exposure must never be hidden by list order.
        if reusable:
            return get(service, reusable[0])
        document = {
            "name": "Independent validation · " + parent["name"],
            "job_kind": VALIDATION_KIND,
            "plugin_id": "calibrated_chart_capture_v1",
            "specification": {**parent["specification"], **payload},
            "campaign_snapshot": campaign,
            "measurement_snapshots": measurements,
            "measurement_resource_budget": measurement_resource_budget(measurements),
            "base_profile": parent["base_profile"],
            "candidate_profile": summary["candidate_profile"],
            "candidate_sha256": summary["candidate_sha256"],
            "parent_study_id": identity,
            "parent_result_sha256": digest(parent["result"]),
            "development_lineage": parent["development_lineage"],
            "validation_lineage": sorted(lineage),
            "source_code": current_source,
            "input_artifacts": _unique_artifacts([a for m in measurements for a in m["artifacts"]]),
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        }
        if retry_of:
            document["retry_of_validation_ids"] = sorted(retry_of)
            document["retry_reason"] = "failed_attempt_replayed_after_source_revision_same_frozen_candidate"
        return service.registry.submit(document, digest(document))


def _verdict(summary, measurements, thresholds, *, independent):
    checks = []
    for comparison in summary.get("comparisons", []):
        metric = comparison.get("metric_id", comparison.get("chart", ""))
        threshold = thresholds.get(metric, thresholds.get(comparison.get("chart", "")))
        if isinstance(threshold, dict):
            threshold = threshold.get("max_rmse")
        residuals = comparison.get("residuals", {})
        error = residuals.get("candidate_rmse", comparison.get("candidate_rmse"))
        status = (
            "not_evaluated"
            if threshold is None or error is None
            else ("pass" if error <= threshold else "fail")
        )
        checks.append(
            {
                "measurement_id": comparison.get("measurement_id"),
                "metric_id": metric,
                "rmse": error,
                "max_rmse": threshold,
                "status": status,
            }
        )
    covered = {c["measurement_id"] for c in checks}
    all_covered = all(m["id"] in covered for m in measurements)
    passed = bool(checks) and all_covered and all(c["status"] == "pass" for c in checks)
    physical = all(m["source_kind"] == "measured" for m in measurements)
    uncertainty_known = all(
        m.get("uncertainty", {}).get("kind") not in {None, "unknown"} for m in measurements
    )
    issues = summary.get("issues", [])
    qualified = not any(isinstance(i, dict) and i.get("severity") == "error" for i in issues)
    # A declaration about measurement uncertainty does not bound prediction/model
    # error. This workflow reports chart agreement, not a new physical release gate.
    prediction_uncertainty_known = all(
        row.get("prediction_uncertainty", {}).get("kind")
        in {"absolute_bound", "standard_uncertainty"}
        and row.get("observation_operator_equivalent") is True
        for row in summary.get("comparisons", [])
    ) and bool(checks)
    return {
        "independent": independent,
        "status": (
            "pass"
            if passed and qualified
            else "fail"
            if any(c["status"] == "fail" for c in checks)
            else "inconclusive"
        ),
        "checks": checks,
        "all_measurements_evaluated": all_covered,
        "measurement_uncertainty_recorded": uncertainty_known,
        "prediction_uncertainty_recorded": prediction_uncertainty_known,
        "numerical_tolerance_pass": passed and qualified,
        "physical_data": physical,
        "physical_validation": "not_established",
        "scope": "registered chart observations and frozen camera conditions only",
        "limitations": [
            "Declared instrument metadata is retained; equipment authentication is external.",
            "A numerical tolerance pass does not identify omitted physical mechanisms.",
            "Physical validation requires propagated measurement/prediction uncertainty and "
            "qualified observation equivalence; this report is not that release gate.",
        ],
    }


def _report(summary):
    encoded = html.escape(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return (
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>Calibration evidence</title>'
        "<style>body{max-width:1100px;margin:40px auto;padding:20px;font:16px system-ui;"
        "background:#f7f9fb;color:#182a3a}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "background:white;padding:24px;border:1px solid #dce3ea}h1{font-size:28px}</style>"
        "<h1>CameraE2E Calibration &amp; Validation</h1>"
        "<p>실측 / 기준 / 후보 · 원본, 조건, 프로파일, 데이터 역할을 고정한 실행 근거</p>"
        "<pre>" + encoded + "</pre></html>"
    ).encode()


def execute(service, job):
    from . import calibration_lab_engine as engine

    if get(service, job["id"])["status"] != "running":
        raise ValueError("A claimed running calibration job is required")
    if job["source_code"] != _source_code() or service.loaded_environment != job["source_code"]:
        raise IntegrityError("Calibration code/environment changed after submission; submit again")
    campaign = service.registry.get(CAMPAIGNS, job["campaign_snapshot"]["id"])
    if campaign != job["campaign_snapshot"]:
        raise IntegrityError("Campaign snapshot changed")
    for frozen in job["measurement_snapshots"]:
        if _measurement(service, frozen["id"]) != frozen:
            raise IntegrityError("Measurement snapshot changed")
    directory = _directory(service, job["id"])
    directory.mkdir(parents=True, exist_ok=False)

    def progress(message, *_args):
        state = service.registry.get_run_state(job["id"], job["content_hash"])
        if state["status"] != "running":
            raise RuntimeError("Calibration job cancelled")
        value = message if isinstance(message, dict) else {"message": str(message)}
        (directory / "progress.json").write_text(canonical(value))

    independent = job["job_kind"] == VALIDATION_KIND
    if independent:
        parent = get(service, job["parent_study_id"])
        if digest(parent["result"]) != job["parent_result_sha256"]:
            raise IntegrityError("Frozen candidate study changed")
        candidate = deepcopy(job["candidate_profile"])
        fit = {
            "profile": candidate,
            "diagnostics": [],
            "changed_parameters": [],
            "status": "frozen_candidate_no_refit",
        }
    else:
        progress("차트별 파라미터 추정 · 학습 데이터만 사용")
        fit = engine.fit_profile(
            job["base_profile"],
            [m for m in job["measurement_snapshots"] if m["role"] == "training"],
            max_iterations=job["specification"]["max_iterations"],
            allow_sfr_overshoot=bool(job.get("esfr_development_snapshot")) and job["specification"].get("fit_method") == "esfr_forward_observer_v1",
        )
        if job.get("esfr_development_snapshot"):
            from .calibration_esfr_forward import refine_profile
            from .calibration_esfr_inputs import bound_development_captures
            inputs = job["esfr_development_snapshot"]
            progress("112개 native RAW edge · 개발 영상의 동일 관측 연산자로 보정")
            refined = refine_profile(
                fit["profile"], job["measurement_snapshots"],
                bound_development_captures(service, inputs), inputs["rois"],
                max_iterations=job["specification"]["max_iterations"], on_progress=progress,
            )
            fit["analytic_initialization"] = {
                "profile_sha256": digest(fit["profile"]), "diagnostics": fit["diagnostics"],
                "purpose": "development-only initialization; final objective uses native images",
            }
            fit["profile"] = refined["profile"]
            fit["diagnostics"] = refined["diagnostics"]
            fit["changed_parameters"] = [p for p in engine._changes(job["base_profile"], refined["profile"])
                                         if p["parameter"] != "esfr_forward_model"]
            fit["forward_observer"] = {**refined["forward_observer"],
                "source_study_id": inputs["source_study_id"],
                "candidate_sha256": digest(refined["profile"])}
        candidate = fit["profile"]
    progress("고정된 기준·후보 프로파일로 새 차트 계산")
    summary = engine.run_comparison(
        job["base_profile"],
        candidate,
        job["measurement_snapshots"],
        directory,
        seed=job["specification"]["seed"],
        on_progress=progress,
    )
    if independent:
        from . import calibration_esfr_replay

        progress("112개 native RAW·ISP 재실행 근거 확인")
        supplemental = calibration_esfr_replay.execute(service, job, summary, directory, progress)
        if supplemental:
            summary["esfr_image_validation"] = supplemental
    summary.update(
        {
            "schema": "camera_calibration_study_result_v1",
            "run_id": job["id"],
            "campaign_id": campaign["id"],
            "job_kind": job["job_kind"],
            "baseline_profile": job["base_profile"],
            "candidate_profile": candidate,
            "candidate_sha256": digest(candidate),
            "fit": fit,
            "parameters": fit.get("changed_parameters", []),
            "development_lineage": job["development_lineage"],
            "evidence_kind": "measured_comparison"
            if all(m["source_kind"] == "measured" for m in job["measurement_snapshots"])
            else "implementation_verification",
            "files_base": f"{ROUTE}/studies/{job['id']}/files/",
            "report_url": f"{ROUTE}/studies/{job['id']}/files/report.html",
            "fresh_simulation": True,
            "measurement_resource_budget": job.get("measurement_resource_budget"),
        }
    )
    summary["validation"] = _verdict(
        summary,
        job["measurement_snapshots"],
        campaign.get("thresholds", {}),
        independent=independent,
    )
    summary["physical_validation"] = summary["validation"]["physical_validation"]
    (directory / "request.json").write_text(canonical(job))
    (directory / "candidate-profile.json").write_text(canonical(candidate))
    (directory / "report.html").write_bytes(_report(summary))
    # Operational progress checks deliberately do not verify evidence. Re-read
    # the full frozen job before publishing any result artifacts; registry.finish
    # performs its own full request verification and state CAS as well.
    final_owner = get(service, job["id"])
    if final_owner["content_hash"] != job["content_hash"]:
        raise IntegrityError("Calibration request changed before result publication")
    if final_owner["status"] != "running":
        raise RuntimeError("Calibration job cancelled before result publication")
    artifacts, files = [], {}
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise IntegrityError("Calibration output must not contain symlinks")
        if not path.is_file() or path.name == "progress.json":
            continue
        total += path.stat().st_size
        if total > MAX_OUTPUT_BYTES:
            raise ValueError("Calibration output exceeds the 256 MiB artifact budget")
        relative = path.relative_to(directory).as_posix()
        artifact = service.registry.store_file(
            path, relative, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        artifacts.append(artifact)
        files[relative] = artifact["sha256"]
    summary["files"] = files
    summary["source_code"] = job["source_code"]
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "calibration-result.json", "application/json"
    )
    progress("완료 · 원본과 새 실행 근거 저장")
    input_checksums = {a["sha256"] for a in job["input_artifacts"]}
    artifacts = [a for a in _unique_artifacts(artifacts) if a["sha256"] not in input_checksums]
    return {"summary": summary, "artifacts": [result_artifact, *artifacts]}


def apply(service, identity, payload):
    run = get(service, identity)
    if run["job_kind"] != JOB_KIND or run["status"] != "succeeded":
        raise ValueError("Only completed calibration candidates can be selected")
    campaign_id = run["specification"]["campaign_id"]
    if set(payload) - {"campaign_id", "activate"} or payload.get("campaign_id", campaign_id) != campaign_id:
        raise ValueError("Candidate campaign does not match")
    if type(payload.get("activate", True)) is not bool:
        raise ValueError("activate must be boolean")
    summary = run["result"]["summary"]
    fit = summary["fit"]
    if fit.get("status") in {"refused", "failed", "REFUSED"}:
        raise ValueError("The fitter refused this candidate; correct the measurement inputs")
    if any(
        row.get("status") in {"not_converged", "refused", "failed"}
        for row in fit.get("diagnostics", [])
    ):
        raise ValueError("A required fit did not converge; this candidate cannot be applied")
    profile_id = (
        "calibration-profile-"
        + digest({"study_id": identity, "candidate": summary["candidate_sha256"]})[:32]
    )
    with _lock(service):
        try:
            profile = service.registry.get(PROFILES, profile_id)
        except KeyError:
            profile = service.registry.put(
                PROFILES,
                {
                    "schema": "camera_calibration_profile_revision_v1",
                    "campaign_id": campaign_id,
                    "source_study_id": identity,
                    "source_result_sha256": digest(run["result"]),
                    "parent_profile_id": (run.get("base_profile_snapshot") or {}).get("id"),
                    "profile": summary["candidate_profile"],
                    "profile_sha256": summary["candidate_sha256"],
                    "development_lineage": run["development_lineage"],
                    "physical_validation": "see_frozen_candidate_validation_reports",
                    "runtime": "calibrated_chart_capture_v1",
                    "selection_status": "explicit_application" if payload.get("activate", True) else "saved_without_activation",
                    "db_design_use": "explicit_electronics_only_db_replay"
                    if run["campaign_snapshot"].get("db_bridge_id")
                    else "explicit_calibration_lab_rerun_only",
                    "db_bridge_id": run["campaign_snapshot"].get("db_bridge_id"),
                    "db_bridge_sha256": run["campaign_snapshot"].get("db_bridge_sha256"),
                    "db_optical_profile_id": run["campaign_snapshot"].get("db_optical_profile_id"),
                    "db_optical_profile_sha256": run["campaign_snapshot"].get(
                        "db_optical_profile_sha256"
                    ),
                },
                identity=profile_id,
            )
        activation = _activate(service, campaign_id, profile_id) if payload.get("activate", True) else None
        return {"profile": profile, "activation": activation}


def _activate(service, campaign_id, profile_id):
    service.registry.get(CAMPAIGNS, campaign_id)
    if profile_id is not None:
        profile = service.registry.get(PROFILES, profile_id)
        if profile["campaign_id"] != campaign_id:
            raise ValueError("Cannot activate a profile from another camera campaign")
    previous = _active_profile(service, campaign_id)
    return service.registry.put(
        ACTIVATIONS,
        {
            "schema": "camera_calibration_activation_v1",
            "campaign_id": campaign_id,
            "profile_id": profile_id,
            "previous_profile_id": previous["id"] if previous else None,
            "scope": "future_calibrated_chart_capture_jobs_only",
        },
    )


def activate(service, campaign_id, payload):
    if set(payload) != {"profile_id"}:
        raise ValueError("Activation requires profile_id (null restores the baseline)")
    with _lock(service):
        return _activate(service, campaign_id, payload["profile_id"])


def _file(service, identity, relative):
    run = get(service, identity)
    if run["status"] != "succeeded":
        raise ValueError("Files are available only for a completed calibration study")
    if relative not in run["result"]["summary"]["files"]:
        raise KeyError("File is not in the saved calibration manifest")
    checksum = run["result"]["summary"]["files"][relative]
    binding = service.registry.artifact_binding(checksum, owner_kind="runs", owner_id=identity)
    return binding["path"]


def bundle(service, identity):
    run = get(service, identity)
    if run["status"] != "succeeded":
        raise ValueError("A completed calibration study is required")
    handle = tempfile.NamedTemporaryFile(suffix=".zip", prefix="camera-calibration-", delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("study.json", canonical(run))
            for relative in run["result"]["summary"]["files"]:
                archive.write(_file(service, identity, relative), "results/" + relative)
            for measurement in run["measurement_snapshots"]:
                archive.writestr(
                    "measurements/" + measurement["id"] + ".json", canonical(measurement)
                )
                for index, artifact in enumerate(measurement["artifacts"]):
                    binding = service.registry.artifact_binding(
                        artifact["sha256"], owner_kind=MEASUREMENTS, owner_id=measurement["id"]
                    )
                    archive.write(
                        binding["path"], f"originals/{measurement['id']}/{index}-{artifact['name']}"
                    )
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def demo(service):
    from .calibration_lab_engine import synthetic_fixture

    fixture = synthetic_fixture()
    campaign = create_campaign(service, fixture["campaign"])
    measurements = [
        import_measurement(
            service, {**{k: v for k, v in m.items() if k != "id"}, "campaign_id": campaign["id"]}
        )
        for m in fixture["measurements"]
    ]
    study = submit(
        service,
        {
            "campaign_id": campaign["id"],
            "measurement_ids": [m["id"] for m in measurements if m["role"] != "final_validation"],
            "name": "합성 데이터 · 주요 차트 보정 구현 검증",
            "seed": 42,
            "max_iterations": 100,
        },
    )
    return {
        "campaign": campaign,
        "measurements": measurements,
        "study": study,
        "job": study,
        "evidence_kind": "implementation_verification",
    }


def install_routes(app, service):
    from fastapi import File, Form
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from .calibration_completion import install_routes as install_completion_routes

    install_completion_routes(app, service)

    from .calibration_color import install_routes as install_color_routes
    from .calibration_db_color import install_routes as install_db_color_routes

    install_db_color_routes(app, service)
    from .calibration_db_core import install_routes as install_core_routes

    install_core_routes(app, service)
    from .calibration_unified import install_routes as install_unified_routes

    install_unified_routes(app, service)

    install_color_routes(app, service)

    from . import calibration_db, calibration_db_optics

    calibration_db.install_routes(app, service)
    calibration_db_optics.install_routes(app, service)

    @app.get(ROUTE + "/template", tags=["calibration_lab"])
    def templates(
        source_run: str | None = None, candidate: str | None = None, condition: str | None = None
    ):
        from .calibration_lab_contracts import template

        result = template()
        if source_run is not None or candidate is not None or condition is not None:
            source = {"run_id": source_run, "candidate_id": candidate}
            if condition is not None:
                source["condition_id"] = condition
            result["source_context"] = _source_context(service, source)
            for key in ("campaign", "campaign_template"):
                if key in result:
                    result[key]["source"] = source
        return result

    @app.get(ROUTE + "/campaigns", tags=["calibration_lab"])
    def campaigns():
        return service.registry.list(CAMPAIGNS)

    @app.post(ROUTE + "/campaigns", status_code=201, tags=["calibration_lab"])
    def campaign_create(payload: dict):
        return create_campaign(service, payload)

    @app.get(ROUTE + "/campaigns/{identity}", tags=["calibration_lab"])
    def campaign_get(identity: str):
        return campaign_detail(service, identity)

    @app.post(ROUTE + "/measurements", status_code=201, tags=["calibration_lab"])
    def measurement_create(payload: dict):
        return import_measurement(service, payload)

    @app.post(ROUTE + "/measurements/import", status_code=201, tags=["calibration_lab"])
    async def measurement_import(metadata: str = Form(...), file: UploadFile = File(...)):  # noqa: B008
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        metadata_bytes = metadata.encode()
        return import_measurement(
            service, _json(metadata_bytes), raw, file.filename, metadata_bytes=metadata_bytes
        )

    @app.post(ROUTE + "/studies", status_code=202, tags=["calibration_lab"])
    def study_create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/studies/{identity}", tags=["calibration_lab"])
    def study_get(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/studies/{identity}/progress", tags=["calibration_lab"])
    def study_progress(identity: str, expected_request_hash: str):
        return operational_progress(service, identity, expected_request_hash)

    @app.post(ROUTE + "/studies/{identity}/cancel", tags=["calibration_lab"])
    def study_cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.post(ROUTE + "/studies/{identity}/validate", status_code=202, tags=["calibration_lab"])
    def study_validate(identity: str, payload: dict):
        return validate(service, identity, payload)

    @app.post(ROUTE + "/studies/{identity}/apply", tags=["calibration_lab"])
    def study_apply(identity: str, payload: dict):
        return apply(service, identity, payload)

    @app.post(ROUTE + "/campaigns/{identity}/activate", tags=["calibration_lab"])
    def profile_activate(identity: str, payload: dict):
        return activate(service, identity, payload)

    @app.get(ROUTE + "/studies/{identity}/files/{relative:path}", tags=["calibration_lab"])
    def study_file(identity: str, relative: str):
        return FileResponse(
            _file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )

    @app.get(ROUTE + "/studies/{identity}/bundle", tags=["calibration_lab"])
    def study_bundle(identity: str):
        path = bundle(service, identity)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=identity + ".zip",
            background=BackgroundTask(path.unlink),
        )

    @app.post(ROUTE + "/demo", status_code=202, tags=["calibration_lab"])
    def synthetic_demo(payload: dict):
        if payload:
            raise ValueError("The synthetic demo takes an empty object")
        return demo(service)

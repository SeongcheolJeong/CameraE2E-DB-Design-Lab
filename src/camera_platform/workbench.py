"""Display projections over verified records; never replacement evidence documents."""

from urllib.parse import quote

from .registry import digest

JOB_KINDS = {
    "runs": None,
    "factory_jobs": "characterization",
    "replays": "isp_replay",
    "isp_study_cells": "isp_study_cell",
    "surrogates": "surrogate_training",
    "scene_renders": "spectral_scene_render",
    "scene_projections": "spectral_scene_projection",
    "readout_fits": "readout_fit",
}
PROJECTED = {
    *JOB_KINDS, "models", "surrogate_predictions", "control_programs", "case_reviews",
    "isp_studies", "isp_selections", "isp_references", "readout_calibrations",
    "readout_observations",
}
JOB_FIELDS = {
    "id",
    "name",
    "status",
    "content_hash",
    "created_at",
    "updated_at",
    "error",
    "job_kind",
    "plugin_id",
    "assembly_id",
    "test_id",
    "scene_id",
    "scene_asset_id",
    "scene_sequence_id",
    "control_sequence_id",
    "control_program_id",
    "capture_program_id",
    "control_policy_id",
    "parameters",
    "asset_id",
    "source_id",
    "source_render_id",
    "source_kind",
    "isp_model_id",
    "study_id",
    "candidate_id",
    "dataset_key",
    "plan_hash",
    "base_calibration_id",
}


def project(collection, record):
    """Input has already passed Registry request, result and audit checks."""
    if collection == "case_reviews" and record.get("schema") == "camera_case_review_unavailable_v1":
        # This is an error summary, not a full evidence record or its projection.
        # In particular, do not label its hash as a validated full-detail hash.
        return dict(record)
    if collection in JOB_KINDS:
        value = {k: v for k, v in record.items() if k in JOB_FIELDS}
        if collection == "readout_fits":
            value["name"] = record["specification"]["name"]
            value["base_calibration_id"] = record["specification"]["base_calibration_id"]
        if collection == "scene_renders":
            value.setdefault("scene_asset_id", record["request"]["scene_asset_id"])
            value.setdefault("assembly_id", record["request"]["assembly_id"])
        result = record.get("result") or {}
        summary = result.get("summary") or result
        value["display_summary"] = {
            k: summary[k]
            for k in ("conditions", "field_radius", "channel_ratios", "theoretical_snr_db")
            if k in summary
        }
        frames = summary.get("frames")
        value["display_summary"]["frame_count"] = (
            len(frames)
            if isinstance(frames, list)
            else frames
            if type(frames) is int and frames >= 0
            else None
        )
        if record.get("plugin_id") == "shared_charge_dcg_v1":
            value["display_summary"].update(
                acquisition_count=summary.get("acquisition_count"),
                readout_count=summary.get("readout_count"), readouts_per_acquisition=2,
            )
        if collection == "surrogates":
            value["display_summary"].update(
                {
                    "numerical_status": result.get("numerical_validation", {}).get("status"),
                    "validation_run_ids": [
                        r["run_id"] for r in result.get("datasets", {}).get("validation", [])
                    ],
                }
            )
    elif collection == "isp_studies":
        value = {k: record[k] for k in (
            "id", "name", "created_at", "content_hash", "plan_hash", "specification",
            "cells", "run_ids",
        ) if k in record}
        value["datasets"] = [{k: d[k] for k in (
            "key", "assembly_id", "test_id", "raw_sha256",
        ) if k in d} for d in record["datasets"]]
        value["display_summary"] = {
            "dataset_count": len(record["datasets"]),
            "candidate_count": len(record["candidates"]),
            "cell_count": len(record["cells"]),
            "phase": record["specification"]["phase"],
        }
    elif collection == "isp_selections":
        value = {k: record[k] for k in (
            "id", "created_at", "content_hash", "study_id", "plan_hash", "candidate_id",
            "profile_sha256", "rationale", "calibration_id", "analysis_sha256", "authority",
        ) if k in record}
    elif collection == "readout_calibrations":
        value = {k: record[k] for k in (
            "id", "name", "revision", "source_kind", "physical_instance", "created_at",
            "content_hash", "sensor_model_id", "readout_mode", "geometry", "pixel_pitch_um",
            "charge_reference", "applicability", "raw_formats", "normalization_e",
            "physical_validation", "approval_status", "uncertainty",
        )}
        if record.get("schema") == "camera_fitted_response_calibration_v1":
            value.update({k: record[k] for k in (
                "schema", "fit_job_id", "candidate_sha256", "evaluation_use",
            )})
    elif collection == "readout_observations":
        value = {k: record[k] for k in (
            "id", "name", "revision", "source_kind", "physical_instance", "created_at",
            "content_hash", "capture_id", "dataset_role", "dataset_ids", "roi",
            "charge_statistics", "charge_reference",
        ) if k in record}
    elif collection == "isp_references":
        value = {k: v for k, v in record.items() if k not in {"artifacts", "payload"}}
    elif collection == "case_reviews":
        value = {
            k: record[k]
            for k in (
                "id", "case_id", "name", "question", "revision", "previous_revision_id",
                "created_at", "content_hash", "assembly_ids", "decision", "authority",
            )
            if k in record
        }
        value["display_summary"] = {
            "requirement_count": len(record.get("requirements", [])),
            "evidence_count": len(record.get("evidence", [])),
            "unresolved_count": len(record.get("unresolved", [])),
            "next_experiment_count": len(record.get("next_experiments", [])),
        }
    elif collection == "control_programs":
        value = {
            k: record[k]
            for k in (
                "id",
                "name",
                "revision",
                "created_at",
                "content_hash",
                "assembly_id",
                "scene_sequence_id",
                "control_policy_id",
                "expanded_program_sha256",
                "approval_status",
                "physical_validation",
            )
            if k in record
        }
        value["display_summary"] = {"frame_count": len(record["payload"]["frames"])}
    elif collection == "models":
        value = {k: v for k, v in record.items() if k != "payload"}
        value["display_summary"] = {"has_psf": bool(record.get("payload", {}).get("psf"))}
        psf = record.get("payload", {}).get("psf")
        if psf:
            value["display_summary"]["psf_spatial_model"] = psf.get("spatial_model")
    elif collection == "surrogate_predictions":
        value = {
            k: record[k]
            for k in (
                "id",
                "created_at",
                "content_hash",
                "model_id",
                "model_result_hash",
                "physical_validation",
                "total_uncertainty",
            )
            if k in record
        }
        value["display_summary"] = {"point_count": len(record["rows"])}
    else:
        raise KeyError("Unknown workbench projection collection")
    value["_projection"] = {
        "schema": "camera_workbench_projection_v1",
        "collection": collection,
        "record_sha256": digest(record),
        "hash_scope": "canonical full detail record, including result and artifacts",
        "detail_path": f"/workbench/{collection}/{quote(record['id'], safe='')}",
    }
    return value


def bootstrap(service):
    data = service.bootstrap()
    for collection in PROJECTED:
        data[collection] = [project(collection, r) for r in data[collection]]
    data["workbench_schema"] = "camera_workbench_v1"
    data["projected_collections"] = sorted(PROJECTED)
    return data


def detail(service, collection, identity):
    if collection not in PROJECTED:
        raise KeyError("Unknown workbench projection collection")
    if collection in JOB_KINDS:
        if collection == "isp_study_cells":
            from .isp_studies import get_cell

            record = get_cell(service, identity)
        elif collection == "readout_fits":
            from .readout_fits import get_job

            record = get_job(service, identity)
        else:
            record = service.registry.get_run(identity)
        kind = record.get("job_kind")
        if (collection == "runs" and kind in set(JOB_KINDS.values()) - {None}) or (
            collection != "runs" and kind != JOB_KINDS[collection]
        ):
            raise KeyError("Job belongs to a different collection")
    elif collection == "case_reviews":
        record = service.get_case_review(identity)
    elif collection == "readout_calibrations":
        from .readout_calibrations import get

        record = get(service, identity)
    elif collection == "readout_observations":
        from .readout_observations import get

        record = get(service, identity)
    elif collection in {"isp_studies", "isp_selections", "isp_references"}:
        from .isp_studies import get_reference, get_selection, get_study

        record = {
            "isp_studies": get_study, "isp_selections": get_selection,
            "isp_references": get_reference,
        }[collection](service, identity)
    else:
        record = service.registry.get(collection, identity)
    return {
        "schema": "camera_workbench_record_v1",
        "collection": collection,
        "record_sha256": digest(record),
        "record": record,
    }

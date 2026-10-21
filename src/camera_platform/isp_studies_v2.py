"""Immutable charge-domain ISP Studies over original nonlinear RAW.

Versioned separately from linear Study semantics. Source selection, calibrated
charge, normalization, masks and covariance ancestry are fixed before tuning.
"""

from __future__ import annotations

import copy
import hashlib
import io
import itertools
from pathlib import Path

import numpy as np

from . import isp_studies as legacy
from . import isp_study_metrics_v2 as metrics
from .contracts import ModelCreate
from .isp_study_contracts_v2 import (
    ISPReferenceImport,
    ISPReferenceImportWithLineage,
    ISPSelectionCreate,
    ISPStudyPlan,
    ISPStudySubmit,
)
from .registry import ConflictError, IntegrityError, canonical, digest

MAX_ARCHIVE_BYTES = legacy.MAX_ARCHIVE_BYTES
HALO = legacy.HALO
AUTHORITY = legacy.AUTHORITY
TERMINAL = legacy.TERMINAL
FORECAST_VERSION = "camera_charge_study_evidence_forecast_v1"
# Runtime checked contracts for generated metadata. Input-dependent copies are
# counted by the replay forecaster; these ceilings never stand in for calibration.
COMPLETION_GRAPH_BYTES_PER_CELL = 1024 * 1024
METRIC_BYTES_LIMIT = 256 * 1024
COMPLETION_GRAPH_NODES_PER_CELL = 2048
# Additional per-cell ownership/analysis structure is fixed-schema, not copied calibration.
OWNERSHIP_ANALYSIS_BYTES_PER_CELL = 64 * 1024

_artifacts = legacy._artifacts
_verify_artifacts = legacy._verify_artifacts
_npy = legacy._npy
_array = legacy._array
_store_array = legacy._store_array
_json_bytes = legacy._json_bytes
_semantic = legacy._semantic
_mask = legacy._mask
_DraftRegistry = legacy._DraftRegistry
_fit_tokens = legacy._fit_tokens
get_study = legacy.get_study
get_cell = legacy.get_cell
get_selection = legacy.get_selection
_reference_geometry = legacy._reference_geometry


def _is_capture(source):
    return source["source_kind"] == "capture"


def _lineage(service, source):
    if not _is_capture(source):
        return legacy._lineage(service, source)
    lineage = source["acquisition_lineage"]
    if lineage["schema"] != "camera_capture_lineage_v1":
        raise ValueError("Study requires explicit imported capture exposure lineage")
    return sorted(set(lineage["exposure_tokens"]))


def _capture_fit_tokens(service, closure):
    """Expand exposure aliases without joining captures through shared dark files."""
    records = closure["calibration_lineage"]
    tokens = set(closure["decoder_fitting_tokens"])
    tokens.update(closure.get("decoder_evaluation_tokens", ()))
    tokens.update(token.removeprefix("dataset-id:") for token in list(tokens)
                  if token.startswith("dataset-id:"))
    tokens.update("dataset-id:" + v for record in records for v in record["dataset_ids"])
    tokens.update(v for record in records for v in record["dataset_ids"])
    tokens.update(
        ref["sha256"]
        for record in records
        for ref in record["evidence_refs"]
        if "fit" in ref["role"].lower()
    )
    groups = []
    for capture in service.registry.list("captures"):
        group = {capture["id"], "capture-id:" + capture["id"]}
        for value in capture.get("dataset_ids", []):
            group.update((value, "dataset-id:" + value))
        group.update(capture.get("exposure_tokens", capture.get("exposure_identity_tokens", [])))
        group.update("raw-frame-sha256:" + h for h in capture.get("raw_frame_hashes", []))
        # Only original RAW is an exposure identity. Calibration dark and metadata
        # may be shared by truly different acquisitions and must not join them.
        group.update(
            a["sha256"] for a in capture.get("artifacts", [])
            if a["name"] in {"raw-capture.npy", "raw_frames.npy", "raw_groups.npy"}
        )
        groups.append(group)
    for measurement in service.registry.list("measurements"):
        group = {measurement["id"], *measurement.get("dataset_ids", [])}
        group.update("dataset-id:" + v for v in measurement.get("dataset_ids", []))
        if measurement.get("raw_capture_id"):
            value = measurement["raw_capture_id"]
            group.update((value, "capture-id:" + value))
        groups.append(group)
    changed = True
    while changed:
        changed = False
        for group in groups:
            if tokens & group and not group <= tokens:
                tokens.update(group)
                changed = True
    return tokens


def _dataset_tokens(dataset):
    return {
        *dataset["acquisition_lineage_ids"],
        dataset["raw_sha256"],
        dataset["specification"]["acquisition_group_id"],
        "acquisition-group-id:" + dataset["specification"]["acquisition_group_id"],
        *("raw-frame-sha256:" + h for h in dataset["raw_frame_sha256"]),
    }


def _overlap(left, right):
    if legacy._overlap(left, right):
        return True
    if any(value.get("source", {}).get("source_kind") == "capture" for value in (left, right)):
        # A selected simulation path may be imported in a reencoded paired
        # container and then selected through its sibling. Compare the run's
        # retained selected-frame identities with *all* imported path frames.
        return bool(_dataset_tokens(left) & _dataset_tokens(right))
    return False


def _reference_contract(value):
    return ISPReferenceImportWithLineage if "acquisition_lineage" in value else ISPReferenceImport


def _reference_sources(service, data):
    from .isp_charge_source import resolve

    sources = [resolve(service, item["source_kind"], item["source_id"], item["selector"])
               for item in data["acquisition_lineage"]["sources"]]
    if data["derivation"] == "independent_measurement" and any(
        source["source_provenance"]["source_kind"] != "measured" for source in sources
    ):
        raise ValueError("Measured reference cannot promote simulated acquisition ancestry")
    return sources


def _reference_exposure_tokens(service, reference):
    tokens = {
        reference["acquisition_group_id"],
        "acquisition-group-id:" + reference["acquisition_group_id"],
    }
    lineage = reference.get("acquisition_lineage")
    if lineage is None:
        raise ValueError("Capture Study references require explicit versioned acquisition lineage")
    tokens.update("dataset-id:" + value for value in lineage["dataset_ids"])
    tokens.update(lineage["dataset_ids"])
    for source in reference["source_lineage_snapshots"]:
        tokens.update(_lineage(service, source))
        tokens.add(source["raw_sha256"])
    for parent in reference["parent_snapshots"]:
        tokens.update(_reference_exposure_tokens(service, parent))
    return tokens


def _reference_independence(service, datasets, fitting, previous=()):
    exposure = set().union(*(_dataset_tokens(d) for d in [*datasets, *previous]))
    for dataset in datasets:
        reference = dataset["reference_snapshot"]
        if reference is None:
            continue
        tokens = _reference_exposure_tokens(service, reference)
        if tokens & (exposure | fitting):
            raise ValueError(
                "Independent reference overlaps Study or calibration fitting acquisition"
            )
        pending = [reference]
        while pending:
            item = pending.pop()
            pending.extend(item["parent_snapshots"])
            for source in item["source_lineage_snapshots"]:
                if _is_capture(source) and exposure & set(
                    source["acquisition_lineage"]["decoder_fitting_tokens"]
                ):
                    raise ValueError(
                        "Reference decoder calibration was fitted on Study acquisition"
                    )
                if _is_capture(source) and exposure & set(
                    source["acquisition_lineage"].get("decoder_evaluation_tokens", ())
                ):
                    raise ValueError(
                        "Reference decoder calibration was evaluated on Study acquisition"
                    )


def _closure_sources(datasets):
    sources = [d["source"] for d in datasets]
    pending = [d["reference_snapshot"] for d in datasets if d["reference_snapshot"]]
    while pending:
        reference = pending.pop()
        sources.extend(reference.get("source_lineage_snapshots", []))
        pending.extend(reference["parent_snapshots"])
    return sources


def _refuse_candidate_reference(service, checksum):
    """Check every indexed run owner; input reference reuse is not an output."""
    with service.registry.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT owner_id FROM artifact_owners "
            "WHERE artifact_id=? AND owner_kind='runs'",
            (checksum,),
        ).fetchall()
    for row in rows:
        job = service.registry.get_run(row[0])
        if job.get("job_kind") not in {"isp_replay", "isp_study_cell"}:
            continue
        inputs = list(_artifacts(job.get("replay_request", {}).get("input_artifacts", [])))
        inputs.extend(_artifacts(job.get("input_artifacts", [])))
        inputs.extend(_artifacts(job.get("dataset_snapshot", {}).get("reference_snapshot")))
        if checksum not in {a["sha256"] for a in inputs} and any(
            a["sha256"] == checksum for a in (job.get("result") or {}).get("artifacts", [])
        ):
            raise ValueError("Known ISP candidate output cannot become an independent reference")


def _implementation():
    names = (
        "isp_studies_v2.py",
        "isp_study_metrics_v2.py",
        "isp_study_contracts_v2.py",
        "isp_v3.py",
        "isp_charge_source.py",
        "isp_charge_calibration.py",
        "isp_replay_v3.py",
        "nonlinear_readout.py",
        "nonlinear_dcg.py",
        "charge_voltage_response.py",
        "isp_studies.py",
        "isp_study_registry.py",
        "evidence_package.py",
        "evidence_objects.py",
        "evidence_storage.py",
    )
    return {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in names
    }


def catalog():
    return {
        **metrics.catalog(),
        "study_schema": "camera_isp_study_v2",
        "metric_spec_schema": "camera_isp_metric_spec_v2",
        "axes": ["raw_denoise.strength", "linear_sharpen.amount"],
        "max_jobs": 24,
        "frames": {"min": 4, "max": 16},
        "evaluation_halo_native_pixels": HALO,
        **AUTHORITY,
    }


def _fixed_acquisition(service, source, calibrations):
    record = source["source_snapshot"]
    if record.get("job_kind") in {"isp_replay", "isp_study_cell"}:
        raise ValueError("Study requires original nonlinear RAW, not candidate output")
    if not _is_capture(source) and record.get("plugin_id") not in {
        "hdr_bracket_v1",
        "reference_hdr_v1",
        "shared_charge_dcg_v1",
    }:
        raise ValueError("Charge Study requires supported explicit original acquisition")
    rows = source["frame_conditions"]
    if len(rows) != source["raw_shape"][0]:
        raise ValueError("Every selected RAW frame needs actual acquisition conditions")
    timing_keys = {"frame_id", "acquisition_id", "frame_start_ns", "applied_at_ns", "latch_at_ns"}
    if _is_capture(source):
        timing_keys.update({"raw_index", "frame_index", "acquisition_index"})
    conditions = [{k: v for k, v in row.items() if k not in timing_keys} for row in rows]
    if any(value != conditions[0] for value in conditions[1:]):
        raise ValueError("Study repeats require fixed exposure, gain and applied settings")
    signatures = [
        {k: cal[k] for k in ("model_sha256", "acquisition", "dark_e_sha256")}
        for cal in calibrations
    ]
    if any(value != signatures[0] for value in signatures[1:]):
        raise ValueError(
            "Study repeats require identical bound decoder model, dark and acquisition"
        )
    if _is_capture(source):
        # Imported evidence has no simulated scene or waveform. Stationarity is
        # the explicit Study declaration, not an inference from missing metadata.
        return
    for label, sequence in (
        ("primary", record.get("scene_sequence_snapshot")),
        ("unmodulated", record.get("unmodulated_scene_sequence_snapshot")),
    ):
        if sequence is None:
            continue
        images = [
            service.registry.get("spectral_images", k["spectral_image_id"])
            for k in sequence["payload"]["knots"]
        ]
        hashes = {
            next(a["sha256"] for a in image["artifacts"] if a["name"] == "spectral-image.npy")
            for image in images
        }
        if len(hashes) != 1:
            raise ValueError(
                f"Repeated noise metrics refuse a changing {label} optical scene"
            )
    waveform = record.get("capture_program_snapshot", {}).get("payload", {}).get("waveform")
    if waveform and waveform.get("kind") != "constant":
        raise ValueError("Study fixed acquisition refuses periodic illumination")


def _provenance_closure(service, sources, baseline, calibration):
    from . import isp_charge_calibration, isp_charge_source

    calibrations, calibration_sources = {}, {}
    pending = [calibration] if calibration else []
    while pending:
        value = pending.pop()
        if value["id"] in calibrations:
            continue
        calibrations[value["id"]] = value
        ref = value["reference_source"]
        calibration_sources[value["id"]] = isp_charge_source.resolve(
            service, ref["kind"], ref["id"], ref["selector"]
        )
        pending.extend(isp_charge_calibration.get_calibration(service, i) for i in value["parents"])
    all_sources = [*sources, *calibration_sources.values()]
    capture_sources = [source for source in all_sources if _is_capture(source)]
    imported_closure = {}
    if capture_sources:
        from .isp_capture_source import evidence_closure

        imported_closure = evidence_closure(service, capture_sources)
    models, pending = {}, [baseline["id"]]
    for source in all_sources:
        assembly = service.registry.get("assemblies", source["source_snapshot"]["assembly_id"])
        pending.extend(assembly["model_ids"])
    for capture in imported_closure.get("nonlinear_acquisition_lineage", []):
        assembly = service.registry.get("assemblies", capture["assembly_id"])
        pending.extend(assembly["model_ids"])
    for response in imported_closure.get("readout_calibrations", []):
        pending.append(response["sensor_model_id"])
    while pending:
        identity = pending.pop()
        if identity in models:
            continue
        model = service.registry.get("models", identity)
        models[identity] = model
        pending.extend(model["parents"])
    factory_ids = {
        m[k] for m in models.values() for k in ("factory_job_id", "psf_factory_job_id") if m.get(k)
    }
    closure = {
        "model_lineage": [models[k] for k in sorted(models)],
        "factory_jobs": [service.registry.get_run(k) for k in sorted(factory_ids)],
        "calibration_lineage": [calibrations[k] for k in sorted(calibrations)],
        "calibration_sources": [calibration_sources[k] for k in sorted(calibration_sources)],
    }
    if capture_sources:
        # Conditional additions preserve the exact shape of historical run-only
        # closure dictionaries. Readout evidence is mandatory even without LSC.
        response = {}
        for source in capture_sources:
            key = digest({"source_id": source["source_id"], "selector": source["selector"]})
            response[key] = {
                "source_id": source["source_id"],
                "selector": source["selector"],
                "response_calibration_snapshot": source["response_calibration_snapshot"],
                "response_calibration_lineage": source["response_calibration_lineage"],
                "decoder_binding": source["decoder_binding"],
                "acquisition_lineage": source["acquisition_lineage"],
            }
        closure["imported_readout_evidence"] = [response[k] for k in sorted(response)]
        closure["decoder_fitting_tokens"] = sorted({
            token for source in capture_sources
            for token in source["acquisition_lineage"]["decoder_fitting_tokens"]
        })
        if any("decoder_evaluation_tokens" in source["acquisition_lineage"]
               for source in capture_sources):
            closure["decoder_evaluation_tokens"] = sorted({
                token for source in capture_sources
                for token in source["acquisition_lineage"].get("decoder_evaluation_tokens", ())
            })
        closure.update(imported_closure)
    _verify_artifacts(service, closure)
    return closure


def _dataset(service, spec, calibration_id):
    from . import isp_charge_calibration, isp_charge_source

    source = isp_charge_source.resolve(
        service, spec["source_kind"], spec["source_id"], spec["selector"]
    )
    if _is_capture(source) and (
        spec["acquisition_group_id"] != source["acquisition_lineage"]["acquisition_group_id"]
    ):
        raise ValueError("Study must retain the original capture acquisition group")
    _verify_artifacts(service, source)
    if len(source["raw_shape"]) != 3 or not 4 <= source["raw_shape"][0] <= 16:
        raise ValueError("Repeated Study data requires 4 through 16 retained RAW frames")
    raw, calibrations = isp_charge_source.load_inputs(service, source)
    _fixed_acquisition(service, source, calibrations)
    if calibration_id:
        isp_charge_calibration.resolve_for_source(service, calibration_id, source)
    metric_spec = metrics.validate_dataset(spec["metric_spec"], shape=tuple(raw.shape[-2:]))
    if not metric_spec["stationary"]:
        raise ValueError("Study metrics require a declared stationary acquisition")
    spec = {**spec, "metric_spec": metric_spec}
    mask = _mask(raw.shape[-2:])
    roi = metric_spec["roi"]
    if not mask[roi["row_start"] : roi["row_end"], roi["column_start"] : roi["column_end"]].any():
        raise ValueError("The fixed evaluation ROI is empty after the declared halo")
    frames = []
    for frame in raw:
        value = hashlib.sha256(canonical(list(frame.shape)).encode())
        value.update(np.ascontiguousarray(frame, dtype="<u4").tobytes())
        frames.append(value.hexdigest())
    reference = get_reference(service, spec["reference_id"]) if spec["reference_id"] else None
    if reference and (
        reference["geometry"] != source["geometry"]
        or reference["pixel_pitch_um"] != source["pixel_pitch_um"]
        or reference["shape"] != [*raw.shape[-2:], 3]
        or reference["normalization_e"] != source["normalization_e"]
    ):
        raise ValueError("Charge reference geometry, shape or fixed electron normalization differs")
    record = source["source_snapshot"]
    if not record.get("assembly_id") or not record.get("test_id"):
        raise ValueError("Study requires fixed original acquisition assembly and test")
    processing_binding = {
        "selector": source["selector"],
        "normalization_e": source["normalization_e"],
        "geometry": source["geometry"],
        "readout_sha256": (
            source["decoder_binding"]["model_sha256"] if _is_capture(source)
            else digest(record["capture_program_snapshot"]["payload"]["electrical_model"])
        ),
    }
    if _is_capture(source):
        processing_binding["decoder_binding"] = source["decoder_binding"]
    return {
        "key": spec["key"],
        "specification": spec,
        "source": source,
        "source_sha256": digest(source),
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "processing_binding": processing_binding,
        "raw_frame_sha256": frames,
        "assembly_id": record["assembly_id"],
        "test_id": record["test_id"],
        "acquisition_lineage_ids": _lineage(service, source),
        "reference_snapshot": reference,
        "evaluation_mask": {
            "shape": list(mask.shape),
            "roi": roi,
            "halo": HALO,
            "sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
        },
    }


def _reducers(spec):
    definitions, objectives, constraints = {}, [], []
    catalog_values = metrics.catalog()["metrics"]
    for key, destination in (("objectives", objectives), ("constraints", constraints)):
        for target in spec[key]:
            metric = target["metric"]
            if metric not in catalog_values:
                raise ValueError("Unknown Study metric: " + metric)
            if key == "constraints" and target["unit"] != catalog_values[metric]["unit"]:
                raise ValueError("Study constraint unit differs from metric definition")
            reduction = {
                "metric": metric,
                "dataset_ids": target["dataset_keys"],
                "reducer": target["reducer"],
            }
            identity = "reduction-" + digest(reduction)[:16]
            definitions[identity] = {"id": identity, **reduction}
            destination.append(
                {
                    "metric": identity,
                    **{
                        k: target[k]
                        for k in (
                            ["direction"] if key == "objectives" else ["operator", "value", "unit"]
                        )
                    },
                }
            )
    if len({o["metric"] for o in objectives}) != len(objectives):
        raise ValueError("Study objectives cannot duplicate the same reduction")
    return list(definitions.values()), objectives, constraints


def template(service, source_kind, source_id, calibration_id, baseline_isp_model_id, selector):
    from .isp_charge_source import resolve
    from .isp_v3 import validate_profile

    base = service.registry.get("models", baseline_isp_model_id)
    profile = validate_profile(base["payload"])
    if (
        profile.get("schema") != "camera_isp_profile_v3"
        or profile["calibration_id"] != calibration_id
    ):
        raise ValueError("Study fixes one v3 baseline profile and its exact calibration")
    axes = [
        {"parameter": parameter, "values": values}
        for parameter, values in (
            ("raw_denoise.strength", [0.0, 0.4, 0.8]),
            ("linear_sharpen.amount", [0.0, 0.4]),
        )
        if profile[parameter.split(".")[0]]["mode"] != "bypass"
    ]
    if not axes:
        raise ValueError(
            "Both raw denoise and linear sharpen are bypassed; configure an enabled "
            "stage in the baseline ISP before creating a tuning template"
        )
    source = resolve(service, source_kind, source_id, selector)
    h, w = source["raw_shape"][-2:]
    return ISPStudyPlan.model_validate(
        {
            "schema": "camera_isp_study_v2",
            "name": "동일 비선형 RAW · 잡음과 선명도 비교",
            "question": "고정 RAW에서 허용 가능한 잡음·질감·halo trade-off는?",
            "calibration_id": calibration_id,
            "baseline_isp_model_id": baseline_isp_model_id,
            "axes": axes,
            "datasets": [
                {
                    "key": "flat",
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "selector": selector,
                    "purpose": "tuning",
                    "acquisition_group_id": (
                        source["acquisition_lineage"]["acquisition_group_id"]
                        if _is_capture(source) else "acquisition:" + source_id
                    ),
                    "stationarity": {
                        "source_kind": "estimated",
                        "description": "사용자가 정지 반복 취득 여부를 확인해야 합니다.",
                    },
                    "metric_spec": {
                        "schema": "camera_isp_metric_spec_v2",
                        "kind": "flat",
                        "stationary": True,
                        "roi": {
                            "row_start": HALO,
                            "row_end": h - HALO,
                            "column_start": HALO,
                            "column_end": w - HALO,
                        },
                    },
                }
            ],
            "objectives": [
                {
                    "metric": "temporal_residual_std_e",
                    "dataset_keys": ["flat"],
                    "reducer": "max",
                    "direction": "minimize",
                }
            ],
            "constraints": [],
        }
    ).model_dump()


def _prepare(service, payload):
    from .evidence_reads import operation_service

    service = operation_service(service)
    from .imaging import _current
    from .isp_charge_calibration import get_calibration
    from .isp_charge_source import load_inputs
    from .isp_replay_v3 import prepare as prepare_replay
    from .isp_replay_v3 import summary_forecast
    from .isp_v3 import validate_inputs, validate_profile

    spec = ISPStudyPlan.model_validate(payload).model_dump()
    base = service.registry.get("models", spec["baseline_isp_model_id"])
    profile = validate_profile(base["payload"])
    if (
        profile.get("schema") != "camera_isp_profile_v3"
        or profile["calibration_id"] != spec["calibration_id"]
    ):
        raise ValueError("Study fixes one v3 baseline profile and its exact calibration")
    calibration = (
        get_calibration(service, spec["calibration_id"]) if spec["calibration_id"] else None
    )
    reducers, objectives, constraints = _reducers(spec)
    datasets = [_dataset(service, d, spec["calibration_id"]) for d in spec["datasets"]]
    common = {k: profile[k] for k in ("demosaic", "white_balance", "ccm")}
    for dataset in datasets:
        reference = dataset["reference_snapshot"]
        if reference and reference["common_linear_profile"] != common:
            raise ValueError("Reference fixed demosaic/WB/CCM differs from the Study baseline")
    spec["datasets"] = [d["specification"] for d in datasets]
    closure = _provenance_closure(service, _closure_sources(datasets), base, calibration)
    capture_plan = "imported_readout_evidence" in closure
    fitting = (
        _capture_fit_tokens(service, closure) if capture_plan
        else _fit_tokens(service, closure["calibration_lineage"])
    )
    for dataset in datasets:
        tokens = set(dataset["acquisition_lineage_ids"]) | {
            dataset["raw_sha256"],
            dataset["specification"]["acquisition_group_id"],
        }
        tokens.update("raw-frame-sha256:" + h for h in dataset["raw_frame_sha256"])
        if capture_plan:
            tokens.add("acquisition-group-id:" + dataset["specification"]["acquisition_group_id"])
        if tokens & fitting:
            raise ValueError("Study RAW overlaps calibration fitting ancestry or original capture")
    for index, dataset in enumerate(datasets):
        if any(_overlap(dataset, prior) for prior in datasets[:index]):
            raise ValueError("Study datasets cannot relabel overlapping RAW acquisition")
    if capture_plan:
        _reference_independence(service, datasets, fitting)
    selection = None
    profiles = [(profile, {})]
    if spec["phase"] == "tuning":
        for axis in spec["axes"]:
            stage = axis["parameter"].split(".")[0]
            if profile[stage]["mode"] == "bypass":
                raise ValueError("A tuning axis cannot be hidden by a bypassed stage")
        for values in itertools.product(*(a["values"] for a in spec["axes"])):
            candidate = copy.deepcopy(profile)
            coordinates = {}
            for axis, value in zip(spec["axes"], values, strict=True):
                stage, parameter = axis["parameter"].split(".")
                candidate[stage][parameter] = value
                coordinates[axis["parameter"]] = value
            profiles.append((candidate, coordinates))
    else:
        selection = get_selection(service, spec["selection_id"])
        if (
            selection["calibration_id"] != spec["calibration_id"]
            or selection["baseline_isp_model_id"] != base["id"]
            or spec["parent_study_id"] != selection["study_id"]
        ):
            raise ValueError(
                "Independent validation must retain selected Study calibration and baseline"
            )
        parent = get_study(service, selection["study_id"])
        if parent["specification"]["schema"] != "camera_isp_study_v2":
            raise ValueError("Charge validation cannot consume a linear Study selection")
        allowed = [d["processing_binding"] for d in parent["datasets"]]
        if any(d["processing_binding"] not in allowed for d in datasets):
            raise ValueError(
                "Independent validation must retain selected path, readout and normalization"
            )
        if any(_overlap(d, prior) for d in datasets for prior in parent["datasets"]):
            raise ValueError("Independent validation overlaps tuning acquisition or RAW frames")
        if capture_plan:
            _reference_independence(service, datasets, fitting, parent["datasets"])
        profiles.append(
            (selection["profile"], {"selected_candidate_id": selection["candidate_id"]})
        )
    if spec["parent_study_id"] and spec["phase"] == "tuning":
        parent = get_study(service, spec["parent_study_id"])
        if spec["attempt"] <= parent["specification"]["attempt"]:
            raise ValueError("A retry must increment the preserved parent attempt")
    unique = {}
    for p, coordinates in profiles:
        unique.setdefault(digest(p), (p, coordinates))
    if len(unique) * len(datasets) > 24:
        raise ValueError("Study candidate × dataset count exceeds 24 cells")
    environment = _current(service)
    basis = {
        "evidence_format": "camera_evidence_bundle_v2",
        "evidence_forecast_version": FORECAST_VERSION,
        "specification": spec,
        "environment": environment,
        "calibration_sha256": digest(calibration),
        "provenance_closure": closure,
        "baseline_sha256": digest(base),
        "datasets": datasets,
        "profiles": list(unique),
        "implementation": _implementation(),
        "selection_snapshot": selection,
        "reducers": reducers,
        "objectives": objectives,
        "constraints": constraints,
    }
    plan_hash = digest(basis)
    identity = "isp_study-" + plan_hash
    if spec["phase"] == "independent_validation":
        for previous in service.registry.list("isp_studies"):
            if previous["id"] == identity:
                continue
            if any(_overlap(d, prior) for d in datasets for prior in previous["datasets"]):
                raise ValueError("Validation acquisition was already used by a preserved Study")
    draft = copy.copy(service)
    draft.registry = _DraftRegistry(service.registry)
    candidates, requests, cells = [], [], []
    output_bytes, summary_bytes, working_max = 0, 0, 0
    for profile_hash, (p, coordinates) in unique.items():
        is_baseline = p == profile
        model = base
        if not is_baseline:
            document = {
                k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields
            }
            document.update(
                name=spec["name"] + " · " + profile_hash[:8],
                revision="isp-study-" + plan_hash[:16],
                parents=[base["id"]],
                source_kind="estimated",
                physical_instance=None,
                source_uri="isp-study://" + plan_hash + "/" + profile_hash,
                method_and_version="camera_isp_study_v2:authored_tuning",
                payload=p,
            )
            model = draft.create_model(document)
        candidate = {
            "candidate_id": "candidate-" + profile_hash,
            "is_baseline": is_baseline,
            "profile": p,
            "profile_sha256": profile_hash,
            "coordinates": coordinates,
            "isp_model_id": model["id"],
            "model_snapshot": model,
        }
        candidates.append(candidate)
        for dataset in datasets:
            source = dataset["source"]
            replay = prepare_replay(
                draft,
                {
                    "schema": "camera_isp_replay_v3",
                    "selector": source["selector"],
                    "name": spec["name"] + " · " + dataset["key"],
                    "source_kind": source["source_kind"],
                    "source_id": source["source_id"],
                    "isp_model_id": model["id"],
                },
            )
            raw, decode_calibrations = load_inputs(draft, source)
            parsed = validate_inputs(
                raw,
                decode_calibrations,
                p,
                geometry=source["geometry"],
                normalization_e=source["normalization_e"],
                calibration=calibration["payload"] if calibration else None,
            )
            working_max = max(working_max, parsed["resources"]["working_array_bytes_estimate"])
            forecast = _forecast(replay, parsed, raw.shape, summary_forecast)
            output_bytes += forecast["output_artifact_bytes_limit"]
            summary_bytes += forecast["summary_artifact_bytes_limit"]
            if output_bytes + summary_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    "Study estimated uncompressed evidence exceeds 256 MiB archive budget"
                )
            wrapper = {
                "name": replay["name"],
                "job_kind": "isp_study_cell",
                "plugin_id": "isp_study_v2",
                "study_id": identity,
                "candidate_id": candidate["candidate_id"],
                "dataset_key": dataset["key"],
                "dataset_snapshot": dataset,
                "replay_request": replay,
                "environment": environment,
                "metric_version": metrics.VERSION,
                "implementation": basis["implementation"],
                "evidence_forecast": forecast,
            }
            requests.append(wrapper)
            cells.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "dataset_key": dataset["key"],
                    "request_sha256": digest(wrapper),
                }
            )
    existing_bytes = sum(
        a["bytes"] for a in _verify_artifacts(service, [datasets, calibration, base, closure])
    )
    from .evidence_package import graph, graph_size

    # The same frozen source/model/calibration appears in every request. Count
    # their exact content-addressed object union, not repeated logical JSON.
    # Reserve output arrays separately and room for completion/metric metadata.
    packed = graph(
        {
            "basis": basis,
            "requests": requests,
            "candidates": candidates,
            "calibration": calibration,
            "baseline": base,
        }
    )
    graph_bytes = graph_size(packed)
    completion_bytes = len(cells) * (
        COMPLETION_GRAPH_BYTES_PER_CELL + OWNERSHIP_ANALYSIS_BYTES_PER_CELL
    )
    # Inventory entries are fixed-schema hashes/paths/byte lengths. Bound future
    # graph object count explicitly; completion enforces both bytes and nodes.
    object_entry = {
        "sha256": "f" * 64,
        "bytes": MAX_ARCHIVE_BYTES,
        "path": "objects/" + "f" * 64 + ".json",
    }
    artifact_entry = {
        "sha256": "f" * 64,
        "bytes": MAX_ARCHIVE_BYTES,
        "path": "artifacts/" + "f" * 64,
    }
    generated_artifacts = sum(
        len(r["evidence_forecast"]["output_array_descriptors"])
        + 10 * r["replay_request"]["source"]["raw_shape"][0]
        + 4
        for r in requests
    )
    existing_artifact_count = len(
        _verify_artifacts(service, [datasets, calibration, base, closure])
    )
    inventory_bytes = (
        (len(packed["objects"]) + len(cells) * (COMPLETION_GRAPH_NODES_PER_CELL + 32) + 32)
        * (_json_bytes(object_entry) + 1)
        + (generated_artifacts + existing_artifact_count) * (_json_bytes(artifact_entry) + 1)
        + 4096
    )
    estimated = (
        output_bytes
        + summary_bytes
        + existing_bytes
        + graph_bytes
        + completion_bytes
        + inventory_bytes
    )
    if estimated > MAX_ARCHIVE_BYTES:
        raise ValueError("Study estimated uncompressed evidence exceeds 256 MiB archive budget")
    result = {
        "schema": "camera_isp_study_plan_v2",
        "evidence_format": "camera_evidence_bundle_v2",
        "specification": spec,
        "plan_hash": plan_hash,
        "plan_hash_basis": basis,
        "environment": environment,
        "calibration_snapshot": calibration,
        "baseline_isp_snapshot": base,
        "selection_snapshot": selection,
        "candidates": candidates,
        "datasets": datasets,
        "cells": cells,
        "job_count": len(cells),
        "resources": {
            "estimated_output_bytes": output_bytes,
            "estimated_summary_artifact_bytes": summary_bytes,
            "completion_metadata_reserved_bytes": completion_bytes,
            "bundle_inventory_bytes_limit": inventory_bytes,
            "estimated_bundle_bytes": estimated,
            "unique_evidence_object_bytes": graph_bytes,
            "logical_evidence_bytes": packed["root"]["expanded_bytes"],
            "estimated_working_bytes_max": working_max,
            "queued_jobs": len(cells),
        },
        "independence": {
            "status": "declared_disjoint_data_not_physical_independence",
            "calibration_fitting_dataset_ids": calibration["dataset_ids"] if calibration else [],
            "reference_authorship": "declared_not_independently_authenticated",
        },
        **AUTHORITY,
    }
    return result, draft.registry.records, requests


def plan(service, payload):
    return _prepare(service, payload)[0]


def submit(service, payload):
    from .isp_study_registry import publish

    data = ISPStudySubmit.model_validate(payload).model_dump()
    prepared, records, requests = _prepare(service, data["specification"])
    if prepared["plan_hash"] != data["plan_hash"]:
        raise ConflictError("ISP Study plan changed; review a fresh plan before submission")
    result = publish(service.registry, prepared, records, requests)
    return get_study(service, result["id"])


def _inputs(service, study):
    from .isp_charge_calibration import get_calibration, resolve_for_source
    from .isp_charge_source import resolve as replay_source

    if digest(study["plan_hash_basis"]) != study["plan_hash"]:
        raise IntegrityError("ISP Study semantic plan hash differs")
    if (
        get_calibration(service, study["specification"]["calibration_id"])
        if study["specification"]["calibration_id"]
        else None
    ) != study["calibration_snapshot"]:
        raise IntegrityError("ISP Study calibration differs from frozen evidence")
    for candidate in study["candidates"]:
        if (
            service.registry.get("models", candidate["isp_model_id"]) != candidate["model_snapshot"]
            or digest(candidate["profile"]) != candidate["profile_sha256"]
            or candidate["model_snapshot"]["payload"] != candidate["profile"]
        ):
            raise IntegrityError("ISP candidate differs from its immutable profile")
    for dataset in study["datasets"]:
        source = dataset["source"]
        fresh = replay_source(
            service, source["source_kind"], source["source_id"], source["selector"]
        )
        if fresh != source or digest(source) != dataset["source_sha256"]:
            raise IntegrityError("ISP Study RAW acquisition differs from its frozen snapshot")
        if study["specification"]["calibration_id"]:
            resolve_for_source(service, study["specification"]["calibration_id"], fresh)
        reference = dataset["reference_snapshot"]
        if reference and get_reference(service, reference["id"]) != reference:
            raise IntegrityError("ISP Study independent reference differs")
    fresh_closure = _provenance_closure(
        service,
        _closure_sources(study["datasets"]),
        study["baseline_isp_snapshot"],
        study["calibration_snapshot"],
    )
    if fresh_closure != study["plan_hash_basis"]["provenance_closure"]:
        raise IntegrityError("Original model/Factory/calibration ancestry changed")
    _verify_artifacts(service, study)


def _analyze(study, cells):
    """Derive metrics from the same verified jobs used by the containing read."""
    rows = []
    for cell, job in zip(study["cells"], cells, strict=True):
        result = job["result"] or {}
        rows.append(
            {
                **cell,
                "status": job["status"],
                "error": job["error"],
                "job_sha256": digest(job),
                "artifacts": job["artifacts"],
                "metrics": result.get("metric_result", {}).get("metrics"),
                "metric_result": result.get("metric_result"),
                "summary": {
                    k: result.get("summary", {}).get(k)
                    for k in ("stage_previews", "raw_sha256", "stage_outputs")
                },
            }
        )
    reducers, objectives, constraints = _reducers(study["specification"])
    aggregate_rows = []
    for candidate in study["candidates"]:
        own = [r for r in rows if r["candidate_id"] == candidate["candidate_id"]]
        status = next(
            (
                s
                for s in ("running", "queued", "failed", "cancelled")
                if any(r["status"] == s for r in own)
            ),
            "succeeded",
        )
        reduced = metrics.reduce_datasets(
            {r["dataset_key"]: r["metric_result"] for r in own if r["metric_result"] is not None},
            reducers,
        )
        aggregate_rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "status": status,
                "metrics": reduced["metrics"],
            }
        )
    aggregate = metrics.aggregate(
        aggregate_rows,
        objectives,
        constraints,
        expected_candidate_ids=[c["candidate_id"] for c in study["candidates"]],
    )
    if study["specification"]["phase"] == "independent_validation":
        for row in aggregate["rows"]:
            row["pareto_status"] = "not_ranked_validation"
        aggregate["pareto_scope"] = "fixed_baseline_and_explicit_selection_no_validation_ranking"
        aggregate["pareto_provisional"] = True
    analysis = {
        "schema": "camera_isp_study_analysis_v2",
        "study_id": study["id"],
        "plan_hash": study["plan_hash"],
        "phase": study["specification"]["phase"],
        "status": "complete" if all(r["status"] in TERMINAL for r in rows) else "incomplete",
        "rows": rows,
        "coverage": {
            s: sum(r["status"] == s for r in rows)
            for s in ("queued", "running", "succeeded", "failed", "cancelled")
        },
        "aggregate": aggregate,
        "reduction_definitions": reducers,
        **AUTHORITY,
    }
    analysis["analysis_sha256"] = digest(analysis)
    return analysis


def select_candidate(service, identity, payload):
    data = ISPSelectionCreate.model_validate(payload).model_dump()
    reader = legacy._EvidenceReader(service)
    study, _ = reader.study(identity)
    analysis = reader.analysis(identity)
    if study["specification"]["phase"] != "tuning" or analysis["status"] != "complete":
        raise ValueError("Explicit selection requires a terminal tuning Study")
    if data["analysis_sha256"] != analysis["analysis_sha256"]:
        raise ConflictError("Selection analysis changed; review the current exact result")
    candidate = next(
        (c for c in study["candidates"] if c["candidate_id"] == data["candidate_id"]), None
    )
    if candidate is None or any(
        r["status"] != "succeeded"
        for r in analysis["rows"]
        if r["candidate_id"] == data["candidate_id"]
    ):
        raise ValueError("Selection needs a registered candidate with every dataset completed")
    document = {
        "schema": "camera_isp_selection_v2",
        "study_id": identity,
        "plan_hash": study["plan_hash"],
        **data,
        "profile": candidate["profile"],
        "profile_sha256": candidate["profile_sha256"],
        "analysis_snapshot": analysis,
        "calibration_id": study["specification"]["calibration_id"],
        "baseline_isp_model_id": study["specification"]["baseline_isp_model_id"],
        "decision_kind": "explicit_engineering_choice_not_approval",
        **AUTHORITY,
    }
    selection_id = "isp_selection-" + digest(document)
    try:
        return get_selection(service, selection_id)
    except KeyError:
        try:
            return service.registry.put("isp_selections", document, identity=selection_id)
        except ConflictError:
            existing = get_selection(service, selection_id)
            if _semantic(existing) != document:
                raise IntegrityError("Selection idempotency identity collision") from None
            return existing


def import_reference(service, metadata, npy_bytes):
    data = _reference_contract(metadata).model_validate(metadata).model_dump()
    if not isinstance(npy_bytes, bytes) or len(npy_bytes) > 8 * 1024 * 1024:
        raise ValueError("Reference NPY exceeds the bounded upload contract")
    stream = io.BytesIO(npy_bytes)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Reference requires NPY v1/v2")
        if (
            len(shape) != 3
            or shape[-1] != 3
            or any(not 1 <= n <= 512 for n in shape[:2])
            or dtype.kind != "f"
        ):
            raise ValueError("Reference requires bounded floating H×W×3 pre-tone RGB")
        if np.prod(shape) * dtype.itemsize + stream.tell() != len(npy_bytes):
            raise ValueError("Reference NPY has missing or trailing payload")
        values = np.load(io.BytesIO(npy_bytes), allow_pickle=False)
    except (TypeError, EOFError, OverflowError) as exc:
        raise ValueError("Invalid independent reference NPY") from exc
    if not np.isfinite(values).all():
        raise ValueError("Reference RGB must be finite signed pre-tone values")
    _reference_geometry(data["geometry"], shape)
    parents = [get_reference(service, p) for p in data["parents"]]
    for parent in parents:
        if parent["geometry"] != data["geometry"]:
            raise ValueError("Reference revision must retain its geometry")
    extension = {}
    if "acquisition_lineage" in data:
        _refuse_candidate_reference(service, hashlib.sha256(npy_bytes).hexdigest())
        if any("acquisition_lineage" not in parent for parent in parents):
            raise ValueError("Reference lineage revisions require explicit parent ancestry")
        extension["source_lineage_snapshots"] = _reference_sources(service, data)
    artifact = service.registry.store_blob(
        npy_bytes, "isp-reference-linear-rgb.npy", "application/octet-stream"
    )
    artifact.update(shape=list(shape), dtype=str(values.dtype), unit=data["unit"])
    meta = service.registry.store_blob(
        canonical(data).encode(), "isp-reference-metadata.json", "application/json"
    )
    return service.registry.put(
        "isp_references",
        {
            **data,
            "shape": list(shape),
            "array_artifact": artifact,
            "artifacts": [artifact, meta],
            "parent_snapshots": parents,
            **extension,
            "approval_status": "draft",
            "independence": "declared_not_independently_authenticated",
            **AUTHORITY,
        },
    )


def get_reference(service, identity):
    record = service.registry.get("isp_references", identity)
    contract = _reference_contract(record)
    normalized = contract.model_validate(
        {k: record[k] for k in contract.model_fields}
    ).model_dump()
    artifact = next(a for a in record["artifacts"] if a["name"] == "isp-reference-metadata.json")
    _verify_artifacts(service, record)
    if (
        service.registry.blob_path(artifact["sha256"]).read_bytes()
        != canonical(normalized).encode()
    ):
        raise IntegrityError("Reference metadata differs from its canonical original blob")
    for parent in record["parent_snapshots"]:
        if get_reference(service, parent["id"]) != parent:
            raise IntegrityError("Reference parent differs from frozen lineage")
    if "acquisition_lineage" in normalized:
        if _reference_sources(service, normalized) != record["source_lineage_snapshots"]:
            raise IntegrityError("Reference original acquisition lineage changed")
        _refuse_candidate_reference(service, record["array_artifact"]["sha256"])
    return record


def _npy_bytes(shape, dtype):
    """Exact np.save C-order header and payload bytes, without allocating pixels."""
    stream = io.BytesIO()
    dtype = np.dtype(dtype)
    if dtype.hasobject:
        raise ValueError("Study arrays must not contain objects")
    np.lib.format.write_array_header_1_0(
        stream,
        {
            "descr": np.lib.format.dtype_to_descr(dtype),
            "fortran_order": False,
            "shape": tuple(shape),
        },
    )
    return stream.tell() + int(np.prod(shape, dtype=object)) * dtype.itemsize


def _png_bytes(height, width):
    """RGB8 scanlines, zlib compressBound plus IDAT chunk and PNG overhead."""
    raw = height * (width * 3 + 1)
    compressed = raw + (raw >> 12) + (raw >> 14) + (raw >> 25) + 13
    return compressed + ((compressed + 65535) // 65536) * 12 + 1024


def _forecast(replay, parsed, shape, summary_forecast):
    descriptors = parsed["resources"]["output_array_descriptors"]
    arrays = sum(_npy_bytes(d["shape"], d["dtype"]) for d in descriptors)
    count, height, width = shape
    # Eight stage images + common-color baseline + frame preview; one main PNG.
    previews = (10 * count + 1) * _png_bytes(height, width)
    summary = summary_forecast(replay)
    return {
        "version": FORECAST_VERSION,
        "summary_forecast": summary,
        "summary_artifact_bytes_limit": summary["summary_artifact_bytes_limit"],
        "array_artifact_bytes": arrays,
        "output_array_descriptors": descriptors,
        "preview_artifact_bytes_limit": previews,
        "metric_artifact_bytes_limit": METRIC_BYTES_LIMIT,
        "output_artifact_bytes_limit": (
            arrays + previews + METRIC_BYTES_LIMIT + _npy_bytes((height, width), "bool")
        ),
        "completion_graph_bytes_limit": COMPLETION_GRAPH_BYTES_PER_CELL,
        "completion_graph_nodes_limit": COMPLETION_GRAPH_NODES_PER_CELL,
        "meaning": "uncompressed artifact bytes; exact NPY sizes and guarded generated metadata",
    }


def _verify_forecast(job, result):
    forecast = job["evidence_forecast"]
    if forecast.get("version") != FORECAST_VERSION:
        raise IntegrityError("Unsupported charge Study forecast version")
    summaries = [a for a in result["artifacts"] if a["name"] == "summary.json"]
    if len(summaries) != 1 or summaries[0]["bytes"] > forecast["summary_artifact_bytes_limit"]:
        raise IntegrityError("Charge Study summary exceeds its exact copied-field preflight bound")
    inputs = {(a["sha256"], a["name"]) for a in job["replay_request"]["input_artifacts"]}
    reference = job["dataset_snapshot"]["reference_snapshot"]
    if reference:
        inputs.update((a["sha256"], a["name"]) for a in reference["artifacts"])
    output = [
        a
        for a in result["artifacts"]
        if a["name"] != "summary.json" and (a["sha256"], a["name"]) not in inputs
    ]
    if sum(a["bytes"] for a in output) > forecast["output_artifact_bytes_limit"]:
        raise IntegrityError("Charge Study output exceeds its declared artifact bound")


def execute(service, job):
    from .evidence_package import graph
    from .imaging import _current
    from .isp_replay_v3 import execute as replay_execute

    if get_cell(service, job["id"]) != job:
        raise IntegrityError("Worker cell differs from the registered exact request")
    if job["environment"] != _current(service) or job["implementation"] != _implementation():
        raise ValueError("Worker differs from frozen charge Study environment/implementation")
    result = replay_execute(service, job["replay_request"])
    _verify_forecast(job, result)
    dataset, source = job["dataset_snapshot"], job["dataset_snapshot"]["source"]
    rgb, rgb_artifact = _array(service, result, "sharpened_rgb_frames")
    valid, valid_artifact = _array(service, result, "rgb_valid_mask_frames")
    baseline, baseline_artifact = _array(service, result, "baseline_ccm_rgb_frames")
    baseline_valid, baseline_valid_artifact = _array(
        service, result, "baseline_rgb_valid_mask_frames"
    )
    mask = _mask(tuple(source["raw_shape"][-2:]))
    if hashlib.sha256(mask.tobytes()).hexdigest() != dataset["evaluation_mask"]["sha256"]:
        raise IntegrityError("Charge Study fixed evaluation mask changed")
    reference = dataset["reference_snapshot"]
    measured = metrics.measure(
        rgb,
        normalization_e=source["normalization_e"],
        dataset=dataset["specification"]["metric_spec"],
        evaluation_mask=mask,
        valid_mask_frames=valid,
        baseline_rgb_frames=baseline,
        baseline_valid_mask_frames=baseline_valid,
        reference_rgb=_npy(service, reference["array_artifact"]) if reference else None,
    )
    mask_artifact = _store_array(service, mask, "isp-study-evaluation-mask")
    measured["evidence"] = {
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "source_sha256": dataset["source_sha256"],
        "selector": source["selector"],
        "normalization_e": source["normalization_e"],
        "calibration_summaries_sha256": digest(source["calibration_summaries"]),
        "rgb_artifact": rgb_artifact,
        "valid_mask_artifact": valid_artifact,
        "evaluation_mask_artifact": mask_artifact,
        "baseline_artifact": baseline_artifact,
        "baseline_valid_mask_artifact": baseline_valid_artifact,
        "reference_artifact": reference["array_artifact"] if reference else None,
        "implementation": job["implementation"],
        "environment": job["environment"],
    }
    encoded = canonical(measured).encode()
    if len(encoded) > job["evidence_forecast"]["metric_artifact_bytes_limit"]:
        raise IntegrityError("Charge Study metrics exceed generated metadata contract")
    metric_artifact = service.registry.store_blob(
        encoded, "isp-study-metrics.json", "application/json"
    )
    result["artifacts"].extend([mask_artifact, metric_artifact])
    if reference:
        result["artifacts"].extend(reference["artifacts"])
    result.update(
        metric_result=measured,
        study_id=job["study_id"],
        candidate_id=job["candidate_id"],
        dataset_key=job["dataset_key"],
        **AUTHORITY,
    )
    # Measure only newly generated graph nodes: large exact input-dependent copies
    # have already been counted in the read-only plan and retain identical nodes.
    original = graph({"request": job["replay_request"], "dataset": dataset})
    completion = graph(result)
    added = [data for key, data in completion["objects"].items() if key not in original["objects"]]
    delta = sum(map(len, added))
    if (
        delta > job["evidence_forecast"]["completion_graph_bytes_limit"]
        or len(added) > job["evidence_forecast"]["completion_graph_nodes_limit"]
    ):
        raise IntegrityError("Charge Study generated completion graph exceeds preflight guard")
    _verify_forecast(job, result)
    return result


def _verify_cell_result(job):
    result = job["result"]
    dataset = job["dataset_snapshot"]
    source = dataset["source"]
    evidence = result["metric_result"]["evidence"]
    expected = {
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "source_sha256": dataset["source_sha256"],
        "selector": source["selector"],
        "normalization_e": source["normalization_e"],
        "calibration_summaries_sha256": digest(source["calibration_summaries"]),
    }
    if any(evidence.get(k) != v for k, v in expected.items()):
        raise IntegrityError("Charge Study metric differs from fixed selected RAW/calibration")
    if result["metric_result"].get("schema") != metrics.VERSION:
        raise IntegrityError("Charge Study completion uses a different metric version")
    _verify_forecast(job, result)

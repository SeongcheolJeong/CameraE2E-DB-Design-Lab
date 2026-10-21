"""Remeasure every frozen Calibration Lab edge through the E-SFR image operator.

Curve fitting and image replay are deliberately separate checks. No optimization,
held-out geometry fitting, PSF deconvolution or threshold adaptation occurs here.
"""
import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

from .registry import IntegrityError, canonical, digest


def write_replay_result(result, directory):
    """Store every original result value losslessly, with a small browsing summary.

    Reports are rendered from the complete result before this serialization step.
    This changes storage only: no curve samples, ROI rows, profile values or
    comparison metrics are recomputed, clipped, averaged or dropped from evidence.
    """
    from .esfr_lab import compact_summary

    directory = Path(directory)
    archive = directory / "full-result.json.gz"
    summary_path = directory / "result.json"
    if archive.exists() or summary_path.exists():
        raise ValueError("Replay result serialization requires new evidence paths")
    expanded_sha = hashlib.sha256()
    expanded_bytes = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    with archive.open("xb") as target:
        # Empty filename and fixed mtime make identical logical results produce
        # identical gzip bytes regardless of output path and wall-clock time.
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=target, mtime=0, compresslevel=6
        ) as stream:
            buffer = bytearray()
            for text in encoder.iterencode(result):
                data = text.encode("utf-8")
                expanded_bytes += len(data)
                buffer.extend(data)
                if len(buffer) >= 256 * 1024:
                    expanded_sha.update(buffer)
                    stream.write(buffer)
                    buffer.clear()
            if buffer:
                expanded_sha.update(buffer)
                stream.write(buffer)
    compressed_sha = hashlib.sha256()
    with archive.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            compressed_sha.update(data)
    compact = compact_summary(result)
    # The standalone E-SFR compact_summary points to uncompressed detail-only
    # JSON. This supplementary workflow instead retains the complete original
    # result in its explicitly encoded archive, including all profiles/metadata.
    compact.pop("measurement_details_file", None)
    compact["full_result_artifact"] = {
        "schema": "camera_calibration_lossless_result_v1",
        "path": archive.name,
        "media_type": "application/gzip",
        "encoding": "gzip",
        "sha256": compressed_sha.hexdigest(),
        "bytes": archive.stat().st_size,
        "expanded_sha256": expanded_sha.hexdigest(),
        "expanded_bytes": expanded_bytes,
        "expanded_media_type": "application/json",
        "canonicalization": "registry.canonical UTF-8",
        "contents": "complete original E-SFR replay result before summary compaction",
    }
    compact["curve_storage"] = "all samples retained losslessly in full_result_artifact"
    summary_path.write_text(canonical(compact), encoding="utf-8")
    return compact


def adapted_job(source, calibration):
    """Bind exact native ROI centers to fitted nodes, without nearest-neighbor lookup."""
    from .esfr_bridge import _field as native_field
    from .esfr_engine import _field

    records = calibration["measurement_snapshots"]
    candidates = calibration["candidate_profile"]
    baseline = calibration["base_profile"]
    metadata = source["capture_snapshots"][0]["metadata"]
    for profile in (baseline, candidates):
        if (profile["blur"].get("field_coordinate_domain", "sensor_normalized") != "sensor_normalized"
            or profile["native_shape"] != metadata["geometry"]["full_sensor_shape"]
            or profile["cfa"] != metadata["raw_format"]["cfa"]
            or profile["pixel_pitch_um"] != metadata["pixel_pitch_um"]
            or profile["electronics"]["bit_depth"] != metadata["raw_format"]["bit_depth"]
            or any(metadata["conditions"].get(k) != v for k, v in profile["conditions"].items())):
            raise IntegrityError("Calibration profile sensor coordinates or capture conditions differ from E-SFR source")
    nodes = {tuple(n["field_xy"]): n for n in candidates["blur"]["field_nodes"]}
    selected = {}
    for measurement in records:
        provenance = measurement.get("provenance", {})
        roi_id = provenance.get("roi_id")
        if not roi_id or roi_id in selected:
            raise IntegrityError("E-SFR replay requires one aggregated observation per ROI")
        selected[roi_id] = measurement
    spec = deepcopy(source["specification"])
    edges = [roi for roi in spec["rois"] if roi["kind"] == "edge"]
    if set(selected) != {roi["id"] for roi in edges}:
        raise IntegrityError("E-SFR replay requires every source edge exactly once")
    # A spatial baseline must be evaluated by a spatial baseline operator, never
    # silently substituted by the E-SFR engine's scalar before-width.
    widths = {(n["sigma_x_px"], n["sigma_y_px"], n.get("family", "centered_elliptical_gaussian_v1"))
              for n in baseline["blur"]["field_nodes"]}
    if len(widths) != 1 or next(iter(widths))[0] != next(iter(widths))[1] or next(iter(widths))[2] != "centered_elliptical_gaussian_v1":
        raise ValueError("E-SFR full-edge image replay requires an explicitly uniform isotropic baseline")
    spec["baseline_sigma_native_px"] = next(iter(widths))[0]
    forward = candidates.get("esfr_forward_model")
    if forward:
        if (forward.get("fit_data_role") != "development_only"
                or digest(forward.get("profile")) != forward.get("profile_sha256")):
            raise IntegrityError("Frozen development forward-model binding changed")
        frozen = deepcopy(forward["profile"])
        if (any(frozen.get(k) != metadata.get(k) for k in
                ("camera_id", "conditions", "raw_format", "source_kind"))
                or any(frozen["geometry"].get(k) != metadata["geometry"].get(k)
                       for k in ("crop_origin", "full_sensor_shape"))
                or frozen.get("development_capture_ids") != source["development_ids"]
                or set(frozen["fields"]) != set(selected)):
            raise IntegrityError("Frozen forward capture context differs from its development source")
        expected_fields = deepcopy(frozen["fields"])
    else:
        frozen = deepcopy(source["result"]["summary"]["candidate_profile"])
        expected_fields = None
    frozen["fields"] = {}
    frozen["scope"] = ("frozen_calibration_lab_forward_observer_image_remeasurement"
                       if forward else "frozen_calibration_lab_curve_fit_image_remeasurement")
    mapping = []
    for roi in edges:
        measurement = selected[roi["id"]]
        provenance = measurement["provenance"]
        if provenance["native_roi"] != roi["box"]:
            raise IntegrityError("Exported native ROI changed")
        coordinates = tuple(measurement["observations"]["field_xy"])
        if coordinates != tuple(native_field(metadata, roi["box"])):
            raise IntegrityError("Exported field coordinates differ from native ROI center")
        if coordinates not in nodes:
            raise IntegrityError("Fitted profile lacks an exact source ROI field")
        node = nodes[coordinates]
        if expected_fields is not None:
            expected = expected_fields.get(roi["id"], {})
            if any(expected.get(k) != node[k] for k in ("sigma_x_px", "sigma_y_px", "theta_deg")):
                raise IntegrityError("Legacy field and frozen native forward field differ")
        if node.get("family", "centered_elliptical_gaussian_v1") != "centered_elliptical_gaussian_v1":
            raise ValueError("E-SFR image replay currently supports effective Gaussian nodes")
        template = frozen["geometry"]["roi_nuisance"][roi["id"]]
        if template["geometry"]["box"] != roi["box"]:
            raise IntegrityError("Development nuisance geometry differs from source ROI")
        roi["field_id"] = roi["id"]
        template["geometry"]["field_id"] = roi["id"]
        field = _field(node["sigma_x_px"], [measurement["observations"]["orientation_deg"]])
        field.update(sigma_y_px=node["sigma_y_px"], theta_deg=node["theta_deg"],
                     model="frozen_calibration_lab_effective_gaussian",
                     directional_fit="inherited_without_refit")
        frozen["fields"][roi["id"]] = field
        mapping.append(dict(roi_id=roi["id"], field_xy=list(coordinates), measurement_id=measurement["id"],
                            sigma_x_px=node["sigma_x_px"], sigma_y_px=node["sigma_y_px"], theta_deg=node["theta_deg"]))
    frozen["calibration_profile_sha256"] = digest(candidates)
    frozen["fit_assumptions"] += [
        "Frozen Calibration Lab parameters are replayed unchanged; image metrics are a separate test.",
        "Development-only nuisance geometry/levels are frozen in the candidate; no validation refit."
        if forward else "Development-only nuisance geometry/levels are reused from the exact source study; no validation refit.",
    ]
    return {**deepcopy(source), "specification": spec, "frozen_profile_snapshot": frozen}, mapping


def acquisition_scope(acquisition):
    """Describe evidence strength separately from numerical image agreement."""
    if acquisition is None:
        return {
            "candidate_frozen_before_new_acquisition": False,
            "validation_acquisition_status": "reused_source_holdout_not_new_blinded_acquisition",
            "acquisition_evidence": "existing_source_capture_replay",
            "acquisition_time_authenticated": False,
            "physical_identity_verified": False,
            "note": "Source validation captures were already used in the earlier E-SFR study; this is a reused frozen holdout, not a newly blinded acquisition.",
        }
    measured = acquisition.get("source_kind") == "measured"
    return {
        "candidate_frozen_before_new_acquisition": True,
        "validation_acquisition_status": "new_independent_measured_acquisition" if measured else "new_independent_synthetic_acquisition",
        "acquisition_evidence": "operator_declared_not_authenticated" if measured else "synthetic_generation_metadata_and_immutable_import",
        "acquisition_time_authenticated": False,
        "physical_identity_verified": False,
        "note": (
            "New measured RAW/ISP pairs were declared by the operator as captured after the frozen candidate/geometry contract. File hashes, declared chronology and independent lineage are checked; acquisition time, camera identity, printed-chart traceability and ISO conformance are not authenticated or certified."
            if measured else
            "New synthetic noise realizations were acquired after candidate freeze. The source run measured them with fit:false before this frozen replay; no physical camera validation is established."
        ),
    }


def execute(service, job, summary, directory, progress):
    from . import esfr_lab
    from .esfr_engine import run
    from .esfr_report import overviews, publish

    records = job["measurement_snapshots"]
    origins = {m.get("provenance", {}).get("esfr_run_id") for m in records}
    if origins == {None}:
        return None
    if None in origins or len(origins) != 1:
        raise ValueError("An E-SFR full-edge replay must bind one exact source study")
    source = esfr_lab.get(service, next(iter(origins)))
    if source["status"] != "succeeded":
        raise IntegrityError("E-SFR source study is not complete")
    from .esfr_bridge import validation_contract_for_source
    acquisition = validation_contract_for_source(service, source["id"], job["parent_study_id"])
    if acquisition is not None:
        if (acquisition["candidate_sha256"] != job["candidate_sha256"]
                or set(acquisition["validation_ids"]) != set(job["specification"]["measurement_ids"])):
            raise IntegrityError("Fresh acquisition contract differs from frozen validation job")
    # All source captures and their exact original bytes remain separately owned.
    captures = []
    for frozen in source["capture_snapshots"]:
        if esfr_lab.capture(service, frozen["id"]) != frozen:
            raise IntegrityError("E-SFR source capture changed")
        captures.append((frozen, esfr_lab._binding(service, frozen, "raw"), esfr_lab._binding(service, frozen, "isp")))
    adapted, mapping = adapted_job(source, job)
    out = Path(directory) / "esfr-images"
    result = run(adapted, captures, out, lambda msg: progress({"stage": "112-edge RAW/ISP: " + msg}))
    if acquisition and result["source_kind"] != acquisition.get("source_kind", "synthetic"):
        raise IntegrityError("Replay source kind differs from its frozen acquisition contract")
    if result["fit"]["status"] != "frozen_replay" or result["fit"].get("changed_parameters"):
        raise IntegrityError("E-SFR validation unexpectedly refitted the candidate")
    if result["candidate_profile"]["calibration_profile_sha256"] != summary.get("candidate_sha256", digest(job["candidate_profile"])):
        raise IntegrityError("E-SFR replay candidate hash differs from Calibration Lab")
    result["overviews"] = overviews(captures, adapted["specification"]["rois"], out)
    result["chart_profile"] = deepcopy(source["specification"]["chart_profile"])
    from .calibration_completion import _bindings
    source_files = source["result"]["summary"].get("files", {})
    names = [n for n in ("chart.svg", "chart-rois.svg", "reference.png", "chart-manifest.json") if n in source_files]
    bound = _bindings(service, [source_files[n] for n in names], "runs", source["id"])
    if bound and digest(next(iter(bound.values()))["owner"]["result"]) != digest(source["result"]):
        raise IntegrityError("Chart evidence owner differs from source study")
    for name in names:
        (out / name).write_bytes(bound[source_files[name]]["path"].read_bytes())
    report_url = f"/api/v1/calibration-lab/studies/{job['id']}/files/esfr-images/report.html"
    result.update(run_id=job["id"], source_context=source.get("source_context"), report_url=report_url,
                  source_code=job["source_code"], calibration_replay_mapping=mapping,
                  replay_of_applied_profile="frozen Calibration Lab candidate " + job["candidate_sha256"])
    forward = job["candidate_profile"].get("esfr_forward_model")
    result["limitations"].insert(0, "The candidate was fitted only on development RAW Gr using the native forward observer; no held-out refit occurs."
                                 if forward else "This is image remeasurement of a separate Calibration Lab curve fit; it is not a refit of image-domain SFR.")
    scope = acquisition_scope(acquisition)
    result["limitations"].insert(1, scope["note"])
    scope = {key: value for key, value in scope.items() if key != "note"}
    result.update(scope)
    from .calibration_esfr_history import comparison_history
    history = comparison_history(service, job, result) if acquisition else None
    result["acquisition_contract"] = acquisition
    result["comparison_history"] = history
    publish(
        {**result, "measurement_details_file": "full-result.json.gz",
         "request_file": "../request.json"},
        out,
    )
    write_replay_result(result, out)
    (out / "field-mapping.json").write_text(canonical(mapping))
    return dict(schema="camera_calibration_esfr_image_validation_v1", metrics=result["metrics"],
                validation=result["validation"], report_url=report_url, field_count=len(mapping),
                comparison_count=len([c for c in result["comparisons"] if c["role"] == "validation"]),
                candidate_sha256=digest(job["candidate_profile"]), adapted_profile_sha256=result["candidate_profile_sha256"],
                source_study_id=source["id"], source_result_sha256=digest(source["result"]),
                source_kind=result["source_kind"], physical_validation="not_established", refit_performed=False,
                basis="frozen_development_native_forward_fit" if forward else "frozen_legacy_curve_fit_remeasured_with_esfr_image_operator",
                acquisition_contract=acquisition, comparison_history=history, **scope)


def linked_completion(service, source):
    """Return a verified completion only for the exact DB tuple and active campaign profile."""
    from . import calibration_completion
    from . import calibration_lab as lab
    exports = [r for r in service.registry.list("camera_validation_esfr_exports") if r.get("source") == source]
    campaigns = {r["campaign_id"] for r in exports}
    reports = [r for r in service.registry.list(calibration_completion.COLLECTION) if r["campaign_id"] in campaigns]
    for record in sorted(reports, key=lambda r: (r["created_at"], r["id"]), reverse=True):
        active = lab._active_profile(service, record["campaign_id"])
        if not active or active["id"] != record["profile_id"]:
            continue
        study, validation = lab.get(service, record["study_id"]), lab.get(service, record["validation_id"])
        if digest(study["result"]) != record["study_result_sha256"] or digest(validation["result"]) != record["validation_result_sha256"]:
            raise IntegrityError("DB linked calibration completion differs from source results")
        evidence = validation["result"]["summary"].get("esfr_image_validation")
        matching = [e for e in exports if e["campaign_id"] == record["campaign_id"]
                    and set(e["training_ids"]) == set(study["result"]["summary"]["fit"].get("training_measurement_ids", []))
                    and set(e["validation_ids"]) == set(validation["specification"]["measurement_ids"])
                    and evidence and evidence["source_study_id"] == e["run_id"]]
        if not matching:
            continue
        query = dict(campaign=record["campaign_id"], study=record["study_id"],
                     source_run=source["run_id"], candidate=source["candidate_id"], condition=source.get("condition_id", ""))
        return dict(completion=record, profile_id=active["id"], source=source,
                    field_count=len(study["result"]["summary"]["fit"].get("training_measurement_ids", [])),
                    image_validation=validation["result"]["summary"].get("esfr_image_validation"),
                    lab_url="/calibration-lab.html?" + urlencode(query), scope="camera_effective_response_no_optical_prescription_overwrite")
    return None

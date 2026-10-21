"""Lossless measured-response bridge; E-SFR and optical prescriptions stay distinct."""

from __future__ import annotations

import copy
import csv
import io
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import numpy as np

from .registry import ConflictError, IntegrityError, canonical, digest

EXPORTS = "camera_validation_esfr_exports"
VALIDATION_CONTRACTS = "camera_validation_esfr_holdout_contracts"
MEASURED_CONTRACT_SCHEMA = "camera_esfr_frozen_measured_holdout_contract_v1"
MEASURED_CONTEXT_FIELDS = (
    "camera_id", "conditions", "raw_format", "geometry", "isp", "pixel_pitch_um",
    "source_kind", "sampling", "raw_processing",
)
READINESS_VERSION = "full_edge_forward_observer_v3"
ASSUMPTIONS = [
    "Only RAW Gr camera-effective SFR curves are exported; QE, Dark, PTC and CCM are not fitted.",
    "The chart MTF is unknown: effective response includes the chart and capture conditions.",
    "External-curve fitting initializes an effective zero-phase response, not a unique lens PSF.",
    "Analytic MTF initializes the profile; the requested refinement uses the same frozen E-SFR RAW observation operator.",
    "Unspecified electrical/color values are authored simulator placeholders, not measurements.",
    "Camera identity, instrument calibration and ISO algorithm conformance are not verified.",
]


def baseline_profile(metadata, sigma=1.5):
    """Build a scoped curve-fit baseline using declared geometry and conditions.

    Required capture additions: pixel_pitch_um and all legacy conditions fields.
    An optional calibration_bridge.baseline_profile can replace authored defaults,
    but cannot override the declared camera geometry, CFA, depth, or conditions.
    """
    from .calibration_lab_contracts import DEFAULT_CONDITIONS, conditions_spec
    from .calibration_lab_engine import default_profile, validate_profile

    fmt, geometry = metadata["raw_format"], metadata["geometry"]
    missing = sorted(set(DEFAULT_CONDITIONS) - set(metadata.get("conditions", {})))
    if "pixel_pitch_um" not in metadata:
        missing.append("pixel_pitch_um")
    if missing:
        raise ValueError(
            "Calibration bridge needs explicit capture declarations: " + ", ".join(missing)
        )
    if fmt["cfa"] != "RGGB":
        raise ValueError(
            "Calibration Lab bridge supports full-sensor RGGB only; E-SFR analysis still works"
        )
    full = geometry["full_sensor_shape"]
    if any(type(x) is not int or x < 32 or x % 2 for x in full):
        raise ValueError(
            "Calibration Lab bridge requires even full-sensor dimensions >=32; "
            "no cropping/resizing is inferred"
        )
    conditions = conditions_spec(
        {
            k: v
            for k, v in metadata["conditions"].items()
            if k in {*DEFAULT_CONDITIONS, "exposure_s"}
        }
    )
    supplied = metadata.get("calibration_bridge", {}).get("baseline_profile")
    p = copy.deepcopy(supplied) if supplied is not None else default_profile()
    if supplied is not None:
        for key, value in {
            "native_shape": full,
            "pixel_pitch_um": metadata["pixel_pitch_um"],
            "cfa": fmt["cfa"],
            "conditions": conditions,
        }.items():
            if p.get(key) != value:
                raise ValueError("Explicit baseline differs from capture " + key)
        if p.get("electronics", {}).get("bit_depth") != fmt["bit_depth"]:
            raise ValueError("Explicit baseline bit depth differs from capture")
    else:
        p.update(
            native_shape=copy.deepcopy(full),
            pixel_pitch_um=metadata["pixel_pitch_um"],
            conditions=conditions,
            render_shape=[min(128, x) for x in full],
        )
        levels = fmt["black_level_dn"]
        gr_black = float(levels[1] if isinstance(levels, list) else levels)
        p["electronics"].update(bit_depth=fmt["bit_depth"], black_level_dn=gr_black)
        if not np.isfinite(sigma) or not 0.03 <= sigma <= 8:
            raise ValueError("Legacy curve-fit baseline sigma must be in [0.03,8]")
        p["blur"]["field_nodes"][0].update(sigma_x_px=sigma, sigma_y_px=sigma)
    p["esfr_bridge_scope"] = {
        "source_kind": metadata["source_kind"],
        "fit_domains": ["camera_raw_mtf"],
        "unmeasured_parameters": [
            "dn_per_e",
            "read_noise_e",
            "dark_current_e_per_s",
            "full_well_e",
            "color_matrix",
            "tone",
            "reference_white_e",
        ],
        "placeholder_defaults_used": supplied is None,
        "assumptions": ASSUMPTIONS,
    }
    return validate_profile(p)


def _field(metadata, box):
    r, c, h, w = box
    origin = metadata["geometry"]["crop_origin"]
    full = metadata["geometry"]["full_sensor_shape"]
    return [
        2 * (origin[1] + c + (w - 1) / 2) / (full[1] - 1) - 1,
        2 * (origin[0] + r + (h - 1) / 2) / (full[0] - 1) - 1,
    ]


def _curve(metadata, capture, roi, campaign_id, run_id, chart):
    from .calibration_lab_contracts import DEFAULT_CONDITIONS, import_measurement

    metric = roi.get("raw", {}).get("Gr", {})
    if metric.get("status") != "ok":
        return None
    f = np.asarray(metric.get("frequency_cy_native_px"), dtype=float)
    sfr = np.asarray(metric.get("sfr"), dtype=float)
    if (
        f.ndim != 1
        or len(f) < 3
        or f.shape != sfr.shape
        or not np.isfinite(f).all()
        or not np.isfinite(sfr).all()
        or f[0] != 0
        or np.any(np.diff(f) <= 0)
        or f[-1] > 0.25
        or np.any(sfr < 0)
    ):
        raise IntegrityError("E-SFR Gr curve is not a finite native-pixel [0,0.25] observation")
    # ISO slanted-edge tangent and frequency direction differ by 90 degrees.
    tangent = float(metric["edge_angle_deg"])
    normal = float(metric.get("frequency_direction_deg", (tangent + 90) % 180))
    if not np.isfinite(normal) or abs(((normal - tangent - 90 + 90) % 180) - 90) > 1e-7:
        raise IntegrityError("E-SFR edge tangent and frequency direction disagree")
    field = _field(metadata, roi["box"])
    role = {"development": "training", "validation": "final_validation"}[metadata["role"]]
    parents = list(
        dict.fromkeys(
            [
                *metadata.get("parent_acquisition_ids", []),
                "raw-sha256:" + capture["files"]["raw"]["sha256"],
                "native-pixels-sha256:" + capture["native_pixel_sha256"],
            ]
        )
    )
    fmt = metadata["raw_format"]
    levels = fmt["black_level_dn"]
    gr_black = float(levels[1] if isinstance(levels, list) else levels)
    data = {
        "campaign_id": campaign_id,
        "chart": "mtf",
        "source_kind": metadata["source_kind"],
        "physical_instance": metadata["camera_id"],
        "session_id": metadata["session_id"],
        "acquisition_id": metadata["acquisition_id"],
        "parent_acquisition_ids": parents,
        "acquisition_group_id": metadata["acquisition_id"],
        "role": role,
        "conditions": {
            k: v
            for k, v in metadata["conditions"].items()
            if k in {*DEFAULT_CONDITIONS, "exposure_s"}
        },
        "measurement_domain": "camera_raw",
        "observations": {},
        "analysis": {
            "rois": [copy.deepcopy(roi["box"])],
            "field_xy": field,
            "field_coordinate_domain": "sensor_normalized",
            "orientation_deg": normal,
            "channel": "G1",
        },
        "raw_format": {
            "cfa": fmt["cfa"],
            "bit_depth": fmt["bit_depth"],
            "black_level_dn": gr_black,
            "response_type": "linear",
        },
        "geometry": metadata["geometry"],
        "method": {"id": "external_esfr_native_gr_sfr", "version": "1"},
        "uncertainty": {
            "kind": "unknown",
            "reason": "No traceable chart or instrument uncertainty supplied",
        },
        "instrument": {
            "id": metadata["camera_id"],
            "calibration_revision": metadata.get("instrument", {}).get(
                "calibration_revision", "not_provided_unverified"
            ),
        },
        "reference": {
            "id": chart.get("id", "owned-esfr-2023"),
            "source_uri": f"/api/v1/camera-validation/esfr/studies/{run_id}",
            "method": "Owned chart response includes chart MTF; physical reference not certified",
        },
        "units": {"frequency_cy_px": "cy/px", "mtf": "1"},
    }
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["frequency_cy_px[cy/px]", "mtf[1]"])
    writer.writerows(zip(f.tolist(), sfr.tolist(), strict=True))
    csv_bytes = stream.getvalue().encode()
    parsed = import_measurement(data, csv_bytes, "esfr-gr-sfr.csv")
    parsed["provenance"].update(
        {
            "esfr_run_id": run_id,
            "capture_id": capture["id"],
            "capture_sha256": capture["content_hash"],
            "roi_id": roi["id"],
            "field_id": roi.get("field_id", roi["id"]),
            "native_roi": copy.deepcopy(roi["box"]),
            "source_metadata": copy.deepcopy(metadata),
            "source_files": copy.deepcopy(capture["files"]),
            "source_frame_hashes": [
                capture["native_pixel_sha256"],
                capture["files"]["raw"]["sha256"],
            ],
            "edge_tangent_deg": tangent,
            "frequency_direction_deg": normal,
            "operator_id": metric.get("method", "esfr_native_slanted_edge_v1"),
            "import_classification": "external_camera_raw_curve_not_legacy_image_operator",
        }
    )
    parsed["source_frame_hashes"] = parsed["provenance"]["source_frame_hashes"]
    return data, parsed, csv_bytes


def _aggregate(plans, enabled):
    """Average repeats only within the same role and native ROI; retain every input."""
    from .calibration_lab import _lineage
    from .calibration_lab_contracts import import_measurement

    groups = {}
    for capture, metadata, parsed, csv_bytes in plans:
        key = digest(
            {
                "role": parsed["role"],
                "source_kind": parsed["source_kind"],
                "roi_id": parsed["provenance"]["roi_id"],
                "native_roi": parsed["provenance"]["native_roi"],
                "field_xy": parsed["observations"]["field_xy"],
                "conditions": parsed["conditions"],
                "capture_id": None if enabled else capture["id"],
            }
        )
        groups.setdefault(key, []).append((capture, metadata, parsed, csv_bytes))
    result = []
    for group in groups.values():
        capture, metadata, first, _ = group[0]
        metadata = copy.deepcopy(metadata)
        frequencies = first["observations"]["frequency_cy_px"]
        if any(row[2]["observations"]["frequency_cy_px"] != frequencies for row in group):
            raise ValueError("Repeat SFR frequency grids differ; no implicit curve resampling")
        angles = np.asarray([row[2]["observations"]["orientation_deg"] for row in group])
        vector = np.exp(2j * np.radians(angles)).mean()
        angle = float(np.degrees(np.angle(vector)) / 2 % 180)
        spread = float(np.max(np.abs((angles - angle + 90) % 180 - 90)))
        if spread > 1:
            raise ValueError("Repeated ROI frequency directions differ by more than one degree")
        curves = np.asarray([row[2]["observations"]["mtf"] for row in group])
        mean = curves.mean(axis=0)
        std = curves.std(axis=0, ddof=1).tolist() if len(group) > 1 else None
        metadata["analysis"]["orientation_deg"] = angle
        metadata["method"] = {
            "id": "external_esfr_repeat_mean_gr_sfr" if enabled else "external_esfr_native_gr_sfr",
            "version": "1",
        }
        if len(group) > 1:
            identity = digest(
                {
                    "run": first["provenance"]["esfr_run_id"],
                    "role": first["role"],
                    "roi": first["provenance"]["roi_id"],
                }
            )[:24]
            metadata["acquisition_id"] = "esfr-repeat-mean-" + identity
            metadata["acquisition_group_id"] = metadata["acquisition_id"]
            metadata["session_id"] = "esfr-repeat-mean-session-" + identity
        metadata["parent_acquisition_ids"] = list(
            dict.fromkeys(
                x
                for _, _, p, _ in group
                for x in [p["acquisition_id"], *p["parent_acquisition_ids"]]
            )
        )
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["frequency_cy_px[cy/px]", "mtf[1]"])
        writer.writerows(zip(frequencies, mean.tolist(), strict=True))
        csv_bytes = stream.getvalue().encode()
        parsed = import_measurement(metadata, csv_bytes, "esfr-gr-sfr.csv")
        provenance = parsed["provenance"]
        parsed["provenance"] = {**copy.deepcopy(first["provenance"]), **provenance}
        hashes = sorted({x for _, _, p, _ in group for x in p["source_frame_hashes"]})
        parsed["source_frame_hashes"] = hashes
        parsed["provenance"].update(
            {
                "source_frame_hashes": hashes,
                "aggregation_method": "arithmetic_mean_all_same_role_native_roi_repeats"
                if enabled
                else "individual_capture",
                "constituent_count": len(group),
                "frequency_direction_deg": angle,
                "max_direction_deviation_deg": spread,
                "frequency_cy_native_px": frequencies,
                "repeat_sample_std_sfr": std,
                "repeat_std_interpretation": "sample spread, not traceable measurement uncertainty",
                "constituents": [
                    {
                        "capture_id": c["id"],
                        "capture_sha256": c["content_hash"],
                        "metadata": c["metadata"],
                        "source_files": c["files"],
                        "native_pixel_sha256": c["native_pixel_sha256"],
                        "observations": p["observations"],
                        "operator": p["method"],
                    }
                    for c, _, p, _ in group
                ],
            }
        )
        original_tokens = set().union(
            *[
                _lineage({**p, "artifacts": [c["files"]["raw"], c["files"]["isp"]]})
                for c, _, p, _ in group
            ]
        )
        parsed["constituent_lineage_tokens"] = sorted(original_tokens)
        result.append(([row[0] for row in group], metadata, parsed, csv_bytes))
    return result


def _save_measurement_plans(service, campaign, plans, *, stable_key=None):
    from . import calibration_lab as lab, esfr_lab

    saved = []
    for sources, metadata, parsed, csv_bytes in plans:
        metadata["campaign_id"] = campaign["id"]
        parsed["campaign_id"] = campaign["id"]
        metadata_bytes = canonical(metadata).encode()
        parsed["provenance"]["metadata_sha256"] = digest(metadata)
        artifacts = [
            service.registry.store_blob(
                metadata_bytes, "measurement-metadata.json", "application/json"
            ),
            service.registry.store_blob(csv_bytes, "esfr-gr-sfr.csv", "text/csv"),
        ]
        for capture in sources:
            artifacts.extend(
                [
                    capture["files"]["raw"],
                    capture["files"]["isp"],
                    service.registry.store_blob(
                        canonical(capture["metadata"]).encode(),
                        "original-capture-metadata.json",
                        "application/json",
                    ),
                ]
            )
        artifacts.append(
            service.registry.store_blob(
                canonical(parsed["provenance"]).encode(),
                "esfr-curve-provenance.json",
                "application/json",
            )
        )
        data = {
            **parsed,
            "artifacts": lab._unique_artifacts(artifacts),
            "schema": "camera_calibration_measurement_v1",
            "campaign_sha256": campaign["content_hash"],
        }
        data["lineage_tokens"] = sorted(
            lab._lineage(data) | set(parsed["constituent_lineage_tokens"])
        )
        lab._conditions_match(campaign, data)
        identity = None
        if stable_key:
            identity = "calibration_lab_measurement-" + digest({"key": stable_key, "data": data})[:32]
            try:
                existing = lab._measurement(service, identity)
            except KeyError:
                pass
            else:
                if {k: v for k, v in existing.items() if k not in {"id", "created_at", "content_hash"}} != data:
                    raise IntegrityError("Partial validation import differs from its deterministic input")
                saved.append(existing)
                continue
        saved.append(
            service.registry.put_verified_record(
                lab.MEASUREMENTS,
                data,
                [(lab.CAMPAIGNS, campaign), *[(esfr_lab.CAPTURES, c) for c in sources]],
                identity=identity,
            )
        )
    return saved


def _study_readiness(campaign, saved):
    """Evaluate current fitter admission without changing any measured curve."""
    from .calibration_lab_contracts import measurement_resource_budget, study_measurement_ids

    train_rows = [m for m in saved if m["role"] == "training"]
    validation_rows = [m for m in saved if m["role"] == "final_validation"]
    training = [m["id"] for m in train_rows]
    validation = [m["id"] for m in validation_rows]
    blockers, budget = [], {}
    for label, rows in (("training", train_rows), ("validation", validation_rows)):
        if not rows:
            if label == "training":
                blockers.append("Export requires at least one usable training observation")
            continue
        try:
            study_measurement_ids([m["id"] for m in rows])
            budget[label] = measurement_resource_budget(rows)
        except ValueError as exc:
            blockers.append(f"{label} resource budget: {exc}")
    curves = [np.asarray(m["observations"]["mtf"], dtype=float) for m in train_rows]
    if any(not np.all(np.isfinite(curve)) or np.any(curve < 0)
           or not np.isclose(curve[0], 1, atol=1e-8) for curve in curves):
        blockers.append(
            "Forward RAW SFR requires finite nonnegative observations and DC=1; "
            "original observations are never clipped or silently normalized."
        )
    return {
        "readiness_version": READINESS_VERSION,
        "training_ids": training,
        "validation_ids": validation,
        "resource_budget": budget,
        "sfr_observation_scope": {
            "overshoot_measurement_count": sum(bool(np.any(curve > 1)) for curve in curves),
            "overshoot_preserved": True,
            "overshoot_fit_admission": "verified_native_forward_observer_only",
        },
        "field_position_count": len({tuple(m["observations"]["field_xy"]) for m in train_rows}),
        "study_request": None if blockers else {
            "name": campaign["name"],
            "campaign_id": campaign["id"],
            "measurement_ids": training,
            "base_profile_id": "baseline",
            "seed": 17,
            "max_iterations": 100,
            "fit_method": "esfr_forward_observer_v1",
        },
        "validation_request": {"measurement_ids": validation} if validation else None,
        "study_blockers": blockers,
        "status": "export_only_limits" if blockers else "ready_for_mtf_study",
    }


def _existing_export(service, run_id, aggregate_repeats, summary):
    """Append a new readiness revision for old exports; never rewrite source records."""
    from . import calibration_lab as lab

    for prior in service.registry.list(EXPORTS):
        if prior["run_id"] != run_id or prior.get("aggregate_repeats") != aggregate_repeats:
            continue
        if prior["summary_sha256"] != digest(summary):
            raise IntegrityError("Previously exported E-SFR owner has changed")
        campaign = service.registry.get(lab.CAMPAIGNS, prior["campaign_id"])
        ids = prior["measurement_ids"]
        if len(ids) != len(set(ids)):
            raise IntegrityError("Previously exported measurements contain duplicates")
        saved = [lab._measurement(service, identity) for identity in ids]
        for item in saved:
            if item["campaign_id"] != campaign["id"]:
                raise IntegrityError("Previously exported measurement campaign differs")
            lab._conditions_match(campaign, item)
        if prior.get("validation_contract_id"):
            validation_contract_for_source(service, run_id, prior["parent_study_id"])
            return prior
        readiness = _study_readiness(campaign, saved)
        if (
            readiness["training_ids"] != prior["training_ids"]
            or readiness["validation_ids"] != prior["validation_ids"]
            or len(readiness["training_ids"]) + len(readiness["validation_ids"]) != len(ids)
        ):
            raise IntegrityError("Previously exported measurement roles differ")
        if all(prior.get(key) == value for key, value in readiness.items()):
            return prior
        document = {
            key: copy.deepcopy(value) for key, value in prior.items()
            if key not in {"id", "created_at", "content_hash"}
        }
        document.update(readiness)
        document.update(
            supersedes_export_id=prior["id"],
            supersedes_export_sha256=prior["content_hash"],
            refresh_reason="Re-evaluated fitter admission using resource budgets; "
            "all original measurement IDs and curve bytes are unchanged",
        )
        return service.registry.put_verified_record(
            EXPORTS, document,
            [(EXPORTS, prior), (lab.CAMPAIGNS, campaign), *[(lab.MEASUREMENTS, m) for m in saved]],
        )
    return None


def export(service, run_id, *, aggregate_repeats=True):
    """Export a completed immutable E-SFR study into a new, MTF-only Lab campaign.

    No model is applied and no study automatically submitted. Repeated calls
    return the verified existing export. All original capture artifacts are bound
    to measurements; different ROIs from one acquisition retain shared lineage.
    """
    from . import calibration_lab as lab
    from . import esfr_lab

    if type(aggregate_repeats) is not bool:
        raise ValueError("aggregate_repeats must be a boolean")
    row = esfr_lab.get(service, run_id)
    if row["status"] != "succeeded":
        raise ValueError("Only a completed E-SFR study can be exported")
    summary = row["result"]["summary"]
    for snapshot in row["capture_snapshots"]:
        if esfr_lab.capture(service, snapshot["id"]) != snapshot:
            raise IntegrityError("E-SFR capture snapshot differs from immutable source")
    with lab._lock(service):
        existing = _existing_export(service, run_id, aggregate_repeats, summary)
        if existing is not None:
            return existing
    observations = summary["observations"]
    if summary.get("measurement_details_file"):
        from .esfr_lab import file

        details = json.loads(file(service, run_id, summary["measurement_details_file"]).read_text())
        observations = details["observations"]
    captures = {}
    for snapshot in row["capture_snapshots"]:
        current = esfr_lab.capture(service, snapshot["id"])
        if current != snapshot:
            raise IntegrityError("E-SFR capture snapshot differs from immutable source")
        for key in ("raw", "isp"):
            esfr_lab._binding(service, current, key)
        captures[current["id"]] = current
    profile = baseline_profile(
        next(iter(captures.values()))["metadata"],
        row["specification"].get("baseline_sigma_native_px", 1.5),
    )
    spec = row["specification"]
    plans, skipped, seen = [], [], set()
    for observation in observations:
        capture = captures.get(observation["capture_id"])
        if capture is None:
            raise IntegrityError("Observation references a capture outside its owner")
        metadata = capture["metadata"]
        if (
            observation["role"] != metadata["role"]
            or observation["source_kind"] != metadata["source_kind"]
        ):
            raise IntegrityError("Observation source kind or role differs from original capture")
        for roi in observation["rois"]:
            if roi["kind"] != "edge":
                continue
            identity = (capture["id"], roi["id"])
            if identity in seen:
                raise IntegrityError("Duplicate capture/ROI observation")
            seen.add(identity)
            result = _curve(metadata, capture, roi, "pending", run_id, spec["chart_profile"])
            if result is None:
                skipped.append(
                    {
                        "capture_id": capture["id"],
                        "roi_id": roi["id"],
                        "reason": "RAW Gr metric unavailable; not replaced with zeros",
                    }
                )
            else:
                plans.append((capture, *result))
    if not plans or not any(p[2]["role"] == "training" for p in plans):
        raise ValueError("Export requires at least one usable development RAW Gr edge")
    source_kind = {p[2]["source_kind"] for p in plans}
    if len(source_kind) != 1:
        raise IntegrityError("One E-SFR campaign cannot mix measured and synthetic source kinds")
    # Preflight existing and new role isolation before creating a campaign.
    prepared = []
    for capture, _, parsed, _ in plans:
        prepared.append({**parsed, "artifacts": [capture["files"]["raw"], capture["files"]["isp"]]})
    check_campaign = {
        "camera_id": next(iter(captures.values()))["metadata"]["camera_id"],
        "native_shape": profile["native_shape"],
        "conditions": profile["conditions"],
        "baseline_profile": profile,
    }
    for item in prepared:
        lab._conditions_match(check_campaign, item)
    plans = _aggregate(plans, aggregate_repeats)
    with lab._lock(service):
        existing = _existing_export(service, run_id, aggregate_repeats, summary)
        if existing is not None:
            return existing
        existing = service.registry.list(lab.MEASUREMENTS)
        for i, item in enumerate(prepared):
            tokens = lab._lineage(item)
            for old in [*existing, *prepared[:i]]:
                old_tokens = (
                    set(old["lineage_tokens"]) if "lineage_tokens" in old else lab._lineage(old)
                )
                if item["role"] != old["role"] and tokens & old_tokens:
                    raise ConflictError(
                        "Export would reuse source/session/acquisition across "
                        "training and validation"
                    )
        campaign = lab.create_campaign(
            service,
            {
                "name": "E-SFR export · " + row.get("name", run_id),
                "camera_id": next(iter(captures.values()))["metadata"]["camera_id"],
                "native_shape": profile["native_shape"],
                "pixel_pitch_um": profile["pixel_pitch_um"],
                "conditions": profile["conditions"],
                "baseline_profile": profile,
                "thresholds": {},
                "source": spec.get("source"),
            },
        )
        saved = _save_measurement_plans(service, campaign, plans)
        readiness = _study_readiness(campaign, saved)
        query = {"campaign": campaign["id"]}
        if spec.get("source"):
            query.update(
                {
                    "source_run": spec["source"]["run_id"],
                    "candidate": spec["source"]["candidate_id"],
                    "condition": spec["source"].get("condition_id", ""),
                }
            )
        document = {
            "schema": "camera_esfr_calibration_export_v1",
            "run_id": run_id,
            "aggregate_repeats": aggregate_repeats,
            "aggregation_method": "arithmetic_mean_all_same_role_native_roi_repeats"
            if aggregate_repeats
            else "individual_capture",
            "summary_sha256": digest(summary),
            "campaign_id": campaign["id"],
            "measurement_ids": [m["id"] for m in saved],
            **readiness,
            "source_kind": next(iter(source_kind)),
            "source_context": row.get("source_context"),
            "source": spec.get("source"),
            "calibration_lab_url": "/calibration-lab.html?" + urlencode(query),
            "fit_domains": ["camera_raw_mtf"],
            "assumptions": ASSUMPTIONS,
            "skipped": skipped,
            "physical_validation": "not_established",
        }
        return service.registry.put_verified_record(
            EXPORTS, document, [(lab.CAMPAIGNS, campaign), *[(lab.MEASUREMENTS, m) for m in saved]]
        )


def _capture_context(metadata):
    """Capture interpretation is frozen; timestamps/frame IDs remain per acquisition."""
    if any(key not in metadata for key in MEASURED_CONTEXT_FIELDS):
        raise ValueError("Measured acquisition contract requires complete sensor/capture context")
    return {key: copy.deepcopy(metadata[key]) for key in MEASURED_CONTEXT_FIELDS}


def _aware_time(value, field):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field + " requires an ISO8601 timestamp with a timezone") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(field + " requires an ISO8601 timestamp with a timezone")
    return stamp


def _measured_contract_fields(source, parent):
    """Bind measured-only development and the exact forward observation method."""
    if source.get("validation_ids"):
        raise ValueError("Measured fresh-holdout calibration must begin with development only")
    captures = source["capture_snapshots"]
    if (len(captures) != 3 or {c["id"] for c in captures} != set(source["development_ids"])
            or any(c["metadata"].get("role") != "development"
                   or c["metadata"].get("source_kind") != "measured" for c in captures)):
        raise ValueError("Measured contract requires exactly three measured development captures")
    context = _capture_context(captures[0]["metadata"])
    if any(_capture_context(c["metadata"]) != context for c in captures):
        raise IntegrityError("Measured development capture context differs")
    if parent.get("specification", {}).get("fit_method") != "esfr_forward_observer_v1":
        raise ValueError("Measured fresh validation requires the development RAW forward observer")
    candidate = parent["result"]["summary"]["candidate_profile"]
    binding = candidate.get("esfr_forward_model", {})
    forward = binding.get("profile", {})
    edges = {roi["id"] for roi in source["specification"]["rois"] if roi["kind"] == "edge"}
    if (binding.get("fit_data_role") != "development_only"
            or binding.get("method") != "development_native_bayer_same_observer_gaussian_v1"
            or binding.get("profile_sha256") != digest(forward)
            or forward.get("source_kind") != "measured"
            or forward.get("development_capture_ids") != source["development_ids"]
            or len(edges) != 112 or set(forward.get("fields", {})) != edges
            or set(forward.get("geometry", {}).get("roi_nuisance", {})) != edges):
        raise IntegrityError("Measured candidate lacks its frozen development forward binding")
    return {
        "schema": MEASURED_CONTRACT_SCHEMA,
        "source_kind": "measured",
        "capture_context": context,
        "capture_context_sha256": digest(context),
        "fit_method": parent["specification"]["fit_method"],
        "forward_method": binding["method"],
        "forward_profile_sha256": binding["profile_sha256"],
        "development_geometry_sha256": digest(forward["geometry"]),
        "source_code_sha256": digest(parent["source_code"]),
        "contract_scope": "candidate_and_capture_geometry_frozen_before_operator_declared_new_measured_acquisition",
        "acquisition_evidence": "operator_declared_not_authenticated",
        "physical_identity_verified": False,
    }


def create_validation_contract(service, parent_study_id):
    """Freeze the candidate and acquisition conditions before any new holdout exists."""
    from . import calibration_lab as lab, esfr_lab

    parent = lab.get(service, parent_study_id)
    if parent["status"] != "succeeded" or parent["job_kind"] != lab.JOB_KIND:
        raise ValueError("Fresh holdout acquisition requires a completed frozen calibration candidate")
    summary = parent["result"]["summary"]
    if summary["candidate_sha256"] != digest(summary["candidate_profile"]):
        raise IntegrityError("Frozen candidate profile hash changed")
    training = parent["measurement_snapshots"]
    if any(m["role"] != "training" for m in training):
        raise ValueError("Fresh E-SFR contract requires training-only parent inputs")
    origins = {m.get("provenance", {}).get("esfr_run_id") for m in training}
    if None in origins or len(origins) != 1:
        raise ValueError("Parent training must bind one E-SFR source study")
    source = esfr_lab.get(service, next(iter(origins)))
    edges = {r["id"] for r in source["specification"]["rois"] if r["kind"] == "edge"}
    if {m["provenance"]["roi_id"] for m in training} != edges or len(training) != len(edges):
        raise ValueError("Fresh holdout contract requires every source edge exactly once")
    if set(summary["fit"].get("training_measurement_ids", [])) != {m["id"] for m in training}:
        raise IntegrityError("Frozen fit did not use every declared development edge")
    if len(source["development_ids"]) != 3:
        raise ValueError("This fresh chart contract requires exactly three source development captures")
    document = {
        "schema": "camera_esfr_frozen_holdout_contract_v1",
        "parent_study_id": parent_study_id,
        "parent_result_sha256": digest(parent["result"]),
        "candidate_sha256": summary["candidate_sha256"],
        "campaign_id": parent["campaign_snapshot"]["id"],
        "training_ids": [m["id"] for m in training],
        "source_study_id": source["id"],
        "source_result_sha256": digest(source["result"]),
        "source": source["specification"].get("source"),
        "development_capture_ids": source["development_ids"],
        "required_validation_capture_count": 3,
        "rois": source["specification"]["rois"],
        "chart_profile": source["specification"]["chart_profile"],
        "thresholds": source["specification"]["thresholds"],
        "baseline_sigma_native_px": source["specification"]["baseline_sigma_native_px"],
        "contract_scope": "candidate_frozen_before_three_new_synthetic_noise_realizations",
        "physical_validation": "not_established",
    }
    kinds = {c["metadata"]["source_kind"] for c in source["capture_snapshots"]}
    if kinds == {"measured"}:
        document.update(_measured_contract_fields(source, parent))
    elif kinds != {"synthetic"}:
        raise ValueError("Frozen holdout source must have one explicit measured or synthetic kind")
    with lab._lock(service):
        for existing in service.registry.list(VALIDATION_CONTRACTS):
            if existing["parent_study_id"] == parent_study_id:
                if any(existing.get(k) != v for k, v in document.items()):
                    raise IntegrityError("Existing validation contract differs from frozen inputs")
                return existing
        return service.registry.put_verified_record(
            VALIDATION_CONTRACTS, document,
            [(lab.CAMPAIGNS, parent["campaign_snapshot"]), *[(lab.MEASUREMENTS, m) for m in training]],
        )


def _verify_validation_contract(service, identity, parent_study_id):
    from . import calibration_lab as lab, esfr_lab

    contract = service.registry.get(VALIDATION_CONTRACTS, identity)
    parent = lab.get(service, parent_study_id)
    source = esfr_lab.get(service, contract["source_study_id"])
    if (
        contract["parent_study_id"] != parent_study_id
        or parent["status"] != "succeeded"
        or digest(parent["result"]) != contract["parent_result_sha256"]
        or parent["result"]["summary"]["candidate_sha256"] != contract["candidate_sha256"]
        or digest(source["result"]) != contract["source_result_sha256"]
        or contract["training_ids"] != [m["id"] for m in parent["measurement_snapshots"]]
    ):
        raise IntegrityError("Fresh validation contract differs from its frozen parent/source")
    if datetime.fromisoformat(contract["created_at"]) < datetime.fromisoformat(parent["updated_at"]):
        raise IntegrityError("Holdout contract predates completed candidate")
    if contract.get("source_kind") == "measured":
        expected = _measured_contract_fields(source, parent)
        if any(contract.get(key) != value for key, value in expected.items()):
            raise IntegrityError("Measured holdout contract capture context or method changed")
    return contract, parent


def _verify_measured_metadata(metadata, contract, *, imported_at=None):
    if (contract.get("schema") != MEASURED_CONTRACT_SCHEMA
            or contract.get("source_kind") != "measured"
            or metadata.get("source_kind") != "measured" or metadata.get("role") != "validation"):
        raise ValueError("Measured validation requires a measured-only frozen acquisition contract")
    if metadata.get("validation_contract") != {"id": contract["id"], "content_hash": contract["content_hash"]}:
        raise IntegrityError("Measured capture is not bound to the frozen acquisition contract")
    if _capture_context(metadata) != contract["capture_context"]:
        raise IntegrityError("Measured validation changed frozen sensor, conditions, ISP or sampling")
    declared = _aware_time(metadata.get("captured_at"), "captured_at")
    end = _aware_time(imported_at, "imported_at") if imported_at else datetime.now(timezone.utc)
    if not _aware_time(contract["created_at"], "contract created_at") <= declared <= end:
        raise IntegrityError("Declared measured capture time must follow candidate freeze and precede upload")
    receipt = metadata.get("acquisition_attestation", {})
    operator = receipt.get("operator_id") if isinstance(receipt, dict) else None
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 160:
        raise ValueError("Measured validation needs a bounded operator_id acquisition attestation")
    for key in ("captured_after_contract", "original_raw_isp_pair", "camera_and_chart_unchanged"):
        if receipt.get(key) is not True:
            raise ValueError("Measured acquisition attestation must explicitly confirm " + key)
    return contract


def verify_measured_validation_metadata(service, metadata):
    """Pre-upload admission; operator declarations never authenticate physical identity."""
    binding = metadata.get("validation_contract", {})
    if not isinstance(binding, dict) or set(binding) != {"id", "content_hash"}:
        raise ValueError("Measured validation upload requires the frozen validation_contract binding")
    record = service.registry.get(VALIDATION_CONTRACTS, binding["id"])
    contract, _ = _verify_validation_contract(service, binding["id"], record["parent_study_id"])
    return _verify_measured_metadata(metadata, contract)


def _verify_fresh_source(service, row, contract):
    from . import esfr_lab

    spec = row["specification"]
    if row["status"] != "succeeded" or spec.get("fit") is not False:
        raise ValueError("Fresh holdout source must be a completed fit:false measurement-only study")
    if row["development_ids"] != contract["development_capture_ids"]:
        raise IntegrityError("Fresh source changed the original development captures")
    for key in ("rois", "chart_profile", "thresholds", "baseline_sigma_native_px", "source"):
        if spec.get(key) != contract.get(key):
            raise IntegrityError("Fresh source changed frozen " + key)
    kind = contract.get("source_kind", "synthetic")
    if kind == "measured" and digest(row["source_code"]) != contract["source_code_sha256"]:
        raise IntegrityError("Measured validation observation source code changed after candidate freeze")
    ids = row["validation_ids"]
    if len(ids) != contract["required_validation_capture_count"] or len(set(ids)) != len(ids):
        raise ValueError("Fresh holdout must contain exactly three unique validation captures")
    by_id = {c["id"]: c for c in row["capture_snapshots"]}
    development_lineage = set(row["development_lineage"])
    fresh = []
    for identity in ids:
        capture = esfr_lab.capture(service, identity)
        if capture != by_id[identity]:
            raise IntegrityError("Fresh capture differs from source snapshot")
        meta = capture["metadata"]
        binding = meta.get("validation_contract", {})
        if binding != {"id": contract["id"], "content_hash": contract["content_hash"]}:
            raise IntegrityError("Fresh capture is not bound to the frozen acquisition contract")
        if meta["role"] != "validation" or meta["source_kind"] != kind:
            raise ValueError("Fresh validation source kind differs from its frozen contract")
        if kind == "measured":
            _verify_measured_metadata(meta, contract, imported_at=capture["created_at"])
        else:
            stamp = datetime.fromisoformat(meta["generated_at"])
            if not datetime.fromisoformat(contract["created_at"]) <= stamp <= datetime.fromisoformat(capture["created_at"]):
                raise IntegrityError("Fresh capture was not generated after candidate freeze")
        if esfr_lab._lineage(capture) & development_lineage:
            raise IntegrityError("Fresh holdout overlaps development lineage")
        fresh.append(capture)
    if (
        len({c["native_pixel_sha256"] for c in fresh}) != len(fresh)
        or len({c["isp_pixel_sha256"] for c in fresh}) != len(fresh)
    ):
        raise IntegrityError("The three fresh captures must have distinct native RAW and ISP pixels")
    fresh_ids = set(ids)
    companion_ids = set()
    if spec.get("iq_validation_admission_id"):
        from .esfr_iq_service import mtf_noise_companions
        companion_ids = mtf_noise_companions(service, spec["iq_validation_admission_id"], contract, ids)
    fresh_lineage = set().union(*(esfr_lab._lineage(c) for c in fresh))
    for old in service.registry.list(esfr_lab.CAPTURES):
        if old["id"] not in fresh_ids | companion_ids and fresh_lineage & esfr_lab._lineage(old):
            raise ConflictError("Fresh holdout bytes or acquisition lineage duplicate an existing capture")
    # The measurement-only source may expose the new pixels once, after freeze.
    # No earlier E-SFR fit/replay is allowed to have used them.
    for prior in service.registry.list_runs(job_kinds={esfr_lab.KIND}):
        if prior["id"] != row["id"] and fresh_ids & set(prior.get("development_ids", []) + prior.get("validation_ids", [])):
            raise ConflictError("Fresh validation captures were already exposed in another E-SFR study")
    return fresh


def export_validation(service, run_id, *, parent_study_id, validation_contract_id):
    """Append only fresh holdout curves to the frozen candidate's existing campaign."""
    from . import calibration_lab as lab, esfr_lab

    contract, parent = _verify_validation_contract(service, validation_contract_id, parent_study_id)
    row = esfr_lab.get(service, run_id)
    captures = _verify_fresh_source(service, row, contract)
    campaign = parent["campaign_snapshot"]
    summary = row["result"]["summary"]
    observations = summary["observations"]
    if summary.get("measurement_details_file"):
        observations = json.loads(esfr_lab.file(service, run_id, summary["measurement_details_file"]).read_text())["observations"]
    by_id = {c["id"]: c for c in captures}
    edge_ids = {r["id"] for r in contract["rois"] if r["kind"] == "edge"}
    plans, seen = [], set()
    for observation in observations:
        if observation["capture_id"] not in by_id:
            continue
        capture = by_id[observation["capture_id"]]
        if observation["role"] != "validation" or observation["source_kind"] != contract.get("source_kind", "synthetic"):
            raise IntegrityError("Fresh observation role/source differs")
        for roi in observation["rois"]:
            if roi["kind"] != "edge":
                continue
            key = (capture["id"], roi["id"])
            if key in seen or roi["id"] not in edge_ids:
                raise IntegrityError("Fresh observation edge is duplicate or unregistered")
            seen.add(key)
            curve = _curve(capture["metadata"], capture, roi, campaign["id"], run_id, contract["chart_profile"])
            if curve is None:
                raise ValueError("Every fresh holdout edge must have an available RAW Gr curve")
            plans.append((capture, *curve))
    if seen != {(c["id"], edge) for c in captures for edge in edge_ids}:
        raise IntegrityError("Fresh source does not cover every frozen edge in every capture")
    plans = _aggregate(plans, True)
    # Complete semantic admission before publishing any measurement revision.
    for _, _, parsed, _ in plans:
        lab._conditions_match(campaign, parsed)
    with lab._lock(service):
        for existing in service.registry.list(EXPORTS):
            if existing.get("validation_contract_id") != validation_contract_id:
                continue
            if existing["run_id"] != run_id or existing["parent_study_id"] != parent_study_id:
                raise ConflictError("This frozen acquisition contract already has a different holdout source")
            for identity in existing["measurement_ids"]:
                lab._measurement(service, identity)
            return existing
        incoming = set().union(*(set(p[2]["constituent_lineage_tokens"]) for p in plans))
        for old in service.registry.list(lab.MEASUREMENTS):
            if incoming & set(old["lineage_tokens"]):
                if (old["campaign_id"] == campaign["id"] and old["role"] == "final_validation"
                        and old.get("provenance", {}).get("esfr_run_id") == run_id):
                    # Recover the same deterministic partial publication after
                    # an interrupted import; no record is replaced or relabelled.
                    continue
                raise ConflictError("Fresh holdout lineage has already been exported")
        saved = _save_measurement_plans(service, campaign, plans, stable_key={
            "validation_contract_id": validation_contract_id, "source_run_id": run_id,
            "parent_study_id": parent_study_id})
        training = parent["measurement_snapshots"]
        readiness = _study_readiness(campaign, [*training, *saved])
        query = dict(campaign=campaign["id"], study=parent_study_id)
        if contract["source"]:
            query.update(source_run=contract["source"]["run_id"], candidate=contract["source"]["candidate_id"], condition=contract["source"].get("condition_id", ""))
        document = {
            "schema": "camera_esfr_calibration_export_v1", "run_id": run_id,
            "aggregate_repeats": True, "aggregation_method": "arithmetic_mean_all_same_role_native_roi_repeats",
            "summary_sha256": digest(summary), "campaign_id": campaign["id"],
            "measurement_ids": [m["id"] for m in [*training, *saved]], **readiness,
            "study_request": None, "status": "ready_for_frozen_validation",
            "parent_study_id": parent_study_id, "candidate_sha256": contract["candidate_sha256"],
            "validation_contract_id": contract["id"], "validation_contract_sha256": contract["content_hash"],
            "new_validation_capture_ids": row["validation_ids"],
            "source": contract["source"], "source_context": row.get("source_context"),
            "source_kind": contract.get("source_kind", "synthetic"), "fit_domains": ["camera_raw_mtf"],
            "calibration_lab_url": "/calibration-lab.html?" + urlencode(query),
            "assumptions": ASSUMPTIONS, "skipped": [], "physical_validation": "not_established",
        }
        return service.registry.put_verified_record(EXPORTS, document,
            [(VALIDATION_CONTRACTS, contract), (lab.CAMPAIGNS, campaign),
             *[(lab.MEASUREMENTS, m) for m in [*training, *saved]]])


def validation_contract_for_source(service, source_run, parent_study_id):
    """Verify the exact new holdout contract used by a frozen candidate replay."""
    from . import esfr_lab

    matches = [r for r in service.registry.list(EXPORTS)
               if r["run_id"] == source_run and r.get("parent_study_id") == parent_study_id
               and r.get("validation_contract_id")]
    if not matches:
        return None
    record = matches[0]
    contract, _ = _verify_validation_contract(service, record["validation_contract_id"], parent_study_id)
    if record["validation_contract_sha256"] != contract["content_hash"]:
        raise IntegrityError("Export validation contract hash changed")
    source = esfr_lab.get(service, source_run)
    _verify_fresh_source(service, source, contract)
    if record["summary_sha256"] != digest(source["result"]["summary"]):
        raise IntegrityError("Fresh validation export summary changed")
    return {**contract, "new_validation_capture_ids": source["validation_ids"],
            "validation_ids": record["validation_ids"], "export_id": record["id"],
            "measurement_only_source_run_id": source_run}


def db_baseline(service, source, metadata, field_mapping=None):
    """Read exact legacy DB sampled kernels; never infer peripheral correspondences.

    ``field_mapping`` follows the existing optical conversion contract: a list of
    {source_field_id, field_xy:[x,y]} in normalized sensor coordinates. Omission
    means center-only. This helper does not create or activate registry models.
    """
    from . import calibration_db_optics_engine as engine
    from .calibration_db import source_selection
    from .calibration_db_optics import _field_mapping
    from .calibration_lab_engine import validate_profile
    from .reference_workshop_job import _directory

    profile = baseline_profile(metadata)
    if profile["pixel_pitch_um"] != 2.9:
        raise ValueError("Exact DB baseline adapter supports 2.9 um RGGB only; no pitch resampling")
    mapping = _field_mapping(field_mapping)
    row, context = source_selection(service, source)
    converted = engine.convert_source(
        _directory(service, row["id"]),
        row["result"]["summary"],
        context["candidate_id"],
        profile,
        condition_id=context["condition_id"],
        expected_manifest_sha256=context["manifest_sha256"],
        field_mapping=mapping,
    )
    if not converted.get("can_apply"):
        raise ValueError("DB sampled-kernel conversion did not qualify")
    identity = converted.get("source_identity", {})
    keys = ("run_id", "candidate_id", "condition_id", "manifest_sha256", "candidate_sha256")
    if any(identity.get(key) != context[key] for key in keys if key in context):
        raise IntegrityError("DB sampled kernels differ from the selected source identity")
    for key, value in converted["profile_patch"].get("conditions", {}).items():
        if profile["conditions"].get(key) != value:
            raise ValueError("DB source and measured capture condition differ: " + key)
    profile["blur"] = converted["profile_patch"]["blur"]
    nodes = profile["blur"]["field_nodes"]
    if any(n.get("family") != "sampled_native_kernel_v1" or "kernel" not in n for n in nodes):
        raise IntegrityError(
            "DB baseline must retain exact sampled kernels, not Gaussian approximations"
        )
    again, again_context = source_selection(service, source)
    if again != row or again_context != context:
        raise IntegrityError("DB baseline source changed during conversion")
    return {
        "schema": "camera_esfr_db_baseline_v1",
        "profile": validate_profile(profile),
        "source_context": context,
        "source_identity": converted["source_identity"],
        "source_files": converted["source_files"],
        "report": converted["report"],
        "field_mapping": mapping,
        "field_scope": "center_only" if mapping is None else "explicit_mapped_fields",
        "evidence_kind": "model_derived_not_measured",
        "physical_validation": "not_established",
    }

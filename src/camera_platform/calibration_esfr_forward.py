"""Development-only native-Bayer forward-observer refinement of legacy PSF nodes.

An analytical Gaussian transfer curve and a measured native Bayer SFR are not
the same operator. This module fits the latter by replaying the frozen edge
through the same pixel sampling, DN quantization and SFR observer as validation.
Every edge retains its own sensor position. One measured direction identifies
one effective width, not a rotated ellipse or a unique physical optical PSF.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import minimize_scalar

from . import esfr_engine
from .calibration_lab_engine import _mtf, validate_profile
from .esfr_analysis import edge_metrics
from .esfr_bridge import _field as native_field
from .esfr_io import RawFrame, read_isp
from .registry import digest

METHOD = "development_native_bayer_same_observer_gaussian_v1"
BINDING_SCHEMA = "camera_calibration_esfr_forward_binding_v1"


def _bound_inputs(candidate, measurements, captures, rois):
    """Reject wrong roles, missing edges or mixed provenance before image IO."""
    if not captures or any(c[0].get("metadata", {}).get("role") != "development" for c in captures):
        raise ValueError("Forward refinement accepts development captures only")
    if not measurements or any(m.get("role") != "training" for m in measurements):
        raise ValueError("Forward refinement accepts training measurements only")
    by_capture = {c[0]["id"]: c[0] for c in captures}
    if len(by_capture) != len(captures):
        raise ValueError("Development capture IDs must be unique")
    edges = [deepcopy(r) for r in rois if r["kind"] == "edge"]
    esfr_engine.validate_roi_budget(edges)
    by_roi = {r["id"]: r for r in edges}
    selected = {m.get("provenance", {}).get("roi_id"): m for m in measurements}
    if (not edges or len(by_roi) != len(edges) or len(selected) != len(measurements)
            or set(selected) != set(by_roi)
            or len({m["id"] for m in measurements}) != len(measurements)):
        raise ValueError("Forward refinement requires every native edge exactly once")
    first = captures[0][0]["metadata"]
    if (candidate["blur"].get("field_coordinate_domain", "sensor_normalized") != "sensor_normalized"
            or candidate["native_shape"] != first["geometry"]["full_sensor_shape"]
            or candidate["cfa"] != first["raw_format"]["cfa"]
            or candidate["pixel_pitch_um"] != first["pixel_pitch_um"]
            or candidate["electronics"]["bit_depth"] != first["raw_format"]["bit_depth"]
            or any(first["conditions"].get(k) != v for k, v in candidate["conditions"].items())):
        raise ValueError("Candidate profile differs from development sensor or conditions")
    for capture in by_capture.values():
        meta = capture["metadata"]
        if any(meta.get(k) != first.get(k) for k in (
                "camera_id", "conditions", "raw_format", "geometry", "source_kind", "pixel_pitch_um", "isp")):
            raise ValueError("Development captures require identical sensor, conditions and ISP mapping")
        if meta["isp"]["coordinate_mapping"] != "same_native_raster":
            raise ValueError("Forward refinement requires registered same-native-raster ISP")
    nodes = {tuple(n["field_xy"]): n for n in candidate["blur"]["field_nodes"]}
    for roi in edges:
        record = selected[roi["id"]]
        provenance = record["provenance"]
        xy = tuple(native_field(first, roi["box"]))
        if (provenance.get("native_roi") != roi["box"]
                or tuple(record["observations"]["field_xy"]) != xy or xy not in nodes):
            raise ValueError("Training field and candidate node must match the exact native ROI center")
        node = nodes[xy]
        if node.get("family", "centered_elliptical_gaussian_v1") != "centered_elliptical_gaussian_v1":
            raise ValueError("Forward refinement supports effective Gaussian nodes only")
        constituents = provenance.get("constituents", [])
        if (len(constituents) != len(captures)
                or {x["capture_id"] for x in constituents} != set(by_capture)):
            raise ValueError("Every training edge must retain every development repeat exactly once")
        for source in constituents:
            capture = by_capture[source["capture_id"]]
            if (source.get("metadata", {}).get("role") != "development"
                    or source["metadata"] != capture["metadata"]
                    or source["source_files"] != capture["files"]
                    or source.get("capture_sha256") != capture.get("content_hash")
                    or not capture.get("content_hash")):
                raise ValueError("Training constituent differs from its development capture binding")
        # Original star-field labels group different locations. They must never
        # merge the independent native edge positions in the legacy field map.
        roi["field_id"] = roi["id"]
    if len({tuple(native_field(first, r["box"])) for r in edges}) != len(edges):
        raise ValueError("Independent forward edges require distinct native field positions")
    return first, edges, selected, nodes


def _verify_file(path, descriptor):
    path = Path(path)
    if path.stat().st_size != descriptor["bytes"]:
        raise ValueError("Development image byte count differs from its frozen binding")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != descriptor["sha256"]:
        raise ValueError("Development image SHA differs from its frozen binding")


def _metrics(values):
    before, after = [v[0] for v in values], [v[1] for v in values]
    return {"before": float(np.mean(before)), "after": float(np.mean(after)),
            "count": len(values), "worst_before": max(before), "worst_after": max(after)}


def refine_profile(candidate_profile, measurements, development_captures, rois, *,
                   max_iterations=60, on_progress=None):
    """Return an independently frozen profile; no validation input is accepted.

    RAW Gr full-curve losses from *all* development repeats are averaged with
    equal weight. ISP is measured only after fitting and is not an objective.
    No thresholds, excluded ROI lists or validation-dependent search controls
    are accepted by this interface.
    """
    if type(max_iterations) is not int or not 10 <= max_iterations <= 200:
        raise ValueError("Forward optimizer iterations must be in [10,200]")
    progress = on_progress or (lambda message: None)
    candidate = validate_profile(candidate_profile)
    first, edges, selected, nodes = _bound_inputs(candidate, measurements, development_captures, rois)
    templates, targets, isp_targets = {}, {r["id"]: [] for r in edges}, {r["id"]: [] for r in edges}
    captures = list(development_captures)
    for capture, raw_path, isp_path in captures:
        progress(f"Development-only native RAW/ISP measurement: {capture['id']}")
        for key, path in (("raw", raw_path), ("isp", isp_path)):
            _verify_file(path, capture["files"][key])
        metadata = capture["metadata"]
        isp, info = read_isp(isp_path, metadata["isp"])
        if not info["linear"]:
            raise ValueError("Forward ISP diagnostics require explicitly linear ISP values")
        with RawFrame(raw_path, metadata) as raw:
            if tuple(isp.shape[:2]) != tuple(raw.shape):
                raise ValueError("Declared registered development RAW/ISP dimensions differ")
            for roi in edges:
                row, col, height, width = roi["box"]
                planes, sampling = {}, {}
                for channel in esfr_engine.CHANNELS:
                    planes[channel], sampling[channel] = raw.plane(roi["box"], channel)
                metric = edge_metrics(planes["Gr"], pitch_native=2, black_level=0,
                                      white_level=sampling["Gr"]["white_level_dn"] - sampling["Gr"]["black_level_dn"])
                if metric["status"] != "ok":
                    raise ValueError(f"Development Gr edge unavailable: {capture['id']} / {roi['id']}")
                targets[roi["id"]].append(metric)
                rgb = isp[row:row + height, col:col + width]
                isp_metric = edge_metrics(rgb @ np.array([0.2126, 0.7152, 0.0722]))
                if isp_metric["status"] != "ok":
                    raise ValueError(f"Development ISP edge unavailable: {capture['id']} / {roi['id']}")
                isp_targets[roi["id"]].append(isp_metric)
                if roi["id"] not in templates:
                    geometry, levels = esfr_engine._geometry(roi, planes, sampling, metric, metadata)
                    templates[roi["id"]] = {"geometry": geometry, "levels": levels,
                                             "shape": [height, width], "capture_id": capture["id"]}

    diagnostics, fields, raw_errors, isp_errors = [], {}, [], []
    for index, roi in enumerate(edges):
        identity, template = roi["id"], templates[roi["id"]]
        progress(f"Development forward fit {index + 1}/{len(edges)}: {identity}")
        record = selected[identity]
        node = nodes[tuple(record["observations"]["field_xy"])]
        before = deepcopy(node)
        angles = [float(t["frequency_direction_deg"]) for t in targets[identity]]

        def render(field):
            return esfr_engine.simulate_edge({"fields": {identity: field}},
                    template["geometry"], template["levels"], template["shape"],
                    first["raw_format"]["cfa"], template["geometry"]["crop_origin"])

        def raw_metric(field):
            return edge_metrics(render(field)["raw_planes"]["Gr"], pitch_native=2)

        def objective(sigma):
            simulated = raw_metric(esfr_engine._field(float(sigma), angles))
            errors = [esfr_engine._curve_rmse(target, simulated) for target in targets[identity]]
            return float(np.mean(np.square(errors))) if all(x is not None for x in errors) else 100.0

        fit = minimize_scalar(objective, bounds=(0.15, 8.0), method="bounded",
                              options={"xatol": 0.001, "maxiter": max_iterations})
        if not fit.success or not np.isfinite(fit.fun) or fit.fun >= 1:
            raise ValueError(f"Development forward optimization failed: {identity}")
        field = esfr_engine._field(float(fit.x), angles)
        field["directional_fit"] = "single_native_roi_isotropic_full_curve"
        before_replay, after_replay = render(before), render(field)
        before_raw = edge_metrics(before_replay["raw_planes"]["Gr"], pitch_native=2)
        after_raw = edge_metrics(after_replay["raw_planes"]["Gr"], pitch_native=2)
        before_isp = edge_metrics(before_replay["isp"] @ np.array([0.2126, 0.7152, 0.0722]))
        after_isp = edge_metrics(after_replay["isp"] @ np.array([0.2126, 0.7152, 0.0722]))
        rows = []
        for capture, measured_raw, measured_isp in zip(captures, targets[identity], isp_targets[identity], strict=True):
            raw_pair = (esfr_engine._curve_rmse(measured_raw, before_raw), esfr_engine._curve_rmse(measured_raw, after_raw))
            isp_pair = (esfr_engine._curve_rmse(measured_isp, before_isp), esfr_engine._curve_rmse(measured_isp, after_isp))
            if any(x is None for x in (*raw_pair, *isp_pair)):
                raise ValueError(f"Development full-curve comparison unavailable: {identity}")
            raw_errors.append(raw_pair)
            isp_errors.append(isp_pair)
            rows.append({"capture_id": capture[0]["id"], "raw_sfr_rmse": dict(zip(("before", "after"), raw_pair)),
                         "isp_sfr_rmse": dict(zip(("before", "after"), isp_pair)),
                         "measured_raw_mtf50_cy_native_px": measured_raw.get("mtf50"),
                         "simulated_raw_mtf50_cy_native_px": {"before": before_raw.get("mtf50"), "after": after_raw.get("mtf50")},
                         "raw_quality": {k: measured_raw[k] for k in ("status", "warnings", "quality") if k in measured_raw}})
        f = np.asarray(record["observations"]["frequency_cy_px"])
        observed = np.asarray(record["observations"]["mtf"])
        angle = record["observations"]["orientation_deg"]
        analytic_error = {stage: float(np.sqrt(np.mean((observed - _mtf(n, f, angle)) ** 2)))
                          for stage, n in (("before", before), ("after", field))}
        node.update(sigma_x_px=field["sigma_x_px"], sigma_y_px=field["sigma_y_px"], theta_deg=0.0,
                    fit_observer=METHOD, source_measurement_ids=[record["id"]])
        fields[identity] = field
        diagnostics.append({"chart": "mtf", "status": "fitted", "method": METHOD,
                            "roi_id": identity, "field_xy": list(node["field_xy"]), "native_roi": roi["box"],
                            "measurement_id": record["id"], "development_repeat_count": len(captures),
                            "sigma_before_px": before["sigma_x_px"], "sigma_after_px": field["sigma_x_px"],
                            "theta_after_deg": 0.0, "isotropic_assumption": True,
                            "covariance_orientation_identifiable": False, "frequency_direction_deg": angles,
                            "analytic_curve_rmse": analytic_error, "objective_mean_squared_raw_sfr_error": float(fit.fun),
                            "optimizer": {"iterations": int(fit.nit), "evaluations": int(fit.nfev), "success": bool(fit.success)},
                            "raw_sfr_rmse": _metrics([(r["raw_sfr_rmse"]["before"], r["raw_sfr_rmse"]["after"]) for r in rows]),
                            "isp_sfr_rmse": _metrics([(r["isp_sfr_rmse"]["before"], r["isp_sfr_rmse"]["after"]) for r in rows]),
                            "development_comparisons": rows})

    forward = {"schema": esfr_engine.SCHEMA, "camera_id": first["camera_id"],
               "conditions": deepcopy(first["conditions"]), "raw_format": deepcopy(first["raw_format"]),
               "geometry": {**deepcopy(first["geometry"]), "roi_nuisance": templates}, "fields": fields,
               "scope": "chart_condition_bound_effective_camera_response", "physical_validation": "not_established",
               "source_kind": first["source_kind"], "development_capture_ids": [c[0]["id"] for c in captures],
               "fit_assumptions": [
                   "Only development RAW Gr full curves optimize one isotropic width per exact native ROI.",
                   "All development repeats receive equal objective weight; ISP is diagnostic only.",
                   "Development geometry, CFA phase and plateau levels are frozen before validation.",
                   "Effective response includes chart and sensor sampling; no unique optical PSF is inferred."]}
    candidate["esfr_forward_model"] = {"schema": BINDING_SCHEMA, "method": METHOD,
                                      "fit_data_role": "development_only", "profile": forward,
                                      "profile_sha256": digest(forward)}
    scope = candidate.get("esfr_bridge_scope")
    if isinstance(scope, dict):
        scope["prior_analytic_fit_assumptions"] = deepcopy(scope.get("assumptions", []))
        scope["assumptions"] = [
            "Analytical transfer-curve fitting supplies the initial effective Gaussian field nodes.",
            "The candidate widths are refined against development-only native Bayer RAW Gr SFR with the same forward observer as image validation.",
            "Each exact native ROI identifies one isotropic effective width; no physical optical PSF or covariance orientation is identified.",
        ]
        scope["fit_method"] = METHOD
    metadata = {"method": METHOD, "fit_data_role": "development_only", "prior_method": "analytic_curve",
                "before_reference": "analytical_initializer_candidate", "after_reference": "native_forward_refined_candidate",
                "field_count": len(edges), "development_capture_count": len(captures),
                "development_metrics": {"raw_sfr_rmse": _metrics(raw_errors), "isp_sfr_rmse": _metrics(isp_errors)},
                "objective": "equal_mean_squared_full_curve_RAW_Gr_SFR_errors_all_development_repeats",
                "isp_used_in_objective": False, "privileged_truth_used": False,
                "sigma_bounds_native_px": [0.15, 8.0],
                "profile_sha256": digest(forward), "fields": diagnostics,
                "input_bindings": {"development_capture_ids": [c[0]["id"] for c in captures],
                    "training_measurement_ids": [m["id"] for m in measurements],
                    "roi_geometry_sha256": digest(edges),
                    "capture_files": [{"id": c[0]["id"], "capture_sha256": c[0]["content_hash"],
                                       "metadata_sha256": digest(c[0]["metadata"]),
                                       "files": deepcopy(c[0]["files"])} for c in captures]},
                "diagnosis": {"observation": "analytic_transfer_fit_and_native_Bayer_SFR_replay_are_distinct_operators",
                              "correction": "fit_the_native_Bayer_forward_operator_on_development_data",
                              "causal_scope": "operator-consistent effective width; optical phase and individual physical blur sources are not identified",
                              "validation_data_used": False}}
    return {"profile": validate_profile(candidate), "diagnostics": diagnostics,
            "development_replay_profile": forward, "forward_observer": metadata}

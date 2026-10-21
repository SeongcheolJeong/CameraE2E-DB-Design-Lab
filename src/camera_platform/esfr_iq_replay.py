"""Apply a frozen CCM to the saved native effective-MTF replay, without refitting.

The saved arrays are normalized bilinear Reference ISP, not proprietary ISP or
new lens-prescription simulations. Every source array is read through its owner.
"""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import numpy as np

from .registry import IntegrityError, canonical, digest


def execute(service, validation_run, iq_profile, directory):
    from . import calibration_lab as lab
    from .esfr_analysis import edge_metrics
    from .esfr_engine import _curve_rmse
    from .esfr_iq_engine import apply_profile_rgb

    if validation_run.get("status") != "succeeded":
        raise ValueError("Combined replay requires a completed frozen MTF validation")
    summary = validation_run["result"]["summary"]
    image = summary.get("esfr_image_validation", {})
    if image.get("refit_performed") is not False or image.get("field_count") != 112:
        raise ValueError("Combined replay requires the full frozen 112-edge evidence")
    frozen_hash = digest(iq_profile)
    run_id = validation_run["id"]
    files = summary["files"]
    source_bindings = {}

    def path(relative):
        if relative not in files:
            raise IntegrityError("Combined replay source missing from manifest: " + relative)
        if files[relative] in source_bindings:
            return source_bindings[files[relative]]["path"]
        return service.registry.artifact_binding(files[relative], owner_kind="runs", owner_id=run_id)["path"]

    with gzip.open(path("esfr-images/full-result.json.gz"), "rt", encoding="utf-8") as stream:
        result = json.load(stream)
    if result["candidate_profile"].get("calibration_profile_sha256") != image["candidate_sha256"]:
        raise IntegrityError("MTF source candidate differs from frozen validation")
    selected = [r for r in result["comparisons"] if r["role"] == "validation"]
    if len(selected) != 336 or len({r["roi_id"] for r in selected}) != 112:
        raise IntegrityError("Combined replay must cover 112 edges in three independent captures")
    # The deterministic simulation is identical across repeated captures. Its
    # saved native arrays exist once per ROI; observations remain all 3 repeats.
    donors = {r["roi_id"]: r for r in selected if all(stage in r.get("images", {}) for stage in ("before", "after"))}
    if len(donors) != 112:
        raise IntegrityError("Native replay arrays are not available for every edge")
    names = ["esfr-images/" + row["images"][stage][key]
             for row in donors.values() for stage in ("before", "after")
             for key in ("raw_array", "isp_array")]
    if any(name not in files for name in names):
        raise IntegrityError("A native replay array is missing from its immutable manifest")
    from .calibration_completion import _bindings
    source_bindings = _bindings(service, [files[name] for name in names], "runs", run_id)
    output = Path(directory) / "combined-replay"
    output.mkdir(exist_ok=False)
    cache, bindings = {}, []
    for roi_id, row in donors.items():
        cache[roi_id] = {}
        for stage in ("before", "after"):
            array_name = "esfr-images/" + row["images"][stage]["isp_array"]
            raw_name = "esfr-images/" + row["images"][stage]["raw_array"]
            raw_path = path(raw_name)  # verify native RAW owner/hash even though CCM does not touch it
            raw = np.load(raw_path, allow_pickle=False)
            rgb = np.load(path(array_name), allow_pickle=False)
            if rgb.shape != (*raw.shape, 3) or not np.isfinite(rgb).all():
                raise IntegrityError("Native RAW/ISP replay geometry differs")
            corrected = apply_profile_rgb(iq_profile, rgb, baseline=stage == "before")
            if corrected.shape != rgb.shape or not np.isfinite(corrected).all():
                raise IntegrityError("Frozen CCM produced invalid combined replay pixels")
            destination = output / (roi_id + "-" + stage + ".npy")
            np.save(destination, corrected.astype(np.float32), allow_pickle=False)
            curves = {ch: edge_metrics(corrected[..., i]) for i, ch in enumerate(("R", "G", "B"))}
            curves["Y"] = edge_metrics(corrected @ np.array([.2126, .7152, .0722]))
            cache[roi_id][stage] = curves
            bindings.append({"roi_id": roi_id, "stage": stage, "source_run_id": run_id,
                "source_isp_file": array_name, "source_isp_sha256": files[array_name],
                "source_raw_file": raw_name, "raw_before_ccm_sha256": files[raw_name],
                "raw_after_ccm_sha256": files[raw_name], "raw_changed_by_ccm": False,
                "output_file": destination.relative_to(directory).as_posix()})
    rows = []
    for comparison in selected:
        curves = cache[comparison["roi_id"]]
        measured = comparison["curves"]["isp"]["measured"]
        before = _curve_rmse(measured, curves["before"]["Y"])
        after = _curve_rmse(measured, curves["after"]["Y"])
        eligible = bool(comparison["isp"].get("acceptance_eligible", True))
        rows.append({"capture_id": comparison["capture_id"], "roi_id": comparison["roi_id"],
            "raw_before_rmse": comparison["raw"]["before_rmse"],
            "raw_after_rmse": comparison["raw"]["after_rmse"],
            "isp_before_rmse": before, "isp_after_rmse": after,
            "isp_threshold": .05, "isp_threshold_status": "diagnostic" if not eligible else
                "unavailable" if after is None else "pass" if after <= .05 else "fail",
            "acceptance": "supplementary_fixed_profile_comparison_not_iq_activation_gate"})
    valid = [r for r in rows if r["isp_after_rmse"] is not None and r["isp_before_rmse"] is not None]
    evidence = {"schema": "camera_esfr_iq_combined_replay_v1",
        "scope": "conditional_effective_MTF_plus_fixed_WB_exposure_CCM_reference_ISP",
        "optical_prescription_recalculated": False, "source_run_id": run_id,
        "source_mtf_candidate_sha256": image["candidate_sha256"], "iq_profile_sha256": frozen_hash,
        "edge_count": 112, "comparison_count": len(rows), "refit_performed": False,
        "raw_changed_by_ccm": False, "raw_gate_unchanged": image["validation"],
        "isp_sfr_rmse": {"before": float(np.mean([r["isp_before_rmse"] for r in valid])) if valid else None,
                         "after": float(np.mean([r["isp_after_rmse"] for r in valid])) if valid else None,
                         "worst_after": max((r["isp_after_rmse"] for r in valid), default=None),
                         "count": len(valid), "unavailable_count": len(rows)-len(valid)},
        "source_bindings": bindings, "comparisons": rows,
        "curves": cache, "judgement": "pending_criteria", "physical_validation": "not_established"}
    if frozen_hash != digest(iq_profile):
        raise IntegrityError("IQ profile changed during combined replay")
    (output / "result.json").write_text(canonical(evidence))
    with (output / "metrics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return evidence

"""Pin only development RAW/ISP inputs for the native E-SFR forward fitter."""
from copy import deepcopy

from .registry import IntegrityError, digest


def development_snapshot(service, measurements):
    from . import esfr_lab

    origins = {m.get("provenance", {}).get("esfr_run_id") for m in measurements}
    if origins == {None}:
        return None
    if None in origins or len(origins) != 1:
        raise ValueError("Forward E-SFR fitting requires one source study")
    if any(m["role"] != "training" for m in measurements):
        raise ValueError("Forward E-SFR inputs must all be training measurements")
    source = esfr_lab.get(service, next(iter(origins)))
    if source["status"] != "succeeded":
        raise IntegrityError("E-SFR source study is incomplete")
    captures = [deepcopy(c) for c in source["capture_snapshots"]
                if c["id"] in source["development_ids"]]
    if {c["id"] for c in captures} != set(source["development_ids"]):
        raise IntegrityError("Development capture snapshots are incomplete")
    if any(c["metadata"]["role"] != "development" for c in captures):
        raise IntegrityError("Development partition includes a held-out capture")
    captures.sort(key=lambda c: source["development_ids"].index(c["id"]))
    by_id = {c["id"]: c for c in captures}
    edges = {r["id"]: r for r in source["specification"]["rois"] if r["kind"] == "edge"}
    selected = set()
    for m in measurements:
        p = m["provenance"]
        roi = edges.get(p.get("roi_id"))
        if roi is None or p.get("native_roi") != roi["box"] or roi["id"] in selected:
            raise IntegrityError("Training measurement differs from its native E-SFR ROI")
        selected.add(roi["id"])
        constituents = p.get("constituents") or [p]
        if {c.get("capture_id") for c in constituents} != set(by_id):
            raise IntegrityError("Every training edge must bind all development repeats")
        for row in constituents:
            capture = by_id[row["capture_id"]]
            if (row.get("metadata", p.get("source_metadata")) != capture["metadata"]
                    or row.get("capture_sha256") != capture["content_hash"]
                    or row.get("source_files") != capture["files"]
                    or row.get("native_pixel_sha256") != capture["native_pixel_sha256"]):
                raise IntegrityError("Training measurement capture provenance changed")
    if selected != set(edges):
        raise ValueError("Forward calibration requires all source edges, without truncation")
    return dict(
        schema="camera_esfr_development_inputs_v1", source_study_id=source["id"],
        source_result_sha256=digest(source["result"]), fit_data_role="development_only",
        capture_snapshots=captures, rois=[deepcopy(edges[r["id"]]) for r in source["specification"]["rois"] if r["kind"] == "edge"],
        thresholds=deepcopy(source["specification"]["thresholds"]),
        input_artifacts=[a for c in captures for a in c["files"].values()],
    )


def bound_development_captures(service, snapshot):
    from . import esfr_lab

    captures = snapshot["capture_snapshots"]
    if not captures or any(c["metadata"]["role"] != "development" for c in captures):
        raise IntegrityError("Forward fitter cannot receive validation acquisitions")
    result = []
    for frozen in captures:
        if esfr_lab.capture(service, frozen["id"]) != frozen:
            raise IntegrityError("Frozen development capture changed")
        result.append((frozen, esfr_lab._binding(service, frozen, "raw"),
                       esfr_lab._binding(service, frozen, "isp")))
    return result

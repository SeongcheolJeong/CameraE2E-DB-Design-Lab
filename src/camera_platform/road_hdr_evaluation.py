"""Causal detector replay and event-level locked road HDR selection."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from .pedestrian_braking_detector import infer_sequence
from .pedestrian_braking_metrics import evaluate_event
from .road_design_inputs import write_json
from .road_hdr_inputs import bound_specification, resource_plan
from .road_hdr_study import acquire, manifest, reprocess, verify_manifest
from .road_rgb_source import file_sha256, json_sha256


def choose(records, single_only=False):
    arms = sorted({r["arm"] for r in records if not single_only or r["arm"].startswith("single-")})
    if not arms:
        raise ValueError("No eligible selection records.")

    def rank(arm):
        rr = [r for r in records if r["arm"] == arm and r["risk"]]
        successes = sum(r["status"] == "success" for r in rr)
        worst = min(
            (r["braking_margin_interval_m"][0] for r in rr if r["braking_margin_interval_m"]),
            default=-float("inf"),
        )
        # Stable tie preference: short single exposure, then baseline retention.
        return (-successes, -worst, arm)

    return min(arms, key=rank)


def acceptance(base, candidate):
    """Paired events/seeds; unreviewed false tracks cannot justify a recommendation."""
    b = {(r["event_id"], r["condition"], r["seed"]): r for r in base}
    c = {(r["event_id"], r["condition"], r["seed"]): r for r in candidate}
    if b.keys() != c.keys():
        return {"accepted": False, "reason": "missing_paired_events"}
    conversions = defaultdict(list)
    regressions = []
    fp_ok = True
    for key, x in b.items():
        y = c[key]
        if x["risk"]:
            conversions[(key[0], key[1])].append(
                x["status"] != "success" and y["status"] == "success"
            )
            if x["status"] == "success" and y["status"] != "success":
                regressions.append(list(key))
        # Incomplete annotations leave a conservative FP upper bound unresolved.
        fp_ok &= len(y["confirmed_false_track_ids"]) + len(y["uncertain_track_ids"]) <= len(
            x["confirmed_false_track_ids"]
        )
    gained = [list(k) for k, v in conversions.items() if len(v) == 5 and all(v)]
    return {
        "accepted": bool(gained) and not regressions and fp_ok,
        "all_seed_success_conversions": gained,
        "successful_event_regressions": regressions,
        "false_positive_increase_excluded": bool(fp_ok),
    }


def evaluate_study(workspace, package, on_progress=lambda x: None):
    package = Path(package)
    spec, binding = bound_specification(package)
    plan = resource_plan(workspace, package, spec, "full")
    write_json(package / "resource-plan.json", plan)
    if plan["status"] != "ready":
        raise ValueError("resource_blocked: " + ",".join(plan["reasons"]))
    # Acquisition is original-data generation. Reprocessing/detection then reuses it.
    acquire(workspace, package, stage="full", on_progress=on_progress)
    output = reprocess(
        workspace, package, profiles=tuple(spec["profiles"]), on_progress=on_progress
    )
    root = Path(output["result_directory"])
    verify_manifest(root, "manifest.json")
    results = []
    identity = None
    for split in ("selection", "evaluation"):
        lock = package / "selection-lock.json"
        if split == "evaluation":
            chosen = json.loads(lock.read_text())
            if chosen["specification_sha256"] != binding["sha256"]:
                raise ValueError("Selection binding changed.")
        for event in [e for e in spec["events"] if e["split"] == split]:
            tokens = {f["token"] for f in event["frames"]}
            groups = defaultdict(list)
            for out in output["outputs"]:
                if out["frame_token"] in tokens and (
                    split == "selection" or out["arm"] in {chosen["baseline"], chosen["selected"]}
                ):
                    groups[(out["arm"], out["condition"], out["seed"])].append(out)
            for (arm, condition, seed), frames in groups.items():
                frames.sort(key=lambda f: f["timestamp_s"])
                detector_frames = [
                    {"token": f["frame_token"], "timestamp_s": f["timestamp_s"], "path": f["path"]}
                    for f in frames
                ]
                config = {
                    "association_iou": 0.3,
                    "max_missed": 2,
                    "consecutive_required": 3,
                    "expected_output_timestamps": [f["timestamp_s"] for f in frames],
                }
                inferred, identity = infer_sequence(
                    spec["detector"],
                    config,
                    detector_frames,
                    lambda f: np.array(Image.open(root / f["path"] / "rgb.png")),
                )
                for detected, out in zip(inferred, frames, strict=True):
                    detected["result_available_ns"] = out["timing"]["result_available_ns"]
                    detected["image_path"] = str(
                        (root / out["path"] / "rgb.png").relative_to(package)
                    )
                result = evaluate_event(
                    event, inferred, processing_latency_s=0.0, braking=spec["braking"]
                )
                result.update(arm=arm, condition=condition, seed=seed, split=split)
                sensitivity = []
                if arm.startswith("hdr-"):
                    for extra in spec["timing"]["hdr_sensitivity_s"]:
                        changed = [
                            {
                                **f,
                                "result_available_ns": f["result_available_ns"]
                                + int(round((extra - spec["timing"]["hdr_processing_s"]) * 1e9)),
                            }
                            for f in inferred
                        ]
                        sensitivity.append(
                            {
                                "hdr_extra_s": extra,
                                **evaluate_event(
                                    event,
                                    changed,
                                    processing_latency_s=0.0,
                                    braking=spec["braking"],
                                ),
                            }
                        )
                result["latency_sensitivity"] = sensitivity
                results.append(result)
                write_json(
                    package / "evaluation" / split / event["id"] / f"{arm}-{condition}-{seed}.json",
                    {"result": result, "frames": inferred, "detector_identity": identity},
                )
                on_progress("event evaluated: " + event["id"] + "/" + arm)
        if split == "selection":
            selection = [r for r in results if r["split"] == "selection"]
            baseline = choose(selection, True)
            base = [r for r in selection if r["arm"] == baseline]
            viable = [baseline]
            for arm in sorted({r["arm"] for r in selection if r["arm"].startswith("hdr-")}):
                if acceptance(base, [r for r in selection if r["arm"] == arm])["accepted"]:
                    viable.append(arm)
            selected = choose([r for r in selection if r["arm"] in viable])
            if selected != baseline:
                # A rank tie must retain the chosen single baseline.
                if sum(r["status"] == "success" for r in selection if r["arm"] == selected) == sum(
                    r["status"] == "success" for r in base
                ):
                    selected = baseline
            document = {
                "specification_sha256": binding["sha256"],
                "selection_records_sha256": json_sha256(selection),
                "baseline": baseline,
                "selected": selected,
                "heldout_reselection_allowed": False,
            }
            if lock.exists() and json.loads(lock.read_text()) != document:
                raise ValueError("Locked selection changed.")
            write_json(lock, document)
    heldout = [r for r in results if r["split"] == "evaluation"]
    base = [r for r in heldout if r["arm"] == chosen["baseline"]]
    changed = [r for r in heldout if r["arm"] == chosen["selected"]]
    gate = acceptance(base, changed)
    decision = {
        "title": spec["title"],
        "status": "improvement_confirmed"
        if gate["accepted"]
        else "baseline_retained"
        if chosen["baseline"] == chosen["selected"]
        else "inconclusive",
        "baseline": chosen["baseline"],
        "selected": chosen["selected"],
        "acceptance": gate,
        "records": results,
        "independent_event_count": len(spec["events"]),
        "detector_identity": identity,
        "selection_lock_sha256": file_sha256(package / "selection-lock.json"),
        "sampling": json.loads((package / "sampling/result.json").read_text()),
        "recommended_conditions": sorted(
            {k[1] for k in gate.get("all_seed_success_conversions", [])}
        )
        if gate["accepted"]
        else [],
        "physical_validation": False,
        "RGB_proxy_scope_retained": True,
    }
    write_json(package / "decision.json", decision)
    from .road_hdr_report import decision_report

    decision_report(package, decision)
    manifest(package, "study-manifest.json")
    return decision

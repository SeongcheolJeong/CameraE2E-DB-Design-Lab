"""Frozen full-window RAW sequence -> causal inference -> held-out decision."""

from __future__ import annotations

import fcntl
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from .pedestrian_braking_detector import infer_sequence
from .pedestrian_braking_inputs import bound_specification
from .pedestrian_braking_metrics import compare_candidate, evaluate_event
from .road_design_compute import (
    _roi_bounds,
    compute_mean,
    fixed_isp,
    fixed_profile,
    load_candidate,
    native_coordinates,
    reference_linear_isp,
    roi_capture_sensor,
    to_common_grid,
    well_saturation_mask,
)
from .road_design_inputs import write_json
from .road_rgb_source import file_sha256, json_sha256


def window(spec):
    return {"id": "fixed-front-window", **spec["observation"]}


def resource_plan(workspace, package, spec, *, stage="full"):
    """Conservative storage bound, before any expensive camera computation.

    Full stage reserves evaluation storage for the largest possible candidate;
    it cannot use selection outcomes to shrink the admission estimate.
    """
    rows = []
    for candidate in spec["candidates"]:
        optics, sensor = load_candidate(Path(workspace), spec, candidate)
        bounds, _ = _roi_bounds(
            native_coordinates(optics, sensor, window(spec)), sensor.fields["size"]
        )
        cells = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
        pitch = candidate["sensor"]["native_response"]["pixel_pitch_um"]
        rows.append(
            {
                "id": candidate["id"],
                "native_bounds": list(bounds),
                "native_cells": cells,
                "estimated_native_capture_isp_working_bytes": cells * 192 + 64 * 1024**2,
                "virtual_sensor_shape": list(sensor.fields["size"]),
                "sensor_area_mm2": np.prod(sensor.fields["size"]) * (pitch / 1000) ** 2,
            }
        )
    baseline = next(c for c in rows if c["id"] == spec["baseline_id"])
    first = stage == "first-sequence"
    events = [next(e for e in spec["events"] if e["risk"])] if first else spec["events"]
    chosen = [r for r in rows if r["id"] in (spec["baseline_id"], "L2-S1")] if first else rows
    bytes_needed = 0
    for event in events:
        admitted = (
            chosen
            if first or event["split"] == "selection"
            else [baseline, max(rows, key=lambda r: r["native_cells"])]
        )
        for candidate in admitted:
            # Native mean float64, integer RAW, common linear RGB float32 + PNG
            per_frame = candidate["native_cells"] * (8 + 4 * len(spec["seeds"])) + 720 * 1280 * (
                12 + 4
            ) * len(spec["seeds"])
            bytes_needed += len(event["frames"]) * per_frame
    # Additional representative refinement mean + previews, file metadata.
    bytes_needed += sum(c["native_cells"] * 8 for c in rows) * 9
    bytes_needed = int(bytes_needed * 1.1)
    destination = Path(package)
    parent = destination
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    reserve = spec["resources"]["min_free_disk_bytes"]
    return {
        "schema": "camera_pedestrian_resource_plan_v1",
        "stage": stage,
        "status": "ready" if free >= bytes_needed + reserve else "blocked_storage",
        "estimated_package_bytes": bytes_needed,
        "observed_free_bytes": free,
        "reserved_free_bytes": reserve,
        "policy_is_hardware_limit": False,
        "candidates": rows,
        "events": len(events),
        "seeds_per_event": len(spec["seeds"]),
        "representative_refinement_reserved": True,
        "sampling_or_event_count_reduced": False,
    }


def _verified_record(path, spec_sha):
    checksum = path.with_suffix(path.suffix + ".sha256")
    if not checksum.exists() or checksum.read_text().strip() != file_sha256(path):
        raise ValueError("Cached sequence record changed or has no completion checksum.")
    record = json.loads(path.read_text())
    if record.get("specification_sha256") != spec_sha:
        raise ValueError("Cached sequence is bound to another specification.")
    for relative, expected in record["files"].items():
        target = (path.parent / relative).resolve()
        if not target.is_relative_to(path.parent.resolve()) or file_sha256(target) != expected:
            raise ValueError("Cached sequence output changed.")
    return record


def _write_record(path, document):
    write_json(path, document)
    path.with_suffix(path.suffix + ".sha256").write_text(file_sha256(path) + "\n")


def sampling_check(workspace, package, spec, on_progress):
    """Joint signal and three-frame recognition check at fixed observation/FOV.

    Three original frames, one fixed seed, all four candidates; finer Scene
    changes neither the native receiver aperture nor the stored PSF. This is
    source-grid qualification, not a claim of PSF/diffraction convergence.
    """
    from pyisetcam.sensor_capture import resample_sensor_capture
    from pyisetcam.utils import linear_to_srgb

    from .pedestrian_braking_metrics import iou

    path = package / "sampling.json"
    spec_sha = file_sha256(package / "specification.json")
    if path.exists():
        return _verified_record(path, spec_sha)
    event = next(e for e in spec["events"] if e["risk"])
    frames = event["frames"][:3]
    checks, files = [], {}
    for candidate in spec["candidates"]:
        arrays, inferences = {}, {}
        for refinement in (1, 2, 4):
            arrays[refinement] = []
            images = {}
            for frame in frames:
                frame = {
                    **frame,
                    "source_path": str(
                        package / "inputs" / event["id"] / (frame["token"] + ".jpg")
                    ),
                }
                directory = (
                    package / "sampling" / candidate["id"] / f"grid-{refinement}" / frame["token"]
                )
                on_progress(f"Sampling {candidate['id']} grid x{refinement} {frame['token']}")
                mean, report, optics, sensor, coords = compute_mean(
                    Path(workspace),
                    spec,
                    frame,
                    window(spec),
                    candidate,
                    directory,
                    refinement=refinement,
                )
                profile, _ = fixed_profile(
                    sensor,
                    np.asarray(report["spectral_proxy"]["primary_photons"]),
                    spec["spectral_proxy"]["wavelength_nm"],
                )
                bounds = tuple(report["native_roi_bounds"])
                seed = int(json_sha256([event["id"], frame["token"], spec["seeds"][0]])[:15], 16)
                capture_sensor = roi_capture_sensor(sensor, bounds, mean * 0.25)
                capture = resample_sensor_capture(capture_sensor, seed=seed)
                linear, _ = fixed_isp(capture.data["raw_dn"], sensor, bounds, profile)
                common = to_common_grid(linear, coords, bounds)
                images[frame["token"]] = np.rint(
                    linear_to_srgb(np.clip(common, 0, 1)) * 255
                ).astype(np.uint8)
                Image.fromarray(images[frame["token"]]).save(directory / "detector-input.png")
                write_json(
                    directory / "capture-isp.json",
                    {
                        "capture_seed": seed,
                        "fixed_profile": profile,
                        "native_geometry_bounds": list(bounds),
                        "source_refinement": refinement,
                    },
                )
                # Arrays stay disk backed between passes; source and receive grids are separate.
                noise_free = to_common_grid(
                    reference_linear_isp(mean * 0.25, sensor, bounds, profile), coords, bounds
                )
                luminance = noise_free @ [0.2126, 0.7152, 0.0722]
                arrays[refinement].append(
                    (
                        directory / "mean_electrons.npy",
                        float(np.mean(np.hypot(*np.gradient(luminance)))),
                    )
                )
            inferred, identity = infer_sequence(
                spec["detector"],
                spec["tracker"],
                frames,
                lambda f, images=images: images[f["token"]],
            )
            inferences[refinement] = inferred
            inference_path = (
                package / "sampling" / candidate["id"] / f"inference-grid-{refinement}.json"
            )
            write_json(inference_path, {"frames": inferred, "identity": identity})
            if refinement == 1:
                continue
            coarse_grid, fine_grid = refinement // 2, refinement
            deltas, contrast_deltas = [], []
            for coarse, fine in zip(arrays[coarse_grid], arrays[fine_grid], strict=True):
                a, b = np.load(coarse[0], mmap_mode="r"), np.load(fine[0], mmap_mode="r")
                deltas.append(float(np.mean(abs(a - b)) / max(1e-15, np.mean(b))))
                contrast_deltas.append(abs(coarse[1] - fine[1]) / max(1e-15, fine[1]))
            stable = True
            for a, b in zip(inferences[coarse_grid], inferences[fine_grid], strict=True):
                if len(a["detections"]) != len(b["detections"]):
                    stable = False
                    continue
                unmatched = list(b["detections"])
                for detection in a["detections"]:
                    if not unmatched:
                        stable = False
                        break
                    best = max(unmatched, key=lambda d: iou(d["bbox"], detection["bbox"]))
                    stable &= (
                        iou(best["bbox"], detection["bbox"]) >= 0.9
                        and abs(best["score"] - detection["score"]) <= 0.01
                    )
                    unmatched.remove(best)
                stable &= sum(t["stable"] for t in a["tracks"]) == sum(
                    t["stable"] for t in b["tracks"]
                )
            qualified = bool(stable and max(deltas) <= 0.01 and max(contrast_deltas) <= 0.01)
            checks.append(
                {
                    "candidate_id": candidate["id"],
                    "coarse_source_refinement": coarse_grid,
                    "fine_source_refinement": fine_grid,
                    "mean_relative_deltas": deltas,
                    "gradient_contrast_relative_deltas": contrast_deltas,
                    "recognition_stable": bool(stable),
                    "qualified": qualified,
                }
            )
            if qualified:
                break
    for saved in (package / "sampling").rglob("*"):
        if saved.is_file():
            files[str(saved.relative_to(package))] = file_sha256(saved)
    result = {
        "specification_sha256": spec_sha,
        "files": files,
        "checks": checks,
        "qualified": all(
            any(c["candidate_id"] == candidate["id"] and c["qualified"] for c in checks)
            for candidate in spec["candidates"]
        ),
        "selected_source_refinement": {
            c["candidate_id"]: c["coarse_source_refinement"] for c in checks if c["qualified"]
        },
        "source_refinement_options": [1, 2, 4],
        "stored_psf_refined": False,
        "physical_sampling_validation": False,
        "representative_frames": [f["token"] for f in frames],
    }
    _write_record(path, result)
    return result


def compute_sequence(workspace, package, spec, event, candidate, on_progress):
    """Store noise-free mean once per frame, independent native RAW per seed."""
    from pyisetcam.sensor_capture import resample_sensor_capture
    from pyisetcam.utils import linear_to_srgb

    root = package / "calculations" / event["id"] / candidate["id"]
    completed = root / "sequence.json"
    spec_sha = file_sha256(package / "specification.json")
    sampling = _verified_record(package / "sampling.json", spec_sha)
    sampling_sha = file_sha256(package / "sampling.json")
    if completed.exists():
        cached = _verified_record(completed, spec_sha)
        if cached.get("sampling_sha256") != sampling_sha:
            raise ValueError("Completed sequence sampling qualification changed.")
        return cached
    refinement = sampling["selected_source_refinement"][candidate["id"]]
    all_files, all_frames = {}, {}
    for index, frame in enumerate(event["frames"]):
        frame = {
            **frame,
            "source_path": str(package / "inputs" / event["id"] / (frame["token"] + ".jpg")),
        }
        on_progress(f"{event['id']} {candidate['id']} frame {index + 1}/{len(event['frames'])}")
        path = root / "frames" / frame["token"]
        completion = path / "capture.json"
        if completion.exists():
            cache = _verified_record(completion, spec_sha)
            if (
                cache["input_sha256"] != frame["source_sha256"]
                or file_sha256(frame["source_path"]) != frame["source_sha256"]
                or cache["source_refinement"] != refinement
            ):
                raise ValueError("Sequence original RGB changed.")
        else:
            mean, optical, optics, sensor, coords = compute_mean(
                Path(workspace), spec, frame, window(spec), candidate, path, refinement=refinement
            )
            bounds = tuple(optical["native_roi_bounds"])
            profile, calibration = fixed_profile(
                sensor,
                np.asarray(optical["spectral_proxy"]["primary_photons"]),
                spec["spectral_proxy"]["wavelength_nm"],
            )
            capture_sensor = roi_capture_sensor(sensor, bounds, mean * 0.25)
            captures = []
            for seed in spec["seeds"]:
                # Temporal shots are independent, fixed maps keep global coordinates.
                actual_seed = int(json_sha256([event["id"], frame["token"], seed])[:15], 16)
                capture = resample_sensor_capture(capture_sensor, seed=actual_seed)
                raw = capture.data["raw_dn"]
                well = well_saturation_mask(capture_sensor, actual_seed)
                if (
                    abs(
                        float(well.mean())
                        - capture.metadata["sensor_capture"]["saturation"]["well_fraction"]
                    )
                    > 1e-14
                ):
                    raise ValueError("Native well saturation replay disagrees.")
                linear, isp_report = fixed_isp(raw, sensor, bounds, profile)
                common = to_common_grid(linear, coords, bounds)
                if not np.isfinite(common).all():
                    raise ValueError("Observation window lacks declared optical/pixel/ISP support.")
                rgb = np.rint(linear_to_srgb(np.clip(common, 0, 1)) * 255).astype(np.uint8)
                np.save(path / f"raw-{seed}.npy", raw)
                np.save(path / f"linear-rgb-{seed}.npy", common.astype(np.float32))
                Image.fromarray(rgb).save(path / f"rgb-{seed}.png")
                captures.append(
                    {
                        "seed": seed,
                        "actual_capture_seed": actual_seed,
                        "well_fraction": float(well.mean()),
                        "adc_fraction": float(
                            np.mean(raw == 2 ** sensor.fields["sensor_readout"]["bit_depth"] - 1)
                        ),
                        "native_shape": list(raw.shape),
                        "isp": isp_report,
                    }
                )
            cache = {
                "specification_sha256": spec_sha,
                "input_sha256": frame["source_sha256"],
                "token": frame["token"],
                "timestamp_s": frame["timestamp_s"],
                "source_refinement": refinement,
                "fixed_profile": profile,
                "chromatic_calibration": calibration,
                "captures": captures,
                "files": {
                    p.name: file_sha256(p)
                    for p in path.iterdir()
                    if p.is_file() and not p.name.endswith(".tmp")
                },
            }
            _write_record(completion, cache)
        all_frames[frame["token"]] = str(path.relative_to(package))
        for name, sha in cache["files"].items():
            all_files[str((path / name).relative_to(root))] = sha
        all_files[str(completion.relative_to(root))] = file_sha256(completion)
        checksum = completion.with_suffix(completion.suffix + ".sha256")
        all_files[str(checksum.relative_to(root))] = file_sha256(checksum)
    records, identities = [], []
    for seed in spec["seeds"]:
        inference_file = root / f"inference-{seed}.json"
        if inference_file.exists():
            saved = _verified_record(inference_file, spec_sha)
            frames, identity = saved["frames"], saved["identity"]
        else:
            frames, identity = infer_sequence(
                spec["detector"],
                spec["tracker"],
                event["frames"],
                lambda f, seed=seed: np.asarray(
                    Image.open(package / all_frames[f["token"]] / f"rgb-{seed}.png").convert("RGB")
                ),
            )
            _write_record(
                inference_file,
                {
                    "specification_sha256": spec_sha,
                    "frames": frames,
                    "identity": identity,
                    "files": {},
                },
            )
        for latency in spec["timing"]["latency_sensitivity_s"]:
            metric = evaluate_event(
                event, frames, processing_latency_s=latency, braking=spec["braking"]
            )
            metric.update(seed=seed, candidate_id=candidate["id"])
            records.append(metric)
        all_files[inference_file.name] = file_sha256(inference_file)
        checksum = inference_file.with_suffix(inference_file.suffix + ".sha256")
        all_files[checksum.name] = file_sha256(checksum)
        identities.append(identity)
    result = {
        "specification_sha256": spec_sha,
        "sampling_sha256": sampling_sha,
        "event_id": event["id"],
        "candidate_id": candidate["id"],
        "records": records,
        "detector_identities": identities,
        "frame_directories": all_frames,
        "files": all_files,
        "status": "sequence_calculated",
    }
    _write_record(completed, result)
    return result


def select_candidate(spec, records):
    baseline = [
        r
        for r in records
        if r["candidate_id"] == spec["baseline_id"] and r["processing_latency_s"] == 0.1
    ]
    viable, comparisons = [], []
    for candidate in spec["candidates"]:
        if candidate["id"] == spec["baseline_id"]:
            continue
        subset = [
            r
            for r in records
            if r["candidate_id"] == candidate["id"] and r["processing_latency_s"] == 0.1
        ]
        comparison = compare_candidate(baseline, subset)
        comparison["candidate_id"] = candidate["id"]
        comparisons.append(comparison)
        if comparison["recommend_change"]:
            margins = [
                r["braking_margin_interval_m"][0]
                for r in subset
                if r["risk"] and r["braking_margin_interval_m"] is not None
            ]
            viable.append(
                (
                    comparison["candidate_confirmed_success_events"],
                    min(margins, default=-float("inf")),
                    candidate["id"],
                )
            )
    viable.sort(reverse=True)
    # Equal success/margin cannot justify selection by lexical camera ID.
    tie = len(viable) > 1 and viable[0][:2] == viable[1][:2]
    return {
        "selected_id": viable[0][2] if viable and not tie else spec["baseline_id"],
        "comparisons": comparisons,
        "baseline_on_tie": tie,
    }


def verify_package(package):
    package = Path(package)
    bound_specification(package)
    manifest = json.loads((package / "package-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (package / name).resolve()
        if not path.is_relative_to(package.resolve()) or file_sha256(path) != expected:
            raise ValueError("Pedestrian package checksum changed.")
    return {"status": "verified", "files": len(manifest["files"])}


def run_study(workspace, package, *, stage="full", on_progress=lambda message: None):
    package = Path(package).resolve()
    spec, binding = bound_specification(package)
    if stage not in {"first-sequence", "full"}:
        raise ValueError("Unknown pedestrian study stage.")
    with (package / ".execution.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another worker owns this package.") from exc
        if (package / "package-manifest.json").exists():
            verify_package(package)
            return json.loads((package / "decision.json").read_text())
        plan = resource_plan(workspace, package, spec, stage=stage)
        write_json(package / "resource-plan.json", plan)
        if plan["status"] != "ready":
            raise ValueError(
                f"storage_blocked: require {plan['estimated_package_bytes']} bytes plus reserve, "
                f"available {plan['observed_free_bytes']}; keep the frozen resolution/events/seeds."
            )

        def progress(message):
            write_json(package / "progress.json", {"message": message, "stage": stage})
            on_progress(message)

        sampling = sampling_check(workspace, package, spec, progress)
        if not sampling["qualified"]:
            raise ValueError(
                "sampling_inconclusive: representative source refinement changed signal "
                "or recognition beyond the frozen tolerance; do not select a camera."
            )

        if stage == "first-sequence":
            event = next(e for e in spec["events"] if e["risk"])
            pilot = []
            for candidate in spec["candidates"]:
                if candidate["id"] in (spec["baseline_id"], "L2-S1"):
                    pilot.append(
                        compute_sequence(workspace, package, spec, event, candidate, progress)
                    )
            result = {
                "status": "connection_only_not_selection",
                "recommended_id": None,
                "selected_id": "L2-S1",
                "sequences": pilot,
            }
            write_json(package / "first-sequence.json", result)
            from .pedestrian_braking_report import build_report

            build_report(package, spec, result, [r for s in pilot for r in s["records"]], plan)
            write_json(
                package / "first-sequence-manifest.json",
                {
                    "files": {
                        str(p.relative_to(package)): file_sha256(p)
                        for p in package.rglob("*")
                        if p.is_file()
                        and not p.name.startswith(".")
                        and p.name not in {"progress.json", "first-sequence-manifest.json"}
                    }
                },
            )
            return {"status": "first_sequence_calculated", "selected_id": "L2-S1"}
        selection_file = package / "selection-lock.json"
        selection_records = []
        for event in spec["events"]:
            if event["split"] == "selection":
                for candidate in spec["candidates"]:
                    selection_records.extend(
                        compute_sequence(workspace, package, spec, event, candidate, progress)[
                            "records"
                        ]
                    )
        write_json(package / "selection-records.json", selection_records)
        selected = select_candidate(spec, selection_records)
        selected.update(
            specification_sha256=binding["sha256"],
            selection_records_sha256=file_sha256(package / "selection-records.json"),
        )
        if selection_file.exists() and json.loads(selection_file.read_text()) != selected:
            raise ValueError(
                "Previously locked selection changed; no held-out reselection allowed."
            )
        if not selection_file.exists():
            write_json(selection_file, selected)
        evaluation_records = []
        for event in spec["events"]:
            if event["split"] == "evaluation":
                for candidate in spec["candidates"]:
                    if candidate["id"] in {spec["baseline_id"], selected["selected_id"]}:
                        evaluation_records.extend(
                            compute_sequence(workspace, package, spec, event, candidate, progress)[
                                "records"
                            ]
                        )
        baseline = [
            r
            for r in evaluation_records
            if r["candidate_id"] == spec["baseline_id"] and r["processing_latency_s"] == 0.1
        ]
        changed = [
            r
            for r in evaluation_records
            if r["candidate_id"] == selected["selected_id"] and r["processing_latency_s"] == 0.1
        ]
        comparison = compare_candidate(baseline, changed)
        # A baseline tie never invents improvement.
        recommend = (
            selected["selected_id"] != spec["baseline_id"] and comparison["recommend_change"]
        )
        decision = {
            "status": "conditional_model_recommendation" if recommend else "baseline_retained",
            "recommended_id": selected["selected_id"] if recommend else spec["baseline_id"],
            "selected_id": selected["selected_id"],
            "heldout_comparison": comparison,
            "specification_sha256": binding["sha256"],
            "selection_lock_sha256": file_sha256(selection_file),
            "physical_validation": "not_established",
            "aeb_certification": False,
            "closed_loop_collision_avoidance": False,
        }
        write_json(package / "evaluation-records.json", evaluation_records)
        write_json(package / "decision.json", decision)
        from .pedestrian_braking_report import build_report

        build_report(package, spec, decision, evaluation_records, plan)
        write_json(
            package / "package-manifest.json",
            {
                "files": {
                    str(p.relative_to(package)): file_sha256(p)
                    for p in package.rglob("*")
                    if p.is_file()
                    and not p.name.startswith(".")
                    and p.name not in {"package-manifest.json", "progress.json"}
                    and ".tmp" not in p.name
                }
            },
        )
        verify_package(package)
        return decision

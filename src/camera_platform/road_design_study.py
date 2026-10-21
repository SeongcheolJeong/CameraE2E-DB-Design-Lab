"""One reproducible road design study, from frozen inputs to a locked decision."""

from __future__ import annotations

import fcntl
import shutil
import time
from pathlib import Path

import numpy as np

from .road_design_compute import (
    capture_series,
    compute_mean,
    fixed_profile,
    json_load,
    load_candidate,
    native_coordinates,
    reference_linear_isp,
    to_common_grid,
)
from .road_design_inputs import SCHEMA, write_json
from .road_design_statistics import choose_candidate, evaluate_selection
from .road_rgb_source import file_sha256, json_sha256


def bound_specification(package: Path):
    spec_path = package / "specification.json"
    binding = json_load(package / "specification-binding.json")
    if file_sha256(spec_path) != binding["sha256"]:
        raise ValueError("Frozen specification changed after binding.")
    spec = json_load(spec_path)
    if spec["schema"] != SCHEMA or json_sha256(spec) != binding["semantic_sha256"]:
        raise ValueError("Unsupported or altered specification.")
    for split in ("selection", "evaluation"):
        if len([image for image in spec["images"] if image["split"] == split]) != 6:
            raise ValueError("The study requires six selection and six held-out images.")
    logs = [image["log_token"] for image in spec["images"]]
    if len(set(logs)) != len(logs):
        raise ValueError("Images must come from distinct logs.")
    if len(spec["candidates"]) != 4 or spec["baseline_id"] != spec["candidates"][0]["id"]:
        raise ValueError("Freeze a 2x2 candidate cohort with its first candidate as baseline.")
    return spec, binding


def _mean(workspace, package, spec, image, roi, candidate, refinement=1):
    snapshot = package / "inputs" / image["id"] / "original.jpg"
    if snapshot.exists():
        if file_sha256(snapshot) != image["source_sha256"]:
            raise ValueError("Pinned original-photo snapshot changed.")
        image = {**image, "source_path": str(snapshot)}
    directory = package / "calculations" / image["id"] / candidate["id"] / roi["id"]
    if refinement != 1:
        directory /= f"refinement-{refinement}"
    manifest = directory / "optical-execution.json"
    if manifest.exists():
        report = json_load(manifest)
        if report.get("specification_sha256") != file_sha256(package / "specification.json"):
            raise ValueError("Cached mean is bound to a different specification.")
        if file_sha256(directory / "mean_electrons.npy") != report["mean_sha256"]:
            raise ValueError("Cached mean artifact changed.")
        optics, sensor = load_candidate(workspace, spec, candidate)
        coordinates = native_coordinates(optics, sensor, roi)
        mean = np.load(directory / "mean_electrons.npy", mmap_mode="r")
    else:
        mean, report, optics, sensor, coordinates = compute_mean(
            workspace,
            spec,
            image,
            roi,
            candidate,
            directory,
            refinement=refinement,
        )
        report.update(
            {
                "package_root": str(package),
                "specification_sha256": file_sha256(package / "specification.json"),
                "mean_sha256": file_sha256(directory / "mean_electrons.npy"),
            }
        )
        write_json(manifest, report)
    report["package_root"] = str(package)
    return directory, mean, report, optics, sensor, coordinates


def sampling_check(workspace, package, spec, progress):
    path = package / "sampling.json"
    if path.exists():
        saved = json_load(path)
        if saved["specification_sha256"] != file_sha256(package / "specification.json"):
            raise ValueError("Sampling evidence is bound to a different specification.")
        return saved
    image = next(
        image
        for image in spec["images"]
        if image["id"] == spec["sampling"]["representative_image_id"]
    )
    roi = next(
        roi for roi in image["rois"] if roi["id"] == spec["sampling"]["representative_roi_id"]
    )
    results = []
    for candidate in spec["candidates"]:
        outputs = []
        for refinement in (1, spec["sampling"]["refinement_multiplier"]):
            progress(f"Sampling {candidate['id']} source grid x{refinement}")
            directory, mean, report, optics, sensor, coords = _mean(
                workspace, package, spec, image, roi, candidate, refinement
            )
            profile, _ = fixed_profile(
                sensor,
                np.asarray(report["spectral_proxy"]["primary_photons"]),
                spec["spectral_proxy"]["wavelength_nm"],
            )
            linear = to_common_grid(
                reference_linear_isp(mean, sensor, tuple(report["native_roi_bounds"]), profile),
                coords,
                tuple(report["native_roi_bounds"]),
            )
            with np.load(package / roi["mask_path"]) as masks:
                obj, bg = masks["object"], masks["background"]
            y = linear @ [0.2126, 0.7152, 0.0722]
            contrast = abs(float(y[obj].mean() - y[bg].mean()))
            outputs.append((mean, report, contrast))
        coarse, fine = outputs
        relative = float(np.mean(abs(coarse[0] - fine[0])) / max(1e-15, np.mean(fine[0])))
        contrast_delta = abs(coarse[2] - fine[2]) / max(1e-15, abs(fine[2]))
        stable = (
            relative <= spec["sampling"]["mean_relative_tolerance"]
            and contrast_delta <= spec["sampling"]["contrast_relative_tolerance"]
        )
        results.append(
            {
                "candidate_id": candidate["id"],
                "converged_for_representative_roi": stable,
                "mean_relative_l1_delta": relative,
                "contrast_relative_delta": contrast_delta,
                "coarse_convolution_pitch_um": coarse[1]["convolution_pitch_um"],
                "fine_convolution_pitch_um": fine[1]["convolution_pitch_um"],
                "sensor_pitch_um": fine[1]["sensor_pixel_pitch_um"],
                "stored_psf_pitch_um": fine[1]["psf_source_pitch_um"],
                "scope": (
                    "one representative original-RGB angular ROI; source-grid refinement, "
                    "fixed stored geometric PSF"
                ),
            }
        )
    # Check the cached linear mean assumption with an actual spectral-input
    # brightness change, before reusing means for the study's readout sweep.
    candidate = spec["candidates"][0]
    progress("Verifying pre-readout brightness linearity")
    snapshot = package / "inputs" / image["id"] / "original.jpg"
    scaled, scaled_report, *_ = compute_mean(
        workspace,
        spec,
        {**image, "source_path": str(snapshot)} if snapshot.exists() else image,
        roi,
        candidate,
        package / "linearity",
        brightness=0.25,
    )
    _, source, *_ = _mean(workspace, package, spec, image, roi, candidate)
    linearity_delta = float(np.mean(abs(scaled - source * 0.25)) / max(1e-15, scaled.mean()))
    if linearity_delta > 2e-6:
        raise ValueError(
            "Pre-readout brightness response is not linear enough to reuse cached means."
        )
    result = {
        "results": results,
        "brightness_linearity_relative_delta": linearity_delta,
        "specification_sha256": file_sha256(package / "specification.json"),
        "source_id": image["id"],
        "roi_id": roi["id"],
        "native_nyquist_qualified": False,
        "psf_refinement_qualified": False,
        "original_information_recovered": False,
        "limitations": (
            "The input is a bilinear RGB spectral proxy; no finer source texture, "
            "diffraction or CRA accuracy is established."
        ),
    }
    write_json(path, result)
    return result


def _collect(workspace, package, spec, images, candidates, progress, *, expand=None):
    rows = []
    for image in images:
        source = Path(image["source_path"])
        if not source.exists():
            source = package / "inputs" / image["id"] / "original.jpg"
        if file_sha256(source) != image["source_sha256"]:
            raise ValueError("Original input changed since study freeze.")
        for candidate in candidates:
            for roi in image["rois"]:
                progress(
                    f"{image['split']} {image['id'][:8]} {candidate['id']} "
                    f"{roi['kind']} {roi['id'][-6:]}"
                )
                directory, mean, report, optics, sensor, coords = _mean(
                    workspace, package, spec, image, roi, candidate
                )
                expanded = bool(expand and (image["id"], roi["id"]) in expand)
                repeats = (
                    {0.25: spec["capture"]["expanded_repeats"]}
                    if expanded
                    else spec["capture"]["initial_repeats"]
                )
                rows += capture_series(
                    spec,
                    candidate,
                    image,
                    roi,
                    directory,
                    mean=mean,
                    report=report,
                    optics=optics,
                    sensor=sensor,
                    coordinates=coords,
                    target_repeats=repeats,
                )
    return rows


def run_study(workspace: Path, package: Path, *, stage="full", on_progress=None):
    """Protect shared resumable files against concurrent CLI/Worker execution."""
    if stage not in {"first-photo", "full"}:
        raise ValueError("Unsupported road study execution stage.")
    package = package.resolve()
    with (package / ".execution.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "This package is already running; wait or cancel its current job."
            ) from exc
        try:
            return _run_study(workspace, package, stage=stage, on_progress=on_progress)
        except BaseException as exc:
            write_json(
                package / "progress.json",
                {
                    "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                    "error": str(exc),
                    "recoverable_calculations_preserved": True,
                },
            )
            raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh_first_photo_statistics(package, spec):
    """Correct preliminary intervals against their original, preserved frames."""
    from .road_design_statistics import STATISTICS_METHOD, cnr_with_interval

    path = package / "first-photo.json"
    if not path.exists():
        return
    first = json_load(path)
    rois = {(image["id"], roi["id"]): roi for image in spec["images"] for roi in image["rois"]}
    changed = False
    for record in first["records"]:
        if record["roi_kind"] != "object" or record.get("statistics_method") == STATISTICS_METHOD:
            continue
        frames = package / record["luminance_file"]
        if file_sha256(frames) != record["luminance_sha256"]:
            raise ValueError("Preliminary frame series changed before statistics correction.")
        roi = rois[(record["image_id"], record["roi_id"])]
        mask = package / roi["mask_path"]
        if file_sha256(mask) != roi["mask_sha256"]:
            raise ValueError("Preliminary frozen mask changed.")
        with np.load(mask) as masks:
            corrected = cnr_with_interval(
                np.load(frames, mmap_mode="r"),
                masks["object"],
                masks["background"],
                replicates=spec["statistics"]["bootstrap_replicates"],
                seed=spec["capture"]["seed"],
            )
        if abs(corrected["cnr"] - record["cnr"]) > 1e-10:
            raise ValueError("Statistics correction changed the original CNR point estimate.")
        record.update(corrected)
        changed = True
    if changed:
        archive = package / "diagnostics" / "first-photo-statistics-v1.json"
        if not archive.exists():
            # Read the original document again; the records above were updated
            # in memory, while the preserved source remains unchanged on disk.
            write_json(archive, json_load(path))
        first["statistics_correction"] = {
            "method": STATISTICS_METHOD,
            "original_raw_and_repetition_counts_preserved": True,
        }
        write_json(path, first)


def _run_study(workspace: Path, package: Path, *, stage="full", on_progress=None):
    """Resume immutable calculations; evaluate only after a selection lock."""
    package, workspace = package.resolve(), workspace.resolve()
    spec, binding = bound_specification(package)
    if (package / "decision.json").exists() and (package / "package-manifest.json").exists():
        verify_package(package)
        if stage == "full":
            return json_load(package / "decision.json")
        if (package / "first-photo.json").exists():
            return json_load(package / "first-photo.json")
        ids = [candidate["id"] for candidate in spec["candidates"][:2]]
        return {
            "status": "first_real_photo_executed",
            "records": [
                record
                for record in json_load(package / "selection-records.json")
                if record["image_id"] == spec["images"][0]["id"] and record["candidate_id"] in ids
            ],
            "candidate_ids": ids,
            "design_decision_complete": True,
            "specification_sha256": binding["sha256"],
            "source": "verified complete study",
        }
    _refresh_first_photo_statistics(package, spec)
    start = time.monotonic()

    def progress(message):
        write_json(
            package / "progress.json",
            {
                "status": "running",
                "message": message,
                "elapsed_s": time.monotonic() - start,
                "specification_sha256": binding["sha256"],
            },
        )
        if on_progress:
            on_progress(message)

    if shutil.disk_usage(package).free < spec["resources"]["min_free_disk_bytes"]:
        raise ValueError("Insufficient free disk for the explicitly declared study policy.")
    if stage == "first-photo":
        records = _collect(
            workspace, package, spec, spec["images"][:1], spec["candidates"][:2], progress
        )
        result = {
            "status": "first_real_photo_executed",
            "records": records,
            "candidate_ids": [row["id"] for row in spec["candidates"][:2]],
            "design_decision_complete": False,
            "specification_sha256": binding["sha256"],
        }
        write_json(package / "first-photo.json", result)
        from .road_design_report import build_report

        build_report(package, spec, result, records, [])
        write_json(
            package / "progress.json",
            {"status": "first_photo_completed", "specification_sha256": binding["sha256"]},
        )
        return result
    sampling = sampling_check(workspace, package, spec, progress)
    sampling_ok = {
        row["candidate_id"]: row["converged_for_representative_roi"] for row in sampling["results"]
    }
    lock_path = package / "selection-lock.json"
    selected_images = [image for image in spec["images"] if image["split"] == "selection"]
    if lock_path.exists():
        selection = json_load(lock_path)
        if selection["specification_sha256"] != binding["sha256"]:
            raise ValueError("Existing decision is bound to a different specification.")
        selected_records = json_load(package / "selection-records.json")
        if json_sha256(selected_records) != selection["selection_records_sha256"]:
            raise ValueError("Selection cohort changed after decision lock.")
    else:
        selected_records = _collect(
            workspace, package, spec, selected_images, spec["candidates"], progress
        )
        selection = choose_candidate(spec, selected_records, sampling_ok=sampling_ok)
        expand = {
            (row["image_id"], row["roi_id"])
            for candidate in selection["ranking"]
            for row in candidate["expand_conditions"]
        }
        if expand:
            progress(f"Expanding {len(expand)} uncertain selection ROI comparisons to 128 captures")
            selected_records = _collect(
                workspace,
                package,
                spec,
                selected_images,
                spec["candidates"],
                progress,
                expand=expand,
            )
            selection = choose_candidate(spec, selected_records, sampling_ok=sampling_ok)
        selection.update(
            {
                "specification_sha256": binding["sha256"],
                "selection_records_sha256": json_sha256(selected_records),
                "locked_before_held_out_execution": True,
            }
        )
        write_json(package / "selection-records.json", selected_records)
        write_json(lock_path, selection)
    evaluation_images = [image for image in spec["images"] if image["split"] == "evaluation"]
    active = [
        candidate
        for candidate in spec["candidates"]
        if candidate["id"] in {selection["selected_id"], spec["baseline_id"]}
    ]
    evaluation_records = _collect(workspace, package, spec, evaluation_images, active, progress)
    evaluation = evaluate_selection(
        spec, selection["selected_id"], evaluation_records, sampling_ok=sampling_ok
    )
    expand = {(row["image_id"], row["roi_id"]) for row in evaluation["expand_conditions"]}
    if expand:
        progress(f"Expanding {len(expand)} held-out ROI comparisons crossing acceptance thresholds")
        evaluation_records = _collect(
            workspace, package, spec, evaluation_images, active, progress, expand=expand
        )
        evaluation = evaluate_selection(
            spec, selection["selected_id"], evaluation_records, sampling_ok=sampling_ok
        )
    write_json(package / "evaluation-records.json", evaluation_records)
    result = {
        "schema": "camera_road_design_decision_v1",
        "title": spec["title"],
        "status": evaluation["status"],
        "recommended_id": evaluation["recommended_id"],
        "selection": selection,
        "evaluation": evaluation,
        "sampling": sampling,
        "specification_sha256": binding["sha256"],
        "elapsed_s_this_invocation": time.monotonic() - start,
        "physical_validation": "not_established",
        "commercial_dataset_license": "not_established",
        "scope": (
            "relative designs using stored geometric PSF and saved estimated "
            "QE/readout in an RGB-derived sparse spectral proxy"
        ),
        "limitations": [
            "Original camera blur, noise, sharpening and clipping remain in the input.",
            (
                "Three visible spectral samples and a fixed display basis do not "
                "reconstruct road reflectance."
            ),
            (
                "Lens transmission and relative illumination are unity assumptions; "
                "diffraction/CRA coupling unvalidated."
            ),
            "Sensor electrical values are saved assumptions, not measured product specifications.",
            (
                "Native pixel pitch is preserved; full sensor extent is a virtual "
                "common field, not product active-array FOV."
            ),
            (
                "One representative source-grid check does not qualify all "
                "optical/pixel physics or native Nyquist."
            ),
            "CNR is signal separation relative to temporal noise, not detection accuracy.",
            (
                "12 disjoint-log images provide an experiment, not a statistical "
                "guarantee for all roads."
            ),
        ],
    }
    write_json(package / "decision.json", result)
    from .road_design_report import build_report

    build_report(package, spec, result, selected_records, evaluation_records)
    progress("Design package completed")
    write_json(
        package / "progress.json",
        {
            "status": "completed",
            "decision": result["status"],
            "recommended_id": result["recommended_id"],
            "specification_sha256": binding["sha256"],
        },
    )
    return result


def verify_package(package: Path):
    spec, binding = bound_specification(package)
    manifest = json_load(package / "package-manifest.json")
    if manifest["specification_sha256"] != binding["sha256"]:
        raise ValueError("Package specification binding changed.")
    for relative, sha in manifest["files"].items():
        path = (package / relative).resolve()
        if not path.is_relative_to(package.resolve()) or file_sha256(path) != sha:
            raise ValueError("Package artifact missing or changed: " + relative)
    for image in spec["images"]:
        source = package / "inputs" / image["id"] / "original.jpg"
        if file_sha256(source) != image["source_sha256"]:
            raise ValueError("Original photograph missing or changed.")
    return {
        "status": "verified",
        "files": len(manifest["files"]),
        "specification_sha256": binding["sha256"],
    }

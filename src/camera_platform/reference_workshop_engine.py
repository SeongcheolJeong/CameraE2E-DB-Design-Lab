"""Reference bench: pinned patent rays × reviewed pixel structure × preserved-RAW ISP."""

from __future__ import annotations

import copy
import html
import json
import shutil
import time
from pathlib import Path

import numpy as np

from . import design_workshop_engine as shared
from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as pixel
from .design_workshop_engine import write_json
from .registry import digest
from .virtual_lab_engine import XYZ_TO_RGB, _process, _spectra
from .virtual_lab_job import sha256

KINDS = ("resolution", "color", "uniform", "texture")
WAVE = shared.WAVE


def _notify(callback, message):
    if callback:
        callback({"stage": message})


def optical_specification(workspace, spec):
    result = optics.template(workspace)
    result["controls"].update({k: spec["lens"][k] for k in ("curvature_scale", "sensor_shift_um")})
    result["optical_profile"] = spec["lens"]["optical_profile"]
    return optics.normalize(result)


def optical_directory(workspace, spec, cache):
    request = optical_specification(workspace, spec)
    return Path(cache) / "optics" / optics.cache_key(workspace, request)


def _solution(workspace, spec, cache, callback):
    folder = optical_directory(workspace, spec, cache)
    optics.solve(workspace, optical_specification(workspace, spec), folder, on_progress=callback)
    return optics.load_solution(folder), folder


def _fields(solution):
    return solution["result"]["fields"]


def _success(field):
    return field["status"] in {"succeeded", "PROPOSED"}


def _apply(solution, field, photons, response):
    return optics.apply_field(
        solution,
        field["id"],
        photons,
        angular_response=lambda theta, phi, wave: pixel.absolute_angular_response(
            response, theta, phi, wave
        ),
    )


def _rate(workspace, solution, field, response, spec, kind, cache, *, return_planes=False):
    from .reference_workshop_job import fingerprint

    key = digest(
        {
            "optics": solution["result"]["revision"],
            "field": field["id"],
            "pixel": response,
            "kind": kind,
            "luminance": spec["luminance_cd_m2"],
            "planes": return_planes,
            "code": fingerprint(),
        }
    )
    folder = Path(cache) / "rates" / key
    if (folder / "metadata.json").exists():
        metadata = json.loads((folder / "metadata.json").read_text())
        if sha256(folder / "rate.npy") != metadata["sha256"]:
            raise ValueError("Reference mean-response cache changed.")
        result = (np.load(folder / "rate.npy"), metadata["contract"], True)
        if return_planes:
            if sha256(folder / "planes.npy") != metadata["planes_sha256"]:
                raise ValueError("Reference plane-response cache changed.")
            return (*result, np.load(folder / "planes.npy"))
        return result
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    if kind == "demosaic_chirp":
        from .reference_isp_scene import chirp_scene

        photons, _ = chirp_scene(field["geometry"], spectral)
    else:
        photons, _ = shared._scene(kind, field["geometry"], spectral)
    applied = _apply(solution, field, photons, response)
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / "rate.npy", applied["mean_electron_rate"])
    extra = {}
    if return_planes:
        np.save(folder / "planes.npy", applied["planes_electron_rate"])
        extra["planes_sha256"] = sha256(folder / "planes.npy")
    write_json(
        folder / "metadata.json",
        {"sha256": sha256(folder / "rate.npy"), "contract": applied["metadata"], **extra},
    )
    result = (applied["mean_electron_rate"], applied["metadata"], False)
    return (*result, applied["planes_electron_rate"]) if return_planes else result


def _calibrations(workspace, spec, cache, callback):
    from .reference_workshop_job import defaults, fingerprint

    baseline = defaults()
    baseline["lens"]["optical_profile"] = spec["lens"]["optical_profile"]
    baseline["luminance_cd_m2"] = 1000
    solution, _ = _solution(workspace, baseline, cache, callback)
    cell = pixel.build_cell(workspace)
    key = digest(
        {
            "optical_revision": solution["result"]["revision"],
            "pixel": cell["cell_sha256"],
            "code": fingerprint(),
            "method": "baseline-field-white-and-color-v1",
        }
    )
    folder = Path(cache) / "calibrations" / key
    path = folder / "calibration.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["sha256"] != digest(saved["data"]):
            raise ValueError("Frozen Reference calibration was modified.")
        return saved["data"]
    center = next(f for f in _fields(solution) if f["id"] == "center")
    if not _success(center):
        raise ValueError(
            "The selected optical profile cannot calibrate its baseline center: "
            + str(center.get("error", center.get("issues")))
        )
    response = pixel.build_response(cell, wavelength_nm=WAVE)
    spectral = _spectra(workspace, "D65", 1000)
    shape = center["geometry"]["input_shape_row_col"]
    photons = np.broadcast_to(spectral["white"], (*shape, len(WAVE)))
    applied = _apply(solution, center, photons, response)
    sites = np.asarray(applied["center_electron_rate_per_band"]) * 0.004
    rgb = sites[:, [0, 1, 3]].copy()
    rgb[:, 1] = sites[:, 1:3].mean(axis=1)
    target = spectral["xyz_per_photon"] * spectral["white"][:, None] / 1000
    scale = max(float(np.max(rgb)), 1e-30)
    x, white = rgb / scale, rgb.sum(axis=0) / scale
    lhs = np.block([[x.T @ x, white[:, None]], [white[None, :], np.zeros((1, 1))]])
    matrix = (
        np.linalg.solve(lhs, np.vstack([x.T @ target, target.sum(axis=0)]))[:3]
        / scale
        @ XYZ_TO_RGB.T
    )
    color = {
        "matrix": matrix.tolist(),
        "reference_exposure_ms": 4,
        "reference_luminance_cd_m2": 1000,
        "baseline_cell_sha256": cell["cell_sha256"],
        "baseline_optical_revision": solution["result"]["revision"],
        "method": "baseline_center_pupil_spectral_fit_with_white_constraint",
        "per_candidate_fit": False,
    }
    rates = {
        f["id"]: _rate(workspace, solution, f, response, baseline, "uniform", cache)[0]
        for f in _fields(solution)
        if _success(f)
    }
    lsc = pixel.build_lsc(
        rates,
        center_id="center",
        max_gain=4,
        provenance={
            "baseline_optical_revision": solution["result"]["revision"],
            "baseline_cell_sha256": cell["cell_sha256"],
            "luminance_cd_m2": 1000,
        },
    )
    result = {"color": color, "lsc": lsc, "identity": key, "baseline_only": True}
    folder.mkdir(parents=True, exist_ok=True)
    write_json(path, {"data": result, "sha256": digest(result)})
    return result


def _render_field(root, capture, spec, optical_field, response, cell, calibrations):
    tuning_spec = {**spec, "lens": {**spec["lens"], "pixel_pitch_um": cell["pixel_pitch_um"]}}
    gains = (
        pixel.lsc_gain_map(
            calibrations["lsc"],
            optical_field["id"],
            shape=tuple(capture["geometry"]["native_shape_row_col"]),
        )
        if spec["isp"]["lsc_enabled"]
        else None
    )

    def process(raw, readout, matrix, tuning):
        if not tuning["lsc_enabled"]:
            return _process(raw, readout, matrix, tuning)
        # Gain the signed, black-subtracted samples once; preserve acquired integer RAW.
        offset = float(tuning.get("black_offset_dn", 0))
        signed = (raw.astype(float) - offset - readout["black_level_dn"]) * gains
        return _process(signed + readout["black_level_dn"] + offset, readout, matrix, tuning)

    from . import reference_isp

    observer = None
    render_capture = copy.deepcopy(capture)
    render_capture["calibration"] = calibrations["color"]
    if spec["isp"].get("schema") == reference_isp.SCHEMA:
        from .reference_isp_lab import Processor

        observer = Processor(
            render_capture, spec["isp"], gains, reference_matrix=capture["calibration"]["matrix"]
        )
        process = observer
    rendered = shared._render(
        root,
        render_capture,
        tuning_spec,
        optical_field,
        response,
        cell,
        process=process,
        observer=observer,
        texture_reference_process=observer.bypass if observer else None,
    )
    if observer is not None:
        observer.finalize_tests(rendered["tests"])
        rendered["tests"]["isp_lab"] = observer.tests
        rendered["metrics"]["processed_temporal_noise"] = observer.tests.get("temporal_noise_luma")
        write_json(root / "render.json", rendered)
    prefix = "fields/" + optical_field["id"] + "/"
    images = {
        kind: {stage: prefix + path for stage, path in paths.items()}
        for kind, paths in rendered["images"].items()
    }
    diagnostics = copy.deepcopy(optical_field)
    if diagnostics.get("images"):
        diagnostics["images"] = {
            stage: "optics/" + path for stage, path in diagnostics["images"].items()
        }
    return {
        "id": optical_field["id"],
        "label": optical_field.get("label", optical_field["id"]),
        "status": "succeeded",
        "geometry": optical_field["geometry"],
        "metrics": rendered["metrics"],
        "tests": rendered["tests"],
        "images": images,
        "optics": diagnostics,
        "capture_files": capture["captures"],
        "lsc_applied": spec["isp"]["lsc_enabled"],
        "calibration_id": calibrations["identity"],
    }


def _summary(root, spec, solution, cell, calibrations, fields):
    reference = {
        "lens": solution["result"].get("reference", solution["result"].get("source", {})),
        "pixel": {
            "source_report": cell["source_report"],
            "pixel_pitch_um": cell["pixel_pitch_um"],
            "cross_section": cell["cross_section"],
            "provenance": cell["provenance"],
            "readout": cell["readout"],
        },
    }
    result = {
        "schema": "camera_reference_workshop_result_v1",
        "configuration": spec,
        "reference": reference,
        "prescription": solution["result"].get("prescription"),
        "fields": fields,
        "successful_fields": sum(f["status"] == "succeeded" for f in fields),
        "failed_fields": sum(f["status"] != "succeeded" for f in fields),
        "calibration": {
            "id": calibrations["identity"],
            "color": calibrations["color"],
            "lsc_file": "calibration.json",
            "lsc_fixed_baseline": True,
            "lsc_max_gain": 4,
        },
        "optical_revision": solution["result"]["revision"],
        "pixel": {"cell": cell, "response_file": "pixel-response.json"},
        "raw_preserved": True,
        "physical_validation": "not_established",
        "limitations": [
            "Virtual field probes, not a full product sensor frame.",
            "Geometric profile excludes diffraction; wavefront failures are not replaced.",
            "Local shift-invariant PSF; no cross-field image reconstruction.",
            "Independent virtual RGGB probes each declare local CFA origin [0,0].",
            "Reviewed structure dimensions with assumed material response, collection and readout.",
            "Frozen baseline LSC; declared immutable color calibration; no frame brightness fit.",
        ],
    }
    write_json(root / "render.json", result)
    return result


def run(workspace, output_dir, spec, cache, on_progress=None):
    start = time.monotonic()
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    solution, optical_path = _solution(workspace, spec, cache, on_progress)
    calibrations = _calibrations(workspace, spec, cache, on_progress)
    cell = pixel.build_cell(workspace, cfa_thickness_scale=spec["pixel"]["cfa_thickness_scale"])
    response = pixel.build_response(cell, wavelength_nm=WAVE)
    write_json(root / "calibration.json", calibrations)
    write_json(root / "pixel-cell.json", cell)
    write_json(root / "pixel-response.json", response)
    write_json(root / "optical-response.json", solution["result"])
    shutil.copytree(optical_path, root / "optics", dirs_exist_ok=True)
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    fields = []
    captures = {}
    for fi, field in enumerate(_fields(solution)):
        if not _success(field):
            fields.append({**field, "status": "failed", "images": {}, "metrics": {}})
            continue
        field_root = root / "fields" / field["id"]
        field_root.mkdir(parents=True, exist_ok=True)
        records = {}
        kinds = (*KINDS, "demosaic_chirp") if spec.get("chart_suite") == "isp_lab_v1" else KINDS
        for kind in kinds:
            _notify(on_progress, f"{field['id']} · {kind} · native RAW")
            rates = _rate(
                workspace,
                solution,
                field,
                response,
                spec,
                kind,
                cache,
                return_planes=kind == "demosaic_chirp",
            )
            rate, contract, reused = rates[:3]
            folder = field_root / kind
            folder.mkdir(exist_ok=True)
            np.save(folder / "mean-electron-rate.npy", rate)
            if kind == "demosaic_chirp":
                from .reference_isp_scene import chirp_scene

                _, pattern = chirp_scene(field["geometry"], spectral)
                reference = pattern[..., None] * np.asarray(spectral["white_xyz"]) @ XYZ_TO_RGB.T
                np.save(folder / "planes-electron-rate.npy", rates[3])
            else:
                reference = shared._reference_image(kind, rate.shape, spectral)
            np.save(folder / "source-reference-linear.npy", reference.astype(np.float32))
            frames = []
            for repeat in range(spec["frames"]):
                seed = spec["seed"] + fi * 32 + repeat
                capture = shared._capture(rate, cell["electrical_model"], spec, seed)
                path = folder / f"raw-{repeat:02d}.npy"
                np.save(path, capture["arrays"]["raw_dn"])
                frames.append({"file": path.name, "sha256": sha256(path), "seed": seed})
                if repeat == 0:
                    write_json(folder / "capture.json", capture["summary"])
            records[kind] = {
                "raw": frames,
                "rate_sha256": sha256(folder / "mean-electron-rate.npy"),
                "source_reference_sha256": sha256(folder / "source-reference-linear.npy"),
                "optical_pixel_contract": contract,
                "response_cache_reused": reused,
            }
            if kind == "demosaic_chirp":
                records[kind]["planes_sha256"] = sha256(folder / "planes-electron-rate.npy")
        capture = {
            "schema": "camera_reference_field_capture_v1",
            "configuration": spec,
            "geometry": field["geometry"],
            "captures": records,
            "readout": {**cell["readout"], "gain": spec["gain"]},
            "calibration": calibrations["color"],
            "scene_reference": {
                "reference_xyz": spectral["reference_xyz"].tolist(),
                "white_xyz": spectral["white_xyz"].tolist(),
                "asset": spectral["asset"],
                "chromatic_adaptation": spectral["chromatic_adaptation"],
            },
        }
        write_json(field_root / "capture.json", capture)
        captures[field["id"]] = capture
        fields.append(_render_field(field_root, capture, spec, field, response, cell, calibrations))
    write_json(
        root / "capture.json",
        {
            "configuration": spec,
            "fields": captures,
            "calibration_sha256": sha256(root / "calibration.json"),
        },
    )
    result = _summary(root, spec, solution, cell, calibrations, fields)
    result["performance"] = {
        "elapsed_seconds": time.monotonic() - start,
        "wall_clock_is_vehicle_latency": False,
    }
    return result


def replay(source, output_dir, spec, on_progress=None, *, color_calibration=None):
    start = time.monotonic()
    source, root = Path(source), Path(output_dir)
    source, root = source.resolve(), root.resolve()
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise ValueError("Replay requires disjoint source and output directories.")
    capture = json.loads((source / "capture.json").read_text())
    from .reference_workshop_job import acquisition_configuration

    if capture.get("preview_scope"):
        raise ValueError("Replay requires a complete preserved acquisition, not a preview.")
    if acquisition_configuration(spec) != acquisition_configuration(capture["configuration"]):
        raise ValueError("Same-RAW replay may change only ISP and name.")
    if sha256(source / "calibration.json") != capture["calibration_sha256"]:
        raise ValueError("Preserved baseline calibration changed.")
    shutil.copytree(
        source,
        root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "manifest.json", "progress.json", "result.json", "report.html"
        ),
    )
    solution = {"result": json.loads((root / "optical-response.json").read_text())}
    cell = json.loads((root / "pixel-cell.json").read_text())
    response = json.loads((root / "pixel-response.json").read_text())
    calibrations = json.loads((root / "calibration.json").read_text())
    requested_color = spec["isp"].get("color_calibration_id")
    if requested_color:
        from .reference_isp_color import validate_binding

        path = root / "color-calibrations" / (requested_color + ".json")
        if color_calibration is None:
            if not path.is_file():
                raise ValueError("Selected immutable color calibration is unavailable.")
            color_calibration = json.loads(path.read_text())
        validate_binding(color_calibration, source)
        if color_calibration["id"] != requested_color:
            raise ValueError("Color calibration identity mismatch.")
        path.parent.mkdir(exist_ok=True)
        write_json(path, color_calibration)
        calibrations = copy.deepcopy(calibrations)
        calibrations["color"] = color_calibration
    fields = []
    for field in _fields(solution):
        if not _success(field):
            fields.append({**field, "status": "failed", "images": {}, "metrics": {}})
            continue
        _notify(on_progress, f"{field['id']} · same RAW → fixed LSC / ISP")
        fields.append(
            _render_field(
                root / "fields" / field["id"],
                capture["fields"][field["id"]],
                spec,
                field,
                response,
                cell,
                calibrations,
            )
        )
    result = _summary(root, spec, solution, cell, calibrations, fields)
    result["performance"] = {
        "elapsed_seconds": time.monotonic() - start,
        "acquisition_reused": True,
    }
    return result


def preview(source, output_dir, spec, on_progress=None, *, field_id, chart, color_calibration=None):
    """Process one preserved frame without acquisition or study/temporal metrics.

    A preview deliberately does not call the full chart renderer: that path
    processes every repeat and creates a deterministic texture readout. Only the
    selected chart's first saved RAW and its source artifacts are copied here.
    """
    from . import reference_isp
    from .reference_isp_lab import Processor
    from .reference_workshop_job import acquisition_configuration
    from .virtual_lab_engine import _png

    start = time.monotonic()
    source, root = Path(source).resolve(), Path(output_dir).resolve()
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise ValueError("Preview requires disjoint source and output directories.")
    if spec["isp"].get("schema") != reference_isp.SCHEMA:
        raise ValueError("Single-frame preview requires Reference ISP v2.")
    profile = reference_isp.validate_profile(spec["isp"])
    capture = json.loads((source / "capture.json").read_text())
    if capture.get("preview_scope"):
        raise ValueError("Preview requires a complete preserved acquisition source.")
    if acquisition_configuration(spec) != acquisition_configuration(capture["configuration"]):
        raise ValueError("Same-RAW preview may change only ISP and name.")
    if sha256(source / "calibration.json") != capture["calibration_sha256"]:
        raise ValueError("Preserved baseline calibration changed.")
    solution = {"result": json.loads((source / "optical-response.json").read_text())}
    field = next((f for f in _fields(solution) if f["id"] == field_id), None)
    if field is None or not _success(field) or field_id not in capture["fields"]:
        raise ValueError("Selected preview field has no successful preserved capture.")
    original = capture["fields"][field_id]
    if chart not in (*KINDS, "demosaic_chirp") or chart not in original["captures"]:
        raise ValueError("Selected chart is absent from this preserved acquisition.")
    record = copy.deepcopy(original["captures"][chart])
    if not record.get("raw"):
        raise ValueError("Selected chart has no preserved RAW frame.")
    record["raw"] = record["raw"][:1]
    raw_record = record["raw"][0]
    if Path(raw_record["file"]).name != raw_record["file"]:
        raise ValueError("Invalid preserved RAW filename.")
    relative = Path("fields") / field_id / chart
    expected = {
        raw_record["file"]: raw_record["sha256"],
        "mean-electron-rate.npy": record["rate_sha256"],
        "source-reference-linear.npy": record["source_reference_sha256"],
    }
    if record.get("planes_sha256"):
        expected["planes-electron-rate.npy"] = record["planes_sha256"]
    for name, checksum in expected.items():
        path = source / relative / name
        if not path.is_file() or sha256(path) != checksum:
            raise ValueError(f"Preserved preview artifact changed: {name}")
    cell = json.loads((source / "pixel-cell.json").read_text())
    calibrations = json.loads((source / "calibration.json").read_text())
    requested_color = profile.get("color_calibration_id")
    if requested_color:
        from .reference_isp_color import validate_binding

        if color_calibration is None:
            path = source / "color-calibrations" / (requested_color + ".json")
            if not path.is_file():
                raise ValueError("Selected immutable color calibration is unavailable.")
            color_calibration = json.loads(path.read_text())
        validate_binding(color_calibration, source)
        if color_calibration["id"] != requested_color:
            raise ValueError("Color calibration identity mismatch.")
        calibrations = copy.deepcopy(calibrations)
        calibrations["color"] = color_calibration
    gains = (
        pixel.lsc_gain_map(
            calibrations["lsc"],
            field_id,
            shape=tuple(original["geometry"]["native_shape_row_col"]),
        )
        if profile["lsc_enabled"]
        else None
    )
    scope = {
        "kind": "selected_field_chart_first_preserved_frame",
        "field_id": field_id,
        "chart": chart,
        "frame_index": 0,
        "processed_frame_count": 1,
        "temporal_noise": "not_evaluated_single_frame",
        "candidate_selection": "not_evaluated",
        "all_field_constraints": "not_evaluated",
        "comparison_support": "selected_frame_only_not_repeat_common_support",
    }
    rendered_capture = copy.deepcopy(original)
    rendered_capture["captures"] = {chart: record}
    rendered_capture["calibration"] = calibrations["color"]
    processor = Processor(
        rendered_capture, profile, gains, reference_matrix=original["calibration"]["matrix"]
    )
    _notify(on_progress, f"{field_id} · {chart} · preserved frame 0 preview")
    raw = np.load(source / relative / raw_record["file"], allow_pickle=False)
    linear, processed, display = processor(
        raw, original["readout"], calibrations["color"]["matrix"], profile
    )
    folder = root / relative
    folder.mkdir(parents=True, exist_ok=True)
    for name in expected:
        shutil.copy2(source / relative / name, folder / name)
        if sha256(folder / name) != expected[name]:
            raise ValueError(f"Preserved preview artifact changed while copying: {name}")
    for name in (
        "calibration.json",
        "pixel-cell.json",
        "pixel-response.json",
        "optical-response.json",
    ):
        shutil.copy2(source / name, root / name)
    if requested_color:
        (root / "color-calibrations").mkdir(exist_ok=True)
        write_json(root / "color-calibrations" / (requested_color + ".json"), color_calibration)
    processor.frame(chart, 0, folder, raw, linear, processed)
    white = profile["display_white_level"]
    readout = original["readout"]
    _png(folder / "display.png", display)
    _png(folder / "linear.png", linear / white, gamma=True)
    _png(
        folder / "raw.png",
        (raw.astype(float) - readout["black_level_dn"])
        / (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"]),
    )
    _png(
        folder / "source-reference.png",
        np.load(folder / "source-reference-linear.npy") / white,
        gamma=True,
    )
    _png(folder / "preview-valid-mask.png", processor.last["valid_mask"].astype(float))
    np.save(folder / "linear.npy", linear.astype(np.float32))
    np.save(folder / "processed.npy", processed.astype(np.float32))
    paths = {
        "display": "display.png",
        "linear": "linear.png",
        "reference": "source-reference.png",
        "raw_preview": "raw.png",
        "raw": raw_record["file"],
        "mean": "mean-electron-rate.npy",
        "raw_lsc": "raw_lsc.png",
        "raw_denoise": "raw_denoise.png",
        "demosaic": "demosaic.png",
        "sharpen_delta": "sharpen_delta.png",
        "valid_mask": "preview-valid-mask.png",
    }
    diagnostics = copy.deepcopy(field)
    # The preview does not copy the full optical cache. Avoid dangling image links.
    diagnostics.pop("images", None)
    item = {
        "id": field_id,
        "label": field.get("label", field_id),
        "status": "succeeded",
        "geometry": field["geometry"],
        "images": {chart: {k: (relative / v).as_posix() for k, v in paths.items()}},
        "metrics": {
            "mean_electrons": float(np.load(folder / "mean-electron-rate.npy").mean())
            * spec["exposure_ms"]
            / 1000,
            "delta_e00": None,
            "temporal_noise_dn": None,
            "processed_temporal_noise": None,
            "texture_contrast_ratio": None,
        },
        "tests": {"preview": scope},
        "optics": diagnostics,
        "capture_files": {chart: record},
        "lsc_applied": profile["lsc_enabled"],
        "calibration_id": calibrations["identity"],
    }
    provenance = {
        "source_capture_sha256": sha256(source / "capture.json"),
        "source_calibration_sha256": capture["calibration_sha256"],
        "raw_sha256": raw_record["sha256"],
        "source_artifact_sha256": expected,
        "preview_scope": scope,
    }
    write_json(root / "preview.json", provenance)
    write_json(root / "fields" / field_id / "capture.json", rendered_capture)
    write_json(
        root / "capture.json",
        {
            "configuration": capture["configuration"],
            "fields": {field_id: rendered_capture},
            "calibration_sha256": capture["calibration_sha256"],
            "preview_scope": scope,
        },
    )
    result = _summary(root, spec, solution, cell, calibrations, [item])
    result.update(preview_scope=scope, provenance=provenance, acquisition_reused=True)
    result["limitations"].append(
        "Single preserved frame preview; no temporal-noise or full-study selection metrics."
    )
    result["performance"] = {
        "elapsed_seconds": time.monotonic() - start,
        "acquisition_reused": True,
        "processed_frame_count": 1,
        "wall_clock_is_vehicle_latency": False,
    }
    write_json(root / "render.json", result)
    return result


def report(result, root):
    candidates = result.get("candidates", [result])
    sections = []
    if result.get("study"):
        sections.append(
            "<h2>ISP study · fixed source</h2><pre>"
            + html.escape(json.dumps(result["study"], indent=2, ensure_ascii=False))
            + "</pre>"
        )
    for item in candidates:
        prefix = item["id"] + "/" if "candidates" in result else ""
        sections.append(
            "<h2>" + html.escape(item.get("configuration", {}).get("name", "Reference")) + "</h2>"
        )
        sections.append(
            "<pre>" + html.escape(json.dumps(item.get("configuration", {}), indent=2)) + "</pre>"
        )
        for field in item.get("fields", []):
            sections.append(
                "<h3>" + html.escape(field["id"]) + " · " + html.escape(field["status"]) + "</h3>"
            )
            if field["status"] != "succeeded":
                sections.append(
                    "<pre>"
                    + html.escape(json.dumps(field.get("error", field.get("issues", []))))
                    + "</pre>"
                )
                continue
            for kind, paths in field["images"].items():
                source = html.escape(prefix + paths["display"], quote=True)
                label = html.escape(kind)
                sections.append(
                    f'<figure><img src="{source}"><figcaption>{label}</figcaption></figure>'
                )
            sections.append(
                "<pre>" + html.escape(json.dumps(field["metrics"], indent=2)) + "</pre>"
            )
            if field.get("tests", {}).get("isp_lab"):
                sections.append(
                    "<details><summary>ISP diagnostics / withheld color</summary><pre>"
                    + html.escape(
                        json.dumps(
                            {
                                "isp": field["tests"]["isp_lab"],
                                "color": field["tests"].get("color"),
                            },
                            indent=2,
                        )
                    )
                    + "</pre></details>"
                )
    (Path(root) / "report.html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>Reference Bench</title>'
        "<style>body{font:16px system-ui;max-width:1200px;margin:auto;padding:30px}"
        "figure{display:inline-block;margin:8px}img{width:256px}"
        "pre{white-space:pre-wrap}</style>"
        "<h1>특허 렌즈 × TechInsights 구조 · Reference 설계 비교</h1>"
        "<p>가상 시야별 탐침. 원래 pitch 유지. 기하 프로필은 회절을 포함하지 않습니다.</p>"
        + "".join(sections)
        + "</html>"
    )

"""Joint aperture/exposure demo on a declared transverse local-motion model.

Saved full R/Gr/Gb/B rate planes are translated before CFA sampling. A box
exposure integrates their native linear interpolant exactly; it does not blur
an already mosaiced or noisy RAW. This is local pinhole chart motion, not a
new scene/optical solve or a distortion-aware moving-pedestrian renderer.
"""

from __future__ import annotations

import copy
import math
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np
from scipy.ndimage import convolve1d

from . import db_design_conditions as acquisition
from . import db_design_motion_metrics as metrics
from . import design_workshop_engine as shared
from .virtual_lab_job import sha256

SCHEMA = "camera_db_aperture_motion_result_v1"
MODEL_SCHEMA = "camera_native_planar_transverse_motion_v1"
ISP_HALO = 8  # Existing Reference comparison support erodes every ISP source by 8px.
SCOPE = (
    "D65 native local charts, fixed camera; horizontal constant transverse object velocity "
    "at declared axial depth. Pinhole image speed uses a common 550nm paraxial focal length, "
    "not a measured peripheral distortion derivative. The saved native linear interpolant "
    "of each pre-mosaic R/Gr/Gb/B optical/pixel response is integrated over exposure. "
    "Distance sets nominal image velocity only: original chart scale, focus, finite-conjugate "
    "conditions and source PSF are unchanged. "
    "No recovered sub-native scene detail, 3D occlusion, independently moving foreground, "
    "rolling shutter, camera motion or physical product validation. Fixed CCM/ISP/display."
)


def _positive(value, name, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(name + " must be finite and numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(name + " is outside its positive domain")
    return value


def image_velocity_px_s(speed_m_s, distance_m, focal_length_mm, pixel_pitch_um):
    """Nominal transverse pinhole motion at axial depth, without lens distortion."""
    speed = _positive(speed_m_s, "object speed", zero=True)
    depth = _positive(distance_m, "object axial depth")
    focal = _positive(focal_length_mm, "paraxial focal length")
    pitch = _positive(pixel_pitch_um, "pixel pitch")
    return focal / (pitch * .001) * speed / depth


def box_kernel(blur_length_px):
    """Exact box integral of native triangular interpolation basis functions.

    An exposure centered on t=0 samples positions in [-L/2,+L/2]. Each weight
    is the integral of max(0, 1-|position-offset|)/L over this interval. The
    L=0 identity is explicit so a stationary scene never incurs interpolation.
    """
    length = _positive(blur_length_px, "motion blur length", zero=True)
    if length == 0:
        return np.array([1.0])
    if length <= 2:
        return np.array([length / 8, 1 - length / 4, length / 8])
    if length > 512:
        raise ValueError("Native motion support exceeds the bounded saved chart")
    half = length / 2
    radius = math.ceil(half + 1)
    offsets = np.arange(-radius, radius + 1, dtype=float)

    # Integrate each linear half by width times midpoint height. Unlike a
    # difference of CDFs this stays stable for tiny subpixel displacements.
    left0, left1 = np.maximum(-half, offsets - 1), np.minimum(half, offsets)
    right0, right1 = np.maximum(-half, offsets), np.minimum(half, offsets + 1)
    left = np.maximum(0, left1 - left0) * np.maximum(0, (left0 + left1) / 2 - offsets + 1)
    right = np.maximum(0, right1 - right0) * np.maximum(0, offsets + 1 - (right0 + right1) / 2)
    weights = (left + right) / length
    # Endpoints exactly outside the triangle support carry no contribution.
    nonzero = np.flatnonzero(weights > 0)
    weights = weights[nonzero[0]:nonzero[-1] + 1]
    if len(weights) % 2 != 1 or not np.allclose(weights, weights[::-1], atol=2e-14):
        raise ValueError("Motion integration must retain a centered symmetric kernel")
    if not math.isclose(float(weights.sum()), 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("Motion integration lost exposure energy")
    return weights


def integrate_planes(planes, blur_length_px):
    values = np.asarray(planes)
    if (values.ndim != 3 or values.shape[2] != 4 or values.dtype.kind not in "iuf"
            or not np.isfinite(values).all() or np.any(values < 0)):
        raise ValueError("Motion input requires finite nonnegative full R/Gr/Gb/B rate planes")
    weights = box_kernel(blur_length_px)
    if len(weights) == 1:
        return values.copy()
    # Padding serves previews only. No metric may include this halo; all
    # candidates in a map cell share the conservative largest support crop.
    return convolve1d(values.astype(float), weights, axis=1, mode="nearest")


def remosaic_native(planes):
    values = np.asarray(planes)
    if values.ndim != 3 or values.shape[-1] != 4 or any(n % 2 for n in values.shape[:2]):
        raise ValueError("RGGB remosaic requires even full-plane native geometry")
    mosaic = np.empty(values.shape[:2], dtype=values.dtype)
    for channel, (row, col) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        mosaic[row::2, col::2] = values[row::2, col::2, channel]
    return mosaic


def common_valid_rect(shape, maximum_blur_length_px):
    if tuple(shape) != (256, 256):
        raise ValueError("Motion demo requires the unchanged 256x256 native source")
    length = _positive(maximum_blur_length_px, "maximum motion blur length", zero=True)
    motion_halo = 0 if length == 0 else math.ceil(length / 2 + 1)
    inset = ISP_HALO + motion_halo
    rectangle = [ISP_HALO, shape[0] - ISP_HALO, inset, shape[1] - inset]
    r0, r1, c0, c1 = rectangle
    # The existing predeclared central edge SFR is 40x40. Do not silently
    # change its location, source sampling, exposure or chart when support fails.
    if not (r0 <= 108 and r1 >= 148 and c0 <= 108 and c1 >= 148):
        raise ValueError("Motion plus ISP halo leaves insufficient saved input for the fixed central SFR ROI")
    return rectangle


def source_projection(payloads):
    values = []
    for payload in payloads:
        source = payload["candidate"]["source_identities"]["lens"]["source"]
        paraxial = source["paraxial"]
        focal = _positive(paraxial["effective_focal_length_mm"], "source paraxial EFL")
        wave = paraxial.get("wavelength_nm")
        pitch = _positive(payload["pixel-cell"]["pixel_pitch_um"], "source pixel pitch")
        if any(not math.isclose(float(field["capture"]["geometry"]["pixel_pitch_um"]), pitch,
                                rel_tol=1e-12, abs_tol=1e-12) for field in payload["fields"].values()):
            raise ValueError("Native field and pixel-cell pitch disagree")
        if wave != 550:
            raise ValueError("Motion comparison requires all source focal lengths at 550nm")
        values.append((focal, pitch))
    if not values or any(not np.allclose(value, values[0], rtol=1e-12, atol=1e-12) for value in values):
        raise ValueError("All aperture candidates must share one focal length and pixel pitch")
    return {"effective_focal_length_mm": values[0][0], "pixel_pitch_um": values[0][1],
            "focal_reference_wavelength_nm": 550,
            "focal_basis": "pinned source_identities.lens.source.paraxial.effective_focal_length_mm"}


def motion_metadata(projection, speed, depth, exposure, valid_rect):
    velocity = image_velocity_px_s(speed, depth, projection["effective_focal_length_mm"],
                                   projection["pixel_pitch_um"])
    length = velocity * exposure / 1000
    weights = box_kernel(length)
    return {
        "schema": MODEL_SCHEMA, **projection, "object_speed_m_s": speed,
        "object_distance_m": depth, "distance_definition": "axial depth along optical axis",
        "exposure_ms": exposure, "velocity_px_s": velocity, "blur_length_px": length,
        "direction": "horizontal", "camera_motion": "fixed",
        "time_reference": "target midpoint at exposure center; [-exposure/2,+exposure/2]",
        "projection": "local_pinhole_transverse_constant_velocity",
        "distortion_derivative_applied": False,
        "interpolation": "native_linear_triangle_basis_exact_box_integral",
        "kernel_weights": weights.tolist(),
        "kernel_offsets_native_px": list(range(-(len(weights) // 2), len(weights) // 2 + 1)),
        "valid_rect_row_col": list(valid_rect), "isp_support_halo_native_px": ISP_HALO,
        "edge_policy": "nearest_padding_for_preview_only; common_valid_rectangle_excludes_padding_and_ISP_halo",
        "processing_order": "full R/Gr/Gb/B rates → motion integral → remosaic → independent electrical capture → fixed ISP",
        "assumptions": SCOPE,
    }


def prepare_payload(payload, metadata, exposure_index, requested_f_number):
    """Transform independent native response planes, retaining their original hashes."""
    result = {**payload, "candidate": copy.deepcopy(payload["candidate"]), "fields": {}}
    source_id = payload["candidate"]["id"]
    result["source_candidate_id"] = source_id
    result["candidate"].update(
        id=f"{source_id}-e{exposure_index:02d}", source_candidate_id=source_id,
        exposure_ms=metadata["exposure_ms"], requested_f_number=requested_f_number,
        motion=copy.deepcopy(metadata),
        label=f"F/{requested_f_number:g} · {metadata['exposure_ms']:g} ms",
    )
    for field_id, field in payload["fields"].items():
        values = {}
        for chart, arrays in field["arrays"].items():
            original_planes = arrays["planes-electron-rate.npy"]
            original_rate = arrays["mean-electron-rate.npy"]
            # This prevents a source with a different CFA origin/site convention
            # from being silently interpreted as our full-plane RGGB contract.
            if not np.array_equal(remosaic_native(original_planes), original_rate):
                raise ValueError("Saved full planes do not reproduce their bound native mosaic")
            integrated = integrate_planes(original_planes, metadata["blur_length_px"])
            values[chart] = {
                **arrays, "planes-electron-rate.npy": integrated,
                "mean-electron-rate.npy": remosaic_native(integrated),
            }
        result["fields"][field_id] = {**field, "arrays": values}
    return result


def _camera(root, payload, condition, projection, depth, exposure, exposure_index, requested_f, identity, notify, check):
    metadata = motion_metadata(projection, condition["object_speed_m_s"], depth, exposure,
                               condition["common_valid_rect_row_col"])
    prepared = prepare_payload(payload, metadata, exposure_index, requested_f)
    acquisition_condition = {**condition, "exposure_ms": exposure, "motion": metadata, "_scope": SCOPE}
    candidate = acquisition._camera(root / "conditions", prepared, acquisition_condition,
                                    identity, notify, check)
    # The detailed artifact retains every repeated RAW hash, complete optics,
    # calibration and ISP diagnostics. The registry receives only its index.
    candidate["motion"] = metadata
    return candidate


def comparison(root, candidates, f_numbers, policy, valid_rect, baseline_id):
    rows = []
    for candidate in candidates:
        source = candidate["source_identities"]["lens"]["source"]
        condition = source.get("pupil_condition")
        applied = condition["effective_f_number"] if condition else source["paraxial"]["effective_f_number"]
        fields = []
        for field in candidate["result"]["fields"]:
            measured = metrics.measure_field(Path(root) / candidate["prefix"] / "fields" / field["id"],
                                             field, valid_rect=valid_rect)
            fields.append({"field_id": field["id"], **measured})
        rows.append({
            "candidate_id": candidate["id"], "source_candidate_id": candidate["source_candidate_id"],
            "f_number": applied, "requested_f_number": candidate["requested_f_number"],
            "exposure_ms": candidate["exposure_ms"], "blur_length_px": candidate["motion"]["blur_length_px"],
            "velocity_px_s": candidate["motion"]["velocity_px_s"],
            "aperture_condition": condition or {"mode": "source_stop", "effective_f_number": applied,
                                                 "reference_wavelength_nm": 550},
            "fields": fields,
        })
    if len(rows) != len(f_numbers) * len({r["exposure_ms"] for r in rows}):
        raise ValueError("Every aperture/exposure candidate must complete before selection")
    references = {
        (row["exposure_ms"], f["field_id"]): f["mean_signal_e"]
        for row in rows if row["requested_f_number"] == f_numbers[0] for f in row["fields"]
    }
    for row in rows:
        for field in row["fields"]:
            signal = field.get("mean_signal_e")
            reference = references.get((row["exposure_ms"], field["field_id"]))
            field["relative_signal"] = signal / reference if signal is not None and reference and reference > 0 else None
        for name, key, aggregate in (
            ("worst_field_snr_db", "snr_db", min), ("worst_field_sfr50_cpp", "processed_sfr50_cpp", min),
            ("min_relative_signal", "relative_signal", min), ("max_saturation_fraction", "saturation_fraction", max),
        ):
            values = [f.get(key) for f in row["fields"]]
            row[name] = aggregate(values) if all(value is not None for value in values) else None
    return {
        "schema": "camera_db_aperture_sweep_result_v1", "joint_exposure_selection": True,
        "rows": rows, "baseline_candidate_id": baseline_id,
        "selection": metrics.assess(rows, policy), "scope": SCOPE,
        "signal_reference": "same-field uniform Green photoelectrons at lowest F-number with the SAME exposure",
        "sfr_scope": "Processed moving-chart SFR within the common supported crop; not lens-only MTF",
        "common_valid_rect_row_col": valid_rect, "physical_validation": "not_established",
    }


def _slim_source(value):
    """Keep identity and provenance; detailed geometry remains in source/result artifacts."""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    omitted = {"surfaces", "physical_model", "provenance", "wavefront_diagnostic", "diagnostics",
               "cross_section", "parameter_sources", "field_classification"}
    return {key: _slim_source(item) if isinstance(item, dict) else copy.deepcopy(item)
            for key, item in value.items() if key not in omitted}


def _compact_curve(curve):
    keys = ("cycles_per_native_pixel", "radial", "includes_receiver_pixel_aperture")
    return {key: curve[key] for key in keys if key in curve}


def compact_candidate(candidate, detail_sha256):
    data = candidate["result"]
    fields = []
    for field in data["fields"]:
        item = {key: copy.deepcopy(field[key]) for key in (
            "id", "label", "status", "geometry", "images", "metrics", "measurement_support", "motion",
            "motion_valid_mask_file", "calibration_id", "lsc_applied", "supported_motion_metrics") if key in field}
        site = copy.deepcopy(field.get("tests", {}).get("resolution", {}).get("sites", {}).get("center", {}))
        for result in (site.get("linear_system", {}), site.get("processed_system", {})):
            result.pop("esf", None)
            result.pop("lsf", None)
        item["tests"] = {"resolution": {"status": field.get("tests", {}).get("resolution", {}).get("status"),
                                         "sites": {"center": site}}}
        item["capture_files"] = {kind: {"raw": record["raw"][:1], "rate_sha256": record["rate_sha256"]}
                                 for kind, record in field["capture_files"].items()}
        optics = field["optics"]
        item["optics"] = {key: copy.deepcopy(optics[key]) for key in (
            "id", "label", "status", "geometry", "chief_ray_angle_deg", "relative_illumination",
            "irradiance_per_radiance_sr", "distortion_percent", "diffraction_included", "psf_kind",
            "mtf_reference_wavelength_nm", "images", "source_revision_id") if key in optics}
        item["optics"]["mtf"] = _compact_curve(optics.get("mtf", {}))
        item["optics"]["mtf_comparison"] = {key: _compact_curve(value) for key, value in optics.get("mtf_comparison", {}).items()}
        fields.append(item)
    compact = {key: copy.deepcopy(candidate[key]) for key in (
        "id", "status", "label", "lens_key", "sensor_key", "prefix", "source_candidate_id",
        "exposure_ms", "requested_f_number", "motion", "parameter_sources", "source_capture") if key in candidate}
    compact["source_identities"] = _slim_source(candidate["source_identities"])
    compact["result"] = {key: copy.deepcopy(data[key]) for key in (
        "schema", "configuration", "successful_fields", "failed_fields", "limitations", "optical_revision",
        "raw_preserved", "physical_validation") if key in data}
    compact["result"].update(fields=fields, reference=_slim_source(data.get("reference", {})),
                             pixel={"cell": _slim_source(data["pixel"]["cell"])})
    compact["detail_file"] = candidate["prefix"] + "result.json"
    compact["detail_sha256"] = detail_sha256
    compact["index_scope"] = "compact display index; complete RAW hashes/optical/ISP arrays in bound detail artifact"
    return compact


def run(workspace, directory, request, source_root, source_summary, progress, *, check=lambda: None, cancel_check=lambda: None):
    del workspace
    started = time.monotonic()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    reader = acquisition.VerifiedSource(source_root, source_summary.get("manifest_sha256"))
    if request["source_run_id"] != source_summary.get("id"):
        raise ValueError("Motion request does not identify the bound source")
    payloads = acquisition._validate_source(reader, source_summary)
    projection = source_projection(payloads)
    depth = _positive(request["object_distance_m"], "object axial depth")
    exposures = request["exposure_ms"]
    f_numbers = source_summary["configuration"]["f_numbers"]
    base_exposure = 4 if 4 in exposures else exposures[0]
    baseline_id = f"{payloads[0]['candidate']['id']}-e{exposures.index(base_exposure):02d}"
    identity = {"run_id": request["source_run_id"], "manifest_sha256": reader.manifest_sha256}
    shared.write_json(root / "source-binding.json", {
        "schema": "camera_db_aperture_motion_source_v1", **identity,
        "verified_files": reader.used, "projection": projection,
        "reuse": "pre-noise full spectral-integrated R/Gr/Gb/B planes; every RAW is reacquired",
        "optics_recomputed": False, "source_raw_modified": False,
    })
    # All condition supports are checked before generating any new RAW.
    maximum_velocity = image_velocity_px_s(max(request["object_speed_m_s"]), depth,
        projection["effective_focal_length_mm"], projection["pixel_pitch_um"])
    support = common_valid_rect((256, 256), maximum_velocity * max(exposures) / 1000)
    messages = queue.Queue()
    check()
    cells = []
    for luminance in request["luminance_cd_m2"]:
        for speed in request["object_speed_m_s"]:
            cell = {"id": f"condition-{len(cells):03d}", "luminance_cd_m2": luminance,
                    "object_speed_m_s": speed, "object_distance_m": depth,
                    "common_valid_rect_row_col": support, "baseline_candidate_id": baseline_id}
            stopped = threading.Event()
            def worker_check():
                if stopped.is_set():
                    raise RuntimeError("Motion worker stopped after cancellation or sibling failure")
            tasks = [(payload, fi, exposure, ei) for fi, payload in enumerate(payloads)
                     for ei, exposure in enumerate(exposures)]
            ordered = {}
            with ThreadPoolExecutor(max_workers=min(acquisition.WORKERS, len(tasks))) as pool:
                futures = {pool.submit(_camera, root, payload, cell, projection, depth, exposure, ei,
                                       f_numbers[fi], identity, messages.put, worker_check): index
                           for index, (payload, fi, exposure, ei) in enumerate(tasks)}
                pending = set(futures)
                try:
                    while pending:
                        done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                        cancel_check()
                        while not messages.empty():
                            progress(messages.get_nowait())
                        for future in done:
                            ordered[futures[future]] = future.result()
                except BaseException:
                    stopped.set()
                    for future in pending:
                        future.cancel()
                    raise
            full_candidates = [ordered[index] for index in range(len(tasks))]
            sweep = comparison(root, full_candidates, f_numbers, request["selection_policy"], support, baseline_id)
            compact = []
            for candidate, row in zip(full_candidates, sweep["rows"], strict=True):
                candidate["supported_measurements"] = row
                for field, measured in zip(candidate["result"]["fields"], row["fields"], strict=True):
                    field["supported_motion_metrics"] = measured
                    field["metrics"].update(
                        supported_green_photoelectrons=measured.get("mean_signal_e"),
                        supported_green_temporal_noise_e=measured.get("temporal_noise_e"),
                        supported_snr_db=measured.get("snr_db"),
                        supported_saturation_fraction=measured.get("saturation_fraction"),
                    )
                path = root / candidate["prefix"] / "result.json"
                shared.write_json(path, candidate)
                compact.append(compact_candidate(candidate, sha256(path)))
            cell.update(candidates=compact, aperture_sweep=sweep,
                        capture_count=len(tasks) * len(acquisition.FIELDS) * len(acquisition.CHARTS) * 8)
            shared.write_json(root / "conditions" / cell["id"] / "index.json", cell)
            cells.append(cell)
            progress(f"{cell['id']} completed · {luminance:g} cd/m² / {speed:g} m/s · joint aperture + exposure")
            check()
    default_cell = next((c for c in cells if c["luminance_cd_m2"] == 1000 and c["object_speed_m_s"] == 1), cells[0])
    condition_result = {
        "schema": SCHEMA, "comparison_mode": "aperture_motion", "conditions": cells,
        "default_condition_id": default_cell["id"], "luminance_cd_m2": request["luminance_cd_m2"],
        "object_speed_m_s": request["object_speed_m_s"], "object_distance_m": depth,
        "exposure_ms": exposures, "f_numbers": f_numbers, "projection": projection,
        "common_valid_rect_row_col": support,
        "common_support_scope": "Same rectangle for the entire study at maximum speed and exposure, including stationary cells",
        "source_run_id": request["source_run_id"], "source_manifest_sha256": reader.manifest_sha256,
        "motion_blur_included": True, "auto_brightness": False, "calibration_reused": True,
        "optical_response_recomputed": False, "scope": SCOPE,
        "selection_scope": "joint aperture and exposure within each light/speed map cell",
        "seed_policy": "common paired seed stream; eight independent reset captures per chart",
        "baseline": {"source_candidate_id": payloads[0]["candidate"]["id"], "exposure_ms": base_exposure,
                     "candidate_id": baseline_id},
        "detail_policy": "compact index only; complete candidates are separately manifest-bound result.json files",
    }
    shared.write_json(root / "aperture-motion.json", condition_result)
    return {
        "schema": "camera_db_design_study_result_v1", "configuration": request,
        "comparison_mode": "aperture_motion", "candidates": default_cell["candidates"],
        "baseline_candidate_id": baseline_id, "aperture_sweep": default_cell["aperture_sweep"],
        "aperture_conditions": condition_result, "capture_count": sum(c["capture_count"] for c in cells),
        "raw_shape_row_col": [256, 256], "raw_preserved": True, "physical_validation": "not_established",
        "product_ranking_allowed": False, "comparison_scope": SCOPE,
        "calibration": payloads[0]["calibration"], "performance": {
            "elapsed_seconds": time.monotonic() - started, "candidate_workers": acquisition.WORKERS,
            "optics_recomputed": False, "compact_candidate_index": True,
        },
    }

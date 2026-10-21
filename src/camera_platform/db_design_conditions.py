"""Recapture a frozen diffraction aperture sweep under declared light/exposure.

Only the pre-noise photoelectron rate is scaled with luminance. Every exposure
passes through the stored electrical model again, retaining independent reset,
shot/dark/read noise, clipping and ADC. The source CCM and display stay fixed.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import queue
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path, PurePosixPath

import numpy as np

from . import db_design_engine as base
from . import design_workshop_engine as shared
from . import reference_workshop_engine as reference
from .electrical import default_electrical_model
from .virtual_lab_job import sha256

FIELDS = base.FIELDS
CHARTS = base.CHARTS
WORKERS = 4
SCOPE = (
    "Static D65 charts; luminance is scene cd/m2, not illuminance lux. "
    "Frozen diffraction optics, pixel structure, gain1, common CCM and display; "
    "independent exposures with fresh shot/dark/read noise and clipping. "
    "No motion blur, AE, per-frame brightness fitting or measured product optimum."
)


class VerifiedSource:
    """Read the exact bytes checked against the pinned source manifest."""

    def __init__(self, root, expected_manifest_sha256=None):
        self.root = Path(root).resolve()
        data = (self.root / "manifest.json").read_bytes()
        self.manifest_sha256 = hashlib.sha256(data).hexdigest()
        if expected_manifest_sha256 and self.manifest_sha256 != expected_manifest_sha256:
            raise ValueError("Aperture source manifest changed")
        manifest = json.loads(data)
        if manifest.get("schema") != "camera_reference_workshop_files_v1":
            raise ValueError("Aperture source requires a preserved Reference manifest")
        self.files = manifest["files"]
        self.used = {}

    def read(self, name):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("Source artifact must have a normalized relative path")
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Source artifact leaves its bound run")
        expected = self.files.get(name)
        if not isinstance(expected, str):
            raise ValueError("Source artifact is not present in its pinned manifest: " + name)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Source artifact hash changed: " + name)
        self.used[name] = expected
        return data

    def json(self, name):
        return json.loads(self.read(name))

    def array(self, name):
        array = np.load(io.BytesIO(self.read(name)), allow_pickle=False)
        if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
            raise ValueError("Preserved source array must be finite and numeric")
        array.setflags(write=False)
        return array


def _validate_source(reader, summary):
    actual = reader.json("result.json")
    if any(summary.get(key) != value for key, value in actual.items()):
        raise ValueError("Source result differs from its preserved manifest-bound summary")
    if (
        actual.get("schema") != "camera_db_design_study_result_v1"
        or actual.get("comparison_mode") != "aperture_sweep"
        or actual.get("status") != "succeeded"
    ):
        raise ValueError("Conditions require a completed diffraction aperture sweep")
    selected = [c for c in actual["candidates"] if c["lens_key"] != "reference"]
    if not 2 <= len(selected) <= 8 or len(selected) != len(actual["configuration"]["f_numbers"]):
        raise ValueError("Every source aperture must have a completed candidate")
    identities = [c["id"] for c in selected]
    if len(set(identities)) != len(identities):
        raise ValueError("Source aperture IDs must be unique")
    loaded, common = [], None
    for candidate in selected:
        prefix = candidate["prefix"]
        if prefix != f"candidates/{candidate['id']}/":
            raise ValueError("Unsupported source candidate artifact prefix")
        payload = {"candidate": copy.deepcopy(candidate), "fields": {}, "support": {}}
        for name in ("pixel-cell", "pixel-response", "calibration", "optical-response"):
            payload[name] = reader.json(prefix + name + ".json")
        cell, calibration = payload["pixel-cell"], payload["calibration"]
        spec = candidate["result"]["configuration"]
        if (
            candidate.get("status") != "succeeded"
            or spec.get("frames") != 8 or spec.get("gain") != 1
            or spec.get("luminance_cd_m2") != 1000 or spec.get("exposure_ms") != 4
            or spec.get("chart_suite") != "classic_v1"
            or spec.get("lens", {}).get("optical_profile") != "exit_pupil_diffraction"
            or spec.get("isp", {}).get("lsc_enabled") is not False
            or calibration.get("lsc") is not None
        ):
            raise ValueError("Source must be the frozen 1000cd/m2, 4ms, gain1 native diffraction bench")
        electrical = default_electrical_model({**cell["readout"], "gain": 1})
        if cell["electrical_model"] != electrical:
            raise ValueError("Source electrical contract is not the frozen linear FPN-off model")
        invariant = {"calibration": calibration, "isp": spec["isp"], "cell": cell}
        if common is not None and common != invariant:
            raise ValueError("All source apertures must share their sensor, ISP and calibration")
        common = invariant
        optics = payload["optical-response"]
        optical_fields = {f["id"]: f for f in optics["fields"] if f["id"] in FIELDS}
        if set(optical_fields) != set(FIELDS):
            raise ValueError("Both source fields are required")
        for field_id in FIELDS:
            field = optical_fields[field_id]
            if field.get("status") != "succeeded" or not field.get("diffraction_included"):
                raise ValueError("Source requires successful diffraction in each field")
            for image_path in field.get("images", {}).values():
                name = "optics/" + image_path
                payload["support"][name] = reader.read(prefix + name)
            field_prefix = prefix + f"fields/{field_id}/"
            capture = reader.json(field_prefix + "capture.json")
            if capture["configuration"] != spec or capture["calibration"] != calibration["color"]:
                raise ValueError("Preserved field capture and source configuration disagree")
            if capture["geometry"]["native_shape_row_col"] != [256, 256]:
                raise ValueError("Condition recapture requires the unchanged native 256x256 grid")
            records = capture["captures"]
            if set(records) != set(CHARTS):
                raise ValueError("Conditions require all four frozen native charts")
            arrays = {}
            for chart in CHARTS:
                chart_prefix = field_prefix + chart + "/"
                record = records[chart]
                values = {}
                for filename, key in (
                    ("mean-electron-rate.npy", "rate_sha256"),
                    ("planes-electron-rate.npy", "planes_sha256"),
                    ("source-reference-linear.npy", "source_reference_sha256"),
                ):
                    name = chart_prefix + filename
                    if reader.files.get(name) != record[key]:
                        raise ValueError("Source capture hash differs from source manifest")
                    values[filename] = reader.array(name)
                rate, planes = values["mean-electron-rate.npy"], values["planes-electron-rate.npy"]
                if rate.shape != (256, 256) or planes.shape != (256, 256, 4):
                    raise ValueError("Preserved mean/plane rates must keep native grid and four CFA sites")
                if (rate < 0).any() or (planes < 0).any():
                    raise ValueError("Pre-noise optical photoelectron rates cannot be negative")
                if values["source-reference-linear.npy"].shape != (256, 256, 3):
                    raise ValueError("Preserved chart reference must keep its RGB native grid")
                arrays[chart] = values
            payload["fields"][field_id] = {"capture": capture, "optics": field, "arrays": arrays}
        loaded.append(payload)
    return loaded


def _camera(root, payload, condition, source_identity, notify, check):
    started = time.monotonic()
    candidate = copy.deepcopy(payload["candidate"])
    folder = Path(root) / condition["id"] / "candidates" / candidate["id"]
    folder.mkdir(parents=True, exist_ok=False)
    cell, response = payload["pixel-cell"], payload["pixel-response"]
    calibration = payload["calibration"]
    spec = copy.deepcopy(candidate["result"]["configuration"])
    spec.update(luminance_cd_m2=condition["luminance_cd_m2"], exposure_ms=condition["exposure_ms"])
    spec["name"] = f"{candidate['label']} · {condition['luminance_cd_m2']:g} cd/m² · {condition['exposure_ms']:g} ms"
    source_spec = candidate["result"]["configuration"]
    light_scale = spec["luminance_cd_m2"] / source_spec["luminance_cd_m2"]
    reference_scale = light_scale * spec["exposure_ms"] / source_spec["exposure_ms"]
    for name in ("pixel-cell", "pixel-response", "calibration", "optical-response"):
        shared.write_json(folder / (name + ".json"), payload[name])
    for name, data in payload["support"].items():
        destination = folder / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    fields, captures = [], {}
    for fi, field_id in enumerate(FIELDS):
        check()
        source = payload["fields"][field_id]
        field_root = folder / "fields" / field_id
        field_root.mkdir(parents=True)
        capture = copy.deepcopy(source["capture"])
        capture["configuration"] = spec
        supported_roi = condition.get("common_valid_rect_row_col")
        if supported_roi is not None:
            field_motion = condition.get("_field_motion", {}).get(field_id, condition["motion"])
            from .db_design_motion_metrics import edge_support_roi
            rendered_source_field = next(f for f in candidate["result"]["fields"] if f["id"] == field_id)
            sfr_support_roi = edge_support_roi(
                rendered_source_field["tests"]["resolution"]["sites"]["center"]["roi"],
                capture["geometry"]["native_shape_row_col"],
            )
            capture["measurement_support"] = {
                "common_valid_rect_row_col": supported_roi,
                "motion": copy.deepcopy(field_motion),
                "scope": "motion input support and ISP halo excluded; preview padding is invalid",
            }
            mask = np.zeros(tuple(capture["geometry"]["native_shape_row_col"]), dtype=bool)
            r0, r1, c0, c1 = supported_roi
            mask[r0:r1, c0:c1] = True
            np.save(field_root / "motion-valid-mask.npy", mask)
            from .virtual_lab_engine import _png
            _png(field_root / "motion-valid-mask.png", mask.astype(float))
        # Reference targets follow the physical light/exposure dose. This is not
        # image normalization: the CCM, white level and raw/display units do not change.
        for name in ("reference_xyz", "white_xyz"):
            capture["scene_reference"][name] = (
                np.asarray(capture["scene_reference"][name]) * reference_scale
            ).tolist()
        capture["scene_reference"]["condition_reference_scale"] = reference_scale
        capture["scene_reference"]["condition_reference_policy"] = (
            "source target scaled by luminance ratio and exposure ratio; no display rescaling"
        )
        for chart in CHARTS:
            check()
            notify(f"{condition['id']} · {candidate['id']} · {field_id}/{chart} · fresh RAW")
            chart_root = field_root / chart
            chart_root.mkdir()
            values = source["arrays"][chart]
            rate = values["mean-electron-rate.npy"] * light_scale
            np.save(chart_root / "mean-electron-rate.npy", rate)
            np.save(chart_root / "planes-electron-rate.npy", values["planes-electron-rate.npy"] * light_scale)
            np.save(chart_root / "source-reference-linear.npy", (
                values["source-reference-linear.npy"] * reference_scale
            ).astype(np.float32))
            frames = []
            for repeat in range(8):
                check()
                # Common seed stream across apertures/conditions is an intentional
                # paired comparison; each repeated exposure has a distinct draw.
                seed = spec["seed"] + fi * 32 + repeat
                acquired = shared._capture(rate, cell["electrical_model"], spec, seed)
                path = chart_root / f"raw-{repeat:02d}.npy"
                np.save(path, acquired["arrays"]["raw_dn"])
                record = {"file": path.name, "sha256": sha256(path), "seed": seed,
                          "saturation_fractions": acquired["summary"]["saturation_fractions"]}
                if supported_roi is not None:
                    r0, r1, c0, c1 = supported_roi
                    record["supported_roi_row_col"] = list(supported_roi)
                    record["supported_saturation_fractions"] = {
                        name: float(np.mean(acquired["arrays"][name][r0:r1, c0:c1]))
                        for name in ("well_saturated_mask", "adc_high_mask", "digital_high_mask")
                    }
                    acquired["summary"]["supported_roi_row_col"] = list(supported_roi)
                    acquired["summary"]["supported_saturation_fractions"] = record["supported_saturation_fractions"]
                    if chart == "resolution":
                        er0, er1, ec0, ec1 = sfr_support_roi
                        record["sfr_support_roi_row_col"] = list(sfr_support_roi)
                        record["sfr_support_saturation_fractions"] = {
                            name: float(np.mean(acquired["arrays"][name][er0:er1, ec0:ec1]))
                            for name in ("well_saturated_mask", "adc_high_mask", "digital_high_mask")
                        }
                        acquired["summary"]["sfr_support_roi_row_col"] = list(sfr_support_roi)
                        acquired["summary"]["sfr_support_saturation_fractions"] = record["sfr_support_saturation_fractions"]
                frames.append(record)
                if repeat == 0:
                    shared.write_json(chart_root / "capture.json", acquired["summary"])
            record = capture["captures"][chart]
            record.update(
                raw=frames, rate_sha256=sha256(chart_root / "mean-electron-rate.npy"),
                planes_sha256=sha256(chart_root / "planes-electron-rate.npy"),
                source_reference_sha256=sha256(chart_root / "source-reference-linear.npy"),
                response_cache_reused=True,
                condition_rate_source={**source_identity, "candidate_id": payload.get("source_candidate_id", candidate["id"]),
                    "field_id": field_id, "chart": chart, "luminance_scale": light_scale,
                    "source_rate_sha256": source["capture"]["captures"][chart]["rate_sha256"],
                    "source_planes_sha256": source["capture"]["captures"][chart]["planes_sha256"],
                    "unit": "photoelectrons_per_second_before_dark_noise_gain_clipping"},
            )
        shared.write_json(field_root / "capture.json", capture)
        captures[field_id] = capture
        notify(f"{condition['id']} · {candidate['id']} · {field_id} · fixed ISP and metrics")
        rendered = reference._render_field(
            field_root, capture, spec, source["optics"], response, cell, calibration
        )
        rendered["comparison_scope"] = "same_native_sensor_plane_chart_not_matched_camera_FOV"
        if supported_roi is not None:
            rendered["measurement_support"] = capture["measurement_support"]
            rendered["motion"] = copy.deepcopy(field_motion)
            for image_set in rendered["images"].values():
                image_set["motion_valid"] = f"fields/{field_id}/motion-valid-mask.png"
            rendered["motion_valid_mask_file"] = f"fields/{field_id}/motion-valid-mask.npy"
        display_white = spec["isp"]["display_white_level"]
        display_clipping = {}
        for chart in CHARTS:
            linear = np.load(field_root / chart / "processed.npy", allow_pickle=False)
            display_clipping[chart] = {
                "first_frame_pixels_with_any_channel_at_or_above_white_fraction": float(
                    np.mean(np.any(linear >= display_white, axis=-1))
                ),
                "first_frame_channels_at_or_above_white_fraction": float(np.mean(linear >= display_white)),
            }
        rendered["tests"]["display_clipping"] = {
            "display_white_level": display_white, "charts": display_clipping,
            "scope": "first processed linear frame before display clipping; not sensor saturation",
        }
        rendered["metrics"]["display_high_clipped_pixel_fraction"] = max(
            row["first_frame_pixels_with_any_channel_at_or_above_white_fraction"]
            for row in display_clipping.values()
        )
        dn_per_e = (2 ** cell["readout"]["bit_depth"] - 1 - cell["readout"]["black_level_dn"]) / cell["readout"]["full_well_e"]
        rendered["metrics"]["temporal_noise_e"] = rendered["metrics"]["temporal_noise_dn"] / dn_per_e
        rendered["tests"]["noise"]["electron_conversion"] = {
            "dn_per_e": dn_per_e,
            "definition": "input-referred Green temporal RAW noise including quantization",
        }
        fields.append(rendered)
    shared.write_json(folder / "capture.json", {
        "configuration": spec, "fields": captures,
        "calibration_sha256": sha256(folder / "calibration.json"),
        "source": source_identity,
    })
    result = copy.deepcopy(candidate["result"])
    result.update(configuration=spec, fields=fields, successful_fields=len(fields), failed_fields=0)
    result["condition_source"] = source_identity
    result["limitations"] = list(dict.fromkeys([*result["limitations"], condition.get("_scope", SCOPE)]))
    shared.write_json(folder / "render.json", result)
    candidate.update(prefix=f"conditions/{condition['id']}/candidates/{candidate['id']}/", result=result)
    candidate["source_capture"] = source_identity
    candidate["performance"] = {"elapsed_seconds": time.monotonic() - started}
    shared.write_json(folder / "sources.json", candidate["source_identities"])
    shared.write_json(folder / "result.json", candidate)
    return candidate


def run(workspace, directory, request, source_root, source_summary, progress, *, check=lambda: None, cancel_check=lambda: None):
    """Recapture a bounded Cartesian condition grid; never mutate source RAW."""
    del workspace  # Every optical, pixel and calibration input is source-bound.
    started = time.monotonic()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    reader = VerifiedSource(source_root, source_summary.get("manifest_sha256", request.get("source_manifest_sha256")))
    if request["source_run_id"] != source_summary.get("id"):
        raise ValueError("Condition request does not identify its bound source run")
    payloads = _validate_source(reader, source_summary)
    source_identity = {"run_id": request["source_run_id"], "manifest_sha256": reader.manifest_sha256}
    shared.write_json(root / "source-binding.json", {
        "schema": "camera_db_aperture_conditions_source_v1", **source_identity,
        "verified_files": dict(reader.used),
        "reuse": "immutable pre-noise optical/pixel photoelectron rates; raw acquisition rerun",
        "optics_recomputed": False, "source_raw_modified": False,
    })
    check()
    messages = queue.Queue()
    conditions = []
    f_numbers = source_summary["configuration"]["f_numbers"]
    for luminance in request["luminance_cd_m2"]:
        for exposure in request["exposure_ms"]:
            condition = {"id": f"condition-{len(conditions):03d}", "luminance_cd_m2": luminance,
                         "exposure_ms": exposure}
            ordered = {}
            stopped = threading.Event()

            def worker_check():
                if stopped.is_set():
                    raise RuntimeError("Condition worker stopped after cancellation or sibling failure")

            with ThreadPoolExecutor(max_workers=min(WORKERS, len(payloads))) as pool:
                futures = {pool.submit(_camera, root / "conditions", payload, condition,
                                      source_identity, messages.put, worker_check): index
                           for index, payload in enumerate(payloads)}
                pending = set(futures)
                try:
                    while pending:
                        done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
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
            candidates = [ordered[index] for index in range(len(payloads))]
            comparison = base.aperture_comparison(root, candidates, {
                "f_numbers": f_numbers, "selection_policy": request["selection_policy"],
            })
            comparison["scope"] = (
                f"D65 {luminance:g} cd/m2; {exposure:g} ms; gain1; native charts and fixed ISP. " + SCOPE
            )
            condition.update(candidates=candidates, aperture_sweep=comparison,
                             capture_count=len(candidates) * len(FIELDS) * len(CHARTS) * 8)
            shared.write_json(root / "conditions" / condition["id"] / "result.json", condition)
            conditions.append(condition)
            progress(f"{condition['id']} completed · {luminance:g} cd/m² / {exposure:g} ms")
            check()
    if not conditions:
        raise ValueError("At least one light/exposure condition is required")
    default_condition = next((c for c in conditions if c["luminance_cd_m2"] == 1000 and c["exposure_ms"] == 4), conditions[0])
    axes = {"luminance_cd_m2": request["luminance_cd_m2"], "exposure_ms": request["exposure_ms"]}
    condition_result = {
        "schema": "camera_db_aperture_conditions_result_v1", **axes, "conditions": conditions,
        "source_run_id": request["source_run_id"], "source_manifest_sha256": reader.manifest_sha256,
        "f_numbers": f_numbers, "scope": SCOPE, "default_condition_id": default_condition["id"],
        "seed_policy": "common paired seed stream; eight independent reset captures per chart",
        "motion_blur_included": False, "auto_brightness": False,
        "calibration_reused": True, "optical_response_recomputed": False,
        "selection_scope": "each luminance/exposure cell independently; exposure is not optimized",
    }
    shared.write_json(root / "aperture-conditions.json", condition_result)
    return {
        "schema": "camera_db_design_study_result_v1", "configuration": request,
        "comparison_mode": "aperture_conditions", "candidates": default_condition["candidates"],
        "baseline_candidate_id": default_condition["aperture_sweep"]["baseline_candidate_id"],
        "capture_count": sum(c["capture_count"] for c in conditions),
        "raw_shape_row_col": [256, 256], "raw_preserved": True,
        "physical_validation": "not_established", "product_ranking_allowed": False,
        "comparison_scope": "same_native_sensor_plane_charts_common_reference_CCM",
        "calibration": payloads[0]["calibration"], "aperture_conditions": condition_result,
        "aperture_sweep": default_condition["aperture_sweep"],
        "performance": {"elapsed_seconds": time.monotonic() - started,
                        "candidate_workers": min(WORKERS, len(payloads)), "optics_recomputed": False},
    }

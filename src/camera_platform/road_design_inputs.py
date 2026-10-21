"""Freeze road inputs, source-coordinate masks and DB revisions before capture."""

from __future__ import annotations

import base64
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation, binary_erosion, uniform_filter

from .road_rgb_source import (
    file_sha256,
    json_default,
    json_sha256,
    pixels_to_tangent,
    tangent_to_pixels,
)

SCHEMA = "camera_road_design_study_v1"
TITLE = "도로 사진 기반 분광 근사 장면에서의 상대 설계평가"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=json_default)
        + "\n"
    )
    temporary.replace(path)


def decode_mask(mask):
    """Official nuImages base64-encoded COCO instance RLE."""
    try:
        from pycocotools.mask import decode
    except ImportError as exc:
        raise RuntimeError(
            "Install pyisetcam[road-study] to decode official instance masks."
        ) from exc
    return decode({"size": mask["size"], "counts": base64.b64decode(mask["counts"])}).astype(bool)


def _angular_roi(box, camera):
    x0, y0, x1, y1 = box
    u = np.concatenate(
        (np.linspace(x0, x1, 20), np.linspace(x0, x1, 20), np.full(20, x0), np.full(20, x1))
    )
    v = np.concatenate(
        (np.full(20, y0), np.full(20, y1), np.linspace(y0, y1, 20), np.linspace(y0, y1, 20))
    )
    x, y = pixels_to_tangent(u, v, camera["camera_intrinsic"], camera["camera_distortion"])
    return [float(x.min()), float(y.min()), float(x.max()), float(y.max())]


def _common_masks(roi, camera, masks):
    x0, y0, x1, y1 = roi["tangent_bounds_xyxy"]
    # Never pretend a small original patch contains 128 independent samples.
    box = roi["source_bbox_xyxy"]
    cols = min(128, int(box[2] - box[0]))
    rows = min(128, int(box[3] - box[1]))
    y, x = np.meshgrid(np.linspace(y0, y1, rows), np.linspace(x0, x1, cols), indexing="ij")
    u, v = tangent_to_pixels(x, y, camera["camera_intrinsic"], camera["camera_distortion"])
    ui, vi = np.rint(u).astype(int), np.rint(v).astype(int)
    inside = (ui >= 0) & (vi >= 0) & (ui < masks[0].shape[1]) & (vi < masks[0].shape[0])
    ui, vi = np.clip(ui, 0, masks[0].shape[1] - 1), np.clip(vi, 0, masks[0].shape[0] - 1)
    common = [array[vi, ui] & inside for array in masks]
    return (rows, cols), common


def freeze_nuimages_inputs(dataset_root: Path, output: Path, *, count=12):
    """Deterministic input-only eligibility; one image per log, no camera scores."""
    table_root = dataset_root / "v1.0-mini"
    names = (
        "sample",
        "sample_data",
        "calibrated_sensor",
        "object_ann",
        "category",
        "log",
        "sensor",
    )
    tables = {name: json.loads((table_root / (name + ".json")).read_text()) for name in names}
    indices = {
        name: {row["token"]: row for row in tables[name]} for name in names if name != "object_ann"
    }
    annotations = defaultdict(list)
    for row in tables["object_ann"]:
        annotations[row["sample_data_token"]].append(row)
    selected, excluded, used_logs = [], [], set()
    for sample in sorted(tables["sample"], key=lambda row: row["token"]):
        if sample["log_token"] in used_logs:
            continue
        data = indices["sample_data"][sample["key_camera_token"]]
        camera = indices["calibrated_sensor"][data["calibrated_sensor_token"]]
        if len(camera["camera_distortion"]) != 5:
            excluded.append(
                {
                    "sample": sample["token"],
                    "reason": "fisheye/extended distortion not supported by Brown5 provider",
                }
            )
            continue
        image_path = (dataset_root / data["filename"]).resolve()
        with Image.open(image_path) as image:
            rgb = np.asarray(image.convert("RGB"), float) / 255.0
        luminance = rgb @ [0.2126, 0.7152, 0.0722]
        invalid = rgb.max(axis=-1) >= 0.98
        anns = sorted(annotations[data["token"]], key=lambda row: row["token"])
        decoded = {
            row["token"]: decode_mask(row["mask"]) for row in anns if row["mask"] is not None
        }
        occupied = np.zeros(rgb.shape[:2], bool)
        for mask in decoded.values():
            occupied |= mask
        for row in anns:
            if row["mask"] is None:
                a, b, c, d = row["bbox"]
                occupied[b:d, a:c] = True
        feasible = []
        for row in anns:
            if row["mask"] is None:
                continue
            x0, y0, x1, y1 = row["bbox"]
            if not (8 <= x1 - x0 <= 70 and 8 <= y1 - y0 <= 70):
                continue
            box = [
                max(0, x0 - 12),
                max(0, y0 - 12),
                min(rgb.shape[1] - 1, x1 + 12),
                min(rgb.shape[0] - 1, y1 + 12),
            ]
            angular = _angular_roi(box, camera)
            if max(abs(angular[0]), abs(angular[2])) >= math.tan(math.radians(14)) or max(
                abs(angular[1]), abs(angular[3])
            ) >= math.tan(math.radians(7.5)):
                continue
            original = decoded[row["token"]]
            obj = binary_erosion(original, iterations=1) & ~invalid
            bg = binary_dilation(original, iterations=8) & ~occupied & ~invalid
            scope = np.zeros_like(bg)
            scope[box[1] : box[3], box[0] : box[2]] = True
            bg &= scope
            if (
                obj.sum() < 30
                or bg.sum() < 50
                or (invalid & original).sum() / max(1, original.sum()) > 0.02
            ):
                continue
            contrast = abs(float(luminance[obj].mean() - luminance[bg].mean()))
            if contrast < 0.005:
                continue
            roi = {
                "id": "object-" + row["token"],
                "kind": "object",
                "annotation_token": row["token"],
                "category": indices["category"][row["category_token"]]["name"],
                "source_bbox_xyxy": box,
                "object_bbox_xyxy": row["bbox"],
                "tangent_bounds_xyxy": angular,
                "input_only_contrast": contrast,
                "source_mask_definition": (
                    "official instance eroded 1px; background dilation8px excluding all "
                    "instances and original clipped pixels"
                ),
            }
            shape, common = _common_masks(roi, camera, (obj, bg))
            if min(array.sum() for array in common) < 25:
                continue
            roi["common_shape_row_col"] = list(shape)
            feasible.append((row, roi, common))
        if len(feasible) < 2:
            excluded.append(
                {
                    "sample": sample["token"],
                    "reason": (
                        "fewer than two eligible small-object/background masks in common field"
                    ),
                }
            )
            continue
        # Smallest object plus lowest-contrast other object, before simulation.
        first = min(
            feasible,
            key=lambda item: (
                np.prod(np.diff(np.asarray(item[0]["bbox"]).reshape(2, 2), axis=0)),
                item[0]["token"],
            ),
        )
        other = min(
            (item for item in feasible if item is not first),
            key=lambda item: (item[1]["input_only_contrast"], item[0]["token"]),
        )
        chosen = [first, other]
        # Designated highlight is the brightest eligible 32px source patch.
        yy, xx = np.indices(rgb.shape[:2])
        tanx, tany = pixels_to_tangent(
            xx[::8, ::8], yy[::8, ::8], camera["camera_intrinsic"], camera["camera_distortion"]
        )
        score = uniform_filter(np.where(invalid, 0.0, luminance), size=32)[::8, ::8]
        eligible = (abs(tanx) < math.tan(math.radians(13.5))) & (
            abs(tany) < math.tan(math.radians(7))
        )
        score = np.where(eligible, score, -1.0)
        ir, ic = np.unravel_index(np.argmax(score), score.shape)
        cx, cy = ic * 8, ir * 8
        box = [cx - 16, cy - 16, cx + 16, cy + 16]
        highlight = np.zeros(rgb.shape[:2], bool)
        patch = luminance[cy - 16 : cy + 16, cx - 16 : cx + 16]
        threshold = float(np.quantile(patch, 0.7))
        highlight[cy - 16 : cy + 16, cx - 16 : cx + 16] = (patch >= threshold) & ~invalid[
            cy - 16 : cy + 16, cx - 16 : cx + 16
        ]
        roi = {
            "id": "highlight",
            "kind": "highlight",
            "source_bbox_xyxy": box,
            "tangent_bounds_xyxy": _angular_roi(box, camera),
            "source_mask_definition": (
                "brightest supported 32px patch; top30pct source luminance excluding "
                "original clipped channels"
            ),
        }
        shape, common = _common_masks(roi, camera, (highlight,))
        if common[0].sum() < 25:
            excluded.append(
                {"sample": sample["token"], "reason": "insufficient unsaturated highlight support"}
            )
            continue
        roi["common_shape_row_col"] = list(shape)
        chosen.append((None, roi, common))
        image_dir = output / "inputs" / sample["token"]
        image_dir.mkdir(parents=True, exist_ok=True)
        rois = []
        with Image.open(image_path) as source:
            preview = source.convert("RGB")
        draw = ImageDraw.Draw(preview)
        for number, (_, roi, masks) in enumerate(chosen):
            mask_path = image_dir / (roi["id"] + ".npz")
            np.savez_compressed(
                mask_path,
                object=masks[0],
                background=masks[1] if len(masks) > 1 else np.zeros_like(masks[0]),
            )
            roi["mask_path"] = str(mask_path.relative_to(output))
            roi["mask_sha256"] = file_sha256(mask_path)
            rois.append(roi)
            draw.rectangle(
                roi["source_bbox_xyxy"], outline=("#ff765e", "#6df0a8", "#f7da69")[number], width=3
            )
            draw.text(tuple(roi["source_bbox_xyxy"][:2]), str(number + 1), fill="white")
        preview.save(image_dir / "context.jpg", quality=90)
        selected.append(
            {
                "id": sample["token"],
                "log_token": sample["log_token"],
                "split": "selection" if len(selected) < count // 2 else "evaluation",
                "source_path": str(image_path),
                "source_sha256": file_sha256(image_path),
                "source_filename": data["filename"],
                "source_sample_data_token": data["token"],
                "camera": camera,
                "channel": indices["sensor"][camera["sensor_token"]]["channel"],
                "log": indices["log"][sample["log_token"]],
                "timestamp": data["timestamp"],
                "original_shape_row_col": list(rgb.shape[:2]),
                "rois": rois,
                "context_path": str((image_dir / "context.jpg").relative_to(output)),
            }
        )
        used_logs.add(sample["log_token"])
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"Only {len(selected)} qualified disjoint-log images; require {count}.")
    return selected, {
        "dataset": "nuImages v1.0-mini",
        "dataset_root": str(dataset_root.resolve()),
        "source_url": "https://www.nuscenes.org/data/nuimages-v1.0-mini.tgz",
        "license_scope": (
            "noncommercial internal research only; commercial permission not established"
        ),
        "license_url": "https://www.nuscenes.org/nuimages",
        "schema_url": "https://github.com/nutonomy/nuscenes-devkit/blob/master/docs/schema_nuimages.md",
        "attribution": "nuImages / Motional (formerly nuTonomy)",
        "table_sha256": {name: file_sha256(table_root / (name + ".json")) for name in names},
        "input_selection": (
            "sample token order; distinct logs; smallest and lowest input-contrast "
            "eligible object; no simulated scores"
        ),
        "excluded": excluded,
        "commercial_export_allowed": False,
    }


def freeze_db_candidates(workspace: Path):
    from .lens_catalog_bridge import geometric_lens_preview, lens_revision_catalog
    from .sensor_db_bridge import inspect_sensor_revision, sensor_revision_catalog

    root = workspace / "camerae2e_db"
    ready = [
        item
        for item in lens_revision_catalog(catalog_root=root)["revisions"]
        if item["preview_status"] == "ready"
    ]
    lenses = [geometric_lens_preview(item["revision_id"], catalog_root=root) for item in ready[:2]]
    sensors = []
    for row in sensor_revision_catalog(root=root):
        if row["status"] != "simulated" or row["model"] != "db_estimate_v1":
            continue
        model = inspect_sensor_revision(row["report_code"], root=root, run_id=row["run_id"])
        response = model.get("native_response", {})
        if (
            model["executable"]
            and response.get("filter_names") == ["red", "green", "blue"]
            and response["native_cfa"]["pattern"] == [[1, 2], [2, 3]]
        ):
            sensors.append(model)
    sensors.sort(
        key=lambda item: (
            -item["native_response"]["pixel_pitch_um"],
            item["revision"]["report_code"],
        )
    )
    sensors = sensors[:2]
    if len(lenses) != 2 or len(sensors) != 2:
        raise ValueError(
            "Two executable geometric Lens revisions and two saved RGGB sensor "
            "revisions are required."
        )
    candidates = []
    for li, lens in enumerate(lenses):
        for si, sensor in enumerate(sensors):
            candidates.append(
                {
                    "id": f"L{li + 1}-S{si + 1}",
                    "lens": lens,
                    "sensor": sensor,
                    "baseline": li == 0 and si == 0,
                }
            )
    return candidates


def freeze_study(workspace: Path, dataset_root: Path, output: Path):
    """Persist complete predeclared specification once, before any camera output."""
    path = output / "specification.json"
    if path.exists():
        raise ValueError(
            "Specification already exists; use replay/resume, never refreeze after results."
        )
    output.mkdir(parents=True, exist_ok=True)
    candidates = freeze_db_candidates(workspace)
    images, dataset = freeze_nuimages_inputs(dataset_root, output)
    spec = {
        "schema": SCHEMA,
        "title": TITLE,
        "dataset": dataset,
        "images": images,
        "candidates": candidates,
        "baseline_id": candidates[0]["id"],
        "capture": {
            "exposure_s": 0.004,
            "gain": 1.0,
            "brightness_multipliers": [0.25, 1.0, 4.0],
            "initial_repeats": 32,
            "expanded_repeats": 128,
            "seed": 20261003,
            "fixed_pattern_seed": 0,
        },
        "spectral_proxy": {
            "wavelength_nm": [450.0, 550.0, 650.0],
            "display": "LCD-Apple",
            "linear_grey_luminance_cd_m2": 100.0,
            "linear_grey_rgb": [0.18] * 3,
        },
        "geometry": {
            "horizontal_fov_deg": 30.0,
            "aspect_row_col": 9 / 16,
            "object_distance_m": "infinity",
            "sensor_extent": (
                "virtual common field with saved native pixel pitch; not product "
                "active-array/FOV qualification"
            ),
            "intrinsic_source": (
                "official per-image Brown5 calibration; unsupported extended/fisheye "
                "inputs excluded"
            ),
        },
        "sampling": {
            "sensor_pixel_oversampling": 2,
            "refinement_multiplier": 2,
            "mean_relative_tolerance": 0.01,
            "contrast_relative_tolerance": 0.01,
            "representative_image_id": images[0]["id"],
            "representative_roi_id": images[0]["rois"][0]["id"],
            "psf_sample_angles_deg": list(range(0, 361, 30)),
            "target": (
                "common angular ROI band limited by original RGB; not native sensor "
                "Nyquist qualification"
            ),
        },
        "selection_rule": {
            "objective": "maximize worst lowlight object/background CNR on selection images",
            "contrast_retention_min": 0.95,
            "highlight_well_fraction_max": 0.01,
            "highlight_adc_fraction_max": 0.01,
            "uncertain_tie": "prefer baseline; do not select unresolved differences",
        },
        "acceptance": {
            "lowlight_median_cnr_gain_min": 0.10,
            "individual_cnr_ratio_min": 0.95,
            "contrast_ratio_min": 0.95,
            "highlight_well_fraction_max": 0.01,
            "highlight_adc_fraction_max": 0.01,
        },
        "statistics": {
            "cnr": (
                "abs(object_mean-background_mean)/sqrt(mean temporal pixel variance "
                "object + mean temporal pixel variance background)"
            ),
            "bootstrap_replicates": 500,
            "confidence": 0.95,
            "expand_on_interval_crossing": True,
        },
        "resources": {
            "estimated_optical_working_bytes": 1024**3,
            "source_tile_side": 512,
            "maximum_native_roi_cells": 1_048_576,
            "maximum_package_bytes": 100 * 1024**3,
            "min_free_disk_bytes": 5 * 1024**3,
            "policy_is_hardware_limit": False,
        },
        "isp": {
            "profile": (
                "fixed bilinear, fixed grey-normalized sensor chromatic matrix, fixed sRGB transfer"
            ),
            "cnr_array": "pre-tone signed linear ISP luminance",
            "frame_max_normalization": False,
            "auto_exposure": False,
            "auto_wb": False,
        },
        "created_before_camera_execution": True,
    }
    from .road_design_compute import required_native_roi_cells

    needed = required_native_roi_cells(workspace, spec)
    spec["resources"]["maximum_native_roi_cells"] = max(1_048_576, needed)
    spec["resources"]["roi_size_basis"] = (
        "geometry-only maximum over all frozen input/candidate angular ROI bounds and ISP halos"
    )
    write_json(path, spec)
    write_json(
        output / "specification-binding.json",
        {"sha256": file_sha256(path), "semantic_sha256": json_sha256(spec)},
    )
    return spec

"""Deterministic RAW acquisition fixtures for the complete calibration workflow.

These are synthetic, same-model experiments, not physical measurements. Upload
only ``entry.metadata`` and ``entry.path``; ``truth_profile`` is evaluator-only.
Observations must be obtained through the normal RAW importer, never from truth.
"""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image

from . import calibration_lab_engine as engine

SCHEMA = "camera_calibration_synthetic_raw_dataset_v1"
GENERATOR = "synthetic_native_rggb_chart_acquisition_v1"
ROLES = ("training", "final_validation")
RECOVERY_TOLERANCES = {
    "electronics.black_level_dn": {"absolute": 0.3, "unit": "DN"},
    "electronics.dn_per_e": {"relative": 0.03, "unit": "DN/e"},
    "electronics.read_noise_e": {"absolute": 1.0, "unit": "e"},
    "electronics.dark_current_e_per_s": {"relative": 0.06, "unit": "e/s"},
    "shading.radial_coefficients": {"max_absolute": 0.05, "unit": "1"},
    "geometry.radial_coefficients": {"max_absolute": 0.025, "unit": "1"},
    "blur.field_nodes.0.sigma_x_px": {"absolute": 0.2, "unit": "native_pixel"},
    "blur.field_nodes.0.sigma_y_px": {"absolute": 0.2, "unit": "native_pixel"},
    "raw_response.color_matrix": {"max_absolute": 0.03, "unit": "1"},
    "raw_response.tone_output": {"max_absolute": 0.015, "unit": "1"},
}
CHART_THRESHOLDS = {
    "dark": 0.5,
    "ptc": 5.0,
    "shading": 0.02,
    "geometry": 0.006,
    "mtf": 0.08,
    "color": 0.012,
    "tone": 0.012,
    "texture": 0.15,
}
SCOPE = [
    "Synthetic integer RGGB acquisitions; the same model family generates and fits images.",
    "Truth is evaluator-only; upload metadata holds chart references and acquisition declarations.",
    "Shading is an effective radial polynomial. Identity truth RAW tone avoids confounding it.",
    "Truth RAW color matrix has unit neutral column sums, fixing the tone/color gain ambiguity.",
    "The baseline RAW tone is deliberately wrong; recovery still updates its monotone LUT.",
    "Geometry is an ideal-image radial warp; intrinsics/lens prescription are not fitted.",
    "MTF uses isotropic Gaussian blur and cardinal RAW edges; phase/anisotropy are not identified.",
    "Texture is comparison-only. ISP, full well, spectral QE, PRNU/DSNU and timing stay fixed.",
    "Black subtraction metadata is the declared baseline pedestal, not the secret truth pedestal.",
    "Independent role seeds/sessions test held-out acquisitions, not a different physical camera.",
]


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _profiles():
    baseline = engine.default_profile()
    baseline["native_shape"] = baseline["render_shape"] = [256, 256]
    baseline["raw_response"].update(
        tone_input=[0, 0.25, 0.5, 0.75, 1], tone_output=[0, 0.30, 0.56, 0.79, 1]
    )
    truth = deepcopy(baseline)
    truth["electronics"].update(
        black_level_dn=72.0, dn_per_e=0.55, read_noise_e=6.0, dark_current_e_per_s=18.0
    )
    truth["shading"]["radial_coefficients"] = [-0.24, 0.06]
    truth["geometry"]["radial_coefficients"] = [0.055, -0.018]
    truth["blur"]["field_nodes"][0].update(sigma_x_px=1.45, sigma_y_px=1.45)
    truth["raw_response"].update(
        tone_input=[0, 0.25, 0.5, 0.75, 1],
        tone_output=[0, 0.25, 0.5, 0.75, 1],
        color_matrix=[[0.92, 0.03, 0.02], [0.05, 0.94, 0.04], [0.03, 0.03, 0.94]],
    )
    return engine.validate_profile(baseline), engine.validate_profile(truth)


def _seed(seed, role_index, chart_index, level, repeat):
    return int(
        np.random.SeedSequence([seed, role_index, chart_index, level, repeat]).generate_state(1)[0]
    )


def _metadata(seed, role, identity, chart, baseline, shape, analysis):
    origin = [(256 - size) // 2 for size in shape]
    return {
        "campaign_id": "REPLACE_WITH_CAMPAIGN_ID",
        "chart": chart,
        "source_kind": "synthetic",
        "measurement_domain": "camera_raw",
        "physical_instance": f"synthetic-raw-camera-{seed}",
        "session_id": f"synthetic-raw-{seed}-{role}",
        "acquisition_id": f"synthetic-raw-{seed}-{role}-{identity}",
        "role": role,
        "conditions": {**deepcopy(baseline["conditions"]), "exposure_s": 0.01},
        "observations": {},
        "uncertainty": {"kind": "unknown"},
        "method": {"id": GENERATOR, "version": "1"},
        "instrument": {"id": "synthetic-camera-simulator", "calibration_revision": "synthetic-v1"},
        "reference": {
            "id": f"authored-{chart}-target",
            "source_uri": f"synthetic://raw/{seed}/{role}/{identity}",
            "method": "known generated chart raster and reference patch levels",
            "encoding": "linear_rgb",
            "white_point": "D65",
        },
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": baseline["electronics"]["black_level_dn"],
            "response_type": "linear",
        },
        "geometry": {"full_sensor_shape": [256, 256], "crop_origin": origin},
        "analysis": analysis,
    }


def _patch_rois(count):
    return [[24 + 80 * (i // 3), 24 + 80 * (i % 3), 24, 24] for i in range(count)]


def _target(chart, role_index):
    """Known target data only; no fitted/truth response values are returned."""
    if chart == "shading":
        radii = [0, 0.24, 0.44, 0.64, 0.84] if role_index == 0 else [0, 0.29, 0.49, 0.69, 0.86]
        rois = []
        for radius in radii:
            center = 127.5 + radius * 127.5
            start = int(round(center - 9.5))
            rois.append({"box": [start, start, 20, 20], "field_radius": radius})
        return {"rois": rois}, {"field_radius": radii, "relative_response": [1] * len(radii)}
    if chart == "geometry":
        axis = (
            [-0.65, -0.3, 0.0, 0.3, 0.65] if role_index == 0 else [-0.61, -0.26, 0.04, 0.34, 0.68]
        )
        points = [[x, y] for y in axis for x in axis if abs(x) + abs(y) > 0.1]
        rois = [
            [int(round((y + 1) * 255 / 2)) - 9, int(round((x + 1) * 255 / 2)) - 9, 20, 20]
            for x, y in points
        ]
        return {"rois": rois, "object_xy": points}, {
            "object_xy": points,
            "image_xy": [[0, 0]] * len(points),
        }
    if chart == "color":
        colors = np.array(
            [
                [0.7, 0.08, 0.08],
                [0.08, 0.7, 0.08],
                [0.08, 0.08, 0.7],
                [0.25, 0.25, 0.25],
                [0.6, 0.35, 0.1],
                [0.1, 0.45, 0.6],
                [0.5, 0.15, 0.55],
                [0.55, 0.6, 0.15],
                [0.6, 0.6, 0.6],
            ]
        )
        if role_index:
            colors = 0.91 * colors + 0.018
        values = colors.tolist()
        return {"rois": _patch_rois(len(values)), "reference_rgb": values}, {
            "reference_rgb": values
        }
    if chart == "tone":
        levels = [0, 0.25, 0.5, 0.75, 1] if role_index == 0 else [0, 0.15, 0.4, 0.65, 0.85, 1]
        return {"rois": _patch_rois(len(levels)), "input_level": levels}, {"input_level": levels}
    raise ValueError("Unsupported generated target")


def _texture(shape, role_index):
    h, w = shape
    _, xx = np.indices((h * 4, w * 4), dtype=float)
    phase = 0.0 if role_index == 0 else 0.37
    frequencies = [0.0625, 0.125, 0.1875]
    raster = 0.45 + sum(0.08 * np.sin(2 * np.pi * f * (xx / 4 - 0.5) + phase) for f in frequencies)
    image = np.repeat(raster[..., None], 3, axis=2)
    reference = raster[2::4, 2::4][0::2, 1::2]
    amplitude = np.abs(np.fft.fft2(reference / reference.mean() - 1)) / reference.size
    fy, fx = np.meshgrid(
        np.fft.fftfreq(reference.shape[0], d=2),
        np.fft.fftfreq(reference.shape[1], d=2),
        indexing="ij",
    )
    radius = np.hypot(fy, fx)
    step = max(1 / (2 * reference.shape[0]), 1 / (2 * reference.shape[1]))
    ideal = [float(amplitude[abs(radius - f) <= step / 2].mean()) for f in frequencies]
    return image, {"frequency_cy_px": frequencies, "reference_amplitude": ideal}


def generate_dataset(output_dir, seed=1729):
    """Write 18 independent acquisition entries and an evaluator-only manifest.

    Paths in the returned manifest are absolute. Refuse nonempty output folders.
    The protocol/tolerances are written before any held-out acquisition exists.
    """
    if type(seed) is not int or not 0 <= seed < 2**31:
        raise ValueError("seed must be an integer in [0,2**31)")
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Synthetic dataset output must be a new or empty directory")
    root.mkdir(parents=True, exist_ok=True)
    baseline, truth = _profiles()
    campaign = {
        "name": f"Synthetic RAW all-chart calibration {seed}",
        "camera_id": f"synthetic-raw-camera-{seed}",
        "native_shape": baseline["native_shape"],
        "pixel_pitch_um": baseline["pixel_pitch_um"],
        "conditions": deepcopy(baseline["conditions"]),
        "baseline_profile": baseline,
        "thresholds": deepcopy(CHART_THRESHOLDS),
    }
    protocol = {
        "generator": GENERATOR,
        "seed": seed,
        "scope": SCOPE,
        "recovery_tolerances": RECOVERY_TOLERANCES,
        "chart_thresholds": CHART_THRESHOLDS,
        "split_policy": "separate seeds/sessions/acquisitions; freeze candidate before validation",
        "fixed_parameters": [
            "electronics.full_well_e",
            "electronics.bit_depth",
            "pixel_pitch_um",
            "isp",
        ],
        "source_kind": "synthetic",
        "physical_validation": "not_established",
    }
    _write_json(root / "protocol.json", protocol)
    _write_json(root / "truth-profile.json", truth)
    model = engine._electrical_model(truth)
    entries = []
    charts = ["dark", "ptc", "shading", "geometry", "color", "tone", "mtf", "mtf", "texture"]
    for role_index, role in enumerate(ROLES):
        directory = root / role
        directory.mkdir()
        for chart_index, chart in enumerate(charts):
            orientation = 90 if chart_index == 7 else 0
            identity = f"mtf-{orientation}" if chart == "mtf" else chart
            shape = (128, 128) if chart in {"dark", "ptc", "mtf"} else (256, 256)
            analysis = {"channel": "G1"}
            observations = {}
            if chart in {"dark", "ptc"}:
                levels = (
                    [0.001, 0.01, 0.04, 0.1, 0.3, 0.8, 1.5, 3.0]
                    if chart == "dark"
                    else [8, 24, 60, 140, 300, 600, 1000]
                )
                if role_index:
                    levels = [x * 1.07 for x in levels]
                if chart == "dark":
                    analysis["exposure_s"] = levels
                charges = [np.full(shape, 0.0 if chart == "dark" else level) for level in levels]
                exposures = levels if chart == "dark" else [0.01] * len(levels)
                repeats = 32
            else:
                if chart == "mtf":
                    analysis.update(
                        orientation_deg=orientation,
                        field_xy=[0, 0],
                        field_coordinate_domain="sensor_normalized",
                    )
                    observations = {"orientation_deg": orientation, "field_xy": [0, 0]}
                elif chart == "texture":
                    scene, reference = _texture(shape, role_index)
                    analysis.update(
                        reference, field_xy=[0, 0], field_coordinate_domain="sensor_normalized"
                    )
                else:
                    specific, observations = _target(chart, role_index)
                    analysis.update(specific)
                meta = _metadata(seed, role, identity, chart, baseline, shape, analysis)
                if chart != "texture":
                    scene, _ = engine._scene(truth, {**meta, "observations": observations}, shape)
                planes, _ = engine._effective_blur(
                    engine._source_response(scene, truth), truth["blur"]["field_nodes"][0]
                )
                yy, xx = np.indices(shape)
                channels = np.where(
                    yy % 2 == 0, np.where(xx % 2 == 0, 0, 1), np.where(xx % 2 == 0, 1, 2)
                )
                charges = [
                    np.take_along_axis(planes, channels[..., None], axis=2)[..., 0]
                    * truth["reference_white_e"]
                ]
                exposures, repeats = [0.01], 16
            meta = _metadata(seed, role, identity, chart, baseline, shape, analysis)
            raw = np.stack(
                [
                    np.stack(
                        [
                            engine._capture_electrical(
                                truth,
                                model,
                                meta["geometry"],
                                charge,
                                exposure,
                                _seed(seed, role_index, chart_index, level_index, repeat),
                            )["arrays"]["raw_dn"]
                            for repeat in range(repeats)
                        ]
                    )
                    for level_index, (charge, exposure) in enumerate(
                        zip(charges, exposures, strict=True)
                    )
                ]
            ).astype(np.uint16)
            if len(charges) == 1:
                raw = raw[0]
            path = directory / f"{identity}.npy"
            stream = io.BytesIO()
            np.save(stream, raw, allow_pickle=False)
            content = stream.getvalue()
            path.write_bytes(content)
            metadata_path, preview = directory / f"{identity}.json", directory / f"{identity}.png"
            _write_json(metadata_path, meta)
            first = raw.reshape(-1, *shape)[0]
            Image.fromarray(np.rint(first / 4095 * 255).astype(np.uint8)).save(preview)
            entries.append(
                {
                    "id": f"{role}-{identity}",
                    "chart": chart,
                    "role": role,
                    "metadata": meta,
                    "path": str(path),
                    "metadata_path": str(metadata_path),
                    "preview": str(preview),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "shape": list(raw.shape),
                }
            )
    manifest = {
        "schema": SCHEMA,
        **protocol,
        "campaign": campaign,
        "truth_profile": truth,
        "entries": entries,
        "artifact_paths": {
            "manifest": str(root / "manifest.json"),
            "protocol": str(root / "protocol.json"),
            "truth_profile": str(root / "truth-profile.json"),
        },
    }
    _write_json(root / "manifest.json", manifest)
    return manifest

#!/usr/bin/env python3
"""Render moving-bar global/rolling shutter captures through spectral optics.

Run from an installed checkout: python tools/render_temporal_capture_demo.py.
The PNG uses one fixed DN scale. NPZ arrays are unnormalized sensor outputs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from pyisetcam import (
    TemporalCaptureConfig,
    camera_compute_temporal,
    camera_create,
    configure_e2e_sensor,
    scene_create,
    scene_set,
    sensor_get,
    sensor_set,
)


def moving_bar_input():
    """A static monochrome camera and a continuous subpixel bar trajectory."""
    rows, cols = 32, 64
    camera = camera_create("monochrome")
    sensor = sensor_set(camera.fields["sensor"], "size", (rows, cols))
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 0)
    camera.fields["sensor"] = configure_e2e_sensor(
        sensor,
        readout={
            "black_level_dn": 0,
            "read_noise_e": 0,
            "dark_current_e_per_s": 0,
            "full_well_e": 10000,
            "bit_depth": 12,
        },
    )
    scene = scene_create("uniform ee", rows)
    scene = scene_set(scene, "wave", np.arange(400.0, 701.0, 50.0))
    spectrum = np.asarray(scene.data["photons"])[0, 0, :].copy()
    scene = scene_set(
        scene, "photons", np.broadcast_to(spectrum, (rows, cols, spectrum.size)).copy()
    )
    scene = scene_set(
        scene, "fov", sensor_get(sensor, "fov horizontal", scene, camera.fields["oi"])
    )
    pixel_left = np.arange(cols, dtype=float) - 0.5

    def scene_at(time_s: float):
        # Fractional pixel coverage avoids discrete jumps in the input motion.
        center = 16.0 + 500.0 * float(time_s)
        coverage = np.maximum(
            np.minimum(pixel_left + 1.0, center + 1.5) - np.maximum(pixel_left, center - 1.5),
            0.0,
        )
        photons = np.broadcast_to(
            coverage[None, :, None] * spectrum[None, None, :],
            (rows, cols, spectrum.size),
        ).copy()
        return scene_set(scene.clone(), "photons", photons)

    return camera, scene_at


def row_metrics(mean_electrons: np.ndarray) -> dict:
    """Measure bar skew and blur on charge before clipping or quantization.

    Width is the spatial standard deviation of each row's signal distribution.
    Removing the row minimum rejects a constant background without thresholding
    the blur tails. Skew is a fitted full-height centroid displacement.
    """
    charge = np.asarray(mean_electrons, dtype=float)
    signal = np.maximum(charge - charge.min(axis=1, keepdims=True), 0.0)
    totals = signal.sum(axis=1)
    if not np.all(totals > 0):
        raise ValueError("The moving bar must have positive signal in every row.")
    x = np.arange(charge.shape[1], dtype=float)
    centroids = (signal * x[None, :]).sum(axis=1) / totals
    variances = (signal * (x[None, :] - centroids[:, None]) ** 2).sum(axis=1) / totals
    # Exclude first/last rows, where optical boundary handling is most visible.
    margin = max(1, charge.shape[0] // 8)
    interior = slice(margin, charge.shape[0] - margin)
    row_numbers = np.arange(charge.shape[0], dtype=float)
    slope = float(np.polyfit(row_numbers[interior], centroids[interior], 1)[0])
    return {
        "row_centroid_skew_px": slope * (charge.shape[0] - 1),
        "row_centroid_slope_px_per_row": slope,
        "mean_row_sigma_px": float(np.mean(np.sqrt(variances[interior]))),
        "row_centroid_px": centroids.tolist(),
        "row_sigma_px": np.sqrt(variances).tolist(),
        "mean_electrons_per_pixel": float(charge.mean()),
        "max_mean_electrons": float(charge.max()),
    }


def render(output_dir: Path, *, samples_per_exposure: int = 16) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    camera, scene_at = moving_bar_input()
    arrays = {}
    captures = {}
    case_names = []
    for rolling in (False, True):
        for exposure_name, exposure_s in (("short", 0.002), ("long", 0.016)):
            name = f"{'rolling' if rolling else 'global'}_{exposure_name}"
            current = camera.clone()
            current.fields["sensor"] = sensor_set(
                current.fields["sensor"], "integration time", exposure_s
            )
            timing = TemporalCaptureConfig(
                frame_start_s=0.0,
                line_time_s=0.001,
                rolling_shutter=rolling,
                samples_per_exposure=samples_per_exposure,
                max_scene_samples=4096,
            )
            result = camera_compute_temporal(
                current, scene_at, timing=timing, seed=2026, sensor_resize=False
            )
            sensor = result.fields["sensor"]
            raw = np.asarray(sensor.data["raw_dn"])
            charge = np.asarray(sensor.data["mean_electrons"])
            arrays[f"{name}_raw_dn"] = raw
            arrays[f"{name}_mean_electrons"] = charge
            arrays[f"{name}_reference_dn"] = np.asarray(sensor.data["reference_dn"])
            captures[name] = {
                "rolling_shutter": rolling,
                "exposure_s": exposure_s,
                "line_time_s": 0.001,
                "raw_shape": list(raw.shape),
                "raw_dtype": str(raw.dtype),
                "raw_min_dn": int(raw.min()),
                "raw_max_dn": int(raw.max()),
                **row_metrics(charge),
                "temporal_capture": sensor.metadata.get("temporal_capture", {}),
            }
            case_names.append(name)

    summary = {
        "description": "Moving vertical bar captured by one stationary spectral camera",
        "bar_width_scene_pixels": 3.0,
        "bar_speed_scene_pixels_per_s": 500.0,
        "samples_per_exposure": samples_per_exposure,
        "noise_flag": 0,
        "dark_current_e_per_s": 0.0,
        "read_noise_e": 0.0,
        "bit_depth": 12,
        "black_level_dn": 0.0,
        "display_scale_dn": [0.0, 1000.0],
        "display_interpolation": "nearest",
        "display_normalization": "one shared fixed scale; no per-image scaling",
        "metrics_domain": "mean photoelectrons before noise/full-well/ADC",
        "blur_width_definition": "mean within-row spatial standard deviation in pixels",
        "skew_definition": "fitted centroid slope over interior rows times (height - 1)",
        "captures": captures,
        "artifacts": {
            "raw_npz": "temporal_capture_raw.npz",
            "montage_png": "temporal_capture_montage.png",
            "metrics_json": "temporal_capture_metrics.json",
        },
    }
    np.savez_compressed(output_dir / summary["artifacts"]["raw_npz"], **arrays)
    figure, axes = plt.subplots(2, 2, figsize=(11.5, 6.8), layout="constrained")
    for axis, name in zip(axes.flat, case_names, strict=True):
        item = captures[name]
        displayed = axis.imshow(
            arrays[f"{name}_raw_dn"],
            cmap="gray",
            vmin=0,
            vmax=1000,
            interpolation="nearest",
            origin="upper",
        )
        mode = "Rolling shutter" if item["rolling_shutter"] else "Global shutter"
        axis.set_title(f"{mode}  |  {item['exposure_s'] * 1000:g} ms exposure", fontsize=12)
        axis.set_xlabel(
            f"Skew {item['row_centroid_skew_px']:+.2f} px  ·  "
            f"Row blur σ {item['mean_row_sigma_px']:.2f} px",
            fontsize=10,
        )
        axis.set_ylabel("Sensor row")
        axis.set_xticks([0, 16, 32, 48, 63])
        axis.set_yticks([0, 15, 31])
    figure.colorbar(displayed, ax=axes, shrink=0.8, label="RAW DN (same display scale)")
    figure.suptitle(
        "Exposure blur and rolling shutter from temporal photon integration\n"
        "500 scene px/s · 1 ms/row · noise disabled · fixed camera / optics / readout",
        fontsize=14,
    )
    figure.savefig(output_dir / summary["artifacts"]["montage_png"], dpi=160)
    plt.close(figure)
    (output_dir / summary["artifacts"]["metrics_json"]).write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/temporal_capture"))
    parser.add_argument("--samples-per-exposure", type=int, default=16)
    args = parser.parse_args()
    summary = render(args.output_dir, samples_per_exposure=args.samples_per_exposure)
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir.resolve()),
                "captures": {
                    name: {
                        key: capture[key] for key in ("row_centroid_skew_px", "mean_row_sigma_px")
                    }
                    for name, capture in summary["captures"].items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

"""Reference ISP stage artifacts and fixed-source diagnostics, in linear RGB units."""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_erosion, gaussian_filter

from . import reference_isp
from . import virtual_lab_metrics as metrics
from .design_workshop_engine import write_json
from .virtual_lab_engine import _png
from .virtual_lab_job import sha256


def bypass_profile(profile):
    result = copy.deepcopy(profile)
    result["raw_denoise"]["strength"] = 0.0
    result["linear_sharpen"]["amount"] = 0.0
    # Texture loss is measured against the full frozen calibration, never the
    # candidate's dimmed/capped LSC. Preserve the original LSC enable policy.
    result.pop("lsc_tuning", None)
    return result


def common_mask(raws, readout):
    """One source-only support for every candidate, including the full filter halo."""
    values = np.asarray(raws)
    valid = np.all((values > 0) & (values < 2 ** readout["bit_depth"] - 1), axis=0)
    return binary_erosion(valid, structure=np.ones((17, 17), bool), border_value=0)


def temporal_rgb(frames, mask):
    frames = np.asarray(frames, dtype=float)
    if len(frames) < 2 or not mask.any():
        return None, None
    rgb = np.sqrt(np.mean(np.var(frames[:, mask], axis=0, ddof=1), axis=0))
    luma = np.sqrt(np.mean(np.var(frames[:, mask] @ metrics.LUMA, axis=0, ddof=1)))
    return dict(zip(("r", "g", "b"), map(float, rgb), strict=True)), float(luma)


def _supported_rectangle(mask, minimum=8):
    """Largest all-valid rectangle, with deterministic ties and no hole filling."""
    heights = np.zeros(mask.shape[1], dtype=int)
    best, area = None, 0
    for row in range(mask.shape[0]):
        heights = np.where(mask[row], heights + 1, 0)
        stack = []
        for col in range(mask.shape[1] + 1):
            height = int(heights[col]) if col < mask.shape[1] else 0
            left = col
            while stack and stack[-1][1] > height:
                start, old_height = stack.pop()
                width = col - start
                if old_height >= minimum and width >= minimum and old_height * width > area:
                    best = [row + 1 - old_height, row + 1, start, col]
                    area = old_height * width
                left = start
            if height and (not stack or stack[-1][1] < height):
                stack.append((left, height))
    return best


def temporal_noise_power_spectrum(frames, mask):
    """Two-dimensional temporal NPS and its radial mean in native pixel units.

    Fixed scene and FPN are removed by subtracting the per-pixel temporal mean.
    The periodograms divide by N-1 and sum(window**2), so integrating the full
    2-D spectrum equals the Hann-weighted mean unbiased temporal variance. No
    per-frame spatial mean is removed: frame-wide temporal noise remains.
    """
    values = np.asarray(frames, dtype=float)
    support = np.asarray(mask, dtype=bool)
    record = {
        "status": "unavailable",
        "reason": None,
        "frequency_cycles_per_pixel": [],
        "radial_power": [],
        "frequency_bin_counts": [],
        "units": "linear_srgb_luma_squared_per_(cycles_per_native_pixel)^2",
        "frequency_units": "cycles_per_native_pixel",
        "signal": "processed_linear_RGB_dot_Rec709_luma_before_display_white_and_gamma",
        "temporal_estimator": "per_pixel_temporal_mean_subtraction; sum_periodograms/(N-1)",
        "window": "separable_symmetric_Hann; normalized_by_sum_window_squared",
        "radial_definition": (
            "mean_2D_density_in_nearest_radius_bins; radial_mean_is_not_band_power"
        ),
        "radial_support": [0.0, 0.5],
        "roi_policy": "largest_fully_source_supported_rectangle_minimum_8x8",
        "spatial_detrend": None,
        "scope": "temporal_noise_only; excludes_fixed_scene_and_repeat_fixed_pattern",
    }
    if values.ndim != 4 or values.shape[-1] != 3 or support.shape != values.shape[1:3]:
        record["reason"] = "invalid_repeat_or_mask_shape"
        return record
    record["repeat_count"] = len(values)
    if len(values) < 2:
        record["reason"] = "at_least_two_independent_captures_required"
        return record
    bounds = _supported_rectangle(support)
    if bounds is None:
        record["reason"] = "no_fully_supported_8x8_rectangle"
        return record
    r0, r1, c0, c1 = bounds
    luma = values[:, r0:r1, c0:c1] @ metrics.LUMA
    if not np.isfinite(luma).all():
        record["reason"] = "nonfinite_supported_samples"
        return record
    rows, cols = luma.shape[1:]
    window = np.hanning(rows)[:, None] * np.hanning(cols)[None, :]
    window_energy = float(np.sum(window**2))
    residuals = luma - np.mean(luma, axis=0, keepdims=True)
    spectrum = np.fft.fft2(residuals * window, axes=(-2, -1))
    power = np.sum(np.abs(spectrum) ** 2, axis=0) / ((len(values) - 1) * window_energy)
    radius = np.hypot(np.fft.fftfreq(rows)[:, None], np.fft.fftfreq(cols)[None, :])
    bins = min(rows, cols) // 2
    step = 0.5 / bins
    selected = radius <= 0.5
    indices = np.minimum(np.floor(radius[selected] / step + 0.5).astype(int), bins)
    counts = np.bincount(indices, minlength=bins + 1)
    sums = np.bincount(indices, weights=power[selected], minlength=bins + 1)
    record.update(
        status="ok",
        roi=bounds,
        roi_shape=[rows, cols],
        frequency_cycles_per_pixel=(np.arange(bins + 1) * step).tolist(),
        radial_power=[float(total / count) if count else None
                      for total, count in zip(sums, counts, strict=True)],
        frequency_bin_counts=counts.tolist(),
        window_energy=window_energy,
        integrated_2d_nps_variance=float(power.sum() / (rows * cols)),
        window_weighted_temporal_variance=float(
            np.sum(np.var(luma, axis=0, ddof=1) * window**2) / window_energy
        ),
        integration_note="full_2D_NPS_sum/(rows*cols); includes diagonal radii above 0.5",
    )
    return record


def _roi_support(mask, rois):
    """Refuse a metric when any of its measured pixels lacks source support."""
    valid_pixels = int(mask.sum()) if mask is not None else 0
    invalid = []
    for identity, bounds in rois.items():
        if (
            mask is None
            or not isinstance(bounds, (list, tuple))
            or len(bounds) != 4
            or any(type(value) is not int for value in bounds)
        ):
            invalid.append(identity)
            continue
        r0, r1, c0, c1 = bounds
        if not (
            0 <= r0 < r1 <= mask.shape[0]
            and 0 <= c0 < c1 <= mask.shape[1]
            and mask[r0:r1, c0:c1].all()
        ):
            invalid.append(identity)
    eligible = bool(rois) and not invalid
    return {
        "eligible": eligible,
        "reason": None
        if eligible
        else "measurement_rois_missing" if not rois else "measurement_roi_outside_common_support",
        "invalid_roi_ids": invalid,
        "valid_pixels": valid_pixels,
        "policy": "all_measurement_roi_pixels_inside_source_common_mask",
    }


class Processor:
    def __init__(self, capture, profile, gain, *, matrix=None, reference_matrix=None):
        self.capture = capture
        self.profile = profile
        self.gain = gain
        self.matrix = np.asarray(matrix if matrix is not None else capture["calibration"]["matrix"])
        self.reference_matrix = np.asarray(
            reference_matrix if reference_matrix is not None else self.matrix
        )
        self.last = None
        self.tests = {
            "schema": "camera_reference_isp_diagnostics_v1",
            "reference_kind": "simulation_reference",
            "charts": {},
            "metric_support": {},
        }
        self.masks = {}

    def __call__(self, raw, readout, matrix, tuning):
        self.last = reference_isp.process(
            raw, readout, self.matrix, tuning, lsc_gain=self.gain, return_stages=True
        )
        return self.last["linear"], self.last["processed"], self.last["display"]

    def bypass(self, raw, readout, matrix, tuning):
        fixed = bypass_profile(tuning)
        fixed["demosaic_method"] = "adaptive_laplacian"
        fixed["color_calibration_id"] = None
        return reference_isp.process(raw, readout, self.reference_matrix, fixed, lsc_gain=self.gain)

    def frame(self, kind, index, folder, raw, linear, processed):
        if index:
            return
        stage_dir = folder / "isp-stages"
        stage_dir.mkdir(exist_ok=True)
        stages = self.last["stages"]
        files = {}
        for name, value in stages.items():
            path = stage_dir / (name + ".npy")
            np.save(path, np.asarray(value, dtype=np.float32))
            files[name] = {"file": "isp-stages/" + path.name, "sha256": sha256(path)}
        for name in ("valid_mask", "noise_hint", "lsc_gain", "frozen_lsc_gain"):
            value = self.last.get(name)
            if value is not None:
                path = stage_dir / (name + ".npy")
                np.save(path, value)
                files[name] = {"file": "isp-stages/" + path.name, "sha256": sha256(path)}
        # All color previews use the same manual white. RAW stages keep normalized DN.
        for key, stage in (("raw_lsc", "lsc_normalized"), ("raw_denoise", "denoised_normalized")):
            _png(folder / (key + ".png"), stages[stage])
        white = self.profile["display_white_level"]
        _png(folder / "demosaic.png", stages["color_linear"] / white, gamma=True)
        delta = np.asarray(stages["sharpen_delta"])
        # Fixed signed +/-5 percent display scale; no per-image normalization.
        _png(folder / "sharpen_delta.png", 0.5 + delta / (0.1 * white))
        write_json(
            stage_dir / "manifest.json",
            {
                "files": files,
                "frame_index": 0,
                "raw_sha256": self.capture["captures"][kind]["raw"][0]["sha256"],
                "profile": self.profile,
                "preview_delta_scale": [-0.05, 0.05],
                "effective_lsc": self.last.get("effective_lsc"),
            },
        )

    def chart(self, kind, folder, raws, linears, processed, images):
        mask = common_mask(raws, self.capture["readout"])
        self.masks[kind] = mask
        np.save(folder / "comparison-valid-mask.npy", mask)
        _png(folder / "comparison-valid-mask.png", mask.astype(float))
        for key in ("raw_lsc", "raw_denoise", "demosaic", "sharpen_delta"):
            images[key] = f"{kind}/{key}.png"
        images["valid_mask"] = f"{kind}/comparison-valid-mask.png"
        rgb, luma = temporal_rgb(processed, mask)
        self.tests["charts"][kind] = {
            "valid_pixels": int(mask.sum()),
            "temporal_noise_rgb": rgb,
            "temporal_noise_luma": luma,
        }
        if kind in {"uniform", "color"}:
            np.save(folder / "processed-repeats.npy", np.asarray(processed, dtype=np.float32))
            prefix = "" if kind == "uniform" else "color_"
            self.tests[prefix + "temporal_noise_rgb" if not prefix else "color_noise_rgb"] = rgb
            self.tests[prefix + "temporal_noise_luma" if not prefix else "color_noise_luma"] = luma
        if kind == "uniform":
            values = np.asarray(processed, dtype=float)
            self.tests["uniform_mean_luma"] = (
                float(np.mean(values[:, mask] @ metrics.LUMA)) if mask.any() else None
            )
            self.tests["uniform_mean_luma_units"] = (
                "processed_linear_srgb_luma_before_display_scaling"
            )
            self.tests["noise_power_spectrum"] = temporal_noise_power_spectrum(values, mask)
            self.tests["effective_lsc"] = self.last.get("effective_lsc") if self.last else None
            write_json(folder / "noise-power-spectrum.json", self.tests["noise_power_spectrum"])
        if kind == "demosaic_chirp":
            self._demosaic(folder, mask, images)
        if kind == "texture":
            # scipy's sigma=1, truncate=4 high-pass filter reads four more pixels
            # around every output of the already supported ISP pipeline.
            metric_mask = binary_erosion(mask, structure=np.ones((9, 9), bool), border_value=0)
            self.masks["texture_metric"] = metric_mask
            np.save(folder / "texture-metric-valid-mask.npy", metric_mask)
            _png(folder / "texture-metric-valid-mask.png", metric_mask.astype(float))
            images["metric_valid_mask"] = "texture/texture-metric-valid-mask.png"
            self.tests["metric_support"]["texture"] = {
                "eligible": bool(metric_mask.any()),
                "reason": None if metric_mask.any() else "no_valid_highpass_support",
                "valid_pixels": int(metric_mask.sum()),
                "isp_halo_pixels": 8,
                "metric_halo_pixels": 4,
            }

    def finalize_tests(self, tests):
        """Qualify the exact color and edge ROIs already used by shared metrics."""
        color_rois = {
            index: patch.get("roi")
            for index, patch in enumerate(tests.get("color", {}).get("patches", []))
        }
        edge_rois = {
            identity: site.get("roi")
            for identity, site in tests.get("resolution", {}).get("sites", {}).items()
        }
        for kind, rois in (("color", color_rois), ("resolution", edge_rois)):
            self.tests["metric_support"][kind] = _roi_support(self.masks.get(kind), rois)

    def texture(self, before, after, record):
        mask = self.masks["texture_metric"]
        if not mask.any():
            self.tests["texture_retention"] = None
            record.update(
                contrast_ratio_to_linear=None,
                fixed_reference_highpass_rms=None,
                processed_highpass_rms=None,
                comparison_valid_pixels=0,
            )
            return
        reference = before - gaussian_filter(before, 1, truncate=4)
        candidate = after - gaussian_filter(after, 1, truncate=4)
        denom = float(np.sqrt(np.mean(reference[mask] ** 2)))
        numerator = float(np.sqrt(np.mean(candidate[mask] ** 2)))
        ratio = numerator / denom if denom > 1e-15 else None
        self.tests["texture_retention"] = ratio
        if ratio is None:
            self.tests["metric_support"]["texture"].update(
                eligible=False, reason="zero_reference_texture_energy"
            )
        record.update(
            contrast_ratio_to_linear=ratio,
            fixed_reference_highpass_rms=denom,
            processed_highpass_rms=numerator,
            comparison_valid_pixels=int(mask.sum()),
        )

    def _demosaic(self, folder, mask, images):
        from pyisetcam.bayer_demosaic import demosaic_bayer

        from .reference_isp_scene import colocated_reference

        record = self.capture["captures"]["demosaic_chirp"]
        path = folder / "planes-electron-rate.npy"
        if not path.exists() and not record.get("planes_sha256"):
            self.tests["demosaic"] = {
                "status": "unavailable",
                "reason": "co_located_reference_missing",
            }
            return
        if not path.exists() or sha256(path) != record.get("planes_sha256"):
            raise ValueError(
                "Preserved co-located plane response changed before demosaic diagnosis."
            )
        planes = np.load(path)
        exposure = self.capture["configuration"]["exposure_ms"] / 1000
        gains = (
            reference_isp.effective_lsc_gain(self.gain, self.profile)
            if self.profile["lsc_enabled"] else None
        )
        target = colocated_reference(
            planes,
            exposure,
            self.matrix,
            gains=gains,
            wb=self.profile["wb"],
            ccm=self.profile["ccm"],
        )
        mosaic = np.load(folder / "mean-electron-rate.npy") * exposure
        if gains is not None:
            mosaic = mosaic * gains
        rgb = demosaic_bayer(mosaic, method=self.profile["demosaic_method"])
        actual = (rgb @ self.matrix) * np.asarray(self.profile["wb"])
        actual = actual @ np.asarray(self.profile["ccm"]).T
        error = actual - target
        luma = error @ metrics.LUMA
        chroma = error - luma[..., None]
        white = self.profile["display_white_level"]
        np.save(folder / "demosaic-reference.npy", target.astype(np.float32))
        np.save(folder / "demosaic-residual.npy", error.astype(np.float32))
        _png(folder / "demosaic_residual.png", 0.5 + chroma / (0.1 * white))
        images["demosaic_residual"] = "demosaic_chirp/demosaic_residual.png"
        self.tests["demosaic"] = {
            "status": "ok" if mask.any() else "unavailable",
            "chroma_residual_rms": float(np.sqrt(np.mean(chroma[mask] ** 2)))
            if mask.any()
            else None,
            "luma_rmse": float(np.sqrt(np.mean(luma[mask] ** 2))) if mask.any() else None,
            "method": self.profile["demosaic_method"],
            "noise_adc_bypassed": True,
            "interpretation": "Co-located model response; not measured ground truth or optical MTF",
        }


def validate_calibration(calibration, root, cell, optical_revision):
    """Replay matrices are immutable and bound to this acquired physical configuration."""
    from .reference_isp_color import validate_binding

    validate_binding(calibration, Path(root))
    return calibration

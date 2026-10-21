"""Synthetic evaluation RAW sweep; separate noise, bias, halo and texture metrics.

This numerical acceptance tool does not fit calibration, choose an optimal
profile, call an API, or establish measured-camera/production ISP performance.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy

from camera_platform import isp, isp_calibration, isp_v2

FRAMES, SIDE, BLACK, WHITE, SIGMA_DN = 16, 48, 64, 4095, 12.0
HALO = 8
CALIBRATION_ID = "fixture-isp-tradeoff-noise-v1"
STRENGTHS, AMOUNTS = (0.0, 0.4, 0.8), (0.0, 0.6)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def save(path, data):
    path.write_bytes(data)
    return {"path": str(path.resolve()), "sha256": digest(data), "bytes": len(data)}


def save_array(path, value):
    stream = io.BytesIO()
    np.save(stream, value, allow_pickle=False)
    return {**save(path, stream.getvalue()), "shape": list(value.shape), "dtype": str(value.dtype)}


def require(condition, description, checks):
    if not condition:
        raise AssertionError(description)
    checks.append(description)


def reflect(index, length):
    if index < 0:
        return -index
    if index >= length:
        return 2 * length - 2 - index
    return index


def scalar_bilinear(signal_dn):
    """Independent scalar RGGB oracle; does not call the ISP demosaic helper."""
    height, width = signal_dn.shape
    site_rgb = ((0, 1), (1, 2))
    result = np.zeros((height, width, 3))
    for y, x, channel in np.ndindex(result.shape):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy, xx = reflect(y + dy, height), reflect(x + dx, width)
                if site_rgb[yy % 2][xx % 2] != channel:
                    continue
                if channel == 1:
                    weight = 1.0 if (dy, dx) == (0, 0) else 0.25 if abs(dy) + abs(dx) == 1 else 0.0
                else:
                    weight = (1.0 if dy == 0 else 0.5) * (1.0 if dx == 0 else 0.5)
                result[y, x, channel] += signal_dn[yy, xx] * weight
    return result


def noise_hint():
    return isp_calibration.validate_numeric_payload(
        {
            "schema": "camera_isp_calibration_v1",
            "sites": ["R", "Gr", "Gb", "B"],
            "lsc": None,
            "noise": {
                "schema": "native_four_site_noise_v1",
                "slope_dn": [0.0] * 4,
                "intercept_dn2": [SIGMA_DN**2] * 4,
                "signal_interval_dn": [-64, 4031],
                "reference": "black_subtracted_dn_before_lsc",
                "variance_model": "slope_times_max_signal_zero_plus_intercept",
                "spatial_independence": "assumed",
            },
        }
    )


def input_signal(kind):
    y, x = np.indices((SIDE, SIDE))
    if kind == "flat":
        return np.full((SIDE, SIDE), 700.0)
    if kind == "edge":
        return np.where(x < SIDE // 2, 400.0, 1000.0)
    return 700.0 + 24 * np.sin(2 * np.pi * x / 8) + 12 * np.cos(2 * np.pi * y / 8)


def metrics(linear_rgb, truth_rgb_dn, kind):
    # This is an arithmetic RGB guide in input-equivalent DN, not luminance.
    guide = linear_rgb.mean(axis=-1) * (WHITE - BLACK)
    truth = truth_rgb_dn.mean(axis=-1)
    region = guide[:, HALO:-HALO, HALO:-HALO]
    average = guide.mean(axis=0)
    residual = region - region.mean(axis=0, keepdims=True)
    result = {
        "temporal_residual_std_dn": float(
            np.sqrt(np.mean(np.sum(residual**2, axis=0) / (FRAMES - 1)))
        ),
        "mean_bias_dn": float(np.mean(region - truth[None, HALO:-HALO, HALO:-HALO])),
        "edge_overshoot_dn": None,
        "edge_undershoot_dn": None,
        "texture_horizontal_amplitude_dn": None,
        "texture_reference_amplitude_dn": None,
        "texture_amplitude_ratio": None,
        "texture_attenuation_fraction": None,
    }
    if kind == "edge":
        # Average repetitions and rows before estimating the edge profile.
        edge = average[HALO:-HALO].mean(axis=0)[SIDE // 2 - 6 : SIDE // 2 + 6]
        result.update(
            edge_overshoot_dn=max(0.0, float(edge.max() - 1000)),
            edge_undershoot_dn=max(0.0, float(400 - edge.min())),
        )
    if kind == "texture":
        y, x = np.indices((SIDE, SIDE))
        basis = np.stack(
            [
                np.ones(x.shape),
                np.sin(2 * np.pi * x / 8),
                np.cos(2 * np.pi * x / 8),
                np.sin(2 * np.pi * y / 8),
                np.cos(2 * np.pi * y / 8),
            ],
            axis=-1,
        )
        basis = basis[HALO:-HALO, HALO:-HALO].reshape(-1, 5)
        observed = np.linalg.lstsq(basis, average[HALO:-HALO, HALO:-HALO].ravel(), rcond=None)[0]
        reference = np.linalg.lstsq(basis, truth[HALO:-HALO, HALO:-HALO].ravel(), rcond=None)[0]
        amplitude, expected = float(np.hypot(*observed[1:3])), float(np.hypot(*reference[1:3]))
        result.update(
            texture_horizontal_amplitude_dn=amplitude,
            texture_reference_amplitude_dn=expected,
            texture_amplitude_ratio=amplitude / expected,
            texture_attenuation_fraction=1 - amplitude / expected,
        )
    return result


def environment():
    import pyisetcam.ip
    import pyisetcam.utils

    modules = [isp, isp_v2, isp_calibration, pyisetcam.ip, pyisetcam.utils]
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "matplotlib": matplotlib.__version__,
        "script_sha256": digest(Path(__file__).read_bytes()),
        "source_sha256": {
            module.__name__: digest(Path(module.__file__).read_bytes()) for module in modules
        },
    }


def plot(results, path):
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5))
    colors = ("#2878b5", "#d76b26")
    for amount, color in zip(AMOUNTS, colors, strict=True):

        def select(kind, selected_amount=amount):
            return [
                next(
                    r
                    for r in results
                    if r["kind"] == kind
                    and r["denoise_strength"] == strength
                    and r["sharpen_amount"] == selected_amount
                )["metrics"]
                for strength in STRENGTHS
            ]

        flat, edge, texture = select("flat"), select("edge"), select("texture")
        label = f"Sharpen amount {amount:g}"
        axes[0, 0].plot(
            STRENGTHS, [r["temporal_residual_std_dn"] for r in flat], "o-", color=color, label=label
        )
        axes[0, 1].plot(
            STRENGTHS, [r["mean_bias_dn"] for r in flat], "o-", color=color, label=label
        )
        axes[1, 0].plot(
            STRENGTHS,
            [r["edge_overshoot_dn"] for r in edge],
            "o-",
            color=color,
            label=label + " / overshoot",
        )
        axes[1, 0].plot(
            STRENGTHS,
            [r["edge_undershoot_dn"] for r in edge],
            "x--",
            color=color,
            label=label + " / undershoot",
        )
        axes[1, 1].plot(
            STRENGTHS,
            [r["texture_amplitude_ratio"] for r in texture],
            "o-",
            color=color,
            label=label,
        )
    titles = (
        "Flat: temporal residual noise",
        "Flat: mean bias",
        "Edge: averaged profile halo",
        "Texture: horizontal harmonic response",
    )
    labels = (
        "Residual RMS [input-equivalent DN]",
        "Mean error [input-equivalent DN]",
        "Overshoot / undershoot [DN]",
        "Amplitude / noiseless v1 reference",
    )
    for ax, title, label in zip(axes.flat, titles, labels, strict=True):
        ax.set(title=title, xlabel="RAW denoise strength", ylabel=label, xticks=STRENGTHS)
        ax.grid(alpha=0.18)
        ax.legend(fontsize=8, frameon=False)
    axes[0, 1].axhline(0, color="#727c86", linewidth=0.8, linestyle=":")
    axes[1, 1].axhline(1, color="#727c86", linewidth=0.8, linestyle=":")
    fig.suptitle("Same synthetic RAW | noise, bias, halo and texture remain separate", fontsize=17)
    fig.text(
        0.5,
        0.927,
        "16 independent repeats x 48 x 48 pixels per scene | "
        "fixed 12 DN Gaussian hint | no calibration fitting",
        ha="center",
        fontsize=10,
    )
    fig.text(
        0.5,
        0.035,
        "Arithmetic sensor-RGB guide; fixed linear display and interior ROI. "
        "Texture < 1 = attenuation, > 1 = boost. No automatic winner.",
        ha="center",
        fontsize=9,
    )
    fig.text(
        0.5,
        0.013,
        "Numerical synthetic acceptance only. Finite sample descriptive metrics are not "
        "calibrated uncertainty, physical-camera SNR or optical MTF.",
        ha="center",
        fontsize=9,
        color="#566575",
    )
    fig.tight_layout(rect=(0, 0.06, 1, 0.915), h_pad=2.3)
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/camera_platform/advanced-isp-v016/tradeoffs"),
    )
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    env, checks, records, inputs = environment(), [], [], []
    calibration = noise_hint()
    calibration_file = save(out / "noise-calibration-numeric.json", canonical(calibration))
    calibration_source = {
        "kind": "estimated",
        "dataset_role": "authored_evaluation_not_used_for_calibration",
        "description": "Fixed authored zero-slope, 144 DN^2 intercept before any test RAW draws. "
        "The calibration is a declared Gaussian noise hint, not estimated from these "
        "evaluation frames or measured hardware.",
        "physical_validation": "not_established",
        "calibration_id": CALIBRATION_ID,
    }
    save(out / "noise-calibration-source.json", canonical(calibration_source))
    fmt = {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": BLACK,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }
    geom = {"kind": "native_sensor", "full_sensor_shape": [SIDE, SIDE], "crop_origin": [0, 0]}
    legacy = isp.default_profile()
    legacy["tone"]["curve"] = "linear"
    for kind, seed in zip(("flat", "edge", "texture"), (7601, 7602, 7603), strict=True):
        signal = input_signal(kind)
        noiseless_raw = np.rint(signal + BLACK).astype(np.uint16)
        noisy = np.rint(
            signal + BLACK + np.random.default_rng(seed).normal(0, SIGMA_DN, (FRAMES, SIDE, SIDE))
        )
        require(
            bool((noisy > 0).all() and (noisy < WHITE).all()),
            kind + ": no input ADC censoring",
            checks,
        )
        raw = noisy.astype(np.uint16)
        raw.setflags(write=False)
        before = digest(raw.tobytes(order="C"))
        truth = scalar_bilinear(noiseless_raw.astype(float) - BLACK)
        exact = isp.process_raw(noiseless_raw, fmt, legacy, geometry=geom)
        require(
            bool(
                np.allclose(
                    exact["arrays"]["ccm_rgb_frames"][0] * (WHITE - BLACK),
                    truth,
                    atol=1e-12,
                    rtol=1e-14,
                )
            ),
            kind + ": independent scalar noiseless demosaic",
            checks,
        )
        baseline = isp.process_raw(raw, fmt, legacy, geometry=geom)
        bypass = isp_v2.default_profile()
        bypass["tone"]["curve"] = "linear"
        bypass_output = isp_v2.process_raw(raw, fmt, bypass, geometry=geom)
        require(
            all(
                bypass_output["arrays"][name].tobytes() == array.tobytes()
                for name, array in baseline["arrays"].items()
            ),
            kind + ": v2 all-bypass bytes equal v1",
            checks,
        )
        inputs.append(
            {
                "kind": kind,
                "seed": seed,
                "raw": save_array(out / f"{kind}-raw.npy", raw),
                "noiseless_raw": save_array(out / f"{kind}-noiseless-raw.npy", noiseless_raw),
                "truth_rgb_dn": save_array(out / f"{kind}-scalar-truth-rgb-dn.npy", truth),
                "raw_array_bytes_sha256": before,
                "raw_format": fmt,
                "geometry": geom,
            }
        )
        for strength in STRENGTHS:
            for amount in AMOUNTS:
                label = f"{kind}-denoise-{strength:g}-sharpen-{amount:g}"
                profile = isp_v2.default_profile(CALIBRATION_ID)
                profile["tone"]["curve"] = "linear"
                profile["raw_denoise"].update(
                    mode="calibrated_same_site_filter_v1", strength=strength, range_floor_dn2=1.0
                )
                profile["linear_sharpen"].update(
                    mode="bounded_detail_v1", amount=amount, threshold=0.0, max_delta=0.025
                )
                profile = isp_v2.validate_profile(profile)
                result = isp_v2.process_raw(
                    raw, fmt, profile, geometry=geom, calibration=calibration
                )
                arrays, summary = result["arrays"], result["summary"]
                require(
                    digest(raw.tobytes(order="C")) == before == summary["input_array_bytes_sha256"],
                    label + ": original RAW bytes unchanged",
                    checks,
                )
                require(
                    summary["calibration_numeric_sha256"] == calibration_file["sha256"],
                    label + ": fixed calibration hash",
                    checks,
                )
                require(
                    arrays["baseline_rgb_frames"].tobytes()
                    == baseline["arrays"]["rgb_frames"].tobytes(),
                    label + ": same v1 baseline",
                    checks,
                )
                require(
                    bool(arrays["rgb_valid_mask_frames"].all()),
                    label + ": all evaluated stencils valid",
                    checks,
                )
                require(
                    bool(
                        (arrays["sharpened_rgb_frames"] > 0).all()
                        and (arrays["sharpened_rgb_frames"] < 1).all()
                    ),
                    label + ": final tone adds no clipping",
                    checks,
                )
                denoise_changed = int(
                    np.count_nonzero(
                        arrays["denoised_normalized_frames"]
                        != arrays["black_subtracted_normalized_frames"]
                    )
                )
                sharpen_changed = int(np.count_nonzero(arrays["sharpen_delta_frames"]))
                require(
                    (denoise_changed > 0) == (strength > 0),
                    label + ": denoise changes actual pixels iff enabled",
                    checks,
                )
                require(
                    (sharpen_changed > 0) == (amount > 0),
                    label + ": sharpen changes actual pixels iff enabled",
                    checks,
                )
                trace = io.BytesIO()
                np.savez_compressed(
                    trace,
                    **{
                        key: arrays[key]
                        for key in (
                            "denoised_normalized_frames",
                            "sharpened_rgb_frames",
                            "sharpen_delta_frames",
                            "rgb_valid_mask_frames",
                        )
                    },
                )
                records.append(
                    {
                        "kind": kind,
                        "denoise_strength": strength,
                        "sharpen_amount": amount,
                        "profile": save(out / (label + "-profile.json"), canonical(profile)),
                        "profile_sha256": summary["profile_sha256"],
                        "summary": save(out / (label + "-summary.json"), canonical(summary)),
                        "stage_trace": save(out / (label + "-stages.npz"), trace.getvalue()),
                        "changed_pixels": {"denoise": denoise_changed, "sharpen": sharpen_changed},
                        "metrics": metrics(arrays["sharpened_rgb_frames"], truth, kind),
                    }
                )
    require(
        environment() == env,
        "Implementation/environment stayed fixed during all candidates",
        checks,
    )
    figure = out / "isp-tradeoffs.png"
    plot(records, figure)
    report = {
        "schema": "camera_isp_synthetic_tradeoff_evidence_v1",
        "status": "passed",
        "checks": checks,
        "inputs": inputs,
        "calibration": calibration_file,
        "calibration_source": calibration_source,
        "environment": env,
        "environment_sha256": digest(canonical(env)),
        "procedure": {
            "frames": FRAMES,
            "noise_model": "independent authored Gaussian before integer nearest rounding; "
            "no shot/PRNU/DSNU",
            "sigma_dn": SIGMA_DN,
            "roi": {"row": [HALO, SIDE - HALO], "column": [HALO, SIDE - HALO]},
            "temporal_noise": "sqrt(mean_pixel(sum_frame((guide-temporal_mean)^2)/(F-1)))",
            "bias": "mean of arithmetic RGB guide minus independent noiseless bilinear "
            "reference, in input-equivalent DN",
            "edge": "temporal and row average; maximum overshoot above1000 / undershoot "
            "below400 in 12-column edge band",
            "texture": "least-squares sine/cosine coefficients at period8; horizontal amplitude "
            "divided by independent noiseless v1 reference amplitude",
            "tuning_order": "signal/noise, calibration, seeds, ROI, metric definitions and six "
            "candidates fixed before sampling",
            "calibration_fitted_to_inputs": False,
            "held_out_from": "calibration fitting only; used to evaluate ISP tuning",
            "independent_final_validation_after_profile_selection": False,
            "automatic_score_or_winner": False,
        },
        "results": records,
        "figure": {"path": str(figure.resolve()), "sha256": digest(figure.read_bytes())},
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Synthetic Gaussian code-space fixtures are not physical sensor characterization.",
            "These finite-sample metrics describe noise/bias/detail tradeoffs separately, "
            "not verified SNR gain or total uncertainty.",
            "No calibration fitter, hardware latency, TNR, optical MTF or automatic "
            "profile selection is tested.",
        ],
    }
    save(out / "tradeoff-evidence.json", canonical(report))
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": len(checks),
                "candidates": len(records),
                "report": str(out / "tradeoff-evidence.json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

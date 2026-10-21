#!/usr/bin/env python3
"""Plot saved native-pixel qualification artifacts, after verifying each SHA.

This reads an existing completed synthetic qualification. It runs no simulator,
contacts no API and changes no input artifact or registry state.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402


def sha(content):
    return hashlib.sha256(content).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def plot(directory: Path):
    directory = directory.resolve()
    image_path = directory / "native-sites-comparison.png"
    report_path = directory / "plot-evidence.json"
    require(not image_path.exists() and not report_path.exists(), "Plot outputs already exist")
    inputs = []

    def read_file(path):
        content = path.read_bytes()
        inputs.append({"path": str(path), "sha256": sha(content), "bytes": len(content)})
        return content

    def record(name):
        return json.loads(read_file(directory / (name + ".json")))

    evidence = record("summary")
    require(evidence["status"] == "passed", "Qualification is not passed")
    hdr = record("hdr-run-record")
    spatial = record("spatial-run-record")
    sensor = record("sensor-record")
    for key, value in (("hdr_run", hdr), ("spatial_run", spatial), ("sensor", sensor)):
        require(evidence["records"][key] == value["id"], "Wrong saved record identity")
    for run in (hdr, spatial):
        require(run["status"] == "succeeded", "Run is not succeeded")
        require(sensor in run["model_snapshots"], "Sensor differs from frozen run input")
    require(sensor["source_kind"] == "estimated", "Expected declared synthetic sensor")
    require(sensor["factory"]["physical_validation"] == "not_established",
            "Physical validation scope differs from this plot")
    summary = hdr["result"]["summary"]
    require(summary["source_kind"] == "simulated" and summary["noise_enabled"] is True,
            "Expected simulated noisy bracket")
    require(summary["simulation_oracle"]["used_by_hdr_merge"] is False,
            "Expectation must not be used by RAW fusion")
    names = sensor["payload"]["native_pixel_response"]["site_names"]
    require(names == ["R", "Gr", "Gb", "B"], "Expected four native Bayer sites")
    artifacts = []

    def artifact(run, name, *, is_json=False):
        candidates = [a for a in run["result"]["artifacts"] if a["name"] == name]
        require(len(candidates) == 1, "Missing or ambiguous artifact " + name)
        descriptor = candidates[0]
        identity = descriptor["sha256"]
        require(len(identity) == 64 and all(c in "0123456789abcdef" for c in identity),
                "Invalid artifact digest")
        require(Path(name).name == name, "Unsafe artifact name")
        index_key = identity + "/" + name
        require(evidence["artifacts"].get(index_key) == descriptor,
                "Artifact differs from completed HTTP qualification")
        path = directory / "artifacts" / identity / name
        content = read_file(path)
        require(sha(content) == identity and len(content) == descriptor["bytes"],
                "Artifact SHA/length mismatch: " + name)
        artifacts.append({"run_id": run["id"], "descriptor": descriptor, "path": str(path)})
        if is_json:
            return json.loads(content)
        array = np.load(io.BytesIO(content), allow_pickle=False)
        require(list(array.shape) == descriptor["shape"], "Wrong array shape: " + name)
        require(str(array.dtype) == descriptor["dtype"], "Wrong array dtype: " + name)
        require(np.isfinite(array).all(), "Nonfinite fixture array: " + name)
        return array

    response = artifact(hdr, "native_pixel_response.json", is_json=True)
    require(response == sensor["payload"]["native_pixel_response"], "Sensor blob mismatch")
    require(response["wavelength_nm"] == [500, 600], "Expected fixture wavelengths")
    np.testing.assert_array_equal(np.asarray(response["values"])[:, 0, 0, :],
                                  [[.2, .35, .6, .8], [.2, .35, .6, .8]])
    raw = artifact(hdr, "raw_frames.npy")
    mean = artifact(hdr, "mean_electrons.npy")
    sites = artifact(hdr, "native_site_index.npy")
    dark = artifact(hdr, "calibration_dark_e.npy")
    merged = artifact(hdr, "hdr_merged_rate.npy")
    valid = artifact(hdr, "hdr_valid_mask.npy")
    spatial_mean = artifact(spatial, "mean_electrons.npy")
    spatial_raw = artifact(spatial, "raw_frames.npy")
    require(raw.shape == mean.shape == dark.shape == (2, 8, 8), "Expected 2x8x8 bracket")
    require(sites.shape == merged.shape == valid.shape == (8, 8), "Expected native 8x8 maps")
    require(np.issubdtype(raw.dtype, np.integer), "RAW must contain actual integer ADC samples")
    require(valid.all(), "This comparison requires all 64 saved HDR pixels valid")
    origin = summary["geometry"]["crop_origin"]
    tile = np.asarray(response["site_index_tile"])
    expected_sites = tile[(np.arange(8)[:, None] + origin[0]) % 2,
                          (np.arange(8)[None, :] + origin[1]) % 2]
    np.testing.assert_array_equal(sites, expected_sites)
    frames = summary["timing"]["frames"]
    exposure_ns = [f["applied"]["exposure_ns"] for f in frames]
    require(exposure_ns == [2_000_000, 4_000_000], "Expected exactly 2 and 4 ms")
    require(np.array_equal(mean[1], 2 * mean[0]), "Expected charge does not scale exactly 2x")
    np.testing.assert_allclose(mean[0], spatial_mean, rtol=5e-13, atol=1e-10)
    calibrations = summary["calibrations"]
    require(all(c["black_level_dn"] == 64 for c in calibrations),
            "Expected the declared fixture black level of 64 DN")
    corrected = np.stack([
        raw[f].astype(float) - calibration["black_level_dn"]
        - dark[f] * calibration["dn_per_e"]
        for f, calibration in enumerate(calibrations)
    ])
    require(np.all(mean > 0) and np.all(corrected > 0), "Nonpositive fixture signal")
    site_count = [int(np.count_nonzero(sites == site)) for site in range(4)]
    expected_means = np.array([[frame[sites == site].mean() for site in range(4)]
                              for frame in mean])
    raw_means = np.array([[frame[sites == site].mean() for site in range(4)]
                         for frame in corrected])
    raw_ratios = raw_means[1] / raw_means[0]
    require(not np.array_equal(raw_means[1], 2 * raw_means[0]),
            "The plot distinguishes sampled RAW from expected charge")
    np.testing.assert_allclose(raw_means.ravel(), summary["prediction"]["values"],
                               rtol=0, atol=1e-10)

    colors = ["#cf5350", "#79b941", "#178d87", "#4e83cf"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.titleweight": "bold", "savefig.facecolor": "white"})
    fig, axes = plt.subplots(2, 3, figsize=(16, 10.2))
    fig.subplots_adjust(left=.055, right=.975, bottom=.16, top=.84, wspace=.32, hspace=.45)
    fig.suptitle("Native Bayer sites | saved 2 ms / 4 ms RAW bracket", x=.055, y=.965,
                 ha="left", fontsize=23, fontweight="bold", color="#172432")
    fig.text(.055, .925,
             "8 x 8 pixels  /  independent R, Gr, Gb, B sites  /  "
             "synthetic sensor response + simulated shot noise and ADC", fontsize=12)
    fig.text(.055, .895,
             "Physical validation: NOT ESTABLISHED  |  "
             "Expected charge is a simulation oracle and is not used by HDR fusion.",
             fontsize=11, color="#7b4434")

    def map_axes(ax, title):
        ax.set_title(title, loc="left", pad=12, fontsize=12)
        ax.set(xticks=range(8), yticks=range(8), xlabel="Native column", ylabel="Native row")
        ax.set_xticks(np.arange(-.5, 8, 1), minor=True)
        ax.set_yticks(np.arange(-.5, 8, 1), minor=True)
        ax.grid(which="minor", color="white", linewidth=.7, alpha=.8)
        ax.tick_params(which="minor", bottom=False, left=False)

    ax = axes[0, 0]
    ax.imshow(sites, cmap=ListedColormap(colors), vmin=-.5, vmax=3.5, interpolation="nearest")
    map_axes(ax, "A  Native CFA (16 pixels per site)")
    for row in range(8):
        for col in range(8):
            ax.text(col, row, names[int(sites[row, col])], ha="center", va="center",
                    fontsize=8, color="white", fontweight="bold")
    raw_max = int(raw.max())
    for index, ax in enumerate(axes[0, 1:]):
        im = ax.imshow(raw[index], cmap="magma", vmin=0, vmax=raw_max,
                       interpolation="nearest")
        map_axes(ax, f"{'BC'[index]}  Actual RAW | {exposure_ns[index] / 1e6:g} ms")
        for row in range(8):
            for col in range(8):
                value = int(raw[index, row, col])
                ax.text(col, row, str(value), ha="center", va="center", fontsize=6.8,
                        color="white" if value < .55 * raw_max else "#252025")
        fig.colorbar(im, ax=ax, fraction=.045, pad=.04, label="ADC DN (includes black = 64)")

    x = np.arange(4)
    for ax, values, title, label, ratios in (
        (axes[1, 0], expected_means, "D  Expected photocharge | exact 2x",
         "Mean photoelectrons / native site pixel", expected_means[1] / expected_means[0]),
        (axes[1, 1], raw_means, "E  Sampled RAW | noise changes the ratio",
         "Mean DN after black + dark subtraction", raw_ratios),
    ):
        ax.set_title(title, loc="left", pad=12, fontsize=12)
        for index in range(2):
            bars = ax.bar(x + (index - .5) * .34, values[index], width=.32,
                          color=colors, alpha=.45 if index == 0 else 1,
                          edgecolor="white", linewidth=.5)
            ax.bar_label(bars, fmt="%.1f", fontsize=7.5, padding=2)
        for site, ratio in enumerate(ratios):
            ax.text(site, values[1, site] + values.max() * .095, f"{ratio:.3f}x",
                    ha="center", fontsize=9, fontweight="bold", color=colors[site])
        ax.set(xticks=x, xticklabels=names, ylabel=label, ylim=(0, values.max() * 1.28))
        ax.yaxis.grid(True, color="#dbe0e5", linewidth=.6)
        ax.set_axisbelow(True)
        ax.legend(handles=[Patch(facecolor="#526278", alpha=.45, label="2 ms"),
                           Patch(facecolor="#526278", label="4 ms")], loc="upper left",
                  frameon=False, ncols=2, fontsize=9)
    ax = axes[1, 2]
    im = ax.imshow(merged / 1e6, cmap="viridis", interpolation="nearest")
    map_axes(ax, "F  HDR native rate | 64/64 valid")
    fig.colorbar(im, ax=ax, fraction=.045, pad=.04, label="Million electrons / pixel / s")
    ax.text(.5, -.23, "RAW + supplied calibration only; no oracle input",
            transform=ax.transAxes, ha="center", fontsize=8.5)
    fig.text(.055, .104,
             "Declared normal-incidence EQE R / Gr / Gb / B: "
             "0.20 / 0.35 / 0.60 / 0.80 at 500 and 600 nm. "
             "Off-axis LUT also declares small azimuth dependence.", fontsize=10)
    fig.text(.055, .075, "Sensor: " + sensor["id"] + "  |  " + sensor["source_uri"],
             fontsize=9, color="#526278")
    fig.text(.055, .048,
             "Saved NPY bytes verified against descriptors before reading. "
             "One noisy bracket; no physical-camera accuracy or statistical performance claim.",
             fontsize=9, color="#526278")
    for source in inputs:
        require(sha(Path(source["path"]).read_bytes()) == source["sha256"],
                "Input changed while plotting")
    fig.savefig(image_path, dpi=180, bbox_inches="tight", pad_inches=.25)
    plt.close(fig)
    tool_path = Path(__file__).resolve()
    report = {
        "schema": "camera_native_pixel_plot_evidence_v1", "status": "passed_for_declared_scope",
        "scope": "Plot of SHA-verified saved arrays; no simulation rerun or physical validation",
        "source_kind": "simulated", "physical_validation": "not_established",
        "tool": {"path": str(tool_path), "sha256": sha(tool_path.read_bytes())},
        "inputs": inputs, "artifacts": artifacts,
        "output": {"path": str(image_path), "sha256": sha(image_path.read_bytes()),
                   "bytes": image_path.stat().st_size},
        "sensor": {"id": sensor["id"], "source_kind": sensor["source_kind"],
                   "source_uri": sensor["source_uri"], "model_sha256": sha(canonical(sensor)),
                   "response_sha256": sha(canonical(response)), "source": response["source"]},
        "runs": {"hdr": hdr["id"], "spatial": spatial["id"]},
        "numeric": {
            "site_names": names, "native_site_pixel_counts": site_count,
            "exposure_ns": exposure_ns, "expected_charge_mean_e": expected_means.tolist(),
            "raw_dark_black_corrected_site_means_dn": raw_means.tolist(),
            "raw_mean_ratios_4ms_over_2ms": raw_ratios.tolist(),
            "expected_charge_pixelwise_exact_ratio": 2,
            "sampled_raw_exact_2x": False, "noise_enabled": summary["noise_enabled"],
            "hdr_valid_pixels": int(valid.sum()),
            "hdr_merged_rate_range_e_per_pixel_s": [float(merged.min()), float(merged.max())],
            "hdr_oracle_used": False,
            "spatial_expected_2ms_max_abs_difference_e": float(abs(spatial_mean-mean[0]).max()),
            "spatial_raw_frame_count": len(spatial_raw),
        },
        "limitations": ["Authored synthetic EQE; independent sites, no charge redistribution",
                        "Site means cover 16 native pixels, not repeated trials",
                        "Expectation scales with exposure; noisy integer RAW need not",
                        "Merged rate retains Bayer site response; not colorimetric radiance"],
        "visual_layout_review": "pending; inspect generated PNG with view_image",
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"image": str(image_path), "report": str(report_path),
                      "raw_ratios": raw_ratios.tolist()}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    plot(parser.parse_args().directory)

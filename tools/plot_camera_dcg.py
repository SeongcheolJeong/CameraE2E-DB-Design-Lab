"""Plot retained synthetic DCG evidence without executing a simulator or API."""

import argparse
import hashlib
import io
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_case(folder, label):
    record_path = folder / f"{label}-run-record.json"
    content = record_path.read_bytes()
    record = json.loads(content)
    result = record["result"]
    if (
        record["status"] != "succeeded"
        or record["plugin_id"] != "shared_charge_dcg_v1"
        or result["output_size"] != [8, 8]
        or result["acquisition_count"] != 1
        or result["readout_ids"] != ["HCG", "LCG"]
        or not result["merge_applied"]
        or result["source_kind"] != "simulated"
        or result["simulation_oracle"]["used_by_merge"]
    ):
        raise ValueError("This figure requires one completed synthetic 8x8 DCG acquisition")
    arrays, sources = {}, []
    for name, unit in {
        "raw_groups": "DN",
        "usable_mask": "boolean",
        "valid_mask": "boolean",
        "electrical_well_saturated_mask": "boolean",
        "electrical_mean_total_charge_e": "electron",
        "merged_rate": "electron/pixel/s",
    }.items():
        matches = [a for a in record["artifacts"] if a["name"] == name + ".npy"]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous artifact: " + name)
        descriptor = matches[0]
        digest = descriptor["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact SHA")
        path = folder / "artifacts" / digest / descriptor["name"]
        data = path.read_bytes()
        if sha(data) != digest or len(data) != descriptor["bytes"] or descriptor["unit"] != unit:
            raise ValueError("Artifact hash, size or unit differs: " + name)
        value = np.load(io.BytesIO(data), allow_pickle=False)
        shape = [1, 2, 8, 8] if name in ("raw_groups", "usable_mask") else [1, 8, 8]
        if (
            list(value.shape) != shape
            or descriptor["shape"] != shape
            or str(value.dtype) != descriptor["dtype"]
        ):
            raise ValueError("Artifact shape or dtype differs: " + name)
        if name.endswith("mask") and value.dtype != np.bool_:
            raise ValueError("Mask must be boolean")
        arrays[name] = value[0]
        sources.append(
            {
                "path": str(path.resolve()),
                "sha256": digest,
                "name": descriptor["name"],
                "shape": shape,
                "dtype": str(value.dtype),
                "unit": unit,
            }
        )
    raw, usable, valid, well = (
        arrays[k]
        for k in ("raw_groups", "usable_mask", "valid_mask", "electrical_well_saturated_mask")
    )
    merge = result["dcg_merge"][0]
    if label == "dark" and np.any(arrays["electrical_mean_total_charge_e"] != 0):
        raise ValueError("Zero-light row requires retained zero mean total charge")
    if (
        sha(raw.tobytes(order="C")) != merge["input_array_bytes_sha256"]
        or not np.array_equal(valid, usable.any(axis=0))
        or int(valid.sum()) != merge["valid_pixels"]
        or not np.isnan(arrays["merged_rate"][~valid]).all()
        or not np.isfinite(arrays["merged_rate"][valid]).all()
    ):
        raise ValueError("Retained RAW, fusion mask, summary or invalid radiometry differs")
    counts = {
        "both_usable": int(usable.all(axis=0).sum()),
        "hcg_only": int((usable[0] & ~usable[1]).sum()),
        "lcg_only": int((~usable[0] & usable[1]).sum()),
        "invalid": int((~valid).sum()),
        "oracle_common_clipped": int(well.sum()),
        "oracle_common_clipped_and_invalid": int((well & ~valid).sum()),
        "oracle_common_clipped_but_guard_usable": int((well & valid).sum()),
    }
    evidence = {
        "label": label,
        "run_id": record["id"],
        "record_path": str(record_path.resolve()),
        "record_sha256": sha(content),
        "capture_program_id": record["capture_program_id"],
        "counts": counts,
        "sources": sources,
        "geometry": result["geometry"],
        "channels": result["channels"],
        "noise_enabled": result["noise_enabled"],
        "seed_sequence": result["seed_sequence"],
        "calibration": result["calibrations"][0],
        "raw_color_limits_dn": [int(raw.min()), int(raw.max())],
    }
    return arrays, result, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", type=Path, default=Path("reports/camera_platform/dcg-v016/http")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports/camera_platform/dcg-v016"))
    args = parser.parse_args()
    cases = [load_case(args.input_dir, label) for label in ("bright", "dark")]
    if cases[0][2]["capture_program_id"] != cases[1][2]["capture_program_id"]:
        raise ValueError("Comparison requires the same fixed capture program")
    colors = ["#243449", "#638ee3", "#e7ad43", "#27aa94"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, axes = plt.subplots(2, 4, figsize=(17, 9.8))
    fig.subplots_adjust(left=0.065, right=0.985, top=0.86, bottom=0.19, wspace=0.26, hspace=0.52)
    fig.suptitle("Shared-charge DCG | retained synthetic RAW evidence", fontsize=21, y=0.965)
    fig.text(
        0.5,
        0.918,
        "8 x 8 native pixels, R / Gr / Gb / B preserved | "
        "one acquisition per row | no physical-camera validation",
        ha="center",
        fontsize=11,
        color="#465465",
    )
    for row, (arrays, result, evidence) in enumerate(cases):
        raw, usable, valid, well = (
            arrays[k]
            for k in ("raw_groups", "usable_mask", "valid_mask", "electrical_well_saturated_mask")
        )
        low, high = evidence["raw_color_limits_dn"]
        for column in range(2):
            ax = axes[row, column]
            image = ax.imshow(
                raw[column], cmap="viridis", vmin=low, vmax=high, interpolation="nearest"
            )
            ax.set_title(f"{['HCG', 'LCG'][column]} original RAW [DN]", fontsize=12, pad=10)
            for y, x in np.ndindex(8, 8):
                ax.text(
                    x,
                    y,
                    str(raw[column, y, x]),
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="#101b25"
                    if (raw[column, y, x] - low) / max(1, high - low) > 0.55
                    else "white",
                )
        fig.colorbar(
            image,
            ax=axes[row, :2],
            orientation="horizontal",
            fraction=0.052,
            pad=0.11,
            label="RAW code [DN] | same color range within this row",
        )
        axes[row, 2].imshow(
            valid,
            cmap=ListedColormap([colors[0], colors[3]]),
            vmin=0,
            vmax=1,
            interpolation="nearest",
        )
        axes[row, 2].set_title(
            "Merged valid mask (RAW guard)\n"
            f"{valid.sum()}/64 retained; {(~valid).sum()}/64 invalid",
            fontsize=12,
        )
        category = usable[0].astype(int) + 2 * usable[1].astype(int)
        axes[row, 3].imshow(
            category, cmap=ListedColormap(colors), vmin=0, vmax=3, interpolation="nearest"
        )
        y, x = np.where(well)
        axes[row, 3].scatter(x, y, marker="x", c="#ff6072", s=32, linewidths=1.5)
        count = evidence["counts"]
        axes[row, 3].set_title(
            "RAW path selection + oracle overlay\n"
            f"LCG only: {count['lcg_only']}; common-clipped: {well.sum()}",
            fontsize=12,
        )
        exposure = result["acquisitions"][0]["exposure_s"] * 1000
        axes[row, 0].set_ylabel(
            f"{'Bright input' if row == 0 else 'Zero-light input'} | {exposure:g} ms\nNative row",
            fontsize=12,
        )
        for ax in axes[row]:
            ax.set_xticks([0, 2, 4, 6, 7])
            ax.set_yticks([0, 2, 4, 6, 7])
            ax.set_xlabel("Native column")
            ax.tick_params(labelsize=8)
    handles = [
        Patch(color=color, label=label)
        for color, label in zip(
            colors, ["Neither usable", "HCG only", "LCG only", "Both usable"], strict=True
        )
    ]
    handles.append(
        Line2D(
            [],
            [],
            marker="x",
            linestyle="none",
            color="#ff6072",
            label="Common-well clipping (simulator oracle)",
        )
    )
    fig.legend(
        handles=handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.09),
        ncol=5,
        frameon=False,
        title="Rightmost panels: path-selection colors and clipping overlay",
    )
    fig.text(
        0.5,
        0.060,
        "Oracle crosses were not used by RAW fusion. Guard validity does not prove "
        "nonclipping. Binary valid-mask panels: teal = valid, navy = invalid.",
        ha="center",
        fontsize=10,
    )
    fig.text(
        0.5,
        0.033,
        "No exposure/scene averaging, noise-floor estimate, SNR improvement, recovery "
        "accuracy or commercial DCG performance is inferred from these two small samples.",
        ha="center",
        fontsize=9,
        color="#465465",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / "dcg-comparison.png"
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)
    report = {
        "schema": "camera_dcg_plot_evidence_v1",
        "script_sha256": sha(Path(__file__).read_bytes()),
        "figure": {"path": str(path.resolve()), "sha256": sha(path.read_bytes()), "dpi": 200},
        "cases": [case[2] for case in cases],
        "rendering": "nearest native cells; no interpolation or demosaic",
        "category_formula": "usable_HCG + 2*usable_LCG; oracle only adds cross markers",
        "physical_validation": "not_established",
        "performance_claim": "none",
    }
    (args.output_dir / "plot-evidence.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print(path)
    print(json.dumps({case[2]["label"]: case[2]["counts"] for case in cases}))


if __name__ == "__main__":
    main()

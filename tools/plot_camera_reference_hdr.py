#!/usr/bin/env python3
"""Plot retained RAW reference fusion evidence without recomputing camera outputs."""

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with zipfile.ZipFile(args.bundle) as z:
        run = json.loads(z.read("manifest.json"))["run"]
        arrays = {}
        for a in run["artifacts"]:
            if a["name"].endswith(".npy"):
                content = z.read(f"artifacts/{a['sha256']}/{a['name']}")
                if hashlib.sha256(content).hexdigest() != a["sha256"]:
                    raise ValueError("Artifact SHA mismatch")
                arrays[a["name"][:-4]] = np.load(io.BytesIO(content), allow_pickle=False)
    s = run["result"]["summary"]["hdr_merge"]
    a = arrays
    ref = s["reference_frame_index"]
    rates = a["hdr_exposure_rate"]
    valid = a["hdr_reference_valid_mask"]
    base = np.where(valid, rates[ref], np.nan)
    fused = a["hdr_merged_rate"]
    vmax = float(np.nanpercentile(np.stack([base, fused]), 99))
    fig, axes = plt.subplots(2, 3, figsize=(13, 8), constrained_layout=True)
    for ax, image, title in (
        (axes[0, 0], base, f"Reference frame {ref}: usable RAW rate"),
        (axes[0, 1], np.where(a["hdr_exposure_valid_mask"][-1], rates[-1], np.nan),
         "Last frame: usable RAW rate"),
        (axes[0, 2], fused, "Reference-constrained fused rate"),
    ):
        im = ax.imshow(image, vmin=0, vmax=vmax, cmap="viridis", interpolation="nearest")
        ax.set_title(title, fontsize=10)
        fig.colorbar(im, ax=ax, shrink=.65, label="electron / pixel / s")
    policy = np.zeros(valid.shape)
    policy[a["hdr_fused_mask"]] = 1
    policy[a["hdr_reference_fallback_mask"]] = 2
    policy[a["hdr_reference_only_unknown_mask"]] = 3
    colors = ListedColormap(["#dadada", "#348270", "#d58c39", "#9c75aa"])
    im = axes[1, 0].imshow(policy, cmap=colors, norm=BoundaryNorm([-.5,.5,1.5,2.5,3.5],4),
                           interpolation="nearest")
    axes[1, 0].set_title("Recorded per-site decision", fontsize=10)
    bar = fig.colorbar(im, ax=axes[1, 0], shrink=.65, ticks=[0,1,2,3])
    bar.ax.set_yticklabels(["Invalid", "Fused", "Reference fallback", "Reference only"])
    ratio = a["hdr_estimated_variance"] / a["hdr_exposure_estimated_variance"][ref]
    im = axes[1, 1].imshow(ratio, vmin=0, vmax=1, cmap="magma", interpolation="nearest")
    axes[1, 1].set_title("Conditional estimated variance / reference", fontsize=10)
    fig.colorbar(im, ax=axes[1, 1], shrink=.65, label="ratio; excludes gate bias / motion error")
    axes[1, 2].axis("off")
    axes[1, 2].text(0, .96, "Authored moving image-plane target\n"
                   "Independent reset exposures; constant light\n\n"
                   f"Fused: {s['fused_pixels']} sites\n"
                   f"Reference fallback: {s['reference_fallback_pixels']} sites\n"
                   f"Reference only / unknown: {s['reference_only_unknown_pixels']} sites\n"
                   f"Invalid reference: {s['reference_invalid_pixels']} sites\n\n"
                   "RAW agreement is not motion correspondence.\n"
                   "No registration or clipped-reference recovery.\n"
                   "Physical validation is not established.", va="top", fontsize=10)
    for ax in axes.flat:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(
        "CameraE2E | Reference-constrained RAW fusion | retained Worker evidence", fontsize=14,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=160)
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()

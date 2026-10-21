#!/usr/bin/env python3
"""Plot retained HDR evidence offline; never reconstruct RAW from simulation truth."""

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with zipfile.ZipFile(args.bundle) as archive:
        run = json.loads(archive.read("manifest.json"))["run"]

        def array(name):
            item = next(a for a in run["artifacts"] if a["name"] == name + ".npy")
            content = archive.read(f"artifacts/{item['sha256']}/{item['name']}")
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("Evidence array checksum mismatch")
            return np.load(io.BytesIO(content), allow_pickle=False)

        rgb, hdr, weights = array("rgb_frames"), array("hdr_rgb"), array("hdr_weights")
        valid = array("hdr_valid_mask")
        exposures = [f["applied"]["exposure_ns"] / 1e6 for f in run["result"]["summary"]["frames"]]
    fig, axes = plt.subplots(1, 4, figsize=(13, 3.6), layout="constrained")
    labels = [f"{exposures[0]:g} ms RAW / per-frame ISP",
              f"{exposures[-1]:g} ms RAW / per-frame ISP",
              "RAW HDR / separate tone curve", "Highest fusion weight: frame index"]
    chosen = np.ma.masked_where(~valid, np.argmax(weights, axis=0))
    for index, (ax, im, label) in enumerate(zip(axes, [rgb[0], rgb[-1], hdr, chosen],
                                              labels, strict=True)):
        artist = ax.imshow(im, interpolation="nearest", **(
            {"cmap": "viridis", "vmin": 0, "vmax": len(exposures) - 1} if index == 3 else {}))
        ax.set_title(label, fontsize=10)
        ax.set_axis_off()
    fig.colorbar(artist, ax=axes[-1], ticks=range(len(exposures)), fraction=0.045)
    fig.suptitle("Authored static spectral chart | separate display pipelines | "
                 "simulation, not physical validation", fontsize=11)
    fig.savefig(args.output, dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only comparison figure from verified 3D camera artifacts."""

import argparse
import hashlib
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    proof = json.loads(args.report.read_text())
    fig, axes = plt.subplots(2, 3, figsize=(10, 6), layout="constrained")
    fig.suptitle("3D material swap | same geometry, illumination and camera", fontsize=14)
    with PlatformClient(args.base_url) as client:
        assert client.get("session")["state_dir"] == proof["state_dir"]
        for row, pair in enumerate(proof["records"]["pairs"]):
            job = client.get("scene_renders/" + pair["render"])
            run = client.get("runs/" + pair["run"])

            def array(record, name):
                a = next(a for a in record["artifacts"] if a["name"] == name)
                response = client.http.get("/api/v1/artifacts/" + a["id"])
                response.raise_for_status()
                assert hashlib.sha256(response.content).hexdigest() == a["sha256"]
                return np.load(io.BytesIO(response.content), allow_pickle=False)

            rgb = array(run, "rgb_frames.npy")[0]
            raw = array(run, "raw_frames.npy")[0]
            depth = array(job, "first-hit-depth-m.npy")
            axes[row, 0].imshow(np.clip(rgb * 4, 0, 1), interpolation="nearest")
            axes[row, 0].set_title(
                ("Red" if row == 0 else "Blue") + " material / ISP RGB, display x4"
            )
            im = axes[row, 1].imshow(raw, cmap="gray", vmin=64, vmax=130, interpolation="nearest")
            axes[row, 1].set_title("Actual RGGB RAW (DN)")
            fig.colorbar(im, ax=axes[row, 1], shrink=0.8)
            im = axes[row, 2].imshow(
                depth, cmap="viridis", vmin=2.68, vmax=3.01, interpolation="nearest"
            )
            axes[row, 2].set_title("First-hit depth (m)")
            fig.colorbar(im, ax=axes[row, 2], shrink=0.8)
            for axis in axes[row]:
                axis.set_xticks([])
                axis.set_yticks([])
    fig.supxlabel(
        "Synthetic 32 x 32 native crop | shared display gain 4 affects this RGB preview only\n"
        "Mitsuba radiance -> declared paraxial projection -> Prysm PSF -> RAW / ISP\n"
        "Physical validation and total prediction uncertainty remain unestablished.",
        fontsize=9,
    )
    fig.savefig(args.output, dpi=150)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Plot retained HTTP proof artifacts without regenerating sensor data."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.proof.read_text())
    if report["status"] != "passed":
        raise ValueError("Use a completed HTTP verification journal")
    root = args.proof.parent
    verified = {}

    def content(entry):
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Proof artifact path leaves its evidence directory")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry["sha256"] or len(data) != entry["bytes"]:
            raise ValueError("Retained proof bytes differ from their identity")
        verified[str(path)] = actual
        return data

    def arrays(label):
        record = json.loads(content(report["records"][label + "-run"]))
        if record["result"]["summary"].get("noise_enabled") is not False:
            raise ValueError("This comparison requires the declared deterministic cases")
        result = {}
        for name in ("raw_groups", "retained_delivered_e", "retained_residual_e",
                     "retained_clear_loss_e", "retained_gap_generated_e"):
            artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
            result[name] = np.load(io.BytesIO(content(report["artifacts"][artifact["sha256"]])),
                                   allow_pickle=False)
        return result

    cases = {name: arrays(name) for name in (
        "retain", "clear", "complete", "gap-dark-short", "gap-dark-long",
    )}
    x = np.arange(3)
    palette = {"retain": "#276078", "clear": "#bb7744", "complete": "#71865b"}
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.facecolor": "white"})
    figure, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    for name, label in (("retain", "50% transfer; retain"),
                        ("clear", "50% transfer; clear before frame 1"),
                        ("complete", "100% transfer")):
        axes[0, 0].plot(x, cases[name]["raw_groups"][:, 0].mean(axis=(1, 2)),
                        "o-", color=palette[name], label=label)
    axes[0, 0].set(title="Actual saved RAW: bright then dark", ylabel="Mean RAW code (DN)")
    axes[0, 0].legend(fontsize=8)
    for name, style in (("delivered", "o-"), ("residual", "s--")):
        axes[0, 1].plot(x, cases["retain"]["retained_" + name + "_e"].mean(axis=(1, 2)),
                        style, label=name)
    axes[0, 1].set(title="Simulation truth: one population split",
                   ylabel="Mean electrons per pixel")
    axes[0, 1].legend()
    for name, label in (("gap-dark-short", "8 ms frame interval"),
                        ("gap-dark-long", "20 ms frame interval")):
        axes[1, 0].plot(x, cases[name]["raw_groups"][:, 0].mean(axis=(1, 2)), "o-", label=label)
    axes[1, 0].set(title="Continuous dark generation between exposures",
                   ylabel="Mean RAW code (DN)")
    axes[1, 0].legend(fontsize=8)
    for name, key, label in (("retain", "retained_residual_e", "Retained: residual"),
                            ("clear", "retained_clear_loss_e", "Clear: removed before exposure")):
        axes[1, 1].plot(x, cases[name][key].mean(axis=(1, 2)), "o-", label=label)
    axes[1, 1].set(title="PD clear removes stored charge", ylabel="Mean electrons per pixel")
    axes[1, 1].legend(fontsize=8)
    for axis in axes.flat:
        axis.set_xticks(x, ["0: bright", "1: dark", "2: dark"])
        axis.set_xlabel("Frame")
        axis.grid(alpha=.15)
    figure.suptitle(
        "Retained photodiode charge · immutable HTTP / Worker evidence\n"
        "Authored deterministic model; ideal optical image input; physical accuracy unknown",
        fontsize=13,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / "retained-charge-comparison.png"
    figure.savefig(path, dpi=160)
    plt.close(figure)
    values = {
        "schema": "camera_retained_charge_plot_v1",
        "proof_sha256": hashlib.sha256(args.proof.read_bytes()).hexdigest(),
        "source_artifact_hashes": verified,
        "figure_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "physical_validation": "not_established",
        "cases": {name: {key: value.mean(axis=tuple(range(1, value.ndim))).tolist()
                         for key, value in data.items()} for name, data in cases.items()},
    }
    (args.output_dir / "numerical-comparison.json").write_text(
        json.dumps(values, indent=2, allow_nan=False) + "\n")
    print(path.resolve())


if __name__ == "__main__":
    main()

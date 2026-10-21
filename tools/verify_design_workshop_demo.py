#!/usr/bin/env python3
"""Verify completed workshop artifacts, same-RAW replay and actual design differences."""

import argparse
import json
from pathlib import Path

import numpy as np

from camera_platform.design_workshop_job import _directory, get, verify_files
from camera_platform.design_workshop_storage import status as storage_status
from camera_platform.service import PlatformService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", default="outputs/design-workshop-delivery-20261011/runs.json")
    parser.add_argument("--state", default="outputs/camera-platform-virtual-lab-cra-20261011")
    parser.add_argument(
        "--output", default="outputs/design-workshop-delivery-20261011/verification.json"
    )
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    service = PlatformService(workspace / args.state, workspace)
    records = json.loads((workspace / args.runs).read_text())
    successful = {r["label"]: r for r in records if r["status"] == "succeeded"}
    required = {
        "baseline",
        "cfa-thickness",
        "through-focus",
        "aperture",
        "low-noise",
        "detail",
        "bright-baseline",
        "bright-cfa",
        "bright-aperture",
    }
    if not required <= set(successful):
        raise ValueError("All nine planned demo groups must finish.")
    checked = []
    for record in successful.values():
        live = get(service, record["id"])
        verify_files(service, live)
        folder = _directory(service, record["id"])
        manifest = json.loads((folder / "manifest.json").read_text())
        checked.append(
            {"id": record["id"], "label": record["label"], "files_verified": len(manifest["files"])}
        )
    baseline = successful["baseline"]["result"]["summary"]

    def raw_hashes(x):
        return [[r["sha256"] for r in scene["raw"]] for scene in x["capture_files"].values()]

    for name in ("low-noise", "detail"):
        result = successful[name]["result"]["summary"]
        assert raw_hashes(baseline) == raw_hashes(result), "Replay RAW changed"
    cfa = successful["cfa-thickness"]["result"]["summary"]["candidates"]
    assert len(cfa) == 3 and all(c["status"] == "succeeded" for c in cfa)
    means = [c["metrics"]["mean_electrons"] for c in cfa]
    assert np.ptp(means) > 0, "CFA structure did not reach RAW"
    focus = successful["through-focus"]["result"]["summary"]["candidates"]
    assert len(focus) == 3 and all(c["status"] == "succeeded" for c in focus)
    assert len({c["optics"]["revision"] for c in focus}) == 3
    assert len({c["capture_files"]["resolution"]["rate_sha256"] for c in focus}) == 3
    bright = successful["bright-baseline"]["result"]["summary"]
    aperture = successful["bright-aperture"]["result"]["summary"]
    assert bright["configuration"]["isp"] == aperture["configuration"]["isp"]
    assert bright["configuration"]["isp"]["display_white_level"] == 10
    assert np.isclose(
        bright["metrics"]["mean_electrons"], baseline["metrics"]["mean_electrons"] * 10
    )
    assert aperture["metrics"]["mean_electrons"] > bright["metrics"]["mean_electrons"]
    bright_cfa = successful["bright-cfa"]["result"]["summary"]["candidates"]
    assert all(c["status"] == "succeeded" for c in bright_cfa)
    assert all(c["configuration"]["isp"] == bright["configuration"]["isp"] for c in bright_cfa)
    # A display-range change must use fixed saved linear data, not frame maxima.
    from PIL import Image

    for name in ("bright-baseline", "bright-aperture"):
        folder = _directory(service, successful[name]["id"])
        summary = successful[name]["result"]["summary"]
        processed = np.load(folder / "color/processed.npy")
        tuning = summary["configuration"]["isp"]
        expected = np.round(np.clip(processed / 10, 0, 1) ** (1 / tuning["tone_gamma"]) * 255)
        actual = np.asarray(Image.open(folder / "color/display.png"))
        assert np.max(np.abs(actual.astype(float) - expected)) <= 1
    ui_replays = []
    qa = workspace / "outputs/design-workshop-demo-20261011/ui-browser-qa.json"
    if qa.exists():
        identity = json.loads(qa.read_text()).get("same_raw_isp", {}).get("run_id")
        if identity:
            record = get(service, identity)
            verify_files(service, record)
            source = get(service, record["source"]["run_id"])
            selected = next(
                c
                for c in source["result"]["summary"]["candidates"]
                if c["id"] == record["source"]["candidate_id"]
            )
            assert raw_hashes(selected) == raw_hashes(record["result"]["summary"])
            ui_replays.append(identity)
    output = {
        "status": "verified",
        "physical_validation": "not_established",
        "groups": checked,
        "same_raw_replays": 2,
        "ui_same_raw_candidate_replays": ui_replays,
        "fixed_display_white_level_verified": 10,
        "bright_aperture_mean_signal_ratio": aperture["metrics"]["mean_electrons"]
        / bright["metrics"]["mean_electrons"],
        "bright_metrics": {"baseline": bright["metrics"], "aperture": aperture["metrics"]},
        "storage": storage_status(service),
        "cfa_mean_electrons": means,
        "native_shape": baseline["geometry"]["native_shape_row_col"],
        "elapsed_seconds": {
            name: r["result"]["summary"].get("performance") for name, r in successful.items()
        },
        "baseline_metrics": baseline["metrics"],
        "limitations": baseline["limitations"],
    }
    target = workspace / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

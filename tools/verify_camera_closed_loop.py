#!/usr/bin/env python3
"""Run and preserve actual development AE/AWB output, separate from installed delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from camera_platform.closed_loop import ClosedLoopSceneRuntime, request_template
from camera_platform.controls import default_policy
from camera_platform.service import environment_fingerprint


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenario():
    request = request_template()
    request["noise_enabled"] = False
    wave = np.asarray(request["wavelength_nm"])
    request["qe"] = [
        (0.6 * np.exp(-0.5 * ((wave - center) / width) ** 2)).tolist()
        for center, width in [(620, 50), (540, 45), (460, 40)]
    ]
    request["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    request["lens"]["relative_illumination"] = [[1, 1]] * 5
    request["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    scene = deepcopy(request["scene_sequence"]["knots"][0]["scene"])
    scene["sampling"]["full_sensor_shape"] = [16, 24]
    scene["spectral_irradiance"] = np.full((16, 24, 5), 3e-5).tolist()
    bright = deepcopy(scene)
    bright["spectral_irradiance"] = (
        np.array(scene["spectral_irradiance"]) * [3.2, 2.4, 2, 1.6, 1]
    ).tolist()
    request["scene_sequence"].update(
        interpolation="zero_order_hold",
        knots=[
            {"time_ns": 0, "scene": scene},
            {"time_ns": 240_000_000, "scene": bright},
            {"time_ns": 480_000_000, "scene": bright},
        ],
    )
    program = request["control_program"]
    program["frames"] = [
        {"frame_id": f"feedback-{i}", "frame_start_ns": i * 10_000_000} for i in range(48)
    ]
    program["policy"] = default_policy([16, 24])
    program["policy"]["latency"].update(frame_transport_ns=2_000_000, statistics_ns=1_000_000)
    return request


def write_result(directory, name, result):
    arrays_path = directory / f"{name}-arrays.npz"
    np.savez_compressed(arrays_path, **result["arrays"])
    summary_path = directory / f"{name}-summary.json"
    summary_path.write_text(json.dumps(result["summary"], indent=2, allow_nan=False) + "\n")
    return {
        "arrays": {"path": str(arrays_path), "sha256": digest(arrays_path)},
        "summary": {"path": str(summary_path), "sha256": digest(summary_path)},
    }


def figure(directory, result):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frames = result["summary"]["control_trace"]["frames"]
    t = [f["frame_start_ns"] * 1e-6 for f in frames]
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), constrained_layout=True)
    fig.suptitle(
        "Camera control runtime\nAuthored spectral step at 240 ms · declared latency model",
        fontsize=14,
    )
    ax = axes[0, 0]
    ax.plot(t, [f["statistics"]["ae"]["value"] for f in frames], color="#2563eb", marker=".")
    ax.axhline(0.18, color="#b45309", linestyle="--", label="AE target 0.18")
    ax.set(title="Observed RAW green mean", ylabel="Normalized ADC signal")
    ax.legend()
    ax = axes[0, 1]
    ax.step(
        t, [f["applied"]["exposure_ns"] * 1e-6 for f in frames], where="post", label="Exposure [ms]"
    )
    ax.step(t, [f["applied"]["gain"] for f in frames], where="post", label="Readout gain [1]")
    ax.set(title="Applied acquisition settings", ylabel="Declared units in legend")
    ax.legend()
    ax = axes[1, 0]
    for channel, color in enumerate(["#dc2626", "#16a34a", "#2563eb"]):
        ax.step(
            t,
            [f["applied"]["wb_gains_rgb"][channel] for f in frames],
            where="post",
            color=color,
            label="RGB"[channel],
        )
    ax.set(title="Applied white-balance gains", ylabel="Channel multiplier")
    ax.legend()
    ax = axes[1, 1]
    linear = result["arrays"]["white_balanced_rgb_frames"].mean(axis=(1, 2))
    for channel, color in enumerate(["#dc2626", "#16a34a", "#2563eb"]):
        ax.plot(t, linear[:, channel], color=color, label="RGB"[channel])
    ax.set(title="Actual output after WB, before CCM/tone", ylabel="Linear normalized value")
    ax.legend()
    for ax in axes.flat:
        ax.axvline(240, color="#64748b", linestyle=":", alpha=0.8)
        ax.set_xlabel("Declared frame time [ms]")
        ax.grid(alpha=0.15)
    path = directory / "closed-loop-response.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return {"path": str(path), "sha256": digest(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/camera_platform/closed-loop-development")
    )
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    environment = environment_fingerprint()
    request = scenario()
    runtime = ClosedLoopSceneRuntime()
    controlled = runtime.execute(request)
    fixed_request = deepcopy(request)
    fixed_request["control_program"]["policy"]["ae"]["enabled"] = False
    fixed_request["control_program"]["policy"]["awb"]["enabled"] = False
    fixed = runtime.execute(fixed_request)
    awb_request = deepcopy(fixed_request)
    awb_request["control_program"]["initial"]["exposure_ns"] = 8_000_000
    awb_fixed = runtime.execute(awb_request)
    awb_request["control_program"]["policy"]["awb"]["enabled"] = True
    awb = runtime.execute(awb_request)
    commands = controlled["summary"]["control_trace"]["commands"]
    frames = controlled["summary"]["control_trace"]["frames"]
    checks = {
        "48_actual_raw_and_rgb_frames": controlled["arrays"]["raw_frames"].shape == (48, 16, 24)
        and controlled["arrays"]["rgb_frames"].shape == (48, 16, 24, 3),
        "ae_changes_collected_photons": not np.array_equal(
            controlled["arrays"]["channel_electrons"], fixed["arrays"]["channel_electrons"]
        ),
        "ae_changes_raw": not np.array_equal(
            controlled["arrays"]["raw_frames"], fixed["arrays"]["raw_frames"]
        ),
        "ae_recovers_after_light_step": abs(frames[-1]["statistics"]["ae"]["value"] - 0.18) < 0.012,
        "awb_preserves_raw": np.array_equal(
            awb["arrays"]["raw_frames"], awb_fixed["arrays"]["raw_frames"]
        ),
        "awb_changes_rgb": not np.array_equal(
            awb["arrays"]["rgb_frames"], awb_fixed["arrays"]["rgb_frames"]
        ),
        "applied_commands_respect_source_and_deadline": all(
            c["applied_frame_index"] > c["source_frame_index"]
            and c["applied_frame_index"] >= c["earliest_frame_index"]
            and c["applied_at_ns"] >= c["actuator_ready_ns"] >= c["issued_at_ns"]
            for c in commands
            if c["status"] == "applied"
        ),
        "no_hardware_or_physical_claim": controlled["summary"]["physical_validation"]
        == "not_established"
        and controlled["summary"]["control_trace"]["latency_evidence"]
        == "declared_simulation_not_hardware_observation",
        "frozen_source": environment_fingerprint() == environment,
    }
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request, indent=2, allow_nan=False) + "\n")
    artifacts = {
        name: write_result(directory, name, output)
        for name, output in [
            ("controlled", controlled),
            ("fixed", fixed),
            ("awb-only", awb),
            ("awb-fixed", awb_fixed),
        ]
    }
    artifacts["figure"] = figure(directory, controlled)
    report = {
        "schema": "camera_closed_loop_development_verification_v1",
        "status": "passed" if all(checks.values()) else "failed",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "verifier_sha256": digest(Path(__file__)),
        "delivery": "development_python_runtime_only; not installed workbench",
        "environment": environment,
        "checks": checks,
        "request": {"path": str(request_path), "sha256": digest(request_path)},
        "artifacts": artifacts,
        "observed": {
            "first_raw_green_mean": frames[0]["statistics"]["ae"]["value"],
            "last_raw_green_mean": frames[-1]["statistics"]["ae"]["value"],
            "first_applied": frames[0]["applied"],
            "last_applied": frames[-1]["applied"],
            "command_count": len(commands),
        },
        "remaining": [
            "immutable policy/program registry and full model-domain preflight",
            "API/SDK/worker, replay and validation condition integration",
            "workbench controls/traces, installed package and browser verification",
            "full original platform scope; physical validation not established",
        ],
    }
    path = directory / "verification.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": checks,
                "report": str(path),
                "observed": report["observed"],
            },
            indent=2,
        )
    )
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

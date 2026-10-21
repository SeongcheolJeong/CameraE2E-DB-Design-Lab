"""Generate reproducible RAW/ISP boundary and static AE evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from pyisetcam import (
    camera_get,
    camerae2e_run_scenario,
    image_sensor_db_config,
    scene_adjust_illuminant,
    scene_adjust_luminance,
    scene_create,
)


def verify(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    scene = scene_create("uniform ee", 16)
    scene = scene_adjust_illuminant(scene, "D65")
    scene = scene_adjust_luminance(scene, 100)
    base = image_sensor_db_config("DEF-2011-804", model="db_estimate_v1")["scenario"]

    def capture(gain=1, bits=12, black=64, sensor_code="DEF-2011-804"):
        scenario = image_sensor_db_config(sensor_code, model="db_estimate_v1")["scenario"]
        return camerae2e_run_scenario({
            **scenario,
            "sensor": {"noise_flag": 0, "integration_time": 0.001},
            "readout": {"gain": gain, "bit_depth": bits, "black_level_dn": black},
        }, scene=scene, seed=17, include_arrays=False)

    baseline = capture()
    double = capture(gain=2)
    coarse = capture(bits=2, black=0)
    fine = capture(black=0)
    other = capture(sensor_code="DEF-1807-801")
    sensors = [camera_get(r["camera"], "sensor") for r in (baseline, double, other)]
    ips = [camera_get(r["camera"], "ip") for r in (coarse, fine)]
    ae = camerae2e_run_scenario({
        **base,
        "sensor": {"noise_flag": 0},
        "hw_isp": {
            "enabled": True, "nframes": 16,
            "exposure_time_us": 1000,
            "control_path": {
                "apply_to_image": True, "ae_enabled": True, "awb_enabled": False,
                "ae_apply_delay_frames": 1, "target_luma": 0.18,
            },
        },
    }, scene=scene, seed=17, include_arrays=False)
    frames = []
    for frame in ae["hw_isp_sequence"].frames:
        controls = frame.controls_applied
        stats = controls["produced_stats"]["ae"]
        frames.append({
            "frame": frame.timeline.frame_id,
            "exposure_us": controls["applied_controls"]["exposure_time_us"],
            "gain": controls["applied_controls"]["analog_gain"],
            "metering_luma": stats["metering_luma"], "ev_error": stats["ev_error"],
        })
    checks = {
        "gain_preserves_original_photocharge": bool(np.array_equal(
            sensors[0].data["mean_electrons"], sensors[1].data["mean_electrons"],
        )),
        "gain_doubles_reference_code_above_black": bool(np.allclose(
            sensors[1].data["reference_dn"] - 64,
            2 * (sensors[0].data["reference_dn"] - 64),
        )),
        "adc_changes_isp_output": not np.array_equal(ips[0].data["result"], ips[1].data["result"]),
        "db_structure_changes_photocharge": not np.allclose(
            sensors[0].data["mean_electrons"], sensors[2].data["mean_electrons"],
        ),
        "ae_reduces_target_error": abs(frames[-1]["ev_error"]) < abs(frames[0]["ev_error"]),
        "ae_settles_within_quarter_stop": all(abs(f["ev_error"]) <= 0.25 for f in frames[-2:]),
    }
    result = {
        "schema_version": "camerae2e_static_sensor_verification_v1",
        "estimate_only": True, "measured_accuracy_verified": False,
        "stimulus": {"illuminant": "D65", "mean_scene_luminance_cd_m2": 100},
        "checks": checks, "ok": all(checks.values()),
        "imx462_mean_photoelectrons": float(sensors[0].data["mean_electrons"].mean()),
        "imx307_mean_photoelectrons": float(sensors[2].data["mean_electrons"].mean()),
        "adc_isp_max_abs_difference": float(np.max(np.abs(
            ips[0].data["result"] - ips[1].data["result"],
        ))),
        "ae_frames": frames,
        "capture": baseline["sensor_capture"],
    }
    (output_dir / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.5), layout="constrained")
    axes[0].plot([f["frame"] for f in frames], [f["metering_luma"] for f in frames], "o-")
    axes[0].axhline(0.18, color="gray", linestyle="--", label="Target 0.18")
    axes[0].set(xlabel="Frame", ylabel="RAW metering level", title="Static AE convergence")
    axes[0].legend()
    axes[1].plot([f["frame"] for f in frames], [f["ev_error"] for f in frames], "o-")
    axes[1].axhspan(-0.25, 0.25, color="green", alpha=0.12)
    axes[1].set(xlabel="Frame", ylabel="Target error (EV)", title="Exposure/gain feedback")
    fig.savefig(output_dir / "ae_convergence.png", dpi=140)
    plt.close(fig)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/db_sensor_e2e/verification"))
    args = parser.parse_args()
    result = verify(args.output)
    print(json.dumps({"ok": result["ok"], "checks": result["checks"], "output": str(args.output)}))
    raise SystemExit(0 if result["ok"] else 1)

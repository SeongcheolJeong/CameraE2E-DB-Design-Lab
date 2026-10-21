"""Save an authored moving-image/PWM phase counterexample from actual RAW/RGB.

This pure-runtime verifier writes only its chosen report directory. It does not
connect to the installed Workbench, a registry, a sensor or a measurement device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np

from camera_platform.hdr_runtime import HDRBracketRuntime, default_capture_program
from camera_platform.service import environment_fingerprint
from camera_platform.temporal import request_template


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    request = request_template()
    request["noise_enabled"] = False
    request["psf"]["values"] = np.ones((5, 1, 1)).tolist()
    request["lens"]["relative_illumination"] = [[1, 1]] * 5
    request["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    request["capture_program"] = default_capture_program(request["readout"])
    request["capture_program"]["hdr_merge"] = None
    control = request["control_sequence"]
    control["mode"].update(shutter="global", line_time_ns=0)
    for frame in control["frames"]:
        frame["applied"]["exposure_ns"] = 2_000_000
    request["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["clock"]["clock_id"], "period_ns": 1_000_000,
        "on_time_ns": 500_000, "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    shifted = deepcopy(request)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] = 500_000
    first = HDRBracketRuntime().execute(request)
    second = HDRBracketRuntime().execute(shifted)
    names = ("raw_frames", "rgb_frames", "mean_electrons", "waveform_row_effective_s",
             "row_start_ns", "row_end_ns")
    output_arrays = {
        prefix + name: result["arrays"][name]
        for prefix, result in (("phase_0_", first), ("phase_half_", second))
        for name in names
    }
    artifact = args.output / "arrays.npz"
    np.savez_compressed(artifact, **output_arrays)
    inputs = args.output / "requests.json"
    inputs.write_text(json.dumps({"phase_0": request, "phase_half": shifted},
                                default=json_value, allow_nan=False) + "\n")
    a, b = first["arrays"], second["arrays"]
    report = {
        "schema": "camera_motion_pwm_counterexample_v1",
        "source_kind": "estimated",
        "source": "authored moving-step irradiance timeline; not physical measurements",
        "environment": environment_fingerprint(),
        "physical_validation": "not_established",
        "equal_effective_row_seconds": bool(np.array_equal(
            a["waveform_row_effective_s"], b["waveform_row_effective_s"])),
        "max_charge_delta_e": float(np.max(np.abs(a["mean_electrons"] - b["mean_electrons"]))),
        "raw_changed_samples": int(np.count_nonzero(a["raw_frames"] != b["raw_frames"])),
        "rgb_changed_samples": int(np.count_nonzero(a["rgb_frames"] != b["rgb_frames"])),
        "raw_samples": int(a["raw_frames"].size),
        "raw_array_bytes_sha256": {
            "phase_0": hashlib.sha256(a["raw_frames"].tobytes(order="C")).hexdigest(),
            "phase_half": hashlib.sha256(b["raw_frames"].tobytes(order="C")).hexdigest(),
        },
        "array_hash_scope": "C-order native RAW array bytes; container hash is separate",
        "artifacts": {
            path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "bytes": path.stat().st_size}
            for path in (artifact, inputs)
        },
        "conclusion": "Equal ON duration across whole PWM periods does not establish "
                      "equal time-weighted irradiance for a moving declared image.",
        "limitations": "Authored linear image interpolation is a crossfade. "
                       "No source motion reconstruction, hardware LFM or measured accuracy.",
    }
    assert report["equal_effective_row_seconds"]
    assert report["max_charge_delta_e"] > 0
    assert report["raw_changed_samples"] > 0 and report["rgb_changed_samples"] > 0
    report["status"] = "passed"
    path = args.output / "verification.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: report[key] for key in (
        "status", "equal_effective_row_seconds", "max_charge_delta_e",
        "raw_changed_samples", "rgb_changed_samples", "raw_samples",
    )}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Verify a sealed saved LFM fixture against four temporal-engine phase runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from camera_platform.lens_sensor_hdr_bridge import _prepare_lens_sensor_capture
from camera_platform.lfm_temporal_parity import (
    crosscheck_pwm_four_phase_set,
    load_verified_lfm_artifact,
)


def _seconds_to_ns(value: object, name: str) -> int:
    if type(value) not in (int, float):
        raise ValueError(f"Saved {name} must be numeric seconds.")
    scaled = float(value) * 1e9
    rounded = round(scaled)
    if rounded < 0 or abs(scaled - rounded) > 1e-6:
        raise ValueError(f"Saved {name} is not representable as integer nanoseconds.")
    return rounded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lfm-report", type=Path, required=True)
    parser.add_argument("--lfm-npz", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--root", type=Path, required=True, help="CameraE2E DB root")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--exposure-index", type=int, choices=(0, 1), default=0)
    parser.add_argument("--samples-per-exposure", type=int, nargs=2, default=(128, 512))
    parser.add_argument("--maximum-relative-error", type=float, default=0.003)
    parser.add_argument("--allow-numerical-negative-photons", action="store_true")
    parser.add_argument("--accept-approximate-lens-radiometry", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not arguments.accept_approximate_lens_radiometry:
        parser.error("Explicit approximate lens-radiometry acceptance is required.")
    source = load_verified_lfm_artifact(
        arguments.lfm_report, arguments.lfm_npz,
        expected_report_sha256=arguments.report_sha256,
    )
    report = source["report"]
    controls = report["controls"]
    if report.get("psf_compute_mode") != "bounded_streaming":
        raise ValueError("Pinned parity requires a bounded saved geometric PSF run.")
    exposures_ns = [_seconds_to_ns(value, "exposure_s") for value in controls["exposure_s"]]
    period_ns = _seconds_to_ns(controls["pwm_period_s"], "pwm_period_s")
    on_time_ns = _seconds_to_ns(
        controls["pwm_period_s"] * controls["duty_cycle"], "PWM ON duration"
    )
    line_time_ns = _seconds_to_ns(controls["line_time_s"], "line_time_s")
    lens_revision_id = report["lens_identity"]["revision_id"]
    sensor_revision = report["sensor_revision"]
    prepared = _prepare_lens_sensor_capture(
        lens_revision_id,
        sensor_revision["report_code"],
        sensor_run_id=sensor_revision["run_id"],
        root=arguments.root,
        workspace=arguments.workspace,
        exposure_s=exposures_ns[-1] / 1e9,
        scene_kind="uniform",
        sensor_size=tuple(controls["sensor_size"]),
        luminance_cd_m2=controls["on_luminance_cd_m2"],
        seed=controls["seed"],
        accept_approximate_lens_radiometry=True,
        psf_compute_mode="bounded_streaming",
    )
    if (
        prepared["lens_identity"] != report["lens_identity"]
        or prepared["sensor_revision"] != sensor_revision
        or prepared["upstream_assets"] != report["upstream_assets"]
        or prepared["saved_readout"] != controls["saved_readout"]
        or prepared["psf_execution"] != report["psf_execution"]
    ):
        raise ValueError("Recomputed Lens/Sensor/PSF source differs from saved LFM bindings.")
    selected = arguments.exposure_index
    sensor = prepared["sensor"].clone()
    sensor.fields["integration_time"] = exposures_ns[selected] / 1e9
    phase_report = report["cases"][selected]["phases"]
    comparison = crosscheck_pwm_four_phase_set(
        sensor,
        prepared["optical_image"],
        period_ns=period_ns,
        on_time_ns=on_time_ns,
        line_time_ns=line_time_ns,
        off_to_on_ratio=controls["off_to_on_ratio"],
        saved_mean_electrons=source["integrated_mean_electrons"][selected],
        phase_seeds=[phase["noise_seed"] for phase in phase_report],
        original_raw_dn=source["original_pwm_raw_dn"][selected],
        samples_per_exposure=arguments.samples_per_exposure,
        maximum_relative_error=arguments.maximum_relative_error,
        allow_numerical_negative_photons=arguments.allow_numerical_negative_photons,
    )
    payload = {
        "schema": "camerae2e_saved_lfm_four_phase_temporal_parity_proof_v1",
        "status": comparison["status"],
        "source": {
            "lfm_report_sha256": source["report_sha256"],
            "lfm_npz_sha256": source["npz_sha256"],
            "original_pwm_raw_sha256": source["original_raw_sha256"],
            "integrated_mean_electrons_sha256": source["integrated_mean_sha256"],
            "lens_identity": prepared["lens_identity"],
            "sensor_revision": prepared["sensor_revision"],
            "upstream_assets": prepared["upstream_assets"],
            "saved_readout": prepared["saved_readout"],
            "recomputed_on_optical_photons_sha256": hashlib.sha256(
                np.ascontiguousarray(prepared["optical_image"].data["photons"])
                .tobytes(order="C")
            ).hexdigest(),
            "saved_lfm_artifact_compared": True,
            "source_raw_phase_hashes_reverified": True,
            "original_optical_image_bytes_reverified": False,
            "exposure_index": selected,
            "exposure_ns": exposures_ns[selected],
            "timing_provenance": controls["timing_provenance"],
        },
        "comparison": comparison,
        "physical_validation": "not_established",
        "product_hdr_lfm_decision": "not_established",
    }
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if arguments.output is None:
        print(rendered)
    else:
        destination = arguments.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as output:
            output.write(rendered + "\n")
        print(destination)


if __name__ == "__main__":
    main()
